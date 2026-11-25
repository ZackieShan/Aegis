"""Calendar arithmetic: the days between two dates, a date so many days,
weeks, months or working days on, and what day of the week a date falls on.

Models are unreliable at this and a calendar is not, so the assistant asks
here. Dates are read the ways people write them — ``2027-03-12``,
``March 12, 2027``, ``12 March 2027``, ``3/12/2027`` (month first, as in the
United States; ``12.3.2027`` with dots is day first), ``today``,
``tomorrow``, ``next Friday``. The calendar is the Gregorian one throughout,
including for dates before it was adopted.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTH = {name.lower(): index for index, name in enumerate(MONTHS, 1)}
_MONTH.update({name[:3].lower(): index for index, name in enumerate(MONTHS, 1)})
_MONTH["sept"] = 9
_DAY = {name.lower(): index for index, name in enumerate(DAYS)}
_DAY.update({name[:3].lower(): index for index, name in enumerate(DAYS)})
_DAY.update({"tues": 1, "thur": 3, "thurs": 3})


class DateError(ValueError):
    """Why a date could not be read or reached, in words for the person."""


# Days people name instead of dating. Fixed ones as (month, day); the ones that move as
# (month, weekday, which) — the which-th weekday of the month, -1 for the last — as kept in the United States.
_FIXED_DAYS = {
    "christmas": (12, 25), "christmas day": (12, 25), "xmas": (12, 25), "christmas eve": (12, 24), "boxing day": (12, 26),
    "new year": (1, 1), "new years": (1, 1), "new years day": (1, 1), "new years eve": (12, 31),
    "halloween": (10, 31), "valentines": (2, 14), "valentines day": (2, 14), "st patricks day": (3, 17), "saint patricks day": (3, 17),
    "independence day": (7, 4), "fourth july": (7, 4), "july fourth": (7, 4), "juneteenth": (6, 19), "veterans day": (11, 11),
    "groundhog day": (2, 2), "april fools day": (4, 1), "earth day": (4, 22), "cinco de mayo": (5, 5),
}
_MOVING_DAYS = {
    "thanksgiving": (11, 3, 4), "thanksgiving day": (11, 3, 4), "memorial day": (5, 0, -1), "labor day": (9, 0, 1),
    "mothers day": (5, 6, 2), "fathers day": (6, 6, 3), "martin luther king day": (1, 0, 3), "mlk day": (1, 0, 3),
    "presidents day": (2, 0, 3), "columbus day": (10, 0, 2), "indigenous peoples day": (10, 0, 2),
}


def easter(year: int) -> date:
    """Easter Sunday in the Gregorian calendar (the anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    return date(year, month, (h + l - 7 * m + 33 * month + 19) % 32)


def _named_in(year: int, name: str) -> Optional[date]:
    if name in _FIXED_DAYS:
        return date(year, *_FIXED_DAYS[name])
    if name in _MOVING_DAYS:
        month, weekday, which = _MOVING_DAYS[name]
        if which > 0:
            first = date(year, month, 1)
            return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (which - 1))
        last = date(year, month, calendar.monthrange(year, month)[1])
        return last - timedelta(days=(last.weekday() - weekday) % 7)
    if name in ("easter", "easter sunday"):
        return easter(year)
    if name == "good friday":
        return easter(year) - timedelta(days=2)
    return None


def named_day(text: str, today: date) -> Optional[date]:
    """"Christmas", "Thanksgiving 2027", "last Easter": the date, or None when ``text`` names no such day.
    Without a year it is the one coming (today counts)."""
    cleaned = re.sub(r"['’.]", "", str(text or "").lower())
    cleaned = re.sub(r"\s+", " ", cleaned.replace(",", " ")).strip()
    cleaned = re.sub(r"^(?:on|the)\s+", "", re.sub(r"\bof\s+", "", cleaned))
    found = re.fullmatch(r"(?:(this|next|last|this coming|coming)\s+)?(.+?)(?:\s+(\d{4}))?", cleaned)
    if not found or _named_in(2000, found.group(2)) is None:
        return None
    which, name, year = found.group(1) or "", found.group(2), found.group(3)
    if year:
        return _named_in(int(year), name)
    this_year = _named_in(today.year, name)
    if which == "last":
        return this_year if this_year < today else _named_in(today.year - 1, name)
    return this_year if this_year >= today else _named_in(today.year + 1, name)


def written(day: date) -> str:
    return f"{DAYS[day.weekday()]}, {MONTHS[day.month - 1]} {day.day}, {day.year}"


def _year(text: str) -> int:
    year = int(text)
    if len(text) <= 2:
        year += 2000 if year < 69 else 1900
    return year


