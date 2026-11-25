"""A person's shelf and what they marked: opened documents, where they
stopped, their highlights and notes — and handing a highlight to Aegis's
memory so it is there in later conversations.

Everything is per account. A highlight shared with memory becomes a note in a
topic of that person's deep memory (``services/memory/mind``), marked as
coming from their reading, with a link back to the page: it shows up in /mind
like everything else Aegis keeps, and can be edited or deleted there.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from core.database import SessionLocal, utcnow_naive

from . import content
from .content import ReaderError
from .models import ReaderItem, ReaderMark, epoch

COLORS = ("yellow", "green", "blue", "pink")
MAX_QUOTE = 6000
MAX_NOTE = 4000


def _owner(value: Optional[str]) -> Optional[str]:
    return value or None


def _mine(query, model, owner: Optional[str]):
    return query.filter(model.owner.is_(None)) if owner is None else query.filter(model.owner == owner)


def _squash(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def reader_link(item: ReaderItem, page: int = 0) -> str:
    """The address that opens a shelf entry in the reader."""
    link = f"/read?b={quote(item.book, safe='')}&p={quote(item.path, safe='/')}"
    return f"{link}&page={int(page)}" if page else link


def _item_view(item: ReaderItem) -> Dict[str, Any]:
    return {"id": item.id, "kind": item.kind, "book": item.book, "path": item.path, "title": item.title or item.path,
            "pages": item.pages or 0, "page": item.page or 1, "position": float(item.position or 0.0),
            "pinned": bool(item.pinned), "opened": epoch(item.opened_at),
            "link": reader_link(item, item.page if item.kind == "manual" else 0)}


def _mark_view(mark: ReaderMark, item: Optional[ReaderItem] = None) -> Dict[str, Any]:
    view = {"id": mark.id, "item": mark.item_id, "page": mark.page or 0, "start": mark.start or 0, "end": mark.end or 0,
            "quote": mark.quote or "", "prefix": mark.prefix or "", "suffix": mark.suffix or "", "note": mark.note or "",
            "color": mark.color or "yellow", "shared": epoch(mark.shared_at),
            "shared_topic": mark.shared_topic or "", "created": epoch(mark.created_at)}
    if item is not None:
        view["title"] = item.title or item.path
        view["link"] = reader_link(item, mark.page or 0)
    return view


# ── opening ───────────────────────────────────────────────────────────────

def open_document(owner: Optional[str], url: str = "", book: str = "", path: str = "", page: int = 0) -> Dict[str, Any]:
    """Open something from the library: put it on the shelf (or bring it to
    the front) and return what there is to read, with the person's marks."""
    owner = _owner(owner)
    target = content.resolve(url=url, book=book, path=path, page=page)
    book, path, kind = str(target["book"]), str(target["path"]), str(target["kind"])
    if kind == "article":
        body = content.article(book, path)
        path, title, total = str(body["path"]), str(body["title"]), 0
        if not path:
            raise ReaderError("That is not something from the library.")
    else:
        info = content.document(book, path)
        title, total = str(info["title"]), int(info["pages"])
    db = SessionLocal()
    try:
        item = _mine(db.query(ReaderItem), ReaderItem, owner).filter(ReaderItem.book == book, ReaderItem.path == path).first()
        if item is None:
            item = ReaderItem(id=uuid.uuid4().hex[:16], owner=owner, kind=kind, book=book, path=path, title=title,
                              pages=total, page=1, position=0.0, pinned=False)
            db.add(item)
        item.title, item.pages, item.kind, item.opened_at = title, total, kind, utcnow_naive()
        asked = int(target["page"] or 0)
        if kind == "manual":
            if asked and (url or page):
                item.page = max(1, min(asked, max(total, 1)))
            item.page = max(1, min(int(item.page or 1), max(total, 1)))
        db.commit()
        result: Dict[str, Any] = {"item": _item_view(item)}
        if kind == "article":
            result["content"] = {"html": body["html"], "original": content.library_url(book, path)}
        else:
            result["content"] = {"pages": content.pages(book, path, item.page), "total": total,
                                 "original": content.library_url(book, path, item.page)}
        marks = _mine(db.query(ReaderMark), ReaderMark, owner).filter(ReaderMark.item_id == item.id) \
            .order_by(ReaderMark.page, ReaderMark.start).all()
        result["marks"] = [_mark_view(mark) for mark in marks]
        return result
    finally:
        db.close()


