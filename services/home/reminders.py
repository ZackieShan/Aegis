"""Reminders: a line of text and a time, once or repeating.

A reminder is a note with a due time (``notes.due_date``), which is what the
app's reminder scanner already watches: when the time comes it is delivered
through whatever the server is set up for (in the app, e-mail, ntfy, push).
Marking one done archives it — or, if it repeats, moves it to the next time.
Repeats follow the server's own clock, so a daily 8:00 stays 8:00 across a
daylight-saving change.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from core.database import Note, SessionLocal

from .common import HomeError, iso, owner_of, scoped, squash

REPEATS = ("none", "daily", "weekly", "monthly", "yearly")
_REPEAT_WORDS = {"": "none", "never": "none", "once": "none", "no": "none", "day": "daily", "every day": "daily",
                 "week": "weekly", "every week": "weekly", "month": "monthly", "every month": "monthly",
                 "year": "yearly", "every year": "yearly", "annually": "yearly"}


def _parse(value) -> Optional[datetime]:
    """A stored due time as an aware UTC datetime. Naive values are the
    server's local time (the scanner reads them the same way)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _to_stored(value) -> str:
    """Whatever the person or the assistant gave — ISO, or words like
    "tomorrow at 3pm" — as an ISO time with its offset."""
    text = str(value or "").strip()
    if not text:
        raise HomeError("A reminder needs a time.")
    try:
        from routes.calendar_routes import parse_due_for_user
        stored = parse_due_for_user(text)
    except Exception:
        stored = text
    if _parse(stored) is None:
        raise HomeError(f"I could not read “{text}” as a time. Try something like “tomorrow at 3pm”.")
    return stored


def normal_repeat(value) -> str:
    word = str(value or "").strip().lower()
    word = _REPEAT_WORDS.get(word, word)
    return word if word in REPEATS else "none"


def next_due(due: datetime, repeat: str, after: Optional[datetime] = None) -> Optional[datetime]:
    """The next time a repeating reminder is due, strictly later than
    ``after`` (now when omitted). Worked out on the server's wall clock."""
    if repeat not in REPEATS or repeat == "none":
        return None
    after = (after or datetime.now(timezone.utc)).astimezone(timezone.utc)
    wall = due.astimezone().replace(tzinfo=None)             # server-local wall time
    for _ in range(20000):
        if repeat == "daily":
            wall += timedelta(days=1)
        elif repeat == "weekly":
            wall += timedelta(days=7)
        elif repeat == "monthly":
            year, month = (wall.year + 1, 1) if wall.month == 12 else (wall.year, wall.month + 1)
            wall = wall.replace(year=year, month=month, day=min(due.astimezone().day, calendar.monthrange(year, month)[1]))
        else:
            year = wall.year + 1
            wall = wall.replace(year=year, day=min(wall.day, calendar.monthrange(year, wall.month)[1]))
        aware = wall.astimezone()
        if aware.astimezone(timezone.utc) > after:
            return aware
    return None


def _text(note: Note) -> str:
    title = squash(note.title) or squash(note.content) or "Reminder"
    return title[len("Reminder:"):].strip() if title.lower().startswith("reminder:") else title


def _view(note: Note, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    due = _parse(note.due_date)
    repeat = normal_repeat(note.repeat)
    return {
        "id": note.id, "text": _text(note), "due": due.isoformat() if due else None, "repeat": repeat,
        "done": bool(note.archived), "overdue": bool(due and due < now and not note.archived),
        "updated": iso(note.updated_at),
    }


def _query(db, owner: Optional[str]):
    q = scoped(db.query(Note), Note, owner)
    return q.filter(Note.due_date.isnot(None), Note.due_date != "")


def _get(db, owner: Optional[str], reminder_id: str) -> Note:
    note = _query(db, owner).filter(Note.id == reminder_id).first()
    if note is None:
        raise HomeError("That reminder no longer exists.")
    return note


def all_reminders(owner: Optional[str], include_done: bool = False) -> List[dict]:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        q = _query(db, owner)
        if not include_done:
            q = q.filter(Note.archived == False)  # noqa: E712
        views = [_view(note, now) for note in q.all()]
        if include_done:
            cutoff = (now - timedelta(days=14)).isoformat()
            views = [v for v in views if not v["done"] or (v["due"] or "") >= cutoff]
        views.sort(key=lambda v: (v["done"], v["due"] or "9999"))
        return views
    finally:
        db.close()


def create_reminder(owner: Optional[str], text: str, due, repeat="none") -> dict:
    owner = owner_of(owner)
    text = squash(text)[:300]
    if not text:
        raise HomeError("What should the reminder say?")
    stored = _to_stored(due)
    db = SessionLocal()
    try:
        same = [n for n in _query(db, owner).filter(Note.archived == False).all()  # noqa: E712
                if _text(n).lower() == text.lower() and _parse(n.due_date) == _parse(stored)]
        if same:
            return dict(_view(same[0]), duplicate=True)
        note = Note(id=str(uuid.uuid4()), owner=owner, title=text, content="", note_type="todo", items=None,
                    due_date=stored, repeat=normal_repeat(repeat), source="home", label="reminder")
        db.add(note)
        db.commit()
        return _view(note)
    finally:
        db.close()


def update_reminder(owner: Optional[str], reminder_id: str, text: Optional[str] = None, due=None,
                    repeat=None, done: Optional[bool] = None) -> dict:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        note = _get(db, owner, reminder_id)
        if text is not None and squash(text):
            note.title = squash(text)[:300]
        if due is not None:
            note.due_date = _to_stored(due)
            note.archived = False
        if repeat is not None:
            note.repeat = normal_repeat(repeat)
        if done is True:
            current = _parse(note.due_date)
            upcoming = next_due(current, normal_repeat(note.repeat)) if current else None
            if upcoming is not None:
                note.due_date = upcoming.isoformat()         # it repeats: on to the next time
            else:
                note.archived = True
        elif done is False:
            note.archived = False
        db.commit()
        return _view(note)
    finally:
        db.close()


def snooze(owner: Optional[str], reminder_id: str, minutes: int = 60) -> dict:
    owner = owner_of(owner)
    minutes = max(1, min(int(minutes or 60), 60 * 24 * 14))
    db = SessionLocal()
    try:
        note = _get(db, owner, reminder_id)
        note.due_date = (datetime.now().astimezone() + timedelta(minutes=minutes)).replace(second=0, microsecond=0).isoformat()
        note.archived = False
        db.commit()
        return _view(note)
    finally:
        db.close()


def delete_reminder(owner: Optional[str], reminder_id: str) -> bool:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        db.delete(_get(db, owner, reminder_id))
        db.commit()
        return True
    finally:
        db.close()


def advance_after_firing(note: Note) -> bool:
    """Called by the reminder scanner once a note has been delivered: a
    repeating one moves to its next time (the caller commits). Returns
    whether anything changed."""
    repeat = str(note.repeat or "none").strip().lower()
    if repeat not in REPEATS or repeat == "none":
        return False
    due = _parse(note.due_date)
    upcoming = next_due(due, repeat) if due else None
    if upcoming is None:
        return False
    note.due_date = upcoming.isoformat()
    return True
