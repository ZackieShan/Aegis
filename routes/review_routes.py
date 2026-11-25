"""The daily review, for the administrator's console. Administrators only.

    GET  /api/admin/review          the settings, whether a run is going, the days reviewed, and the latest review
    GET  /api/admin/review/{day}    one day's review
    PUT  /api/admin/review          {"on": true, "time": "04:00", "skip": ["name"], "notify": "problems"} — any of them
    POST /api/admin/review/run      {"day": "yesterday" | "today" | "YYYY-MM-DD"} — review that day now

A review is shown as the reader may see it (``services.review.report.for_reader``): the words of a chat — the
excerpt, the reviewer's note and suggested fix — only for the reader's own chats; everyone else's as figures
and categories.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from core.middleware import require_admin
from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)
_runs: set = set()            # the runs started from here, kept so they are not collected while they work


def setup_review_routes() -> APIRouter:
    router = APIRouter(tags=["review"])

    def _reader(request: Request) -> str:
        require_admin(request)
        return str(get_current_user(request) or "")

    def _state(reader: str, day: Optional[str] = None) -> Dict[str, Any]:
        from services.review import config, judge, report, run, signals

        admins, family, names = run.people()
        settings = config.get()
        history = report.history()
        shown = report.load(day) if day else (report.load(history[0]["day"]) if history else None)
        upcoming = run.next_time()
        return {
            "settings": settings, "status": dict(run.status), "zone": config.zone_name(),
            "next": upcoming.isoformat(timespec="minutes") if upcoming else None,
            "history": history,
            "review": report.for_reader(shown, reader, names) if shown else None,
            "people": [{"username": name, "name": names.get(name, name), "admin": name in admins, "family": name in family,
                        "included": name not in settings["skip"]} for name in sorted(names)],
            "codes": signals.CODES,
            "agents": {key: label for key, (label, _) in judge.AGENTS.items()},
        }

    @router.get("/api/admin/review")
    async def review_overview(request: Request) -> Dict[str, Any]:
        reader = _reader(request)
        return await asyncio.to_thread(_state, reader)

    @router.get("/api/admin/review/{day}")
    async def review_of_day(day: str, request: Request) -> Dict[str, Any]:
        reader = _reader(request)
        try:
            date.fromisoformat(day)
        except ValueError:
            raise HTTPException(400, "A day is written YYYY-MM-DD.")
        from services.review import report, run
        found = await asyncio.to_thread(report.load, day)
        if found is None:
            raise HTTPException(404, "There is no review of that day.")
        return report.for_reader(found, reader, run.people()[2])

    @router.put("/api/admin/review")
    async def review_settings(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        reader = _reader(request)
        from services.review import config
        try:
            await asyncio.to_thread(config.update, body.get("on"), body.get("time"), body.get("skip"), body.get("notify"))
        except config.Refused as problem:
            raise HTTPException(400, str(problem))
        logger.info("daily review: settings changed by %s (%s)", reader or "an administrator", ", ".join(sorted(body)) or "nothing")
        return await asyncio.to_thread(_state, reader)

    @router.post("/api/admin/review/run")
    async def review_now(request: Request, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        _reader(request)
        from services.review import run
        wanted = str((body or {}).get("day") or "yesterday").strip().lower()
        day = run.yesterday() if wanted == "yesterday" else run.today() if wanted == "today" else wanted
        try:
            date.fromisoformat(day)
        except ValueError:
            raise HTTPException(400, "Say yesterday, today, or a day written YYYY-MM-DD.")
        if run.status.get("running"):
            raise HTTPException(409, "A review is already running.")

        async def go() -> None:
            try:
                await run.run(day, force=True, polite=False)
            except run.Nothing as nothing:
                logger.info("daily review (asked for): %s", nothing)
            except Exception as problem:
                logger.warning("daily review (asked for) failed: %s", problem)

        work = asyncio.create_task(go())
        _runs.add(work)
        work.add_done_callback(_runs.discard)
        return {"started": True, "day": day}

    return router
