"""Movie maker + unified queue endpoints.

/api/movie/*  — stitch generated clips into one film (Studio → Movie).
/api/queue/*  — what's running, what's next, and cancelling it.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from src import job_queue, movie_maker, video_editing
from src.auth_helpers import get_current_user, require_privilege
from src.video_editing import VideoEditError

logger = logging.getLogger(__name__)


def setup_movie_routes() -> APIRouter:
    router = APIRouter(tags=["movie"])

    @router.get("/api/movie/clips")
    async def movie_clips(request: Request):
        """Gallery videos this user can put in a film, newest first."""
        user = get_current_user(request)
        from src.database import GalleryImage, SessionLocal
        from src.auth_helpers import owner_filter

        db = SessionLocal()
        try:
            q = db.query(GalleryImage)
            q = owner_filter(q, GalleryImage, user)
            rows = q.order_by(GalleryImage.created_at.desc()).limit(200).all()
        finally:
            db.close()

        out = []
        for r in rows:
            name = str(getattr(r, "filename", "") or "")
            if not name.lower().endswith((".webm", ".mp4", ".avi")):
                continue  # stills can't go in a film
            out.append({
                "name": name,
                "url": f"/api/generated-image/{name}",
                "prompt": getattr(r, "prompt", "") or "",
                "model": getattr(r, "model", "") or "",
                "size": getattr(r, "size", "") or "",
            })
        return {"clips": out}

    @router.post("/api/movie/probe")
    async def movie_probe(request: Request):
        """Duration/size/fps for one clip — the Movie tab needs real durations
        to offer trim handles."""
        get_current_user(request)
        body = await request.json()
        try:
            path = video_editing.safe_media_path(str(body.get("name") or ""))
            import asyncio
            return await asyncio.to_thread(video_editing.probe, path)
        except VideoEditError as e:
            raise HTTPException(400, str(e))

    @router.post("/api/movie/build")
    async def movie_build(request: Request):
        # Same privilege as generating media: this writes a new gallery item.
        user = require_privilege(request, "can_generate_images")
        body = await request.json()
        try:
            qid = await movie_maker.build(
                body.get("clips"),
                title=str(body.get("title") or ""),
                owner=user,
                session_id=str(body.get("session_id") or "") or None,
            )
        except VideoEditError as e:
            raise HTTPException(400, str(e))
        return {"job_id": qid, "status": "queued"}

    # ── queue ──

    @router.get("/api/queue")
    async def queue_list(request: Request):
        """Everything long-running: renders, movies, recipe runs, research."""
        user = get_current_user(request)
        rows = job_queue.snapshot(owner=user)
        busy = job_queue.gpu_busy()
        return {
            "jobs": rows,
            "gpu_busy": bool(busy),
            "gpu_job": busy,
        }

    @router.post("/api/queue/{qid}/cancel")
    async def queue_cancel(qid: str, request: Request):
        user = get_current_user(request)
        entry = job_queue.get(qid)
        if not entry:
            raise HTTPException(404, "No such job")
        # Don't let one user cancel another's work. Unowned (scheduler) entries
        # stay cancellable — they're system work everyone can see.
        if entry.get("owner") not in (None, user):
            raise HTTPException(403, "That job belongs to someone else")

        # Ask the owning subsystem to actually stop, not just relabel it.
        external = entry.get("external_id")
        if entry.get("kind") == "video" and external:
            try:
                from src import video_generation
                video_generation.cancel_job(external)
            except Exception:
                logger.debug("Underlying video cancel failed for %s", qid, exc_info=True)

        if not job_queue.cancel(qid):
            return {"ok": False, "reason": "already finished"}
        return {"ok": True}

    @router.post("/api/queue/clear")
    async def queue_clear(request: Request):
        user = get_current_user(request)
        return {"cleared": job_queue.clear_finished(owner=user)}

    return router
