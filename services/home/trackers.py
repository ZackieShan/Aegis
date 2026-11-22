"""Trackers: something followed day by day.

* ``check``  — done or not done today (took vitamins, walked the dog)
* ``number`` — an amount that adds up over the day (glasses of water, steps)
* ``scale``  — one rating a day from 1 to 5 (mood, sleep, energy)

Days are the person's own calendar days.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from core.database import SessionLocal

from .common import HomeError, best_match, iso, new_id, owner_of, scoped, squash, user_today
from .models import HomeTracker, HomeTrackerEntry

KINDS = ("check", "number", "scale")
MAX_TRACKERS = 60


def _day(value: Optional[str]) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "today":
        return user_today()
    if text.lower() == "yesterday":
        return (date.fromisoformat(user_today()) - timedelta(days=1)).isoformat()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        raise HomeError(f"“{text}” is not a date I can use (try 2026-11-22, today or yesterday).")


def _totals(kind: str, entries: List[HomeTrackerEntry]) -> Dict[str, float]:
    """day → value. Amounts add up; a check is 1; a rating is the day's last."""
    out: Dict[str, float] = {}
    for entry in sorted(entries, key=lambda e: e.created_at or datetime.min):
        value = float(entry.value or 0)
        if kind == "number":
            out[entry.day] = round(out.get(entry.day, 0.0) + value, 4)
        elif kind == "check":
            out[entry.day] = 1.0 if value > 0 else out.get(entry.day, 0.0)
        else:
            out[entry.day] = value
    return out


def _met(tracker: HomeTracker, value: float) -> bool:
    if tracker.kind == "number" and tracker.goal:
        return value >= float(tracker.goal)
    return value > 0


def _view(tracker: HomeTracker, entries: List[HomeTrackerEntry], today: str, days: int = 14) -> dict:
    totals = _totals(tracker.kind, entries)
    end = date.fromisoformat(today)
    series = []
    for back in range(days - 1, -1, -1):
        day = (end - timedelta(days=back)).isoformat()
        series.append({"day": day, "value": totals.get(day, 0.0)})
    streak = 0
    cursor = end if _met(tracker, totals.get(today, 0.0)) else end - timedelta(days=1)
    while _met(tracker, totals.get(cursor.isoformat(), 0.0)):
        streak += 1
        cursor -= timedelta(days=1)
    week = [totals.get((end - timedelta(days=back)).isoformat(), 0.0) for back in range(7)]
    logged = [v for v in week if v]
    return {
        "id": tracker.id, "name": tracker.name, "kind": tracker.kind, "unit": tracker.unit or "",
        "goal": tracker.goal, "color": tracker.color or "", "archived": bool(tracker.archived),
        "today": totals.get(today, 0.0), "done_today": _met(tracker, totals.get(today, 0.0)),
        "streak": streak, "series": series,
        "week_total": round(sum(week), 2), "week_average": round(sum(logged) / len(logged), 2) if logged else 0.0,
        "days_logged": len(totals), "updated": iso(tracker.updated_at),
    }


def _get(db, owner: Optional[str], tracker_id: str) -> HomeTracker:
    tracker = scoped(db.query(HomeTracker), HomeTracker, owner).filter(HomeTracker.id == tracker_id).first()
    if tracker is None:
        raise HomeError("That tracker no longer exists.")
    return tracker


def _entries(db, tracker_id: str, since: Optional[str] = None) -> List[HomeTrackerEntry]:
    q = db.query(HomeTrackerEntry).filter(HomeTrackerEntry.tracker_id == tracker_id)
    if since:
        q = q.filter(HomeTrackerEntry.day >= since)
    return q.all()


def all_trackers(owner: Optional[str], archived: bool = False) -> List[dict]:
    owner = owner_of(owner)
    today = user_today()
    since = (date.fromisoformat(today) - timedelta(days=400)).isoformat()
    db = SessionLocal()
    try:
        trackers = scoped(db.query(HomeTracker), HomeTracker, owner).filter(HomeTracker.archived == archived).all()  # noqa: E712
        trackers.sort(key=lambda t: (t.sort_order or 0, t.created_at or datetime.min))
        return [_view(t, _entries(db, t.id, since), today) for t in trackers]
    finally:
        db.close()


def get_tracker(owner: Optional[str], tracker_id: str, days: int = 60) -> dict:
    owner = owner_of(owner)
    today = user_today()
    db = SessionLocal()
    try:
        tracker = _get(db, owner, tracker_id)
        entries = _entries(db, tracker.id)
        view = _view(tracker, entries, today, days=max(7, min(int(days or 60), 366)))
        recent = sorted(entries, key=lambda e: (e.day, e.created_at or datetime.min), reverse=True)[:60]
        view["entries"] = [{"id": e.id, "day": e.day, "value": e.value, "note": e.note or ""} for e in recent]
        return view
    finally:
        db.close()