def more_pages(owner: Optional[str], item_id: str, first: int, count: int = content.PAGES_PER_REQUEST) -> Dict[str, Any]:
    item = get_item(owner, item_id)
    if item["kind"] != "manual":
        raise ReaderError("That is not a document with pages.")
    return {"pages": content.pages(item["book"], item["path"], first, count), "total": item["pages"],
            "original": content.library_url(item["book"], item["path"], max(1, int(first)))}


def get_item(owner: Optional[str], item_id: str) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        item = _mine(db.query(ReaderItem), ReaderItem, _owner(owner)).filter(ReaderItem.id == item_id).first()
        if item is None:
            raise ReaderError("That is not on your shelf.")
        return _item_view(item)
    finally:
        db.close()


def update_item(owner: Optional[str], item_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    """Remember the place (``page`` / ``position``) or keep it on the shelf (``pinned``)."""
    db = SessionLocal()
    try:
        item = _mine(db.query(ReaderItem), ReaderItem, _owner(owner)).filter(ReaderItem.id == item_id).first()
        if item is None:
            raise ReaderError("That is not on your shelf.")
        if isinstance(changes.get("pinned"), bool):
            item.pinned = changes["pinned"]
        if isinstance(changes.get("page"), (int, float)) and not isinstance(changes.get("page"), bool):
            item.page = max(1, min(int(changes["page"]), max(int(item.pages or 1), 1)))
        if isinstance(changes.get("position"), (int, float)) and not isinstance(changes.get("position"), bool):
            item.position = max(0.0, min(float(changes["position"]), 1.0))
        db.commit()
        return _item_view(item)
    finally:
        db.close()


def remove_item(owner: Optional[str], item_id: str) -> bool:
    """Take a document off the shelf, with the marks made in it."""
    db = SessionLocal()
    try:
        owner = _owner(owner)
        item = _mine(db.query(ReaderItem), ReaderItem, owner).filter(ReaderItem.id == item_id).first()
        if item is None:
            return False
        _mine(db.query(ReaderMark), ReaderMark, owner).filter(ReaderMark.item_id == item.id).delete(synchronize_session=False)
        db.delete(item)
        db.commit()
        return True
    finally:
        db.close()


def shelf(owner: Optional[str], limit: int = 40) -> Dict[str, Any]:
    """What the reader's front page shows: kept documents, what was read
    lately, the newest highlights."""
    db = SessionLocal()
    try:
        owner = _owner(owner)
        items = _mine(db.query(ReaderItem), ReaderItem, owner).order_by(ReaderItem.opened_at.desc()).limit(200).all()
        by_id = {item.id: item for item in items}
        marks = _mine(db.query(ReaderMark), ReaderMark, owner).order_by(ReaderMark.created_at.desc()).limit(8).all()
        total = _mine(db.query(ReaderMark), ReaderMark, owner).count()
        counts: Dict[str, int] = {}
        for (item_id,) in _mine(db.query(ReaderMark.item_id), ReaderMark, owner).all():
            counts[item_id] = counts.get(item_id, 0) + 1
        def with_count(item: ReaderItem) -> Dict[str, Any]:
            return {**_item_view(item), "marks": counts.get(item.id, 0)}
        return {
            "kept": [with_count(item) for item in items if item.pinned][:limit],
            "recent": [with_count(item) for item in items if not item.pinned][:limit],
            "highlights": [_mark_view(mark, by_id.get(mark.item_id)) for mark in marks if mark.item_id in by_id],
            "highlight_count": total,
        }
    finally:
        db.close()


# ── marks ─────────────────────────────────────────────────────────────────

def add_mark(owner: Optional[str], item_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    quote_text = str(data.get("quote") or "").strip()
    if not quote_text:
        raise ReaderError("Select some text to highlight.")
    if len(quote_text) > MAX_QUOTE:
        raise ReaderError("That is too much for one highlight. Mark a shorter passage.")
    db = SessionLocal()
    try:
        owner = _owner(owner)
        item = _mine(db.query(ReaderItem), ReaderItem, owner).filter(ReaderItem.id == item_id).first()
        if item is None:
            raise ReaderError("That is not on your shelf.")
        start = max(0, int(data.get("start") or 0))
        mark = ReaderMark(
            id=uuid.uuid4().hex[:16], owner=owner, item_id=item.id, page=max(0, int(data.get("page") or 0)),
            start=start, end=max(start, int(data.get("end") or 0)), quote=quote_text,
            prefix=str(data.get("prefix") or "")[-60:], suffix=str(data.get("suffix") or "")[:60],
            note=str(data.get("note") or "").strip()[:MAX_NOTE],
            color=data.get("color") if data.get("color") in COLORS else "yellow")
        db.add(mark)
        db.commit()
        return _mark_view(mark, item)
    finally:
        db.close()


def update_mark(owner: Optional[str], mark_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        mark = _mine(db.query(ReaderMark), ReaderMark, _owner(owner)).filter(ReaderMark.id == mark_id).first()
        if mark is None:
            raise ReaderError("That highlight is gone.")
        if isinstance(changes.get("note"), str):
            mark.note = changes["note"].strip()[:MAX_NOTE]
        if changes.get("color") in COLORS:
            mark.color = changes["color"]
        db.commit()
        return _mark_view(mark)
    finally:
        db.close()


def remove_mark(owner: Optional[str], mark_id: str) -> bool:
    db = SessionLocal()
    try:
        mark = _mine(db.query(ReaderMark), ReaderMark, _owner(owner)).filter(ReaderMark.id == mark_id).first()
        if mark is None:
            return False
        db.delete(mark)
        db.commit()
        return True
    finally:
        db.close()


def all_marks(owner: Optional[str], item_id: str = "", limit: int = 500) -> List[Dict[str, Any]]:
    """Highlights, newest first — everything, or one document's in reading order."""
    db = SessionLocal()
    try:
        owner = _owner(owner)
        query = _mine(db.query(ReaderMark), ReaderMark, owner)
        if item_id:
            query = query.filter(ReaderMark.item_id == item_id).order_by(ReaderMark.page, ReaderMark.start)
        else:
            query = query.order_by(ReaderMark.created_at.desc())
        marks = query.limit(max(1, min(int(limit), 2000))).all()
        items = {item.id: item for item in _mine(db.query(ReaderItem), ReaderItem, owner)
                 .filter(ReaderItem.id.in_({mark.item_id for mark in marks} or {""})).all()}
        return [_mark_view(mark, items.get(mark.item_id)) for mark in marks]
    finally:
        db.close()


def as_markdown(owner: Optional[str], item_id: str = "") -> str:
    """Highlights as a text file a person can keep or send on."""
    lines: List[str] = []
    current = None
    for mark in sorted(all_marks(owner, item_id, 2000), key=lambda m: (m.get("title") or "", m["page"], m["start"])):
        if mark.get("title") != current:
            current = mark.get("title")
            lines += ["", f"## {current}", ""]
        where = f" (page {mark['page']})" if mark["page"] else ""
        lines.append("> " + _squash(mark["quote"]) + where)
        if mark["note"]:
            lines.append("")
            lines.append(_squash(mark["note"]))
        lines.append("")
    return ("# Highlights\n" + "\n".join(lines)).strip() + "\n"


# ── handing a highlight to Aegis's memory ─────────────────────────────────

def share_mark(owner: Optional[str], mark_id: str, memory_manager, topic: str = "") -> Dict[str, Any]:
    """Keep a highlight in the person's memory: a note in a topic (the one
    named, else one called after the document), traceable to the page."""
    from services.memory import journal, mind

    db = SessionLocal()
    try:
        owner_key = _owner(owner)
        mark = _mine(db.query(ReaderMark), ReaderMark, owner_key).filter(ReaderMark.id == mark_id).first()
        if mark is None:
            raise ReaderError("That highlight is gone.")
        item = _mine(db.query(ReaderItem), ReaderItem, owner_key).filter(ReaderItem.id == mark.item_id).first()
        title = (item.title if item else "") or "something read"
        name = _squash(topic)[:60] or _squash(title)[:60]
        where = f", page {mark.page}" if mark.page else ""
        room = mind.NOTE_CHARS - len(f"“” — {title[:70]}{where}") - 2
        said = _squash(mark.quote)
        if len(said) > room:
            said = said[: max(40, room - 1)].rstrip() + "…"
        text = f"“{said}” — {title[:70]}{where}"
        if mark.note:
            remaining = mind.NOTE_CHARS - len(text) - 12
            if remaining > 20:
                text += " · My note: " + _squash(mark.note)[:remaining]
        link = f"{reader_link(item, mark.page or 0)}#mark={mark.id}" if item else ""      # straight to the passage
        with journal.cause("you", detail="reader"):
            saved, added = mind.add_notes(memory_manager, owner_key, name, [text], kind="other", by="you",
                                          keywords=[w for w in re.findall(r"[a-z]{4,}", title.lower())][:6],
                                          source={"kind": "reading", "title": title, "url": link})
        if saved is None:
            raise ReaderError("It could not be added to memory just now.")
        mark.shared_at, mark.shared_topic = utcnow_naive(), saved.get("name") or name
        db.commit()
        return {"mark": _mark_view(mark, item), "topic": {"id": saved.get("id"), "name": saved.get("name")},
                "added": bool(added)}
    finally:
        db.close()
