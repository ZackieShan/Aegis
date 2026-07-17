"""Local video generation via stable-diffusion.cpp's async job API.

sd-server (behind llama-swap) exposes video generation ONLY on its native
async surface — POST /sdcpp/v1/vid_gen returns a job envelope immediately and
GET /sdcpp/v1/jobs/{id} is polled for progress/result. Neither path is an
OpenAI route llama-swap can body-route, so both are reached through
llama-swap's explicit /upstream/{model}/ passthrough, which also triggers the
on-demand model load.

A minutes-long generation is never held on one HTTP request: start_video_job
submits, remembers the remote job id, and a background asyncio task polls
until the encoded video (base64 webm) arrives, then writes it into
GENERATED_IMAGES_DIR and inserts a GalleryImage row — the existing serve
route, gallery grid and lightbox already speak video for those files.
"""

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from src import job_queue
from src.constants import GENERATED_IMAGES_DIR

logger = logging.getLogger(__name__)

# Model ids that are video pipelines (wan2.2-t2v, ltx2-video-…). Anchored on
# separators so e.g. "wanda-chat" never matches.
VIDEO_MODEL_RE = re.compile(r"(?:^|[/\-_.])(?:wan[0-9.]*|ltx[0-9.]*|t2v|i2v|video)(?:$|[/\-_.])", re.I)

# The model id is spliced into the /upstream/{model}/ URL path — keep it to a
# strict charset so a crafted id can never restructure the URL.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]*$")

_POLL_INTERVAL_S = 3.0
# Hard cap per generation. 60 min, not 30: a 10s LTX-2 clip (241 frames) is a
# legitimate request that takes well over half an hour on a 4090 — a cap it
# can't fit just abandons healthy renders (and until the cancel-on-deadline
# below, left sd-server grinding on a result nobody would collect).
_JOB_DEADLINE_S = 3600
_MAX_JOBS_KEPT = 50

_JOBS: Dict[str, Dict[str, Any]] = {}


def is_video_model(model_id: str) -> bool:
    return bool(VIDEO_MODEL_RE.search(str(model_id or "")))


def apply_wan_lightning(model_id: str, prompt: str, negative_prompt: str) -> tuple:
    """(prompt, negative_prompt) with the Wan2.2 Lightning LoRAs spliced in
    for native sd-server T2V renders.

    sd.cpp applies LoRAs through prompt syntax — `<lora:name:1>` targets the
    low-noise expert, `<lora:|high_noise|name:1>` the high-noise one (per its
    docs/wan.md Lightning example, which pairs them with 4+4 euler steps at
    cfg 3.5). The llama-swap entry's step counts assume these tags, so this
    must stay in lockstep with engine/llama-swap.yaml. An empty negative also
    gets Wan's standard one — at cfg 3.5 it actively steers away from the
    extra-fingers/bad-hands failure mode. No-ops for non-Wan/i2v models, when
    the LoRA files are missing, or when the prompt already carries a
    lightx2v tag (e.g. from a style preset)."""
    from src import comfyui_workflows

    mid = str(model_id or "").lower()
    if "wan" not in mid or "t2v" not in mid:
        return prompt, negative_prompt
    if not comfyui_workflows.wan_lightning_available():
        return prompt, negative_prompt
    negative_prompt = negative_prompt or comfyui_workflows.WAN_DEFAULT_NEGATIVE
    if "lightx2v" in (prompt or "").lower():
        return prompt, negative_prompt
    low = comfyui_workflows.WAN_LIGHTNING_LOW_LORA.removesuffix(".safetensors")
    high = comfyui_workflows.WAN_LIGHTNING_HIGH_LORA.removesuffix(".safetensors")
    return f"{prompt}<lora:{low}:1><lora:|high_noise|{high}:1>", negative_prompt


def list_video_models(owner: Optional[str] = None) -> List[Dict[str, str]]:
    """Video-capable model ids across enabled endpoints (from cached_models)."""
    from src.database import SessionLocal, ModelEndpoint
    from src.auth_helpers import owner_filter

    out: List[Dict[str, str]] = []
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        for ep in owner_filter(q, ModelEndpoint, owner).all():
            try:
                cached = json.loads(getattr(ep, "cached_models", None) or "[]")
            except Exception:
                cached = []
            for mid in cached:
                if is_video_model(mid):
                    out.append({"model": str(mid), "endpoint": getattr(ep, "name", "") or ""})
    finally:
        db.close()
    # de-dup preserving order
    seen = set()
    return [m for m in out if not (m["model"] in seen or seen.add(m["model"]))]


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    return _JOBS.get(job_id)


def _prune_jobs() -> None:
    if len(_JOBS) <= _MAX_JOBS_KEPT:
        return
    done = sorted(
        (j for j in _JOBS.values() if j["status"] in ("done", "error", "canceled")),
        key=lambda j: j.get("created", 0),
    )
    for j in done[: len(_JOBS) - _MAX_JOBS_KEPT]:
        _JOBS.pop(j["id"], None)


def _upstream_root(model_spec: str, owner: Optional[str]) -> tuple[str, str, dict]:
    """Resolve (engine_root_url, model_id, headers) for a video model.

    Reuses ai_interaction._resolve_model — the same live-probe endpoint
    resolution image generation uses — then strips the chat suffix back to the
    server root that /upstream/ hangs off.
    """
    from src.ai_interaction import _resolve_model

    chat_url, model_id, headers = _resolve_model(model_spec, owner)
    root = chat_url.replace("/chat/completions", "").replace("/v1/messages", "").rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    if not _MODEL_ID_RE.fullmatch(model_id):
        raise ValueError(f"Model id {model_id!r} contains characters unsafe for URL routing")
    return root, model_id, headers


async def start_video_job(
    prompt: str,
    model: str,
    owner: Optional[str] = None,
    session_id: Optional[str] = None,
    negative_prompt: str = "",
    width: int = 832,
    height: int = 480,
    video_frames: int = 33,
    fps: int = 16,
    seed: int = -1,
    init_image_b64: Optional[str] = None,
) -> str:
    """Submit a generation to sd-server and return a local job id.

    Raises ValueError when the model can't be resolved or the submit fails.
    """
    import httpx

    # _resolve_model does sync httpx probes + DB queries — keep them off the
    # event loop.
    root, model_id, headers = await asyncio.to_thread(_upstream_root, model, owner)
    submit_url = f"{root}/upstream/{model_id}/sdcpp/v1/vid_gen"
    # Only the server sees the <lora:...> tags — the job record (and thus the
    # gallery/movie-maker prompt) keeps the user's clean prompt.
    gen_prompt, negative_prompt = apply_wan_lightning(model_id, prompt, negative_prompt)
    payload = {
        "prompt": gen_prompt,
        "negative_prompt": negative_prompt or "",
        "width": int(width),
        "height": int(height),
        "video_frames": int(video_frames),
        "fps": int(fps),
        "seed": int(seed),
        "output_format": "webm",
    }
    if init_image_b64:
        # First-frame conditioning (image-to-video). sd-server resizes the
        # reference to the requested dimensions itself.
        payload["init_image"] = init_image_b64

    # The submit itself is quick, but llama-swap holds the request while it
    # cold-loads the model (two Wan experts + a CPU text encoder ≈ minutes).
    timeout = httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(submit_url, json=payload, headers=headers)
        if r.status_code not in (200, 202):
            detail = r.text[:300]
            raise ValueError(f"Video submit failed ({r.status_code}): {detail}")
        envelope = r.json()

    remote_id = str(envelope.get("id") or "")
    if not remote_id:
        raise ValueError(f"Video server returned no job id: {str(envelope)[:200]}")
    poll_path = str(envelope.get("poll_url") or f"/sdcpp/v1/jobs/{remote_id}")
    if poll_path.startswith(("http://", "https://")):
        # Keep polling through llama-swap (which resets the model's idle ttl);
        # an absolute poll_url would point at sd-server's ephemeral port.
        from urllib.parse import urlsplit
        poll_path = urlsplit(poll_path).path or f"/sdcpp/v1/jobs/{remote_id}"

    job_id = uuid.uuid4().hex[:12]
    # Register with the unified queue before anything can fail: this entry is
    # what tells chat the GPU is taken, and a render that never registers is a
    # render that chat will happily evict.
    queue_id = job_queue.add(
        "video",
        prompt or "video",
        owner=owner,
        session_id=session_id,
        external_id=job_id,
        meta={"model": model_id, "width": int(width), "height": int(height)},
    )
    _JOBS[job_id] = {
        "id": job_id,
        "kind": "video",
        "queue_id": queue_id,
        "status": "queued",
        "prompt": prompt,
        "model": model_id,
        "owner": owner,
        "session_id": session_id,
        "width": int(width),
        "height": int(height),
        "video_frames": int(video_frames),
        "fps": int(fps),
        "has_init_image": bool(init_image_b64),
        "created": time.time(),
        "remote_id": remote_id,
        "video_url": None,
        "image_id": None,
        "error": None,
        "progress": None,
    }
    _prune_jobs()

    poll_url = f"{root}/upstream/{model_id}{poll_path}"
    cancel_url = f"{root}/upstream/{model_id}/sdcpp/v1/jobs/{remote_id}/cancel"
    _JOBS[job_id]["_cancel_url"] = cancel_url
    asyncio.create_task(_poll_job(job_id, poll_url, headers))
    logger.info("Video job %s submitted (model=%s, %sx%s, %s frames)", job_id, model_id, width, height, video_frames)
    return job_id


