"""The whole job: read a day's chats, rate each exchange, put the review together, keep it, say so.

It works through the day a few exchanges at a time and keeps what it has after each step, so a run that
is interrupted — the nightly job gives way the moment anyone starts chatting — picks up where it left off
instead of starting again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import collect, config, judge, report, signals

logger = logging.getLogger(__name__)

MOST = 300                # exchanges reviewed for one day; a busier day is reviewed up to here
_lock = asyncio.Lock()
status: Dict[str, Any] = {"running": False, "day": "", "done": 0, "total": 0, "said": "", "error": "", "finished": ""}


class Nothing(Exception):
    """There is nothing to review; the message says why."""


class Interrupted(Exception):
    """Someone started using the app. What was rated so far is kept; the review carries on later."""


def _busy() -> bool:
    """Is the app serving someone right now? (A screen merely left open does not count.)"""
    try:
        from src.interactive_gate import someone_is_working
        return bool(someone_is_working())
    except Exception:
        return False


def yesterday() -> str:
    return (datetime.now(config.zone()).date() - timedelta(days=1)).isoformat()


def today() -> str:
    return datetime.now(config.zone()).date().isoformat()


# ── what is kept between steps ─────────────────────────────────────────────

def _state_path() -> str:
    return os.path.join(report.folder(), "in-progress.json")


def _kept_so_far(day: str) -> Dict[str, Dict[str, Any]]:
    try:
        with open(_state_path(), "r", encoding="utf-8") as handle:
            found = json.load(handle)
        return dict(found.get("reviews") or {}) if isinstance(found, dict) and found.get("day") == day else {}
    except (OSError, ValueError):
        return {}


def _keep_so_far(day: str, reviews: Dict[str, Dict[str, Any]]) -> None:
    from core.atomic_io import atomic_write_json
    atomic_write_json(_state_path(), {"day": day, "reviews": reviews, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, mode=0o600)


def _forget_so_far() -> None:
    try:
        os.remove(_state_path())
    except OSError:
        pass


# ── who is who ─────────────────────────────────────────────────────────────

def people() -> Tuple[Set[str], Set[str], Dict[str, str]]:
    """(the administrators, the accounts on the family screen, {account: the name shown for it})."""
    try:
        from core.auth import AuthManager
        manager = AuthManager()
        admins, family, names = set(), set(), {}
        for name in manager.users:
            if manager.is_admin(name):
                admins.add(name)
            try:
                seen = manager.get_experience(name) or {}
            except Exception:
                seen = {}
            if seen.get("mode") == "simple":
                family.add(name)
            names[name] = str(seen.get("display_name") or name)
        return admins, family, names
    except Exception:
        logger.debug("the accounts could not be read; nobody's words will be kept", exc_info=True)
        return set(), set(), {}


def _candidates() -> List[tuple]:
    """The models to review with, best first: the main chat model (it reasons properly), then the background ones."""
    found: List[tuple] = []
    try:
        from src.endpoint_resolver import resolve_endpoint
        url, model, headers = resolve_endpoint("default")
        if url and model:
            found.append((url, model, headers or {}))
    except Exception:
        pass
    try:
        from src.task_endpoint import resolve_task_candidates
        for url, model, headers in resolve_task_candidates():
            if url and model and not any((url, model) == (other[0], other[1]) for other in found):
                found.append((url, model, headers or {}))
    except Exception:
        pass
    return found


def _jobs(start: datetime, end: datetime) -> Dict[str, int]:
    """How the background jobs went that day: how many ran, and how many failed. Figures only."""
    try:
        from sqlalchemy import text

        from core.database import SessionLocal
        db = SessionLocal()
        try:
            rows = db.execute(text("SELECT status, COUNT(*) FROM task_runs WHERE started_at >= :start AND started_at < :end GROUP BY status"),
                              {"start": start, "end": end}).fetchall()
        finally:
            db.close()
        counts = {str(status or ""): int(number) for status, number in rows}
        return {"ran": counts.get("success", 0) + counts.get("error", 0), "failed": counts.get("error", 0)}
    except Exception:
        return {}


# ── the job ────────────────────────────────────────────────────────────────

def _say(progress: Optional[Callable[[str], None]], words: str) -> None:
    status["said"] = words
    if progress:
        try:
            progress(words)
        except Exception:
            pass


async def run(day: Optional[str] = None, progress: Optional[Callable[[str], None]] = None, force: bool = False,
              polite: bool = True) -> Dict[str, Any]:
    """Review ``day`` (yesterday when not given) and keep the result. Returns its summary.

    Raises ``Nothing`` when there is nothing to do: the day is already reviewed, nobody chatted, or another
    run is going. A ``polite`` run (the nightly one) stops the moment anyone uses the app and raises
    ``Interrupted``; what it had rated is kept and the next run carries on from there. An administrator
    who pressed the button gets an impolite one, which runs to the end.
    """
    day = day or yesterday()
    date.fromisoformat(day)
    if _lock.locked():
        raise Nothing("a review is already running")
    async with _lock:
        status.update(running=True, day=day, done=0, total=0, said="Reading the day's chats…", error="")
        try:
            return await _run(day, progress, force, polite)
        except Nothing as nothing:
            status["said"] = str(nothing)
            raise
        except (Interrupted, asyncio.CancelledError):
            status["said"] = "Paused: someone started using the app. It carries on later from where it stopped."
            raise
        except Exception as problem:
            status["error"] = f"{type(problem).__name__}: {problem}"[:300]
            raise
        finally:
            status.update(running=False, finished=time.strftime("%Y-%m-%dT%H:%M:%S%z"))


async def _rate(batch: list, candidates: List[tuple], polite: bool) -> Dict[str, Dict[str, Any]]:
    """Rate one batch.

    The model call runs as a task of its own, because the app's model gate cancels a background call the
    moment someone's chat wants the model — and it must be that call it cancels, not the whole job. A
    polite run then stops (and carries on later); a run an administrator asked for waits a moment and asks
    again. A polite run also watches for anyone using the app in other ways, and gives the batch up at once.
    """
    for _ in range(4):
        work = asyncio.ensure_future(judge.rate(batch, candidates))
        try:
            while not work.done():
                await asyncio.wait({work}, timeout=0.5)
                if polite and not work.done() and _busy():
                    raise Interrupted()
        finally:
            if not work.done():
                work.cancel()
                try:
                    await work
                except BaseException:
                    pass
        if not work.cancelled():
            return work.result()
        if polite:
            raise Interrupted()
        await asyncio.sleep(3.0)            # the model went to someone's chat; ask again when it is free
    raise RuntimeError("the model was kept busy by chats")


async def _run(day: str, progress, force: bool, polite: bool) -> Dict[str, Any]:
    if not force and report.load(day) is not None:
        raise Nothing(f"{day} is already reviewed")
    settings = config.get()
    start, end = collect.window(date.fromisoformat(day), config.zone())
    chats = await asyncio.to_thread(collect.read, start, end)
    everything = collect.exchanges(chats, start, end)
    admins, family, _ = people()
    skip = set(settings["skip"])
    exchanges = [one for one in everything if one["owner"] not in skip]
    left_out = len(everything) - len(exchanges)
    if not exchanges:
        raise Nothing(f"nobody chatted on {day}" if not everything else f"every chat on {day} belongs to an account that is left out")
    if len(exchanges) > MOST:
        exchanges = exchanges[:MOST]
    candidates = _candidates()
    if not candidates:
        raise RuntimeError("There is no model to review with: set a default model first.")
    found = {one["id"]: signals.read(one) for one in exchanges}
    reviews = _kept_so_far(day)
    status.update(total=len(exchanges), done=sum(1 for one in exchanges if one["id"] in reviews))

    by_chat: Dict[str, List[Dict[str, Any]]] = {}
    for one in exchanges:
        by_chat.setdefault(one["chat"], []).append(one)
    failures = 0
    for mine in by_chat.values():
        waiting = [one for one in mine if one["id"] not in reviews and (one["reply"] or one["tools"])]
        for at in range(0, len(waiting), judge.BATCH):
            batch = waiting[at: at + judge.BATCH]
            if polite and _busy():
                raise Interrupted()
            _say(progress, f"Reviewing {status['done'] + 1} to {status['done'] + len(batch)} of {len(exchanges)} exchanges…")
            try:
                rated = await _rate([(one, found[one["id"]], one["owner"] in family) for one in batch], candidates, polite)
                failures = 0
            except (Interrupted, asyncio.CancelledError):
                raise
            except Exception as problem:
                failures += 1
                logger.warning("review: a batch could not be rated (%s)", type(problem).__name__)
                if failures >= 2:
                    raise RuntimeError("The model stopped answering during the review. What was rated so far is kept; "
                                       "run it again to finish.") from problem
                status["done"] += len(batch)
                continue                    # nothing is kept for these: a later run asks about them again
            for one in batch:
                # The model answered, but with no usable review of this one. It is kept as "not rated",
                # so that a second run does not ask about it for ever.
                reviews[one["id"]] = rated.get(one["id"]) or {"rating": None, "problems": [], "tool": "", "note": "", "fix": ""}
            status["done"] += len(batch)
            await asyncio.to_thread(_keep_so_far, day, reviews)

    entries, details = [], {}
    for one in exchanges:
        review = reviews.get(one["id"])
        if review is not None and review.get("rating") is None:
            review = None
        entries.append(report.entry(one, found[one["id"]], review, judge.agent_of(one, one["owner"] in family), one["owner"] in admins))
        details[one["id"]] = {"errors": [tool.get("out") or "" for tool in one["tools"] if not tool.get("ok")]}
    made = report.build(day, entries, details, candidates[0][1], len(by_chat), left_out, await asyncio.to_thread(_jobs, start, end))
    await asyncio.to_thread(report.save, made)
    _forget_so_far()
    summary = report.summary(made)
    _say(progress, said(summary))
    await _tell(summary, settings["notify"])
    return summary


def said(summary: Dict[str, Any]) -> str:
    """The review in a line."""
    score = summary.get("score")
    exchanges, chats, poor = summary.get("exchanges", 0), summary.get("chats", 0), summary.get("needs_a_look", 0)
    head = f"{score} out of 5" if score is not None else "not rated"
    look = "none needs a look" if not poor else f"{poor} need{'s' if poor == 1 else ''} a look"
    return f"{summary.get('day')}: {head}. {exchanges} exchange{'' if exchanges == 1 else 's'} in {chats} chat{'' if chats == 1 else 's'}; {look}."


async def _tell(summary: Dict[str, Any], when: str) -> None:
    if when == "never" or (when == "problems" and not summary.get("needs_a_look")):
        return
    try:
        from services import notify
        score = summary.get("score")
        title = f"Daily review: {score} out of 5" if score is not None else "Daily review"
        await notify.tell_admins(title, said(summary).split(": ", 1)[-1], url="/admin#review", tag="daily-review")
    except Exception:
        logger.debug("the administrators were not told about the review", exc_info=True)


# ── once a day ─────────────────────────────────────────────────────────────

RETRY_MINUTES = 15
_tried: Dict[str, float] = {}             # day → when the nightly job last had a go at it
_settled: Dict[str, str] = {}             # day → why there is nothing to review (so it is not asked again all day)
_waiting: Dict[str, Any] = {}             # the day it is waiting to review, and when it last said so in the log


def due(now: Optional[datetime] = None) -> Optional[str]:
    """The day the nightly job should review now, or None.

    Each day, from the set time on, the day before is reviewed — once. If the server was off at that time
    the review is made when it is next up; if a run was interrupted it is taken up again a quarter of an
    hour later.
    """
    settings = config.get()
    if not settings["on"]:
        return None
    now = now or datetime.now(config.zone())
    hour, minute = (int(part) for part in settings["time"].split(":"))
    if (now.hour, now.minute) < (hour, minute):
        return None
    day = (now.date() - timedelta(days=1)).isoformat()
    if day in _settled or report.load(day) is not None:
        return None
    if time.monotonic() - _tried.get(day, -1e9) < RETRY_MINUTES * 60:
        return None
    return day


def next_time(now: Optional[datetime] = None) -> Optional[datetime]:
    """When the nightly job will next start, on the house's clock; None while it is switched off."""
    settings = config.get()
    if not settings["on"]:
        return None
    now = now or datetime.now(config.zone())
    hour, minute = (int(part) for part in settings["time"].split(":"))
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


