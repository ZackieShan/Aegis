"""
wiki_routes.py — repo → structured wiki API (Phase 3, deepwiki-open pattern).

  POST /api/wiki/generate   {path, model?} -> scan a repo + write a markdown wiki
  GET  /api/wiki            list saved wikis
  GET  /api/wiki/{name}     fetch a saved wiki's markdown

All admin-gated. Generation runs locally (see src/repo_wiki.py); no external
service or API key.
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin

logger = logging.getLogger(__name__)


def _default_model() -> str:
    """A strong local tool/reasoning model to write the wiki, if none given."""
    try:
        from core.database import ModelEndpoint, SessionLocal
        db = SessionLocal()
        try:
            models = []
            for ep in db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all():  # noqa: E712
                for m in (json.loads(ep.cached_models) if ep.cached_models else []) or []:
                    models.append(m)
        finally:
            db.close()
        for pref in ("qwen", "gemma", "llama-3", "llama3", "mistral", "deepseek"):
            for m in models:
                if pref in m.lower():
                    return m
        return models[0] if models else ""
    except Exception:
        return ""


def setup_wiki_routes() -> APIRouter:
    router = APIRouter(prefix="/api/wiki", tags=["wiki"])

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
        path = (body.get("path") or "").strip()
        if not path:
            raise HTTPException(400, "path is required")
        model = (body.get("model") or "").strip() or _default_model()
        if not model:
            return {"ok": False, "error": "no model available — add one in Settings first."}
        from src import repo_wiki
        return await repo_wiki.generate_wiki(path, model, owner=_owner(request))

    @router.get("")
    def list_wikis(request: Request):
        require_admin(request)
        from src import repo_wiki
        return {"wikis": repo_wiki.list_wikis()}

    @router.get("/{name}")
    def get_wiki(name: str, request: Request):
        require_admin(request)
        from src import repo_wiki
        md = repo_wiki.get_wiki(name)
        if md is None:
            raise HTTPException(404, "wiki not found")
        return {"name": name, "markdown": md}

    return router
