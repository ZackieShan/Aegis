"""The forecast, weather alerts and air quality for a place — the Almanac's only outside sources.

Each is a keyless public service, asked with the position rounded to two decimal places (about a
kilometre) and nothing else — no account, no name, no address:

    weather   the forecast, worldwide            Open-Meteo                     api.open-meteo.com
              … and when that does not answer    US National Weather Service    api.weather.gov   (United States only)
    alerts    watches and warnings in effect     US National Weather Service    api.weather.gov   (United States only)
    air       air quality now                    Open-Meteo                     air-quality-api.open-meteo.com

Adding a source is one function that returns the same few fields (see ``PROVIDERS``).

An administrator can switch each off (People and access → Household). With the internet down, or the
forecast switched off, the answer says so in plain words — and gives the last forecast it fetched when
that is recent enough to be worth reading, marked with its age. Nothing is ever guessed.

``answer()`` is the whole interface: the assistant's ``weather`` tool, the straight-to-tool lane and the
family screen's "today" line all call it, so they agree.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SOURCES: Dict[str, Dict[str, str]] = {
    "weather": {"label": "Weather forecast", "who": "Open-Meteo, then the US National Weather Service",
                "host": "api.open-meteo.com, api.weather.gov", "covers": "Worldwide",
                "about": "Today, the week ahead and hour by hour. Used when someone asks about the weather or plans a day out."},
    "alerts": {"label": "Weather alerts", "who": "US National Weather Service", "host": "api.weather.gov", "covers": "United States only",
               "about": "Watches, warnings and advisories in effect, added to every forecast."},
    "air": {"label": "Air quality", "who": "Open-Meteo", "host": "air-quality-api.open-meteo.com", "covers": "Worldwide",
            "about": "The air quality index now, added to today's forecast."},
}
SENDS = "Only the position, rounded to about a kilometre. No name, account or address."

FRESH = 20 * 60                 # seconds a fetched forecast is reused without asking again
WORTH_READING = 12 * 3600       # how old a forecast may be and still be offered when the internet is down
DAYS = 7
AGENT = "aegis-home-server"     # a generic client name: nothing that identifies the household
_TIMEOUT = 8.0

_lock = threading.Lock()
_memory: Dict[str, Dict[str, Any]] = {}


class WeatherProblem(Exception):
    """Why there is no forecast, in words for the person. ``reason``: "off", "offline", "failed", "range" or "place"."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def catalogue() -> List[Dict[str, Any]]:
    """The outside sources and whether the household has each switched on, for the console."""
    from services import household
    return [{"id": name, **about, "sends": SENDS, "on": household.live(name)} for name, about in SOURCES.items()]


# ── what the sky is doing, in words ────────────────────────────────────────

_SKY = {
    0: ("clear", "sun"), 1: ("mostly clear", "sun"), 2: ("partly cloudy", "cloud-sun"), 3: ("overcast", "cloud"),
    45: ("fog", "fog"), 48: ("freezing fog", "fog"),
    51: ("light drizzle", "rain"), 53: ("drizzle", "rain"), 55: ("heavy drizzle", "rain"),
    56: ("light freezing drizzle", "rain"), 57: ("freezing drizzle", "rain"),
    61: ("light rain", "rain"), 63: ("rain", "rain"), 65: ("heavy rain", "rain"),
    66: ("light freezing rain", "rain"), 67: ("freezing rain", "rain"),
    71: ("light snow", "snow"), 73: ("snow", "snow"), 75: ("heavy snow", "snow"), 77: ("snow grains", "snow"),
    80: ("light rain showers", "rain"), 81: ("rain showers", "rain"), 82: ("violent rain showers", "rain"),
    85: ("snow showers", "snow"), 86: ("heavy snow showers", "snow"),
    95: ("thunderstorms", "storm"), 96: ("thunderstorms with hail", "storm"), 99: ("thunderstorms with heavy hail", "storm"),
}


def sky(code: Any, night: bool = False) -> Tuple[str, str]:
    """``(words, icon name)`` for a WMO weather code."""
    try:
        words, icon = _SKY.get(int(code), ("unsettled", "cloud"))
    except (TypeError, ValueError):
        words, icon = "unsettled", "cloud"
    if night and icon in ("sun", "cloud-sun"):
        icon = "moon" if icon == "sun" else "cloud"
    return words, icon


