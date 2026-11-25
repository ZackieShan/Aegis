"""Places nearby: a pharmacy, a pizza place, a hardware store — with the address, the phone number, today's
hours and whether it is open now.

Asked of Brave Search's place index, near the person's home (theirs, else the household's) or a town they
name. The position sent is rounded to about a kilometre; the distances shown are worked out here from the
exact one, which never leaves the house.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from services.connections.http import Problem

logger = logging.getLogger(__name__)

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_SPAN = re.compile(r"(\d{1,2}):(\d{2})\s*[–-]\s*(\d{1,2}):(\d{2})")


def _spans(text: str) -> List[Tuple[int, int]]:
    """"11:00–14:00, 17:00–02:00" as minutes from midnight; a span that closes after midnight runs past 1440."""
    found = []
    for opens_h, opens_m, closes_h, closes_m in _SPAN.findall(text or ""):
        start, end = int(opens_h) * 60 + int(opens_m), int(closes_h) * 60 + int(closes_m)
        if end <= start:
            end += 1440
        found.append((start, end))
    return found


def open_now(spot: Dict[str, Any], now: Optional[datetime] = None) -> Optional[bool]:
    """True / False when the hours say so, None when they are not known."""
    week = spot.get("week") or {}
    today_text = spot.get("today") or ""
    if not week and not today_text:
        return None
    if now is None:
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(spot["zone"])) if spot.get("zone") else datetime.now().astimezone()
        except Exception:
            now = datetime.now().astimezone()
    minute = now.hour * 60 + now.minute
    today = week.get(_DAYS[now.weekday()], today_text if not week else "")
    if any(start <= minute < end for start, end in _spans(today)):
        return True
    yesterday = week.get(_DAYS[(now - timedelta(days=1)).weekday()], "")
    if any(end > 1440 and minute < end - 1440 for _, end in _spans(yesterday)):
        return True
    return False


def _twelve(text: str) -> str:
    """"11:00–22:00" → "11 am–10 pm" for a household that reads the clock that way."""
    def one(hour: str, minute: str) -> str:
        h = int(hour) % 24
        word = "am" if h < 12 else "pm"
        shown = h % 12 or 12
        return f"{shown}{'' if minute == '00' else ':' + minute} {word}"
    return re.sub(r"(\d{1,2}):(\d{2})", lambda found: one(found.group(1), found.group(2)), text or "")


def _household_units() -> str:
    try:
        from services import household
        return household.units()
    except Exception:
        return "us"


def find(query: str, near: str = "", owner: Optional[str] = None, count: int = 6) -> Tuple[List[dict], str, str]:
    """(places, the name of where they are near, a note on how that place was read — "" when there is
    nothing to say). Raises ``Problem`` in words for the person."""
    from services.search import brave

    query = " ".join(str(query or "").split())
    if not query:
        raise Problem("Say what kind of place to look for.", "refused")
    problem = brave.why_not()
    if problem is not None:
        raise problem
    position = None
    note = ""
    label = " ".join(str(near or "").split())
    try:
        from services.almanac import places as almanac
        try:
            where = almanac.resolve(label, owner)
            position, label, note = (where.lat, where.lon), where.label, str(where.note or "")
        except almanac.PlaceError:
            if not label or label.lower() in ("home", "here", "me", "near me", "nearby"):
                raise Problem("No home position is saved, so I do not know where “nearby” is. "
                              "Ask the person which town to look in.", "refused")
    except ImportError:
        pass
    if not label:
        raise Problem("Say which town to look in.", "refused")
    sent = (round(position[0], 2), round(position[1], 2)) if position else None
    spots = brave.places(query, near=label, position=sent, count=20)
    if position:
        for spot in spots:
            if spot.get("position"):
                spot["miles"] = round(brave.miles_between(position, spot["position"]), 1)
        spots.sort(key=lambda spot: (spot["miles"] is None, spot["miles"] or 0.0))
    if not spots:
        raise Problem(f"No places matching “{query}” were found near {label}.", "nothing")
    return spots[: max(1, min(int(count or 6), 12))], label, note


def answer(args: Dict[str, Any], owner: Optional[str] = None) -> Tuple[str, List[dict]]:
    query = str(args.get("query") or args.get("what") or args.get("find") or args.get("q") or args.get("_text") or "")
    near = str(args.get("near") or args.get("place") or args.get("where") or args.get("town") or "")
    spots, label, note = find(query, near, owner, args.get("count") or 6)
    metric = _household_units() == "metric"
    only_open = str(args.get("open_now") or "").lower() in ("true", "1", "yes")
    lines = [f"PLACES: {' '.join(query.split())} — near {label} (from Brave Search)"]
    if note:
        lines.append(f"({note})")           # which of several towns of that name was meant
    lines.append("")
    shown = 0
    sources = []
    for spot in spots:
        state = open_now(spot)
        if only_open and state is not True:
            continue
        shown += 1
        head = f"[{shown}] {spot['name']}"
        about = [part for part in (spot["kind"], ", ".join(spot["cuisine"]), spot["price"]) if part]
        if about:
            head += " — " + " · ".join(dict.fromkeys(about))
        lines.append(head)
        if spot["address"]:
            distance = ""
            if spot.get("miles") is not None:
                distance = f" ({spot['miles'] * 1.609344:.1f} km away)" if metric else f" ({spot['miles']:.1f} miles away)"
            lines.append(f"    {spot['address']}{distance}")
        hours = spot["today"] if metric else _twelve(spot["today"])
        if state is True:
            lines.append(f"    Open now — today {hours}" if hours else "    Open now")
        elif state is False:
            lines.append(f"    Closed now — today {hours}" if hours else "    Closed today")
        if spot["phone"]:
            lines.append(f"    Phone {spot['phone']}")
        if spot["rating"]:
            reviews = f" from {int(spot['reviews']):,} reviews" if spot.get("reviews") else ""
            lines.append(f"    Rated {spot['rating']} out of 5{reviews}")
        if spot["website"]:
            lines.append(f"    {spot['website']}")
            sources.append({"url": spot["website"], "title": spot["name"]})
        lines.append("")
    if not shown:
        raise Problem(f"None of the places found near {label} is open right now.", "nothing")
    lines.append("Hours and ratings are as the place index has them; for anything that matters (a pharmacy before it "
                 "closes, a holiday), tell the person to ring first. Give the address and phone number as written.")
    return "\n".join(lines), sources
