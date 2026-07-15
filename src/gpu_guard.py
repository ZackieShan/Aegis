"""Keep chat from killing renders.

The problem, verified on llama-swap v238: a render fills the 4090 (sd-server sits
at ~23.6GB of 24GB) and llama-swap resolves model contention by *eviction*. Send
a chat message while a video renders and llama-swap stops sd-server to load the
chat model — the render dies mid-job. There is no VRAM left to share, so the only
way chat can stay usable during a render is to not touch the GPU at all.

Two halves make that work, and both are required:

  * engine/llama-swap.yaml puts the CPU model in a group with
    ``exclusive: false`` + ``persistent: true``, so the two may coexist;
  * this module routes chat onto that CPU model while a render is live.

The swap is deliberately narrow. It only fires when the chat model would
*actually* contend — same local engine, GPU busy, fallback really available.
A remote API model touches no VRAM, so swapping it would degrade chat for
nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# The llama-swap alias of the CPU-only entry. Lives in the `cpu` group so it can
# run alongside a render. See engine/llama-swap.yaml.
DEFAULT_FALLBACK_MODEL = "chat-lite-cpu"

# The fallback's context window — must match `-c` on chat-lite-cpu in
# engine/llama-swap.yaml. Callers clamp to this when swapping: the caller's
# context_length is sized for the *GPU* model (the coder model runs a 45K
# window), and handing 45K of messages to a 32K model earns a 400 "request
# exceeds available context size" instead of a reply.
FALLBACK_CONTEXT = 32768

# Setting key: "off" disables the swap entirely (one click, per the product call
# that this should be easy to turn off).
SETTING_ENABLED = "gpu_busy_fallback"
SETTING_MODEL = "gpu_busy_fallback_model"


def _setting(key: str, owner: Optional[str], default: str = "") -> str:
    try:
        from src.settings import get_user_setting, load_settings
        settings = load_settings()
        return (get_user_setting(key, owner or "", settings.get(key, default)) or "").strip()
    except Exception:
        return default


def is_enabled(owner: Optional[str] = None) -> bool:
    """On by default — the failure it prevents (a dead render) is worse than the
    cost when it misfires (a slower reply for a couple of minutes)."""
    return _setting(SETTING_ENABLED, owner, "on").lower() not in ("off", "false", "0", "no")


def fallback_model(owner: Optional[str] = None) -> str:
    return _setting(SETTING_MODEL, owner, DEFAULT_FALLBACK_MODEL) or DEFAULT_FALLBACK_MODEL


def _is_local_engine(endpoint_url: str) -> bool:
    """Whether this endpoint is the local llama-swap — i.e. whether a model load
    here would actually contend with a render.

    Only loopback counts: a remote endpoint's models live on someone else's GPU,
    so evicting is not our problem and swapping would be pure downside.
    """
    u = (endpoint_url or "").lower()
    return "127.0.0.1" in u or "localhost" in u or "0.0.0.0" in u or "::1" in u


def _endpoint_serves(endpoint_url: str, model: str, owner: Optional[str] = None) -> bool:
    """Whether `model` is actually servable on `endpoint_url`.

    Guards against pointing chat at a fallback that isn't configured — better to
    leave the user on their real model (and risk the render) than to hand them a
    404 and no reply at all.
    """
    try:
        import json as _json
        from src.database import ModelEndpoint, SessionLocal
        from src.endpoint_resolver import normalize_base

        target = normalize_base(endpoint_url)
        db = SessionLocal()
        try:
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
            if owner:
                from src.auth_helpers import owner_filter
                q = owner_filter(q, ModelEndpoint, owner)
            for ep in q.all():
                if normalize_base(getattr(ep, "base_url", "") or "") != target:
                    continue
                try:
                    cached = _json.loads(getattr(ep, "cached_models", None) or "[]")
                except Exception:
                    cached = []
                if model in [str(m) for m in cached]:
                    return True
        finally:
            db.close()
    except Exception:
        logger.debug("Fallback availability check failed", exc_info=True)
    return False


def busy_swap(
    model: str,
    endpoint_url: str,
    owner: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Decide whether this chat turn should run on the CPU model instead.

    Returns None to leave the request alone, or a dict with the replacement
    `model` plus a human-readable `notice` and the `job` that is holding the GPU.
    """
    if not is_enabled(owner):
        return None

    busy = None
    try:
        from src import job_queue
        busy = job_queue.gpu_busy()
    except Exception:
        logger.debug("gpu_busy() check failed", exc_info=True)
    if not busy:
        return None

    if not _is_local_engine(endpoint_url):
        return None  # remote model — no contention to avoid

    target = fallback_model(owner)
    if not target or str(model or "") == target:
        return None  # already there

    if not _endpoint_serves(endpoint_url, target, owner=owner):
        logger.warning(
            "GPU is busy (%s) but the fallback model %r is not served on %s — "
            "leaving chat on %r; this request may evict the render.",
            busy.get("kind"), target, endpoint_url, model,
        )
        return None

    kind = str(busy.get("kind") or "render")
    title = str(busy.get("title") or "")
    what = f"{kind} “{title[:40]}”" if title else kind
    return {
        "model": target,
        "original_model": model,
        "context_length": FALLBACK_CONTEXT,
        "notice": f"GPU busy rendering {what} — replying on {target} (CPU) so the render survives.",
        "job": busy,
    }


def clamp_context(context_length: Optional[int], swap: Optional[Dict[str, Any]]) -> Optional[int]:
    """Shrink a caller's context budget to what the fallback can actually take.

    No-op when there's no swap. Without this, agent mode would keep packing to
    the GPU model's window (45K on the coder model) and the 32K CPU model would
    reject the request outright.
    """
    if not swap:
        return context_length
    limit = int(swap.get("context_length") or FALLBACK_CONTEXT)
    if not context_length:
        return limit
    try:
        return min(int(context_length), limit)
    except (TypeError, ValueError):
        return limit
