"""Turn a list of generated clips into one film, in the background.

Thin layer over video_editing: it resolves the caller's clip references safely,
runs the encode off the event loop, registers the result in the gallery so the
film shows up next to the clips it came from, and reports the whole thing through
the unified queue.

The encode is CPU work (ffmpeg/libx264), so it is registered with gpu=False —
building a movie must not push chat onto the CPU fallback, because it isn't
competing for the GPU.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from src import job_queue, video_editing
from src.constants import GENERATED_IMAGES_DIR
from src.video_editing import Clip, VideoEditError

logger = logging.getLogger(__name__)

MAX_CLIPS = 50


def parse_clips(raw: Any) -> List[Clip]:
    """Validate the request's clip list into resolved, confined Clips.

    Order is the caller's order — that *is* the reorder feature; the frontend
    sends the list as arranged.
    """
    if not isinstance(raw, list) or not raw:
        raise VideoEditError("Pick at least one clip")
    if len(raw) > MAX_CLIPS:
        raise VideoEditError(f"That's more than {MAX_CLIPS} clips — trim the list down")

    clips: List[Clip] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            raise VideoEditError(f"Clip {i + 1} is malformed")
        name = item.get("name") or item.get("filename") or item.get("url") or ""
        path = video_editing.safe_media_path(name)

        def _num(key: str) -> Optional[float]:
            v = item.get(key)
            if v is None or v == "":
                return None
            try:
                f = float(v)
            except (TypeError, ValueError):
                raise VideoEditError(f"Clip {i + 1}: {key} must be a number")
            if f < 0:
                raise VideoEditError(f"Clip {i + 1}: {key} cannot be negative")
            return f

        start, end = _num("start"), _num("end")
        if start is not None and end is not None and end <= start:
            raise VideoEditError(f"Clip {i + 1}: end must be after start")
        clips.append(Clip(path=path, start=start, end=end))
    return clips


def _register_in_gallery(filename: str, title: str, info: Dict[str, Any],
                         owner: Optional[str], session_id: Optional[str]) -> str:
    """Add the finished film to the gallery so it lands beside its source clips."""
    try:
        from src.database import GalleryImage, SessionLocal
        image_id = str(uuid.uuid4())
        db = SessionLocal()
        try:
            db.add(GalleryImage(
                id=image_id,
                filename=filename,
                prompt=title,
                model="movie-maker",
                size=f"{info.get('width')}x{info.get('height')}",
                quality=f"{info.get('clips')} clips @ {info.get('duration')}s",
                session_id=session_id,
                owner=owner,
            ))
            db.commit()
        finally:
            db.close()
        return image_id
    except Exception as e:
        # A gallery-record failure must not lose the film — it's already on disk
        # and the queue entry carries the URL.
        logger.warning("Movie gallery record failed: %s", e)
        return ""


async def build(
    raw_clips: Any,
    title: str = "",
    owner: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Validate + queue a movie build. Returns the queue id immediately.

    Validation happens up front (synchronously) so a bad request fails loudly at
    the API rather than as a mystery entry that errors seconds later.
    """
    clips = parse_clips(raw_clips)
    name = (str(title or "").strip() or f"Movie of {len(clips)} clips")[:120]

    qid = job_queue.add(
        "movie", name, owner=owner, session_id=session_id,
        meta={"clips": len(clips)},
    )
    asyncio.create_task(_run(qid, clips, name, owner, session_id))
    return qid


async def _run(qid: str, clips: List[Clip], title: str,
               owner: Optional[str], session_id: Optional[str]) -> None:
    job_queue.start(qid, detail=f"Stitching {len(clips)} clips")
    filename = f"{uuid.uuid4().hex[:12]}.mp4"
    out = Path(GENERATED_IMAGES_DIR)
    try:
        out.mkdir(parents=True, exist_ok=True)
        dest = out / filename
        # Encoding blocks; keep it off the event loop or the whole app stalls
        # for the length of the render.
        info = await asyncio.to_thread(video_editing.build_movie, clips, dest)
        image_id = _register_in_gallery(filename, title, info, owner, session_id)
        url = f"/api/generated-image/{filename}"
        job_queue.update(qid, detail=f"{info['duration']}s · {info['clips']} clips")
        job_queue.finish(qid, "done", result_url=url)
        logger.info("Movie %s built → %s (%ss, %s clips, image_id=%s)",
                    qid, filename, info.get("duration"), info.get("clips"), image_id or "-")
    except VideoEditError as e:
        # Expected failure — show the user exactly what ffmpeg objected to.
        job_queue.finish(qid, "error", error=str(e))
        logger.warning("Movie %s failed: %s", qid, e)
    except Exception as e:
        job_queue.finish(qid, "error", error=f"Movie build failed: {e}")
        logger.exception("Movie %s crashed", qid)
