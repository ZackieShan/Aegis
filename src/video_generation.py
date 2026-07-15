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
_JOB_DEADLINE_S = 1800  # 30 min hard cap per generation
_MAX_JOBS_KEPT = 50

_JOBS: Dict[str, Dict[str, Any]] = {}


def is_video_model(model_id: str) -> bool:
    return bool(VIDEO_MODEL_RE.search(str(model_id or "")))


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
    payload = {
        "prompt": prompt,
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
                if result.get("b64_json"):
                    # File + DB writes are blocking — keep them off the loop.
                    await asyncio.to_thread(_finish_job, job, result)
                    return
                if status in ("failed", "error", "canceled", "cancelled"):
                    job["error"] = str(remote.get("error") or f"Video generation {status}")
                    job["status"] = "error"
                    return
                if not job.get("cancel_requested"):
                    job["status"] = "running" if status in ("running", "processing", "in_progress", "generating") else (job["status"] if job["status"] == "running" else "queued")
                _mirror_queue(job)
                await asyncio.sleep(_POLL_INTERVAL_S)
        job["error"] = f"Video generation exceeded the {_JOB_DEADLINE_S // 60}-minute cap"
        job["status"] = "error"
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


def _finish_job(job: Dict[str, Any], result: Dict[str, Any]) -> None:
    ext = str(result.get("output_format") or "webm").lower()
    try:
        raw = base64.b64decode(result["b64_json"])
    except Exception:
        job["error"] = "Video server returned undecodable data"
        job["status"] = "error"
        return
    finish_job_bytes(job, raw, ext)


def finish_job_bytes(job: Dict[str, Any], raw: bytes, ext: str) -> None:
    """Write a finished clip to disk + gallery and mark the job done. Shared
    by the sd-server poller (b64 results) and the ComfyUI runner (raw bytes)."""
    ext = str(ext or "webm").lower()
    if ext not in ("webm", "mp4", "avi", "webp"):
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
        from src.database import SessionLocal, GalleryImage
        image_id = str(uuid.uuid4())
        db = SessionLocal()
        db.add(GalleryImage(
            id=image_id,
            filename=filename,
            prompt=job.get("prompt"),
            model=job.get("model"),
            size=f"{job.get('width')}x{job.get('height')}",
            quality=f"{job.get('video_frames')}f@{job.get('fps')}fps",
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
