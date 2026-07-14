"""Style presets — one saved "look" applied across many image/video prompts.

A preset bundles everything that makes generations match: the model to use
(image and/or video), prompt prefix/suffix, negative prompt, a locked seed,
sampler steps / CFG, a default size, and LoRA tags. Generation paths resolve
the preset server-side (explicit ``style=`` first, then the caller's active
style from prefs), so chat-agent gens, /image, /video and the gallery editor
all pick up the same look without each UI reimplementing it.

Storage mirrors recipes: one JSON file per preset under STYLES_DIR, addressed
by a filesystem-safe id derived from the name.
"""

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from src.constants import STYLES_DIR

# Fields a caller may set; anything else is dropped on save.
_FIELDS = (
    "name", "description",
    "image_model", "video_model",
    "prompt_prefix", "prompt_suffix", "negative_prompt",
    "seed", "steps", "cfg_scale", "size",
    "loras",
)

_LORA_NAME_RE = re.compile(r"^[\w][\w .()\-]*$")


def _ensure_dir() -> None:
    os.makedirs(STYLES_DIR, exist_ok=True)


def style_id(name: str) -> str:
    """Filesystem-safe id from a preset name ("Neon Noir" -> "neon-noir")."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")
    if not slug:
        raise ValueError("invalid style name")
    return slug[:64]


def _path(sid: str) -> str:
    return os.path.join(STYLES_DIR, f"{style_id(sid)}.json")


def list_styles() -> List[Dict[str, Any]]:
    _ensure_dir()
    out = []
    for fn in sorted(os.listdir(STYLES_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(STYLES_DIR, fn), encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            continue
    out.sort(key=lambda s: s.get("updated") or 0, reverse=True)
    return out


def get_style(name_or_id: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_path(name_or_id), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def _clean_loras(raw) -> List[Dict[str, Any]]:
    """[{name, weight}] with names safe to splice into a <lora:...> tag."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            name, _, w = item.partition(":")
            item = {"name": name, "weight": w or 1.0}
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or not _LORA_NAME_RE.fullmatch(name):
            continue
        try:
            weight = round(float(item.get("weight", 1.0)), 3)
        except (TypeError, ValueError):
            weight = 1.0
        out.append({"name": name, "weight": max(-2.0, min(2.0, weight))})
    return out[:8]


def _clean_int(v, lo, hi) -> Optional[int]:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return None


def save_style(data: Dict[str, Any], owner: Optional[str] = None) -> Dict[str, Any]:
    _ensure_dir()
    name = str(data.get("name") or "").strip()[:80]
    sid = style_id(name)  # raises ValueError on an unusable name
    existing = get_style(sid)

    record: Dict[str, Any] = {
        "id": sid,
        "name": name,
        "owner": (existing or {}).get("owner", owner),
        "created": (existing or {}).get("created", time.time()),
        "updated": time.time(),
    }
    for key in _FIELDS:
        if key == "name":
            continue
        val = data.get(key, (existing or {}).get(key))
        if val in (None, ""):
            continue
        if key == "loras":
            val = _clean_loras(val)
            if not val:
                continue
        elif key == "seed":
            val = _clean_int(val, 0, 2**31 - 1)
        elif key == "steps":
            val = _clean_int(val, 1, 150)
        elif key == "cfg_scale":
            try:
                val = max(0.0, min(30.0, float(val)))
            except (TypeError, ValueError):
                val = None
        elif key == "size":
            if not re.fullmatch(r"\d{2,4}x\d{2,4}", str(val)):
                val = None
        else:
            val = str(val)[:2000]
        if val is None:
            continue
        record[key] = val

    tmp = _path(sid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    os.replace(tmp, _path(sid))
    return record


def delete_style(name_or_id: str) -> bool:
    try:
        os.remove(_path(name_or_id))
        return True
    except (FileNotFoundError, ValueError):
        return False


# ── resolution + application ─────────────────────────────────────────────────

def active_style_name(owner: Optional[str]) -> str:
    """The caller's active style.

    Reads the same prefs slot /api/styles/active writes — including the
    anonymous slot when auth is off (get_user_setting skips prefs entirely
    for a falsy owner, which silently lost single-user activations). A pref
    key that is present-but-empty means "explicitly off"; only a never-set
    pref falls through to the global setting.
    """
    try:
        from routes.prefs_routes import _load_for_user
        prefs = _load_for_user(owner or None) or {}
        if "media_style" in prefs:
            return str(prefs.get("media_style") or "").strip()
    except Exception:
        pass
    try:
        from src.settings import get_setting
        return str(get_setting("media_style", "") or "").strip()
    except Exception:
        return ""


def resolve_style(explicit: Optional[str], owner: Optional[str]) -> Optional[Dict[str, Any]]:
    """Preset for a generation call: explicit ``style=`` wins, "none"/"off"
    disables, otherwise the caller's active style applies."""
    name = (explicit or "").strip()
    if name.lower() in ("none", "off"):
        return None
    if not name:
        name = active_style_name(owner)
    if not name:
        return None
    try:
        return get_style(name)
    except Exception:
        return None


def lora_tags(style: Dict[str, Any]) -> str:
    return "".join(
        f" <lora:{l['name']}:{l['weight']}>" for l in style.get("loras") or []
    )


def styled_prompt(style: Optional[Dict[str, Any]], prompt: str, with_loras: bool = True) -> str:
    """prefix + prompt + suffix (+ LoRA tags for sd-server backends)."""
    if not style:
        return prompt
    parts = [style.get("prompt_prefix", "").strip(), prompt.strip(), style.get("prompt_suffix", "").strip()]
    out = ", ".join(p for p in parts if p)
    if with_loras:
        out += lora_tags(style)
    return out