async def start_image_edit_job(
    image_b64: str,
    prompt: str,
    model: str,
    owner: Optional[str] = None,
    session_id: Optional[str] = None,
    negative_prompt: str = "",
    width: int = 512,
    height: int = 512,
    seed: int = -1,
) -> str:
    """Submit an instruction edit (qwen-image-edit family) to sd-server's
    async /sdcpp/v1/img_gen and return a local job id in the same registry
    video jobs use — status/cancel/queue behavior is identical.

    Async on purpose: the synchronous /v1/images/edits call has to survive a
    llama-swap model swap + a 21GB cold load + the CPU vision pass inside one
    HTTP read timeout, and it demonstrably doesn't (first stylize after a
    chat session timed out at 600s while the GPU kept rendering). A job
    survives all of that, shows up in the unified queue, and is visible to
    the GPU guard so chat can't evict it mid-render."""
    import httpx

    root, model_id, headers = await asyncio.to_thread(_upstream_root, model, owner)
    submit_url = f"{root}/upstream/{model_id}/sdcpp/v1/img_gen"
    payload = {
        "prompt": prompt,
        "negative_prompt": negative_prompt or "",
        "width": int(width),
        "height": int(height),
        "seed": int(seed),
        # Mirror sd-server's own /v1/images/edits mapping exactly: the source
        # rides as ref_images[0] AND init_image (routes_openai.cpp does both),
        # with auto-resize on so a mismatched ref can never run the vision
        # encoder at full source resolution.
        "ref_images": [image_b64],
        "init_image": image_b64,
        "auto_resize_ref_image": True,
        "output_format": "png",
    }

    timeout = httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(submit_url, json=payload, headers=headers)
        if r.status_code not in (200, 202):
            raise ValueError(f"Edit submit failed ({r.status_code}): {r.text[:300]}")
        envelope = r.json()
    remote_id = str(envelope.get("id") or "")
    if not remote_id:
        raise ValueError(f"Image server returned no job id: {str(envelope)[:200]}")
    poll_path = str(envelope.get("poll_url") or f"/sdcpp/v1/jobs/{remote_id}")
    if poll_path.startswith(("http://", "https://")):
        from urllib.parse import urlsplit
        poll_path = urlsplit(poll_path).path or f"/sdcpp/v1/jobs/{remote_id}"

    job_id = uuid.uuid4().hex[:12]
    queue_id = job_queue.add(
        "image",
        prompt or "image edit",
        owner=owner,
        session_id=session_id,
        external_id=job_id,
        meta={"model": model_id, "width": int(width), "height": int(height)},
    )
    _JOBS[job_id] = {
        "id": job_id,
        "kind": "image",
        "queue_id": queue_id,
        "status": "queued",
        "prompt": prompt,
        "model": model_id,
        "owner": owner,
        "session_id": session_id,
        "width": int(width),
        "height": int(height),
        "created": time.time(),
        "remote_id": remote_id,
        "video_url": None,
        "image_id": None,
        "error": None,
        "progress": None,
    }
    _prune_jobs()
    _JOBS[job_id]["_cancel_url"] = f"{root}/upstream/{model_id}/sdcpp/v1/jobs/{remote_id}/cancel"
    asyncio.create_task(_poll_job(job_id, f"{root}/upstream/{model_id}{poll_path}", headers))
    logger.info("Image edit job %s submitted (model=%s, %sx%s)", job_id, model_id, width, height)
    return job_id


