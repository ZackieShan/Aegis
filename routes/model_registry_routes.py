"""Model registry — the Media Studio panel's inventory view.

Merges three sources into one tagged catalog:
- served aliases from every enabled endpoint's cached_models (what you can
  actually call right now),
- the local models/ drop folder (every .gguf/.safetensors on disk),
- the engine config (llama-swap.yaml), which links aliases to the files they
  serve so files get an accurate "served" badge.

Tags come from src.model_tags (name heuristics). The walk is cheap (~dozens
of files) so GET always reflects the folder live; the UI's Rescan button
additionally hits /api/models?refresh=true to re-probe endpoint model lists.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List

from fastapi import APIRouter, Request

from src import model_tags
from src.auth_helpers import get_current_user, owner_filter

logger = logging.getLogger(__name__)

_MODEL_EXTS = (".gguf", ".safetensors")
_QUOTED_MODEL_PATH_RE = re.compile(r'"([^"]+\.(?:gguf|safetensors))"')


def _models_dir() -> str:
    from core.constants import BASE_DIR
    return os.path.join(BASE_DIR, "models")


def _walk_model_files() -> List[Dict[str, Any]]:
    root = _models_dir()
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.lower().endswith(_MODEL_EXTS):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            out.append({
                "file": rel,
                "name": fn,
                "size_gb": round(size / (1024 ** 3), 2),
                "is_lora": rel.lower().startswith("loras/"),
            })
    out.sort(key=lambda f: f["file"].lower())
    return out


def _engine_alias_files() -> Dict[str, List[str]]:
    """alias -> [file basenames] from llama-swap.yaml (empty if unreadable)."""
    try:
        from src.engine_tuner import _read_config, _model_blocks
        text = _read_config()
        if not text:
            return {}
        return {
            b["name"]: [os.path.basename(p).lower() for p in _QUOTED_MODEL_PATH_RE.findall(b["text"])]
            for b in _model_blocks(text)
        }
    except Exception as e:
        logger.debug("engine config parse failed: %s", e)
        return {}


def _served_aliases(owner) -> List[Dict[str, str]]:
    from src.database import SessionLocal, ModelEndpoint

    out: List[Dict[str, str]] = []
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        for ep in owner_filter(q, ModelEndpoint, owner).all():
            try:
                cached = json.loads(getattr(ep, "cached_models", None) or "[]")
            except Exception:
                cached = []
            for mid in cached:
                out.append({"model": str(mid), "endpoint": getattr(ep, "name", "") or ""})
    finally:
        db.close()
    seen = set()
    return [m for m in out if not (m["model"] in seen or seen.add(m["model"]))]


def setup_model_registry_routes() -> APIRouter:
    router = APIRouter(tags=["model-registry"])

    @router.get("/api/models/registry")
    async def models_registry(request: Request):
        import asyncio as _asyncio

        user = get_current_user(request)
        files = _walk_model_files()
        aliases = _served_aliases(user)
        alias_files = _engine_alias_files()

        # ComfyUI workflows count as served models too (second engine).
        try:
            from src import comfyui_client, comfyui_workflows
            if await _asyncio.to_thread(comfyui_client.is_up):
                _catalogs = (
                    (comfyui_workflows.available_video_models(), comfyui_workflows.COMFY_VIDEO_MODELS),
                    (comfyui_workflows.available_image_models(), comfyui_workflows.COMFY_IMAGE_MODELS),
                )
                for mids, catalog in _catalogs:
                    for mid in mids:
                        if all(a["model"] != mid for a in aliases):
                            aliases.append({"model": mid, "endpoint": "comfyui"})
                            alias_files[mid] = [
                                os.path.basename(f).lower()
                                for f in catalog[mid]["required_files"]
                            ]
        except Exception as e:
            logger.debug("comfyui registry probe failed: %s", e)

        # basename -> aliases that serve it (engine config linkage)
        file_to_aliases: Dict[str, List[str]] = {}
        for alias, basenames in alias_files.items():
            for bn in basenames:
                file_to_aliases.setdefault(bn, []).append(alias)

        served_names = {a["model"] for a in aliases}
        for a in aliases:
            tags = model_tags.classify(a["model"])
            a["files"] = alias_files.get(a["model"], [])
            # Terse aliases lose what their filenames carry ("qwen3.6-27b-neo"
            # vs "...NEO-CODE-HERE..."), so union in tags from the files the
            # engine config says this alias serves. Only for text-model
            # aliases: diffusion/video entries also link their text-encoder
            # LLM, whose chat tags don't describe the pipeline.
            _MEDIA_CAPS = ("text-to-image", "image-to-image", "text-to-video",
                           "image-to-video", "video-to-video")
            if not any(c in tags["capabilities"] for c in _MEDIA_CAPS):
                for bn in a["files"]:
                    ft = model_tags.classify(bn)
                    if model_tags.is_companion(ft) or any(c in ft["capabilities"] for c in _MEDIA_CAPS):
                        continue
                    for t in ft["capabilities"]:
                        if t not in tags["capabilities"]:
                            tags["capabilities"].append(t)
                    for t in ft["best_for"]:
                        if t not in tags["best_for"]:
                            tags["best_for"].append(t)
            a.update(tags)

        for f in files:
            tags = model_tags.classify(f["name"], is_lora=f["is_lora"])
            f.update(tags)
            f.pop("is_lora", None)
            linked = [al for al in file_to_aliases.get(f["name"].lower(), []) if al in served_names]
            f["served_as"] = linked
            f["served"] = bool(linked)

        return {
            "served": aliases,
            "files": files,
            "counts": {
                "served": len(aliases),
                "files": len(files),
                "unserved_files": sum(
                    1 for f in files
                    if not f["served"] and "companion" not in f["capabilities"] and "lora" not in f["capabilities"]
                ),
            },
        }

    return router
