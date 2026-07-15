"""Automations (Jobs) API — a recipe on a trigger that delivers its result.

Gated on `can_use_recipes` (same as the recipe library): running a vetted
canned recipe on a schedule is safe. Jobs are owner-scoped.

  GET    /api/jobs                list this owner's automations
  POST   /api/jobs               create / update a job
  DELETE /api/jobs/{id}          delete
  POST   /api/jobs/{id}/toggle   enable / disable
  POST   /api/jobs/{id}/run      run now (ignores the schedule)
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from src import jobs as jobs_engine

logger = logging.getLogger(__name__)


def setup_jobs_routes() -> APIRouter:
    router = APIRouter(prefix="/api/jobs", tags=["jobs"])

    def _owner(request: Request):
        try:
            from src.auth_helpers import get_current_user
            return get_current_user(request) or None
        except Exception:
            return None

    def _gate(request: Request):
        from src.auth_helpers import require_privilege
        return require_privilege(request, "can_use_recipes")

    def _owned(request: Request, job_id: str):
        job = jobs_engine.get_job(job_id)
        if not job:
            raise HTTPException(404, "automation not found")
        owner = _owner(request)
        if (job.get("owner") or None) not in (None, owner):
            raise HTTPException(404, "automation not found")
        return job

    @router.get("")
    def list_jobs(request: Request):
        _gate(request)
        return {"jobs": jobs_engine.list_jobs(owner=_owner(request))}

    @router.post("")
    async def save_job(request: Request):
        _gate(request)
        body = await request.json()
        # editing an existing job: enforce ownership before the write
        if body.get("id"):
            _owned(request, body["id"])
        try:
            return {"job": jobs_engine.save_job(body, owner=_owner(request))}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.delete("/{job_id}")
    def delete_job(job_id: str, request: Request):
        _gate(request)
        _owned(request, job_id)
        if not jobs_engine.delete_job(job_id):
            raise HTTPException(404, "automation not found")
        return {"ok": True}

    @router.post("/{job_id}/toggle")
    async def toggle_job(job_id: str, request: Request):
        _gate(request)
        _owned(request, job_id)
        body = await request.json() if await _has_body(request) else {}
        job = jobs_engine.get_job(job_id)
        enabled = bool(body.get("enabled", not job.get("enabled")))
        return {"job": jobs_engine.set_enabled(job_id, enabled)}

    @router.post("/{job_id}/run")
    async def run_job(job_id: str, request: Request):
        _gate(request)
        job = _owned(request, job_id)
        result = await jobs_engine.fire_job(job)
        return result

    async def _has_body(request: Request) -> bool:
        try:
            return bool(await request.body())
        except Exception:
            return False

    return router
