"""Going out: "plan a day hike for Saturday" done as one job, the same way every time.

Asked this, the assistant used to answer with a paragraph from general knowledge: two tools of five,
nothing saved. Left to follow a list of steps it did better, but not dependably — one day it skipped the
library, another the reminder. So the steps are code:

    1. the sun      sunset and last light for that day and place (the Almanac)
    2. the weather  the forecast, or "no forecast" said plainly
    3. the library  what the field manuals say to carry and to watch for
    4. one question to the model: from those pages and that forecast, what to pack and what to watch for
    5. the list     saved, named for the outing
    6. a reminder   the evening before

and the answer is a trip card: when to leave and when to turn back, the weather, what to watch for with
the page each came from, the saved list and the reminder. The model writes two short lists and nothing
else; if it cannot be asked, the usual essentials go on the list and the card says so.

``read()`` says whether a message is such a request and what it asks; ``plan()`` does the job, yielding
what a screen shows as it goes. ``routes/chat_routes.py`` drives it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

TOOLS = ("sun_moon", "weather", "library_search", "manage_lists", "manage_reminders")

_KINDS = (
    (r"backpack(?:ing)?", "backpacking trip"), (r"(?:day )?hik(?:e|ing)|trek", "hike"), (r"camp(?:ing)?(?: ?out| trip)?", "camping trip"),
    (r"fish(?:ing)?(?: trip)?", "fishing trip"), (r"hunt(?:ing)?(?: trip)?", "hunt"), (r"picnic", "picnic"), (r"bike ride|cycling", "bike ride"),
    (r"canoe(?:ing)?|kayak(?:ing)?|paddle", "paddle"), (r"ski(?:ing)?(?: trip)?|snowshoe(?:ing)?", "day in the snow"), (r"climb(?:ing)?", "climb"),
    (r"forag(?:e|ing)", "foraging walk"), (r"bird ?watching", "birdwatching walk"), (r"stargazing", "night under the stars"),
    (r"beach day|trip to the beach", "beach day"), (r"road trip", "road trip"), (r"walk", "walk"), (r"day out|day trip|outing|trip to the \w+", "day out"),
)
_DAY = re.compile(r"\b(today|tonight|tomorrow|the day after tomorrow|(?:this |next |on |this coming )?(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
                  r"(?:this |next )?weekend)\b", re.I)
# A place is capitalised words after "near", "in", "at"…; it stops at the end of the sentence ("near Tulsa. When…").
_PLACE = re.compile(r"\b(?:near|in|at|around|outside|by) ((?:(?:St|Mt|Ft|Pt)\. )?[A-Z][\w'-]+(?:,? (?:(?:St|Mt|Ft|Pt)\. )?[A-Z][\w'-]+){0,3})")
_HOURS = re.compile(r"\b(\d+(?:\.\d+)?|a|an|two|three|four|five|six|half an?)[- ]?(?:hours?|hrs?)\b", re.I)
_MILES = re.compile(r"\b(\d+(?:\.\d+)?)[- ]?(?:miles?|mi)\b", re.I)
_WORDS = {"a": 1.0, "an": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0, "six": 6.0, "seven": 7.0, "eight": 8.0,
          "half a": 0.5, "half an": 0.5}
_NUMBER = r"(\d+(?:\.\d+)?|a|an|two|three|four|five|six|seven|eight|half an?)"
_OPENER = r"(?:(?:actually|ok(?:ay)?|no|so|oh)[, ]+)?"
# What a person says back to a plan, and nothing else: "it will be about 6 hours", "make it Sunday instead".
_LENGTH_ONLY = re.compile(
    rf"^{_OPENER}(?:it(?:'s| is| will be|'ll be)|make it|plan (?:it )?for|we(?:'ll| will) be (?:out|gone)(?: for)?|more like)?\s*"
    rf"(?:about|around|roughly|more like|only)?\s*{_NUMBER}[- ]?(?:hours?|hrs?)(?: (?:out and back|round trip|in all|total|long|altogether))?[.!]?"
    r"(?: (?:please )?redo the times[.!]?)?$", re.I)
_DAY_ONLY = re.compile(
    rf"^{_OPENER}(?:what about|how about|make it|change it to|move it to|let'?s (?:do|go|make it)(?: on)?|and)?\s*(?:on )?"
    r"(today|tomorrow|(?:mon|tues|wednes|thurs|fri|satur|sun)day)(?: instead)?[.?!]?$", re.I)

# What goes on the list when the model cannot be asked: the essentials every outdoor manual starts with.
ESSENTIALS = ["Water, at least 2 litres each", "Food and something extra", "Map and compass, or the route saved on a phone", "Warm layer",
              "Rain shell", "Torch with fresh batteries", "First-aid kit", "Knife or multi-tool", "Matches or a lighter", "Phone, charged", "Whistle"]


def read(message: str, today: date) -> Optional[Dict[str, Any]]:
    """What an outing request asks for — the kind, the day, the place and the length — or None when the
    message is not one (services/smart_tools has the trigger)."""
    from services import smart_tools
    if smart_tools.pick(message) is not smart_tools.GOING_OUT:
        return None
    text = " ".join(str(message or "").split())
    kind = next((name for pattern, name in _KINDS if re.search(rf"\b(?:{pattern})\b", text, re.I)), "day out")
    from services.almanac import dates
    said = _DAY.search(text)
    day_words = said.group(1).lower() if said else ""
    try:
        day = dates.parse("saturday" if "weekend" in day_words else (re.sub(r"^(?:on|this coming) ", "", day_words) or "today"), today)
    except dates.DateError:
        day, day_words = today, ""
    place = _PLACE.search(text)
    hours = None
    length = _HOURS.search(text)
    if length:
        hours = _WORDS.get(length.group(1).lower()) or float(length.group(1))
    elif _MILES.search(text) and kind in ("hike", "walk", "backpacking trip", "foraging walk", "birdwatching walk"):
        hours = float(_MILES.search(text).group(1)) / 2.0              # a walking pace with stops
    return {"kind": kind, "day": day, "said_day": bool(day_words), "place": place.group(1).strip(" .,") if place else "",
            "hours": hours if hours and 0.5 <= hours <= 16 else None}


def _last_plan(history) -> Optional[Dict[str, Any]]:
    """What the answer just before this message planned, when that answer was a trip plan."""
    for message in reversed(list(history or [])[-4:]):
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", "")
        if role != "assistant":
            continue
        metadata = (message.get("metadata") if isinstance(message, dict) else getattr(message, "metadata", None)) or {}
        for event in metadata.get("tool_events") or []:
            card = event.get("card") if isinstance(event, dict) else None
            if isinstance(event, dict) and event.get("tool") == "plan_outing" and isinstance(card, dict) and isinstance(card.get("asked"), dict):
                return card["asked"]
        return None                                    # only the last answer counts
    return None


def again(message: str, history, today: date) -> Optional[Dict[str, Any]]:
    """The same outing planned again with one thing changed — "it will be about 6 hours", "make it
    Sunday instead" — when that is all the message says and the answer before it was a trip plan."""
    text = " ".join(str(message or "").split())
    if not text or len(text) > 90:
        return None
    length, another_day = _LENGTH_ONLY.match(text), _DAY_ONLY.match(text)
    if not length and not another_day:
        return None
    before = _last_plan(history)
    if not before:
        return None
    try:
        asked = {"kind": str(before.get("kind") or "day out"), "day": date.fromisoformat(str(before.get("day"))), "said_day": True,
                 "place": str(before.get("place") or ""), "hours": before.get("hours") if isinstance(before.get("hours"), (int, float)) else None}
    except ValueError:
        return None
    if length:
        hours = _WORDS.get(length.group(1).lower()) or float(length.group(1))
        if not 0.5 <= hours <= 16:
            return None
        asked["hours"] = hours
    else:
        from services.almanac import dates
        try:
            asked["day"] = dates.parse(another_day.group(1).lower(), today)
        except dates.DateError:
            return None
    if asked["day"] < today:
        return None
    return asked


