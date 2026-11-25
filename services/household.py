"""The household: what is true for everyone under this roof, set once by an administrator.

    home       where "here" is — for the Almanac, the forecast and anything else that needs a place
    units      "us" (°F, miles, cups) or "metric" (°C, kilometres, litres): how answers are given
    language   the language the household reads; print in any other language gets translated into it
    live       which outside sources may be asked, each with its own switch (services/almanac/weather.py)

Kept in ``settings.json`` under ``household``. A person's own saved home (set on the Almanac page) still
wins for that person; everyone else gets the household's. Nothing in this module reaches the network.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

KEY = "household"
UNITS = ("us", "metric")
UNIT_LABEL = {"us": "US customary", "metric": "Metric"}
UNIT_WORDS = {
    "us": "°F, miles, feet and inches, pounds and ounces, cups and US gallons",
    "metric": "°C, kilometres, metres, kilograms and grams, litres and millilitres",
}
LIVE_SOURCES = ("weather", "alerts", "air")

_lock = threading.Lock()

# Time zones kept in the United States, for a first guess at the units on a server that was never told.
_US_ZONES = re.compile(
    r"^(?:US/|America/(?:New_York|Detroit|Kentucky/|Indiana/|Chicago|Menominee|North_Dakota/|Denver|Boise|Phoenix|"
    r"Los_Angeles|Anchorage|Juneau|Sitka|Metlakatla|Yakutat|Nome|Adak)|Pacific/Honolulu)")


def _server_zone() -> str:
    try:
        target = os.path.realpath("/etc/localtime")
        return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
    except Exception:
        return ""


def default_units() -> str:
    """A first guess before anyone chose: the server's locale, then its time zone."""
    for name in ("LC_MEASUREMENT", "LC_ALL", "LANG"):
        value = os.environ.get(name, "")
        if re.search(r"_[A-Z]{2}\b", value):
            return "us" if re.search(r"_(?:US|LR|MM)\b", value) else "metric"
    return "us" if _US_ZONES.match(_server_zone()) else "metric"


def default_language() -> str:
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        found = re.match(r"([a-z]{2})(?:[_.@-]|$)", os.environ.get(name, ""))
        if found:
            try:
                from services.translate import languages
                if found.group(1) in languages.LANGUAGES:
                    return found.group(1)
            except Exception:
                return found.group(1)
    return "en"


def _stored() -> Dict[str, Any]:
    if os.environ.get("AEGIS_HOUSEHOLD_DISABLED"):       # the test suite: never the real settings file
        return {}
    try:
        from src.settings import load_settings
        kept = load_settings().get(KEY)
        return dict(kept) if isinstance(kept, dict) else {}
    except Exception:
        return {}


def _store(kept: Dict[str, Any]) -> None:
    if os.environ.get("AEGIS_HOUSEHOLD_DISABLED"):
        raise RuntimeError("household settings are switched off (AEGIS_HOUSEHOLD_DISABLED)")
    from src.settings import load_settings, save_settings
    with _lock:
        settings = dict(load_settings())
        settings[KEY] = kept
        save_settings(settings)


