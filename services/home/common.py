"""Shared bits for the home services."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional


class HomeError(Exception):
    """Something the person (or the assistant) can be told in plain words."""


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def owner_of(value: Optional[str]) -> Optional[str]:
    """Routes hand in "" when sign-in is off; rows then carry no owner."""
    return value or None


def scoped(query, model, owner: Optional[str]):
    """Only this account's rows (rows without an owner in single-user mode)."""
    if owner is None:
        return query.filter(model.owner.is_(None))
    return query.filter(model.owner == owner)


def squash(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def best_match(items, name: str, key=lambda item: item.name):
    """The item a person means by ``name``: exact (ignoring case and
    punctuation), else the only one whose name starts with or contains it."""
    wanted = norm(name)
    if not wanted:
        return None
    exact = [item for item in items if norm(key(item)) == wanted]
    if exact:
        return exact[0]
    for test in (lambda n: n.startswith(wanted), lambda n: wanted in n, lambda n: n in wanted and len(n) >= 3):
        found = [item for item in items if test(norm(key(item)))]
        if len(found) == 1:
            return found[0]
    return None


def user_today() -> str:
    """Today's date on the calendar of whoever is asking."""
    try:
        from src.user_time import now_user_local
        return now_user_local().strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def iso(value) -> Optional[str]:
    """A stored naive-UTC timestamp as an ISO string a browser can parse."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()