def _clock(moment: Optional[datetime]) -> str:
    if moment is None:
        return ""
    moment = (moment + timedelta(seconds=30)).replace(second=0, microsecond=0)
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


_OUT_AND_BACK = ("hike", "walk", "foraging walk", "birdwatching walk", "hunt", "climb", "bike ride", "paddle", "day in the snow")
_OVERNIGHT = ("camping trip", "backpacking trip")
_STAY_PUT = ("picnic", "beach day", "fishing trip")


def timing(kind: str, sky_day: Dict[str, Any], hours: Optional[float]) -> Dict[str, Any]:
    """The times that matter for this kind of outing, from the day's sun: ``{"tiles", "line", "note", "hours"}``.

    Out and back (a hike, a ride, a paddle): when to turn back so as to be out before dark — the way back
    is half the outing (two hours when no length was given), with half an hour in hand — and when to leave.
    A night out: when camp should be up. A picnic or a day by the water: when to pack up. Under the stars:
    when it is fully dark, and the moon."""
    sunset, last_light = sky_day.get("sunset"), sky_day.get("civil_dusk")
    tiles: List[Dict[str, str]] = []
    out: Dict[str, Any] = {"tiles": tiles, "line": "", "note": "", "hours": hours}

    def tile(label: str, moment: Optional[datetime]) -> None:
        if moment is not None:
            tiles.append({"label": label, "value": _clock(moment)})

    same_day = lambda moment: sunset is not None and moment.date() == sunset.date()      # noqa: E731
    if kind == "night under the stars":
        tile("Sunset", sunset)
        tile("Fully dark", sky_day.get("astronomical_dusk"))
        moon = sky_day.get("moon") or {}
        if moon.get("name"):
            rises = f", rises {_clock(sky_day['moonrise'])}" if sky_day.get("moonrise") else ""
            out["note"] = f"Moon: {str(moon['name']).lower()}, {float(moon.get('lit') or 0) * 100:.0f}% lit{rises}. The darker the moon, the more stars."
        if sky_day.get("astronomical_dusk"):
            out["line"] = f"It is fully dark from {_clock(sky_day['astronomical_dusk'])}."
        return out
    if sunset is not None and kind in _OUT_AND_BACK:
        length = hours or 4.0
        turn = sunset - timedelta(hours=length / 2, minutes=30)
        leave = turn - timedelta(hours=length / 2)
        if same_day(turn) and same_day(leave):
            tile("Leave by", leave)
            tile("Turn back by", turn)
            out["line"] = f"Turn back by {_clock(turn)} to be out before dark."
            out["note"] = (f"For {length:g} hours out and back, with half an hour in hand." if hours else
                           f"That allows about {length:g} hours out and back. Tell me how long it really is and I will redo the times.")
            out["hours"] = length
    elif sunset is not None and kind in _OVERNIGHT:
        up = sunset - timedelta(hours=1, minutes=30)
        if same_day(up):
            tile("Camp set up by", up)
            out["line"] = f"Have camp set up by {_clock(up)}, while there is light to do it."
    elif sunset is not None and kind in _STAY_PUT:
        done = sunset - timedelta(minutes=30)
        if same_day(done):
            tile("Pack up by", done)
            out["line"] = f"Pack up by {_clock(done)} to be done before dark."
    tile("Sunset", sunset)
    tile("Last light", last_light)
    if not out["line"] and last_light is not None:
        out["line"] = f"The light is gone by {_clock(last_light)}."
    return out


