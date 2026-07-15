"""
canvas_routes.py — code canvas API (generate + inline AI edit of a code buffer).

  POST /api/canvas/generate  {prompt, language?, model?}  -> full program
  POST /api/canvas/edit      {code, instruction, language?, model?} -> updated code
  POST /api/canvas/preview   {code}    -> {token}   (stage HTML for the run iframe)
  GET  /api/canvas/preview/{token}     -> the staged HTML

Generate/edit are admin-gated; see src/canvas.py. Preview is how Run executes
HTML/JS: the code is staged server-side and served back under a CSP `sandbox`
policy (opaque origin, scripts allowed, no cookies/API reach — see
SecurityHeadersMiddleware). A srcdoc/blob iframe can't do this job because it
inherits the app's nonce CSP, which blocks the generated page's inline scripts.
"""

import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from core.middleware import require_admin
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

_PREVIEW_TTL_S = 600
_PREVIEW_MAX = 40
_PREVIEW_MAX_BYTES = 2_000_000
_previews: dict = {}


def _prune_previews() -> None:
    now = time.time()
    stale = [k for k, v in _previews.items() if now - v["ts"] > _PREVIEW_TTL_S]
    for k in stale:
        _previews.pop(k, None)
    # >= because pruning runs before the caller inserts one more entry.
    while len(_previews) >= _PREVIEW_MAX:
        oldest = min(_previews, key=lambda k: _previews[k]["ts"])
        _previews.pop(oldest, None)


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

    @router.post("/preview")
    async def preview_create(request: Request):
        require_user(request)
        body = await request.json()
        code = str(body.get("code") or "")
        if not code.strip():
            raise HTTPException(400, "No code")
        if len(code.encode("utf-8", "ignore")) > _PREVIEW_MAX_BYTES:
            raise HTTPException(413, "Code too large to preview")
        _prune_previews()
        token = uuid.uuid4().hex
        _previews[token] = {"code": code, "ts": time.time()}
        return {"token": token, "url": f"/api/canvas/preview/{token}"}

    @router.get("/preview/{token}")
    async def preview_serve(request: Request, token: str):
        require_user(request)
        entry = _previews.get(token)
        if not entry or time.time() - entry["ts"] > _PREVIEW_TTL_S:
            raise HTTPException(404, "Preview expired — hit Run again")
        return HTMLResponse(entry["code"])

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
