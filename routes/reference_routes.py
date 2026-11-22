"""Offline reference library + photo identification.

    GET  /api/reference/status            what the library holds
    GET  /api/reference/search?q=…        search articles and manual pages
    GET  /api/reference/asset?p=…         an image out of the library (same origin)
    POST /api/reference/identify          identify the subject of an uploaded photo

"reference" rather than "library": /api/library and the Library panel already
mean the user's own documents. The library itself is ``services/library``
(Kiwix + the PDF-manual index); identification is ``services/identify``.
"""

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

_MAX_QUERY_CHARS = 300
_ASSET_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
_ASSET_MAX_BYTES = 8 * 1024 * 1024


def setup_reference_routes(upload_handler=None) -> APIRouter:
    router = APIRouter(tags=["reference"])

    @router.get("/api/reference/status")
    async def reference_status(request: Request) -> Dict[str, Any]:
        require_user(request)
        from services import library

        info = await asyncio.to_thread(library.status)
        info["books"] = [
            {
                "id": book["id"], "title": book["title"], "summary": book["summary"],
                "articles": book["articles"], "date": book["date"], "fulltext": book["fulltext"],
            }
            for book in info.get("books", [])
        ]
        info.pop("library_dir", None)
        return info

    @router.get("/api/reference/search")
    async def reference_search(request: Request, q: str = "", scope: str = "all", count: int = 10) -> Dict[str, Any]:
        require_user(request)
        from services import library

        query = (q or "").strip()[:_MAX_QUERY_CHARS]
        if not query:
            raise HTTPException(400, "q is required")
        if scope not in library.VALID_SCOPES:
            scope = "all"
        count = max(1, min(int(count or 10), 25))
        results = await asyncio.to_thread(library.library_search, query, count, scope)
        return {
            "query": query,
            "scope": scope,
            "available": await asyncio.to_thread(library.available),
            "results": [
                {
                    "title": hit.get("title", ""),
                    "url": hit.get("url", ""),
                    "snippet": hit.get("snippet", ""),
                    "source": hit.get("book_title") or hit.get("book") or "",
                    "kind": hit.get("kind", "article"),
                    "page": hit.get("page"),
                }
                for hit in results
            ],
        }

    @router.get("/api/reference/asset")
    async def reference_asset(request: Request, p: str = ""):
        """Serve one raster image from the library under Aegis's own origin.

        Identification answers show reference photos next to the user's. The
        library lives on another origin (and, for the server itself, on
        loopback), which the page's Content-Security-Policy and a phone's
        network both object to. Only ``/content/…`` paths and only raster
        image types pass — no HTML, no SVG — so nothing from an archive can
        run script here.
        """
        require_user(request)
        from services.library import config as library_config, kiwix

        path = (p or "").split("#", 1)[0].split("?", 1)[0]
        if not path.startswith("/content/") or "/../" in path or len(path) > 1024:
            raise HTTPException(400, "p must be a /content/ path")
        fetched = await asyncio.to_thread(kiwix.fetch, library_config.kiwix_url() + path, _ASSET_MAX_BYTES)
        if not fetched:
            raise HTTPException(404, "Not in the library")
        content_type = fetched[0].split(";", 1)[0].strip().lower()
        if content_type not in _ASSET_TYPES:
            raise HTTPException(415, "Not an image")
        return Response(
            content=fetched[1],
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=604800", "X-Content-Type-Options": "nosniff"},
        )

    @router.post("/api/reference/identify")
    async def reference_identify(request: Request) -> Dict[str, Any]:
        """Identify what an uploaded photo shows.

        Body (JSON): ``file_id`` (from /api/upload), optional ``hint`` (where /
        when / anything the user knows) and ``session_id``. Returns the
        written-up identification as markdown plus the structured candidates
        and library sources.
        """
        user = require_user(request)
        from services import identify

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "JSON body required")
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON object required")
        file_id = str(body.get("file_id") or "").strip()
        hint = str(body.get("hint") or "").strip()[:500]
        if not file_id:
            raise HTTPException(400, "file_id is required (upload the photo with /api/upload first)")
        if upload_handler is None:
            raise HTTPException(503, "uploads are not available")
        info = upload_handler.resolve_upload(file_id, owner=user or None)
        if not info or not info.get("path"):
            raise HTTPException(404, "No such upload")
        mime = str(info.get("mime") or "")
        if not mime.startswith("image/"):
            raise HTTPException(400, "That upload is not an image")
        try:
            return await identify.identify_image(info["path"], hint=hint, owner=user or None)
        except identify.IdentifyError as e:
            raise HTTPException(e.status, str(e))

    return router
