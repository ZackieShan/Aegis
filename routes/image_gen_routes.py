"""Direct image generation route — drives the /image slash command.

Thin wrapper over ai_interaction.do_generate_image (the single image-gen
implementation) that exposes the style-consistency knobs: model, size,
quality, seed, negative prompt, steps, cfg and style preset. Lives under
/api/image/* so it inherits the request-timeout exemption local diffusion
needs (a cold model load alone can exceed the 45s middleware cap).
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import get_current_user, require_privilege

logger = logging.getLogger(__name__)


def list_image_models(owner=None):
    """Image-capable model ids across enabled endpoints (from cached_models),
    mirroring video_generation.list_video_models. Video pipelines are
    excluded — wan/ltx ids also match the broad image regex."""
    from routes.gallery.gallery_routes import _IMAGE_MODEL_ID_RE
    from src.video_generation import is_video_model
    from src.database import SessionLocal, ModelEndpoint
    from src.auth_helpers import owner_filter

    out = []
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        for ep in owner_filter(q, ModelEndpoint, owner).all():
            try:
                cached = json.loads(getattr(ep, "cached_models", None) or "[]")
            except Exception:
                cached = []
            for mid in cached:
                if _IMAGE_MODEL_ID_RE.search(str(mid)) and not is_video_model(mid):
                    out.append({"model": str(mid), "endpoint": getattr(ep, "name", "") or ""})
    finally:
        db.close()
    seen = set()
    return [m for m in out if not (m["model"] in seen or seen.add(m["model"]))]


async def _comfy_generate(body, user, model, prompt, style, _num):
    """Render via a ComfyUI image workflow; mirrors do_generate_image's result
    shape (image_url/image_id/...) so the /image command needs no special
    casing. Style affixes apply without <lora:> tags — ComfyUI graphs don't
    parse them (that's an sd-server/A1111 prompt convention)."""
    import base64
    import uuid as _uuid
    from pathlib import Path
    from src import comfyui_client, style_presets
    from src.constants import GENERATED_IMAGES_DIR

    styled = style_presets.styled_prompt(style, prompt, with_loras=False)
    seed = _num("seed", int)
    if seed is None and style and style.get("seed") is not None:
        seed = int(style["seed"])
    steps = _num("steps", int)
    if steps is None and style and style.get("steps") is not None:
        steps = int(style["steps"])
    size = str(body.get("size") or "").strip() or (style or {}).get("size") or "1024x1024"
    try:
        w, h = (int(x) for x in size.split("x"))
    except (TypeError, ValueError):
        w = h = 1024
    negative = str(body.get("negative_prompt") or "") or (style or {}).get("negative_prompt", "")

    result = await comfyui_client.render_image(
        model, prompt=styled, negative_prompt=negative,
        width=w, height=h, seed=seed, steps=steps,
    )
    if result.get("error"):
        return {"error": result["error"]}

    out_dir = Path(GENERATED_IMAGES_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_uuid.uuid4().hex[:12]}.png"
    (out_dir / filename).write_bytes(base64.b64decode(result["b64_json"]))

    image_id = ""
    try:
        from src.database import SessionLocal, GalleryImage
        image_id = str(_uuid.uuid4())
        db = SessionLocal()
        db.add(GalleryImage(
            id=image_id, filename=filename, prompt=styled, model=model,
            size=f"{w}x{h}", quality="comfyui",
            session_id=body.get("session_id"), owner=user,
        ))
        db.commit()
        db.close()
    except Exception as e:
        logger.warning("comfy gallery record failed: %s", e)
        image_id = ""

    out = {
        "results": f"Generated image for: {styled[:100]}",
        "image_url": f"/api/generated-image/{filename}",
        "image_id": image_id or None,
        "image_prompt": styled,
        "image_model": model,
        "image_size": f"{w}x{h}",
        "image_quality": "comfyui",
        "image_seed": result.get("seed"),
    }
    if style:
        out["image_style"] = style.get("id")
    return out


def setup_image_gen_routes() -> APIRouter:
    router = APIRouter(tags=["image-gen"])

    async def _comfy_image_models() -> list:
        import asyncio
        from src import comfyui_client, comfyui_workflows
        try:
            if not await asyncio.to_thread(comfyui_client.is_up):
                return []
            return [{"model": m, "endpoint": "comfyui"} for m in comfyui_workflows.available_image_models()]
        except Exception:
            return []

    @router.get("/api/image/models")
    async def image_models(request: Request):
        user = get_current_user(request)
        return {"models": list_image_models(user) + await _comfy_image_models()}

    @router.post("/api/image/enhance-prompt")
    async def enhance_prompt(request: Request):
        """Rewrite rough intent into a diffusion-ready scene description.

        Diffusion models depict, they don't obey: "generate a movie trailer in
        the style of Ghibli" produces a painting OF a screen playing a movie
        (observed, literally). This turns intent into what the model actually
        needs — subject, setting, light, camera, motion — using the local
        utility model, so the Studio's ✨ button costs no cloud call.
        """
        user = require_privilege(request, "can_generate_images")
        body = await request.json()
        rough = str(body.get("prompt") or "").strip()
        if not rough:
            raise HTTPException(400, "Prompt is required")
        # The rewrite targets 40-80 words; anything past a couple thousand chars
        # is pasted noise that only pads the utility model's context.
        rough = rough[:2000]
        kind = "video" if str(body.get("kind") or "").lower() == "video" else "image"

        from src.endpoint_resolver import resolve_endpoint
        url, model, headers = resolve_endpoint("utility", owner=user or None)
        if not url or not model:
            url, model, headers = resolve_endpoint("default", owner=user or None)
        if not url or not model:
            return {"ok": False, "error": "No utility model configured"}

        motion = (
            " Describe the motion: what moves, how fast, and how the camera moves."
            if kind == "video" else ""
        )
        system = (
            "You write prompts for local diffusion models. Rewrite the user's rough "
            "idea into ONE vivid scene description: concrete subject, setting, "
            "lighting, mood, camera framing, and art style." + motion + " Never use "
            "instruction words (generate, create, make, show me) or meta terms like "
            "'movie trailer', 'poster', or 'screenshot' — the model draws those "
            "literally instead of obeying them. 40-80 words. Output ONLY the prompt "
            "text, no quotes, no preamble."
        )
        try:
            from src.llm_core import llm_call_async
            text = await llm_call_async(
                url, model,
                [{"role": "system", "content": system},
                 {"role": "user", "content": rough}],
                headers=headers, temperature=0.7, max_tokens=220, timeout=60,
            )
        except Exception as e:
            logger.warning("Prompt enhance failed: %s", e)
            return {"ok": False, "error": f"Enhance failed: {e}"}

        cleaned = str(text or "").strip().strip('"').strip()
        if not cleaned:
            return {"ok": False, "error": "The model returned nothing"}
        return {"ok": True, "prompt": cleaned, "model": model}

    @router.post("/api/image/generate")
    async def image_generate(request: Request):
        user = require_privilege(request, "can_generate_images")
        body = await request.json()
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(400, "Prompt is required")

        def _num(key, cast):
            v = body.get(key)
            if v in (None, ""):
                return None
            try:
                return cast(v)
            except (TypeError, ValueError):
                return None

        # ComfyUI-served image workflows (e.g. flux2-klein) render through the
        # second engine; everything else goes through do_generate_image.
        from src import comfyui_workflows, style_presets
        model = str(body.get("model") or "").strip()
        style = style_presets.resolve_style(str(body.get("style") or "") or None, user)
        if not model and style and style.get("image_model") in comfyui_workflows.COMFY_IMAGE_MODELS:
            model = style["image_model"]
        if model in comfyui_workflows.COMFY_IMAGE_MODELS:
            return await _comfy_generate(body, user, model, prompt, style, _num)

        # do_generate_image's positional line format: prompt/model/size/quality
        content = "\n".join([
            prompt.replace("\n", " "),
            str(body.get("model") or "").strip(),
            str(body.get("size") or "").strip(),
            str(body.get("quality") or "").strip() or "medium",
        ])

        from src.ai_interaction import do_generate_image
        result = await do_generate_image(
            content,
            session_id=body.get("session_id"),
            owner=user,
            seed=_num("seed", int),
            negative_prompt=str(body.get("negative_prompt") or "") or None,
            steps=_num("steps", int),
            cfg_scale=_num("cfg_scale", float),
            style=str(body.get("style") or "") or None,
        )
        # Errors surface in-band ({"error": ...}) like the gallery proxies do —
        # the slash command renders them inline.
        return result

    return router
