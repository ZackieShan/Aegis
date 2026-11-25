"""The day's review put together, kept, and shaped for whoever reads it.

A review is one JSON file a day in ``<data>/reviews``. What it keeps about an exchange depends on whose chat
it was: for an administrator's own chats, a short excerpt of what they asked and the reviewer's note and
suggested fix; for every other account, the rating, the problems (words from the fixed list), the tools used
and the timings — never what was said. ``for_reader`` then hides even an administrator's excerpts from any
other administrator: the words of a chat are shown to the person whose chat it is, and to nobody else.
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
import time
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

from . import signals
from .judge import AGENTS

logger = logging.getLogger(__name__)

VERSION = 1
EXCERPT = 180             # characters of an administrator's own request kept with its review
KEEP_DAYS = 120
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def folder() -> str:
    from src.constants import DATA_DIR
    return os.environ.get("AEGIS_REVIEWS_DIR") or os.path.join(DATA_DIR, "reviews")


def _path(day: str) -> str:
    if not _DAY.fullmatch(str(day)):
        raise ValueError("a day is written YYYY-MM-DD")
    return os.path.join(folder(), f"{day}.json")


# ── one exchange, as it is kept ────────────────────────────────────────────

def entry(exchange: Dict[str, Any], found: Dict[str, Any], review: Optional[Dict[str, Any]], agent: str, keep_words: bool) -> Dict[str, Any]:
    """What is kept about one exchange. ``keep_words`` is true only for an administrator's own chat."""
    problems = list(dict.fromkeys(list((review or {}).get("problems") or []) + signals.codes(found)))
    rating = signals.capped((review or {}).get("rating"), problems) if review else (1 if found.get("no_reply") else None)
    kept: Dict[str, Any] = {
        "id": exchange["id"], "owner": exchange.get("owner") or "", "agent": agent, "model": exchange.get("model") or "",
        "at": exchange["at"].isoformat(timespec="seconds") if exchange.get("at") else "",
        "rating": rating, "problems": problems, "tools": list(dict.fromkeys(found.get("tools") or [])),
        "failed": sorted(set(found.get("failed") or [])), "tool": (review or {}).get("tool") or "",
        "seconds": found.get("seconds"), "reread": bool(found.get("reread")),
        "refusal_words": bool(found.get("refusal_words")), "words": False,
    }
    if keep_words:
        asked = " ".join(str(exchange.get("asked") or "").split())
        kept.update(words=True, chat=exchange.get("chat") or "", asked=asked[:EXCERPT] + ("…" if len(asked) > EXCERPT else ""),
                    note=(review or {}).get("note") or "", fix=(review or {}).get("fix") or "",
                    errors=[_first_line(tool.get("out") or "") for tool in (exchange.get("tools") or []) if not tool.get("ok")][:4])
    return kept


# ── putting the day together ───────────────────────────────────────────────

def _score(entries: Iterable[Dict[str, Any]]) -> Optional[float]:
    ratings = [one["rating"] for one in entries if isinstance(one.get("rating"), int)]
    return round(sum(ratings) / len(ratings), 1) if ratings else None


