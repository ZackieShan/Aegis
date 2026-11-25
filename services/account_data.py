"""What an account leaves on the server.

A username is the ownership key of everything a person stores: chats, notes,
memory, lists, files. Deleting an account removes the sign-in; this module is
the other half. It says what is stored under a name, erases it, and moves the
file-based parts along when an account is renamed.

The account store keeps the name of a deleted account reserved until what it
left is erased here or deliberately handed on (see ``AuthManager.removed``),
so a new account never picks up someone else's data by accident.

Everything here matches on the exact, lower-cased account name. An empty name
is refused outright: that is how shared rows are stored.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# How the counts are grouped for people to read.
_CHAT_TABLES = ("sessions",)
_NOTE_TABLES = ("notes", "home_trackers", "home_boards", "calendars", "scheduled_tasks", "documents")
_NOT_COUNTED = ("chat_messages", "home_tracker_entries", "home_pins", "calendar_events", "task_runs",
                "document_versions", "user_tool_data")


def _name(username: Optional[str]) -> str:
    name = str(username or "").strip().lower()
    if not name:
        raise ValueError("an account name is required")
    return name


def _files_in(folder: Path) -> int:
    return sum(1 for path in folder.rglob("*") if path.is_file()) if folder.is_dir() else 0


# ── the database ──────────────────────────────────────────────────────────

def _tables():
    from core.database import Base
    try:                                        # the home tables register themselves on import
        import services.home.models  # noqa: F401
    except Exception:
        pass
    return Base.metadata.sorted_tables          # what is pointed at comes before what points at it


def _their_rows(table, username: str, _seen: Iterable[str] = ()):
    """A condition for the rows of ``table`` that belong to the account: its
    own rows where the table records whose they are, otherwise the rows that
    hang off one of its rows. None when nothing in the table can be theirs."""
    from sqlalchemy import func, or_, select

    if "owner" in table.c:
        return func.lower(table.c.owner) == username
    seen = set(_seen) | {table.name}
    parts = []
    for column in table.c:
        for key in column.foreign_keys:
            parent = key.column.table
            if parent.name in seen:
                continue
            theirs = _their_rows(parent, username, seen)
            if theirs is not None:
                parts.append(column.in_(select(key.column).where(theirs)))
    return or_(*parts) if parts else None


def _count_rows(username: str) -> Dict[str, int]:
    from sqlalchemy import func, select
    from core.database import SessionLocal

    counts: Dict[str, int] = {}
    db = SessionLocal()
    try:
        for table in _tables():
            theirs = _their_rows(table, username)
            if theirs is None:
                continue
            found = db.execute(select(func.count()).select_from(table).where(theirs)).scalar() or 0
            if found:
                counts[table.name] = int(found)
    finally:
        db.close()
    return counts


def _erase_rows(username: str) -> Dict[str, Any]:
    """Delete every row that belongs to the account, in one transaction,
    dependents first. Another account's row that merely points at one of
    theirs (a document filed under their chat) is detached, not deleted."""
    from sqlalchemy import delete, func, or_, select, update
    from core.database import Base, SessionLocal

    removed: Dict[str, int] = {}
    images: List[str] = []
    db = SessionLocal()
    try:
        tables = _tables()
        gallery = Base.metadata.tables.get("gallery_images")
        if gallery is not None:
            images = [row[0] for row in db.execute(
                select(gallery.c.filename).where(func.lower(gallery.c.owner) == username)) if row[0]]
        for table in reversed(tables):
            if "owner" in table.c:
                someone_elses = or_(table.c.owner.is_(None), func.lower(table.c.owner) != username)
                for column in table.c:
                    for key in column.foreign_keys:
                        target = _their_rows(key.column.table, username)
                        if target is None:
                            continue
                        pointing = column.in_(select(key.column).where(target)) & someone_elses
                        if column.nullable:
                            db.execute(update(table).where(pointing).values({column.name: None}))
                        else:
                            db.execute(delete(table).where(pointing))
            theirs = _their_rows(table, username)
            if theirs is not None:
                gone = db.execute(delete(table).where(theirs)).rowcount or 0
                if gone:
                    removed[table.name] = int(gone)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"rows": removed, "images": images}


# ── the stores kept in files ──────────────────────────────────────────────

def _facts(memory_manager, username: str) -> List[dict]:
    if memory_manager is None:
        return []
    return [m for m in memory_manager.load_all()
            if isinstance(m, dict) and str(m.get("owner") or "").strip().lower() == username]


def _mind_folder(memory_manager, username: str) -> Optional[Path]:
    from services.memory import store
    return store.mind_dir(memory_manager, username) if memory_manager is not None else None


def _profile_file(memory_manager, username: str) -> Optional[Path]:
    from services.memory import profile
    return profile.profile_path(username, memory_manager) if memory_manager is not None else None


def _pins_folder(username: str) -> Path:
    from services.home import boards
    return boards.pins_dir(username)


def _documents_folder(username: str) -> Optional[Path]:
    """The account's own folder of uploaded documents — None for a name that
    would land on the folder shared by a server without accounts."""
    from routes.personal_routes import _personal_upload_dir_for_owner
    from src.upload_handler import secure_filename

    segment = secure_filename(username)[:80]
    if not segment or segment == "local":
        return None
    folder = Path(_personal_upload_dir_for_owner(username, create=False))
    return folder if folder.name == segment else None


def _research_files(username: str) -> List[Path]:
    from src.constants import DEEP_RESEARCH_DIR

    found = []
    folder = Path(DEEP_RESEARCH_DIR)
    if folder.is_dir():
        for path in folder.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and str(data.get("owner") or "").strip().lower() == username:
                found.append(path)
    return found


def _skill_files(username: str) -> List[Path]:
    """SKILL.md files whose front matter names the account as owner."""
    from src.constants import SKILLS_DIR

    owner_line = re.compile(r"(?mi)^owner:\s*['\"]?" + re.escape(username) + r"['\"]?\s*$")
    found = []
    root = Path(SKILLS_DIR)
    if root.is_dir():
        for path in root.rglob("SKILL.md"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            front = text.split("\n---", 1)[0] if text.startswith("---") else ""
            if owner_line.search(front):
                found.append(path)
    return found


def _upload_entries(username: str) -> int:
    from src.constants import UPLOAD_DIR

    try:
        index = json.loads((Path(UPLOAD_DIR) / "uploads.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(index, dict):
        return 0
    return sum(1 for info in index.values()
               if isinstance(info, dict) and str(info.get("owner") or "").strip().lower() == username)


# ── what is there ─────────────────────────────────────────────────────────

def leftover(username: str, *, memory_manager=None) -> Dict[str, int]:
    """How much is stored under an account name, grouped the way a person
    would count it. ``total`` is zero only when nothing at all is left."""
    name = _name(username)
    rows = _count_rows(name)
    facts = len(_facts(memory_manager, name))
    deep = 0                                              # topics, digests, rules, history, the profile
    mind = _mind_folder(memory_manager, name)
    if mind is not None:
        deep += _files_in(mind)
    profile = _profile_file(memory_manager, name)
    if profile is not None and profile.is_file():
        deep += 1
    on_disk = _upload_entries(name) + _files_in(_pins_folder(name))
    folder = _documents_folder(name)
    if folder is not None:
        on_disk += _files_in(folder)
    loose = len(_research_files(name)) + len(_skill_files(name))
    grouped = set(_CHAT_TABLES) | set(_NOTE_TABLES) | set(_NOT_COUNTED) | {"gallery_images"}
    return {
        "chats": sum(rows.get(table, 0) for table in _CHAT_TABLES),
        "memories": facts,
        "notes": sum(rows.get(table, 0) for table in _NOTE_TABLES),
        "files": on_disk + rows.get("gallery_images", 0),
        "other": sum(count for table, count in rows.items() if table not in grouped) + loose + deep,
        "total": sum(rows.values()) + facts + deep + on_disk + loose,
    }


# ── erasing it ────────────────────────────────────────────────────────────

def erase(username: str, *, accounts: Iterable[str] = (), memory_manager=None, session_manager=None,
          memory_vector=None, upload_handler=None, personal_docs_manager=None) -> Dict[str, Any]:
    """Erase everything stored under an account name.

    ``accounts`` are the names that still have an account: the name being
    erased must not be one of them, and a folder that another account's name
    also maps to is left alone. Each store is handled on its own — one that
    fails is named in ``failed`` and the others still go. Returns
    ``{"removed": {what: how many}, "failed": [what]}``.
    """
    name = _name(username)
    live = {str(account or "").strip().lower() for account in accounts}
    if name in live:
        raise ValueError("that account still exists")
    removed: Dict[str, int] = {}
    failed: List[str] = []

    def step(label: str, work: Callable[[], int]) -> None:
        try:
            count = int(work() or 0)
            if count:
                removed[label] = removed.get(label, 0) + count
        except Exception:
            logger.exception("erasing account data: %s failed for %s", label, name)
            failed.append(label)

    def alone(folder_of: Callable[[str], Optional[Path]]) -> Optional[Path]:
        """This name's folder, unless a live account's name lands on the same one."""
        mine = folder_of(name)
        if mine is None:
            return None
        for other in live:
            try:
                if folder_of(other) == mine:
                    return None
            except Exception:
                return None
        return mine

    images: List[str] = []

    def rows() -> int:
        result = _erase_rows(name)
        images.extend(result["images"])
        for table, count in result["rows"].items():
            removed[table] = count
        if session_manager is not None:                       # chats are also held in memory
            held = getattr(session_manager, "sessions", None)
            if isinstance(held, dict):
                for session_id, session in list(held.items()):
                    if str(getattr(session, "owner", "") or "").strip().lower() == name:
                        held.pop(session_id, None)
        return 0

    def image_files() -> int:
        from src.constants import GENERATED_IMAGES_DIR
        root = Path(GENERATED_IMAGES_DIR).resolve()
        count = 0
        for filename in images:
            path = (root / str(filename)).resolve()
            if path.parent == root and path.is_file():        # a stored name never leaves the folder
                path.unlink()
                count += 1
        return count

    def facts() -> int:
        theirs = _facts(memory_manager, name)
        if not theirs:
            return 0
        ids = {m.get("id") for m in theirs}
        from services.memory import journal
        with journal.cause("system", detail="account erased"):
            memory_manager.save([m for m in memory_manager.load_all() if m.get("id") not in ids])
        if memory_vector is not None and getattr(memory_vector, "healthy", False):
            for memory_id in ids:
                try:
                    memory_vector.remove(memory_id)
                except Exception:
                    logger.warning("memory vector not removed for %s", memory_id, exc_info=True)
        return len(theirs)

    def deep_memory() -> int:
        count = 0
        profile = alone(lambda who: _profile_file(memory_manager, who))
        if profile is not None and profile.is_file():
            profile.unlink()
            count += 1
        folder = alone(lambda who: _mind_folder(memory_manager, who))
        if folder is not None and folder.is_dir():
            count += _files_in(folder)
            shutil.rmtree(folder)
        return count

    def pictures() -> int:
        folder = alone(_pins_folder)
        if folder is None or not folder.is_dir():
            return 0
        count = _files_in(folder)
        shutil.rmtree(folder)
        return count

    def uploads() -> int:
        if _upload_entries(name) == 0:
            return 0
        remove = getattr(upload_handler, "remove_owner", None)
        if not callable(remove):
            raise RuntimeError("the upload store is not available")
        return int(remove(name) or 0)

    def documents() -> int:
        folder = alone(_documents_folder)
        count = 0
        if folder is not None and folder.is_dir():
            count = _files_in(folder)
            if personal_docs_manager is not None:
                personal_docs_manager.remove_directory(str(folder))      # its search index entries too
            shutil.rmtree(folder)
        rag = getattr(personal_docs_manager, "rag_manager", None)
        remove = getattr(rag, "remove_owner", None)
        if callable(remove):
            remove(name)
        return count

    def research() -> int:
        found = _research_files(name)
        for path in found:
            path.unlink()
        return len(found)

    def skills() -> int:
        from src.constants import SKILLS_DIR
        root = Path(SKILLS_DIR)
        found = _skill_files(name)
        for path in found:
            folder = path.parent
            only_this = folder != root and [p for p in folder.rglob("SKILL.md")] == [path]
            if only_this:
                shutil.rmtree(folder)                         # the skill's own folder
            else:
                path.unlink()
        usage = root / "_usage.json"
        if found and usage.is_file():
            try:
                data = json.loads(usage.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    kept = {key: value for key, value in data.items()        # keys are "owner::skill"
                            if "::" not in key or key.partition("::")[0].strip().lower() != name}
                    if len(kept) != len(data):
                        from core.atomic_io import atomic_write_json
                        atomic_write_json(str(usage), kept)
            except (OSError, ValueError):
                pass
        return len(found)

    step("rows", rows)
    step("images", image_files)
    step("memories", facts)
    step("memory files", deep_memory)
    step("board pictures", pictures)
    step("uploads", uploads)
    step("documents", documents)
    step("research", research)
    step("skills", skills)
    step("settings", lambda: forget_account_settings(name))
    return {"removed": removed, "failed": failed}


def forget_account_settings(username: str) -> int:
    """Drop what belongs to the sign-in rather than to the person's data: the
    phones that get their notifications, their interface preferences and the
    record of their sign-ins. Done whenever an account is deleted — a later
    account of the same name must never get the old one's notifications."""
    name = _name(username)
    count = 0
    try:
        from services import push
        count += len(push.devices(name))
        push.forget(name)
    except Exception:
        logger.warning("push devices not forgotten for %s", name, exc_info=True)
    try:
        from routes import prefs_routes
        prefs = prefs_routes._load()
        users = prefs.get("_users") if isinstance(prefs, dict) else None
        if isinstance(users, dict):
            keys = [key for key in users if str(key).strip().lower() == name]
            for key in keys:
                users.pop(key, None)
            if keys:
                prefs_routes._save(prefs)
                count += len(keys)
    except Exception:
        logger.warning("preferences not forgotten for %s", name, exc_info=True)
    try:
        from core import access
        if access.forget_sign_ins(name):
            count += 1
    except Exception:
        logger.warning("sign-in record not forgotten for %s", name, exc_info=True)
    return count