def _make(year: int, month: int, day: int, original: str) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        raise DateError(f"“{original.strip()[:40]}” is not a day on the calendar.") from None


def parse(text: str, today: Optional[date] = None) -> date:
    """A date from the ways people write one. ``today`` anchors words like "tomorrow" and dates given without a year."""
    today = today or date.today()
    original = str(text or "")
    cleaned = re.sub(r"\s+", " ", original.strip().lower().replace(",", " ").rstrip(".")).strip()
    cleaned = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", cleaned)
    cleaned = re.sub(r"^(?:on|the)\s+", "", re.sub(r"\bof\s+", "", cleaned))
    if not cleaned:
        raise DateError("Give a date, such as 2027-03-12, March 12 2027, today or next Friday.")
    named = named_day(original, today)
    if named is not None:
        return named
    if cleaned in ("today", "now", "tonight", "this morning", "this afternoon", "this evening", "later today"):
        return today
    if cleaned in ("tomorrow", "tomorrow morning", "tomorrow afternoon", "tomorrow evening", "tomorrow night"):
        return today + timedelta(days=1)
    if cleaned in ("yesterday", "last night"):
        return today - timedelta(days=1)
    if cleaned == "day after tomorrow":
        return today + timedelta(days=2)
    found = re.fullmatch(r"(?:in (\d{1,4}) (day|week)s?|(\d{1,4}) (day|week)s? (from now|from today|ago))", cleaned)
    if found:
        count, unit = int(found.group(1) or found.group(3)), found.group(2) or found.group(4)
        return today + timedelta(days=count * (7 if unit == "week" else 1) * (-1 if found.group(5) == "ago" else 1))
    found = re.fullmatch(r"(?:(this|next|last|this coming|coming)\s+)?([a-z]+)", cleaned)
    if found and found.group(2) in _DAY:
        ahead = (_DAY[found.group(2)] - today.weekday()) % 7
        which = found.group(1) or ""
        if which == "last":
            return today - timedelta(days=(today.weekday() - _DAY[found.group(2)]) % 7 or 7)
        return today + timedelta(days=ahead or 7)                       # "Friday" and "next Friday" alike: the one coming
    found = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[t ].*)?", cleaned)
    if found:
        return _make(int(found.group(1)), int(found.group(2)), int(found.group(3)), original)
    found = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?", cleaned)
    if found:
        first, second = int(found.group(1)), int(found.group(2))
        month, day = (second, first) if first > 12 >= second else (first, second)
        return _make(_year(found.group(3)) if found.group(3) else today.year, month, day, original)
    found = re.fullmatch(r"(\d{1,2})[.-](\d{1,2})[.-](\d{2}|\d{4})", cleaned)
    if found:
        return _make(_year(found.group(3)), int(found.group(2)), int(found.group(1)), original)
    found = re.fullmatch(r"(\d{1,2}) ([a-z]+)\.?(?: (\d{4}))?", cleaned) or None
    if found and found.group(2) in _MONTH:
        return _make(int(found.group(3)) if found.group(3) else today.year, _MONTH[found.group(2)], int(found.group(1)), original)
    found = re.fullmatch(r"([a-z]+)\.? (\d{1,2})(?: (\d{4}))?", cleaned)
    if found and found.group(1) in _MONTH:
        return _make(int(found.group(3)) if found.group(3) else today.year, _MONTH[found.group(1)], int(found.group(2)), original)
    found = re.fullmatch(r"([a-z]+)\.? (\d{4})", cleaned)
    if found and found.group(1) in _MONTH:
        return _make(int(found.group(2)), _MONTH[found.group(1)], 1, original)
    raise DateError(f"I could not read “{original.strip()[:40]}” as a date. Write it like 2027-03-12, March 12 2027, today or next Friday.")


