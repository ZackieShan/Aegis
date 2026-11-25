"""The almanac as agent tools: exact answers, worked out rather than recalled.

``convert_units``  5 ft 10 in → cm, 72 °F → °C, 30 mpg → L/100 km
``map_position``   a place in every notation (lat/long, UTM, MGRS); distance and bearing
``sun_moon``       sunrise, sunset, twilight, moonrise, the moon's phase
``date_math``      days between dates, a date some time on, the weekday
``weather``        the forecast, weather alerts and air quality (the one outside source)

All but ``weather`` is arithmetic in ``services/almanac`` — no model, no network — so a
call costs milliseconds and the answer is the same every time. Each answer also carries a
``card``: the same thing as the few large figures a screen shows (see ``docs/cards.md``).
"""

import json
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from src.constants import MAX_OUTPUT_CHARS


def _args(content: str) -> dict:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {"_text": raw} if raw else {}


def _owner(ctx) -> Optional[str]:
    return (ctx.get("owner") if isinstance(ctx, dict) else "") or None


def _text(args: dict, *names: str) -> str:
    for name in names:
        value = args.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _fail(tool: str, problem) -> dict:
    return {"error": f"{tool}: {problem}", "exit_code": 1}


def _ok(text: str, card: Optional[dict] = None) -> dict:
    found = {"output": text[:MAX_OUTPUT_CHARS], "exit_code": 0}
    if card:
        found["card"] = card
    return found


def convert_card(found: dict) -> dict:
    """A conversion as one large figure: what was asked, and what it comes to, rounded for reading."""
    import re
    from services.almanac.units import show

    answer = str(found.get("answer") or "")
    single = re.fullmatch(r"-?[\d.,]+(?:e[-+]?\d+)?\s*(.*)", answer)
    if single and isinstance(found.get("value"), (int, float)):
        answer = f"{show(found['value'], 4)} {single.group(1)}".strip()
    also = [str(other.get("text") or "") for other in (found.get("also") or [])[1:3] if other.get("text")]
    return {"type": "result", "label": f"{found.get('from') or ''} is", "value": answer, "detail": " · ".join(also),
            "note": str(found.get("note") or ""), "source": "Worked out"}


# ── units ──────────────────────────────────────────────────────────────────

class ConvertUnitsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from services.almanac import units

        args = _args(content)
        amount = _text(args, "amount", "value", "quantity", "_text")
        if not amount:
            return _fail("convert_units", 'takes JSON: {"amount": "5 ft 10 in", "to": "cm"} — "to" may be left out.')
        try:
            found = units.convert(amount, _text(args, "to", "into", "unit"), source=_text(args, "from", "from_unit"))
        except units.UnitError as problem:
            return _fail("convert_units", problem)
        except (ValueError, OverflowError, ZeroDivisionError) as problem:
            return _fail("convert_units", f"that could not be worked out ({problem}).")
        return _ok(found["text"] + (f"\n{found['note']}" if found.get("note") else ""), convert_card(found))


# ── places ─────────────────────────────────────────────────────────────────

def _distance_text(metres: float) -> str:
    from services.almanac.units import show

    if metres < 1000:
        return f"{show(metres, 4)} m ({show(metres / 0.3048, 4)} ft)"
    return f"{show(metres / 1000, 5)} km ({show(metres / 1609.344, 5)} mi, {show(metres / 1852, 5)} nautical miles)"


def _bearing_text(bearing: float) -> str:
    from services.almanac.coordinates import compass

    return f"{bearing:.1f}° ({compass(bearing)}; {bearing * 6400 / 360:.0f} mils)"


def _place_lines(found: dict) -> str:
    """One place, a labelled line for each thing known about it (so nothing is mistaken for something else)."""
    label = str(found.get("label") or "")
    unnamed = label == f"{found['lat']:.4f}, {found['lon']:.4f}"
    if found.get("kind") in ("name", "home"):
        lines = [label]
    elif unnamed:
        lines = ["Position:"]
    else:
        lines = [f"Position: {label} (measured from the middle of that town)"]
    lines.append(f"Latitude, longitude: {found['decimal']}  ({found['dms']})")
    lines.append(f"UTM: {found['utm']}")
    lines.append(f"MGRS: {found['mgrs']}")
    if found.get("elevation_m") is not None:
        lines.append(f"Height above sea level: about {found['elevation_m']:,} m ({found['elevation_m'] / 0.3048:,.0f} ft)")
    if found.get("zone"):
        lines.append(f"Time zone: {found['zone']}")
    if found.get("note"):
        lines.append(found["note"])
    return "\n".join(lines)


class MapPositionTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from services.almanac import coordinates, places, units

        args = _args(content)
        owner = _owner(ctx)
        start, end = _text(args, "from", "start", "origin"), _text(args, "to", "end", "destination")
        place = _text(args, "place", "position", "location", "_text")
        heading, distance = _text(args, "bearing", "azimuth", "heading"), _text(args, "distance")
        try:
            if (start or place) and heading and distance:
                origin = places.resolve(start or place, owner)
                try:
                    degrees = units.convert(heading if not heading.replace(".", "", 1).replace("-", "", 1).isdigit() else f"{heading} deg", "deg")["value"]
                    metres = units.convert(distance, "m")["value"]
                except units.UnitError as problem:
                    return _fail("map_position", f'{problem} Give "bearing" in degrees (or mils) and "distance" with its unit, e.g. "5 km".')
                if metres < 0 or metres > 20_000_000:
                    return _fail("map_position", "the distance has to be between 0 and 20,000 km.")
                lat, lon = coordinates.onward((origin.lat, origin.lon), degrees, metres)
                label, zone, height = places._near(lat, lon)
                arrived = places.describe(places.Where(lat, lon, label, zone, "latlon", "", height))
                return _ok(f"{_distance_text(metres)} from {origin.label} on a true bearing of {_bearing_text(degrees % 360)} is:\n"
                           + _place_lines(arrived) + "\nBearings are true (from the pole); a compass needs the local declination applied.")
            if start and end:
                first, second = places.resolve(start, owner), places.resolve(end, owner)
                found = places.between(first, second)
                lines = [f"{first.label} to {second.label}: {_distance_text(found['metres'])} in a straight line.",
                         f"Bearing to set out on: {_bearing_text(found['bearing'])}. Bearing back: {_bearing_text(found['back_bearing'])}.",
                         "Bearings are true (from the pole); a compass needs the local declination applied. Distance is over the "
                         "Earth's surface as the crow flies — a road or trail is longer."]
                for where in (first, second):
                    if where.note:
                        lines.append(where.note)
                return _ok("\n".join(lines))
            if start or end or place:
                return _ok(_place_lines(places.describe(places.resolve(place or start or end, owner))))
        except (places.PlaceError, coordinates.PlaceError) as problem:
            return _fail("map_position", problem)
        return _fail("map_position", 'takes JSON: {"place": "40.7484, -73.9857"} for one place in every notation, {"from": "Tulsa, OK", '
                                     '"to": "Wichita, KS"} for distance and bearing, or {"from": …, "bearing": 45, "distance": "5 km"} for where that leads.')


# ── sun and moon ───────────────────────────────────────────────────────────

def _clock(moment: Optional[datetime]) -> str:
    if moment is None:
        return "—"
    rounded = (moment + timedelta(seconds=30)).replace(second=0, microsecond=0)
    return f"{rounded.hour % 12 or 12}:{rounded.minute:02d} {'AM' if rounded.hour < 12 else 'PM'}"