def _uv_word(index: float) -> str:
    return "low" if index < 3 else "moderate" if index < 6 else "high" if index < 8 else "very high" if index < 11 else "extreme"


def _air_word(index: float, scale: str) -> str:
    if scale == "US":
        steps = ((50, "good"), (100, "moderate"), (150, "unhealthy for sensitive people"), (200, "unhealthy"), (300, "very unhealthy"))
        return next((word for limit, word in steps if index <= limit), "hazardous")
    steps = ((20, "good"), (40, "fair"), (60, "moderate"), (80, "poor"), (100, "very poor"))
    return next((word for limit, word in steps if index <= limit), "extremely poor")


# ── fetching ───────────────────────────────────────────────────────────────

def _cache_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "almanac", "weather.json")


def _disk() -> Dict[str, Any]:
    try:
        with open(_cache_path(), "r", encoding="utf-8") as handle:
            kept = json.load(handle)
        return kept if isinstance(kept, dict) else {}
    except (OSError, ValueError):
        return {}


def _keep(key: str, found: Dict[str, Any]) -> None:
    with _lock:
        _memory[key] = found
        for old in sorted(_memory, key=lambda k: _memory[k].get("fetched", 0))[:-12]:
            del _memory[old]
    try:
        from core.atomic_io import atomic_write_json
        kept = _disk()
        kept[key] = found
        for old in sorted(kept, key=lambda k: kept[k].get("fetched", 0) if isinstance(kept[k], dict) else 0)[:-8]:
            del kept[old]
        os.makedirs(os.path.dirname(_cache_path()), exist_ok=True)
        atomic_write_json(_cache_path(), kept)
    except Exception as problem:                         # a forecast that could not be kept is still a forecast
        logger.debug("forecast not kept on disk: %s", problem)


def _get(url: str, params: Dict[str, Any], accept: str = "application/json", timeout: float = _TIMEOUT, tries: int = 3) -> Dict[str, Any]:
    """One GET, as JSON. A busy answer (429, 5xx) or a dropped connection is asked again after a short wait:
    the forecast service turns away requests that arrive in a burst and answers the next one."""
    import httpx
    last: Optional[Exception] = None
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=4.0), follow_redirects=True) as client:
        for attempt in range(max(1, tries)):
            if attempt:
                time.sleep(0.7 * attempt + 0.3)
            try:
                response = client.get(url, params=params, headers={"User-Agent": AGENT, "Accept": accept})
            except httpx.TransportError as problem:
                last = problem
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last = httpx.HTTPStatusError(f"{response.status_code} from {response.url.host}", request=response.request, response=response)
                continue
            response.raise_for_status()
            found = response.json()
            if not isinstance(found, dict):
                raise ValueError("not an object")
            return found
    raise last or RuntimeError("no answer")


def _in_the_united_states(lat: float, lon: float) -> bool:
    boxes = ((24.0, 50.0, -125.5, -66.0), (51.0, 72.0, -180.0, -129.0), (18.5, 22.6, -161.0, -154.0), (17.5, 18.8, -67.5, -64.5))
    return any(south <= lat <= north and west <= lon <= east for south, north, west, east in boxes)