# ── renaming ──────────────────────────────────────────────────────────────

def move_files(old_username: str, new_username: str, *, memory_manager=None) -> Dict[str, int]:
    """Move the file-based stores along when an account is renamed: the
    profile, the deep memory (topics, digests, rules, history), board
    pictures, notification devices and the sign-in record. The database rows
    and the other stores are renamed by the account routes."""
    old, new = _name(old_username), _name(new_username)
    moved: Dict[str, int] = {}
    if old == new:
        return moved

    def carry(label: str, source: Optional[Path], target: Optional[Path]) -> None:
        if source is None or target is None or source == target or not source.exists():
            return
        if target.exists():                                   # never write over what is already there
            logger.warning("rename %s -> %s: %s already exists, left where it was", old, new, target)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        moved[label] = 1

    try:
        carry("profile", _profile_file(memory_manager, old), _profile_file(memory_manager, new))
        carry("memory files", _mind_folder(memory_manager, old), _mind_folder(memory_manager, new))
        profile = _profile_file(memory_manager, new)
        if moved.get("profile") and profile is not None:      # the profile records whose it is
            try:
                data = json.loads(profile.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "owner" in data:
                    data["owner"] = new
                    from core.atomic_io import atomic_write_json
                    atomic_write_json(str(profile), data, mode=0o600)
            except (OSError, ValueError):
                pass
    except Exception:
        logger.warning("rename %s -> %s: memory files not moved", old, new, exc_info=True)
    try:
        carry("board pictures", _pins_folder(old), _pins_folder(new))
    except Exception:
        logger.warning("rename %s -> %s: board pictures not moved", old, new, exc_info=True)
    try:
        from services import push
        if push.rename(old, new):
            moved["devices"] = 1
    except Exception:
        logger.warning("rename %s -> %s: notification devices not moved", old, new, exc_info=True)
    try:
        from core import access
        if access.rename_sign_ins(old, new):
            moved["sign-ins"] = 1
    except Exception:
        logger.warning("rename %s -> %s: sign-in record not moved", old, new, exc_info=True)
    return moved