_WATER = ("paddle", "fishing trip", "beach day")
_ON_FOOT = ("hike", "walk", "backpacking trip", "foraging walk", "birdwatching walk", "hunt", "camping trip", "climb")
_LOST = "lost in the woods what to do stay put signal"


# What can be worth a warning: the library search for it, words a page on it must have, and the plain
# advice said when no model has put the page into a sentence.
_CONCERNS: Dict[str, Any] = {
    "Lightning": ("lightning safety outdoors what to do in a thunderstorm", ("lightning", "thunder"),
                  "At the first thunder get off high ground and away from lone trees and water, and wait half an hour after the last."),
    "Frostbite and deep cold": ("frostbite hypothermia signs prevention cold weather", ("frostbite", "hypotherm", "frostnip"),
                                "Cover fingers, ears and nose, keep moving, and turn back if anyone cannot feel their hands or feet."),
    "Getting cold": ("hypothermia prevention signs wet cold wind", ("hypotherm", "shiver", "wind chill"),
                     "Carry a dry warm layer and put it on before you stop: wet clothes and wind chill a body fast."),
    "Heat": ("heat exhaustion heat stroke prevention water", ("heat exhaustion", "heat stroke", "heatstroke", "heat cramp", "dehydrat"),
             "Drink before you are thirsty, rest in the shade at midday, and stop if anyone feels dizzy or sick."),
    "The water": ("drowning prevention cold water immersion life jacket", ("drown", "life jacket", "lifejacket", "flotation", "immersion"),
                  "Wear a life jacket, and stay out of water you could not swim out of in your clothes."),
    "Other hunters": ("hunting safety blaze orange safe handling in the field", ("blaze orange", "hunter orange", "zone of fire", "muzzle"),
                      "Wear blaze orange, and be sure of your target and of what is beyond it."),
    "Look-alikes": ("poisonous mushrooms plants mistaken for edible", ("poison", "toxic"),
                    "Eat nothing you cannot name with certainty: some deadly kinds pass for edible ones."),
    "Fire and fumes": ("carbon monoxide poisoning tent stove ventilation campfire", ("carbon monoxide", "ventilat", "campfire", "camp fire"),
                       "Never run a stove or a heater inside a closed tent, and drown the fire before you sleep."),
    "Rain": ("staying dry in the rain wet clothing outdoors", ("rain", "wet cloth", "waterproof"),
             "Keep one set of clothes dry in a bag, and put rain gear on before you are wet."),
    "Being caught out after dark": (_LOST, ("lost", "stay put", "signal"),
                                    "Carry a light for each person; if dark catches you, stop, stay together and stay put."),
    "Ticks": ("tick bites prevention removal", ("tick",), "Tuck trousers into socks, and check skin and scalp when you get home."),
    "Snakes": ("snakebite first aid prevention", ("snake",), "Watch where you put hands and feet; step onto a log, not over it."),
    "Blisters and turned ankles": ("blisters sprained ankle first aid on the trail", ("blister", "sprain"),
                                   "Tape a hot spot the moment you feel it, and wear boots you have already walked in."),
    "Losing the way": (_LOST, ("lost", "stay put", "signal"), "Tell someone your route and when you will be back; if lost, stop and stay put."),
}


