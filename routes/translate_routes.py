"""Translation.

    GET    /api/translate/languages       the languages, common ones first
    POST   /api/translate                 {"text", "to", "from"?, "keep"?} -> the translation
    POST   /api/translate/stream          the same, a line of JSON per paragraph as it is done
    POST   /api/translate/photo           a picture (multipart: file, to, from?) -> the words in it, translated
    GET    /api/translate/history         the signed-in person's recent translations
    DELETE /api/translate/history         forget them all
    DELETE /api/translate/history/{id}    forget one

Everything runs on this machine (``services/translate``). What a person
translates is kept for them alone unless ``keep`` is false.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from src.auth_helpers import require_user

logger = logging.getLogger(__name__)


def setup_translate_routes() -> APIRouter:
    router = APIRouter(tags=["translate"])

    @router.get("/api/translate/languages")
    async def translate_languages(request: Request) -> Dict[str, Any]:
        require_user(request)
        from services.translate import MAX_CHARS, core, engine, languages
        return {"languages": languages.listing(), "max_chars": MAX_CHARS, "max_image_mb": core.MAX_IMAGE_BYTES // (1024 * 1024),
                "dedicated": not engine.dedicated_known_missing()}

    def _wanted(body: Dict[str, Any]) -> Dict[str, str]:
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON object required")
        return {"text": str(body.get("text") or ""), "to": str(body.get("to") or "")[:40],
                "source": str(body.get("from") or "auto")[:40]}

    @router.post("/api/translate")
    async def translate_now(request: Request, body: dict = Body(...)) -> Dict[str, Any]:
        owner = require_user(request)
        from services import translate
        from services.translate import history

        wanted = _wanted(body)
        try:
            result = await translate.translate_text(wanted["text"], wanted["to"], wanted["source"], owner=owner or None)
        except translate.TranslateError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            logger.warning("translate failed: %s", exc)
            raise HTTPException(503, "The translator is not answering right now. Try again in a moment.")
        if body.get("keep") is not False and not result.get("same_language"):
            result["id"] = history.add(owner or None, kind="text", source=str(result["source"]), target=str(result["target"]),
                                       text=wanted["text"], result=str(result["text"]))["id"]
        return result

    @router.post("/api/translate/stream")
    async def translate_stream(request: Request, body: dict = Body(...)) -> StreamingResponse:
        """One JSON object per line: ``{"part", "of", "text"}`` as each
        paragraph is done, then the whole result with ``"done": true`` (or
        ``{"error": …}``)."""
        owner = require_user(request)
        from services import translate
        from services.translate import history

        wanted = _wanted(body)
        keep = body.get("keep") is not False

        async def lines():
            import asyncio

            queue: asyncio.Queue = asyncio.Queue()

            async def on_part(number: int, of: int, text: str) -> None:
                await queue.put({"part": number, "of": of, "text": text})

            async def work() -> None:
                try:
                    result = await translate.translate_text(wanted["text"], wanted["to"], wanted["source"], owner=owner or None,
                                                            on_part=on_part)
                    if keep and not result.get("same_language"):
                        result["id"] = history.add(owner or None, kind="text", source=str(result["source"]),
                                                   target=str(result["target"]), text=wanted["text"], result=str(result["text"]))["id"]
                    await queue.put(dict(result, done=True))
                except translate.TranslateError as exc:
                    await queue.put({"error": str(exc)})
                except Exception as exc:
                    logger.warning("translate failed: %s", exc)
                    await queue.put({"error": "The translator is not answering right now. Try again in a moment."})

            task = asyncio.create_task(work())
            try:
                while True:
                    item = await queue.get()
                    yield json.dumps(item, ensure_ascii=False) + "\n"
                    if item.get("done") or item.get("error"):
                        break
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(lines(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @router.post("/api/translate/photo")
    async def translate_photo(request: Request, file: UploadFile = File(...), to: str = Form(...),
                              source: str = Form("auto", alias="from"), keep: str = Form("1")) -> Dict[str, Any]:
        owner = require_user(request)
        from services import translate
        from services.translate import core, history

        data = await file.read(core.MAX_IMAGE_BYTES + 1)
        try:
            result = await translate.translate_image(data, file.content_type or "", to[:40], source[:40], owner=owner or None)
        except translate.TranslateError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            logger.warning("photo translate failed: %s", exc)
            raise HTTPException(503, "The translator is not answering right now. Try again in a moment.")
        if keep not in ("0", "false", "no"):
            result["id"] = history.add(owner or None, kind="photo", source=str(result["source"]), target=str(result["target"]),
                                       text="", result=str(result["text"]))["id"]
        return result

    @router.get("/api/translate/history")
    async def translate_history(request: Request, limit: int = 30) -> Dict[str, Any]:
        owner = require_user(request)
        from services.translate import history
        return {"items": history.recent(owner or None, limit)}

    @router.delete("/api/translate/history")
    async def translate_history_clear(request: Request) -> Dict[str, Any]:
        owner = require_user(request)
        from services.translate import history
        return {"removed": history.clear(owner or None)}

    @router.delete("/api/translate/history/{item_id}")
    async def translate_history_remove(request: Request, item_id: str) -> Dict[str, Any]:
        owner = require_user(request)
        from services.translate import history
        if not history.remove(owner or None, item_id):
            raise HTTPException(404, "That translation is gone.")
        return {"ok": True}

    return router
