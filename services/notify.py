"""Finished work finds the person — and so does trouble.

One way to tell an account that something is ready ("your report is ready"), and one way to tell the people
who run the server that something broke for someone ("it failed for Mia"). A notification goes two ways at
once: to the phones the person turned notifications on for (``services/push.py``), and to the app's own
queue, so a screen that is open says so too.

Nothing here raises. A notification that cannot be sent is logged and forgotten: the work it is about is
already done, or already broken, either way.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_loop: Optional[asyncio.AbstractEventLoop] = None
_failures: Dict[str, Dict[str, object]] = {}      # chat id → the last technical cause seen there
_lock = threading.Lock()


def bind_loop(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Remember the server's event loop, so work finishing on another thread can still notify."""
    global _loop
    try:
        _loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        _loop = None


def _queue(owner: Optional[str], title: str, body: str, url: str, tag: Optional[str], status: str) -> None:
    """The app's own queue: what an open screen polls (GET /api/tasks/notifications)."""
    try:
        from routes import note_routes
        scheduler = getattr(note_routes, "_scheduler_ref", None)
        if scheduler is not None:
            # push=False: whoever called here sends the push themselves (tell does), so it is not sent twice
            scheduler.add_notification(title, status, task_id=tag, owner=owner or None, body=body, url=url, push=False)
    except Exception:
        logger.debug("notification not queued for the open screen", exc_info=True)


async def tell(owner: Optional[str], title: str, body: str = "", url: str = "/home", tag: Optional[str] = None,
               status: str = "success") -> int:
    """Tell one account. Returns how many phones took it (0 is ordinary: none set up, or no internet)."""
    _queue(owner, title, body, url, tag, status)
    if not owner:
        return 0
    try:
        from services import push
        return await push.send(owner, title, body, url=url, tag=tag)
    except Exception as problem:
        logger.info("notification not pushed (%s)", type(problem).__name__)
        return 0


def tell_soon(owner: Optional[str], title: str, body: str = "", url: str = "/home", tag: Optional[str] = None,
              status: str = "success") -> None:
    """The same, from anywhere: a worker thread, a callback, code that cannot wait."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    try:
        if loop is not None:
            loop.create_task(tell(owner, title, body, url, tag, status))
        elif _loop is not None and _loop.is_running():
            asyncio.run_coroutine_threadsafe(tell(owner, title, body, url, tag, status), _loop)
        else:
            _queue(owner, title, body, url, tag, status)       # no loop to push from: the open screen still hears
    except Exception:
        logger.debug("notification not scheduled", exc_info=True)


def push_soon(owner: Optional[str], title: str, body: str = "", url: str = "/", tag: Optional[str] = None) -> None:
    """To the person's phones only — for something an open screen has been told already (a scheduled task
    or an automation that finished: ``TaskScheduler.add_notification``). From anywhere; never raises."""
    if not owner:
        return

    async def send() -> None:
        try:
            from services import push
            await push.send(owner, title, body, url=url, tag=tag)
        except Exception as problem:
            logger.info("notification not pushed (%s)", type(problem).__name__)

    try:
        try:
            asyncio.get_running_loop().create_task(send())
        except RuntimeError:
            if _loop is not None and _loop.is_running():
                asyncio.run_coroutine_threadsafe(send(), _loop)
    except Exception:
        logger.debug("push not scheduled", exc_info=True)


def admins() -> List[str]:
    """The accounts that run this server."""
    try:
        from core.auth import AuthManager
        manager = AuthManager()
        return [name for name in sorted(manager.users) if manager.is_admin(name) and not manager.is_disabled(name)]
    except Exception:
        return []


async def tell_admins(title: str, body: str = "", url: str = "/admin", tag: Optional[str] = None, but: Optional[str] = None) -> int:
    """Tell everyone who runs the server (except ``but``). Returns how many phones took it."""
    sent = 0
    for name in admins():
        if name != but:
            sent += await tell(name, title, body, url, tag, status="warning")
    return sent


def tell_admins_soon(title: str, body: str = "", url: str = "/admin", tag: Optional[str] = None, but: Optional[str] = None) -> None:
    for name in admins():
        if name != but:
            tell_soon(name, title, body, url, tag, status="warning")


# ── what went wrong, kept until someone asks ──────────────────────────────

def note_failure(chat: Optional[str], cause: object) -> None:
    """Remember the technical cause of a failed reply, for when the person presses "Tell …"."""
    if not chat:
        return
    with _lock:
        _failures[str(chat)] = {"at": time.time(), "cause": str(cause or "")[:600]}
        for old in sorted(_failures, key=lambda key: _failures[key]["at"])[:-200]:
            del _failures[old]


def last_failure(chat: Optional[str], within: float = 1800.0) -> str:
    """The cause noted for this chat in the last half hour, or ""."""
    with _lock:
        found = _failures.get(str(chat or ""))
    if not found or time.time() - float(found["at"]) > within:
        return ""
    return str(found["cause"])
