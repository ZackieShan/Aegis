"""Unified queue for long-running work — media renders, recipe runs, research.

Two jobs in one:

1. **"What's happening?"** Every slow thing in Aegis used to report progress only
   on its own surface, so there was no single place to see what the machine was
   busy with. Subsystems register here and the Studio's queue panel reads it.

2. **"Is the GPU busy?"** This is the load-bearing one. sd-server fills the card
   during a render (~23.6GB of 24GB on a 4090), and llama-swap resolves model
   contention by *eviction* — so a chat request for a GPU model would stop a
   render mid-job. `gpu_busy()` is the signal the chat router uses to fall back
   to the CPU model instead. Anything that occupies VRAM must register with
   gpu=True, or the guard cannot see it.

In-memory by design: an entry describes work belonging to *this* process, and a
restart kills the work anyway, so persisting it would only ever resurrect lies.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Kinds that occupy VRAM. Keep this in sync with anything that loads a model
# onto the GPU — a kind missing here is invisible to the chat guard, which
# silently brings back the "chat kills the render" bug.
#
# "movie" is deliberately NOT here: stitching clips is ffmpeg + libx264, which
# is CPU work. It belongs in the queue (it's slow, you want to watch it) but it
# contends for no VRAM, so marking it GPU would strand chat on the 3B for
# nothing.
GPU_KINDS = frozenset({"image", "video", "image_edit"})

# Terminal states. Anything else is considered live.
DONE_STATES = frozenset({"done", "error", "cancelled"})

_ENTRIES: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.RLock()
_MAX_KEPT = 100


def _prune_locked() -> None:
    """Drop the oldest finished entries once the log grows past _MAX_KEPT."""
    if len(_ENTRIES) <= _MAX_KEPT:
        return
    finished = [
        (e.get("finished") or e.get("created") or 0.0, qid)
        for qid, e in _ENTRIES.items()
        if e.get("status") in DONE_STATES
    ]
    finished.sort()
    for _, qid in finished[: len(_ENTRIES) - _MAX_KEPT]:
        _ENTRIES.pop(qid, None)


def add(
    kind: str,
    title: str,
    owner: Optional[str] = None,
    session_id: Optional[str] = None,
    gpu: Optional[bool] = None,
    meta: Optional[Dict[str, Any]] = None,
    external_id: Optional[str] = None,
) -> str:
    """Register queued work and return its queue id.

    `gpu` defaults to whether `kind` is in GPU_KINDS; pass it explicitly only to
    override (e.g. a recipe step that happens to drive a diffusion model).
    """
    qid = uuid.uuid4().hex[:12]
    with _LOCK:
        _ENTRIES[qid] = {
            "id": qid,
            "kind": str(kind),
            "title": str(title or kind),
            "status": "queued",
            "owner": owner,
            "session_id": session_id,
            "gpu": bool(GPU_KINDS.__contains__(kind) if gpu is None else gpu),
            "progress": None,
            "detail": None,
            "result_url": None,
            "error": None,
            "created": time.time(),
            "started": None,
            "finished": None,
            "external_id": external_id,
            "meta": dict(meta or {}),
        }
        _prune_locked()
    return qid


def start(qid: str, detail: Optional[str] = None) -> None:
    with _LOCK:
        e = _ENTRIES.get(qid)
        if not e or e.get("status") in DONE_STATES:
            return
        e["status"] = "running"
        e["started"] = e.get("started") or time.time()
        if detail is not None:
            e["detail"] = detail


def update(
    qid: str,
    progress: Optional[float] = None,
    detail: Optional[str] = None,
    status: Optional[str] = None,
) -> None:
    """Report progress. Ignored once the entry is terminal, so a late poll can't
    resurrect a cancelled job into 'running' forever."""
    with _LOCK:
        e = _ENTRIES.get(qid)
        if not e or e.get("status") in DONE_STATES:
            return
        if progress is not None:
            try:
                e["progress"] = max(0.0, min(1.0, float(progress)))
            except (TypeError, ValueError):
                pass
        if detail is not None:
            e["detail"] = detail
        if status:
            e["status"] = str(status)
            if status == "running" and not e.get("started"):
                e["started"] = time.time()


def finish(
    qid: str,
    status: str = "done",
    result_url: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    with _LOCK:
        e = _ENTRIES.get(qid)
        if not e:
            return
        e["status"] = str(status)
        e["finished"] = time.time()
        if result_url:
            e["result_url"] = result_url
        if error:
            e["error"] = str(error)[:500]
        if status == "done" and e.get("progress") is not None:
            e["progress"] = 1.0


def cancel(qid: str) -> bool:
    """Mark an entry cancelled. Returns False if it was already finished.

    This only updates the queue's view — the owning subsystem is responsible for
    actually stopping the work (e.g. video_generation POSTs sd-server's cancel).
    """
    with _LOCK:
        e = _ENTRIES.get(qid)
        if not e or e.get("status") in DONE_STATES:
            return False
        e["status"] = "cancelled"
        e["finished"] = time.time()
        return True


def drop(qid: str) -> bool:
    """Remove an entry entirely — for work that turned out never to have run.

    Distinct from cancel(): a cancelled job happened and the user should see it,
    whereas a dropped one (e.g. an image render that fell back to a remote API
    before touching the GPU) would just be noise in the queue.
    """
    with _LOCK:
        return _ENTRIES.pop(qid, None) is not None


def get(qid: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        e = _ENTRIES.get(qid)
        return dict(e) if e else None


def gpu_busy(owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The GPU-occupying entry currently live, or None.

    Deliberately NOT owner-scoped by default: the GPU is one physical device, so
    another user's render blocks this user's chat just as hard. `owner` is
    accepted only for callers that want to describe *their own* job to the user.
    """
    with _LOCK:
        for e in _ENTRIES.values():
            if not e.get("gpu"):
                continue
            if e.get("status") in DONE_STATES:
                continue
            if owner is not None and e.get("owner") != owner:
                continue
            return dict(e)
    return None


def snapshot(
    owner: Optional[str] = None,
    include_done: bool = True,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Queue contents for the UI: live work first (oldest first, so 'what's next'
    reads top-down), then recent finished work newest-first."""
    with _LOCK:
        rows = [
            dict(e)
            for e in _ENTRIES.values()
            if owner is None or e.get("owner") == owner or e.get("owner") is None
        ]
    live = [r for r in rows if r.get("status") not in DONE_STATES]
    done = [r for r in rows if r.get("status") in DONE_STATES]
    live.sort(key=lambda r: r.get("created") or 0.0)
    done.sort(key=lambda r: r.get("finished") or 0.0, reverse=True)
    out = live + (done if include_done else [])
    # Position among live work only — a finished job has no "next" meaning.
    for i, r in enumerate(live):
        r["position"] = i
    return out[: max(1, int(limit))]


def clear_finished(owner: Optional[str] = None) -> int:
    with _LOCK:
        drop = [
            qid
            for qid, e in _ENTRIES.items()
            if e.get("status") in DONE_STATES
            and (owner is None or e.get("owner") == owner)
        ]
        for qid in drop:
            _ENTRIES.pop(qid, None)
        return len(drop)


def _reset_for_tests() -> None:
    with _LOCK:
        _ENTRIES.clear()
