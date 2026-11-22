"""Where a user's memory files live, and how they are read and written.

Everything Aegis keeps about a person sits beside the memory file:

    <data>/memory.json                 single facts, all accounts
    <data>/profiles/<user>.json        the profile written from them
    <data>/mind/<user>/topics.json     what is known about each subject
    <data>/mind/<user>/episodes.json   a digest of each conversation
    <data>/mind/<user>/rules.json      what the user told it not to keep, and their own edits
    <data>/mind/<user>/journal.jsonl   every change, in order

A test double standing in for the memory store has no real path, and then
nothing is read or written at all.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

_locks: dict = {}
_locks_guard = threading.Lock()


def safe_owner(owner: Optional[str]) -> str:
    name = re.sub(r"[^a-z0-9._-]", "_", str(owner or "").strip().lower()).strip(".")
    return name or "_single_user"


def base_dir(memory_manager) -> Optional[Path]:
    """The folder holding the memory file, or None when there is no real store."""
    memory_file = getattr(memory_manager, "memory_file", None)
    # A real str/Path only: a MagicMock answers to os.PathLike as well.
    if isinstance(memory_file, (str, Path)) and str(memory_file):
        return Path(memory_file).parent
    return None


def mind_dir(memory_manager, owner: Optional[str]) -> Optional[Path]:
    base = base_dir(memory_manager)
    return base / "mind" / safe_owner(owner) if base is not None else None


def lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def read_json(path: Optional[Path], default: Any) -> Any:
    if path is None:
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def write_json(path: Optional[Path], data: Any) -> None:
    """Atomic, owner-only. Silently does nothing without a real store."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(data, out, indent=1, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def append_lines(path: Optional[Path], lines: list) -> None:
    if path is None or not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with lock_for(path):
        with open(path, "a", encoding="utf-8") as out:
            for line in lines:
                out.write(line.rstrip("\n") + "\n")
        if new:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
