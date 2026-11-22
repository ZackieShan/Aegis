"""Client for the species-ID engine (BioCLIP behind llama-swap).

The engine is optional: when it is not installed or not answering,
:func:`classify` returns None and identification falls back to the vision
model alone. See ``scripts/engine-templates/speciesid-server.py``.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_ALIAS = "species-id"


def _settings() -> dict:
    try:
        from src.settings import load_settings
        return load_settings() or {}
    except Exception:
        return {}


def enabled() -> bool:
    return bool(_settings().get("species_id_enabled", True))


def _endpoint() -> str:
    alias = str(_settings().get("species_id_model") or DEFAULT_ALIAS).strip() or DEFAULT_ALIAS
    root = f"http://127.0.0.1:{os.getenv('LLAMA_SWAP_PORT', '9090')}"
    return f"{root}/upstream/{alias}/classify"


def classify(image_jpeg: bytes, k: int = 5, timeout: float = 150.0) -> Optional[dict]:
    """Top-``k`` species / genera / families for a photo, or None.

    The first call after idle pays the model load (~10 s); the timeout covers
    that. Blocking — call it from a worker thread."""
    if not enabled():
        return None
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=3.0), trust_env=False) as client:
            response = client.post(
                _endpoint(),
                files={"file": ("photo.jpg", image_jpeg, "image/jpeg")},
                data={"k": str(k)},
            )
        if response.status_code != 200:
            logger.info("species-id engine answered HTTP %s — continuing without it", response.status_code)
            return None
        data = response.json()
    except Exception as exc:
        logger.info("species-id engine unavailable (%s) — continuing without it", type(exc).__name__)
        return None
    if not isinstance(data, dict) or not data.get("species"):
        return None
    return data


def strength(score: float) -> str:
    """Plain-language reading of a classifier score for one photo."""
    if score >= 0.6:
        return "strong"
    if score >= 0.25:
        return "moderate"
    if score >= 0.08:
        return "weak"
    return "very weak"


def summarize(result: Optional[dict], k: int = 5) -> str:
    """The candidates as the few lines the vision model reads."""
    if not result:
        return ""

    def line(rows: List[dict], key: str) -> str:
        parts = []
        for row in rows[:k]:
            name = row.get(key) or ""
            if not name:
                continue
            common = row.get("common_name") if key == "species" else ""
            label = f"{name} ({common})" if common else name
            parts.append(f"{label} {float(row.get('score') or 0):.2f}")
        return "; ".join(parts)

    lines = []
    for label, key in (("Species", "species"), ("Genus", "genus"), ("Family", "family")):
        text = line(result.get(key) or [], key)
        if text:
            lines.append(f"{label}: {text}")
    return "\n".join(lines)
