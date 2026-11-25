"""What a person has translated lately — theirs alone, and theirs to clear.

The Translate page keeps the last translations so one can be shown again
(the phrase for the pharmacy, the sign from this morning). A picture is not
kept, only what it said. The assistant's ``translate`` tool keeps nothing.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, DateTime, String, Text

from core.database import Base, SessionLocal, engine, utcnow_naive

KEEP = 200                     # per person
MAX_TEXT = 6000


class Translation(Base):
    __tablename__ = "translations"

    id = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    kind = Column(String, default="text")          # text | photo
    source = Column(String, default="")
    target = Column(String, default="")
    text = Column(Text, default="")                # what was given ("" for a photo)
    result = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow_naive, nullable=False, index=True)


def ensure_tables() -> None:
    try:
        Base.metadata.create_all(bind=engine, tables=[Translation.__table__])
    except Exception:  # pragma: no cover - a read-only or stubbed database
        pass


ensure_tables()


def _owner(value: Optional[str]) -> Optional[str]:
    return value or None


def _view(row: Translation) -> Dict[str, Any]:
    from . import languages
    from services.reader.models import epoch

    return {"id": row.id, "kind": row.kind, "source": row.source, "source_name": languages.name(row.source),
            "target": row.target, "target_name": languages.name(row.target), "text": row.text, "result": row.result,
            "at": epoch(row.created_at)}


def add(owner: Optional[str], *, kind: str, source: str, target: str, text: str, result: str) -> Dict[str, Any]:
    with SessionLocal() as db:
        row = Translation(id=uuid.uuid4().hex, owner=_owner(owner), kind="photo" if kind == "photo" else "text",
                          source=str(source or "")[:12], target=str(target or "")[:12], text=str(text or "")[:MAX_TEXT],
                          result=str(result or "")[:MAX_TEXT])
        db.add(row)
        db.commit()
        old = (db.query(Translation).filter(Translation.owner == _owner(owner))
               .order_by(Translation.created_at.desc()).offset(KEEP).all())
        for stale in old:
            db.delete(stale)
        if old:
            db.commit()
        return _view(row)


def recent(owner: Optional[str], limit: int = 30) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        rows = (db.query(Translation).filter(Translation.owner == _owner(owner))
                .order_by(Translation.created_at.desc()).limit(max(1, min(int(limit), KEEP))).all())
        return [_view(row) for row in rows]


def remove(owner: Optional[str], item_id: str) -> bool:
    with SessionLocal() as db:
        row = db.query(Translation).filter(Translation.id == item_id, Translation.owner == _owner(owner)).first()
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True


def clear(owner: Optional[str]) -> int:
    with SessionLocal() as db:
        count = db.query(Translation).filter(Translation.owner == _owner(owner)).delete()
        db.commit()
        return int(count or 0)
