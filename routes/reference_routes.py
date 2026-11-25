"""Offline reference library + photo identification.

    GET  /api/reference/status            what the library holds
    GET  /api/reference/search?q=…        search articles and manual pages
    GET  /api/reference/asset?p=…         an image out of the library (same origin)
    POST /api/reference/identify          identify the subject of an uploaded photo
    GET  /api/reference/added             documents added after the library was built
    GET  /api/reference/added/{path}      one of them (the PDF itself)
    POST /api/reference/added             add one: {"url": …} or {"file_id": …}   (administrator)
    DELETE /api/reference/added?document= remove one                              (administrator)
    GET  /api/reference/files/{path}      one of the owner's own files (copied into the library folder)
    GET  /api/reference/intake            how taking in new files is getting on
    POST /api/reference/intake            look for new files now and shelve them    (administrator)
    POST /api/reference/shelve            {"book", "key", "shelf"}: move one item   (administrator)

"reference" rather than "library": /api/library and the Library panel already
mean the user's own documents. The library itself is ``services/library``
(Kiwix + the PDF-manual index); identification is ``services/identify``.
"""

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

_MAX_QUERY_CHARS = 300
_ASSET_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/svg+xml")
_ASSET_MAX_BYTES = 8 * 1024 * 1024
# What a response from the image proxy may do if it is ever opened as a page: nothing.
ASSET_POLICY = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