def _mirror_queue(job: Dict[str, Any]) -> None:
    """Mirror a video job's state onto its unified-queue entry.

    Best-effort by design: the queue is a view, so a mirroring failure must never
    take down a real render.
    """
    qid = job.get("queue_id")
    if not qid:
        return
    try:
        progress = job.get("progress")
        if isinstance(progress, (int, float)) and progress > 1:
            # sd-server reports percent on some builds and a 0..1 fraction on
            # others; normalise, or every render would pin at 100%.
            progress = float(progress) / 100.0

        status = str(job.get("status") or "")
        if status == "done":
            job_queue.finish(qid, "done", result_url=job.get("video_url"))
        elif status == "error":
            job_queue.finish(qid, "error", error=job.get("error"))
        elif status in ("canceled", "cancelled"):
            job_queue.finish(qid, "cancelled")
        elif status == "running":
            job_queue.start(qid)
            job_queue.update(qid, progress=progress)
        else:
            job_queue.update(qid, progress=progress)
    except Exception:
        logger.debug("Queue mirror failed for video job %s", job.get("id"), exc_info=True)


async def _poll_job(job_id: str, poll_url: str, headers: dict) -> None:
    import httpx

    job = _JOBS.get(job_id)
    if not job:
        return
    deadline = time.time() + _JOB_DEADLINE_S
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=30.0)) as client:
            while time.time() < deadline:
                # cancel_requested (not status) is the authoritative flag — the
                # status writes below must never resurrect a canceled job.
                if job.get("cancel_requested") or job.get("status") == "canceled":
                    job["status"] = "canceled"
                    return
                try:
                    r = await client.get(poll_url, headers=headers)
                except Exception as e:
                    job["error"] = f"Lost contact with the video server: {e}"
                    job["status"] = "error"
                    return
                # A cancel may have landed while the GET was in flight — check
                # again before any state writes so it can't be clobbered.
                if job.get("cancel_requested"):
                    job["status"] = "canceled"
                    return
                if r.status_code == 404:
                    job["error"] = "Video job vanished on the server (model may have been swapped out mid-generation)"
                    job["status"] = "error"
                    return
                if r.status_code != 200:
                    job["error"] = f"Video poll failed ({r.status_code})"
                    job["status"] = "error"
                    return
                remote = r.json()
                status = str(remote.get("status") or "").lower()
                for key in ("progress", "step", "steps", "eta"):
                    if remote.get(key) is not None:
                        job.setdefault("remote_progress", {})[key] = remote[key]
                if isinstance(remote.get("progress"), (int, float)):
                    job["progress"] = remote["progress"]
                result = remote.get("result") or {}
                # vid_gen puts the payload at result.b64_json; img_gen nests
                # it as result.images[{index, b64_json}].
                b64 = result.get("b64_json")
                if not b64:
                    images = result.get("images") or []
                    if isinstance(images, list) and images:
                        b64 = (images[0] or {}).get("b64_json")
                if b64:
                    # File + DB writes are blocking — keep them off the loop.
                    await asyncio.to_thread(_finish_job, job, result, b64)
                    return
                if status in ("failed", "error", "canceled", "cancelled"):
                    job["error"] = str(remote.get("error") or f"Generation {status}")
                    job["status"] = "error"
                    return
                if status in ("completed", "done", "success"):
                    # Terminal without a payload — never leave the job
                    # spinning as "queued" forever.
                    job["error"] = "Generation completed but returned no image data"
                    job["status"] = "error"
                    return
                if not job.get("cancel_requested"):
                    job["status"] = "running" if status in ("running", "processing", "in_progress", "generating") else (job["status"] if job["status"] == "running" else "queued")
                _mirror_queue(job)
                await asyncio.sleep(_POLL_INTERVAL_S)
        job["error"] = f"Video generation exceeded the {_JOB_DEADLINE_S // 60}-minute cap"
        job["status"] = "error"
        # Tell sd-server to stop: without this the GPU keeps grinding on a
        # render whose poller is gone and whose result nobody will collect.
        # Best-effort — some sd.cpp phases refuse interruption ("cannot be
        # interrupted yet"), in which case it runs out on its own.
        cancel_url = job.get("_cancel_url")
        if cancel_url:
            try:
                async with httpx.AsyncClient(timeout=10.0) as _c:
                    await _c.post(cancel_url, headers=headers)
            except Exception:
                logger.debug("Deadline cancel failed for %s", job_id, exc_info=True)
    except Exception as e:  # never let the poller die silently
        logger.exception("Video job %s poller crashed", job_id)
        job["error"] = f"Video job failed: {e}"
        job["status"] = "error"
    finally:
        # Every exit from this poller is terminal for the job — cancel, error,
        # timeout, or success. Mirroring here (rather than at each return) is
        # what guarantees the queue can't be left believing a render is still
        # live, which would strand chat on the CPU model indefinitely.
        _mirror_queue(job)