def _home_of(kept: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    home = kept.get("home")
    if not isinstance(home, dict):
        return None
    try:
        lat, lon = float(home["lat"]), float(home["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    return {"lat": lat, "lon": lon, "label": str(home.get("label") or "Home")[:120], "zone": str(home.get("zone") or "")}


def get() -> Dict[str, Any]:
    """The household's settings, every key present. ``chosen`` lists what an administrator has set by hand."""
    kept = _stored()
    live = kept.get("live") if isinstance(kept.get("live"), dict) else {}
    language = str(kept.get("language") or "")
    try:
        from services.translate import languages
        known = language in languages.LANGUAGES or language in languages.VARIANTS
    except Exception:
        known = bool(re.fullmatch(r"[a-z]{2}(?:-[A-Za-z]{2,4})?", language))
    return {
        "home": _home_of(kept),
        "units": kept.get("units") if kept.get("units") in UNITS else default_units(),
        "language": language if language and known else default_language(),
        "live": {name: bool(live.get(name, True)) for name in LIVE_SOURCES},
        "chosen": sorted(key for key in ("home", "units", "language") if kept.get(key)),
    }


def home():
    """The household's home position as the Almanac's ``Where``, or None when none was saved."""
    found = get()["home"]
    if not found:
        return None
    from services.almanac import places
    return places.Where(found["lat"], found["lon"], found["label"], found["zone"], "home", "")


def set_home(place: str, zone: str = "") -> Dict[str, Any]:
    """Save ``place`` (a town or coordinates) as the household's home. Raises the Almanac's ``PlaceError``
    with words for the person when the place cannot be found."""
    from services.almanac import places
    asked = re.sub(r"\s+", " ", str(place or "")).strip()
    if not asked or asked.lower().strip(" .") in places._HOME_WORDS:
        raise places.PlaceError("Give a town (with its state or country) or coordinates.")
    found = places.resolve(asked, owner=None, household=False)
    if zone.strip():
        places.clock(found, zone)                       # refuses a zone that does not exist
    kept = _stored()
    kept["home"] = {"lat": round(found.lat, 6), "lon": round(found.lon, 6), "label": found.label, "zone": zone.strip() or found.zone}
    _store(kept)
    return describe()


def forget_home() -> bool:
    kept = _stored()
    if "home" not in kept:
        return False
    del kept["home"]
    _store(kept)
    return True


def update(units: Optional[str] = None, language: Optional[str] = None, live: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Change the units, the language or the live-source switches. Unknown values are refused, not guessed at."""
    kept = _stored()
    if units is not None:
        if units not in UNITS:
            raise ValueError("Units are either US customary or metric.")
        kept["units"] = units
    if language is not None:
        from services.translate import languages
        code = languages.resolve(str(language))
        if not code:
            raise ValueError(f"“{str(language)[:40]}” is not a language I know.")
        kept["language"] = code
    if live is not None:
        if not isinstance(live, dict):
            raise ValueError("The live sources are switches: on or off.")
        switches = dict(kept.get("live") or {}) if isinstance(kept.get("live"), dict) else {}
        for name, value in live.items():
            if name in LIVE_SOURCES and isinstance(value, bool):
                switches[name] = value
        kept["live"] = switches
    _store(kept)
    return describe()


def units() -> str:
    return get()["units"]


def language() -> str:
    return get()["language"]


def language_name(code: Optional[str] = None) -> str:
    code = code or language()
    try:
        from services.translate import languages
        return languages.plain_name(code) or code
    except Exception:
        return code


def live(name: str) -> bool:
    """Whether the household lets this outside source be asked."""
    return bool(get()["live"].get(name, True))


def describe() -> Dict[str, Any]:
    """Everything the console shows: the settings, the home in every notation, and the choices on offer."""
    found = get()
    out: Dict[str, Any] = {"units": found["units"], "language": found["language"], "language_name": language_name(found["language"]),
                           "live": found["live"], "chosen": found["chosen"], "home": None,
                           "unit_choices": [{"id": key, "label": UNIT_LABEL[key], "words": UNIT_WORDS[key]} for key in UNITS]}
    kept = home()
    if kept is not None:
        try:
            from services.almanac import places
            out["home"] = places.describe(kept)
        except Exception:
            out["home"] = {"label": kept.label, "lat": kept.lat, "lon": kept.lon, "zone": kept.zone}
    try:
        from services.almanac import weather
        out["sources"] = weather.catalogue()
    except Exception:
        out["sources"] = []
    try:
        from services.translate import languages
        out["language_choices"] = [{"code": row["code"], "name": row["name"]} for row in languages.listing()]
    except Exception:
        out["language_choices"] = [{"code": found["language"], "name": out["language_name"]}]
    return out


def prompt_line() -> str:
    """One short paragraph for the assistant's standing instructions — or "" on a server where nobody has
    set anything, so an untouched install behaves exactly as before. Stable for a given configuration."""
    found = get()
    if not found["chosen"]:
        return ""
    parts = []
    if found["home"]:
        zone = f" (time zone {found['home']['zone']})" if found["home"]["zone"] else ""
        parts.append(f"This household's home is {found['home']['label']}{zone}. Unless another place is named, “here”, "
                     "“tonight”, “outside” and “the weather” mean there: the tools use it when no place is given, so do not ask where they are.")
    if "units" in found["chosen"] or found["home"]:
        parts.append(f"Give measurements the way this household uses them — {UNIT_WORDS[found['units']]} — unless asked for others.")
    if "language" in found["chosen"] and found["language"] != "en":
        parts.append(f"This household reads {language_name(found['language'])}: answer in {language_name(found['language'])} unless asked otherwise.")
    return " ".join(parts)
