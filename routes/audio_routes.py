"""Music generation routes — ACE-Step songs on the ComfyUI engine.

Same job model as video: POST /api/audio/generate returns a job id
immediately, /api/video/status/{id} (the shared render-job registry) is
polled until the MP3 lands in the Studio like any other generation.
"""

import logging
import os
import re
import subprocess
import uuid

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from src.auth_helpers import require_privilege

logger = logging.getLogger(__name__)

_AUDIO_REF_EXTS = ("mp3", "wav", "flac", "ogg", "opus", "m4a")
_AUDIO_REF_MAX_BYTES = 60 * 1024 * 1024
# Names this API hands out for files it wrote into ComfyUI's input dir — the
# generate route only accepts these back, so a caller can never point
# LoadAudio at an arbitrary path.
_AUDIO_REF_NAME_RE = re.compile(r"^aegis_ref_[a-f0-9]{12}\.(?:%s)$" % "|".join(_AUDIO_REF_EXTS))


def _comfy_input_dir() -> str:
    """ComfyUI's input directory (where LoadAudio reads from)."""
    env = os.getenv("COMFYUI_INPUT_DIR")
    if env:
        return env
    from src.engine_tuner import _engine_dir
    return os.path.join(_engine_dir(), "comfyui", "ComfyUI", "input")


_REF_MAX_AGE_S = 24 * 3600


def _gc_stale_references(in_dir: str) -> None:
    """Best-effort prune of aegis_ref_* staging files older than a day —
    every upload and cover request re-stages its own copy, so without this
    the ComfyUI input dir grows by one track per cover forever."""
    import time
    try:
        cutoff = time.time() - _REF_MAX_AGE_S
        for entry in os.scandir(in_dir):
            if entry.is_file() and entry.name.startswith("aegis_ref_") and entry.stat().st_mtime < cutoff:
                try:
                    os.unlink(entry.path)
                except OSError:
                    pass
    except OSError:
        pass