def _plain_name(name: str) -> str:
    """A file name that is safe inside a quoted header value."""
    return "".join(char if char.isascii() and char.isprintable() and char not in '"\\' else "_" for char in name)[:180]


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
    async def reference_search(request: Request, q: str = "", scope: str = "all", count: int = 10,
                               shelf: str = "") -> Dict[str, Any]:
        require_user(request)
        from services import library
        from services.library import shelves

        query = (q or "").strip()[:_MAX_QUERY_CHARS]
        if not query:
            raise HTTPException(400, "q is required")
        if scope not in library.VALID_SCOPES:
            scope = "all"
        count = max(1, min(int(count or 10), 25))
        place = shelves.resolve(shelf[:120]) if shelf else ""
        results = await asyncio.to_thread(library.library_search, query, count, scope, place)
        return {
            "query": query,
            "scope": scope,
            "shelf": place,
            "available": await asyncio.to_thread(library.available),
            "results": [
                {
                    "title": hit.get("title", ""),
                    "url": hit.get("url", ""),
                    "snippet": hit.get("snippet", ""),
                    "source": hit.get("book_title") or hit.get("book") or "",
                    "kind": hit.get("kind", "article"),
                    "page": hit.get("page"),
                    "shelf": hit.get("shelf", ""),
                    "shelf_name": shelves.display(hit.get("shelf", "")),
                    "by": hit.get("by", ""),
                    "dates": hit.get("dates", ""),
                    "form": hit.get("form", ""),
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
        network both object to. Only ``/content/…`` paths and only image
        types pass — no HTML — so nothing from an archive can run script
        here. SVG (formulas and diagrams in the reader) is an image that can
        carry script, so it gets three guards: it is served only when the
        browser says it is loading a picture (``Sec-Fetch-Dest: image``),
        every answer carries a policy that allows nothing and a sandbox, and
        the same policy is set again for this path in ``core/middleware.py``.
        Shown in a page it is a picture; asked for by itself it is refused,
        and where a browser does not say what it is loading, it is inert.
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
        if content_type == "image/svg+xml" and (request.headers.get("sec-fetch-dest") or "image").lower() != "image":
            raise HTTPException(415, "Not an image")
        return Response(
            content=fetched[1],
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=604800", "X-Content-Type-Options": "nosniff",
                     "Content-Security-Policy": ASSET_POLICY, "Vary": "Sec-Fetch-Dest"},
        )

    @router.post("/api/reference/identify")
    async def reference_identify(request: Request) -> Dict[str, Any]:
        """Identify what an uploaded photo shows.

        Body (JSON): ``file_id`` (from /api/upload), optional ``hint`` (where /
        when / anything the user knows), ``session_id`` and ``want`` — the
        correction when the sorting was wrong: ``"text"`` (a label or page:
        read it), ``"describe"`` (just describe it), ``"living"`` (identify
        it as a living thing). Returns the answer as markdown and as a
        ``card``, plus the structured candidates and library sources.
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
        want = str(body.get("want") or "").strip().lower()
        if want not in identify.WANTS:
            raise HTTPException(400, "want is one of: text, describe, living")
        try:
            return await identify.identify_image(info["path"], hint=hint, owner=user or None, want=want)
        except identify.IdentifyError as e:
            raise HTTPException(e.status, str(e))

    # ── documents added after the library was built ──────────────────────

    def _library_on() -> None:
        from services.library import config as library_config
        if not library_config.enabled():
            raise HTTPException(503, "The offline library is switched off (setting library_enabled).")

    @router.get("/api/reference/added")
    async def added_list(request: Request) -> Dict[str, Any]:
        require_user(request)
        _library_on()
        from services.library import additions
        entries = await asyncio.to_thread(additions.list_added)
        return {"documents": [{key: entry.get(key) for key in
                               ("path", "title", "section", "source", "pages", "text_pages", "bytes", "added_at", "ocr", "url")}
                              for entry in entries],
                "sections": additions.sections()}

    @router.get("/api/reference/added/{path:path}")
    async def added_file(request: Request, path: str):
        require_user(request)
        _library_on()
        from services.library import additions
        found = additions.file_for(path)
        if found is None:
            raise HTTPException(404, "No such document")
        return FileResponse(str(found), media_type="application/pdf",
                            headers={"Content-Disposition": f'inline; filename="{found.name}"',
                                     "X-Content-Type-Options": "nosniff"})

    @router.post("/api/reference/added")
    async def added_add(request: Request) -> Dict[str, Any]:
        from core.middleware import require_admin
        require_admin(request)
        _library_on()
        user = require_user(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "JSON body required")
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON object required")
        from services.library import additions
        details = {"section": str(body.get("section") or ""), "title": str(body.get("title") or ""), "added_by": user or ""}
        try:
            if body.get("url"):
                return await asyncio.to_thread(lambda: additions.add_from_url(str(body["url"]), **details))
            file_id = str(body.get("file_id") or "").strip()
            if not file_id:
                raise HTTPException(400, "Give url (a link to a PDF) or file_id (an upload).")
            if upload_handler is None:
                raise HTTPException(503, "uploads are not available")
            info = upload_handler.resolve_upload(file_id, owner=user or None)
            if not info or not info.get("path"):
                raise HTTPException(404, "No such upload")
            details["name"] = str(info.get("original_name") or info.get("name") or "")
            details["source"] = "uploaded: " + details["name"]
            return await asyncio.to_thread(lambda: additions.add_from_file(info["path"], **details))
        except additions.AdditionError as e:
            raise HTTPException(400, str(e))

    @router.delete("/api/reference/added")
    async def added_remove(request: Request, document: str = "") -> Dict[str, Any]:
        from core.middleware import require_admin
        require_admin(request)
        _library_on()
        from services.library import additions
        try:
            return await asyncio.to_thread(additions.remove, document)
        except additions.AdditionError as e:
            raise HTTPException(404, str(e))

    # ── the owner's own files ────────────────────────────────────────────

    @router.get("/api/reference/files/{path:path}")
    async def own_file(request: Request, path: str):
        """One of the files the owner copied into the library folder. Only a
        file the library has read in is ever served — the path cannot reach
        anything else on the machine (``files.file_for``)."""
        require_user(request)
        _library_on()
        from services.library import files
        found = await asyncio.to_thread(files.file_for, path)
        if found is None:
            raise HTTPException(404, "No such document")
        kind = files.kind_of(found.name)
        media_type = files.MEDIA_TYPES.get(kind, "application/octet-stream")
        if kind == "pdf":
            # Shown in the browser's own viewer, like the added documents.
            return FileResponse(str(found), media_type=media_type,
                                headers={"Content-Disposition": f'inline; filename="{_plain_name(found.name)}"',
                                         "X-Content-Type-Options": "nosniff"})
        # Anything else is handed over as a download and can do nothing if a
        # browser opens it anyway (an e-book is a bundle of web pages).
        return FileResponse(str(found), media_type=media_type, filename=found.name,
                            headers={"X-Content-Type-Options": "nosniff",
                                     "Content-Security-Policy": "default-src 'none'; sandbox"})

    @router.get("/api/reference/intake")
    async def intake_status(request: Request) -> Dict[str, Any]:
        require_user(request)
        _library_on()
        from services.library import intake
        return await asyncio.to_thread(intake.status)

    @router.post("/api/reference/intake")
    async def intake_start(request: Request) -> Dict[str, Any]:
        from core.middleware import require_admin
        require_admin(request)
        _library_on()
        from services.library import intake
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        started = intake.start(ask_model=body.get("ask_model") is not False)
        return {"started": started, **(await asyncio.to_thread(intake.status))}

    @router.post("/api/reference/shelve")
    async def shelve_item(request: Request) -> Dict[str, Any]:
        from core.middleware import require_admin
        require_admin(request)
        _library_on()
        from services.library import catalogue
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "JSON body required")
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON object required")
        try:
            return await asyncio.to_thread(catalogue.refile, str(body.get("book") or ""), str(body.get("key") or ""),
                                           str(body.get("shelf") or ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    def _finish_interrupted_ocr() -> None:
        """A scanned document whose reading was cut short by a restart is
        picked up again, a little after start-up."""
        try:
            from services.library import additions, config as library_config
            if library_config.enabled():
                started = additions.resume_ocr()
                if started:
                    logger.info("library: resumed OCR for %d added document(s)", started)
        except Exception as exc:
            logger.warning("library: could not resume OCR: %s", exc)

    import threading
    resume = threading.Timer(45.0, _finish_interrupted_ocr)
    resume.daemon = True
    resume.start()

    return router