def find_tracker(owner: Optional[str], name: str) -> Optional[dict]:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        trackers = scoped(db.query(HomeTracker), HomeTracker, owner).filter(HomeTracker.archived == False).all()  # noqa: E712
        match = best_match(trackers, name)
        return _view(match, _entries(db, match.id), user_today()) if match is not None else None
    finally:
        db.close()


def create_tracker(owner: Optional[str], name: str, kind: str = "check", unit: str = "",
                   goal: Optional[float] = None, color: str = "") -> dict:
    owner = owner_of(owner)
    name = squash(name)[:60]
    if not name:
        raise HomeError("Give the tracker a name.")
    kind = kind if kind in KINDS else "check"
    db = SessionLocal()
    try:
        existing = scoped(db.query(HomeTracker), HomeTracker, owner).all()
        if len(existing) >= MAX_TRACKERS:
            raise HomeError(f"There can be at most {MAX_TRACKERS} trackers.")
        same = next((t for t in existing if t.name.lower() == name.lower() and not t.archived), None)
        if same is not None:
            return dict(_view(same, _entries(db, same.id), user_today()), duplicate=True)
        tracker = HomeTracker(id=new_id(), owner=owner, name=name, kind=kind, unit=squash(unit)[:20],
                              goal=float(goal) if goal not in (None, "") and kind == "number" else None,
                              color=squash(color)[:9], sort_order=len(existing))
        db.add(tracker)
        db.commit()
        return _view(tracker, [], user_today())
    finally:
        db.close()


def update_tracker(owner: Optional[str], tracker_id: str, **fields) -> dict:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        tracker = _get(db, owner, tracker_id)
        if squash(fields.get("name")):
            tracker.name = squash(fields["name"])[:60]
        if "unit" in fields and fields["unit"] is not None:
            tracker.unit = squash(fields["unit"])[:20]
        if "goal" in fields:
            tracker.goal = float(fields["goal"]) if fields["goal"] not in (None, "", 0) else None
        if fields.get("color") is not None:
            tracker.color = squash(fields["color"])[:9]
        if isinstance(fields.get("archived"), bool):
            tracker.archived = fields["archived"]
        db.commit()
        return _view(tracker, _entries(db, tracker.id), user_today())
    finally:
        db.close()


def log(owner: Optional[str], tracker_id: str, value: Optional[float] = None, note: str = "",
        day: Optional[str] = None) -> dict:
    """Record something for a day (today when omitted).

    check:  no value = done; value 0 = not done after all.
    number: the amount to add (may be negative to correct).
    scale:  the day's rating, 1–5; logging again replaces it.
    """
    owner = owner_of(owner)
    day = _day(day)
    db = SessionLocal()
    try:
        tracker = _get(db, owner, tracker_id)
        same_day = [e for e in _entries(db, tracker.id, day) if e.day == day]
        if tracker.kind == "check":
            done = value is None or float(value) > 0
            for entry in same_day:
                db.delete(entry)
            if done:
                db.add(HomeTrackerEntry(id=new_id(), tracker_id=tracker.id, owner=owner, day=day, value=1.0,
                                        note=squash(note)[:300]))
        elif tracker.kind == "scale":
            if value is None or not 1 <= float(value) <= 5:
                raise HomeError("A rating is a number from 1 to 5.")
            for entry in same_day:
                db.delete(entry)
            db.add(HomeTrackerEntry(id=new_id(), tracker_id=tracker.id, owner=owner, day=day, value=float(value),
                                    note=squash(note)[:300]))
        else:
            amount = 1.0 if value is None else float(value)
            if amount == 0:
                raise HomeError("How much should I add?")
            db.add(HomeTrackerEntry(id=new_id(), tracker_id=tracker.id, owner=owner, day=day, value=amount,
                                    note=squash(note)[:300]))
        tracker.updated_at = datetime.utcnow()
        db.commit()
        return _view(tracker, _entries(db, tracker.id), user_today())
    finally:
        db.close()


def delete_entry(owner: Optional[str], tracker_id: str, entry_id: str) -> dict:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        tracker = _get(db, owner, tracker_id)
        entry = db.query(HomeTrackerEntry).filter(HomeTrackerEntry.id == entry_id,
                                                  HomeTrackerEntry.tracker_id == tracker.id).first()
        if entry is None:
            raise HomeError("That entry is already gone.")
        db.delete(entry)
        db.commit()
        return _view(tracker, _entries(db, tracker.id), user_today())
    finally:
        db.close()


def delete_tracker(owner: Optional[str], tracker_id: str) -> bool:
    owner = owner_of(owner)
    db = SessionLocal()
    try:
        tracker = _get(db, owner, tracker_id)
        db.query(HomeTrackerEntry).filter(HomeTrackerEntry.tracker_id == tracker.id).delete()
        db.delete(tracker)
        db.commit()
        return True
    finally:
        db.close()