def _finish_job(job: Dict[str, Any], result: Dict[str, Any], b64: Optional[str] = None) -> None:
    default_ext = "png" if job.get("kind") == "image" else "webm"
    ext = str(result.get("output_format") or default_ext).lower()
    try:
        raw = base64.b64decode(b64 if b64 is not None else result["b64_json"])
    except Exception:
        job["error"] = "The render server returned undecodable data"
        job["status"] = "error"
        return
    finish_job_bytes(job, raw, ext)


def finish_job_bytes(job: Dict[str, Any], raw: bytes, ext: str) -> None:
    """Write a finished render to disk + gallery and mark the job done. Shared
    by the sd-server poller (b64 results) and the ComfyUI runner (raw bytes)."""
    ext = str(ext or "").lower()
    if job.get("kind") == "image":
        if ext not in ("png", "jpg", "jpeg", "webp"):
            ext = "png"
    elif ext not in ("webm", "mp4", "avi", "webp"):
        ext = "webm"

    # Store MP4, not webm/avi: browsers play both, but phones, editors and
    # messengers want H.264 MP4 — and the movie maker's inputs stay uniform.
    # webp is an animated image, not a video container — leave it alone.
    # Best-effort: a failed transcode keeps the original bytes, never loses
    # the render. (This runs in a worker thread on both engine paths.)
    if ext in ("webm", "avi"):
        from src import video_editing
        converted = video_editing.transcode_to_mp4(raw, ext)
        if converted:
            raw, ext = converted, "mp4"

    out_dir = Path(GENERATED_IMAGES_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex[:12]}.{ext}"
    (out_dir / filename).write_bytes(raw)

    image_id = ""
    try:
        from core.database import SessionLocal, GalleryImage
        image_id = str(uuid.uuid4())
        db = SessionLocal()
        quality = ("edit" if job.get("kind") == "image"
                   else f"{job.get('video_frames')}f@{job.get('fps')}fps")
        db.add(GalleryImage(
            id=image_id,
            filename=filename,
            prompt=job.get("prompt"),
            model=job.get("model"),
            size=f"{job.get('width')}x{job.get('height')}",
            quality=quality,
            session_id=job.get("session_id"),
            owner=job.get("owner"),
        ))
        db.commit()
        db.close()
    except Exception as e:
        logger.warning("Video gallery record failed: %s", e)
        image_id = ""

    job["video_url"] = f"/api/generated-image/{filename}"
    job["image_id"] = image_id or None
    job["progress"] = 1.0
    job["status"] = "done"
    logger.info("Video job %s done → %s (%d bytes)", job["id"], filename, len(raw))


async def cancel_job(job_id: str) -> bool:
    import httpx

    job = _JOBS.get(job_id)
    if not job or job["status"] in ("done", "error", "canceled"):
        return False
    job["cancel_requested"] = True
    job["status"] = "canceled"
    job["error"] = "Canceled by user"
    cancel_url = job.get("_cancel_url")
    if cancel_url:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(cancel_url)
        except Exception:
            pass  # local job already marked; remote will hit its own timeout
    return True
