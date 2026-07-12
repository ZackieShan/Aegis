"""
engine_routes.py — local engine (llama-swap) context auto-tuning API.

  GET  /api/engine/status    GPU/RAM + each model's current vs recommended context
  POST /api/engine/tune      apply recommended (auto) or an explicit context size

Admin-gated. Writes engine/llama-swap.yaml, which llama-swap hot-reloads
(-watch-config) — no engine restart. See src/engine_tuner.py.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin

logger = logging.getLogger(__name__)


def setup_engine_routes() -> APIRouter:
    router = APIRouter(prefix="/api/engine", tags=["engine"])

    @router.get("/status")
    def status(request: Request):
        require_admin(request)
        from src import engine_tuner
        return {
            "vram_mb": engine_tuner.gpu_vram_mb(),
            "ram_mb": engine_tuner.system_ram_mb(),
            "config_path": engine_tuner._config_path(),
            "models": engine_tuner.list_models(),
        }

    @router.post("/tune")
    async def tune(request: Request):
        require_admin(request)
        body = await request.json() if await _has_body(request) else {}
        model = (body.get("model") or "").strip() or None
        ctx = body.get("context")
        from src import engine_tuner
        if ctx is not None:
            if not model:
                raise HTTPException(400, "a model is required when setting an explicit context")
            try:
                ctx = int(ctx)
            except (TypeError, ValueError):
                raise HTTPException(400, "context must be an integer")
            res = engine_tuner.set_context(model, ctx)
            if not res.get("ok"):
                raise HTTPException(400, res.get("error", "tune failed"))
            return res
        # auto
        return engine_tuner.autotune(model)

    async def _has_body(request: Request) -> bool:
        try:
            return bool(await request.body())
        except Exception:
            return False

    return router
