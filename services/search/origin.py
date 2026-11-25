"""Where a source came from, so an answer can say so in house words: the home library, or the web."""

from __future__ import annotations

from typing import Any, List

LIBRARY, WEB = "library", "web"


def kind_of(url: Any) -> str:
    """``"library"`` for a page of the offline library or a document kept on this server, else ``"web"``."""
    address = str(url or "")
    if address.startswith(("/api/reference/", "/read?", "/library/")):
        return LIBRARY
    try:
        from services.library import kiwix
        if kiwix.owns_url(address):
            return LIBRARY
    except Exception:
        pass
    return WEB


def marked(sources: Any, default: str = "") -> List[dict]:
    """The sources with ``kind`` on each (``default`` when the tool that found them only looks in one place)."""
    out = []
    for source in sources if isinstance(sources, list) else []:
        if isinstance(source, dict):
            out.append({**source, "kind": default or source.get("kind") or kind_of(source.get("url"))})
    return out
