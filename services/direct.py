"""The straight-to-tool lane: a sentence one exact tool can answer alone is answered by that tool, with
no model round first.

"When does it get dark tonight?", "How many cups is 2 litres?", "How many days until Christmas?", "Will it
rain tomorrow?", "How do you say thank you in Japanese?", "Add milk and eggs to my shopping list" — each
has one right answer and a tool that works it out in milliseconds. Sent through the model they took four to
eleven seconds: a round to choose the tool, a round to put its answer in a sentence. Here the sentence is
read by rule, the tool is called, and its answer is worded by rule too, so it arrives in about a second
and is the same every time.

The reading is narrow on purpose. A message is taken only when the whole of it is one of the shapes
below; anything else — a second question, a follow-up, a place or a date that cannot be read, a unit
nobody knows — returns ``None`` and goes to the assistant as before. Nothing is lost by a miss; a wrong
hit would be an answer to a question that was not asked.

``answer()`` is the whole interface; ``routes/chat_routes.py`` calls it before anything else is built.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Direct:
    tool: str                 # the tool that answered
    command: str              # what it was asked, as shown on the step under the reply
    output: str               # what it said (what a model would have been handed)
    text: str                 # the answer, as a sentence
    card: Optional[dict] = None


# ── reading a message ──────────────────────────────────────────────────────

_LEAD = re.compile(r"^(?:(?:hey|hi|hello|ok|okay|so|please|quick question)[ ,!.:-]+)*(?:(?:can|could|would) you (?:please )?(?:tell me |say )?|"
                   r"do you know |tell me |i (?:want|need|would like|'d like) to know |i was wondering |remind me )?", re.I)
_DAY = (r"(?:today|tonight|now|this (?:morning|afternoon|evening)|tomorrow(?: (?:morning|afternoon|evening|night))?|the day after tomorrow|"
        r"(?:(?:this|next|on|this coming) )?(?:mon|tues|wednes|thurs|fri|satur|sun)day|(?:this |the |next )?weekend|(?:this |the |next )?week)")
_PLACE = r"(?: (?:in|at|near|for|around) (?P<place>[a-z][a-z .,'-]{1,60}?))?"
_WHEN = rf"(?: (?:for |on )?(?P<day>{_DAY}))?"


def _clean(message: str) -> str:
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    text = re.sub(r"[?!.…]+$", "", text).strip()
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    return _LEAD.sub("", text, count=1).strip()


def _full(pattern: str, text: str):
    return re.fullmatch(pattern, text, flags=re.I)


def _day_arg(found) -> str:
    day = (found.groupdict().get("day") or "").lower().strip()
    return re.sub(r"^(?:on|for|this coming) ", "", day)


def _place_arg(found) -> str:
    place = (found.groupdict().get("place") or "").strip(" .,")
    return "" if place.lower() in ("here", "town", "the area", "my area", "our area", "this area") else place


def _today() -> date:
    try:
        from src.user_time import now_user_local
        return now_user_local().date()
    except Exception:
        return date.today()


# ── sun and moon ───────────────────────────────────────────────────────────

_SUN = (
    ("dark", rf"(?:when|what time) (?:does|will|is|'s) it (?:get(?:ting)?|be|going to (?:get|be)) dark{_WHEN}{_PLACE}"),
    ("dark", rf"(?:when|what time) it (?:gets|will get|is going to get|is getting) dark{_WHEN}{_PLACE}"),
    ("dark", rf"how long (?:until|till|before) (?:it(?:'s| is| gets)? )?dark{_PLACE}"),
    ("sunset", rf"(?:when|what time)(?:'s| is| does| will)? (?:the )?(?:sunset|sun ?down|sun (?:set|sets|go down|goes down|going down|setting)){_WHEN}{_PLACE}"),
    ("sunrise", rf"(?:when|what time)(?:'s| is| does| will)? (?:the )?(?:sunrise|sun ?up|sun (?:rise|rises|come up|comes up|coming up|rising)){_WHEN}{_PLACE}"),
    ("light", rf"(?:when|what time) (?:does|will|is|'s) it (?:get(?:ting)?|be|going to (?:get|be)) light{_WHEN}{_PLACE}"),
    ("daylight", rf"how (?:much|many hours of) daylight(?: (?:is there|do we (?:have|get)|will there be|will we (?:have|get)))?{_WHEN}{_PLACE}"),
    ("daylight", rf"how long is (?:the day|daylight){_WHEN}{_PLACE}"),
    ("moon", rf"(?:what(?:'s| is) the moon(?: phase)?(?: doing| like)?|what phase is the moon(?: in)?|is there a (?:full )?moon|"
             rf"what(?:'s| is) the phase of the moon){_WHEN}{_PLACE}"),
    ("next_moon", r"when(?:'s| is) the next (?P<phase>full|new) moon"),
)


def _sun(text: str, owner: Optional[str]) -> Optional[Direct]:
    for kind, pattern in _SUN:
        found = _full(pattern, text)
        if not found:
            continue
        from src.agent_tools.almanac_tools import AlmanacProblem, sun_card, sun_moon_answer
        args: Dict[str, Any] = {}
        day = _day_arg(found) if kind != "next_moon" else ""
        if day in ("the weekend", "weekend", "this weekend", "next weekend"):
            day = "saturday"
        if day and "week" in day and "weekend" not in day:
            return None                                  # a week of sunsets is a table: the assistant's job
        if day:
            args["date"] = day
        place = _place_arg(found) if kind != "next_moon" else ""
        if place:
            args["place"] = place
        try:
            output, summary = sun_moon_answer(args, owner)
        except AlmanacProblem:
            return None                                  # no home yet, or a place it does not know: let the assistant ask
        if not summary:
            return None
        where = summary["label"]
        when = "today" if day in ("", "today", "now", "tonight", "this morning", "this afternoon", "this evening") else f"on {summary['date'].split(',')[0]}"
        if day.startswith("tomorrow"):
            when = "tomorrow"
        moon = summary.get("moon") or {}
        if summary.get("state") in ("up", "down") and kind not in ("moon", "next_moon"):
            said = (f"The sun does not set {when} in {where}: it is up all day." if summary["state"] == "up"
                    else f"The sun does not rise {when} in {where}.")
        elif kind == "dark":
            night = "tonight" if when == "today" else when
            dark = summary.get("dark")
            said = (f"It is fully dark by {dark} {night} in {where}. The sun sets at {summary['sunset']} and the last light goes at {summary['last_light']}."
                    if dark else f"It does not get fully dark {night} in {where}. The sun sets at {summary['sunset']}; the last light is at {summary['last_light']}.")
        elif kind == "sunset":
            said = f"The sun sets at {summary['sunset']} {when} in {where}. The last light goes at {summary['last_light']}."
        elif kind == "sunrise":
            said = f"The sun rises at {summary['sunrise']} {when} in {where}. First light is at {summary['first_light']}."
        elif kind == "light":
            said = f"First light is at {summary['first_light']} {when} in {where}, and the sun rises at {summary['sunrise']}."
        elif kind == "daylight":
            said = f"{summary['daylight']} of daylight {when} in {where}: sunrise {summary['sunrise']}, sunset {summary['sunset']}."
        elif kind == "next_moon":
            wanted = f"{found.group('phase').lower()} moon"
            coming = next((item for item in moon.get("next") or [] if item["name"].lower() == wanted), None)
            if not coming:
                return None
            said = f"The next {wanted} is {coming['when']}."
        else:
            rise = f" It rises at {moon['rise']}." if moon.get("rise") else ""
            said = f"The moon is {str(moon.get('name') or '').lower()}, {moon.get('lit', 0)}% lit.{rise}"
            coming = (moon.get("next") or [None])[0]
            if coming:
                said += f" Next: {coming['name'].lower()}, {coming['when']}."
        return Direct("sun_moon", json.dumps(args) if args else "{}", output, said, sun_card(summary))
    return None


# ── units ──────────────────────────────────────────────────────────────────

_UNIT_WORDS = r"[a-z°µ'\"/. ²³0-9-]{1,40}?"
_UNITS = (
    rf"how many (?P<to>{_UNIT_WORDS}) (?:(?:are|is) )?(?:there )?in (?P<amount>.+)",
    rf"how many (?P<to>{_UNIT_WORDS}) (?:is|are|equals?|makes?|make up|to) (?P<amount>.+)",
    rf"how much is (?P<amount>\d.+?) in (?P<to>{_UNIT_WORDS})",
    rf"(?:convert|change) (?P<amount>.+) (?:to|into) (?P<to>{_UNIT_WORDS})",
    rf"(?:convert|change) (?P<amount>.+?) in (?P<to>{_UNIT_WORDS})",
    rf"(?:what is|what's|whats) (?P<amount>\d.+?) (?:in|as|converted to) (?P<to>{_UNIT_WORDS})",
    rf"(?P<amount>\d[\d.,/ ]*(?:[a-z°µ'\"]+\.?(?: ?\d[\d.,/ ]*[a-z'\"]+\.?)?)) (?:to|in|into|=|equals) (?P<to>{_UNIT_WORDS})",
)


def _units(text: str) -> Optional[Direct]:
    for pattern in _UNITS:
        found = _full(pattern, text)
        if not found:
            continue
        from services.almanac import units
        from src.agent_tools.almanac_tools import convert_card
        amount, wanted = found.group("amount").strip(), found.group("to").strip()
        amount = re.sub(r"^(?:a|an|one) ", "1 ", amount, flags=re.I)
        if not re.match(r"^[\d.½¼¾⅓⅔⅛-]", amount):
            continue
        try:
            done = units.convert(amount, wanted)
        except (units.UnitError, ValueError, OverflowError, ZeroDivisionError):
            continue                                     # money, or a unit nobody knows: another shape, or the assistant's job
        card = convert_card(done)
        note = f" {done['note']}" if done.get("note") else ""
        return Direct("convert_units", json.dumps({"amount": amount, "to": wanted}), done["text"] + (f"\n{done['note']}" if done.get("note") else ""),
                      f"{done['from']} is {card['value']}.{note}", card)
    return None


# ── dates ──────────────────────────────────────────────────────────────────

_SPAN = r"(?P<add>\d{1,4} (?:business days?|working days?|days?|weeks?|months?|years?))"
_DATES = (
    ("until", r"how (?:many|much) (?P<unit>days|weeks|months|sleeps|weekdays|business days|working days|time|longer)(?: (?:are there|is it|is there|do we have|left))? (?:until|till|til|'til|to|before) (?P<date>.+)"),
    ("until", r"how (?:long|far away|far off|soon)(?: is it| do we have)? (?:until|till|til|'til|to|before|is) (?P<date>.+)"),
    ("since", r"how (?:many (?P<unit>days|weeks|months)|long)(?: has it been| is it| have passed)? since (?P<date>.+)"),
    ("today", r"what(?:'s| is)(?: the date| today's date| the date today| today)|what day(?: of the week)? is (?:it|today)(?: today)?|what(?:'s| is) the day today"),
    ("weekday", r"what day(?: of the week)? (?:is|was|will|does|did) (?P<date>.+?)(?: be| fall on| land on| falling on)?"),
    ("move", rf"what(?:'s| is| will be)(?: the date)?(?: in)? {_SPAN}(?: from (?:now|today)| ahead| later| out)?"),
    ("move", rf"{_SPAN} from (?:now|today)"),
    ("when", r"when(?:'s| is) (?P<date>(?:(?:this|next|last) )?[a-z][a-z' .]{2,30}?(?: \d{4})?)(?: this year| next year)?"),
)


def _dates(text: str) -> Optional[Direct]:
    from services.almanac import dates
    today = _today()

    def card(label: str, value: str, detail: str = "") -> dict:
        return {"type": "result", "label": label, "value": value, "detail": detail, "source": "Worked out"}

    for kind, pattern in _DATES:
        found = _full(pattern, text)
        if not found:
            continue
        try:
            if kind == "today":
                return Direct("date_math", '{"date": "today"}', dates.about(today, today), f"Today is {dates.written(today)}.",
                              card("Today", f"{today:%A}", f"{today:%B} {today.day}, {today.year}"))
            if kind == "move":
                moved, notes = dates.move(today, found.group("add"))
                return Direct("date_math", json.dumps({"date": "today", "add": found.group("add")}),
                              "\n".join([f"{dates.written(today)} moved by {found.group('add')}: {dates.about(moved, today)}", *notes]),
                              f"{found.group('add').capitalize()} from today is {dates.written(moved)}.",
                              card(f"{found.group('add')} from today", dates.written(moved)))
            asked = re.sub(r"^(?:the |on )", "", found.group("date").strip(), flags=re.I)
            if kind == "when":
                day = dates.named_day(asked, today)
                if day is None:
                    return None                          # "when is the meeting": not a day on the calendar
            elif kind == "since" and dates.named_day(asked, today) is not None and not re.search(r"\d{4}|^(?:last|next|this)\b", asked, re.I):
                day = dates.named_day(f"last {asked}", today)   # "since Halloween" is the Halloween gone by
            else:
                day = dates.parse(asked, today)
            gap = dates.apart(today, day)
            count = int(gap["days"])
            many = f"{count:,} day{'' if count == 1 else 's'}"
            detail = (f"{gap['weeks']} week{'' if gap['weeks'] == 1 else 's'} and {gap['odd_days']} day{'' if gap['odd_days'] == 1 else 's'} · "
                      f"{gap['weekdays']} weekday{'' if gap['weekdays'] == 1 else 's'}") if count >= 7 else ""
            output = dates.apart_text(today, day) if day != today else dates.about(day, today)
            args = json.dumps({"date": "today", "to": day.isoformat()})
            if kind == "weekday":
                verb = "is" if day >= today else "was"
                return Direct("date_math", json.dumps({"date": day.isoformat()}), dates.about(day, today),
                              f"{day:%B} {day.day}, {day.year} {verb} a {day:%A}.", card(f"{day:%B} {day.day}, {day.year}", f"{day:%A}"))
            if kind == "when":
                away = "today" if count == 0 else f"{many} away" if day > today else f"{many} ago"
                return Direct("date_math", args, output, f"{asked.strip().title()} is {dates.written(day)}: {away}.",
                              card(asked.strip().title(), f"{day:%A}, {day:%B} {day.day}", away))
            if kind == "since":
                if day > today:
                    return None
                return Direct("date_math", args, output, f"It has been {many} since {dates.written(day)}" + (f" ({detail.split(' · ')[0]})." if detail else "."),
                              card(f"Since {dates.written(day)}", many, detail))
            if day < today:
                return None                              # "until" a day already gone: let the assistant sort out which was meant
            unit = (found.groupdict().get("unit") or "").lower()
            if unit in ("weekdays", "business days", "working days"):
                said = f"{gap['weekdays']} weekday{'' if gap['weekdays'] == 1 else 's'} until {dates.written(day)} ({many} in all)."
            elif unit == "weeks" and count >= 7:
                said = f"{gap['weeks']} week{'' if gap['weeks'] == 1 else 's'} and {gap['odd_days']} day{'' if gap['odd_days'] == 1 else 's'} until {dates.written(day)}."
            elif count == 0:
                said = f"That is today: {dates.written(day)}."
            else:
                said = f"{many} until {dates.written(day)}" + (f" ({detail.split(' · ')[0]})." if detail else ".")
            return Direct("date_math", args, output, said, card(f"Until {dates.written(day)}", many, detail))
        except dates.DateError:
            continue                                     # not a date this shape can read: another shape, or the assistant's job
    return None


# ── the weather ────────────────────────────────────────────────────────────

_WEATHER = (
    ("weather", rf"(?:what(?:'s| is| will)|how(?:'s| is| will)) (?:the )?weather(?: (?:going to be|gonna be|be|like|looking like|looking|look like|supposed to be))*{_WHEN}{_PLACE}"),
    ("weather", rf"(?:what(?:'s| is) )?(?:the )?(?:weather|forecast|weather forecast){_WHEN}{_PLACE}"),
    ("weather", rf"(?:what(?:'s| is)|how(?:'s| is)) it (?:like|looking) (?:outside|out){_WHEN}"),
    ("temp", rf"(?:what(?:'s| is| will)|how (?:hot|cold|warm)(?: is| will)?) (?:the )?(?:temperature|it)(?: (?:be|get|going to (?:be|get)))?(?: outside| out)?{_WHEN}{_PLACE}"),
    ("rain", rf"(?:will|is|does|did) it (?:going to |gonna |supposed to )?(?P<wet>rain|snow)(?:ing)?{_WHEN}{_PLACE}"),
    ("rain", rf"(?:is|any) (?:there )?(?:any )?(?P<wet>rain|snow) (?:coming|expected|forecast|in the forecast){_WHEN}{_PLACE}"),
    ("umbrella", rf"(?:do|will|should) (?:i|we) (?:need|take|bring) (?:an? |my |the )?umbrella{_WHEN}"),
    ("coat", rf"(?:do|will|should) (?:i|we) (?:need|take|bring|wear) (?:an? |my |the )?(?:jacket|coat|sweater|hoodie){_WHEN}"),
)


def _tile(card: dict, name: str) -> str:
    return next((str(tile.get("value") or "") for tile in card.get("tiles") or [] if tile.get("label") == name), "")


def _weather(text: str, owner: Optional[str]) -> Optional[Direct]:
    for kind, pattern in _WEATHER:
        found = _full(pattern, text)
        if not found:
            continue
        from services.almanac import weather
        args: Dict[str, Any] = {}
        day = _day_arg(found)
        if day:
            args["when"] = day
        place = _place_arg(found)
        if place:
            args["place"] = place
        try:
            output, card = weather.answer(args, owner)
        except weather.WeatherProblem as problem:
            if problem.reason in ("place", "range"):
                return None                              # no home yet, an unknown town, a day too far off: let the assistant say so
            return Direct("weather", json.dumps(args), str(problem), str(problem), {"type": "notice", "text": str(problem), "reason": problem.reason})
        where, title = card.get("place") or "", str(card.get("title") or "")
        lead = f"{title} in {where}"
        stale = " This is the last forecast I have; I cannot reach a newer one right now." if card.get("stale") else ""
        if card.get("days"):                             # the week, or the weekend
            days = "; ".join(f"{row['label']} {row['summary'].lower()}, {row['high']}/{row['low']}" + (f", {row['rain']} rain" if row.get("rain") not in ("", "0%") else "")
                             for row in card["days"])
            said = f"{lead}: {days}."
            if kind in ("rain", "umbrella"):
                asked = (found.groupdict().get("wet") or "rain").lower()
                most = max((int(re.sub(r"\D", "", row.get("rain") or "") or 0) for row in card["days"]), default=0)
                snowy = any("snow" in row["summary"].lower() for row in card["days"])
                likely = snowy if asked == "snow" else most >= 40
                said = (f"Yes, {asked} is in the forecast. " if likely else f"No {asked} is expected. " if (asked == "snow" or most < 20) else f"It might {asked}. ") + said
            return Direct("weather", json.dumps(args), output, said + stale, card)
        high, low, wet = _tile(card, "High"), _tile(card, "Low"), _tile(card, "Rain") or _tile(card, "Snow")
        what = "snow" if _tile(card, "Snow") else "rain"
        chance = int(re.sub(r"\D", "", wet) or 0)
        when = title.lower() if title in ("Today", "Tonight", "Tomorrow") else f"on {title}"
        summary = str(card.get("summary") or "").lower()
        alerts = card.get("alerts") or []
        warned = f" Weather alert: {alerts[0]['event']}" + (f", {alerts[0]['until']}." if alerts[0].get("until") else ".") if alerts else ""
        if kind in ("rain", "umbrella"):
            asked = (found.groupdict().get("wet") or what).lower()
            if kind == "umbrella":
                said = (f"Yes, take one: {chance}% chance of {what} {when}." if chance >= 40 else
                        f"Probably not: {chance}% chance of {what} {when}." if chance >= 15 else f"No need: no {what} is expected {when}.")
            elif chance >= 50:
                said = f"Yes, {asked} is likely {when} in {where}: {chance}% chance."
            elif chance >= 20:
                said = f"It might: {chance}% chance of {what} {when} in {where}."
            else:
                said = f"No {asked} is expected {when} in {where} ({chance}% chance)."
            said += f" {summary.capitalize()}, high {high}, low {low}."
        elif kind == "coat":
            number = re.sub(r"[^\d-]", "", high)
            if number not in ("", "-"):
                cold, cool = (7, 17) if "°C" in output else (45, 62)
                said = (f"Yes, a warm coat: the high is only {high} {when}, low {low}." if int(number) < cold else
                        f"A jacket is a good idea {when}: high {high}, low {low}." if int(number) < cool else f"No jacket needed {when}: high {high}, low {low}.")
            else:
                said = f"{lead}: high {high}, low {low}."
        else:
            now = _tile(card, "Now")
            rain_line = f", {chance}% chance of {what}" if chance >= 15 else f", no {what} expected"
            wind = _tile(card, "Wind")
            said = f"{lead}: {summary}, high {high}, low {low}{rain_line}" + (f", wind up to {wind}" if wind else "") + "."
            if now and title in ("Today", "Tonight"):
                said = f"It is {now} now. " + said
        return Direct("weather", json.dumps(args), output, said + warned + stale, card)
    return None


# ── translating ────────────────────────────────────────────────────────────

_LANG = r"(?P<lang>[a-z][a-z ()-]{2,30})"
_SAY = (
    rf"how (?:do|would|can|should) (?:you|i|we|one) say (?P<text>.+?) in {_LANG}",
    rf"(?:translate|say) (?P<text>.+?) (?:to|into|in) {_LANG}",
    rf"(?:what(?:'s| is)|how(?:'s| is)) (?P<text>.+?) in {_LANG}",
    rf"what(?:'s| is) the {_LANG} (?:word|phrase|term) for (?P<text>.+)",
    rf"{_LANG} (?:word )?for (?P<text>.+)",
)
_POINTS_BACK = re.compile(r"^(?:this|that|it|these|those|the above|this (?:sentence|message|text|word|phrase)|that (?:sentence|message|text|word|phrase)|"
                          r"what (?:i|you) (?:just )?(?:said|wrote))$", re.I)


async def _translate(text: str, owner: Optional[str]) -> Optional[Direct]:
    for pattern in _SAY:
        found = _full(pattern, text)
        if not found:
            continue
        from services.translate import languages
        target = languages.resolve(found.group("lang").strip())
        phrase = found.group("text").strip().strip("\"'“”‘’ ")
        if not target or not phrase or len(phrase) > 400 or _POINTS_BACK.match(phrase) or re.match(r"^[\d.,/ ]+[a-z°]{0,6}$", phrase, re.I):
            continue
        from services import translate
        try:
            done = await translate.translate_text(phrase, target, "auto", owner=owner)
        except Exception as problem:
            logger.info("direct translation not done (%s): left to the assistant", type(problem).__name__)
            return None
        if done.get("same_language") or not str(done.get("text") or "").strip():
            return None
        card = {"type": "translation", "from": done.get("source_name") or "", "from_code": done.get("source") or "", "to": done.get("target_name") or "",
                "to_code": done.get("target") or "", "source": phrase, "text": str(done["text"]).strip()}
        return Direct("translate", json.dumps({"text": phrase, "to": done.get("target_name") or target}),
                      f"{done.get('source_name') or 'Unknown language'} → {done.get('target_name')}:\n{done['text']}", str(done["text"]).strip(), card)
    return None


# ── lists ──────────────────────────────────────────────────────────────────

_LIST_NAME = r"(?P<list>[a-z0-9][a-z0-9' -]{0,40}?list)"
_TOGETHER = re.compile(r"\b(?:mac(?:aroni)? and cheese|salt and pepper|bread and butter|peanut butter and jelly|fish and chips|rice and beans|"
                       r"half and half|pork and beans|oil and vinegar|cookies and cream|chips and (?:salsa|dip)|franks and beans|sweet and sour\b[^,]*)", re.I)


def list_items(text: str) -> List[str]:
    """"milk, eggs and bread" → three things; "mac and cheese" stays one."""
    kept: Dict[str, str] = {}

    def hold(found) -> str:
        key = f"\x00{len(kept)}\x00"
        kept[key] = found.group(0)
        return key

    text = _TOGETHER.sub(hold, text)
    text = re.sub(r",? and ", ", ", text, flags=re.I) if ("," in text or text.lower().count(" and ") == 1) else text
    items = [part.strip(" .") for part in text.split(",")]
    out = []
    for item in items:
        for key, value in kept.items():
            item = item.replace(key, value)
        item = re.sub(r"^(?:some|a|an|the) ", "", item, flags=re.I).strip()
        if item:
            out.append(item)
    return out


async def _lists(text: str, owner: Optional[str]) -> Optional[Direct]:
    add = _full(rf"(?:add|put) (?P<items>.+?) (?:to|on|onto|in) (?:my|the|our) {_LIST_NAME}", text)
    show = add is None and _full(rf"(?:what(?:'s| is) on|show(?: me)?|read(?: me)?|what do i have on) (?:my|the|our) {_LIST_NAME}", text)
    if not add and not show:
        return None
    from src.agent_tools import TOOL_HANDLERS
    found = add or show
    name = found.group("list").strip()
    name = name[0].upper() + name[1:]
    args: Dict[str, Any] = {"action": "add" if add else "show", "list": name}
    items: List[str] = []
    if add:
        if re.match(r"(?:the |all the )?(?:ingredients|things|what (?:i|we) need|what(?:'s| is) missing|missing (?:things|ingredients))\b.{0,6}\b(?:for|from)\b",
                    add.group("items"), re.I):
            return None                                  # "the ingredients for the stew": a recipe has to be read (services/dinner.py)
        items = list_items(add.group("items"))
        if not items or len(items) > 30 or any(len(item) > 120 for item in items):
            return None
        args["items"] = items
    result = await TOOL_HANDLERS["manage_lists"](json.dumps(args), {"owner": owner})
    if result.get("exit_code") or result.get("error"):
        return None                                      # no such list to show, or anything else that needs a person's judgement
    output = str(result.get("output") or "")
    card = None
    try:
        from services.home import lists
        view = lists.find_list(owner, name)
        if view:
            still = [item["text"] for item in view["items"] if not item["done"]]
            card = {"type": "list", "title": view["title"], "open": view["open"], "done": view["done"], "items": still[:8],
                    "more": max(0, len(still) - 8), "href": f"/home#lists/{view['id']}"}
    except Exception:
        logger.debug("list card not made", exc_info=True)
    title = card["title"] if card else name
    if add:
        listed = items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" and {items[-1]}"
        said = f"Added {listed} to {title}."
    elif card and card["items"]:
        said = f"{title} has {card['open']} to do: " + ", ".join(card["items"]) + (f", and {card['more']} more." if card["more"] else ".")
    else:
        said = f"{title} has nothing left to do on it."
    return Direct("manage_lists", json.dumps(args), output, said, card)


# ── the lane ───────────────────────────────────────────────────────────────

def worth_trying(message: str) -> bool:
    """A cheap first look: short, one line, and not obviously more than one question."""
    text = str(message or "").strip()
    if not text or len(text) > 220 or "\n" in text or text.count("?") > 1 or "http" in text.lower():
        return False
    return not re.search(r"\b(?:and (?:also|then|what|when|how|will|is|can)|, (?:and|then|also) |; )", text, re.I) or bool(re.search(r"\blist\b", text, re.I))


async def answer(message: str, owner: Optional[str] = None, allowed: Optional[Callable[[str], bool]] = None) -> Optional[Direct]:
    """The answer to ``message`` straight from one tool, or None when it is not one of the sentences this lane
    reads (or the tool it would need is not this person's to use). Never raises."""
    if not worth_trying(message):
        return None
    may = allowed or (lambda _tool: True)
    text = _clean(message)
    if not text:
        return None
    try:
        if may("manage_lists") and re.search(r"\blist\b", text, re.I):
            found = await _lists(text, owner)
            if found:
                return found
        if re.search(r"\b(?:and|then|also)\b", text, re.I) and not re.search(r"\bday after\b", text, re.I):
            return None                                  # two things asked at once: the assistant's job
        for tool, read in (("sun_moon", lambda: _sun(text, owner)), ("convert_units", lambda: _units(text)), ("weather", lambda: _weather(text, owner)),
                           ("date_math", lambda: _dates(text))):
            if may(tool):
                # The forecast asks an outside service; nothing else here leaves the machine or takes long.
                found = await asyncio.to_thread(read) if tool == "weather" else read()
                if found:
                    return found
        if may("translate"):
            return await _translate(text, owner)
    except Exception:
        logger.exception("the straight-to-tool lane failed on a message; left to the assistant")
    return None