def _series(block: Any, name: str) -> list:
    values = block.get(name) if isinstance(block, dict) else None
    return values if isinstance(values, list) else []


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _read_forecast(raw: Dict[str, Any], units: str) -> Dict[str, Any]:
    """Open-Meteo's answer, reduced to what is used and named in house words."""
    now, daily, hourly = raw.get("current") or {}, raw.get("daily") or {}, raw.get("hourly") or {}
    days = []
    for index, day in enumerate(_series(daily, "time")):
        def at(name: str, position: int = index):
            values = _series(daily, name)
            return _number(values[position]) if position < len(values) else None
        days.append({"date": str(day), "code": at("weather_code"), "high": at("temperature_2m_max"), "low": at("temperature_2m_min"),
                     "rain_chance": at("precipitation_probability_max"), "rain": at("precipitation_sum"), "snow": at("snowfall_sum"),
                     "wind": at("wind_speed_10m_max"), "gust": at("wind_gusts_10m_max"), "uv": at("uv_index_max")})
    hours = []
    for index, moment in enumerate(_series(hourly, "time")):
        def at(name: str, position: int = index):
            values = _series(hourly, name)
            return _number(values[position]) if position < len(values) else None
        hours.append({"time": str(moment), "temp": at("temperature_2m"), "rain_chance": at("precipitation_probability"),
                      "rain": at("precipitation"), "code": at("weather_code"), "wind": at("wind_speed_10m")})
    if not days:
        raise ValueError("no days in the forecast")
    return {
        "zone": str(raw.get("timezone") or ""), "units": units,
        "symbols": {"temp": "°F", "wind": "mph", "rain": "in"} if units == "us" else {"temp": "°C", "wind": "km/h", "rain": "mm"},
        "now": {"time": str(now.get("time") or ""), "temp": _number(now.get("temperature_2m")), "feels": _number(now.get("apparent_temperature")),
                "humidity": _number(now.get("relative_humidity_2m")), "code": _number(now.get("weather_code")),
                "wind": _number(now.get("wind_speed_10m")), "gust": _number(now.get("wind_gusts_10m")),
                "wind_from": _number(now.get("wind_direction_10m")), "day": bool(now.get("is_day", 1))} if now else None,
        "days": days, "hours": hours,
    }


def _read_alerts(raw: Dict[str, Any]) -> List[Dict[str, str]]:
    found = []
    for feature in raw.get("features") or []:
        about = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(about, dict) or about.get("status") not in (None, "Actual") or about.get("messageType") == "Cancel":
            continue
        found.append({"event": str(about.get("event") or "Weather alert")[:80], "severity": str(about.get("severity") or "")[:20],
                      "until": str(about.get("ends") or about.get("expires") or "")[:40],
                      "headline": re.sub(r"\s+", " ", str(about.get("headline") or ""))[:300],
                      "instruction": re.sub(r"\s+", " ", str(about.get("instruction") or ""))[:500],
                      "sender": str(about.get("senderName") or "")[:60]})
    order = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3}
    return sorted(found, key=lambda alert: order.get(alert["severity"], 4))[:6]


def _open_meteo(lat: float, lon: float, units: str) -> Dict[str, Any]:
    imperial = units == "us"
    raw = _get("https://api.open-meteo.com/v1/forecast", {
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": DAYS,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,wind_gusts_10m,wind_direction_10m,is_day",
        "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,snowfall_sum,"
                 "wind_speed_10m_max,wind_gusts_10m_max,uv_index_max",
        "temperature_unit": "fahrenheit" if imperial else "celsius", "wind_speed_unit": "mph" if imperial else "kmh",
        "precipitation_unit": "inch" if imperial else "mm"})
    return _read_forecast(raw, units)


