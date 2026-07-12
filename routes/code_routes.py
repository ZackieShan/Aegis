"""
code_routes.py — local coding agent (Aider) API (Phase 3).

  GET  /api/code/status      is Aider installed? which models can drive it?
  POST /api/code/run         run a coding task, return the full result (JSON)
  POST /api/code/stream      run a coding task, stream Aider's output (SSE)

All admin-gated, mirroring the rest of the builder surface. The agent is scoped
to a workspace directory and, inside a git repo, auto-commits each edit so
changes are revertible. See src/coding_agent.py for the safety fence.
"""

import json
import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from core.middleware import require_admin

logger = logging.getLogger(__name__)


def setup_code_routes() -> APIRouter:
    router = APIRouter(prefix="/api/code", tags=["code"])

    def _owner(request: Request) -> str:
        try:
            from src.auth_helpers import get_current_user
            return get_current_user(request) or ""
        except Exception:
            return ""

    @router.get("/status")
    def status(request: Request):
        require_admin(request)
        from src import coding_agent
        return {
            "available": coding_agent.is_available(),
            "version": coding_agent.version(),
            "engine_python": coding_agent._aider_python(),
            "code_root": os.getenv("AEGIS_CODE_ROOT") or None,
            "models": coding_agent.list_models(_owner(request)),
        }

    @router.post("/run")
    async def run(request: Request):
        require_admin(request)
        body = await request.json()
        from src import coding_agent
        return await coding_agent.run_collect(
            workspace=(body.get("workspace") or ""),
            task=(body.get("task") or ""),
            model=((body.get("model") or "").strip() or None),
            files=(body.get("files") or None),
            owner=_owner(request),
            use_git=body.get("use_git"),
        )

    @router.post("/stream")
    async def stream(request: Request):
        require_admin(request)
        body = await request.json()
        owner = _owner(request)
        from src import coding_agent

        async def gen():
            try:
                async for ev in coding_agent.run(
                    workspace=(body.get("workspace") or ""),
                    task=(body.get("task") or ""),
                    model=((body.get("model") or "").strip() or None),
                    files=(body.get("files") or None),
                    owner=owner,
                    use_git=body.get("use_git"),
                ):
                    yield f"data: {json.dumps(ev)}\n\n"
            except Exception as e:  # pragma: no cover - defensive
                yield f"data: {json.dumps({'type': 'done', 'ok': False, 'error': str(e)})}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
