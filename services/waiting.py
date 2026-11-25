"""Honest waiting: what to tell a person while nothing is coming back yet.

A model that is not in memory has to be loaded before its first word — tens of seconds on this kind of
machine. The screen used to say "Thinking…" for all of it. ``waking()`` looks (one request to the local
model server, nothing loaded by asking) and, when the model is asleep, says so with how long it took the
last time. The other notices — the internet is down, the machine is busy and a smaller helper is answering
— are worded here too, so every screen says the same thing.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_took: Dict[str, float] = {}        # model → seconds from asking to its first word, the last time it was asleep
DEFAULT_SECONDS = 30.0

OFFLINE = {"type": "notice", "code": "offline", "text": "The internet is down. This is from the home library."}


def _swap_port() -> str:
    return os.getenv("LLAMA_SWAP_PORT", "9090")


def served_here(endpoint_url: str) -> bool:
    """True when the chat endpoint is this machine's own model server (the only one that can be asked what is loaded)."""
    try:
        parsed = urlparse(str(endpoint_url or ""))
    except ValueError:
        return False
    return parsed.hostname in ("127.0.0.1", "localhost", "::1") and str(parsed.port or "") == _swap_port()


def how_long(seconds: float) -> str:
    if seconds < 12:
        return "a few seconds"
    if seconds < 22:
        return "about 20 seconds"
    if seconds < 45:
        return "about half a minute"
    if seconds < 80:
        return "about a minute"
    return f"about {round(seconds / 60)} minutes"


async def waking(endpoint_url: str, model: str) -> Optional[dict]:
    """A notice when ``model`` has to be loaded before it can answer; None when it is ready (or cannot be asked)."""
    if not model or not served_here(endpoint_url):
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2.5, trust_env=False) as client:
            answer = await client.get(f"http://127.0.0.1:{_swap_port()}/running")
        if answer.status_code != 200:
            return None
        data = answer.json()
        running = data.get("running", data) if isinstance(data, dict) else data
    except Exception:
        # The model server is not answering at all: it is restarting, which is waking up too.
        return {"type": "notice", "code": "waking", "seconds": 60, "text": "Waking up, about a minute"}
    for item in running or []:
        if isinstance(item, dict) and item.get("model") == model and str(item.get("state") or "ready") == "ready":
            return None
    seconds = _took.get(model, DEFAULT_SECONDS)
    return {"type": "notice", "code": "waking", "seconds": round(seconds), "text": f"Waking up, {how_long(seconds)}"}


def note_woke(model: str, seconds: float) -> None:
    """Remember how long a sleeping model took to say its first word, for the next estimate."""
    if model and 1.0 < seconds < 900:
        _took[model] = seconds


def busy(notice_from_guard: str = "") -> dict:
    """The machine is making a picture or a video, and a smaller helper answers meanwhile."""
    return {"type": "notice", "code": "busy",
            "text": "The machine is busy making something, so a smaller helper is answering. This answer may be simpler than usual."}
