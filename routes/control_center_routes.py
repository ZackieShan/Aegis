"""
control_center_routes.py — one aggregated capability snapshot for the Control
Center panel.

  GET /api/control-center   every capability + live status + a "try it" action

Admin-gated. See src/control_center.py.
"""

import logging

from fastapi import APIRouter, Request

from core.middleware import require_admin

logger = logging.getLogger(__name__)


def setup_control_center_routes() -> APIRouter:
    router = APIRouter(prefix="/api/control-center", tags=["control-center"])

    @router.get("")
    def snapshot(request: Request):
        require_admin(request)
        from src import control_center
        return control_center.snapshot()

    return router
