"""
doctor_routes.py — capability self-check + guarded self-heal.

  GET  /api/doctor            run all capability checks (statuses + remedies)
  POST /api/doctor/fix/{id}   run the ONE allowlisted remedy for that check

The fix endpoint takes only a check id (from the path) — never a command — so a
caller can't turn it into arbitrary code execution. It's admin-gated on top.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin

logger = logging.getLogger(__name__)


def setup_doctor_routes() -> APIRouter:
    router = APIRouter(prefix="/api/doctor", tags=["doctor"])

    @router.get("")
    def doctor(request: Request):
        require_admin(request)
        from src import doctor as doc
        checks = doc.run_checks()
        return {"checks": checks, "problems": sum(1 for c in checks if not c["ok"])}

    @router.post("/fix/{check_id}")
    def fix(check_id: str, request: Request):
        require_admin(request)
        from src import doctor as doc
        result = doc.apply_fix(check_id)
        if not result.get("ok"):
            # 200 with ok:false so the UI can show the error text; only truly
            # unknown ids are a 404.
            if result.get("error") == "no remedy for this check":
                raise HTTPException(404, "unknown check id")
        return result

    return router
