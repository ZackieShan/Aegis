"""
engine_routes.py — local engine (llama-swap) context auto-tuning API.

  GET  /api/engine/status    GPU/RAM + each model's current vs recommended context
  POST /api/engine/tune      apply recommended (auto) or an explicit context size

Admin-gated. Writes engine/llama-swap.yaml, which llama-swap hot-reloads
(-watch-config) — no engine restart. See src/engine_tuner.py.
"""

import logging
import os

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin

logger = logging.getLogger(__name__)


def _swap_base() -> str:
    """llama-swap's own API root (NOT the /v1 OpenAI surface)."""
    port = os.getenv("LLAMA_SWAP_PORT", "9090")
    return f"http://127.0.0.1:{port}"


async def swap_running_models() -> list:
    """Model ids currently loaded by llama-swap ([] if unreachable/none).

    Shared with the cookbook GPU probe so VRAM held by swappable engine
    models can be reported as reclaimable."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(_swap_base() + "/running")
        if r.status_code != 200:
            return []
        data = r.json()
        items = data.get("running", data) if isinstance(data, dict) else data
        out = []
        for it in items or []:
            if isinstance(it, dict) and it.get("model"):
                out.append(str(it["model"]))
            elif isinstance(it, str):
                out.append(it)
        return out
    except Exception:
        return []


def _warm_meta_cache() -> None:
    """Pre-parse GGUF headers in the background so the first /status call is
    instant. Cold parses take 10-30s on 20GB+ files (worse under disk
    contention while a model is loading), which made /engine time out in the
    browser. Results persist in data/cache/gguf_meta.json."""
    import threading

    def _run():
        try:
            from src import engine_tuner
            engine_tuner.list_models()
        except Exception as e:
            logger.debug(f"engine meta warm-up skipped: {e}")

    threading.Thread(target=_run, name="engine-meta-warmup", daemon=True).start()


def setup_engine_routes() -> APIRouter:
    router = APIRouter(prefix="/api/engine", tags=["engine"])
    _warm_meta_cache()

    @router.get("/status")
    async def status(request: Request):
        require_admin(request)
        import asyncio
        from src import engine_tuner
        running = await swap_running_models()
        # list_models blocks on GGUF reads when the meta cache is cold — keep
        # the event loop free.
        models = await asyncio.to_thread(engine_tuner.list_models)
        return {
            "vram_mb": engine_tuner.gpu_vram_mb(),
            "ram_mb": engine_tuner.system_ram_mb(),
            "config_path": engine_tuner._config_path(),
            "running": running,
            "models": models,
        }

    @router.get("/running")
    async def running(request: Request):
        """Just the model(s) llama-swap currently holds in VRAM — a cheap probe
        (no GGUF reads) for a live "loaded now" indicator in the UI."""
        require_admin(request)
        return {"running": await swap_running_models()}

    @router.post("/unload")
    async def unload(request: Request):
        """Unload every model llama-swap is holding — frees engine VRAM now
        instead of waiting for the ttl. The next request to any model simply
        reloads it (that's the whole point of the swap layer)."""
        require_admin(request)
        import httpx
        was_running = await swap_running_models()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(_swap_base() + "/api/models/unload")
        except Exception as e:
            raise HTTPException(502, f"llama-swap unreachable: {e}")
        if r.status_code // 100 != 2:
            raise HTTPException(502, f"llama-swap unload returned {r.status_code}")
        return {"ok": True, "unloaded": was_running,
                "note": "engine VRAM freed — models reload automatically on next use"}

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
