"""The reader and the kitchen.

    GET    /api/reader/where                       which links are library links (for pages that show them)
    GET    /api/reader/shelf                       kept, recent, latest highlights
    GET    /api/reader/open?u=…|b=…&p=…[&page=N]   open something from the library
    GET    /api/reader/items/{id}/pages?first=N    more pages of a document
    GET    /api/reader/items/{id}/pages/{n}/image  a page as a picture
    PATCH  /api/reader/items/{id}                  remember the place, keep it on the shelf
    DELETE /api/reader/items/{id}                  take it off the shelf (with its highlights)
    GET    /api/reader/marks[?item=…]              highlights
    GET    /api/reader/marks/export[?item=…]       … as a text file
    POST   /api/reader/items/{id}/marks            highlight a passage
    PATCH  /api/reader/marks/{id}                  its note, its colour
    DELETE /api/reader/marks/{id}
    POST   /api/reader/marks/{id}/remember         hand it to Aegis's memory
    GET    /api/reader/collections                 what the library holds
    GET    /api/reader/collections/{id}/documents  the documents inside one
    GET    /api/reader/shelves                     the library by shelf: categories, shelves, how much is on each
    GET    /api/reader/shelves/contents?shelf=…    what is on one: collections, books, documents (q=, offset=)
    GET    /api/reader/find?q=…                    books and documents by title or author

    GET    /api/kitchen/recipes                    the cookbook
    POST   /api/kitchen/recipes                    set a recipe out: {"text": …} or {"url": …}
    GET    /api/kitchen/recipes/{id}
    PATCH  /api/kitchen/recipes/{id}               title, favourite, year
    POST   /api/kitchen/recipes/{id}/redo          set it out again from the original
    DELETE /api/kitchen/recipes/{id}

Everything is the signed-in person's own. The work is in ``services/reader``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from services.reader import ReaderError, content, shelf
from services.reader import kitchen
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)


def setup_reader_routes(memory_manager=None) -> APIRouter:
    router = APIRouter(tags=["reader"])

    def _library_on() -> None:
        from services.library import config
        if not config.enabled():
            raise HTTPException(404, "The offline library is switched off.")

    async def _do(work, *args, **kwargs):
        try:
            return await asyncio.to_thread(work, *args, **kwargs)
        except ReaderError as exc:
            raise HTTPException(400, str(exc))

    # ── the shelf ────────────────────────────────────────────────────────

    @router.get("/api/reader/where")
    async def reader_where(request: Request) -> Dict[str, Any]:
        """What a library link looks like, so that a page showing one (a chat
        reply citing its source) can open it in the reader."""
        require_user(request)
        from services.library import config
        on = config.enabled()
        return {"library": on, "base": config.kiwix_public_url() if on else "", "added": config.ADDED_URL_PREFIX,
                "files": config.FILES_URL_PREFIX}

    @router.get("/api/reader/shelf")
    async def reader_shelf(request: Request) -> Dict[str, Any]:
        owner = require_user(request)
        from services.library import config
        result = await _do(shelf.shelf, owner)
        result["library"] = config.enabled()
        return result

    @router.get("/api/reader/open")
    async def reader_open(request: Request, u: str = "", b: str = "", p: str = "", page: int = 0) -> Dict[str, Any]:
        owner = require_user(request)
        _library_on()
        if len(u) > 2000 or len(p) > 1000 or len(b) > 200:
            raise HTTPException(400, "That link is too long.")
        try:
            return await asyncio.to_thread(shelf.open_document, owner, url=u, book=b, path=p, page=page)
        except ReaderError as exc:
            # Not everything in the library can be shown here (a menu drawn by
            # script, a document that was never indexed): say why, and where
            # the original is.
            return {"problem": str(exc), "original": await asyncio.to_thread(content.original_for, u, b, p, page)}

    @router.get("/api/reader/items/{item_id}/pages")
    async def reader_pages(request: Request, item_id: str, first: int = 1, count: int = content.PAGES_PER_REQUEST) -> Dict[str, Any]:
        owner = require_user(request)
        return await _do(shelf.more_pages, owner, item_id, first, count)

    @router.get("/api/reader/items/{item_id}/pages/{number}/image")
    async def reader_page_image(request: Request, item_id: str, number: int) -> Response:
        owner = require_user(request)
        item = await _do(shelf.get_item, owner, item_id)
        if item["kind"] != "manual":
            raise HTTPException(400, "That is not a document with pages.")
        picture = await _do(content.page_image, item["book"], item["path"], number)
        return Response(content=picture, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})

    @router.patch("/api/reader/items/{item_id}")
    async def reader_item_update(request: Request, item_id: str, body: dict = Body(...)) -> Dict[str, Any]:
        owner = require_user(request)
        return await _do(shelf.update_item, owner, item_id, body if isinstance(body, dict) else {})

    @router.delete("/api/reader/items/{item_id}")
    async def reader_item_remove(request: Request, item_id: str) -> Dict[str, Any]:
        owner = require_user(request)
        if not await _do(shelf.remove_item, owner, item_id):
            raise HTTPException(404, "That is not on your shelf.")
        return {"ok": True}

    # ── highlights ───────────────────────────────────────────────────────

    @router.get("/api/reader/marks")
    async def reader_marks(request: Request, item: str = "") -> Dict[str, Any]:
        owner = require_user(request)
        return {"marks": await _do(shelf.all_marks, owner, item)}

    @router.get("/api/reader/marks/export")
    async def reader_marks_export(request: Request, item: str = "") -> PlainTextResponse:
        owner = require_user(request)
        text = await _do(shelf.as_markdown, owner, item)
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": 'attachment; filename="highlights.md"'})

    @router.post("/api/reader/items/{item_id}/marks")
    async def reader_mark_add(request: Request, item_id: str, body: dict = Body(...)) -> Dict[str, Any]:
        owner = require_user(request)
        return await _do(shelf.add_mark, owner, item_id, body if isinstance(body, dict) else {})

    @router.patch("/api/reader/marks/{mark_id}")
    async def reader_mark_update(request: Request, mark_id: str, body: dict = Body(...)) -> Dict[str, Any]:
        owner = require_user(request)
        return await _do(shelf.update_mark, owner, mark_id, body if isinstance(body, dict) else {})

    @router.delete("/api/reader/marks/{mark_id}")
    async def reader_mark_remove(request: Request, mark_id: str) -> Dict[str, Any]:
        owner = require_user(request)
        if not await _do(shelf.remove_mark, owner, mark_id):
            raise HTTPException(404, "That highlight is gone.")
        return {"ok": True}

    @router.post("/api/reader/marks/{mark_id}/remember")
    async def reader_mark_remember(request: Request, mark_id: str, body: Optional[dict] = Body(None)) -> Dict[str, Any]:
        """Keep a highlight in the person's memory, as a note in a topic."""
        owner = require_user(request)
        if memory_manager is None:
            raise HTTPException(503, "Memory is not available.")
        topic = str((body or {}).get("topic") or "")[:60]
        return await _do(shelf.share_mark, owner, mark_id, memory_manager, topic)

    # ── what there is to read ────────────────────────────────────────────

    @router.get("/api/reader/collections")
    async def reader_collections(request: Request) -> Dict[str, Any]:
        require_user(request)
        _library_on()
        from services.library import config, kiwix, manuals

        books = await asyncio.to_thread(kiwix.list_books)
        counts = await asyncio.to_thread(manuals.document_counts)
        listed = [{"id": book["id"], "title": book["title"], "summary": book["summary"], "articles": book["articles"],
                   "documents": counts.get(book["id"], 0)} for book in books]
        own = ((config.FILES_BOOK, "Your own files", "Documents and books copied into the library folder"),
               (config.ADDED_BOOK, "Added to the library", "Documents added after the library was built"))
        for book_id, title, summary in own:
            if counts.get(book_id):
                listed.insert(0, {"id": book_id, "title": title, "summary": summary, "articles": 0, "documents": counts[book_id]})
        return {"collections": sorted(listed, key=lambda c: (c["id"] not in config.LOCAL_BOOKS, c["id"] != config.ADDED_BOOK, c["title"].lower())),
                "available": await asyncio.to_thread(kiwix.available)}

    @router.get("/api/reader/collections/{book}/documents")
    async def reader_collection_documents(request: Request, book: str, q: str = "", offset: int = 0) -> Dict[str, Any]:
        require_user(request)
        _library_on()
        from services.library import manuals
        return await asyncio.to_thread(manuals.documents, book, q[:120], offset, 60)

    # ── the library by shelf ─────────────────────────────────────────────

    def _item_view(item: Dict[str, Any]) -> Dict[str, Any]:
        from services.library import catalogue, shelves
        return {"title": item["title"], "author": item["author"], "dates": item["dates"], "form": item["form"],
                "note": item["note"], "shelf": item["shelf"], "shelf_name": shelves.display(item["shelf"]),
                "book": item["book"], "key": item["key"], "url": catalogue.link(item["book"], item["key"]),
                "kept": item.get("kept", True)}

    @router.get("/api/reader/shelves")
    async def reader_shelves(request: Request) -> Dict[str, Any]:
        """The whole tree with how much is on every shelf."""
        require_user(request)
        _library_on()
        from services.library import catalogue, kiwix

        def work() -> Dict[str, Any]:
            up = kiwix.available()
            books = kiwix.list_books() if up else []
            names = {book["id"]: book["title"] for book in books}
            tree = catalogue.overview(books)
            for category in tree:
                category["collections"] = [{"id": book, "title": names.get(book, book)} for book in category["collections"]]
                for shelf in category["shelves"]:
                    shelf["collections"] = [{"id": book, "title": names.get(book, book)} for book in shelf["collections"]]
            return {"categories": tree, "available": up}

        return await asyncio.to_thread(work)

    @router.get("/api/reader/shelves/contents")
    async def reader_shelf_contents(request: Request, shelf: str = "", q: str = "", offset: int = 0,
                                    all: int = 0) -> Dict[str, Any]:
        """What is on one shelf (or directly in one category). ``all=1`` also
        shows the books the owner's rating cut leaves off the shelves."""
        require_user(request)
        _library_on()
        from services.library import catalogue, kiwix, manuals, shelves

        place = shelves.resolve(shelf[:160])
        if not place:
            raise HTTPException(404, "There is no such shelf.")

        def work() -> Dict[str, Any]:
            books = kiwix.list_books() if kiwix.available() else []
            counts = manuals.document_counts()
            here = [{"id": book["id"], "title": book["title"], "summary": book["summary"], "articles": book["articles"],
                     "documents": counts.get(book["id"], 0)}
                    for book in catalogue.whole_collections(books, place)]
            category, name = shelves.split(place)
            # A category shows everything under it when searched, only its own loose items otherwise.
            scope = place if (name or q.strip()) else ""
            everything = bool(all)
            found = catalogue.items(scope, q[:120], offset, 60, everything=everything) if scope else {"total": 0, "items": []}
            if not name and not q.strip():
                loose = catalogue.items(place, "", offset, 60)
                found = {"total": catalogue.counts().get(place, 0),
                         "items": [item for item in loose["items"] if item["shelf"] == place]}
            return {"shelf": place, "name": shelves.display(place), "about": shelves.about(place),
                    "is_category": not name, "collections": here if not offset else [],
                    "items": [_item_view(item) for item in found["items"]], "total": found["total"],
                    "hidden": 0 if everything else catalogue.hidden_under(place),
                    "min_rating": catalogue.keeping()["min_rating"]}

        return await asyncio.to_thread(work)

    @router.get("/api/reader/find")
    async def reader_find(request: Request, q: str = "") -> Dict[str, Any]:
        """Books and documents by title or author."""
        require_user(request)
        _library_on()
        from services.library import catalogue
        found = await asyncio.to_thread(catalogue.find, q[:160], 12)
        below = [] if found else await asyncio.to_thread(lambda: catalogue.find(q[:160], 6, everything=True))
        return {"items": [_item_view(item) for item in found], "below_cut": [_item_view(item) for item in below]}

    # ── the kitchen ──────────────────────────────────────────────────────

    @router.get("/api/kitchen/recipes")
    async def kitchen_recipes(request: Request) -> Dict[str, Any]:
        owner = require_user(request)
        return {"recipes": await _do(kitchen.all_recipes, owner)}

    @router.post("/api/kitchen/recipes")
    async def kitchen_add(request: Request, body: dict = Body(...)) -> Dict[str, Any]:
        """Set a recipe out. ``text`` is the recipe's words; or ``url`` — a
        library page or a page on the web — and the words are taken from it."""
        owner = require_user(request)
        body = body if isinstance(body, dict) else {}
        text = str(body.get("text") or "").strip()
        source_title = str(body.get("source_title") or "")[:300]
        source_url = str(body.get("source_url") or "")[:1000]
        if not text:
            link = str(body.get("url") or "").strip()
            if not link:
                raise HTTPException(400, "Give the recipe's text, or a link to it.")
            found = await _do(kitchen.source_text, link)
            text, source_title, source_url = found["text"], source_title or found["title"], found["url"]
        try:
            return await kitchen.add(owner, text, title=str(body.get("title") or ""), source_title=source_title,
                                     source_url=source_url, year=str(body.get("year") or ""))
        except ReaderError as exc:
            raise HTTPException(400, str(exc))

    @router.get("/api/kitchen/recipes/{recipe_id}")
    async def kitchen_get(request: Request, recipe_id: str) -> Dict[str, Any]:
        owner = require_user(request)
        return await _do(kitchen.get, owner, recipe_id)

    @router.get("/api/kitchen/recipes/{recipe_id}/text")
    async def kitchen_text(request: Request, recipe_id: str) -> PlainTextResponse:
        owner = require_user(request)
        recipe = await _do(kitchen.get, owner, recipe_id)
        return PlainTextResponse(kitchen.as_markdown(recipe), media_type="text/markdown; charset=utf-8")

    @router.patch("/api/kitchen/recipes/{recipe_id}")
    async def kitchen_update(request: Request, recipe_id: str, body: dict = Body(...)) -> Dict[str, Any]:
        owner = require_user(request)
        return await _do(kitchen.update, owner, recipe_id, body if isinstance(body, dict) else {})

    @router.post("/api/kitchen/recipes/{recipe_id}/redo")
    async def kitchen_redo(request: Request, recipe_id: str) -> Dict[str, Any]:
        owner = require_user(request)
        try:
            return await kitchen.redo(owner, recipe_id)
        except ReaderError as exc:
            raise HTTPException(400, str(exc))

    @router.delete("/api/kitchen/recipes/{recipe_id}")
    async def kitchen_remove(request: Request, recipe_id: str) -> Dict[str, Any]:
        owner = require_user(request)
        if not await _do(kitchen.remove, owner, recipe_id):
            raise HTTPException(404, "That recipe is not in your cookbook.")
        return {"ok": True}

    return router
