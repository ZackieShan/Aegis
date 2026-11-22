"""A living profile of each user: who they are and what they care about.

Memories are individual facts picked out of conversations; on any one turn
only the few that match the message are recalled. The profile is the
standing picture built from all of them — identity, the people in the user's
life, preferences, interests (each with what is known about it), projects and
goals — and it is given to the model on **every** turn, so the assistant
knows who it is talking to without being asked.

* One profile per account, stored beside the memory file it is derived from
  (``data/profiles/<user>.json``); nothing is shared between users.
* Rebuilt in the background, by the same model that extracts memories, when
  that user's memories have grown. If the model is unavailable the profile
  is assembled from the memories directly — cruder, never wrong.
* Read and written only when memory is on: "Nobody" mode, the per-user memory
  switch and ``no_memory`` requests all skip it.
* It is derived data, and it never outlives its sources: a profile remembers
  which memories it was written from, and as soon as one of them is deleted
  or edited the written profile is set aside for one assembled from what is
  left. Deleting the file just causes a rebuild.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

VERSION = 1
SOURCE_LABEL = "saved memory: user profile"     # how the block is labelled in the prompt
LIST_SECTIONS = ("identity", "people", "preferences", "projects", "goals")
_CAPS = {"identity": 10, "people": 10, "preferences": 12, "projects": 8, "goals": 8}
_LABELS = (("identity", "About them", "About you"), ("people", "People", "People"),
           ("preferences", "Preferences", "Preferences"), ("projects", "Projects", "Projects"),
           ("goals", "Goals", "Goals"))
MAX_INTERESTS = 12
MAX_NOTES = 6
MAX_ITEM_CHARS = 180
MAX_SUMMARY_CHARS = 600

# Which memory categories feed which section when no model is available.
_CATEGORY_SECTION = {
    "identity": "identity", "contact": "people", "preference": "preferences",
    "project": "projects", "goal": "goals",
}
# What the model is shown: per-category caps (newest kept) and an overall cap.
_NOTE_ORDER = ("identity", "contact", "preference", "interest", "project", "goal", "fact", "event", "task")
_NOTE_CAPS = {"event": 20, "task": 10}
_NOTE_CAP_DEFAULT = 80
_NOTE_TOTAL = 260

MIN_ATTEMPT_INTERVAL_S = 10 * 60      # at most one model rebuild per user per 10 minutes
MIN_NEW_FOR_REBUILD = 2               # rebuild once this many new memories have arrived…
STALE_AFTER_S = 12 * 3600             # …or a single new one has waited this long

# Per-process bookkeeping: when a rebuild was last tried for a user (covers
# the case where nothing could be stored) and which users have one running.
_last_attempt: Dict[str, float] = {}
_rebuilding: set = set()

PROFILE_SYSTEM_PROMPT = (
    "You maintain a profile of one person from notes that were saved about them over time. "
    "Write the profile as JSON.\n\n"
    "Rules:\n"
    "- Use only what the notes say. Never invent, guess or generalise beyond them.\n"
    "- Merge duplicates. When two notes conflict, the later one (lower in its list) wins.\n"
    "- Notes marked (pinned) are things the person asked to be remembered: keep them.\n"
    "- Leave out one-off events and finished tasks unless they say something lasting about the person.\n"
    "- An interest is a subject the person keeps coming back to — a hobby, a field, a cause, a kind of "
    "project. Group what is known under it as short notes.\n"
    "- Write each item as one short plain sentence or phrase about the person, without 'the user'.\n\n"
    "Return ONE JSON object and nothing else:\n"
    "{\n"
    '  "summary": "two or three sentences: who this person is and what they are about",\n'
    '  "identity": ["name, where they live, work, household, devices — one short fact each, at most 10"],\n'
    '  "people": ["people in their life and how they relate, at most 10"],\n'
    '  "preferences": ["how they like things done and how they want to be answered, at most 12"],\n'
    '  "interests": [{"name": "short topic name", "notes": ["what is known about their involvement, at most 6"]}],\n'
    '  "projects": ["ongoing projects, at most 8"],\n'
    '  "goals": ["what they are working toward, at most 8"]\n'
    "}\n"
    "Use empty lists for sections the notes say nothing about."
)


# ── storage ────────────────────────────────────────────────────────────────

def profiles_dir(memory_manager) -> Optional[Path]:
    """Where profiles live: a ``profiles`` folder beside the memory file.
    None when there is no real memory store (nothing is kept then)."""
    memory_file = getattr(memory_manager, "memory_file", None)
    # A real str/Path only: a test double answers to os.PathLike too, and
    # would have profiles written under a folder named after the mock.
    if isinstance(memory_file, (str, Path)) and str(memory_file):
        return Path(memory_file).parent / "profiles"
    return None


def _safe_owner(owner: Optional[str]) -> str:
    name = re.sub(r"[^a-z0-9._-]", "_", str(owner or "").strip().lower()).strip(".")
    return name or "_single_user"


def profile_path(owner: Optional[str], memory_manager) -> Optional[Path]:
    folder = profiles_dir(memory_manager)
    return folder / f"{_safe_owner(owner)}.json" if folder is not None else None


def load_profile(owner: Optional[str], memory_manager) -> dict:
    path = profile_path(owner, memory_manager)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_profile(owner: Optional[str], profile: dict, memory_manager) -> None:
    path = profile_path(owner, memory_manager)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp file per write: two chats finishing at once must not
    # trample each other's half-written copy.
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".profile-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(profile, out, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def delete_profile(owner: Optional[str], memory_manager) -> bool:
    path = profile_path(owner, memory_manager)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


# ── shaping ────────────────────────────────────────────────────────────────

def _squash(value: Any) -> str:
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def _clean(value: Any) -> str:
    return _squash(value).strip(" -•*\t")[:MAX_ITEM_CHARS].rstrip()


def _unique(items: List[str], limit: int) -> List[str]:
    seen, out = set(), []
    for item in items:
        key = re.sub(r"[^a-z0-9]+", " ", item.lower()).strip()
        if not item or not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def normalize(raw: Any) -> dict:
    """Coerce whatever the model returned into the profile shape (or {})."""
    if not isinstance(raw, dict):
        return {}
    profile: Dict[str, Any] = {"summary": _squash(raw.get("summary"))[:MAX_SUMMARY_CHARS].rstrip()}
    for section in LIST_SECTIONS:
        values = raw.get(section)
        values = values if isinstance(values, list) else []
        profile[section] = _unique([_clean(v) for v in values], _CAPS[section])
    interests, seen = [], set()
    for item in raw.get("interests") if isinstance(raw.get("interests"), list) else []:
        if isinstance(item, str):
            item = {"name": item, "notes": []}
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"))[:60].rstrip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        notes = item.get("notes") if isinstance(item.get("notes"), list) else []
        interests.append({"name": name, "notes": _unique([_clean(n) for n in notes], MAX_NOTES)})
        if len(interests) >= MAX_INTERESTS:
            break
    profile["interests"] = interests
    return profile


def is_empty(profile: dict) -> bool:
    if not profile:
        return True
    return not (profile.get("summary") or profile.get("interests")
                or any(profile.get(section) for section in LIST_SECTIONS))


def _relevant(memories: Optional[List[dict]]) -> List[dict]:
    return [m for m in memories or [] if isinstance(m, dict) and m.get("id") and str(m.get("text") or "").strip()]


def _mark(memory: dict) -> str:
    """Short fingerprint of what a memory says (text, kind, pinned)."""
    raw = f"{memory.get('category')}|{int(bool(memory.get('pinned')))}|{memory.get('text')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _basis(memories: List[dict]) -> Dict[str, str]:
    return {str(m["id"]): _mark(m) for m in memories}


def basis_intact(profile: dict, memories: List[dict]) -> bool:
    """True while every memory the profile was written from still exists and
    still says the same thing. New memories do not break it."""
    basis = profile.get("basis")
    if not isinstance(basis, dict):
        return False
    current = _basis(_relevant(memories))
    return all(current.get(memory_id) == mark for memory_id, mark in basis.items())


def new_since(profile: dict, memories: List[dict]) -> int:
    """How many memories have arrived since the profile was written."""
    seen = profile.get("seen")
    if not isinstance(seen, list):
        seen = list(profile.get("basis") or {}) if isinstance(profile.get("basis"), dict) else []
    known = set(seen)
    return sum(1 for m in _relevant(memories) if str(m["id"]) not in known)


def _ranked(memories: List[dict]) -> List[dict]:
    return sorted(memories, key=lambda m: (not m.get("pinned"), -(m.get("uses") or 0), -(m.get("timestamp") or 0)))


def assemble(memories: List[dict]) -> dict:
    """A profile straight from the memories, with no model: each category
    becomes its section, interests are listed by name. Used until (or
    whenever) the model-written version is unavailable."""
    raw: Dict[str, Any] = {section: [] for section in LIST_SECTIONS}
    interests: List[dict] = []
    for memory in _ranked(_relevant(memories)):
        category = str(memory.get("category") or "fact")
        text = str(memory.get("text") or "")
        if category == "interest":
            interests.append({"name": text, "notes": []})
        elif category in _CATEGORY_SECTION:
            raw[_CATEGORY_SECTION[category]].append(text)
        elif memory.get("pinned"):
            raw["identity"].append(text)
    raw["interests"] = interests
    raw["summary"] = ""
    return normalize(raw)


def _notes_shown(memories: List[dict]) -> List[dict]:
    """The memories the model is shown, in the order it reads them: grouped
    by kind, oldest first within a kind (so "the later one wins" means
    something), events and tasks last and fewest."""
    groups: Dict[str, List[dict]] = {}
    for memory in _relevant(memories):
        groups.setdefault(str(memory.get("category") or "fact"), []).append(memory)
    order = list(_NOTE_ORDER) + sorted(k for k in groups if k not in _NOTE_ORDER)
    shown: List[dict] = []
    for category in order:
        items = sorted(groups.get(category, []), key=lambda m: m.get("timestamp") or 0)
        pinned = [m for m in items if m.get("pinned")]
        rest = [m for m in items if not m.get("pinned")]
        keep = max(_NOTE_CAPS.get(category, _NOTE_CAP_DEFAULT) - len(pinned), 0)
        kept = {id(m) for m in pinned} | {id(m) for m in (rest[-keep:] if keep else [])}
        shown.extend(m for m in items if id(m) in kept)
    return shown[:_NOTE_TOTAL]


def _notes_text(shown: List[dict]) -> str:
    lines: List[str] = []
    current = None
    for memory in shown:
        category = str(memory.get("category") or "fact")
        if category != current:
            lines.append(f"[{category}]")
            current = category
        lines.append(f"- {_squash(str(memory.get('text') or ''))[:220]}{' (pinned)' if memory.get('pinned') else ''}")
    return "\n".join(lines)


def _parse_object(raw: str) -> dict:
    """First JSON object in a model reply, tolerating reasoning blocks, code
    fences and commentary around it."""
    text = re.sub(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>", "", raw or "")
    start = text.find("{")
    while start >= 0:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:index + 1])
                        if isinstance(value, dict):
                            return value
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return {}


# ── building ───────────────────────────────────────────────────────────────

def _stamp(profile: dict, owner: Optional[str], used: List[dict], source: str,
           attempted: Optional[float] = None, seen: Optional[List[dict]] = None) -> dict:
    """Record what the profile rests on: ``basis`` — the memories it was
    written from (it is dropped when one of them goes), and ``seen`` — every
    memory that existed then (anything else is new)."""
    now = time.time()
    everything = used if seen is None else seen
    profile.update(version=VERSION, owner=owner or "", updated=now, source=source,
                   basis=_basis(used), seen=sorted(str(m["id"]) for m in everything),
                   memory_count=len(everything),
                   attempted=now if attempted is None else attempted)
    return profile


def _assembled(owner: Optional[str], memories: List[dict], attempted: float = 0.0) -> dict:
    memories = _relevant(memories)
    profile = assemble(memories)
    if is_empty(profile):
        return {}
    return _stamp(profile, owner, memories, "assembled", attempted=attempted)


def _key(owner: Optional[str], memory_manager) -> str:
    return f"{profiles_dir(memory_manager)}|{_safe_owner(owner)}"


def needs_rebuild(owner: Optional[str], memories: List[dict], memory_manager,
                  now: Optional[float] = None) -> bool:
    """Whether asking the model to rewrite the profile is worth it right now."""
    memories = _relevant(memories)
    if not memories or profiles_dir(memory_manager) is None:
        return False
    now = time.time() if now is None else now
    key = _key(owner, memory_manager)
    if key in _rebuilding or now - _last_attempt.get(key, 0.0) < MIN_ATTEMPT_INTERVAL_S:
        return False
    current = load_profile(owner, memory_manager)
    if not current or current.get("version") != VERSION:
        return True
    if now - float(current.get("attempted") or 0) < MIN_ATTEMPT_INTERVAL_S:
        return False
    if current.get("source") != "model" or not basis_intact(current, memories):
        return True
    fresh = new_since(current, memories)
    if fresh >= MIN_NEW_FOR_REBUILD:
        return True
    return fresh >= 1 and now - float(current.get("updated") or 0) >= STALE_AFTER_S


def get_profile(owner: Optional[str], memory_manager=None, memories: Optional[List[dict]] = None) -> dict:
    """The profile to use right now.

    The stored one, as long as everything it was written from is still there;
    otherwise one assembled on the spot from the memories that are (and
    stored, so the next rebuild knows a written version is due).
    """
    if memories is None:
        try:
            memories = memory_manager.load(owner=owner) if memory_manager is not None else []
        except Exception as exc:
            logger.debug("profile: could not load memories: %s", exc)
            memories = []
    memories = _relevant(memories)
    current = load_profile(owner, memory_manager)
    usable = bool(current) and current.get("version") == VERSION and not is_empty(current)
    if usable and basis_intact(current, memories):
        return current
    if not memories:
        if current:
            delete_profile(owner, memory_manager)     # nothing left to base a profile on
        return {}
    profile = _assembled(owner, memories, attempted=float(current.get("attempted") or 0) if current else 0.0)
    if not profile:
        if current:
            delete_profile(owner, memory_manager)
        return {}
    try:
        save_profile(owner, profile, memory_manager)
    except OSError as exc:
        logger.debug("profile: could not store the assembled profile: %s", exc)
    return profile


async def rebuild_profile(
    owner: Optional[str],
    memory_manager,
    endpoint_url: str,
    model: str,
    headers: Optional[dict] = None,
) -> dict:
    """Have the model write the profile from this user's memories and store
    it. If that fails, the previous profile stays when it is still valid;
    otherwise an assembled one is stored. Logs, never raises."""
    key = _key(owner, memory_manager)
    if key in _rebuilding:
        return load_profile(owner, memory_manager)
    _rebuilding.add(key)
    _last_attempt[key] = time.time()
    try:
        return await _rebuild(owner, memory_manager, endpoint_url, model, headers)
    finally:
        _rebuilding.discard(key)


async def _rebuild(owner, memory_manager, endpoint_url, model, headers) -> dict:
    try:
        memories = _relevant(memory_manager.load(owner=owner))
    except Exception as exc:
        logger.warning("profile rebuild: could not load memories: %s", exc)
        return load_profile(owner, memory_manager)
    if not memories:
        delete_profile(owner, memory_manager)
        return {}

    shown = _notes_shown(memories)
    written: dict = {}
    if endpoint_url and model:
        try:
            from src.llm_core import llm_call_async

            raw = await llm_call_async(
                endpoint_url, model,
                [
                    {"role": "system", "content": PROFILE_SYSTEM_PROMPT},
                    {"role": "user", "content": (
                        "Notes about the person, grouped by kind, oldest first within each group:\n\n"
                        + _notes_text(shown) + "\n\nReturn the profile JSON now."
                    )},
                ],
                temperature=0.1,
                max_tokens=6000,          # room for a reasoning model to think before the JSON
                headers=headers,
            )
            written = normalize(_parse_object(raw))
        except Exception as exc:
            logger.warning("profile rebuild: model call failed: %s", exc)
            written = {}

    now = time.time()
    if not is_empty(written):
        # The memories may have changed while the model was writing: only
        # vouch for the notes it was shown that are still there, unchanged.
        try:
            latest = _basis(_relevant(memory_manager.load(owner=owner)))
        except Exception:
            latest = _basis(memories)
        if any(latest.get(str(m["id"])) != _mark(m) for m in shown):
            logger.info("profile rebuild: memories changed during the rebuild; keeping the assembled profile")
            written = {}
            memories = [m for m in memories if latest.get(str(m["id"])) == _mark(m)]
    if not is_empty(written):
        profile = _stamp(written, owner, shown, "model", attempted=now, seen=memories)
    else:
        previous = load_profile(owner, memory_manager)
        if (previous and previous.get("source") == "model" and not is_empty(previous)
                and basis_intact(previous, memories)):
            previous["attempted"] = now          # a good profile beats a crude one
            profile = previous
        else:
            profile = _assembled(owner, memories, attempted=now)
    if not profile:
        delete_profile(owner, memory_manager)
        return {}
    try:
        save_profile(owner, profile, memory_manager)
        logger.info("Profile for %s: %s, %d memories, %d interests", owner or "single user",
                    profile.get("source"), profile.get("memory_count") or 0, len(profile.get("interests") or []))
    except OSError as exc:
        logger.warning("profile rebuild: could not save: %s", exc)
    return profile


async def maybe_rebuild(owner: Optional[str], memory_manager, endpoint_url: str, model: str,
                        headers: Optional[dict] = None) -> None:
    """Background-queue entry point: rebuild only if it is due *now* (the
    memory extraction queued just before this may have added something)."""
    try:
        if needs_rebuild(owner, memory_manager.load(owner=owner), memory_manager):
            await rebuild_profile(owner, memory_manager, endpoint_url, model, headers)
    except Exception as exc:
        logger.warning("profile rebuild skipped: %s", exc)


# ── use ────────────────────────────────────────────────────────────────────

# Per-section share of the prompt block, in characters. Without these a long
# list of preferences pushes the interests — last in the profile — out of it.
_SECTION_ROOM = {"identity": 450, "preferences": 450, "people": 250, "projects": 300, "goals": 300}
_INTEREST_LINE_ROOM = 240


def _fit(items: List[str], room: int) -> List[str]:
    """As many whole items, in order, as fit in ``room`` characters."""
    out: List[str] = []
    used = 0
    for item in items:
        if out and used + len(item) + 2 > room:
            break
        out.append(item)
        used += len(item) + 2
    return out


def render_for_prompt(profile: dict, budget: int = 3200) -> str:
    """The profile as the block the model reads on every turn.

    Deterministic for a given profile — the same bytes turn after turn — so a
    local backend's prompt cache is not invalidated. After the first line
    every line is a "- " bullet. Every section gets its share: who they are,
    what they are into, how they like things, then the rest.
    """
    if is_empty(profile):
        return ""
    lines = ["Who you are talking to: what has been learned about the user so far. Let it shape the answer "
             "where it is relevant — always for their health, safety and stated preferences — and do not "
             "recite it or work unrelated parts of it into a reply."]
    if profile.get("summary"):
        lines.append(f"- {profile['summary']}")

    def section(name: str) -> None:
        label = next(model_label for key, model_label, _ in _LABELS if key == name)
        items = _fit(profile.get(name) or [], _SECTION_ROOM[name])
        if items:
            lines.append(f"- {label}: " + "; ".join(items))

    interests = profile.get("interests") or []
    section("identity")
    if interests:
        lines.append("- Interests: " + ", ".join(item["name"] for item in interests))
    section("preferences")
    section("people")
    section("projects")
    section("goals")
    for item in interests:
        notes = _fit((item.get("notes") or [])[:3], _INTEREST_LINE_ROOM)
        if notes:
            lines.append(f"- {item['name']}: " + "; ".join(notes))
    out: List[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget and len(out) > 1:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def to_markdown(profile: dict) -> str:
    """The profile for a person to read (the /me command)."""
    if is_empty(profile):
        return ("Nothing is known about you yet. This fills in by itself as you chat (never in Nobody mode); "
                "you can also tell me something with `/me add …`.")
    parts: List[str] = []
    if profile.get("summary"):
        parts.append(profile["summary"])
    for section, _, label in _LABELS:
        items = profile.get(section) or []
        if items:
            parts.append(f"**{label}**\n" + "\n".join(f"- {item}" for item in items))
    interests = profile.get("interests") or []
    if interests:
        block = ["**Interests**"]
        for item in interests:
            block.append(f"- **{item['name']}**" + (": " + "; ".join(item["notes"]) if item.get("notes") else ""))
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def public_view(profile: dict) -> dict:
    """What the API returns: the content, not the bookkeeping."""
    view: Dict[str, Any] = {
        "empty": is_empty(profile),
        "summary": (profile or {}).get("summary") or "",
        "interests": (profile or {}).get("interests") or [],
        "updated": (profile or {}).get("updated"),
        "source": (profile or {}).get("source"),
        "memory_count": (profile or {}).get("memory_count") or 0,
    }
    for section in LIST_SECTIONS:
        view[section] = (profile or {}).get(section) or []
    return view
