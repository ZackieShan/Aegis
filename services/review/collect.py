"""The day's exchanges, read from the chat records. No model is asked and nothing is changed.

An *exchange* is one thing a person asked and what the assistant did about it: the tools it ran, what they
returned, and its reply. ``read`` fetches the day's chats from the database; ``exchanges`` turns them into
the list the review works through. The second is plain arithmetic on what the first returns, so it can be
tried on made-up chats.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

CONTEXT_DAYS = 2          # how far before the day to read, for the exchange that came just before
_THINK = re.compile(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>|<think(?:ing)?>[\s\S]*$", re.I)


def window(day: date, zone) -> Tuple[datetime, datetime]:
    """The day's first and last moment on the house's clock, as the naive UTC the records are kept in."""
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = start + timedelta(days=1)
    return (start.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None))


def text_of(content: Any) -> str:
    """A message's words. A message with a picture is kept as a list of parts; only the text parts count."""
    if isinstance(content, str) and content[:1] == "[":
        try:
            parts = json.loads(content)
        except ValueError:
            parts = None
        if isinstance(parts, list) and parts and all(isinstance(part, dict) and "type" in part for part in parts):
            content = parts               # otherwise it is ordinary text that happens to open with a bracket
    if isinstance(content, list):
        content = "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    return _THINK.sub("", str(content or "")).strip()


def _meta(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        found = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def read(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Every chat with a message between ``start`` and ``end`` (naive UTC), with its messages from a little
    before ``start`` up to ``end``, oldest first."""
    from sqlalchemy import text

    from core.database import SessionLocal

    db = SessionLocal()
    try:
        ids = [row[0] for row in db.execute(
            text("SELECT DISTINCT session_id FROM chat_messages WHERE timestamp >= :start AND timestamp < :end"),
            {"start": start, "end": end})]
        chats = []
        for session_id in ids:
            head = db.execute(text("SELECT owner, mode, model FROM sessions WHERE id = :id"), {"id": session_id}).first()
            if head is None:
                continue
            rows = db.execute(
                text("SELECT id, role, content, metadata, timestamp FROM chat_messages "
                     "WHERE session_id = :id AND timestamp >= :since AND timestamp < :end ORDER BY timestamp, id"),
                {"id": session_id, "since": start - timedelta(days=CONTEXT_DAYS), "end": end}).fetchall()
            chats.append({"id": session_id, "owner": head[0] or "", "mode": head[1] or "", "model": head[2] or "",
                          "messages": [{"id": row[0], "role": row[1], "content": row[2], "meta": _meta(row[3]), "at": _moment(row[4])}
                                       for row in rows]})
        return chats
    finally:
        db.close()


def _moment(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _tools(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    ran = []
    for event in meta.get("tool_events") or []:
        if not isinstance(event, dict) or not event.get("tool"):
            continue
        ran.append({"tool": str(event["tool"]), "args": str(event.get("command") or "")[:600],
                    "ok": event.get("exit_code") in (0, None), "out": str(event.get("output") or "")[:1500]})
    return ran


def exchanges(chats: Iterable[Dict[str, Any]], start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """The exchanges that began between ``start`` and ``end``, oldest first within each chat.

    Slash commands (``/run ls`` and the like) are left out: the person ran a command themselves and got its
    output, there is nothing of the assistant's to rate.
    """
    found: List[Dict[str, Any]] = []
    for chat in chats:
        turns: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        for message in chat.get("messages") or []:
            role, meta = message.get("role"), message.get("meta") or {}
            if role == "user":
                if meta.get("source") == "slash":
                    current = None
                    continue
                current = {"asked": text_of(message.get("content")), "at": message.get("at"), "user_id": message.get("id"),
                           "pictures": len(meta.get("attachments") or []), "reply": "", "tools": [], "meta": {}, "id": message.get("id")}
                turns.append(current)
            elif role == "assistant" and current is not None and meta.get("source") != "slash":
                words = text_of(message.get("content"))
                current["reply"] = (current["reply"] + "\n\n" + words).strip() if current["reply"] else words
                current["tools"].extend(_tools(meta))
                current["meta"] = {**current["meta"], **{key: meta[key] for key in (
                    "model", "requested_model", "response_time", "time_to_first_token", "stopped", "cancelled", "input_tokens",
                    "output_tokens", "read_tokens", "cached_tokens") if key in meta}}
                if meta.get("variants"):
                    current["meta"]["redone"] = max(0, len(meta["variants"]) - 1)
                current["id"] = message.get("id") or current["id"]
        for number, turn in enumerate(turns):
            at = turn.get("at")
            if at is None or not (start <= at < end) or not turn["asked"]:
                continue
            before = turns[number - 1] if number else None
            after = turns[number + 1] if number + 1 < len(turns) else None
            found.append({
                "id": str(turn["id"]), "chat": str(chat.get("id") or ""), "owner": str(chat.get("owner") or ""),
                "mode": str(chat.get("mode") or ""), "at": at, "turn": number + 1,
                "asked": turn["asked"], "reply": turn["reply"], "tools": turn["tools"], "meta": turn["meta"], "pictures": turn["pictures"],
                # The name the model was asked for by. (What the model server reports back is often a file path.)
                "model": str(turn["meta"].get("requested_model") or chat.get("model") or turn["meta"].get("model") or ""),
                "before": (before["asked"], before["reply"]) if before else None,
                "next": after["asked"] if after else "",
            })
    found.sort(key=lambda one: (one["chat"], one["at"]))
    return _without_copies(found)


def _without_copies(found: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Forking a chat copies its messages into the new one. An exchange that is such a copy — the same
    person, the same words, the same reply, timed to the same tenth of a second — is counted once, in the
    chat it happened in first."""
    kept: Dict[tuple, Dict[str, Any]] = {}
    for one in sorted(found, key=lambda one: one["at"]):
        mark = (one["owner"], one["asked"], one["reply"][:400], one["meta"].get("response_time"))
        if one["meta"].get("response_time") is None or mark not in kept:
            kept.setdefault(mark if one["meta"].get("response_time") is not None else (id(one),), one)
    ids = {id(one) for one in kept.values()}
    return [one for one in found if id(one) in ids]
