"""The almanac: exact answers, for the page at /almanac.

    GET    /api/almanac           what is on hand: unit menus, whether place names are installed, the saved home position
    POST   /api/almanac/convert   {"amount", "to"?}                         a measurement in other units
    POST   /api/almanac/place     {"place"} | {"from", "to"} | {"from", "bearing", "distance"}
    POST   /api/almanac/sky       {"place"?, "date"?, "days"?, "zone"?}     sun and moon
    POST   /api/almanac/dates     {"date", "to"?, "add"?}                   calendar arithmetic
    POST   /api/almanac/weather   {"place"?, "when"?}                       the forecast (the one outside source)
    PUT    /api/almanac/home      {"place", "zone"?}                        save the signed-in person's home position
    DELETE /api/almanac/home      forget it

Nothing here calls a model, and only the forecast reaches the network: it is all ``services/almanac``.
The same code answers the assistant's ``convert_units``, ``map_position``,
``sun_moon`` and ``date_math`` tools, so the page and a chat agree.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException, Request

from src.auth_helpers import require_user

logger = logging.getLogger(__name__)


def setup_almanac_routes() -> APIRouter:
    router = APIRouter(tags=["almanac"])

    def _who(request: Request) -> str:
        owner = require_user(request)
        try:                                            # the clock on the person's own device, for "today" and for times
            from src import user_time
            user_time.set_user_tz_offset(request.headers.get("x-tz-offset"))
            user_time.set_user_tz_name(request.headers.get("x-tz-name"))
        except Exception:
            pass
        return owner

    def _body(body: Any) -> Dict[str, Any]:
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON object required")
        return {key: (value[:400] if isinstance(value, str) else value) for key, value in body.items()}

    async def _ask(tool, body: Dict[str, Any], owner: str) -> Dict[str, Any]:
        found = await tool().execute(json.dumps(body), {"owner": owner or None})
        if found.get("exit_code"):
            raise HTTPException(400, str(found.get("error") or "That could not be worked out.").split(": ", 1)[-1])
        return {"text": found.get("output") or ""}

    @router.get("/api/almanac")
    async def almanac_start(request: Request) -> Dict[str, Any]:
        owner = _who(request)
        from services.almanac import gazetteer, places, units
        kept = places.home(owner or None)
        return {"units": units.listing(), "places": gazetteer.count(), "home": places.describe(kept) if kept else None}

    @router.post("/api/almanac/convert")
    async def almanac_convert(request: Request, body: dict = Body(...)) -> Dict[str, Any]:
        _who(request)
        from services.almanac import units
        asked = _body(body)
        try:
            found = units.convert(str(asked.get("amount") or ""), str(asked.get("to") or ""))
        except units.UnitError as problem:
            raise HTTPException(400, str(problem))
        except (ValueError, OverflowError, ZeroDivisionError):
            raise HTTPException(400, "That could not be worked out.")
        kind = str(found.get("kind") or "")
        choices = next((entry["units"] for entry in units.listing() if entry["kind"] == kind), [])
        return {**found, "choices": choices}

    @router.post("/api/almanac/place")
    async def almanac_place(request: Request, body: dict = Body(...)) -> Dict[str, Any]:
        owner = _who(request)
        from src.agent_tools.almanac_tools import MapPositionTool
        from services.almanac import places
        asked = _body(body)
        answer = await _ask(MapPositionTool, asked, owner)
        if asked.get("place") and not asked.get("bearing"):
            try:                                        # so the page can offer "save as my home"
                answer["place"] = places.describe(places.resolve(str(asked["place"]), owner or None))
            except places.PlaceError:
                pass
        return answer

    @router.post("/api/almanac/sky")
    async def almanac_sky(request: Request, body: dict = Body(...)) -> Dict[str, Any]:
        owner = _who(request)
        from src.agent_tools.almanac_tools import AlmanacProblem, sun_moon_answer
        try:
            text, summary = sun_moon_answer(_body(body), owner or None)
        except AlmanacProblem as problem:
            raise HTTPException(400, str(problem))
        return {"text": text, "summary": summary}

    @router.post("/api/almanac/dates")
    async def almanac_dates(request: Request, body: dict = Body(...)) -> Dict[str, Any]:
        owner = _who(request)
        from src.agent_tools.almanac_tools import DateMathTool
        return await _ask(DateMathTool, _body(body), owner)

    @router.post("/api/almanac/weather")
    async def almanac_weather(request: Request, body: dict = Body(...)) -> Dict[str, Any]:
        owner = _who(request)
        import asyncio
        from services.almanac import weather
        try:
            text, card = await asyncio.to_thread(weather.answer, _body(body), owner or None)
        except weather.WeatherProblem as problem:
            if problem.reason in ("place", "range"):
                raise HTTPException(400, str(problem))
            return {"text": str(problem), "card": None, "unavailable": problem.reason}
        return {"text": text, "card": card}

    @router.put("/api/almanac/home")
    async def almanac_set_home(request: Request, body: dict = Body(...)) -> Dict[str, Any]:
        owner = _who(request)
        from services.almanac import places
        asked = _body(body)
        try:
            kept = places.set_home(owner or None, str(asked.get("place") or ""), str(asked.get("zone") or ""))
        except places.PlaceError as problem:
            raise HTTPException(400, str(problem))
        return {"home": places.describe(kept)}

    @router.delete("/api/almanac/home")
    async def almanac_forget_home(request: Request) -> Dict[str, Any]:
        owner = _who(request)
        from services.almanac import places
        return {"removed": places.forget_home(owner or None)}

    return router
