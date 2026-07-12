"""
canvas_routes.py — code canvas API (generate + inline AI edit of a code buffer).

  POST /api/canvas/generate  {prompt, language?, model?}  -> full program
  POST /api/canvas/edit      {code, instruction, language?, model?} -> updated code

Admin-gated. See src/canvas.py. Runs on a local coder — no workspace/git (that's
the /code Aider agent); this is a stateless single-buffer assistant.
"""

import logging

from fastapi import APIRouter, Request

from core.middleware import require_admin

logger = logging.getLogger(__name__)


def setup_canvas_routes() -> APIRouter:
    router = APIRouter(prefix="/api/canvas", tags=["canvas"])

    def _owner(request: Request) -> str:
        try:
            from src.auth_helpers import get_current_user
            return get_current_user(request) or ""
        except Exception:
            return ""

    @router.post("/generate")
    async def generate(request: Request):
        require_admin(request)
        body = await request.json()
        from src import canvas
        return await canvas.generate(
            prompt=(body.get("prompt") or ""),
            language=(body.get("language") or "").strip(),
            model=(body.get("model") or "").strip(),
            owner=_owner(request),
        )

    @router.post("/edit")
    async def edit(request: Request):
        require_admin(request)
        body = await request.json()
        from src import canvas
        return await canvas.edit(
            code=(body.get("code") or ""),
            instruction=(body.get("instruction") or ""),
            language=(body.get("language") or "").strip(),
            model=(body.get("model") or "").strip(),
            owner=_owner(request),
        )

    return router
