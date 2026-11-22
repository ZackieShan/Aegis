"""The home screen's API: lists and plans, reminders, trackers, boards.

Everything works on the signed-in person's own things only (``services/home``
does the work and the scoping). Times are read and written on the caller's
own clock: the browser sends its offset with every request.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from services.home import HomeError, boards, lists, reminders, trackers
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)


def setup_home_routes(auth_manager=None) -> APIRouter:
    router = APIRouter(prefix="/api/home", tags=["home"])

    def _owner(request: Request) -> str:
        """Who is asking, with their clock set for anything time-related."""
        owner = require_user(request)
        try:
            from src import user_time
            user_time.set_user_tz_offset(request.headers.get("x-tz-offset"))
            user_time.set_user_tz_name(request.headers.get("x-tz-name"))
        except Exception:
            pass
        return owner

    def _do(work, *args, **kwargs):
        try:
            return work(*args, **kwargs)
        except HomeError as exc:
            raise HTTPException(400, str(exc))

    # ── the first screen ─────────────────────────────────────────────────

    @router.get("/start")
    def start(request: Request):
        """Everything the home screen needs to draw itself, in one call."""
        owner = _owner(request)
        experience = {"mode": "full", "display_name": "", "assistant_name": "", "sections": ["lists", "reminders", "trackers", "boards"],
                      "theme": "auto", "accent": "", "allow_full_app": True}
        is_admin = True
        if auth_manager is not None and owner and owner in auth_manager.users:
            experience = auth_manager.get_experience(owner)
            is_admin = auth_manager.is_admin(owner)
        now = datetime.now(timezone.utc)
        soon = (now + timedelta(hours=24)).isoformat()
        mine = reminders.all_reminders(owner)
        due = [r for r in mine if r["due"] and r["due"] <= soon][:6]
        all_lists = lists.all_lists(owner)
        all_trackers = trackers.all_trackers(owner)
        return {
            "me": {
                "username": owner or "", "name": experience.get("display_name") or (owner or "").title(),
                "assistant": experience.get("assistant_name") or "Aegis", "is_admin": is_admin,
                "sections": experience.get("sections") or [], "theme": experience.get("theme") or "auto",
                "accent": experience.get("accent") or "",
                "full_app": bool(is_admin or experience.get("mode") != "simple" or experience.get("allow_full_app")),
            },
            "due": due,
            "counts": {
                "lists": len(all_lists), "open_items": sum(l["open"] for l in all_lists),
                "reminders": len(mine), "overdue": sum(1 for r in mine if r["overdue"]),
                "trackers": len(all_trackers), "trackers_left": sum(1 for t in all_trackers if not t["done_today"]),
                "boards": len(boards.all_boards(owner)),
            },
        }

    # ── lists ────────────────────────────────────────────────────────────

    @router.get("/lists")
    def lists_all(request: Request, archived: bool = False):
        return {"lists": lists.all_lists(_owner(request), archived=archived)}

    @router.post("/lists")
    def lists_create(request: Request, body: dict = Body(...)):
        owner = _owner(request)
        items = body.get("items") if isinstance(body.get("items"), list) else []
        return {"list": _do(lists.create_list, owner, body.get("title") or "", [str(i) for i in items],
                            kind="plan" if body.get("kind") == "plan" else "list")}

    @router.get("/lists/{list_id}")
    def lists_get(request: Request, list_id: str):
        return {"list": _do(lists.get_list, _owner(request), list_id)}

    @router.put("/lists/{list_id}")
    def lists_update(request: Request, list_id: str, body: dict = Body(...)):
        return {"list": _do(lists.update_list, _owner(request), list_id, title=body.get("title"),
                            pinned=body.get("pinned") if isinstance(body.get("pinned"), bool) else None,
                            archived=body.get("archived") if isinstance(body.get("archived"), bool) else None,
                            kind=body.get("kind"))}

    @router.delete("/lists/{list_id}")
    def lists_delete(request: Request, list_id: str):
        _do(lists.delete_list, _owner(request), list_id)
        return {"ok": True}

    @router.post("/lists/{list_id}/items")
    def lists_add_items(request: Request, list_id: str, body: dict = Body(...)):
        texts = body.get("texts") if isinstance(body.get("texts"), list) else [body.get("text")]
        return {"list": _do(lists.add_items, _owner(request), list_id, [str(t) for t in texts if t])}

    @router.put("/lists/{list_id}/items/{item_id}")
    def lists_set_item(request: Request, list_id: str, item_id: str, body: dict = Body(...)):
        return {"list": _do(lists.set_item, _owner(request), list_id, item_id, text=body.get("text"),
                            done=body.get("done") if isinstance(body.get("done"), bool) else None)}

    @router.delete("/lists/{list_id}/items/{item_id}")
    def lists_remove_item(request: Request, list_id: str, item_id: str):
        return {"list": _do(lists.remove_item, _owner(request), list_id, item_id)}

    @router.post("/lists/{list_id}/clear-done")
    def lists_clear_done(request: Request, list_id: str):
        return {"list": _do(lists.clear_done, _owner(request), list_id)}

    # ── reminders ────────────────────────────────────────────────────────

    @router.get("/reminders")
    def reminders_all(request: Request, done: bool = False):
        return {"reminders": reminders.all_reminders(_owner(request), include_done=done)}

    @router.post("/reminders")
    def reminders_create(request: Request, body: dict = Body(...)):
        return {"reminder": _do(reminders.create_reminder, _owner(request), body.get("text") or "",
                                body.get("due") or "", body.get("repeat") or "none")}

    @router.put("/reminders/{reminder_id}")
    def reminders_update(request: Request, reminder_id: str, body: dict = Body(...)):
        return {"reminder": _do(reminders.update_reminder, _owner(request), reminder_id, text=body.get("text"),
                                due=body.get("due"), repeat=body.get("repeat"),
                                done=body.get("done") if isinstance(body.get("done"), bool) else None)}

    @router.post("/reminders/{reminder_id}/snooze")
    def reminders_snooze(request: Request, reminder_id: str, body: dict = Body(default={})):
        return {"reminder": _do(reminders.snooze, _owner(request), reminder_id, int(body.get("minutes") or 60))}

    @router.delete("/reminders/{reminder_id}")
    def reminders_delete(request: Request, reminder_id: str):
        _do(reminders.delete_reminder, _owner(request), reminder_id)
        return {"ok": True}

    # ── trackers ─────────────────────────────────────────────────────────

    @router.get("/trackers")
    def trackers_all(request: Request):
        return {"trackers": trackers.all_trackers(_owner(request))}

    @router.post("/trackers")
    def trackers_create(request: Request, body: dict = Body(...)):
        return {"tracker": _do(trackers.create_tracker, _owner(request), body.get("name") or "",
                               kind=body.get("kind") or "check", unit=body.get("unit") or "",
                               goal=body.get("goal"), color=body.get("color") or "")}

    @router.get("/trackers/{tracker_id}")
    def trackers_get(request: Request, tracker_id: str, days: int = 60):
        return {"tracker": _do(trackers.get_tracker, _owner(request), tracker_id, days)}

    @router.put("/trackers/{tracker_id}")
    def trackers_update(request: Request, tracker_id: str, body: dict = Body(...)):
        fields = {key: body[key] for key in ("name", "unit", "goal", "color", "archived") if key in body}
        return {"tracker": _do(trackers.update_tracker, _owner(request), tracker_id, **fields)}

    @router.post("/trackers/{tracker_id}/log")
    def trackers_log(request: Request, tracker_id: str, body: dict = Body(default={})):
        value = body.get("value")
        try:
            value = None if value in (None, "") else float(value)
        except (TypeError, ValueError):
            raise HTTPException(400, "That is not a number.")
        return {"tracker": _do(trackers.log, _owner(request), tracker_id, value=value,
                               note=body.get("note") or "", day=body.get("day"))}

    @router.delete("/trackers/{tracker_id}/entries/{entry_id}")
    def trackers_delete_entry(request: Request, tracker_id: str, entry_id: str):
        return {"tracker": _do(trackers.delete_entry, _owner(request), tracker_id, entry_id)}

    @router.delete("/trackers/{tracker_id}")
    def trackers_delete(request: Request, tracker_id: str):
        _do(trackers.delete_tracker, _owner(request), tracker_id)
        return {"ok": True}

    # ── boards ───────────────────────────────────────────────────────────

    @router.get("/boards")
    def boards_all(request: Request):
        return {"boards": boards.all_boards(_owner(request))}

    @router.post("/boards")
    def boards_create(request: Request, body: dict = Body(...)):
        return {"board": _do(boards.create_board, _owner(request), body.get("name") or "", body.get("description") or "")}

    @router.get("/boards/{board_id}")
    def boards_get(request: Request, board_id: str):
        return {"board": _do(boards.get_board, _owner(request), board_id)}

    @router.put("/boards/{board_id}")
    def boards_update(request: Request, board_id: str, body: dict = Body(...)):
        return {"board": _do(boards.update_board, _owner(request), board_id, name=body.get("name"),
                             description=body.get("description"))}

    @router.delete("/boards/{board_id}")
    def boards_delete(request: Request, board_id: str):
        _do(boards.delete_board, _owner(request), board_id)
        return {"ok": True}

    @router.post("/boards/{board_id}/pins")
    async def pins_add(request: Request, board_id: str, body: dict = Body(...)):
        """Pin a link (``url``) or a note (``text``)."""
        owner = _owner(request)
        try:
            if body.get("url"):
                pin = await asyncio.to_thread(boards.add_link_pin, owner, board_id, str(body["url"]),
                                              str(body.get("title") or ""), str(body.get("note") or ""))
            else:
                pin = boards.add_note_pin(owner, board_id, str(body.get("text") or body.get("note") or ""),
                                          str(body.get("title") or ""))
        except HomeError as exc:
            raise HTTPException(400, str(exc))
        return {"pin": pin}

    @router.post("/boards/{board_id}/pins/upload")
    async def pins_upload(request: Request, board_id: str, file: UploadFile = File(...),
                          title: str = Form(""), note: str = Form("")):
        """Pin a picture from the phone or computer."""
        owner = _owner(request)
        data = await file.read(boards.MAX_UPLOAD_BYTES + 1)
        try:
            pin = await asyncio.to_thread(boards.add_image_pin, owner, board_id, data, title, note)
        except HomeError as exc:
            raise HTTPException(400, str(exc))
        return {"pin": pin}

    @router.put("/pins/{pin_id}")
    def pins_update(request: Request, pin_id: str, body: dict = Body(...)):
        return {"pin": _do(boards.update_pin, _owner(request), pin_id, title=body.get("title"),
                           note=body.get("note"), board_id=body.get("board_id"))}

    @router.delete("/pins/{pin_id}")
    def pins_delete(request: Request, pin_id: str):
        _do(boards.delete_pin, _owner(request), pin_id)
        return {"ok": True}

    @router.get("/pins/{pin_id}/image")
    def pins_image(request: Request, pin_id: str, thumb: Optional[int] = 0):
        path = boards.image_path(_owner(request), pin_id, thumb=bool(thumb))
        if path is None:
            raise HTTPException(404, "No picture")
        return FileResponse(str(path), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=604800, immutable"})

    return router
