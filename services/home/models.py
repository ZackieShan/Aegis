"""Tables for trackers and boards (lists and reminders live in ``notes``)."""

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text

from core.database import Base, TimestampMixin, engine, utcnow_naive


class HomeTracker(TimestampMixin, Base):
    """Something followed over time: a habit ticked off each day, a number
    logged (glasses of water, kilometres), or a 1–5 rating (mood, sleep)."""
    __tablename__ = "home_trackers"

    id = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False)
    kind = Column(String, default="number")        # number | check | scale
    unit = Column(String, default="")
    goal = Column(Float, nullable=True)            # per day; None = no goal
    color = Column(String, default="")
    archived = Column(Boolean, default=False)
    sort_order = Column(Integer, default=0)


class HomeTrackerEntry(Base):
    __tablename__ = "home_tracker_entries"

    id = Column(String, primary_key=True, index=True)
    tracker_id = Column(String, nullable=False, index=True)
    owner = Column(String, nullable=True, index=True)
    day = Column(String, nullable=False, index=True)   # YYYY-MM-DD on the person's own calendar
    value = Column(Float, default=1.0)
    note = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow_naive, nullable=False)


class HomeBoard(TimestampMixin, Base):
    """A board of pins: pictures, links and notes collected around one idea."""
    __tablename__ = "home_boards"

    id = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, default="")
    sort_order = Column(Integer, default=0)


class HomePin(TimestampMixin, Base):
    __tablename__ = "home_pins"

    id = Column(String, primary_key=True, index=True)
    board_id = Column(String, nullable=False, index=True)
    owner = Column(String, nullable=True, index=True)
    kind = Column(String, default="note")          # image | link | note
    title = Column(String, default="")
    note = Column(Text, default="")
    url = Column(String, default="")               # where it came from
    image = Column(String, default="")             # stored file name ("" = no picture)
    width = Column(Integer, default=0)
    height = Column(Integer, default=0)
    sort_order = Column(Integer, default=0)


def ensure_tables() -> None:
    """Create the tables if they are not there yet (safe to call again)."""
    Base.metadata.create_all(bind=engine, tables=[
        HomeTracker.__table__, HomeTrackerEntry.__table__, HomeBoard.__table__, HomePin.__table__,
    ])


ensure_tables()