async def nightly(now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """One look by the nightly job: review the day that is due, if any, and if nobody is using the app."""
    day = due(now)
    if day is None:
        return None
    if _busy():
        if _waiting.get("day") != day or time.monotonic() - _waiting.get("said", -1e9) > 3600:
            _waiting.update(day=day, said=time.monotonic())
            logger.info("daily review of %s is due; waiting until nobody is using the app", day)
        return None
    _tried[day] = time.monotonic()
    for old in [key for key in _settled if key < day]:
        _settled.pop(old, None)
    try:
        summary = await run(day)
        logger.info("daily review: %s", said(summary))
        return summary
    except Nothing as nothing:
        _settled[day] = str(nothing)
        logger.info("daily review: %s", nothing)
    except Interrupted:
        logger.info("daily review of %s paused: someone is using the app", day)
    except Exception as problem:
        logger.warning("daily review of %s failed: %s", day, problem)
    return None


async def keep_time(every: float = 60.0) -> None:
    """Runs for as long as the server does: once a minute, see whether a review is due."""
    while True:
        await asyncio.sleep(every)
        try:
            await nightly()
        except asyncio.CancelledError:
            raise
        except Exception as problem:
            logger.warning("daily review: the minute's look failed (%s: %s)", type(problem).__name__, problem)