def _span(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    return f"{minutes // 60} h {minutes % 60:02d} min"


def _short_day(moment: datetime) -> str:
    return f"{moment.strftime('%a')} {moment.strftime('%b')} {moment.day}"


def sky_text(found: dict, label: str, zone_label: str, now: Optional[datetime] = None) -> str:
    """One day's sun and moon as a few plain lines."""
    from services.almanac import dates, sky

    lat, lon = found["lat"], found["lon"]
    lines = [f"Sun and moon at {label} — {dates.written(found['date'])} (times: {zone_label})"]
    if found["sun_state"] == "up":
        lines.append(f"The sun does not set this day (midnight sun). It is highest at {_clock(found['noon'])}, {found['noon_height']:.0f}° up.")
    elif found["sun_state"] == "down":
        note = "it stays below the horizon"
        if found["civil_dawn"] and found["civil_dusk"]:
            note += f", with twilight from {_clock(found['civil_dawn'])} to {_clock(found['civil_dusk'])}"
        lines.append(f"The sun does not rise this day (polar night): {note}.")
    else:
        rise, down = found["sunrise"], found["sunset"]
        if rise and down and down > rise:
            sun_line = f"Sunrise {_clock(rise)} · solar noon {_clock(found['noon'])} (sun {found['noon_height']:.0f}° up) · sunset {_clock(down)}"
        elif rise and down:                              # far north in summer: last evening's sunset falls after midnight
            sun_line = f"The sun sets at {_clock(down)}, rises again at {_clock(rise)} and is still up at midnight"
        elif rise:
            sun_line = f"Sunrise {_clock(rise)}; the sun is still up at midnight"
        else:
            sun_line = f"The sun, up since before midnight, sets at {_clock(down)} and does not rise again this date"
        lines.append(f"{sun_line} · daylight {_span(found['day_length'])}")
        light = []
        if found["civil_dawn"] or found["civil_dusk"]:
            light.append(f"First light (civil dawn) {_clock(found['civil_dawn'])}, last light (civil dusk) {_clock(found['civil_dusk'])}")
        if found["nautical_dawn"] or found["nautical_dusk"]:
            light.append(f"nautical twilight {_clock(found['nautical_dawn'])} to {_clock(found['nautical_dusk'])}")
        if found["astronomical_dawn"] and found["astronomical_dusk"]:
            light.append(f"fully dark before {_clock(found['astronomical_dawn'])} and after {_clock(found['astronomical_dusk'])}")
        else:
            light.append("it does not get fully dark")
        text = "; ".join(light)
        lines.append(text[0].upper() + text[1:] + ".")
    moon = found["moon"]
    moon_line = f"Moon: {moon['name'].lower()}, {moon['lit'] * 100:.0f}% lit ({'growing' if moon['waxing'] else 'shrinking'}), {moon['age_days']:.0f} days old."
    rise, down, after = found["moonrise"], found["moonset"], found.get("moon_sets_after")
    later = f"sets at {_clock(after)} the next day" if after and after.date() != rise.date() else "is still up at midnight" if rise else ""
    if rise and down and down > rise:
        moon_line += f" Rises {_clock(rise)}, sets {_clock(down)}."
    elif rise and down:
        moon_line += f" It sets at {_clock(down)} (the end of the night before), then rises at {_clock(rise)} and {later}."
    elif rise:
        moon_line += f" Rises {_clock(rise)} and {later}."
    elif down:
        moon_line += f" Sets {_clock(down)} and does not rise again this date."
    elif found.get("moon_state"):
        moon_line += " It is above the horizon all day." if found["moon_state"] == "up" else " It stays below the horizon all day."
    lines.append(moon_line)
    lines.append("Coming: " + "; ".join(f"{item['name'].lower()} {_short_day(item['at'])} {_clock(item['at'])}" for item in moon["next"]) + ".")
    if now is not None:
        sun, luna = sky.sun_position(now, lat, lon), sky.moon_position(now, lat, lon)

        def where(body: dict) -> str:
            if body["height"] < -0.3:
                return f"{abs(body['height']):.0f}° below the horizon"
            return f"{max(body['height'], 0):.0f}° above the horizon, bearing {_bearing_text(body['bearing']).split(';')[0]})"

        lines.append(f"At {_clock(now)}: the sun is {where(sun)}; the moon is {where(luna)}.")
    lines.append("Times are for a level horizon at sea level and are good to a minute or two; hills and mountains change what you see.")
    return "\n".join(lines)


def _time_of_day(text: str) -> Optional[tuple]:
    import re

    found = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m?\.?", text.strip().lower()) or re.fullmatch(r"(\d{1,2}):?(\d{2})()", text.strip())
    if not found:
        return None
    hour, minute = int(found.group(1)), int(found.group(2) or 0)
    if found.group(3):
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if found.group(3) == "p" else 0)
    return (hour, minute) if hour < 24 and minute < 60 else None


class AlmanacProblem(ValueError):
    """Why an almanac question could not be answered, in words for the person."""


