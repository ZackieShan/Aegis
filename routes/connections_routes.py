"""Connected services, for Settings → Integrations. Administrators only.

    GET    /api/connections            every service the app knows, with whether its key is set (never the key)
    PUT    /api/connections/{id}       {"fields": {"api_key": "…"}, "on": true, "limit": 1000} — any of the three
    DELETE /api/connections/{id}       forget the keys kept for a service
    POST   /api/connections/{id}/try   make one small real request and say whether it worked

The keys are the house's: once saved, every account's assistant uses them (``services/connections``). A key is
write-only here — it can be replaced or removed, but no response ever carries it, only its last four characters.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException, Request

from core.middleware import require_admin

logger = logging.getLogger(__name__)


def setup_connections_routes() -> APIRouter:
    router = APIRouter(tags=["connections"])

    def _service(service_id: str):
        from services.connections import catalog
        service = catalog.get(service_id)
        if service is None:
            raise HTTPException(404, "That is not a service this app knows.")
        return service

    def _who(request: Request) -> str:
        return str(getattr(request.state, "current_user", "") or "")

    @router.get("/api/connections")
    async def connections_list(request: Request) -> Dict[str, Any]:
        require_admin(request)
        from services.connections import store
        return await asyncio.to_thread(store.overview)

    @router.put("/api/connections/{service_id}")
    async def connections_save(service_id: str, request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        require_admin(request)
        service = _service(service_id)
        from services.connections import Refused, store
        fields = body.get("fields")
        if fields is not None and not isinstance(fields, dict):
            raise HTTPException(400, "fields must be an object")
        switch = body.get("on")
        if switch is not None and not isinstance(switch, bool):
            raise HTTPException(400, "on must be true or false")
        try:
            await asyncio.to_thread(store.save, service_id, fields, switch, body.get("limit"))
        except Refused as problem:
            raise HTTPException(400, str(problem))
        # Which fields changed is logged; what was typed never is.
        logger.info("connections: %s changed %s (%s)", _who(request) or "an administrator", service_id,
                    ", ".join(sorted(fields or {})) or ("switch" if switch is not None else "limit"))
        if fields:
            try:
                from services.search import brave
                brave.forget()
            except Exception:
                pass
        return store.describe(service)

    @router.delete("/api/connections/{service_id}")
    async def connections_forget(service_id: str, request: Request) -> Dict[str, Any]:
        require_admin(request)
        service = _service(service_id)
        from services.connections import store
        await asyncio.to_thread(store.forget, service_id)
        logger.info("connections: %s removed the keys for %s", _who(request) or "an administrator", service_id)
        return store.describe(service)

    @router.post("/api/connections/{service_id}/try")
    async def connections_try(service_id: str, request: Request) -> Dict[str, Any]:
        require_admin(request)
        service = _service(service_id)
        from services.connections import checks, store
        ok, said = await asyncio.to_thread(checks.try_it, service_id)
        return {"ok": ok, "said": said, "service": store.describe(service)}

    return router
