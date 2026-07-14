"""Style preset routes — CRUD for saved generation styles + the active style.

A style preset is the "same look across prompts" unit (see src/style_presets):
model bindings, prompt affixes, negative prompt, locked seed, steps/cfg, size
and LoRA tags. The active style is a per-user pref (key ``media_style``) that
generation paths resolve server-side, so setting it once styles every image
and video gen from any surface.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from src import style_presets
from src.auth_helpers import get_current_user, require_privilege

logger = logging.getLogger(__name__)


def setup_styles_routes() -> APIRouter:
    router = APIRouter(tags=["styles"])

    @router.get("/api/styles")
    async def styles_list(request: Request):
        user = get_current_user(request)
        return {
            "styles": style_presets.list_styles(),
            "active": style_presets.active_style_name(user),
        }

    @router.post("/api/styles")
    async def styles_save(request: Request):
        user = require_privilege(request, "can_generate_images")
        body = await request.json()
        try:
            record = style_presets.save_style(body, owner=user)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"style": record}

    @router.delete("/api/styles/{name_or_id}")
    async def styles_delete(request: Request, name_or_id: str):
        user = get_current_user(request)
        style = style_presets.get_style(name_or_id)
        if not style:
            raise HTTPException(404, "Unknown style")
        require_privilege(request, "can_generate_images")
        if not style_presets.delete_style(name_or_id):
            raise HTTPException(404, "Unknown style")
        # A deleted style must not linger as anyone's active pref; clearing
        # only the caller's is best-effort (single-box app).
        if style_presets.active_style_name(user) in (style.get("id"), style.get("name")):
            _set_active(user, "")
        return {"ok": True}

    def _set_active(user, name: str):
        from routes.prefs_routes import _load_for_user, _save_for_user
        prefs = _load_for_user(user)
        prefs["media_style"] = name
        _save_for_user(user, prefs)

    @router.post("/api/styles/active")
    async def styles_set_active(request: Request):
        user = get_current_user(request)
        body = await request.json()
        name = str(body.get("name") or "").strip()
        if name:
            style = style_presets.get_style(name)
            if not style:
                raise HTTPException(404, f"Unknown style '{name}'")
            name = style["id"]
        _set_active(user, name)
        return {"active": name}

    return router