def sky_summary(found: dict, label: str, zone_label: str) -> dict:
    """One day's sun and moon as the few things a page shows large."""
    moon = found["moon"]
    return {"label": label, "date": f"{found['date']:%A}, {found['date']:%B} {found['date'].day}, {found['date'].year}", "zone": zone_label,
            "state": found["sun_state"], "sunrise": _clock(found["sunrise"]) if found["sunrise"] else None,
            "sunset": _clock(found["sunset"]) if found["sunset"] else None,
            "sunset_first": bool(found["sunrise"] and found["sunset"] and found["sunset"] < found["sunrise"]),
            "daylight": _span(found["day_length"]), "noon": _clock(found["noon"]),
            "first_light": _clock(found["civil_dawn"]) if found["civil_dawn"] else None,
            "last_light": _clock(found["civil_dusk"]) if found["civil_dusk"] else None,
            "dark": _clock(found["astronomical_dusk"]) if found.get("astronomical_dusk") else None,
            "moon": {"name": moon["name"], "lit": round(moon["lit"] * 100), "waxing": bool(moon["waxing"]), "state": found.get("moon_state", ""),
                     "rise": _clock(found["moonrise"]) if found["moonrise"] else None, "set": _clock(found["moonset"]) if found["moonset"] else None,
                     "set_first": bool(found["moonrise"] and found["moonset"] and found["moonset"] < found["moonrise"]),
                     "until": (_clock(found["moon_sets_after"]) if found.get("moon_sets_after") and found["moonrise"]
                               and found["moon_sets_after"].date() != found["moonrise"].date() else None),
                     "next": [{"name": item["name"], "when": f"{_short_day(item['at'])}, {_clock(item['at'])}"} for item in moon["next"]]},
            "south": found["lat"] < 0}


def sun_moon_answer(args: dict, owner: Optional[str]) -> tuple:
    """``(text, summary)`` for a sun-and-moon question; ``summary`` is None for a table of several days.
    Raises :class:`AlmanacProblem` with words for the person."""
    from services.almanac import dates, places, sky

    try:
        where = places.resolve(_text(args, "place", "location", "position", "at", "_text"), owner)
        zone, zone_label, zone_note = places.clock(where, _text(args, "zone", "timezone", "tz"))
    except places.PlaceError as problem:
        raise AlmanacProblem(str(problem)) from None
    now = datetime.now(timezone.utc).astimezone(zone)
    try:
        day = dates.parse(_text(args, "date", "day", "on") or "today", now.date())
    except dates.DateError as problem:
        raise AlmanacProblem(str(problem)) from None
    if not 1900 <= day.year <= 2100:
        raise AlmanacProblem("the formulas are good for the years 1900 to 2100.")
    try:
        count = max(1, min(14, int(args.get("days") or 1)))
    except (TypeError, ValueError):
        count = 1
    at_text = _text(args, "time")
    moment = None
    if at_text and at_text.lower() != "now":
        clock = _time_of_day(at_text)
        if clock is None:
            raise AlmanacProblem(f'“{at_text[:20]}” is not a time of day. Write it like "14:30" or "2:30 pm".')
        moment = datetime(day.year, day.month, day.day, clock[0], clock[1], tzinfo=zone)
    elif day == now.date() and count == 1:
        moment = now
    notes = [note for note in (where.note, zone_note) if note]
    summary = None
    if count == 1:
        found = sky.day(day, where.lat, where.lon, zone)
        text = sky_text(found, where.label, zone_label, moment)
        summary = sky_summary(found, where.label, zone_label)
        summary["notes"] = notes
    else:
        lines = [f"Sun and moon at {where.label}, {count} days from {dates.written(day)} (times: {zone_label})",
                 "Date        | Sunrise  | Sunset   | Daylight    | Moonrise | Moonset  | Moon"]
        for step in range(count):
            found = sky.day(day + timedelta(days=step), where.lat, where.lon, zone)
            moon = found["moon"]
            lines.append(f"{_short_day(datetime(found['date'].year, found['date'].month, found['date'].day)):11} | {_clock(found['sunrise']):8} | "
                         f"{_clock(found['sunset']):8} | {_span(found['day_length']):11} | {_clock(found['moonrise']):8} | {_clock(found['moonset']):8} | "
                         f"{moon['name'].lower()}, {moon['lit'] * 100:.0f}% lit")
        lines.append("— means it does not happen on that date. Times are for a level horizon and are good to a minute or two.")
        text = "\n".join(lines)
    return text + ("\n" + "\n".join(notes) if notes else ""), summary