def concerns(kind: str, numbers: Optional[Dict[str, Any]], units: str = "us", daylight: Optional[float] = None) -> List[Dict[str, str]]:
    """What is worth reading up on before this outing: at most three, the most pressing first, each
    ``{"name", "why", "query"}``.

    Chosen by rule from the forecast (``numbers``: one day of services/almanac/weather, in the household's
    ``units``), the hours of daylight and the kind of outing — so the same outing in the same weather always
    gets the same warnings. ``query`` is what the library is searched for; a model only puts the page it
    finds into a sentence."""
    numbers = numbers or {}
    us = units == "us"

    def celsius(value) -> Optional[float]:
        return None if value is None else ((float(value) - 32) * 5 / 9 if us else float(value))

    high, low = celsius(numbers.get("high")), celsius(numbers.get("low"))
    blow = numbers.get("gust") if numbers.get("gust") is not None else numbers.get("wind")
    gust = None if blow is None else float(blow) * (1.609 if us else 1.0)          # km/h
    code, chance = int(numbers.get("code") or 0), float(numbers.get("rain_chance") or 0)
    degree, speed = ("°F", "mph") if us else ("°C", "km/h")
    storm = code in (95, 96, 99)
    out: List[Dict[str, str]] = []

    def add(name: str, why: str) -> None:
        query = _CONCERNS[name][0]
        if len(out) < 3 and all(entry["query"] != query for entry in out):
            out.append({"name": name, "why": why, "query": query})

    cold = low is not None and (low <= -4 or float(numbers.get("snow") or 0) > 0)
    snowy = cold or kind == "day in the snow"
    if storm:
        add("Lightning", "thunderstorms are forecast")
    if snowy:
        add("Frostbite and deep cold", f"a low of {float(numbers['low']):.0f}{degree}" if low is not None else "snow and cold")
    elif low is not None and (low <= 10 or (chance >= 50 and high is not None and high <= 16)):
        why = f"a low of {float(numbers['low']):.0f}{degree}"
        if gust is not None and gust >= 35:
            why += f" with gusts to {float(blow):.0f} {speed}"
        if chance >= 50:
            why += ", and rain likely"
        add("Getting cold", why)
    if high is not None and high >= 30:
        add("Heat", f"a high of {float(numbers['high']):.0f}{degree}")
    if kind in _WATER:
        add("The water", "cold water takes strength fast" if high is not None and high < 21 else "any day on or by the water")
    if kind == "hunt":
        add("Other hunters", "others will be out too")
    if kind == "foraging walk":
        add("Look-alikes", "some poisonous plants and mushrooms pass for edible ones")
    if kind == "camping trip":
        add("Fire and fumes", "a stove or a fire near a tent")
    if chance >= 60 and not storm and not snowy:
        add("Rain", f"rain is likely ({chance:.0f}%)")
    if daylight is not None and daylight < 10.5 and kind in _OUT_AND_BACK + _OVERNIGHT:
        add("Being caught out after dark", "the days are short")
    if high is not None and high >= 15 and kind in _ON_FOOT + ("picnic",):
        add("Ticks", "they are active in mild weather")
    if high is not None and high >= 24 and kind in _ON_FOOT:
        add("Snakes", "they are out in warm weather")
    if kind in _ON_FOOT:
        add("Blisters and turned ankles", "the commonest trouble on foot")
        add("Losing the way", "it happens on familiar ground too")
    return out


