"""Lists and plans: things to buy, pack, do — checked off as you go.

A list is a checklist note (``notes`` table, ``note_type`` checklist / todo /
goal) with no due time, so everything made here is also in the main app's
Notes, and the other way round. A *plan* is a list labelled ``plan``: the
same thing, shown apart, usually written by the assistant from a goal
("plan Emma's birthday").
"""

from __future__ import annotations

import json
import uuid
from typing import Iterable, List, Optional

from core.database import Note, SessionLocal
from sqlalchemy.orm.attributes import flag_modified

from .common import HomeError, best_match, iso, norm, owner_of, scoped, squash

LIST_TYPES = ("checklist", "todo", "goal")
MAX_ITEMS = 500


def _items(note: Note) -> List[dict]:
    try:
        raw = json.loads(note.items) if note.items else []
    except (TypeError, ValueError):
        raw = []
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict) or not squash(item.get("text")):
            continue
        out.append({"id": str(item.get("id") or uuid.uuid4().hex[:10]), "text": squash(item.get("text")),
                    "done": bool(item.get("done") or item.get("checked")), "indent": int(item.get("indent") or 0)})
    return out


def _store(note: Note, items: List[dict]) -> None:
    note.items = json.dumps(items, ensure_ascii=False)
    flag_modified(note, "items")


def _view(note: Note) -> dict:
    items = _items(note)
    return {
        "id": note.id, "title": note.title or "Untitled list", "kind": "plan" if (note.label or "") == "plan" else "list",
        "pinned": bool(note.pinned), "archived": bool(note.archived),
        "items": [{"id": i["id"], "text": i["text"], "done": i["done"]} for i in items],
        "open": sum(1 for i in items if not i["done"]), "done": sum(1 for i in items if i["done"]),
        "updated": iso(note.updated_at),
    }


def _query(db, owner: Optional[str], archived: bool = False):
    q = scoped(db.query(Note), Note, owner).filter(Note.note_type.in_(LIST_TYPES))
    q = q.filter((Note.due_date.is_(None)) | (Note.due_date == ""))
    q = q.filter((Note.label.is_(None)) | (Note.label != "calendar"))
    return q.filter(Note.archived == archived)  # noqa: E712


def _get(db, owner: Optional[str], list_id: str) -> Note:
    note = scoped(db.query(Note), Note, owner).filter(Note.id == list_id).first()
    if note is None:
        raise HomeError("That list no longer exists.")
    return note


def all_lists(owner: Optional[str], archived: bool = False) -> List[dict]:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        notes = _query(db, owner, archived).all()
        notes.sort(key=lambda n: (not n.pinned, -(n.updated_at.timestamp() if n.updated_at else 0)))
        return [_view(n) for n in notes]
    finally:
        db.close()


def get_list(owner: Optional[str], list_id: str) -> dict:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        return _view(_get(db, owner, list_id))
    finally:
        db.close()


def find_list(owner: Optional[str], name: str) -> Optional[dict]:
    """The list a person means by a name ("groceries", "the packing list")."""
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        notes = _query(db, owner).all()
        wanted = norm(name)
        for drop in ("the ", "my "):
            if wanted.startswith(drop):
                wanted = wanted[len(drop):]
        for suffix in (" list", " plan"):
            if wanted.endswith(suffix):
                wanted = wanted[: -len(suffix)]
        match = best_match(notes, name, key=lambda n: n.title or "") or best_match(notes, wanted, key=lambda n: n.title or "")
        if match is None and wanted:
            # "grocery" should find "Groceries".
            stem = wanted.rstrip("s")[: max(len(wanted) - 3, 4)]
            near = [n for n in notes if stem and norm(n.title or "").startswith(stem)]
            match = near[0] if len(near) == 1 else None
        return _view(match) if match is not None else None
    finally:
        db.close()


