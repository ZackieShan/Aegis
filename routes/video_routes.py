"""Video generation routes — submit/poll/cancel jobs on a served video model.

Generation runs on stable-diffusion.cpp's async job API behind llama-swap
(see src/video_generation.py). These routes never hold a long request: start
returns a job id immediately and the UI polls status until the video lands in
the gallery.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from src import model_tags, style_presets, video_generation
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


def _is_i2v(model_id: str) -> bool:
    return "image-to-video" in model_tags.classify(model_id or "")["capabilities"]


def _fit_video_dims(w: int, h: int, budget_px: int = 400_000) -> tuple:
    """Scale source dims into a render-safe pixel budget, /16 aligned, so an
    animated clip keeps the still's aspect ratio by default."""
    try:
        scale = min(1.0, (budget_px / float(max(1, w * h))) ** 0.5)
        fw = max(256, min(1280, int(w * scale) // 16 * 16))
        fh = max(256, min(1280, int(h * scale) // 16 * 16))
        return fw, fh
    except Exception:
        return 832, 480


def _resolve_init_image(body, user) -> tuple:
    """(b64, gallery_image_id, (w, h) | None) for an image-to-video request,
    or (None, None, None).

    Accepts image_id (a gallery row id, or "last" = the caller's newest
    still), or a raw base64 init_image. Ownership is enforced the same way
    the serve route does it — a row owned by someone else 404s.
    """
    raw = str(body.get("init_image") or "").strip()
    if raw:
        return raw, None, None
    image_id = str(body.get("image_id") or "").strip()
    if not image_id:
        return None, None, None

    import base64
    from src.database import SessionLocal, GalleryImage
    from src.generated_images import resolve_generated_image_path

    db = SessionLocal()
    try:
        if image_id.lower() == "last":
            q = db.query(GalleryImage).filter(GalleryImage.is_active == True)  # noqa: E712
            q = q.filter((GalleryImage.owner == user) if user else (GalleryImage.owner.is_(None) | (GalleryImage.owner == "")))
            rows = q.order_by(GalleryImage.created_at.desc()).limit(20).all()
            row = next((r for r in rows if "." in (r.filename or "")
                        and r.filename.rsplit(".", 1)[-1].lower() in ("png", "jpg", "jpeg", "webp")), None)
        else:
            row = db.query(GalleryImage).filter(GalleryImage.id == image_id).first()
    finally:
        db.close()
    if not row:
        raise HTTPException(404, "No matching gallery image")
    if (getattr(row, "owner", "") or "") != (user or ""):
        raise HTTPException(404, "No matching gallery image")
    ext = (row.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "webp"):
        raise HTTPException(400, "That gallery item is not a still image")
    path = resolve_generated_image_path(row.filename)
    data = path.read_bytes()
    dims = None
    try:
        import io
        from PIL import Image
        dims = Image.open(io.BytesIO(data)).size
    except Exception:
        pass
    return base64.b64encode(data).decode(), row.id, dims


# Frame counts both Wan and LTX are trained on follow the 4k+1 pattern; the
# ceiling allows ~10s clips (257f = 10.7s at LTX's 24fps, 16s at Wan's 16fps).
MAX_VIDEO_FRAMES = 257


def _native_fps(model: str) -> int:
    """The fps a video model was trained at — LTX and HunyuanVideo 1.5 are
    24fps pipelines (and sd-server otherwise defaults 16, yielding
    slow-motion clips); Wan is 16."""
    mid = (model or "").lower()
    return 24 if ("ltx" in mid or "hunyuan" in mid) else 16


def _resolve_frames(body, fps: int) -> int:
    """Frame count from explicit video_frames, else duration seconds, else the
    2s default — snapped to the 4k+1 pattern the models are trained on."""
    frames = body.get("video_frames")
    if frames in (None, "", 0):
        dur = body.get("duration", body.get("seconds"))
        try:
            dur = float(dur) if dur is not None else None
        except (TypeError, ValueError):
            dur = None
        if dur is not None:
            frames = int(round(max(0.5, min(30.0, dur)) * fps))
    frames = _clamp(frames, 1, MAX_VIDEO_FRAMES, 33)
    return min(MAX_VIDEO_FRAMES, (frames + 1) // 4 * 4 + 1)


def setup_video_routes() -> APIRouter:
    router = APIRouter(tags=["video"])

    async def _comfy_models() -> list:
        """ComfyUI-served workflow ids (empty when the engine is down)."""
        import asyncio
        from src import comfyui_client, comfyui_workflows
        try:
            if not await asyncio.to_thread(comfyui_client.is_up):
                return []
            return [
                {"model": m, "endpoint": "comfyui",
                 "label": comfyui_workflows.COMFY_VIDEO_MODELS[m].get("label", m)}
                for m in comfyui_workflows.available_video_models()
            ]
        except Exception:
            return []

    @router.get("/api/video/models")
    async def video_models(request: Request):
        user = get_current_user(request)
        return {"models": video_generation.list_video_models(user) + await _comfy_models()}

    @router.post("/api/video/generate")
    async def video_generate(request: Request):
        user = require_privilege(request, "can_generate_images")
        body = await request.json()
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(400, "Prompt is required")

        # Style preset: explicit style= field first, else the caller's active
        # style. Contributes model/prompt affixes/negative/seed defaults —
        # anything the request sets explicitly still wins.
        style = style_presets.resolve_style(str(body.get("style") or ""), user)

        # Image-to-video: an init image constrains model choice — only
        # image-conditioning pipelines (LTX) can honor it; Wan T2V can't.
        init_b64, init_image_id, init_dims = _resolve_init_image(body, user)

        from src import comfyui_workflows

        model = str(body.get("model") or "").strip()
        if not model and style and style.get("video_model"):
            preferred = str(style["video_model"]).strip()
            served = [d.get("model") for d in video_generation.list_video_models(user)]
            served += [m["model"] for m in await _comfy_models()]
            if preferred in served and (not init_b64 or _is_i2v(preferred)):
                model = preferred
            else:
                logger.info("style %r video_model %r is not served (or can't take an init image); using default",
                            style.get("id"), preferred)
        if not model:
            detected = video_generation.list_video_models(user)
            if not detected:
                detected = await _comfy_models()
            if not detected:
                raise HTTPException(400, "No video model is served. Add a Wan/LTX entry to the engine config (llama-swap.yaml) first.")
            if init_b64:
                detected = [d for d in detected if _is_i2v(d.get("model") or "")]
                if not detected:
                    raise HTTPException(400, "No image-to-video capable model is served — LTX handles image conditioning; Wan T2V does not.")
            model = _pick_video_model(user, detected)
        elif init_b64 and not _is_i2v(model):
            raise HTTPException(400, f"'{model}' can't animate a still image — use an image-to-video model (e.g. ltx2.3-video).")

        is_comfy = model in comfyui_workflows.COMFY_VIDEO_MODELS
        if is_comfy and init_b64:
            raise HTTPException(400, f"'{model}' doesn't take an init image yet.")

        # Dimensions must suit video latents (multiples of 16). When animating
        # a still, default to its aspect ratio inside a render-safe budget.
        default_w, default_h = _fit_video_dims(*init_dims) if init_dims else (832, 480)
        width = _clamp(body.get("width"), 256, 1280, default_w) // 16 * 16
        height = _clamp(body.get("height"), 256, 1280, default_h) // 16 * 16
        fps = _clamp(body.get("fps"), 4, 30, _native_fps(model))
        video_frames = _resolve_frames(body, fps)
        seed = _clamp(body.get("seed"), -1, 2**31 - 1, -1)
        if seed < 0 and style and style.get("seed") is not None:
            seed = int(style["seed"])
        negative = str(body.get("negative_prompt") or "") or (style or {}).get("negative_prompt", "")

        try:
            if is_comfy:
                from src import comfyui_client
                job_id = await comfyui_client.start_video_job(
                    prompt=style_presets.styled_prompt(style, prompt),
                    model=model,
                    owner=user,
                    session_id=body.get("session_id"),
                    negative_prompt=negative,
                    width=width,
                    height=height,
                    video_frames=video_frames,
                    fps=fps,
                    seed=seed,
                )
            else:
                job_id = await video_generation.start_video_job(
                    prompt=style_presets.styled_prompt(style, prompt),
                    model=model,
                    owner=user,
                    session_id=body.get("session_id"),
                    negative_prompt=negative,
                    width=width,
                    height=height,
                    video_frames=video_frames,
                    fps=fps,
                    seed=seed,
                    init_image_b64=init_b64,
                )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception:
            logger.exception("video_generate: submit failed")
            raise HTTPException(502, "Video generation could not be started — is the video model served and the engine running?")
        return {
            "job_id": job_id, "status": "queued", "model": model,
            "video_frames": video_frames, "fps": fps,
            "duration_s": round(video_frames / fps, 1),
            "style": (style or {}).get("id") or None,
            "image_id": init_image_id,
            "animating_image": bool(init_b64),
        }

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