def advice(name: str) -> str:
    """What to do about a concern, in one line: said when no model put the library page into a sentence."""
    return _CONCERNS.get(name, ("", (), ""))[2]


def _page_for(name: str) -> Optional[Dict[str, str]]:
    """The best library page on a concern: ``{"title", "url", "text"}`` or None. The outdoor and medical
    manuals first, anywhere in the library when they have nothing; a page that never mentions the thing
    (a search can match on one stray word) is passed over."""
    from services import library
    query, must, _advice = _CONCERNS[name]
    for shelf in ("Survival", ""):
        for hit in library.library_search(query, count=6, shelf=shelf):
            title, url = str(hit.get("title") or ""), str(hit.get("url") or "")
            if not url or "/questions/tagged/" in url or re.search(r"(?:^|/)tagged/|_page=\d+$", title):
                continue                                   # a page of links to questions, not a page that says anything
            try:
                found = library.fetch_content(url, query=query, budget=1100)
            except Exception:
                found = {}
            text = re.sub(r"\s+", " ", str(found.get("content") or hit.get("snippet") or "")).strip()
            if len(text) >= 120 and any(word in text.lower() for word in must):
                return {"title": re.sub(r"\s+", " ", title).strip() or "A page in the library", "url": url, "text": text[:1100]}
    return None


_ASK = """You are helping a household get ready for a {kind} on {day}{place}.

The forecast: {forecast}
Sunset is at {sunset}; the last usable light is at {last_light}.

What they should watch for on this outing, each with a page from the manuals in their home library:
{pages}

Answer with ONE JSON object and nothing else:
{{"packing": ["8 to 14 short things to carry, each a few words, fitted to this forecast and this kind of outing"],
  "watch_for": [{{"about": 1, "what": "ONE short sentence of practical advice for number 1, taken from its page where the page covers it"}}]}}
Rules: "watch_for" has exactly one entry for each numbered thing above, in the same order. Each "what" says what to DO, in under 25 words, and does not repeat the name. Carry rain gear only if rain or snow is possible, extra warm layers if it will be cold, a torch always. No commentary."""


