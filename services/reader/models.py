"""Tables for the reader: what a person has opened, and what they marked in it."""

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text, UniqueConstraint

from core.database import Base, engine, utcnow_naive


class ReaderItem(Base):
    """One library document on a person's shelf: an article, a manual, an
    added document. It remembers where they stopped."""
    __tablename__ = "reader_items"
    __table_args__ = (UniqueConstraint("owner", "book", "path", name="uq_reader_item"),)

    id = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    kind = Column(String, default="article")       # article | manual
    book = Column(String, nullable=False)          # the collection it is in
    path = Column(String, nullable=False)          # its path inside the collection
    title = Column(String, default="")
    pages = Column(Integer, default=0)             # 0 for an article
    page = Column(Integer, default=1)              # the page they were on
    position = Column(Float, default=0.0)          # how far down an article, 0..1
    pinned = Column(Boolean, default=False)        # kept on the shelf (bookmarked)
    opened_at = Column(DateTime, default=utcnow_naive, nullable=False, index=True)
    created_at = Column(DateTime, default=utcnow_naive, nullable=False)


class ReaderMark(Base):
    """A highlight (with an optional note). The place is kept three ways so it
    can be found again: the page, the character range within that page's text,
    and the quoted words with a little of what surrounds them."""
    __tablename__ = "reader_marks"

    id = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    item_id = Column(String, nullable=False, index=True)
    page = Column(Integer, default=0)              # 0 for an article
    start = Column(Integer, default=0)
    end = Column(Integer, default=0)
    quote = Column(Text, default="")
    prefix = Column(String, default="")
    suffix = Column(String, default="")
    note = Column(Text, default="")
    color = Column(String, default="yellow")
    shared_at = Column(DateTime, nullable=True)    # handed to Aegis's memory
    shared_topic = Column(String, default="")
    created_at = Column(DateTime, default=utcnow_naive, nullable=False, index=True)


class KitchenRecipe(Base):
    """A recipe set out for cooking from: the original words, the ingredients
    with today's measures, and the steps one at a time."""
    __tablename__ = "kitchen_recipes"

    id = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    title = Column(String, nullable=False)
    source_title = Column(String, default="")
    source_url = Column(String, default="")
    year = Column(String, default="")
    original = Column(Text, default="")            # the text it was made from, untouched
    data = Column(Text, default="{}")              # JSON: ingredients, steps, notes, yield
    favourite = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow_naive, nullable=False, index=True)
    updated_at = Column(DateTime, default=utcnow_naive, nullable=False)


def epoch(moment) -> "float | None":
    """A stored time as seconds since 1970. Times are kept as UTC without a
    zone; read as local time they would land hours off (and "today" would
    turn into "tomorrow" on the shelf)."""
    if moment is None:
        return None
    from datetime import timezone
    return (moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment).timestamp()


def ensure_tables() -> None:
    try:
        Base.metadata.create_all(bind=engine, tables=[ReaderItem.__table__, ReaderMark.__table__, KitchenRecipe.__table__])
    except Exception:  # pragma: no cover - a read-only or stubbed database
        pass


ensure_tables()