def _probe_audio_seconds(path: str):
    """Duration via the bundled ffmpeg (imageio-ffmpeg) — None if unknowable."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        r = subprocess.run([exe, "-i", path], capture_output=True, text=True, timeout=30)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        logger.debug("audio duration probe failed for %s", path, exc_info=True)
    return None


def _clamp(val, lo, hi, default):
    try:
        v = float(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def setup_audio_routes() -> APIRouter:
    router = APIRouter(tags=["audio"])

    async def _comfy_models() -> list:
        import asyncio
        from src import comfyui_client, comfyui_workflows
        try:
            if not await asyncio.to_thread(comfyui_client.is_up):
                return []
            return [
                {"model": m, "endpoint": "comfyui",
                 "label": comfyui_workflows.COMFY_AUDIO_MODELS[m].get("label", m),
                 "kind": comfyui_workflows.audio_model_kind(m)}
                for m in comfyui_workflows.available_audio_models()
            ]
        except Exception:
            return []

    @router.get("/api/audio/models")
    async def audio_models(request: Request):
        # "models" stays song-only — it feeds the Song Composer's model picker,
        # where a narration model would silently produce spoken tags. Narration
        # availability rides alongside for the Narrate UI.
        entries = await _comfy_models()
        return {
            "models": [e for e in entries if e["kind"] == "song"],
            "narrate": [e for e in entries if e["kind"] == "narration"],
        }

    @router.post("/api/audio/reference")
    async def audio_reference(request: Request, file: UploadFile = File(...)):
        """Stage an uploaded track as a song reference (cover mode).

        The bytes land in ComfyUI's input dir under a server-generated
        aegis_ref_* name — the only names /api/audio/generate accepts back —
        and the response carries the probed duration so the UI can default
        the cover length to the reference's."""
        require_privilege(request, "can_generate_images")
        from src.upload_limits import read_upload_limited

        ext = (file.filename or "").rsplit(".", 1)[-1].lower()
        if ext not in _AUDIO_REF_EXTS:
            raise HTTPException(400, f"Unsupported audio type '.{ext}' — use one of: {', '.join(_AUDIO_REF_EXTS)}")
        data = await read_upload_limited(file, _AUDIO_REF_MAX_BYTES, "Reference audio")
        in_dir = _comfy_input_dir()
        os.makedirs(in_dir, exist_ok=True)
        _gc_stale_references(in_dir)
        name = f"aegis_ref_{uuid.uuid4().hex[:12]}.{ext}"
        path = os.path.join(in_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        seconds = _probe_audio_seconds(path)
        return {"name": name, "seconds": round(seconds, 1) if seconds else None}

    @router.post("/api/audio/generate")
    async def audio_generate(request: Request):
        user = require_privilege(request, "can_generate_images")
        body = await request.json()
        tags = str(body.get("tags") or body.get("prompt") or "").strip()
        if not tags:
            raise HTTPException(400, "Describe the song — genre, mood, tempo, instruments (this is the 'tags' line).")
        lyrics = str(body.get("lyrics") or "").strip()

        # Cover mode: a staged upload (reference_name from /api/audio/reference)
        # or an existing Studio track (reference_id → copied into ComfyUI input).
        reference = str(body.get("reference_name") or "").strip()
        ref_seconds = None
        if reference and not _AUDIO_REF_NAME_RE.fullmatch(reference):
            raise HTTPException(400, "reference_name must come from /api/audio/reference.")
        ref_id = str(body.get("reference_id") or "").strip()
        if ref_id and not reference:
            import shutil
            from sqlalchemy import or_
            from core.database import SessionLocal, GalleryImage
            from src.generated_images import resolve_generated_image_path
            db = SessionLocal()
            try:
                if ref_id.lower() == "last":
                    q = db.query(GalleryImage).filter(GalleryImage.is_active == True)  # noqa: E712
                    q = q.filter((GalleryImage.owner == user) if user else (GalleryImage.owner.is_(None) | (GalleryImage.owner == "")))
                    # Filter to audio IN SQL — a recency window of mixed media
                    # would miss a song older than 50 photos.
                    q = q.filter(or_(*[GalleryImage.filename.ilike(f"%.{e}") for e in _AUDIO_REF_EXTS]))
                    row = q.order_by(GalleryImage.created_at.desc()).first()
                else:
                    row = db.query(GalleryImage).filter(GalleryImage.id == ref_id).first()
            finally:
                db.close()
            if not row or (getattr(row, "owner", "") or "") != (user or ""):
                raise HTTPException(404, "No matching Studio track")
            ext = (row.filename or "").rsplit(".", 1)[-1].lower()
            if ext not in _AUDIO_REF_EXTS:
                raise HTTPException(400, "That Studio item is not an audio track")
            src = resolve_generated_image_path(row.filename)
            in_dir = _comfy_input_dir()
            os.makedirs(in_dir, exist_ok=True)
            _gc_stale_references(in_dir)
            reference = f"aegis_ref_{uuid.uuid4().hex[:12]}.{ext}"
            shutil.copyfile(src, os.path.join(in_dir, reference))
        if reference:
            ref_seconds = _probe_audio_seconds(os.path.join(_comfy_input_dir(), reference))

        # Cover length defaults to the reference's (truncated/padded past it).
        default_seconds = min(600, max(5, ref_seconds)) if ref_seconds else 60
        seconds = _clamp(body.get("seconds", body.get("duration")), 5, 600, default_seconds)
        seed = body.get("seed")
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            seed = -1
        bpm = int(_clamp(body.get("bpm"), 10, 300, 120))

        from src import comfyui_client, comfyui_workflows
        model = str(body.get("model") or "").strip() or "comfy-acestep1.5-song"
        if model not in comfyui_workflows.COMFY_AUDIO_MODELS:
            raise HTTPException(400, f"Unknown audio model '{model}'.")
        if comfyui_workflows.audio_model_kind(model) != "song":
            # Narration builders take script/speaker kwargs, not tags/lyrics —
            # routing one here would TypeError inside the graph builder.
            raise HTTPException(400, f"'{model}' is a narration model — use /api/audio/narrate.")
        served = comfyui_workflows.available_audio_models()
        if model not in served:
            raise HTTPException(400, "The ACE-Step model files are missing from models/audio/.")
        import asyncio
        if not await asyncio.to_thread(comfyui_client.is_up):
            raise HTTPException(502, "ComfyUI isn't running — music renders on the ComfyUI engine.")

        try:
            job_id = await comfyui_client.start_song_job(
                tags=tags,
                lyrics=lyrics,
                model=model,
                owner=user,
                session_id=body.get("session_id"),
                seconds=seconds,
                seed=seed,
                bpm=bpm,
                language=str(body.get("language") or "en"),
                reference_audio=reference,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception:
            logger.exception("audio_generate: submit failed")
            raise HTTPException(502, "Song generation could not be started.")
        return {
            "job_id": job_id, "status": "queued", "model": model,
            "seconds": seconds, "has_lyrics": bool(lyrics),
            "cover": bool(reference),
        }

    @router.post("/api/audio/narrate")
    async def audio_narrate(request: Request):
        """Long-form multi-speaker narration on VibeVoice-Large.

        Body: {"script": "[1]: line\\n[2]: line…", "speakers": {"1": "zac"},
        "seed": n?}. Speaker voices name cloned samples from the Voice Lab
        (data/voices/<slug>.wav — the same store /api/tts/my-voices writes);
        unmapped speakers get a synthetic voice. Returns a job id polled via
        /api/video/status/{id}, same as songs."""
        user = require_privilege(request, "can_generate_images")
        body = await request.json()
        script = str(body.get("script") or body.get("text") or "").strip()
        if not script:
            raise HTTPException(400, "Write the script — '[1]: line' / '[2]: line' speaker labels, up to 4 speakers.")
        if len(script) > 200_000:
            raise HTTPException(400, "Script is too long (200k characters max).")

        from src import comfyui_client, comfyui_workflows
        model = str(body.get("model") or "").strip() or "comfy-vibevoice-narrate"
        if comfyui_workflows.audio_model_kind(model) != "narration":
            raise HTTPException(400, f"'{model}' is not a narration model.")
        if model not in comfyui_workflows.available_audio_models():
            raise HTTPException(400, "The VibeVoice model files are missing from models/vibevoice/.")

        # Speaker → cloned-voice mapping. Slug through the same rule the Voice
        # Lab saves under, and resolve inside VOICES_DIR only — a voice name
        # can never become a path.
        from routes.tts_routes import _voice_slug
        from src.constants import VOICES_DIR
        speakers = body.get("speakers") or {}
        if not isinstance(speakers, dict):
            raise HTTPException(400, "speakers must map speaker numbers to voice names, e.g. {\"1\": \"zac\"}.")
        voice_paths = {}
        for key, name in speakers.items():
            try:
                num = int(key)
            except (TypeError, ValueError):
                raise HTTPException(400, f"Speaker key '{key}' is not a number (use 1-4).")
            if not (1 <= num <= 4):
                raise HTTPException(400, "Speaker numbers run 1-4.")
            if not name:
                continue
            slug = _voice_slug(str(name))
            path = os.path.join(VOICES_DIR, f"{slug}.wav") if slug else ""
            if not (slug and os.path.exists(path)):
                raise HTTPException(404, f"No cloned voice named '{name}' — record one in Studio → Music → Voice Lab.")
            voice_paths[num] = path

        seed = body.get("seed")
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            seed = -1

        import asyncio
        if not await asyncio.to_thread(comfyui_client.is_up):
            raise HTTPException(502, "ComfyUI isn't running — narration renders on the ComfyUI engine.")
        try:
            job_id = await comfyui_client.start_narrate_job(
                script=script,
                model=model,
                owner=user,
                session_id=body.get("session_id"),
                seed=seed,
                speaker_voice_paths=voice_paths,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception:
            logger.exception("audio_narrate: submit failed")
            raise HTTPException(502, "Narration could not be started.")
        return {
            "job_id": job_id, "status": "queued", "model": model,
            "speakers": sorted(voice_paths),
        }

    return router