def create_list(owner: Optional[str], title: str, items: Iterable[str] = (), kind: str = "list") -> dict:
    owner = owner_of(owner)
    title = squash(title)[:120]
    if not title:
        raise HomeError("Give the list a name.")
    db = SessionLocal()
    try:
        note = Note(id=str(uuid.uuid4()), owner=owner, title=title, content="", note_type="checklist",
                    label="plan" if kind == "plan" else None, source="home", items="[]")
        _store(note, _merge([], items))
        db.add(note)
        db.commit()
        return _view(note)
    finally:
        db.close()


def _merge(existing: List[dict], texts: Iterable[str]) -> List[dict]:
    """Append new items, skipping what is already on the list and not done.
    A done item with the same text is un-ticked instead of duplicated."""
    out = list(existing)
    for text in texts or []:
        text = squash(text)[:300]
        if not text:
            continue
        same = next((i for i in out if norm(i["text"]) == norm(text)), None)
        if same is not None:
            same["done"] = False
            continue
        if len(out) >= MAX_ITEMS:
            raise HomeError(f"A list holds at most {MAX_ITEMS} items.")
        out.append({"id": uuid.uuid4().hex[:10], "text": text, "done": False, "indent": 0})
    return out


def add_items(owner: Optional[str], list_id: str, texts: Iterable[str]) -> dict:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        note = _get(db, owner, list_id)
        _store(note, _merge(_items(note), texts))
        db.commit()
        return _view(note)
    finally:
        db.close()


def set_item(owner: Optional[str], list_id: str, item_id: str, text: Optional[str] = None,
             done: Optional[bool] = None) -> dict:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        note = _get(db, owner, list_id)
        items = _items(note)
        item = next((i for i in items if i["id"] == item_id), None)
        if item is None:
            raise HomeError("That item is no longer on the list.")
        if text is not None and squash(text):
            item["text"] = squash(text)[:300]
        if done is not None:
            item["done"] = bool(done)
        _store(note, items)
        db.commit()
        return _view(note)
    finally:
        db.close()


def check_items(owner: Optional[str], list_id: str, texts: Iterable[str], done: bool = True) -> dict:
    """Tick (or un-tick) items by what they say — how the assistant does it."""
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        note = _get(db, owner, list_id)
        items = _items(note)
        missing = []
        for text in texts or []:
            match = best_match(items, text, key=lambda i: i["text"])
            if match is None:
                missing.append(squash(text))
            else:
                match["done"] = bool(done)
        _store(note, items)
        db.commit()
        view = _view(note)
        view["not_found"] = missing
        return view
    finally:
        db.close()


def remove_item(owner: Optional[str], list_id: str, item_id: str) -> dict:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        note = _get(db, owner, list_id)
        _store(note, [i for i in _items(note) if i["id"] != item_id])
        db.commit()
        return _view(note)
    finally:
        db.close()


def remove_items(owner: Optional[str], list_id: str, texts: Iterable[str]) -> dict:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        note = _get(db, owner, list_id)
        items = _items(note)
        for text in texts or []:
            match = best_match(items, text, key=lambda i: i["text"])
            if match is not None:
                items.remove(match)
        _store(note, items)
        db.commit()
        return _view(note)
    finally:
        db.close()


def clear_done(owner: Optional[str], list_id: str) -> dict:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        note = _get(db, owner, list_id)
        _store(note, [i for i in _items(note) if not i["done"]])
        db.commit()
        return _view(note)
    finally:
        db.close()


def update_list(owner: Optional[str], list_id: str, title: Optional[str] = None, pinned: Optional[bool] = None,
                archived: Optional[bool] = None, kind: Optional[str] = None) -> dict:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        note = _get(db, owner, list_id)
        if title is not None and squash(title):
            note.title = squash(title)[:120]
        if pinned is not None:
            note.pinned = bool(pinned)
        if archived is not None:
            note.archived = bool(archived)
        if kind in ("list", "plan"):
            note.label = "plan" if kind == "plan" else None
        db.commit()
        return _view(note)
    finally:
        db.close()


def delete_list(owner: Optional[str], list_id: str) -> bool:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        note = _get(db, owner, list_id)
        db.delete(note)
        db.commit()
        return True
    finally:
        db.close()
