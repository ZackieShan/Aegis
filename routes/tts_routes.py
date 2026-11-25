# routes/tts_routes.py
"""
TTS API routes — multi-provider (local Kokoro, API endpoint, browser),
plus "My Voices": the user's cloned-voice samples for the Chatterbox engine,
and reading answers aloud: each person's voice, and speech made a piece at a time
(services/tts/reading.py).
"""

import asyncio
import logging
import os
import re
import subprocess
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from src.auth_helpers import effective_user, require_privilege

logger = logging.getLogger(__name__)

_VOICE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 _-]{0,38}$")
_VOICE_UPLOAD_MAX = 25 * 1024 * 1024
_VOICE_SRC_EXTS = ("wav", "mp3", "flac", "ogg", "opus", "m4a", "webm")
# What the cloning engines keep beside a sample once they have read the voice from it
# (Chatterbox, Chatterbox Turbo, Qwen3-TTS): made again from the sample whenever it is missing.
_BAKED_SUFFIXES = (".voice.pt", ".turbo.pt", ".qwen3.pt")


def _voice_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")


def _convert_to_wav(src_bytes: bytes, src_ext: str, dest_path: str) -> float:
    """Any browser-recorded/uploaded audio → 24 kHz mono WAV (what Chatterbox
    reads best; also makes webm/opus mic blobs usable — soundfile can't decode
    those). Returns the duration in seconds."""
    import tempfile

    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.NamedTemporaryFile(suffix=f".{src_ext}", delete=False) as tmp:
        tmp.write(src_bytes)
        tmp_path = tmp.name
    try:
        r = subprocess.run(
            [exe, "-y", "-i", tmp_path, "-ac", "1", "-ar", "24000", "-vn", dest_path],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0 or not os.path.exists(dest_path):
            raise ValueError(f"Could not decode that audio ({(r.stderr or '').strip()[-200:]})")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
        probe = subprocess.run([exe, "-i", dest_path], capture_output=True, text=True, timeout=30)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr or "") or m
        return (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

class TTSRequest(BaseModel):
    text: str
    format: str = "audio"  # "audio" or "base64"
    voice: str = ""        # override the saved voice (settings preview)


class SayRequest(BaseModel):
    text: str
    voice: str = ""        # a preview of this voice; otherwise the person's own choice


class ReadingChoice(BaseModel):
    voice: Optional[str] = None     # "device", a voice's id, or "" for the house's
    auto: Optional[bool] = None     # read every answer without being asked


class ShareVoice(BaseModel):
    shared: bool


def _who(request: Request):
    """(username, is an administrator) for whoever is asking."""
    user = effective_user(request)
    if not user:
        return user, True                       # no accounts on this server
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    try:
        return user, bool(auth_mgr.is_admin(user)) if auth_mgr is not None else False
    except Exception:
        return user, False


def _audio_response(audio_data: bytes, extra: Optional[dict] = None) -> Response:
    # Detect format from magic bytes (MP3: ID3 tag or sync word ff e0+)
    is_mp3 = audio_data[:3] == b'ID3' or (len(audio_data) >= 2 and audio_data[0] == 0xff and (audio_data[1] & 0xe0) == 0xe0)
    headers = {"Content-Disposition": "inline; filename=speech.mp3" if is_mp3 else "inline; filename=speech.wav"}
    headers.update(extra or {})
    return Response(content=audio_data, media_type="audio/mpeg" if is_mp3 else "audio/wav", headers=headers)


def setup_tts_routes(tts_service):
    """Setup TTS routes with the provided TTS service"""
    router = APIRouter(prefix="/api/tts", tags=["tts"])

    # ── reading answers aloud ─────────────────────────────────────────────
    @router.get("/reading")
    async def reading_choices(request: Request):
        """What this person can be read to in, what they chose, and how each voice keeps pace."""
        from services.tts import reading
        user, is_admin = _who(request)
        voices = await asyncio.to_thread(reading.choices, tts_service, user, is_admin)
        chosen = reading.prefs(user)
        return {"voices": voices, "voice": chosen["voice"], "auto": chosen["auto"],
                "reads_in": await asyncio.to_thread(reading.resolve, tts_service, user, is_admin),
                "house": reading.house_voice(tts_service), "piece_chars": reading.PIECE_CHARS}

    @router.put("/reading")
    async def choose_reading(request: Request, choice: ReadingChoice):
        from services.tts import reading
        user, is_admin = _who(request)
        if choice.voice not in (None, "", reading.DEVICE):
            allowed = {v["id"] for v in await asyncio.to_thread(reading.choices, tts_service, user, is_admin)}
            if choice.voice not in allowed:
                raise HTTPException(403, "That voice is not yours to use. Its owner can share it from the Studio.")
        chosen = reading.save_prefs(user, voice=choice.voice, auto=choice.auto)
        return {**chosen, "reads_in": await asyncio.to_thread(reading.resolve, tts_service, user, is_admin)}

    @router.post("/say")
    async def say_a_piece(request: Request, body: SayRequest):
        """One piece of an answer as speech, in this person's voice. The screen asks for an answer a
        sentence or two at a time so it can start with the first while the rest is being made. A slow
        engine takes seconds over each piece; the work runs off the event loop so nobody else waits."""
        from services.tts import reading
        user, is_admin = _who(request)
        voice = await asyncio.to_thread(reading.resolve, tts_service, user, is_admin, body.voice)
        if body.voice and body.voice != reading.DEVICE and voice != body.voice:
            raise HTTPException(403, "That voice is not yours to use.")
        if voice == reading.DEVICE:
            # Nothing on the server to read to this person with: the screen uses the device's own voice.
            raise HTTPException(409, detail={"code": "device", "message": "There is no voice on the server for you; this device's own voice is used."})
        made = await asyncio.to_thread(reading.say, tts_service, body.text, voice)
        if not made["audio"]:
            raise HTTPException(503, detail={"code": "no_speech", "message": "The voice could not be made just now."})
        return _audio_response(made["audio"], {"X-Speech-Seconds": str(made["seconds"]), "X-Took-Seconds": str(made["took"]),
                                              "X-Voice": voice, "X-Pace": str(reading.pace_for(voice))})

    @router.post("/warm")
    async def warm_the_voice(request: Request):
        """The answer on its way will be read aloud: have this person's voice loaded by then. The
        engine behind a recorded voice is put away after a few idle minutes and takes seconds to
        come back; asked for as the question is sent, it is back before the first sentence is
        written. Nothing happens for a device's own voice, or for a voice that spoke a moment ago."""
        from services.tts import reading
        user, is_admin = _who(request)
        voice = await asyncio.to_thread(reading.resolve, tts_service, user, is_admin)
        if voice == reading.DEVICE:
            return {"warming": False}
        return {"warming": await asyncio.to_thread(reading.warm, tts_service, voice)}

    @router.get("/stats")
    async def get_tts_stats():
        """Get TTS service statistics"""
        try:
            return tts_service.get_stats()
        except Exception as e:
            logger.error(f"Failed to get TTS stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/voices")
    async def list_voices(request: Request, provider: str = ""):
        """Voice catalog for a provider (drives the settings picker). The
        picker asks for the provider it is ABOUT to switch to, which may not
        be saved yet — hence the query param over the stored setting.
        A recorded voice is listed only for someone who may use it."""
        try:
            from services.tts import reading
            from services.tts.tts_service import cloned_voice_catalog
            user, is_admin = _who(request)
            recorded = {v.get("id") for v in cloned_voice_catalog()}
            voices = [v for v in (tts_service.list_voices(provider or None) or [])
                      if v.get("id") not in recorded or reading.may_use(v.get("id"), user, is_admin)]
            return {"voices": voices}
        except Exception as e:
            logger.error(f"Failed to list TTS voices: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/synthesize")
    async def synthesize_speech(request: TTSRequest, http_request: Request):
        """Synthesize speech from text"""
        try:
            from services.tts import reading
            from services.tts.tts_service import cloned_voice_catalog
            user, is_admin = _who(http_request)
            # A recorded voice is a person's: it speaks for whoever owns it, or for the household
            # once shared. That holds for the voice named here and for the house's saved voice,
            # which this endpoint falls back to.
            recorded = {v.get("id") for v in cloned_voice_catalog()}
            voice = (request.voice or "").strip()
            if voice in recorded and not reading.may_use(voice, user, is_admin):
                raise HTTPException(status_code=403, detail={"message": "That voice is not yours to use."})
            if not voice:
                saved = str(tts_service._load_settings().get("tts_voice") or "")
                if saved in recorded and not reading.may_use(saved, user, is_admin):
                    raise HTTPException(status_code=503, detail={"message": "No voice is set up on the server for you."})
            # A cloned voice needs no working saved provider — synthesize()
            # auto-routes it to the Chatterbox endpoint. Without this bypass,
            # provider 'local' minus the optional kokoro package would 503 a
            # voice the catalog just advertised.
            if not tts_service.available and not tts_service.is_routable_cloned_voice(
                    voice or str(tts_service._load_settings().get("tts_voice") or "")):
                raise HTTPException(
                    status_code=503,
                    detail={"message": "TTS service not available"}
                )

            # Making speech can take many seconds on a slow engine: off the event loop, or the
            # whole app stands still for everyone until it is done.
            if request.format == "base64":
                audio_b64 = await asyncio.to_thread(tts_service.synthesize_to_base64, request.text, voice or None)
                if not audio_b64:
                    raise HTTPException(
                        status_code=500,
                        detail={"message": "Synthesis failed"}
                    )
                return {"audio": audio_b64}

            else:  # audio format
                audio_data = await asyncio.to_thread(tts_service.synthesize, request.text, True, voice or None)
                if not audio_data:
                    raise HTTPException(
                        status_code=500,
                        detail={"message": "Synthesis failed"}
                    )
                return _audio_response(audio_data)
        
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Synthesis error: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={"message": f"Synthesis failed: {str(e)}"}
            )

    @router.get("/my-voices")
    async def my_voices(request: Request):
        """The recorded voices this person may use (Chatterbox samples in VOICES_DIR): their own, and
        the ones shared with the household. Each says whose it is and whether this person may change it."""
        require_privilege(request, "can_generate_images")
        from services.tts import reading
        user, is_admin = _who(request)
        return {"voices": reading.cloned_voices(user, is_admin)}

    @router.put("/my-voices/{name}")
    async def share_my_voice(request: Request, name: str, body: ShareVoice):
        """Share a voice with everyone in the household, or take it back. Its owner's to decide (or an administrator's)."""
        require_privilege(request, "can_generate_images")
        from services.tts import reading
        from services.tts.tts_service import cloned_voice_catalog
        user, is_admin = _who(request)
        slug = _voice_slug(name)
        if not any(v.get("id") == slug for v in cloned_voice_catalog()):
            raise HTTPException(404, f"No cloned voice named '{slug}'")
        if not reading.may_change(slug, user, is_admin):
            raise HTTPException(403, "Only the person who recorded a voice can share it.")
        if not reading.record(slug).get("owner"):
            reading.claim(slug, user)               # a voice from before owners were kept: whoever takes charge of it owns it
        kept = reading.share(slug, body.shared)
        return {"ok": True, "voice": slug, "shared": bool(kept.get("shared"))}

    @router.post("/my-voices")
    async def add_my_voice(request: Request, name: str = Form(...), file: UploadFile = File(...)):
        """Clone a voice: save a ~10s speech sample under a name.

        Any format the mic or a file picker produces (webm/opus included) is
        converted to 24 kHz mono WAV — the shape Chatterbox conditions on.
        Chatterbox only reads the first ~10s, so long uploads are fine.
        Conversion goes through a temp name and only replaces an existing
        voice after validation — a botched re-record must never destroy the
        previous good sample."""
        require_privilege(request, "can_generate_images")
        from src.constants import VOICES_DIR
        from src.upload_limits import read_upload_limited

        if not _VOICE_NAME_RE.fullmatch(name or ""):
            raise HTTPException(400, "Voice name: 1-39 letters/numbers/spaces/dashes.")
        slug = _voice_slug(name)
        if not slug:
            raise HTTPException(400, "Voice name must contain letters or numbers.")
        from services.tts import reading
        user, is_admin = _who(request)
        from src.constants import VOICES_DIR as _voices_dir
        if os.path.exists(os.path.join(_voices_dir, f"{slug}.wav")) and not reading.may_change(slug, user, is_admin):
            raise HTTPException(403, "There is already a voice with that name, and it is someone else's. Pick another name.")
        ext = (file.filename or "").rsplit(".", 1)[-1].lower()
        if ext not in _VOICE_SRC_EXTS:
            raise HTTPException(400, f"Unsupported audio type '.{ext}'.")
        data = await read_upload_limited(file, _VOICE_UPLOAD_MAX, "Voice sample")
        if len(data) < 1024:
            raise HTTPException(400, "That sample looks empty.")

        os.makedirs(VOICES_DIR, exist_ok=True)
        dest = os.path.join(VOICES_DIR, f"{slug}.wav")
        # ffmpeg picks the container from the extension, and the voice
        # catalogs glob VOICES_DIR/*.wav — a .tmp subdir satisfies both.
        tmp_dir = os.path.join(VOICES_DIR, ".tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_dest = os.path.join(tmp_dir, f"{slug}.wav")
        try:
            seconds = _convert_to_wav(data, ext, tmp_dest)
            if seconds and seconds < 3:
                raise HTTPException(400, f"Sample is only {seconds:.1f}s — record at least 3s (about 10s is ideal).")
            os.replace(tmp_dest, dest)
        except ValueError as e:
            raise HTTPException(400, str(e))
        finally:
            try:
                os.unlink(tmp_dest)
            except OSError:
                pass
        # A re-recorded voice must invalidate BOTH caches: what each engine
        # prepared from the old sample and Aegis's synthesized-speech cache
        # (its key is voice-name based, so old audio would replay verbatim).
        for baked in _BAKED_SUFFIXES:
            try:
                os.unlink(f"{dest}{baked}")
            except OSError:
                pass
        try:
            tts_service.clear_cache()
        except Exception:
            pass
        # It is the voice of whoever recorded it, and reads only to them until they share it.
        try:
            reading.claim(slug, user)
        except Exception:
            logger.warning("Could not note the owner of voice '%s'", slug, exc_info=True)
        return {"ok": True, "voice": slug, "seconds": round(seconds, 1) if seconds else None}

    @router.delete("/my-voices/{name}")
    async def delete_my_voice(request: Request, name: str):
        require_privilege(request, "can_generate_images")
        from src.constants import VOICES_DIR
        from services.tts import reading
        slug = _voice_slug(name)
        user, is_admin = _who(request)
        if not reading.may_change(slug, user, is_admin):
            raise HTTPException(403, "Only the person who recorded a voice can delete it.")
        removed = False
        samples = (".wav", ".mp3", ".flac", ".ogg")
        # The recording, and with it everything an engine prepared from it: a voice that is deleted
        # leaves nothing of the person's sound behind.
        for suffix in samples + tuple(f"{sample}{baked}" for sample in samples for baked in _BAKED_SUFFIXES):
            p = os.path.join(VOICES_DIR, f"{slug}{suffix}")
            if os.path.exists(p):
                try:
                    os.unlink(p)
                    removed = removed or suffix in samples
                except OSError:
                    logger.warning("Could not delete voice file %s", p)
        if not removed:
            raise HTTPException(404, f"No cloned voice named '{slug}'")
        try:
            reading.forget(slug)
        except Exception:
            logger.warning("Could not drop the record of voice '%s'", slug, exc_info=True)
        # Deleting the active voice must not leave read-aloud pointing at a
        # ghost id (no auto-route match → Kokoro rejects it → opaque 500s).
        # Repoint the saved default at something that still speaks.
        try:
            from services.tts.tts_service import cloned_voice_catalog
            from src.settings import load_settings, save_settings
            s = load_settings()
            if s.get("tts_voice") == slug:
                provider = s.get("tts_provider", "disabled")
                if provider == "local":
                    s["tts_voice"] = "af_heart"
                elif provider == "browser":
                    s["tts_voice"] = ""  # OS default voice
                else:
                    remaining = [v["id"] for v in cloned_voice_catalog()]
                    s["tts_voice"] = remaining[0] if remaining else "alloy"
                save_settings(s)
        except Exception:
            logger.warning("Could not repoint tts_voice after deleting '%s'", slug, exc_info=True)
        try:
            tts_service.clear_cache()
        except Exception:
            pass
        return {"ok": True}

    @router.post("/clear-cache")
    async def clear_tts_cache():
        """Clear TTS cache"""
        try:
            tts_service.clear_cache()
            return {"success": True, "message": "Cache cleared"}
        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return router
