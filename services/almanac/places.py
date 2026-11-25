"""Where: turning what a person wrote into a position, a name for it and
the clock kept there — and remembering the place they call home.

A place can be coordinates in any notation (:mod:`.coordinates`), a town by
name (:mod:`.gazetteer`, when the place list is installed), or nothing at
all, which means the home position the person saved.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import coordinates, gazetteer

HOME_KEY = "home_place"
_HOME_WORDS = {"", "home", "here", "my home", "my house", "my place", "my location", "our house", "our home", "our place", "where i am",
               "where i live", "where we live", "my position", "current location", "me"}


class PlaceError(ValueError):
    """Why a place could not be found, in words for the person."""


class Where(NamedTuple):
    lat: float
    lon: float
    label: str           # what to call it: "Denver, Colorado", "near Leadville, Colorado" or the coordinates
    zone: str            # the IANA time zone kept there, "" when not known
    kind: str            # "latlon", "utm", "mgrs", "name" or "home"
    note: str            # anything the person should know about how it was read
    elevation: Optional[int] = None


def _near(lat: float, lon: float) -> Tuple[str, str, Optional[int]]:
    """A name for a position, the clock kept there, and the height of the named place when the position is in it."""
    town = gazetteer.locality(lat, lon)
    if town is None:
        return f"{lat:.4f}, {lon:.4f}", "", None
    close = gazetteer.nearest(lat, lon) or town
    distance = gazetteer.kilometres(lat, lon, town.lat, town.lon)
    if distance < 3:
        return town.label, close.zone, town.elevation
    bearing = coordinates.compass(coordinates.between((town.lat, town.lon), (lat, lon))["bearing"])
    return f"{distance:.0f} km {bearing} of {town.label}", close.zone, None


# ── home ───────────────────────────────────────────────────────────────────

def own_home(owner: Optional[str]) -> Optional[Where]:
    """The position this person saved as their own home, if any."""
    try:
        from routes.prefs_routes import _load_for_user
        kept = (_load_for_user(owner) or {}).get(HOME_KEY)
    except Exception:
        return None
    if not isinstance(kept, dict):
        return None
    try:
        lat, lon = float(kept["lat"]), float(kept["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return Where(lat, lon, str(kept.get("label") or "Home")[:120], str(kept.get("zone") or ""), "home", "")


def home(owner: Optional[str], household: bool = True) -> Optional[Where]:
    """Where "home" is for this person: the position they saved themselves, and failing that the
    household's (set once by an administrator, see :mod:`services.household`)."""
    kept = own_home(owner)
    if kept is not None or not household:
        return kept
    try:
        from services import household as shared
        return shared.home()
    except Exception:
        return None


def set_home(owner: Optional[str], place: str, zone: str = "") -> Where:
    """Save ``place`` as this person's home position. Returns what was saved."""
    from routes.prefs_routes import _load_for_user, _save_for_user

    found = resolve(place, owner=None)
    if zone.strip():
        clock(found, zone)                              # refuses a zone that does not exist
    prefs = _load_for_user(owner) or {}
    prefs[HOME_KEY] = {"lat": round(found.lat, 6), "lon": round(found.lon, 6), "label": found.label, "zone": zone.strip() or found.zone}
    _save_for_user(owner, prefs)
    return Where(found.lat, found.lon, found.label, zone.strip() or found.zone, "home", found.note, found.elevation)


def forget_home(owner: Optional[str]) -> bool:
    from routes.prefs_routes import _load_for_user, _save_for_user

    prefs = _load_for_user(owner) or {}
    if HOME_KEY not in prefs:
        return False
    del prefs[HOME_KEY]
    _save_for_user(owner, prefs)
    return True


# ── any place ──────────────────────────────────────────────────────────────

