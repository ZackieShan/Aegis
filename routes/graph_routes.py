"""
graph_routes.py — local knowledge-graph memory API (Phase 2, cognee pattern).

  GET  /api/graph/stats           totals + top entities
  GET  /api/graph/query?q=        connected facts about a term
  POST /api/graph/extract         {text, model?} -> extract + store triples
  POST /api/graph/build           {model?} -> build the graph from saved memories
  POST /api/graph/clear           wipe the graph
"""

import json
import logging

from fastapi import APIRouter, Request

from core.middleware import require_admin

logger = logging.getLogger(__name__)


def _default_model() -> str:
    """A sensible tool/reasoning model to run extraction on, if none given."""
    try:
        from core.database import SessionLocal, ModelEndpoint
        db = SessionLocal()
        try:
            models = []
            for ep in db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all():
                for m in (json.loads(ep.cached_models) if ep.cached_models else []) or []:
                    models.append(m)
        finally:
            db.close()
        # prefer a strong tool-caller if present
        for pref in ("qwen", "gemma", "llama-3", "llama3", "mistral"):
            for m in models:
                if pref in m.lower():
                    return m
        return models[0] if models else ""
    except Exception:
        return ""


def setup_graph_routes() -> APIRouter:
    router = APIRouter(prefix="/api/graph", tags=["graph"])

    def _owner(request: Request):
        try:
            from src.auth_helpers import get_current_user
            return get_current_user(request) or ""
        except Exception:
            return ""

    @router.get("/stats")
    def stats(request: Request):
        require_admin(request)
        from src import graph_memory
        return {"enabled": graph_memory.is_enabled(), **graph_memory.stats()}

    @router.get("/query")
    def query(request: Request, q: str = "", limit: int = 50):
        require_admin(request)
        from src import graph_memory
        return {"query": q, "facts": graph_memory.query(q, limit=max(1, min(limit, 200)), owner=_owner(request))}

    @router.post("/extract")
    async def extract(request: Request):
        require_admin(request)
        body = await request.json()
        text = (body.get("text") or "").strip()
        model = (body.get("model") or "").strip() or _default_model()
        from src import graph_memory
        added = await graph_memory.extract_from_text(text, model, owner=_owner(request), source="manual")
        return {"ok": True, "triples_added": added, "model": model}

    @router.post("/build")
    async def build(request: Request):
        require_admin(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        model = (body.get("model") or "").strip() or _default_model()
        if not model:
            return {"ok": False, "error": "no model available — add one in Settings first."}
        from src import graph_memory
        res = await graph_memory.build_from_memories(model, owner=_owner(request),
                                                     limit=int(body.get("limit", 60)))
        res["model"] = model
        return res

    @router.post("/clear")
    def clear(request: Request):
        require_admin(request)
        from src import graph_memory
        return {"ok": True, "removed": graph_memory.clear()}

    return router