def parse_plan(text: str) -> Dict[str, Any]:
    """The model's answer as ``{"packing": [...], "watch_for": [{"about", "what"}]}`` — whatever it sent."""
    cleaned = re.sub(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>", "", text or "")
    found: Dict[str, Any] = {}
    start = cleaned.find("{")
    if start >= 0:
        depth = 0
        for index in range(start, len(cleaned)):
            if cleaned[index] == "{":
                depth += 1
            elif cleaned[index] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(cleaned[start:index + 1])
                        found = parsed if isinstance(parsed, dict) else {}
                    except json.JSONDecodeError:
                        found = {}
                    break
    if not found:
        # Cut off before its last brace, or wrapped in words: take the two lists where they can be seen.
        listed = re.search(r'"packing"\s*:\s*\[(.*?)(?:\]|$)', cleaned, re.S)
        found = {"packing": re.findall(r'"((?:[^"\\]|\\.)+)"\s*(?:,|\]|$)', listed.group(1)) if listed else [],
                 "watch_for": [{"about": about, "what": what} for about, what in
                               re.findall(r'\{\s*"about"\s*:\s*(\d+)\s*,\s*"what"\s*:\s*"((?:[^"\\]|\\.)+)"\s*\}', cleaned)]}
    packing = [re.sub(r"\s+", " ", str(item)).strip(" .-•")[:80] for item in (found.get("packing") or []) if isinstance(item, (str, int, float))]
    watch = []
    for entry in found.get("watch_for") or []:
        if isinstance(entry, dict) and str(entry.get("what") or "").strip():
            try:
                about = int(entry.get("about") or 0)
            except (TypeError, ValueError):
                about = 0
            watch.append({"about": about, "what": re.sub(r"\s+", " ", str(entry["what"])).strip()[:220]})
    return {"packing": [item for item in packing if item][:14], "watch_for": watch[:3]}


def _packing_list(owner: Optional[str], name: str, items: List[str]):
    """Save the packing list under ``name``: ``(its card, whether it was already there)``."""
    from services.home import lists
    from services.home.common import norm
    from src.agent_tools.home_tools import list_card
    mine = next((entry for entry in lists.all_lists(owner) if norm(entry.get("title") or "") == norm(name)), None)
    if mine is None:
        return list_card(lists.create_list(owner, name, items, kind="list")), False
    if mine.get("open"):
        return list_card(lists.get_list(owner, mine["id"])), True
    return list_card(lists.add_items(owner, mine["id"], items)), False


def _short(title: str) -> str:
    """A library page's title cut to what fits a button: the book, and the page."""
    page = re.search(r"—\s*page\s+(\d+)\s*$", title)
    name = re.sub(r"\s*\([^)]*\)", "", re.sub(r"\s*—\s*page\s+\d+\s*$", "", title)).strip() or "The page"
    if len(name) > 38:
        name = name[:36].rstrip(" ,:;-") + "…"
    return name + (f", p. {page.group(1)}" if page else "")


async def plan(asked: Dict[str, Any], owner: Optional[str], ask_model: Optional[Callable[[str], Any]] = None,
               may: Callable[[str], bool] = lambda tool: True) -> AsyncGenerator[Dict[str, Any], None]:
    """Do the job. Yields what a screen shows as it goes — ``{"step": tool}`` as each blade is reached,
    ``{"sources": [...]}`` for the library pages — and last ``{"done": True, "text", "card", "events"}``.
    Yields ``{"unreadable": reason}`` and stops when there is no place to plan for (no home saved, a town
    it does not know): the assistant then asks, as it would have."""
    from services.almanac import places, sky
    from src.agent_tools import TOOL_HANDLERS

    ctx = {"owner": owner}
    kind, day = asked["kind"], asked["day"]
    events: List[Dict[str, Any]] = []
    try:
        where = places.resolve(asked.get("place") or "", owner)
        zone, _label, _note = places.clock(where)
    except places.PlaceError as problem:
        yield {"unreadable": str(problem)}
        return
    weekday = f"{day:%A}"
    title = f"{weekday} {kind}"

    # 1. the sun
    yield {"step": "sun_moon"}
    found = sky.day(day, where.lat, where.lon, zone)
    when = timing(kind, found, asked.get("hours"))
    sunset_words, last_light_words = _clock(found.get("sunset")) or "not known", _clock(found.get("civil_dusk")) or "not known"
    events.append({"tool": "sun_moon", "command": json.dumps({"date": day.isoformat(), "place": where.label}),
                   "output": f"Sunset {sunset_words}, last light {last_light_words} at {where.label} on {weekday}."})

    # 2. the weather
    forecast_line, weather_card, no_forecast = "no forecast is available", None, ""
    numbers, units = None, "us"
    if may("weather"):
        yield {"step": "weather"}
        from services import household
        from services.almanac import weather
        units = household.units()
        try:
            text, weather_card = await asyncio.to_thread(weather.answer, {"when": day.isoformat(), "place": f"{where.lat}, {where.lon}"}, owner)
            weather_card["place"] = where.label
            forecast_line = " ".join(line for line in text.split("\n")[1:6])
            events.append({"tool": "weather", "command": json.dumps({"when": day.isoformat()}), "output": text})
            # the same forecast as numbers (it is in the cache now), for choosing what to warn about
            whole = await asyncio.to_thread(weather.forecast, where.lat, where.lon, units)
            numbers = next((entry for entry in whole.get("days") or [] if entry.get("date") == day.isoformat()), None)
        except weather.WeatherProblem as problem:
            no_forecast = "No forecast: offline." if problem.reason == "offline" else str(problem)
            events.append({"tool": "weather", "command": json.dumps({"when": day.isoformat()}), "output": no_forecast})

    # 3. the library: what to watch for is chosen by rule; the library is asked for a page on each
    light = None
    if found.get("sunrise") and found.get("sunset"):
        light = (found["sunset"] - found["sunrise"]).total_seconds() / 3600
    watch: List[Dict[str, Any]] = [{"name": entry["name"], "why": entry["why"], "text": "", "source": None, "query": entry["query"]}
                                   for entry in concerns(kind, numbers, units, light)]
    excerpts: List[str] = [""] * len(watch)
    if may("library_search") and watch:
        yield {"step": "library_search"}
        try:
            pages = await asyncio.gather(*(asyncio.to_thread(_page_for, entry["name"]) for entry in watch), return_exceptions=True)
            for index, page in enumerate(pages):
                if isinstance(page, dict):
                    watch[index]["source"] = {"title": page["title"][:140], "label": _short(page["title"]), "url": page["url"]}
                    excerpts[index] = page["text"]
            sources = [entry["source"] for entry in watch if entry["source"]]
            events.append({"tool": "library_search", "command": "; ".join(entry["query"] for entry in watch),
                           "output": "\n".join(f"{entry['name']}: {entry['source']['title'] if entry['source'] else 'nothing found'}" for entry in watch)})
            if sources:
                yield {"sources": sources}
        except Exception:
            logger.info("outing: the library could not be searched", exc_info=True)

    # 4. one question to the model: what to pack, and a sentence of advice for each thing to watch for
    yield {"step": "thinking"}
    made: Dict[str, Any] = {"packing": [], "watch_for": []}
    if ask_model is not None:
        try:
            shown = "\n".join(f"[{index}] {entry['name']} ({entry['why']})"
                              + (f" — from “{entry['source']['title']}”:\n{excerpts[index - 1]}" if excerpts[index - 1] else " — no page was found; use what is generally known.")
                              for index, entry in enumerate(watch, 1)) or "(nothing in particular)"
            prompt = _ASK.format(kind=kind, day=f"{weekday}, {day:%B} {day.day}", place=f" near {where.label}" if where.kind != "home" else "",
                                 forecast=forecast_line, sunset=sunset_words, last_light=last_light_words, pages=shown)
            made = parse_plan(str(await ask_model(prompt) or ""))
            if not made["packing"]:                 # an answer that could not be read: ask once more, plainly
                made = parse_plan(str(await ask_model(prompt + "\n\nSend the JSON object only, starting with { and ending with }.") or ""))
        except Exception:
            logger.info("outing: the model was not asked", exc_info=True)
    from_essentials = not made["packing"]
    packing = made["packing"] or list(ESSENTIALS)
    for position, entry in enumerate(made["watch_for"]):
        index = entry["about"] - 1 if 0 < entry["about"] <= len(watch) else position
        if 0 <= index < len(watch) and not watch[index]["text"]:
            watch[index]["text"] = entry["what"]
    for entry in watch:
        entry.pop("query", None)
        if not entry["text"]:                         # no sentence from the model: the plain advice
            entry["text"] = advice(entry["name"])

    # 5. the list, saved. It is named for the outing and its date, so planning the same day again (a
    #    different length, a second look at the forecast) finds the list already made and leaves it alone,
    #    where adding would double it with the same things in other words.
    list_card, list_kept = None, False
    if may("manage_lists"):
        yield {"step": "manage_lists"}
        list_name = f"{title[0].upper() + title[1:]}, {day:%b} {day.day}"
        try:
            list_card, list_kept = await asyncio.to_thread(_packing_list, owner, list_name, packing)
            events.append({"tool": "manage_lists", "command": f"add to {list_name}",
                           "output": ("The list was already made: " if list_kept else "Saved: ") + ", ".join(list_card.get("items") or [])})
        except Exception:
            logger.info("outing: the packing list was not saved", exc_info=True)

    # 6. a reminder, the evening before
    reminder = ""
    today = datetime.now(zone).date()
    if may("manage_reminders") and day > today:
        yield {"step": "manage_reminders"}
        at = datetime(day.year, day.month, day.day, 19, 0, tzinfo=zone) - timedelta(days=1)
        if at > datetime.now(zone) + timedelta(minutes=30):
            set_it = await TOOL_HANDLERS["manage_reminders"](json.dumps({"action": "create", "text": f"Pack for the {kind} and check the forecast", "when": at.isoformat()}), ctx)
            if not set_it.get("exit_code"):
                reminder = f"{at:%A}, 7:00 PM"
                events.append({"tool": "manage_reminders", "command": "create", "output": str(set_it.get("output") or "")})

    card: Dict[str, Any] = {
        "type": "trip", "title": title[0].upper() + title[1:], "when": f"{weekday}, {day:%B} {day.day}", "place": where.label,
        # what was asked, so that "it will be about 6 hours" can plan the same outing again (see again())
        "asked": {"kind": kind, "day": day.isoformat(), "place": asked.get("place") or "", "hours": asked.get("hours")},
        "times": when["tiles"],
        "assumed": when["note"],
        "can_change_length": kind in _OUT_AND_BACK,
        "note": (f"I could not place “{asked['place_unknown']}”, so the times and the forecast are for {where.label}. "
                 "Name a town near it and I will redo them." if asked.get("place_unknown")
                 else "You did not say which day, so this is for today. Say a day and I will redo it." if not asked.get("said_day", True) else ""),
        "weather": ({"summary": weather_card.get("summary"), "icon": weather_card.get("icon"),
                     "tiles": [tile for tile in weather_card.get("tiles") or [] if tile.get("label") != "Now"],
                     "source": weather_card.get("source"), "alerts": weather_card.get("alerts") or []} if weather_card else None),
        "no_forecast": no_forecast,
        "watch_for": watch,
        "list": list_card,
        "essentials": from_essentials and not list_kept,
        "reminder": reminder,
    }
    lines = [f"Here is the plan for your {kind}" + (f" near {where.label}" if where.kind != "home" else "") + f" on {weekday}."]
    if when["line"]:
        lines.append(when["line"])
    if list_card:
        lines.append("The packing list is saved" + (f", and I will remind you {reminder.split(',')[0]} evening." if reminder else "."))
    events.append({"tool": "plan_outing", "command": title, "output": " ".join(lines), "card": card})
    yield {"done": True, "text": " ".join(lines), "card": card, "events": events}
