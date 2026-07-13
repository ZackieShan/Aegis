"""Video generation routes — submit/poll/cancel jobs on a served video model.

Generation runs on stable-diffusion.cpp's async job API behind llama-swap
(see src/video_generation.py). These routes never hold a long request: start
returns a job id immediately and the UI polls status until the video lands in
the gallery.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from src import video_generation
from src.auth_helpers import get_current_user, require_privilege
from src.settings import get_setting, get_user_setting

logger = logging.getLogger(__name__)


def _pick_video_model(user, detected):
    """Resolve the video model for a generate call that named none.

    Prefers the `video_model` setting (per-user override, then global), but
    only when that model is actually served — llama-swap 404s a request for
    an unknown model id, so an unserved preference falls back to the first
    detected model instead of failing the run.
    """
    served = [d.get("model") for d in detected if d.get("model")]
    preferred = str(
        get_user_setting("video_model", user, get_setting("video_model", "")) or ""
    ).strip()
    if preferred and preferred in served:
        return preferred
    if preferred:
        logger.info(
            "video_model setting %r is not served; falling back to %s",
            preferred, served[0] if served else "none",
        )
    return served[0] if served else ""


def _clamp(val, lo, hi, default):
    try:
        v = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def setup_video_routes() -> APIRouter:
    router = APIRouter(tags=["video"])

    @router.get("/api/video/models")
    async def video_models(request: Request):
        user = get_current_user(request)
        return {"models": video_generation.list_video_models(user)}

    @router.post("/api/video/generate")
    async def video_generate(request: Request):
        user = require_privilege(request, "can_generate_images")
        body = await request.json()
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(400, "Prompt is required")

        model = str(body.get("model") or "").strip()
        if not model:
            detected = video_generation.list_video_models(user)
            if not detected:
                raise HTTPException(400, "No video model is served. Add a Wan/LTX entry to the engine config (llama-swap.yaml) first.")
            model = _pick_video_model(user, detected)

        # Dimensions must suit video latents (multiples of 16); frames follow
        # the 4k+1 pattern both Wan and LTX are trained on.
        width = _clamp(body.get("width"), 256, 1280, 832) // 16 * 16
        height = _clamp(body.get("height"), 256, 1280, 480) // 16 * 16
        video_frames = _clamp(body.get("video_frames"), 1, 161, 33)
        fps = _clamp(body.get("fps"), 4, 30, 16)
        seed = _clamp(body.get("seed"), -1, 2**31 - 1, -1)

        try:
            job_id = await video_generation.start_video_job(
                prompt=prompt,
                model=model,
                owner=user,
                session_id=body.get("session_id"),
                negative_prompt=str(body.get("negative_prompt") or ""),
                width=width,
                height=height,
                video_frames=video_frames,
                fps=fps,
                seed=seed,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception:
            logger.exception("video_generate: submit failed")
            raise HTTPException(502, "Video generation could not be started — is the video model served and the engine running?")
        return {"job_id": job_id, "status": "queued", "model": model}

    def _job_for(request: Request, job_id: str):
        user = get_current_user(request)
        job = video_generation.get_job(job_id)
        if not job:
            raise HTTPException(404, "Unknown video job")
        # Normalize both sides: an anonymous/single-user job (owner "") is only
        # visible to anonymous callers, not to every authenticated user.
        if (job.get("owner") or "") != (user or ""):
            raise HTTPException(403, "Not your job")
        return job

    @router.get("/api/video/status/{job_id}")
    async def video_status(request: Request, job_id: str):
        job = _job_for(request, job_id)
        return {k: v for k, v in job.items() if not k.startswith("_")}

    @router.post("/api/video/cancel/{job_id}")
    async def video_cancel(request: Request, job_id: str):
        _job_for(request, job_id)
        ok = await video_generation.cancel_job(job_id)
        return {"ok": ok}

    return router
