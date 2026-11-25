"""The daily review's few settings, kept in ``settings.json`` under ``chat_review``.

    on       whether the review runs each night (off until an administrator turns it on)
    time     when, on the house's own clock ("03:30")
    skip     accounts whose chats are left out altogether
    notify   "problems" (tell the administrators when something needs a look), "always" or "never"
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, tzinfo
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

KEY = "chat_review"
NOTIFY = ("problems", "always", "never")
DEFAULTS: Dict[str, Any] = {"on": False, "time": "03:30", "skip": [], "notify": "problems"}
_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


class Refused(ValueError):
    """A setting that cannot be kept as given; the message says why, for the person."""


def get() -> Dict[str, Any]:
    try:
        from src.settings import load_settings
        kept = load_settings().get(KEY)
    except Exception:
        kept = None
    kept = kept if isinstance(kept, dict) else {}
    found = dict(DEFAULTS)
    found["on"] = kept.get("on") is True
    if isinstance(kept.get("time"), str) and _TIME.fullmatch(kept["time"]):
        found["time"] = kept["time"]
    found["skip"] = sorted({str(name) for name in (kept.get("skip") or []) if isinstance(name, str) and name})
    if kept.get("notify") in NOTIFY:
        found["notify"] = kept["notify"]
    return found


def update(on: Optional[bool] = None, time: Optional[str] = None, skip: Optional[List[str]] = None,
           notify: Optional[str] = None) -> Dict[str, Any]:
    """Change some of the settings. What is not given stays as it was."""
    from src.settings import load_settings, save_settings

    now = get()
    if on is not None:
        if not isinstance(on, bool):
            raise Refused("On is either true or false.")
        now["on"] = on
    if time is not None:
        if not isinstance(time, str) or not _TIME.fullmatch(time.strip()):
            raise Refused("Give the time as hours and minutes, like 03:30.")
        now["time"] = time.strip()
    if skip is not None:
        if not isinstance(skip, list) or not all(isinstance(name, str) for name in skip):
            raise Refused("The accounts to leave out are given as a list of names.")
        now["skip"] = sorted({name.strip().lower() for name in skip if name.strip()})
    if notify is not None:
        if notify not in NOTIFY:
            raise Refused("Tell the administrators when something needs a look, always, or never.")
        now["notify"] = notify
    settings = dict(load_settings())
    settings[KEY] = now
    save_settings(settings)
    return now


def zone_name() -> str:
    """The house's time zone by name: the household's home if one is set, else the server's own. '' if neither is known."""
    try:
        from services import household
        home = household.home()
        if home is not None and getattr(home, "zone", ""):
            return str(home.zone)
        return household._server_zone() or ""
    except Exception:
        return ""


def zone() -> tzinfo:
    """The house's clock, for "yesterday" and for when the review runs."""
    name = zone_name()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc
