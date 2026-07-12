"""
observability_routes.py — read API for the local trace store (Phase 1).

  GET /api/traces            recent traces (optionally ?session_id=&limit=)
  GET /api/traces/stats      totals + per-model rollup
  GET /api/traces/{id}       one trace with prompt/response previews
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin

logger = logging.getLogger(__name__)


def setup_observability_routes() -> APIRouter:
    router = APIRouter(prefix="/api/traces", tags=["traces"])

    @router.get("")
    def list_traces(request: Request, limit: int = 100, session_id: str = ""):
        require_admin(request)
        from src import tracing
        return {
            "enabled": tracing.is_enabled(),
            "traces": tracing.list_traces(limit=max(1, min(limit, 1000)),
                                          session_id=session_id or None),
        }

    @router.get("/stats")
    def trace_stats(request: Request):
        require_admin(request)
        from src import tracing
        return {"enabled": tracing.is_enabled(), **tracing.stats()}

    @router.get("/{trace_id}")
    def get_trace(trace_id: str, request: Request):
        require_admin(request)
        from src import tracing
        t = tracing.get_trace(trace_id)
        if not t:
            raise HTTPException(404, "trace not found")
        return t

    return router