def resolve(text: str, owner: Optional[str] = None, household: bool = True) -> Where:
    """A position for what the person wrote: coordinates, a town's name, or nothing (their home)."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    lowered = re.sub(r"^(?:at|in|near|from|to)\s+", "", raw.lower()).strip(" .")
    if lowered in _HOME_WORDS:
        kept = home(owner, household=household)
        if kept is None:
            raise PlaceError("No place was given and no home position is saved. Give a town or coordinates.")
        return kept
    try:
        read = coordinates.read(raw)
    except coordinates.PlaceError as problem:
        looks_like_a_position = bool(re.fullmatch(r"\d{1,2}\s*[A-Za-z]{1,3}\s*[\d\s]+", raw)) or not re.search(r"[A-Za-z]{2}", raw)
        if looks_like_a_position:
            raise PlaceError(str(problem)) from None
        name = re.sub(r"^(?:at|in|near|from|to)\s+", "", raw, flags=re.I).strip(" .")
        found = gazetteer.find(name, 4)
        if not found:
            if not gazetteer.available():
                raise PlaceError(f"“{name[:40]}” is not a position I can read, and the list of place names is not installed "
                                 "(scripts/aegis-almanac places fetch). Give latitude and longitude instead.") from None
            raise PlaceError(f"I do not know a place called “{name[:40]}”. Try it with its state or country (Springfield, MO), "
                             "or give latitude and longitude.") from None
        first = found[0]
        note = ""
        if len(found) > 1:
            note = "Took that to be " + first.label + "; there is also " + "; ".join(place.label for place in found[1:4]) + "."
        return Where(first.lat, first.lon, first.label, first.zone, "name", note, first.elevation)
    label, zone, elevation = _near(read.lat, read.lon)
    return Where(read.lat, read.lon, label, zone, read.kind, read.note, elevation)


# ── the clock kept somewhere ───────────────────────────────────────────────

def _fixed(text: str) -> Optional[tzinfo]:
    found = re.fullmatch(r"(?:utc|gmt)?\s*([-+])\s*(\d{1,2})(?::?(\d{2}))?", text.strip(), flags=re.I)
    if not found:
        return timezone.utc if text.strip().lower() in ("utc", "gmt", "z", "zulu") else None
    minutes = int(found.group(2)) * 60 + int(found.group(3) or 0)
    if minutes > 14 * 60:
        return None
    return timezone(timedelta(minutes=minutes if found.group(1) == "+" else -minutes))


def clock(where: Where, asked: str = "") -> Tuple[tzinfo, str, str]:
    """The time zone to give times in for a place: ``(zone, what to call it, a note)``.

    ``asked`` (an IANA name such as ``America/Denver``, ``UTC``, or an offset such as ``-07:00``) wins; otherwise the
    zone of the place; otherwise the clock of the person asking, with a note when the place is far from it."""
    from zoneinfo import ZoneInfo

    asked = str(asked or "").strip()
    if asked:
        fixed = _fixed(asked)
        if fixed is not None:
            return fixed, "UTC" if fixed is timezone.utc else f"UTC{asked.upper().replace('UTC', '').replace('GMT', '').strip()}", ""
        try:
            return ZoneInfo(asked), asked, ""
        except Exception:
            raise PlaceError(f"“{asked[:40]}” is not a time zone I know. Use a name such as America/Denver, or an offset such as -07:00.") from None
    if where.zone:
        try:
            return ZoneInfo(where.zone), where.zone, ""
        except Exception:
            pass
    try:
        from src.user_time import get_user_tz_name, get_user_tz_offset, user_timezone
        name = get_user_tz_name()
        if name:
            try:
                zone: tzinfo = ZoneInfo(name)
            except Exception:
                zone, name = user_timezone(), ""
        elif get_user_tz_offset() is not None:
            zone = user_timezone()
        else:
            zone = datetime.now().astimezone().tzinfo or timezone.utc
    except Exception:
        zone, name = datetime.now().astimezone().tzinfo or timezone.utc, ""
    offset = datetime.now(zone).utcoffset() or timedelta(0)
    label = name or f"UTC{'+' if offset >= timedelta(0) else '-'}{abs(offset) // timedelta(hours=1):02d}:{abs(offset) % timedelta(hours=1) // timedelta(minutes=1):02d}"
    note = ""
    if abs(where.lon / 15 - offset.total_seconds() / 3600) > 2.0:
        note = f"Times are on your clock ({label}); that place is far enough away to keep a different one."
    return zone, label, note


def describe(where: Where) -> Dict[str, object]:
    """One place in every notation, for showing."""
    forms = coordinates.describe(where.lat, where.lon)
    return {"label": where.label, "lat": round(where.lat, 6), "lon": round(where.lon, 6), "zone": where.zone, "kind": where.kind,
            "note": where.note, "elevation_m": where.elevation, **forms}


def between(first: Where, second: Where) -> Dict[str, object]:
    """Distance and direction from one place to another."""
    found = coordinates.between((first.lat, first.lon), (second.lat, second.lon))
    metres = found["metres"]
    return {"metres": metres, "km": metres / 1000, "miles": metres / 1609.344, "nautical_miles": metres / 1852,
            "bearing": found["bearing"], "compass": coordinates.compass(found["bearing"]), "back_bearing": found["back_bearing"],
            "back_compass": coordinates.compass(found["back_bearing"])}