def _add_months(day: date, months: int) -> Tuple[date, bool]:
    """``months`` on from ``day``; the flag says the day had to be pulled back to the end of a shorter month."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    if not 1 <= year <= 9999:
        raise DateError("That is beyond the calendar this can count on (years 1 to 9999).")
    last = calendar.monthrange(year, month + 1)[1]
    return date(year, month + 1, min(day.day, last)), day.day > last


def apart(first: date, second: date) -> Dict[str, object]:
    """How far apart two dates are, several ways. Counts are of the days after ``first`` up to and including ``second``."""
    early, late = (first, second) if first <= second else (second, first)
    days = (late - early).days
    months = (late.year - early.year) * 12 + late.month - early.month
    if _add_months(early, months)[0] > late:
        months -= 1
    rest = (late - _add_months(early, months)[0]).days
    whole_weeks, odd = divmod(days, 7)
    weekdays = whole_weeks * 5 + sum(1 for step in range(1, odd + 1) if (early + timedelta(days=step)).weekday() < 5)
    return {"days": days, "weeks": whole_weeks, "odd_days": odd, "years": months // 12, "months": months % 12, "rest_days": rest,
            "weekdays": weekdays, "backwards": second < first}


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}" + ("" if count == 1 else "s")


def apart_text(first: date, second: date) -> str:
    found = apart(first, second)
    if not found["days"]:
        return f"{written(first)} — the same day."
    parts = [_plural(found["years"], "year")] if found["years"] else []
    if found["months"]:
        parts.append(_plural(found["months"], "month"))
    if found["rest_days"] or not parts:
        parts.append(_plural(found["rest_days"], "day"))
    by_calendar = ", ".join(parts)
    weeks = f"{_plural(found['weeks'], 'week')}" + (f" and {_plural(found['odd_days'], 'day')}" if found["odd_days"] else "")
    text = f"From {written(first)} to {written(second)}: {_plural(found['days'], 'day')}"
    if found["backwards"]:
        text += " back"
    extras = []
    if found["days"] >= 7:
        extras.append(weeks)
    if found["years"] or found["months"]:
        extras.append(by_calendar)
    extras.append(_plural(found["weekdays"], "weekday") + " (Monday to Friday)")
    return text + " — " + "; ".join(extras) + "."


_STEP = re.compile(r"([-+]?)\s*(\d+)\s*(business days?|working days?|work days?|weekdays?|days?|d|weeks?|wks?|w|fortnights?|months?|mos?|years?|yrs?|y)\b", re.I)


def move(start: date, amount: str) -> Tuple[date, List[str]]:
    """``start`` moved by ``amount``: ``"90 days"``, ``"6 weeks"``, ``"-3 months"``, ``"1 year 2 months"``,
    ``"10 business days"``, ``"2 weeks ago"``. Returns the date and anything worth saying about how it was reached."""
    text = str(amount or "").strip().lower()
    if not text:
        raise DateError("Say how far to move: 90 days, 6 weeks, -3 months, 10 business days.")
    back = bool(re.search(r"\b(?:ago|before|earlier|back)\b", text))
    text = re.sub(r"\b(?:ago|before|earlier|back|from now|later|after|and|plus|in)\b", " ", text).replace(",", " ")
    steps = list(_STEP.finditer(text))
    if not steps or _STEP.sub("", text).strip():
        raise DateError(f"I could not read “{str(amount).strip()[:40]}” as a length of time. Write it like 90 days, 6 weeks, 3 months or 10 business days.")
    day, notes = start, []
    try:
        for step in steps:
            count = int(step.group(2)) * (-1 if (step.group(1) == "-") != back else 1)
            unit = step.group(3).lower()
            if unit.startswith(("business", "working", "work d", "weekday")):
                direction, left = (1 if count >= 0 else -1), abs(count)
                whole, left = divmod(left, 5)
                day += timedelta(weeks=whole * direction)
                while left:
                    day += timedelta(days=direction)
                    if day.weekday() < 5:
                        left -= 1
                if "weekends" not in " ".join(notes):
                    notes.append("Counted Monday to Friday; weekends are skipped, public holidays are not.")
            elif unit.startswith(("month", "mo")):
                day, pulled = _add_months(day, count)
                if pulled:
                    notes.append(f"{MONTHS[day.month - 1]} is shorter, so this lands on its last day.")
            elif unit.startswith("y"):
                day, pulled = _add_months(day, count * 12)
                if pulled:
                    notes.append("February 29 does not exist that year, so this lands on February 28.")
            elif unit.startswith("fortnight"):
                day += timedelta(weeks=2 * count)
            elif unit.startswith("w"):
                day += timedelta(weeks=count)
            else:
                day += timedelta(days=count)
    except OverflowError:
        raise DateError("That is beyond the calendar this can count on (years 1 to 9999).") from None
    return day, notes


def about(day: date, today: Optional[date] = None) -> str:
    """One date, described: the weekday, where it falls in the year, and how far it is from today."""
    today = today or date.today()
    number = day.timetuple().tm_yday
    length = 366 if calendar.isleap(day.year) else 365
    week = day.isocalendar()
    text = f"{written(day)} — day {number} of {length}, week {week[1]} of {week[0]} (ISO)"
    gap = (day - today).days
    if gap:
        text += f"; {_plural(abs(gap), 'day')} {'from today' if gap > 0 else 'ago'}"
    else:
        text += "; today"
    return text + "."