def sun_card(summary: Optional[dict]) -> Optional[dict]:
    """One day's sun and moon as tiles."""
    if not summary:
        return None
    tiles = []
    if summary.get("state") == "up":
        tiles.append({"label": "Sun", "value": "Up all day"})
    elif summary.get("state") == "down":
        tiles.append({"label": "Sun", "value": "Does not rise"})
    else:
        for label, key in (("Sunrise", "sunrise"), ("Sunset", "sunset"), ("Last light", "last_light"), ("Fully dark", "dark")):
            if summary.get(key):
                tiles.append({"label": label, "value": summary[key]})
        if summary.get("daylight"):
            tiles.append({"label": "Daylight", "value": summary["daylight"]})
    moon = summary.get("moon") or {}
    line = ""
    if moon.get("name"):
        line = f"Moon: {str(moon['name']).lower()}, {moon.get('lit', 0)}% lit"
        if moon.get("rise"):
            line += f", rises {moon['rise']}"
    return {"type": "sun", "title": summary.get("date") or "", "place": summary.get("label") or "", "tiles": tiles, "moon": line,
            "source": "Worked out"}


class SunMoonTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            text, summary = sun_moon_answer(_args(content), _owner(ctx))
        except AlmanacProblem as problem:
            return _fail("sun_moon", problem)
        return _ok(text, sun_card(summary))


# ── the weather ────────────────────────────────────────────────────────────

class WeatherTool:
    """The forecast for home or a named place. The one tool here that asks an outside service; when it
    cannot (offline, switched off) it says so, and the assistant must not fill the gap from memory."""

    async def execute(self, content: str, ctx: dict) -> dict:
        import asyncio
        from services.almanac import weather

        args = _args(content)
        try:
            text, card = await asyncio.to_thread(weather.answer, args, _owner(ctx))
        except weather.WeatherProblem as problem:
            if problem.reason in ("place", "range"):
                return _fail("weather", problem)
            return {"output": f"{problem} Tell the person there is no forecast to give; do not guess the weather.",
                    "exit_code": 0, "card": {"type": "notice", "text": str(problem), "reason": problem.reason}}
        return _ok(text, card)


# ── dates ──────────────────────────────────────────────────────────────────

class DateMathTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from services.almanac import dates

        args = _args(content)
        try:
            from src.user_time import now_user_local
            today = now_user_local().date()
        except Exception:
            today = date.today()
        start, end, amount = _text(args, "date", "from", "start", "_text"), _text(args, "to", "until", "end"), _text(args, "add", "plus", "move", "offset")
        subtract = _text(args, "subtract", "minus")
        def card(label: str, value: str, detail: str = "") -> dict:
            return {"type": "result", "label": label, "value": value, "detail": detail, "source": "Worked out"}

        try:
            if subtract and not amount:
                first = dates.parse(start or "today", today)
                moved, notes = dates.move(first, subtract + " ago")
                return _ok("\n".join([f"{subtract} before {dates.written(first)}: {dates.about(moved, today)}", *notes]),
                           card(f"{subtract} before {dates.written(first)}", dates.written(moved)))
            if amount:
                first = dates.parse(start or "today", today)
                moved, notes = dates.move(first, amount)
                return _ok("\n".join([f"{dates.written(first)} moved by {amount}: {dates.about(moved, today)}", *notes]),
                           card(f"{amount} from {'today' if first == today else dates.written(first)}", dates.written(moved)))
            if end:
                first, second = dates.parse(start or "today", today), dates.parse(end, today)
                gap = dates.apart(first, second)
                count = int(gap["days"])
                detail = f"{gap['weeks']} week{'' if gap['weeks'] == 1 else 's'} and {gap['odd_days']} day{'' if gap['odd_days'] == 1 else 's'} · " \
                         f"{gap['weekdays']} weekday{'' if gap['weekdays'] == 1 else 's'}" if count >= 7 else ""
                edge = dates.written(first if gap["backwards"] else second)
                label = (f"Since {edge}" if gap["backwards"] else f"Until {edge}") if first == today or second == today \
                    else f"{dates.written(first)} to {dates.written(second)}"
                return _ok(dates.apart_text(first, second), card(label, f"{count:,} day{'' if count == 1 else 's'}", detail))
            if start:
                day = dates.parse(start, today)
                return _ok(dates.about(day, today), card(f"{day:%B} {day.day}, {day.year}", f"{day:%A}"))
        except dates.DateError as problem:
            return _fail("date_math", problem)
        return _fail("date_math", 'takes JSON: {"from": "today", "to": "2027-03-12"} for the time between, {"date": "today", "add": "90 days"} '
                                  'for a date some time on, or {"date": "July 4 1976"} for the weekday.')