_COMPASS = {name: index * 22.5 for index, name in enumerate(
    ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"))}


def code_for(words: str) -> int:
    """The nearest weather code for a forecaster's few words ("Chance Showers And Thunderstorms")."""
    text = str(words or "").lower()
    light = bool(re.search(r"\b(?:slight|light|chance|isolated|scattered|patchy)\b", text))
    heavy = "heavy" in text
    if re.search(r"thunder|t-?storm", text):
        return 95
    if re.search(r"freezing rain|sleet|ice pellets|wintry mix|freezing drizzle", text):
        return 66 if light else 67
    if re.search(r"snow|flurr|blizzard", text):
        return 75 if heavy else 71 if light else 73
    if "drizzle" in text:
        return 51 if light else 53
    if re.search(r"shower", text):
        return 82 if heavy else 80 if light else 81
    if "rain" in text:
        return 65 if heavy else 61 if light else 63
    if re.search(r"fog|haze|mist|smoke", text):
        return 45
    if re.search(r"mostly cloudy|overcast|^cloudy|considerable cloud", text):
        return 3
    if re.search(r"partly|mostly sunny|mostly clear", text):
        return 2 if "partly" in text else 1
    return 0 if re.search(r"sunny|clear|fair", text) else 3


def _read_nws(hourly: Dict[str, Any], zone: str, units: str) -> Dict[str, Any]:
    """The US service's hour-by-hour forecast, in the same shape as the other source's."""
    hours: List[Dict[str, Any]] = []
    extra: List[Dict[str, Any]] = []
    for period in ((hourly.get("properties") or {}).get("periods") or []):
        start = str(period.get("startTime") or "")
        temp = _number(period.get("temperature"))
        if len(start) < 16 or temp is None:
            continue
        speeds = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", str(period.get("windSpeed") or ""))]
        chance = _number((period.get("probabilityOfPrecipitation") or {}).get("value"))
        hours.append({"time": start[:16], "temp": temp, "rain_chance": chance or 0.0, "rain": None,
                      "code": float(code_for(period.get("shortForecast"))), "wind": max(speeds) if speeds else None})
        extra.append({"humidity": _number((period.get("relativeHumidity") or {}).get("value")),
                      "from": _COMPASS.get(str(period.get("windDirection") or "").upper()), "day": bool(period.get("isDaytime", True))})
    if not hours:
        raise ValueError("no hours in the forecast")
    days = []
    for day in sorted({hour["time"][:10] for hour in hours}):
        mine = [hour for hour in hours if hour["time"].startswith(day)]
        lit = [hour for hour in mine if 7 <= int(hour["time"][11:13]) <= 19] or mine
        winds = [hour["wind"] for hour in mine if hour["wind"] is not None]
        days.append({"date": day, "code": max((hour["code"] for hour in lit), key=lambda code: (code >= 45, code)),
                     "high": max(hour["temp"] for hour in mine), "low": min(hour["temp"] for hour in mine),
                     "rain_chance": max(hour["rain_chance"] for hour in mine), "rain": None, "snow": None,
                     "wind": max(winds) if winds else None, "gust": None, "uv": None, "part": len(mine) < 20})
    first = hours[0]
    return {
        "zone": zone, "units": units,
        "symbols": {"temp": "°F", "wind": "mph", "rain": "in"} if units == "us" else {"temp": "°C", "wind": "km/h", "rain": "mm"},
        "now": {"time": first["time"], "temp": first["temp"], "feels": None, "humidity": extra[0]["humidity"], "code": first["code"],
                "wind": first["wind"], "gust": None, "wind_from": extra[0]["from"], "day": extra[0]["day"]},
        "days": days[:DAYS], "hours": hours,
    }


def _nws(lat: float, lon: float, units: str) -> Dict[str, Any]:
    if not _in_the_united_states(lat, lon):
        raise LookupError("outside the United States")
    point = (_get(f"https://api.weather.gov/points/{lat},{lon}", {}, accept="application/geo+json", tries=2).get("properties") or {})
    address = str(point.get("forecastHourly") or "")
    if not address.startswith("https://api.weather.gov/"):
        raise ValueError("no forecast for that point")
    hourly = _get(address, {"units": "us" if units == "us" else "si"}, accept="application/geo+json", tries=2)
    return _read_nws(hourly, str(point.get("timeZone") or ""), units)


# The forecast sources, in the order they are asked. Each takes (lat, lon, units) and returns the fields
# ``_read_forecast`` returns; the first to answer wins. To add one, write its function and list it here.
PROVIDERS = (("Open-Meteo", _open_meteo), ("US National Weather Service", _nws))
_RESTING = 180.0                # seconds a source that just failed is asked last instead of first
_failed_at: Dict[str, float] = {}


def forecast(lat: float, lon: float, units: Optional[str] = None) -> Dict[str, Any]:
    """The forecast for a position. Raises :class:`WeatherProblem` when there is none to give.

    The result carries ``by`` (who it is from), ``fetched`` (when it was got) and ``stale`` ("" for a fresh
    one; "offline" or "failed" when this is the last one kept because a new one could not be had)."""
    from services import household
    from services.search import netstate

    units = units if units in household.UNITS else household.units()
    if os.environ.get("AEGIS_WEATHER_DISABLED"):          # the test suite: no test asks an outside service
        raise WeatherProblem("off", "The forecast is switched off.")
    if not household.live("weather"):
        raise WeatherProblem("off", "The weather forecast is switched off for this household.")
    lat, lon = round(float(lat), 2), round(float(lon), 2)
    key = f"{lat:.2f},{lon:.2f},{units}"
    now = time.time()
    with _lock:
        kept = _memory.get(key)
    if kept and now - kept.get("fetched", 0) < FRESH:
        return {**kept, "stale": ""}
    if kept is None:
        on_disk = _disk().get(key)
        kept = on_disk if isinstance(on_disk, dict) and on_disk.get("days") else None
        if kept and now - kept.get("fetched", 0) < FRESH:
            with _lock:
                _memory[key] = kept
            return {**kept, "stale": ""}

    def last_one(reason: str, message: str) -> Dict[str, Any]:
        if kept and now - kept.get("fetched", 0) < WORTH_READING:
            return {**kept, "stale": reason}
        raise WeatherProblem(reason, message)

    if not netstate.internet_available():
        return last_one("offline", "No forecast: the internet is not reachable right now.")
    found = None
    # A source that failed a moment ago is asked after the others, so one bad hour at a weather service
    # costs a person a few seconds once, not on every question.
    for name, ask in sorted(PROVIDERS, key=lambda provider: now - _failed_at.get(provider[0], 0.0) < _RESTING):
        try:
            found = ask(lat, lon, units)
            found["by"] = name
            _failed_at.pop(name, None)
            break
        except LookupError:
            continue                                     # that source does not cover this place
        except Exception as problem:
            _failed_at[name] = now
            logger.info("no forecast from %s (%s: %s)", name, type(problem).__name__, str(problem)[:120])
    if found is None:
        return last_one("failed", "No forecast: the weather services are not answering right now.")
    found.update({"fetched": now, "lat": lat, "lon": lon, "alerts": None, "air": None})
    if household.live("alerts") and _in_the_united_states(lat, lon):
        try:
            found["alerts"] = _read_alerts(_get("https://api.weather.gov/alerts/active", {"point": f"{lat},{lon}"},
                                                accept="application/geo+json", timeout=6.0, tries=2))
        except Exception as problem:
            logger.info("weather alerts not fetched (%s)", type(problem).__name__)
    if household.live("air"):
        try:
            air = (_get("https://air-quality-api.open-meteo.com/v1/air-quality",
                        {"latitude": lat, "longitude": lon, "timezone": "auto", "current": "us_aqi,european_aqi,pm2_5,pm10"},
                        timeout=6.0, tries=1).get("current") or {})
            index = _number(air.get("us_aqi" if units == "us" else "european_aqi"))
            if index is not None:
                scale = "US" if units == "us" else "European"
                found["air"] = {"index": round(index), "scale": scale, "word": _air_word(index, scale),
                                "pm25": _number(air.get("pm2_5")), "pm10": _number(air.get("pm10"))}
        except Exception as problem:
            logger.info("air quality not fetched (%s)", type(problem).__name__)
    _keep(key, found)
    return {**found, "stale": ""}


# ── saying it ──────────────────────────────────────────────────────────────

def _whole(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.0f}".replace("-0", "0")


def _amount(value: Optional[float], units: str) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}" if units == "us" else f"{value:.1f}"


def _clock(moment: datetime) -> str:
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _hour_word(hour: int) -> str:
    return "midnight" if hour % 24 == 0 else "noon" if hour == 12 else f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


def _zone(found: Dict[str, Any], fallback: str = ""):
    from zoneinfo import ZoneInfo
    for name in (found.get("zone"), fallback):
        if name:
            try:
                return ZoneInfo(name)
            except Exception:
                continue
    return timezone.utc


def _fetched_words(found: Dict[str, Any], zone) -> str:
    at = datetime.fromtimestamp(found.get("fetched") or time.time(), zone)
    today = datetime.now(zone).date()
    day = "" if at.date() == today else " yesterday" if at.date() == today - timedelta(days=1) else f" on {at:%A}"
    return f"{_clock(at)}{day}"


_PARTS = (("Morning", 6, 12), ("Afternoon", 12, 18), ("Evening", 18, 24))


def _parts_of_day(found: Dict[str, Any], day: str, from_hour: int = 0) -> List[Dict[str, Any]]:
    """Morning, afternoon and evening of one day: the temperature range, the chance of rain and the sky."""
    out = []
    for label, start, end in _PARTS:
        if end <= from_hour:
            continue
        hours = [hour for hour in found["hours"] if hour["time"].startswith(day) and start <= int(hour["time"][11:13] or 0) < end
                 and int(hour["time"][11:13] or 0) >= from_hour and hour["temp"] is not None]
        if not hours:
            continue
        temps = [hour["temp"] for hour in hours]
        chances = [hour["rain_chance"] for hour in hours if hour["rain_chance"] is not None]
        codes = [int(hour["code"]) for hour in hours if hour["code"] is not None]
        worst = max(codes, key=lambda code: (code >= 51, code)) if codes else None
        out.append({"label": label, "low": min(temps), "high": max(temps), "rain_chance": max(chances) if chances else None,
                    "sky": sky(worst)[0] if worst is not None else "", "icon": sky(worst)[1] if worst is not None else "cloud"})
    return out


def _alert_until(alert: Dict[str, str], zone) -> str:
    try:
        ends = datetime.fromisoformat(alert["until"]).astimezone(zone)
    except (ValueError, TypeError):
        return ""
    return f"until {ends:%a} {_clock(ends)}"


def _alert_lines(found: Dict[str, Any], zone) -> List[str]:
    alerts = found.get("alerts")
    if alerts is None:
        return []
    if not alerts:
        return ["Weather alerts (US National Weather Service): none in effect."]
    lines = ["Weather alerts in effect (US National Weather Service):"]
    for alert in alerts:
        line = f"- {alert['event']}"
        until = _alert_until(alert, zone)
        if until:
            line += f", {until}"
        if alert["instruction"]:
            line += f". {alert['instruction'][:260]}"
        lines.append(line)
    return lines


def _day_block(found: Dict[str, Any], index: int, today: date, zone, label: str) -> Tuple[List[str], Dict[str, Any]]:
    """One day as labelled lines for the assistant and as a card for the screen."""
    day = found["days"][index]
    symbols, units = found["symbols"], found["units"]
    when = date.fromisoformat(day["date"])
    is_today = when == today
    words, icon = sky(day["code"])
    title = "Today" if is_today else "Tomorrow" if when == today + timedelta(days=1) else f"{when:%A}"
    lines = [f"Forecast for {label} — {when:%A}, {when:%B} {when.day}, {when.year}" + (" (today)" if is_today else "")]
    now = found.get("now") if is_today else None
    tiles = []
    if now and now.get("temp") is not None:
        current_words = sky(now.get("code"), night=not now.get("day", True))[0]
        line = f"Right now: {_whole(now['temp'])} {symbols['temp']}"
        if now.get("feels") is not None and abs(now["feels"] - now["temp"]) >= 3:
            line += f" (feels like {_whole(now['feels'])} {symbols['temp']})"
        line += f", {current_words}"
        if now.get("wind") is not None:
            from services.almanac.coordinates import compass
            direction = f" from the {compass(now['wind_from'])}" if now.get("wind_from") is not None else ""
            line += f", wind {_whole(now['wind'])} {symbols['wind']}{direction}"
        if now.get("humidity") is not None:
            line += f", humidity {_whole(now['humidity'])}%"
        lines.append(line + ".")
        tiles.append({"label": "Now", "value": f"{_whole(now['temp'])}°", "icon": sky(now.get("code"), night=not now.get("day", True))[1]})
    lines.append(f"Sky: {words}.")
    rest = " for the rest of the day" if day.get("part") and is_today else " (only part of that day is in the forecast)" if day.get("part") else ""
    lines.append(f"High {_whole(day['high'])} {symbols['temp']}, low {_whole(day['low'])} {symbols['temp']}{rest}.")
    tiles += [{"label": "High", "value": f"{_whole(day['high'])}°"}, {"label": "Low", "value": f"{_whole(day['low'])}°"}]
    if day["rain_chance"] is not None:
        wet = "snow" if (day["snow"] or 0) > 0 and (day["code"] or 0) in (71, 73, 75, 77, 85, 86) else "rain"
        line = f"Chance of {wet}: {_whole(day['rain_chance'])}%"
        if (day["rain"] or 0) > 0:
            line += f" ({_amount(day['rain'], units)} {symbols['rain']} expected)"
        lines.append(line + ".")
        tiles.append({"label": wet.title(), "value": f"{_whole(day['rain_chance'])}%"})
    if day["wind"] is not None:
        gust = f", gusts to {_whole(day['gust'])} {symbols['wind']}" if day["gust"] is not None and day["gust"] - day["wind"] >= 5 else ""
        lines.append(f"Wind: up to {_whole(day['wind'])} {symbols['wind']}{gust}.")
        tiles.append({"label": "Wind", "value": f"{_whole(day['wind'])} {symbols['wind']}"})
    if day["uv"] is not None and day["uv"] >= 3:
        lines.append(f"UV index: up to {_whole(day['uv'])} ({_uv_word(day['uv'])}).")
    from_hour = datetime.now(zone).hour if is_today else 0
    parts = _parts_of_day(found, day["date"], from_hour)
    if parts:
        said = []
        for part in parts:
            span = _whole(part["low"]) if round(part["low"]) == round(part["high"]) else f"{_whole(part['low'])}–{_whole(part['high'])}"
            chance = f", {_whole(part['rain_chance'])}% chance of rain" if part["rain_chance"] else ""
            said.append(f"{part['label']} {span} {symbols['temp']}, {part['sky']}{chance}")
        lines.append(("Rest of the day — " if is_today and from_hour >= 6 else "Through the day — ") + "; ".join(said) + ".")
    card: Dict[str, Any] = {
        "type": "weather", "title": title, "place": label, "date": f"{when:%A}, {when:%B} {when.day}", "summary": words.capitalize(),
        "icon": icon, "tiles": tiles,
        "parts": [{"label": part["label"], "icon": part["icon"],
                   "text": (_whole(part["low"]) if round(part["low"]) == round(part["high"]) else f"{_whole(part['low'])}–{_whole(part['high'])}") + "°"
                           + (f" · {_whole(part['rain_chance'])}%" if part["rain_chance"] else "")} for part in parts],
    }
    if is_today and found.get("air"):
        air = found["air"]
        lines.append(f"Air quality now: {air['index']} on the {air['scale']} index ({air['word']}).")
        card["air"] = f"Air quality: {air['word']}"
    return lines, card


def _week_block(found: Dict[str, Any], today: date, label: str, start: int = 0, count: int = DAYS) -> Tuple[List[str], Dict[str, Any]]:
    symbols = found["symbols"]
    days = found["days"][start:start + count]
    first, last = date.fromisoformat(days[0]["date"]), date.fromisoformat(days[-1]["date"])
    lines = [f"Forecast for {label}, {first:%A} {first:%B} {first.day} to {last:%A} {last:%B} {last.day}",
             f"Day | Sky | High {symbols['temp']} | Low {symbols['temp']} | Chance of rain | Wind {symbols['wind']}"]
    rows = []
    for day in days:
        when = date.fromisoformat(day["date"])
        words, icon = sky(day["code"])
        name = "Today" if when == today else f"{when:%a}"
        lines.append(f"{when:%A} {when:%b} {when.day}{' (today)' if when == today else ''} | {words} | {_whole(day['high'])} | {_whole(day['low'])} | "
                     f"{_whole(day['rain_chance'])}% | up to {_whole(day['wind'])}")
        rows.append({"label": name, "date": f"{when:%b} {when.day}", "icon": icon, "summary": words.capitalize(),
                     "high": f"{_whole(day['high'])}°", "low": f"{_whole(day['low'])}°",
                     "rain": f"{_whole(day['rain_chance'])}%" if day["rain_chance"] is not None else ""})
    title = "This weekend" if count <= 3 and first.weekday() >= 4 else "The week ahead" if count >= 5 else "The days ahead"
    return lines, {"type": "weather", "title": title, "place": label, "days": rows}


_WEEK = re.compile(r"^(?:(?:this|the|next|coming|whole)\s+)*(?:week|7\s*days?|seven\s+days|week\s+ahead|next\s+few\s+days|few\s+days|coming\s+days|"
                   r"days\s+ahead|outlook|5\s*days?|five\s+days|10\s*days?)$", re.I)
_WEEKEND = re.compile(r"^(?:(?:this|the|next|coming)\s+)?weekend$", re.I)


def answer(args: Dict[str, Any], owner: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """``(text, card)`` for a weather question: ``{"place"?, "when"?}``. Raises :class:`WeatherProblem`.

    ``place`` defaults to home. ``when`` is "now", "today", "tonight", "tomorrow", a weekday, a date, "weekend" or
    "week"; left out, it is today."""
    from services import household
    from services.almanac import dates, places

    def said(*names: str) -> str:
        for name in names:
            value = args.get(name)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    try:
        where = places.resolve(said("place", "location", "at", "in", "_text"), owner)
    except places.PlaceError as problem:
        raise WeatherProblem("place", str(problem)) from None
    found = forecast(where.lat, where.lon, household.units())
    zone = _zone(found, where.zone)
    today = datetime.now(zone).date()
    asked = re.sub(r"\s+", " ", said("when", "date", "day", "on").lower()).strip(" .?")
    asked = re.sub(r"^(?:for|on|the)\s+", "", asked)
    known = [date.fromisoformat(day["date"]) for day in found["days"]]
    if _WEEK.match(asked):
        start = next((i for i, day in enumerate(known) if day >= today), 0)
        lines, card = _week_block(found, today, where.label, start, DAYS)
    elif _WEEKEND.match(asked):
        saturday = today + timedelta(days=(5 - today.weekday()) % 7) if today.weekday() != 6 else today
        wanted = [i for i, day in enumerate(known) if saturday <= day <= saturday + timedelta(days=1 if today.weekday() != 6 else 0)]
        if not wanted:
            raise WeatherProblem("range", f"The forecast reaches {DAYS} days ahead; the weekend is not in it yet.")
        lines, card = _week_block(found, today, where.label, wanted[0], len(wanted))
    else:
        try:
            wanted_day = dates.parse(asked or "today", today)
        except dates.DateError as problem:
            raise WeatherProblem("range", str(problem)) from None
        if wanted_day not in known:
            if wanted_day < today:
                raise WeatherProblem("range", "That day is past: this is a forecast, not a record of what the weather was.")
            raise WeatherProblem("range", f"The forecast reaches {DAYS} days ahead, to {known[-1]:%A, %B} {known[-1].day}. "
                                          f"There is no forecast yet for {wanted_day:%A, %B} {wanted_day.day}.")
        lines, card = _day_block(found, known.index(wanted_day), today, zone, where.label)
        if asked in ("tonight", "this evening"):
            card["title"] = "Tonight"
    lines += _alert_lines(found, zone)
    alerts = found.get("alerts") or []
    if alerts:
        card["alerts"] = [{"event": alert["event"], "until": _alert_until(alert, zone), "text": alert["instruction"] or alert["headline"]}
                          for alert in alerts]
    by = str(found.get("by") or "the weather service")
    source = f"{by}, fetched {_fetched_words(found, zone)}"
    if found.get("stale"):
        why = "The internet is not reachable right now" if found["stale"] == "offline" else "The weather services are not answering right now"
        lines.insert(1, f"{why}: this is the last forecast fetched, at {_fetched_words(found, zone)}. Say so.")
        card["stale"] = True
        card["source"] = (f"Last forecast, from {_fetched_words(found, zone)} "
                          + ("(the internet is down)" if found["stale"] == "offline" else "(no newer one could be had)"))
    else:
        card["source"] = f"Forecast: {by}, {_fetched_words(found, zone)}"
    lines.append(f"Source: {source}. Temperatures in {found['symbols']['temp']}, wind in {found['symbols']['wind']}.")
    if where.note:
        lines.append(where.note)
    return "\n".join(lines), card


def today_line(owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Today at home as one glanceable line for a start screen — or None when there is no home position or no
    forecast. Never raises and never waits long: a start screen must not hang on a weather service."""
    from services.almanac import places
    try:
        if places.home(owner) is None:
            return None
        _text, card = answer({"when": "today"}, owner)
    except WeatherProblem:
        return None
    except Exception:
        logger.debug("today's line not made", exc_info=True)
        return None
    return card
