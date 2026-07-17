"""Music generation routes — ACE-Step songs on the ComfyUI engine.

Same job model as video: POST /api/audio/generate returns a job id
immediately, /api/video/status/{id} (the shared render-job registry) is
polled until the MP3 lands in the Studio like any other generation.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_privilege

logger = logging.getLogger(__name__)


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
                 "label": comfyui_workflows.COMFY_AUDIO_MODELS[m].get("label", m)}
                for m in comfyui_workflows.available_audio_models()
            ]
        except Exception:
            return []

    @router.get("/api/audio/models")
    async def audio_models(request: Request):
        return {"models": await _comfy_models()}

    @router.post("/api/audio/generate")
    async def audio_generate(request: Request):
        user = require_privilege(request, "can_generate_images")
        body = await request.json()
        tags = str(body.get("tags") or body.get("prompt") or "").strip()
        if not tags:
            raise HTTPException(400, "Describe the song — genre, mood, tempo, instruments (this is the 'tags' line).")
        lyrics = str(body.get("lyrics") or "").strip()
        seconds = _clamp(body.get("seconds", body.get("duration")), 5, 600, 60)
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
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception:
            logger.exception("audio_generate: submit failed")
            raise HTTPException(502, "Song generation could not be started.")
        return {
            "job_id": job_id, "status": "queued", "model": model,
            "seconds": seconds, "has_lyrics": bool(lyrics),
        }

    return router
