"""
recipes_routes.py — the Recipes API: a browsable library of one-click canned
workflows, plus the admin authoring surface behind it.

Run access (the library + running canned recipes) needs the `can_use_recipes`
privilege — on by default, so any signed-in user can run a vetted recipe.
Authoring (create / edit / delete / building blocks / run-any-graph) stays
admin-only, because a hand-built recipe can call tools and shell.

  Library (run access):
    GET  /api/recipes/catalog                    canned recipes + availability
    POST /api/recipes/catalog/{tid}/run          run a canned recipe with an input
    POST /api/recipes/toolbox/enable             enable + validate a toolbox (admin)
  Authoring (admin):
    GET/POST/DELETE /api/recipes[...]            saved-recipe CRUD, building blocks, run
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

    def _require_run(request: Request):
        """Gate the library: admins pass, others need can_use_recipes."""
        from src.auth_helpers import require_privilege
        return require_privilege(request, "can_use_recipes")

    def _connected_toolboxes() -> set:
        """Toolbox ids currently connected (their tools are discoverable)."""
        try:
            from src.tool_utils import get_mcp_manager
            from src.builtin_mcp import TOOLBOX_MCP_IDS
            mcp = get_mcp_manager()
            return {t.get("server_id") for t in mcp.get_all_tools()
                    if t.get("server_id") in TOOLBOX_MCP_IDS}
        except Exception as e:
            logger.debug(f"connected toolboxes lookup failed: {e}")
            return set()

    @router.get("")
    def list_recipes(request: Request):
        require_admin(request)
        return {"recipes": recipes_engine.list_recipes(owner=_owner(request))}

    def _building_blocks() -> Dict[str, Any]:
        """Models + toolbox tools available as recipe nodes on this install."""
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

    @router.get("/tools")
    def list_building_blocks(request: Request):
        """Models + toolbox tools the editor can drop as nodes."""
        require_admin(request)
        return _building_blocks()

    @router.get("/starters")
    def list_starters(request: Request):
        """Preview the starter recipes that fit this install (not saved)."""
        require_admin(request)
        from src import recipe_templates
        blocks = _building_blocks()
        recipes = recipe_templates.generate_starters(blocks["models"], blocks["tools"])
        return {"recipes": recipes, "count": len(recipes)}

    @router.post("/starters/install")
    def install_starters(request: Request):
        """Seed the saved-recipes list with the starter set, deduped by name."""
        require_admin(request)
        from src import recipe_templates
        owner = _owner(request)
        blocks = _building_blocks()
        generated = recipe_templates.generate_starters(blocks["models"], blocks["tools"])
        existing_names = {r.get("name") for r in recipes_engine.list_recipes(owner=owner)}
        installed, skipped = [], []
        for rec in generated:
            if rec.get("name") in existing_names:
                skipped.append(rec.get("name"))
                continue
            try:
                saved = recipes_engine.save_recipe(
                    {"name": rec["name"], "nodes": rec["nodes"], "edges": rec["edges"]},
                    owner=owner,
                )
                installed.append({"id": saved["id"], "name": saved["name"]})
            except ValueError as e:
                logger.warning("starter recipe %r skipped: %s", rec.get("name"), e)
        return {"installed": installed, "skipped": skipped, "count": len(installed)}

    # ── library (run access) ──────────────────────────────────────────────────
    @router.get("/catalog")
    def get_catalog(request: Request):
        """The canned-recipe library: every recipe with metadata + availability."""
        _require_run(request)
        from src import recipe_templates
        blocks = _building_blocks()
        cat = recipe_templates.catalog(blocks["models"], _connected_toolboxes())
        # The graph itself is only needed for admins who want "Open in editor";
        # keep the library payload lean for everyone else.
        is_admin = _is_admin(request)
        for e in cat:
            if not is_admin:
                e.pop("recipe", None)
        return {"recipes": cat, "categories": recipe_templates.categories(),
                "can_author": is_admin}

    @router.post("/catalog/{template_id}/run")
    async def run_catalog(template_id: str, request: Request):
        """Run a canned recipe by id with a run input — no graph, no saving."""
        _require_run(request)
        from src import recipe_templates
        meta = recipe_templates.get_template(template_id)
        if not meta:
            raise HTTPException(404, "unknown recipe")
        if meta.get("preview"):
            raise HTTPException(409, "This recipe arrives with Automations — it isn't runnable yet.")
        needs = meta.get("needs_toolbox")
        if needs and needs not in _connected_toolboxes():
            from src.recipe_templates import TOOLBOX_LABELS
            raise HTTPException(409, {
                "error": "toolbox_disabled",
                "toolbox": needs,
                "label": TOOLBOX_LABELS.get(needs, needs),
                "message": f"Enable the {TOOLBOX_LABELS.get(needs, needs)} tools to run this recipe.",
            })
        blocks = _building_blocks()
        from src.recipe_templates import best_model
        model = best_model(blocks["models"])
        graph = recipe_templates.build_graph(template_id, model) if model else None
        if not graph:
            raise HTTPException(400, "No capable model is served — add one in Settings first.")
        body = await request.json() if await _has_body(request) else {}
        run_input = (body or {}).get("input", "")
        return await recipes_engine.run_recipe(graph, run_input, owner=_owner(request))

    @router.post("/toolbox/enable")
    async def enable_toolbox(request: Request):
        """Enable a toolbox and validate that its tools respond (admin only —
        turning on a tool collection is a config change). Ask-first, one click,
        and just as easy to switch back off in the MCP panel."""
        require_admin(request)
        from src.builtin_mcp import TOOLBOX_MCP_IDS
        body = await request.json()
        toolbox = str(body.get("toolbox") or "").strip()
        if toolbox not in TOOLBOX_MCP_IDS:
            raise HTTPException(400, f"unknown toolbox {toolbox!r}")

        from core.database import SessionLocal, McpServer
        import json as _json
        db = SessionLocal()
        try:
            srv = db.query(McpServer).filter(McpServer.id == toolbox).first()
            if not srv:
                raise HTTPException(404, "toolbox not registered")
            srv.is_enabled = True
            db.commit()
            command, args = srv.command, (_json.loads(srv.args) if srv.args else [])
            env = _json.loads(srv.env) if srv.env else {}
            transport, name, url = srv.transport, srv.name, srv.url
        finally:
            db.close()

        from src.tool_utils import get_mcp_manager
        mcp = get_mcp_manager()
        try:
            await mcp.connect_server(server_id=toolbox, name=name, transport=transport,
                                     command=command, args=args, env=env, url=url)
        except Exception as e:
            logger.warning("toolbox %s connect failed: %s", toolbox, e)
        # Validation: the toolbox is only "ready" if its tools are discoverable.
        tools = [t for t in mcp.get_all_tools() if t.get("server_id") == toolbox]
        ok = bool(tools)
        return {
            "ok": ok, "toolbox": toolbox, "tools": len(tools),
            "message": (f"{len(tools)} tools ready." if ok
                        else "Enabled, but the tools didn't respond — check the MCP panel."),
        }

    def _is_admin(request: Request) -> bool:
        try:
            auth_mgr = getattr(request.app.state, "auth_manager", None)
            from src.auth_helpers import get_current_user
            user = get_current_user(request)
            if auth_mgr is None or not getattr(auth_mgr, "is_configured", False):
                return True  # single-user / auth-off → full access
            return bool(user and auth_mgr.is_admin(user))
        except Exception:
            return False

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

    @router.post("/generate")
    async def generate_recipe(request: Request):
        """Draft a recipe graph from a plain-English description (admin)."""
        require_admin(request)
        from src import recipe_authoring
        body = await request.json()
        blocks = _building_blocks()
        return await recipe_authoring.generate_recipe(
            str(body.get("description") or ""), blocks["models"], blocks["tools"],
            owner=_owner(request) or "")

    @router.post("/explain")
    async def explain_recipe(request: Request):
        """Plain-English summary of a recipe graph (admin)."""
        require_admin(request)
        from src import recipe_authoring
        body = await request.json()
        recipe = body.get("recipe") or body
        blocks = _building_blocks()
        return await recipe_authoring.explain_recipe(recipe, blocks["models"], owner=_owner(request) or "")

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
