"""
recipes_routes.py — CRUD + run API for Recipes (visual orchestration workflows).

Endpoints (all admin-gated, mirroring the rest of the builder surface):
  GET    /api/recipes                 list saved recipes (metadata)
  GET    /api/recipes/tools           available node building blocks (models + toolbox tools)
  GET    /api/recipes/{id}            full recipe graph
  POST   /api/recipes                 create/update a recipe (JSON body)
  DELETE /api/recipes/{id}            delete a recipe
  POST   /api/recipes/{id}/run        run a saved recipe with an input
  POST   /api/recipes/run             run an unsaved graph (from the editor) with an input
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import recipes as recipes_engine

logger = logging.getLogger(__name__)


def setup_recipes_routes() -> APIRouter:
    router = APIRouter(prefix="/api/recipes", tags=["recipes"])

    def _owner(request: Request):
        try:
            from src.auth_helpers import get_current_user
            return get_current_user(request) or None
        except Exception:
            return None

    @router.get("")
    def list_recipes(request: Request):
        require_admin(request)
        return {"recipes": recipes_engine.list_recipes(owner=_owner(request))}

    @router.get("/tools")
    def list_building_blocks(request: Request):
        """Models + toolbox tools the editor can drop as nodes."""
        require_admin(request)
        # Available models (from the picker's endpoint list).
        models = []
        try:
            from core.database import SessionLocal, ModelEndpoint
            db = SessionLocal()
            try:
                for ep in db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all():
                    import json as _json
                    for m in (_json.loads(ep.cached_models) if ep.cached_models else []) or []:
                        if m not in models:
                            models.append(m)
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"recipes model list failed: {e}")
        # Toolbox tools (the opt-in MCP toolboxes).
        tools = []
        try:
            from src.tool_utils import get_mcp_manager
            from src.builtin_mcp import TOOLBOX_MCP_IDS
            mcp = get_mcp_manager()
            for t in mcp.get_all_tools():
                if t.get("server_id") in TOOLBOX_MCP_IDS:
                    tools.append({
                        "name": t.get("name"),
                        "server": t.get("server_id"),
                        "description": (t.get("description") or "")[:200],
                        "params": list((t.get("input_schema", {}) or {}).get("properties", {}).keys()),
                    })
        except Exception as e:
            logger.debug(f"recipes tool list failed: {e}")
        return {"models": models, "tools": tools}

    @router.get("/{recipe_id}")
    def get_recipe(recipe_id: str, request: Request):
        require_admin(request)
        r = recipes_engine.get_recipe(recipe_id)
        if not r:
            raise HTTPException(404, "recipe not found")
        return r

    @router.post("")
    async def save_recipe(request: Request):
        require_admin(request)
        body: Dict[str, Any] = await request.json()
        try:
            return recipes_engine.save_recipe(body, owner=_owner(request))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.delete("/{recipe_id}")
    def delete_recipe(recipe_id: str, request: Request):
        require_admin(request)
        if not recipes_engine.delete_recipe(recipe_id):
            raise HTTPException(404, "recipe not found")
        return {"ok": True}

    @router.post("/run")
    async def run_unsaved(request: Request):
        require_admin(request)
        body = await request.json()
        recipe = body.get("recipe") or body
        run_input = body.get("input", "")
        return await recipes_engine.run_recipe(recipe, run_input, owner=_owner(request))

    @router.post("/{recipe_id}/run")
    async def run_saved(recipe_id: str, request: Request):
        require_admin(request)
        r = recipes_engine.get_recipe(recipe_id)
        if not r:
            raise HTTPException(404, "recipe not found")
        body = await request.json() if await _has_body(request) else {}
        run_input = (body or {}).get("input", "")
        return await recipes_engine.run_recipe(r, run_input, owner=_owner(request))

    async def _has_body(request: Request) -> bool:
        try:
            return bool(await request.body())
        except Exception:
            return False

    return router