def _group(entries: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for one in entries:
        groups.setdefault(str(one.get(key) or ""), []).append(one)
    rows = []
    for name, mine in groups.items():
        rows.append({"id": name, "exchanges": len(mine), "score": _score(mine),
                     "needs_a_look": sum(1 for one in mine if isinstance(one.get("rating"), int) and one["rating"] <= 3),
                     "problems": dict(Counter(code for one in mine for code in one["problems"]).most_common())})
    rows.sort(key=lambda row: -row["exchanges"])
    return rows


def _first_line(text: str, room: int = 140) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= room else line[:room].rsplit(" ", 1)[0] + "…"


# What to do about each problem the reviewer can name (the ones a tool or a setting explains are handled apart).
ADVICE = (
    ("no_reply", "See why no reply was given.", "The person asked and got nothing back.", "tool"),
    ("claimed_done", "Stop the assistant saying a thing is done when no tool did it.", "It reported something as saved, sent or changed that was not.", "assistant"),
    ("not_done", "Read these exchanges: what was asked did not get done.", "The request, or part of it, was left undone.", "assistant"),
    ("unsupported", "Keep answers to what was looked up.", "Figures, names or steps were stated that nothing on the page supports.", "assistant"),
    ("tool_failed", "Make the assistant say so when a tool fails.", "A tool failed and the reply passed over it.", "assistant"),
    ("refused", "Find what made the assistant refuse or hedge.", "A plain answer was wanted and not given.", "assistant"),
    ("had_to_repeat", "Read these exchanges: the person had to ask twice.", "They corrected the assistant or repeated the request.", "assistant"),
    ("misread", "Read these exchanges: the request was misunderstood.", "The reply answered something other than what was asked.", "assistant"),
    ("asked_back", "Have it look things up instead of asking.", "It asked the person for something a tool would have told it.", "assistant"),
    ("too_long", "Shorten the answers.", "Replies ran far longer than the question needed.", "assistant"),
    ("unclear", "Make the answers plainer.", "Replies were hard to follow, or in the wrong form for the person and the screen.", "assistant"),
)


def improvements(entries: List[Dict[str, Any]], details: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What the records alone say should change, most often first.

    ``details`` holds, per exchange id, what was learnt but is not kept in the entry (tool errors).
    Each improvement says what to do, why, how many exchanges it touches, and whether it is a setting an
    administrator can change today or work on the assistant.
    """
    found: List[Dict[str, Any]] = []
    rated = [one for one in entries if isinstance(one.get("rating"), int)]

    def add(what: str, why: str, mine: List[Dict[str, Any]], kind: str) -> None:
        if mine:
            found.append({"what": what, "why": why, "count": len(mine), "kind": kind, "from": "records",
                          "examples": [one["id"] for one in mine[:5]]})

    errors = [(one, text) for one in entries for text in (details.get(one["id"], {}).get("errors") or [])]
    no_home = [one for one, text in errors if "no home position is saved" in text.lower()]
    add("Set the household's home town (Admin, Household).", "A place search for “near me” had no home to search near.", no_home, "setting")
    limit = [one for one, text in errors if "limit of" in text.lower() and "used up" in text.lower()]
    add("Raise the monthly limit of the paid service (Settings, Integrations), or expect the fallback.",
        "A look-up was refused because this month's limit was reached.", limit, "setting")
    keyless = [one for one, text in errors if "no brave search key" in text.lower() or "has no appid" in text.lower()]
    add("Add the missing key (Settings, Integrations).", "A look-up needed a service that has no key.", keyless, "setting")

    by_tool: Dict[str, List[Dict[str, Any]]] = {}
    for one in entries:
        for tool in one.get("failed") or []:
            by_tool.setdefault(tool, []).append(one)
    for tool, mine in sorted(by_tool.items(), key=lambda item: -len(item[1])):
        mine = [one for one in mine if one not in no_home and one not in limit and one not in keyless]
        add(f"Look at why {tool} fails.", f"It failed in {len(mine)} exchange(s).", mine, "tool")

    # The tool that should have been used and was not: either nothing was looked up, or the wrong thing was.
    missed: Dict[str, List[Dict[str, Any]]] = {}
    for one in entries:
        if "from_memory" in one["problems"] or "wrong_tool" in one["problems"]:
            missed.setdefault(one.get("tool") or "", []).append(one)
    for tool, mine in sorted(missed.items(), key=lambda item: -len(item[1])):
        add(f"Get {tool} used for questions it fits." if tool else "Get the tool that fits used.",
            "The answer came from the model's memory, or from a tool that does not fit, although the right one was on hand.", mine, "assistant")

    for code, what, why, kind in ADVICE:
        mine = [one for one in entries if code in one["problems"]]
        if code == "tool_failed":
            mine = [one for one in mine if one not in no_home and one not in limit and one not in keyless]
        add(what, why, mine, kind)

    halted = [one for one in entries if "stopped" in one["problems"] or "redone" in one["problems"]]
    add("Read the answers people stopped or asked for again.", "Stopping an answer or asking for it again is the plainest sign it was not what was wanted.", halted, "assistant")
    slow = [one for one in entries if "slow" in one["problems"]]
    if slow:
        times = sorted(one["seconds"] for one in slow if one.get("seconds"))
        add("Find what makes these answers slow.", f"They took over a minute (the middle one {statistics.median(times):.0f} s)." if times else
            "They took over a minute.", slow, "speed")
    reread = [one for one in rated if one.get("reread")]
    if len(reread) >= 2:
        add("Find what keeps changing at the top of the prompt.", "The model read its whole prompt again in the middle of a chat, "
            "which costs several seconds each time.", reread, "speed")
    found.sort(key=lambda item: (-item["count"], item["kind"] != "setting"))
    return found


def build(day: str, entries: List[Dict[str, Any]], details: Dict[str, Dict[str, Any]], model: str, chats: int,
          left_out: int = 0, jobs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The whole review of a day, ready to keep."""
    rated = [one for one in entries if isinstance(one.get("rating"), int)]
    agents = _group(entries, "agent")
    for row in agents:
        row["label"] = AGENTS.get(row["id"], (row["id"] or "Other",))[0]
    improve = improvements(entries, details)
    return {
        "version": VERSION, "day": day, "made": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "model": model,
        "totals": {"chats": chats, "exchanges": len(entries), "rated": len(rated), "score": _score(entries),
                   "needs_a_look": sum(1 for one in rated if one["rating"] <= 3), "left_out": left_out,
                   "by_rating": {str(number): sum(1 for one in rated if one["rating"] == number) for number in (5, 4, 3, 2, 1)}},
        "agents": agents, "models": _group(entries, "model"), "people": _group(entries, "owner"),
        "problems": dict(Counter(code for one in entries for code in one["problems"]).most_common()),
        "improve": improve, "jobs": jobs or {}, "exchanges": entries,
    }


# ── keeping and reading ────────────────────────────────────────────────────

def save(review: Dict[str, Any]) -> str:
    from core.atomic_io import atomic_write_json
    path = _path(review["day"])
    os.makedirs(folder(), mode=0o700, exist_ok=True)
    try:
        os.chmod(folder(), 0o700)           # the folder too: reviews hold excerpts of an administrator's chats
    except OSError:
        pass
    atomic_write_json(path, review, indent=1, mode=0o600)
    prune()
    return path


def load(day: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_path(day), "r", encoding="utf-8") as handle:
            found = json.load(handle)
        return found if isinstance(found, dict) and found.get("day") == day else None
    except (OSError, ValueError):
        return None


def days() -> List[str]:
    """The days there is a review of, newest first."""
    try:
        names = os.listdir(folder())
    except OSError:
        return []
    return sorted((name[:-5] for name in names if name.endswith(".json") and _DAY.fullmatch(name[:-5])), reverse=True)


def prune(keep: int = KEEP_DAYS) -> None:
    for day in days()[keep:]:
        try:
            os.remove(_path(day))
        except OSError:
            pass


def summary(review: Dict[str, Any]) -> Dict[str, Any]:
    totals = review.get("totals") or {}
    return {"day": review.get("day"), "score": totals.get("score"), "exchanges": totals.get("exchanges", 0),
            "chats": totals.get("chats", 0), "needs_a_look": totals.get("needs_a_look", 0)}


def history(limit: int = 30) -> List[Dict[str, Any]]:
    found = []
    for day in days()[:limit]:
        review = load(day)
        if review:
            found.append(summary(review))
    return found


def for_reader(review: Dict[str, Any], reader: str, names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The review as one administrator may see it.

    The words of a chat — the excerpt, the reviewer's note and fix, the chat's id — are shown only for the
    reader's own chats. Every other account appears as figures and categories, under the name the console
    already shows for it.
    """
    names = names or {}
    shown = dict(review)
    exchanges = []
    for one in review.get("exchanges") or []:
        mine = bool(one.get("owner")) and one.get("owner") == reader
        kept = {key: value for key, value in one.items() if key not in ("asked", "note", "fix", "chat", "words", "owner", "errors")}
        kept["mine"] = mine
        kept["who"] = "You" if mine else names.get(one.get("owner") or "", one.get("owner") or "Someone")
        if mine and one.get("words"):
            kept.update(asked=one.get("asked", ""), note=one.get("note", ""), fix=one.get("fix", ""), chat=one.get("chat", ""),
                        errors=one.get("errors") or [])
        exchanges.append(kept)
    shown["exchanges"] = exchanges
    mine_ids = {one["id"] for one in exchanges if one["mine"]}
    # The reviewer's own suggestions, for the reader's chats that went badly, worst first.
    poor = sorted((one for one in exchanges if one["mine"] and one.get("fix") and isinstance(one.get("rating"), int) and one["rating"] <= 3),
                  key=lambda one: one["rating"])
    shown["fixes"] = [{"id": one["id"], "rating": one["rating"], "fix": one["fix"], "note": one.get("note", ""), "asked": one.get("asked", "")}
                      for one in poor[:8]]
    shown["improve"] = [{**item, "examples": [example for example in item.get("examples") or [] if example in mine_ids]}
                        for item in review.get("improve") or []]
    shown["people"] = [{**row, "label": "You" if row["id"] == reader else names.get(row["id"], row["id"] or "Someone")}
                       for row in review.get("people") or []]
    return shown
