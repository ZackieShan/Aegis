"""The record of every change to what Aegis knows about a user.

Append-only, one file per account (``<data>/mind/<user>/journal.jsonl``).
Each line says what changed, the value before and after, when, and who or
what caused it:

    by = "you"        the user, through a screen or a command
         "assistant"  the agent, using its memory tool on the user's behalf
         "auto"       learned in the background from a conversation
         "tidy"       a duplicate-merging pass
         "import"     an import the user started
         "undo"       the user reversing an earlier entry
         "system"     anything else (migrations, older code paths)

Single facts are journaled where they are written — ``MemoryManager.save``
compares the list it is about to write with the one on disk — so no code
path can change a memory without leaving a line here. Topics, conversation
digests, the profile and the user's rules call :func:`record` themselves.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

from services.memory import store

logger = logging.getLogger(__name__)

_cause: contextvars.ContextVar = contextvars.ContextVar("memory_change_cause", default=None)

KINDS = ("memory", "topic", "episode", "profile", "rules")
MAX_BYTES = 4 * 1024 * 1024        # past this the oldest half is dropped
_TRACKED = ("text", "category", "pinned")


@contextmanager
def cause(by: str, session_id: Optional[str] = None, detail: Optional[str] = None):
    """Attribute every change made inside the block."""
    value: Dict[str, Any] = {"by": by}
    if session_id:
        value["session_id"] = str(session_id)
    if detail:
        value["detail"] = str(detail)[:300]
    token = _cause.set(value)
    try:
        yield
    finally:
        _cause.reset(token)


def current_cause() -> Dict[str, Any]:
    return dict(_cause.get() or {"by": "system"})


def _path(memory_manager, owner: Optional[str]):
    folder = store.mind_dir(memory_manager, owner)
    return folder / "journal.jsonl" if folder is not None else None


def _entry(kind: str, op: str, target: Optional[str], before: Any, after: Any, note: Optional[str],
           label: Optional[str]) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "id": uuid.uuid4().hex[:12], "ts": time.time(), "kind": kind, "op": op,
        "target": target, "cause": current_cause(),
    }
    if before is not None:
        entry["before"] = before
    if after is not None:
        entry["after"] = after
    if note:
        entry["note"] = str(note)[:400]
    if label:
        entry["label"] = str(label)[:200]
    return entry


def _write(memory_manager, owner: Optional[str], entries: List[dict]) -> None:
    path = _path(memory_manager, owner)
    if path is None or not entries:
        return
    try:
        store.append_lines(path, [json.dumps(e, ensure_ascii=False) for e in entries])
        if path.stat().st_size > MAX_BYTES:
            with store.lock_for(path):
                lines = path.read_text(encoding="utf-8").splitlines()
                path.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("memory journal not written: %s", exc)


def record(memory_manager, owner: Optional[str], kind: str, op: str, target: Optional[str] = None,
           before: Any = None, after: Any = None, note: Optional[str] = None,
           label: Optional[str] = None) -> Optional[dict]:
    """Write one line. ``label`` is a short human description of the thing
    changed (a topic's name, a conversation's title)."""
    entry = _entry(kind, op, target, before, after, note, label)
    _write(memory_manager, owner, [entry])
    return entry


def _snapshot(memory: dict) -> dict:
    snap = {"text": memory.get("text"), "category": memory.get("category") or "fact",
            "pinned": bool(memory.get("pinned")), "source": memory.get("source")}
    for key in ("session_id", "quote", "timestamp"):
        if memory.get(key):
            snap[key] = memory[key]
    return snap


def record_memory_diff(memory_manager, old: Iterable[dict], new: Iterable[dict]) -> int:
    """Journal the difference between the memory list on disk and the one
    about to replace it. Use counts and other bookkeeping are not changes."""
    if store.base_dir(memory_manager) is None:
        return 0
    before = {m["id"]: m for m in old or [] if isinstance(m, dict) and m.get("id")}
    after = {m["id"]: m for m in new or [] if isinstance(m, dict) and m.get("id")}
    by_owner: Dict[Optional[str], List[dict]] = {}

    def add(owner, op, memory_id, was, now):
        by_owner.setdefault(owner, []).append(_entry("memory", op, memory_id, was, now, None, None))

    for memory_id, memory in after.items():
        previous = before.get(memory_id)
        if previous is None:
            add(memory.get("owner"), "add", memory_id, None, _snapshot(memory))
            continue
        changed = [key for key in _TRACKED
                   if (bool(previous.get(key)) if key == "pinned" else previous.get(key))
                   != (bool(memory.get(key)) if key == "pinned" else memory.get(key))]
        if not changed:
            continue
        if changed == ["pinned"]:
            op = "pin" if memory.get("pinned") else "unpin"
        else:
            op = "edit"
        add(memory.get("owner"), op, memory_id, _snapshot(previous), _snapshot(memory))
    for memory_id, memory in before.items():
        if memory_id not in after:
            add(memory.get("owner"), "delete", memory_id, _snapshot(memory), None)
    for owner, entries in by_owner.items():
        _write(memory_manager, owner, entries)
    return sum(len(entries) for entries in by_owner.values())


def read(memory_manager, owner: Optional[str], limit: int = 100, before_ts: Optional[float] = None,
         kinds: Optional[Iterable[str]] = None, target: Optional[str] = None,
         by: Optional[str] = None) -> List[dict]:
    """Newest first."""
    path = _path(memory_manager, owner)
    if path is None:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    wanted = set(kinds) if kinds else None
    out: List[dict] = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if before_ts is not None and float(entry.get("ts") or 0) >= before_ts:
            continue
        if wanted is not None and entry.get("kind") not in wanted:
            continue
        if target is not None and entry.get("target") != target:
            continue
        if by is not None and (entry.get("cause") or {}).get("by") != by:
            continue
        out.append(entry)
        if len(out) >= max(1, int(limit)):
            break
    return out


def count(memory_manager, owner: Optional[str]) -> int:
    path = _path(memory_manager, owner)
    if path is None:
        return 0
    try:
        with open(path, "rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def get(memory_manager, owner: Optional[str], entry_id: str) -> Optional[dict]:
    for entry in read(memory_manager, owner, limit=10 ** 9):
        if entry.get("id") == entry_id:
            return entry
    return None


def undo_memory_change(memory_manager, owner: Optional[str], entry_id: str) -> Dict[str, Any]:
    """Reverse one journaled change to a single fact.

    Returns ``{"ok", "action", "memory_id", "text"}`` — ``action`` is
    "removed", "restored" or "reverted" so the caller can bring the vector
    index in line — or ``{"ok": False, "error": …}``.
    """
    entry = get(memory_manager, owner, entry_id)
    if not entry or entry.get("kind") != "memory":
        return {"ok": False, "error": "That entry cannot be undone."}
    memory_id = entry.get("target")
    everything = memory_manager.load_all()
    mine = (lambda m: m.get("owner") == owner) if owner is not None else (lambda m: True)
    current = next((m for m in everything if m.get("id") == memory_id and mine(m)), None)
    op = entry.get("op")
    with cause("undo", detail=entry_id):
        if op == "add":
            if current is None:
                return {"ok": False, "error": "That memory is already gone."}
            memory_manager.save([m for m in everything if m is not current])
            return {"ok": True, "action": "removed", "memory_id": memory_id, "text": current.get("text")}
        was = entry.get("before") or {}
        if not was.get("text"):
            return {"ok": False, "error": "Nothing to restore."}
        if op == "delete":
            if current is not None:
                return {"ok": False, "error": "That memory already exists."}
            restored = {"id": memory_id, "text": was["text"], "category": was.get("category") or "fact",
                        "source": was.get("source") or "user", "timestamp": int(was.get("timestamp") or time.time()),
                        "uses": 0}
            if owner is not None:
                restored["owner"] = owner
            if was.get("pinned"):
                restored["pinned"] = True
            for key in ("session_id", "quote"):
                if was.get(key):
                    restored[key] = was[key]
            memory_manager.save(everything + [restored])
            return {"ok": True, "action": "restored", "memory_id": memory_id, "text": restored["text"]}
        if op in ("edit", "pin", "unpin"):
            if current is None:
                return {"ok": False, "error": "That memory no longer exists."}
            current["text"] = was["text"]
            current["category"] = was.get("category") or current.get("category") or "fact"
            if was.get("pinned"):
                current["pinned"] = True
            else:
                current.pop("pinned", None)
            memory_manager.save(everything)
            return {"ok": True, "action": "reverted", "memory_id": memory_id, "text": current["text"]}
    return {"ok": False, "error": "That entry cannot be undone."}


def wipe(memory_manager, owner: Optional[str]) -> None:
    path = _path(memory_manager, owner)
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass
