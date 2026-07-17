# routes/tts_routes.py
"""
TTS API routes — multi-provider (local Kokoro, API endpoint, browser),
plus "My Voices": the user's cloned-voice samples for the Chatterbox engine.
"""

import logging
import os
import re
import subprocess

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from src.auth_helpers import require_privilege

logger = logging.getLogger(__name__)

_VOICE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 _-]{0,38}$")
_VOICE_UPLOAD_MAX = 25 * 1024 * 1024
_VOICE_SRC_EXTS = ("wav", "mp3", "flac", "ogg", "opus", "m4a", "webm")


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

def setup_tts_routes(tts_service):
    """Setup TTS routes with the provided TTS service"""
    router = APIRouter(prefix="/api/tts", tags=["tts"])

    @router.get("/stats")
    async def get_tts_stats():
        """Get TTS service statistics"""
        try:
            return tts_service.get_stats()
        except Exception as e:
            logger.error(f"Failed to get TTS stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/voices")
    async def list_voices(provider: str = ""):
        """Voice catalog for a provider (drives the settings picker). The
        picker asks for the provider it is ABOUT to switch to, which may not
        be saved yet — hence the query param over the stored setting."""
        try:
            return {"voices": tts_service.list_voices(provider or None)}
        except Exception as e:
            logger.error(f"Failed to list TTS voices: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/synthesize")
    async def synthesize_speech(request: TTSRequest):
        """Synthesize speech from text"""
        try:
            # A cloned voice needs no working saved provider — synthesize()
            # auto-routes it to the Chatterbox endpoint. Without this bypass,
            # provider 'local' minus the optional kokoro package would 503 a
            # voice the catalog just advertised.
            if not tts_service.available and not tts_service.is_routable_cloned_voice(request.voice):
                raise HTTPException(
                    status_code=503,
                    detail={"message": "TTS service not available"}
                )
            
            if request.format == "base64":
                audio_b64 = tts_service.synthesize_to_base64(request.text, voice=request.voice)
                if not audio_b64:
                    raise HTTPException(
                        status_code=500,
                        detail={"message": "Synthesis failed"}
                    )
                return {"audio": audio_b64}

            else:  # audio format
                audio_data = tts_service.synthesize(request.text, voice=request.voice)
                if not audio_data:
                    raise HTTPException(
                        status_code=500,
                        detail={"message": "Synthesis failed"}
                    )
                
                # Detect format from magic bytes (MP3: ID3 tag or sync word ff e0+)
                is_mp3 = audio_data[:3] == b'ID3' or (len(audio_data) >= 2 and audio_data[0] == 0xff and (audio_data[1] & 0xe0) == 0xe0)
                mime = "audio/mpeg" if is_mp3 else "audio/wav"
                return Response(
                    content=audio_data,
                    media_type=mime,
                    headers={
                        "Content-Disposition": "inline; filename=speech.mp3" if "mpeg" in mime else "inline; filename=speech.wav"
                    }
                )
        
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
        """The user's cloned voices (Chatterbox samples in VOICES_DIR)."""
        require_privilege(request, "can_generate_images")
        from services.tts.tts_service import cloned_voice_catalog
        return {"voices": cloned_voice_catalog()}

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
        # A re-recorded voice must invalidate BOTH caches: the Chatterbox
        # server's baked conditionals and Aegis's synthesized-speech cache
        # (its key is voice-name based, so old audio would replay verbatim).
        try:
            os.unlink(f"{dest}.voice.pt")
        except OSError:
            pass
        try:
            tts_service.clear_cache()
        except Exception:
            pass
        return {"ok": True, "voice": slug, "seconds": round(seconds, 1) if seconds else None}

    @router.delete("/my-voices/{name}")
    async def delete_my_voice(request: Request, name: str):
        require_privilege(request, "can_generate_images")
        from src.constants import VOICES_DIR
        slug = _voice_slug(name)
        removed = False
        for suffix in (".wav", ".mp3", ".flac", ".ogg", ".wav.voice.pt", ".mp3.voice.pt", ".flac.voice.pt", ".ogg.voice.pt"):
            p = os.path.join(VOICES_DIR, f"{slug}{suffix}")
            if os.path.exists(p):
                try:
                    os.unlink(p)
                    removed = removed or not suffix.endswith(".voice.pt")
                except OSError:
                    logger.warning("Could not delete voice file %s", p)
        if not removed:
            raise HTTPException(404, f"No cloned voice named '{slug}'")
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
