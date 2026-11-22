"""Settings for the offline reference library.

Three knobs, all optional (see ``DEFAULT_SETTINGS`` in ``src/settings.py``):

``library_enabled``   master switch (default on; harmless when nothing is installed)
``kiwix_url``         where Aegis itself reaches ``kiwix-serve``
``kiwix_public_url``  base used in links shown to the user — the address a
                      phone on the tailnet/LAN can open. Empty = ``kiwix_url``.

``AEGIS_KIWIX_URL`` / ``AEGIS_LIBRARY_DIR`` override the defaults from the
environment (settings.json still wins when it holds a value).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_KIWIX_URL = "http://127.0.0.1:8095"
DEFAULT_LIBRARY_DIR = "~/library"


def _settings() -> dict:
    try:
        from src.settings import load_settings
        return load_settings() or {}
    except Exception:
        return {}


def enabled() -> bool:
    """Master switch. ``AEGIS_LIBRARY_DISABLED=1`` wins over settings — the
    test suite sets it so no test ever talks to a real kiwix-serve."""
    if os.environ.get("AEGIS_LIBRARY_DISABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    return bool(_settings().get("library_enabled", True))


def kiwix_url() -> str:
    """Base URL Aegis uses to talk to kiwix-serve (no trailing slash)."""
    value = (
        str(_settings().get("kiwix_url") or "").strip()
        or os.environ.get("AEGIS_KIWIX_URL", "").strip()
        or DEFAULT_KIWIX_URL
    )
    return value.rstrip("/")


def kiwix_public_url() -> str:
    """Base URL for links handed to the user (no trailing slash)."""
    value = str(_settings().get("kiwix_public_url") or "").strip()
    return value.rstrip("/") or kiwix_url()


def library_dir() -> Path:
    value = (
        str(_settings().get("library_dir") or "").strip()
        or os.environ.get("AEGIS_LIBRARY_DIR", "").strip()
        or DEFAULT_LIBRARY_DIR
    )
    return Path(value).expanduser()


def zim_dir() -> Path:
    return library_dir() / "zim"


def manuals_db_path() -> Path:
    return library_dir() / "index" / "manuals.db"
