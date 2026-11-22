"""Roles, per-person experience settings, and the account activity log.

*Roles* answer "what may this account do": a named bundle of privileges and
of things the assistant may do on the person's behalf. *Experience* answers
"what is the app like for this person": which interface they land in, what
the assistant is called and how it talks to them, which sections they see.
Both are set by an administrator, per account; a role supplies the defaults.

Built-in roles
    admin      everything, including the server itself (not editable)
    member     the full app with their own data; no access to the server
    household  the simple home screen: chat, lists, reminders, trackers, boards
    guest      chat only; nothing is remembered

An administrator can adjust the built-in ones (except ``admin``) and add
their own. Definitions live in ``<data>/roles.json``; an account's role,
privilege overrides and experience live in its record in ``auth.json``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# What the assistant may do for a non-administrator, in groups a role can
# grant. Every tool listed works only on the caller's own data. Anything that
# reaches the server, its files, other machines, settings, email or other
# people's data is never grantable: it stays with administrators.
TOOL_GROUPS: Dict[str, Dict[str, Any]] = {
    "home": {
        "label": "Lists, reminders, trackers and boards",
        "description": "Keep lists, set reminders, log trackers and pin things to boards when asked.",
        "tools": ["manage_lists", "manage_reminders", "manage_trackers", "manage_boards", "manage_notes"],
    },
    "memory": {
        "label": "Remember on request",
        "description": "Save, correct and delete what it remembers about them when they ask.",
        "tools": ["manage_memory"],
    },
    "calendar": {
        "label": "Their calendar",
        "description": "Read and change events in their own calendar.",
        "tools": ["manage_calendar"],
    },
}
GRANTABLE_TOOLS = frozenset(tool for group in TOOL_GROUPS.values() for tool in group["tools"])

MODES = ("full", "simple")
HOME_SECTIONS = ("lists", "reminders", "trackers", "boards")
SECTION_LABELS = {"lists": "Lists and plans", "reminders": "Reminders", "trackers": "Trackers", "boards": "Boards"}
THEMES = ("auto", "light", "dark")
DEFAULT_EXPERIENCE: Dict[str, Any] = {
    "mode": "full",                 # which interface they land in
    "display_name": "",             # what they are called on screen and by the assistant
    "assistant_name": "",           # empty = Aegis
    "tone": "",                     # the administrator's note on how to talk to this person
    "sections": list(HOME_SECTIONS),  # what the simple home screen offers besides chat
    "theme": "auto",
    "accent": "",                   # #rrggbb, empty = default
    "allow_full_app": False,        # may a simple-mode person open the full interface
    "hidden_ui": [],                # parts of the full interface hidden for them (display only)
}

_PRIVILEGE_LABELS = {
    "can_use_agent": ("Assistant can act", "Let the assistant use tools — search, lists, reminders — not only talk."),
    "can_use_browser": ("Browser automation", "Let the assistant drive a web browser."),
    "can_use_bash": ("Shell, code and server files", "Run commands and read or write files on the server. Administrators only."),
    "can_use_documents": ("Document editor", "Create and edit documents."),
    "can_use_research": ("Deep research", "Long multi-step web research."),
    "can_generate_images": ("Image generation", "Make images."),
    "can_manage_memory": ("Memory", "Add to and manage what is remembered about them."),
    "can_use_recipes": ("Recipes", "Run saved workflows."),
}


def _builtin_roles() -> Dict[str, Dict[str, Any]]:
    from core.auth import ADMIN_PRIVILEGES, DEFAULT_PRIVILEGES

    return {
        "admin": {
            "label": "Administrator", "builtin": True, "locked": True,
            "description": "Everything, including the server itself, other machines and everyone's settings.",
            "privileges": dict(ADMIN_PRIVILEGES), "tools": list(TOOL_GROUPS),
            "experience": {"mode": "full"},
        },
        "member": {
            "label": "Member", "builtin": True,
            "description": "The full app with their own chats, memory and files. No access to the server.",
            "privileges": dict(DEFAULT_PRIVILEGES), "tools": ["home", "memory", "calendar"],
            "experience": {"mode": "full"},
        },
        "household": {
            "label": "Household", "builtin": True,
            "description": "A simple home screen — chat, lists, reminders, trackers and boards — in plain language.",
            "privileges": {**DEFAULT_PRIVILEGES, "can_use_browser": False, "can_use_documents": False,
                           "can_use_recipes": False},
            "tools": ["home", "memory", "calendar"],
            "experience": {"mode": "simple"},
        },
        "guest": {
            "label": "Guest", "builtin": True,
            "description": "Chat only. Nothing is kept and the assistant cannot act for them.",
            "privileges": {**DEFAULT_PRIVILEGES, "can_use_agent": False, "can_use_browser": False,
                           "can_use_documents": False, "can_use_research": False, "can_generate_images": False,
                           "can_manage_memory": False, "can_use_recipes": False, "max_messages_per_day": 100},
            "tools": [],
            "experience": {"mode": "simple", "sections": []},
        },
    }


BUILTIN_ORDER = ("admin", "member", "household", "guest")
_lock = threading.Lock()


def _data_dir() -> str:
    from src.constants import DATA_DIR
    return DATA_DIR


def _roles_path() -> str:
    return os.path.join(_data_dir(), "roles.json")


def _read_stored() -> Dict[str, dict]:
    try:
        with open(_roles_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        roles = data.get("roles") if isinstance(data, dict) else None
        return {k: v for k, v in (roles or {}).items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def _write_stored(roles: Dict[str, dict]) -> None:
    from core.atomic_io import atomic_write_json
    atomic_write_json(_roles_path(), {"roles": roles}, indent=2, mode=0o600)


def valid_role_name(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", str(name or "")))


def normalize_experience(raw: Any, base: Optional[dict] = None) -> Dict[str, Any]:
    """Whatever was sent, coerced to known keys and safe values, on top of
    ``base`` (defaults when omitted)."""
    out = dict(DEFAULT_EXPERIENCE if base is None else base)
    out["sections"] = list(out.get("sections") or [])
    out["hidden_ui"] = list(out.get("hidden_ui") or [])
    if not isinstance(raw, dict):
        return out
    if raw.get("mode") in MODES:
        out["mode"] = raw["mode"]
    for key, limit in (("display_name", 40), ("assistant_name", 30), ("tone", 600)):
        if isinstance(raw.get(key), str):
            if key == "tone":
                # A note of a few lines stays a note of a few lines.
                lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in re.split(r"\r\n|\r|\n", raw[key])]
                value = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
            else:
                # Names go into the assistant's standing instructions, and a
                # person can set their own: letters, digits and the few marks
                # names use — nothing that could read as an instruction.
                value = re.sub(r"\s+", " ", raw[key]).strip()
                value = re.sub(r"[^\w .'’-]", "", value).strip()
            out[key] = value[:limit].strip()
    if isinstance(raw.get("sections"), list):
        out["sections"] = [s for s in HOME_SECTIONS if s in raw["sections"]]
    if raw.get("theme") in THEMES:
        out["theme"] = raw["theme"]
    if isinstance(raw.get("accent"), str):
        accent = raw["accent"].strip()
        out["accent"] = accent if re.fullmatch(r"#[0-9a-fA-F]{6}", accent) else ""
    if isinstance(raw.get("allow_full_app"), bool):
        out["allow_full_app"] = raw["allow_full_app"]
    if isinstance(raw.get("hidden_ui"), list):
        out["hidden_ui"] = [str(k)[:40] for k in raw["hidden_ui"] if re.fullmatch(r"[a-z0-9-]{2,40}", str(k))][:60]
    return out


def _normalize_role(name: str, raw: dict, builtin: Optional[dict] = None) -> Dict[str, Any]:
    from core.auth import DEFAULT_PRIVILEGES

    base = builtin or {}
    privileges = dict(DEFAULT_PRIVILEGES)
    privileges.update(base.get("privileges") or {})
    for key, value in (raw.get("privileges") or {}).items():
        if key not in DEFAULT_PRIVILEGES:
            continue
        default = DEFAULT_PRIVILEGES[key]
        if isinstance(default, bool) and isinstance(value, bool):
            privileges[key] = value
        elif isinstance(default, int) and not isinstance(default, bool) and isinstance(value, (int, float)):
            privileges[key] = max(0, int(value))
        elif isinstance(default, list) and isinstance(value, list):
            privileges[key] = [str(v) for v in value][:500]
    # Server access is never something a role hands out.
    privileges["can_use_bash"] = False
    tools = raw.get("tools") if isinstance(raw.get("tools"), list) else base.get("tools") or []
    experience = normalize_experience(raw.get("experience"), normalize_experience(base.get("experience")))
    return {
        "label": (re.sub(r"\s+", " ", str(raw.get("label") or base.get("label") or name)).strip()[:40]) or name,
        "description": re.sub(r"\s+", " ", str(raw.get("description") if raw.get("description") is not None
                                                else base.get("description") or "")).strip()[:300],
        "privileges": privileges,
        "tools": [group for group in TOOL_GROUPS if group in tools],
        "experience": {key: experience[key] for key in ("mode", "sections", "theme", "allow_full_app")},
        "builtin": bool(builtin),
    }


def list_roles() -> Dict[str, Dict[str, Any]]:
    """Every role, built-in ones first, each fully resolved."""
    builtins = _builtin_roles()
    stored = _read_stored()
    out: Dict[str, Dict[str, Any]] = {}
    for name in BUILTIN_ORDER:
        if name == "admin":
            out[name] = dict(builtins[name])
            continue
        role = _normalize_role(name, stored.get(name) or {}, builtins[name])
        role["customized"] = name in stored
        out[name] = role
    for name in sorted(stored):
        if name not in builtins and valid_role_name(name):
            out[name] = _normalize_role(name, stored[name])
    return out


def get_role(name: Optional[str]) -> Dict[str, Any]:
    roles = list_roles()
    return roles.get(name or "") or roles["member"]


def role_exists(name: str) -> bool:
    return name in list_roles()


def save_role(name: str, definition: dict) -> Dict[str, Any]:
    if name == "admin":
        raise ValueError("The administrator role cannot be changed.")
    if not valid_role_name(name):
        raise ValueError("Role names are 2–32 lowercase letters, digits, dashes or underscores.")
    builtins = _builtin_roles()
    with _lock:
        stored = _read_stored()
        # What is sent is laid over the role as it stands, so a caller that
        # sends only a new label does not reset everything else.
        base = builtins.get(name)
        if isinstance(stored.get(name), dict):
            base = _normalize_role(name, stored[name], base)
        role = _normalize_role(name, definition or {}, base)
        stored[name] = {key: role[key] for key in ("label", "description", "privileges", "tools", "experience")}
        _write_stored(stored)
    return get_role(name)


def delete_role(name: str) -> bool:
    """Remove a custom role, or put a built-in one back to its defaults."""
    if name == "admin":
        return False
    with _lock:
        stored = _read_stored()
        if name not in stored:
            return False
        del stored[name]
        _write_stored(stored)
    return True


def role_privileges(name: Optional[str]) -> Dict[str, Any]:
    from core.auth import DEFAULT_PRIVILEGES
    return {**DEFAULT_PRIVILEGES, **(get_role(name).get("privileges") or {})}


def granted_tools(name: Optional[str]) -> set:
    """Tools this role lets the assistant use for the person."""
    role = get_role(name)
    return {tool for group in role.get("tools") or [] for tool in TOOL_GROUPS[group]["tools"]}


def effective_experience(role_name: Optional[str], user_experience: Optional[dict]) -> Dict[str, Any]:
    """The role's defaults with this person's own settings on top."""
    return normalize_experience(user_experience, normalize_experience(get_role(role_name).get("experience")))


def persona_prompt(experience: Dict[str, Any], username: str) -> str:
    """The extra system instruction that makes the assistant fit one person.

    Empty when nothing was customised, so an untouched account behaves
    exactly as before. Stable for a given configuration: it sits in the
    cached part of the prompt.
    """
    name = experience.get("display_name") or ""
    assistant = experience.get("assistant_name") or ""
    tone = experience.get("tone") or ""
    simple = experience.get("mode") == "simple"
    if not (simple or name or assistant or tone):
        return ""
    person = name or (username or "").strip().title() or "this person"
    lines: List[str] = []
    if simple:
        lines.append(
            f"You are {assistant or 'Aegis'}, a personal assistant for {person}. {person} is not a technical "
            "person: use plain, friendly, everyday language and keep answers short. Never mention models, "
            "prompts, tools, tokens, settings or how you work."
        )
        lines.append(
            f"When {person} wants something kept track of — things for a list, a reminder, a habit or number "
            "to follow over time, ideas or pictures to collect — do it straight away with your tools rather than "
            "explaining how, then say in one short sentence what you did. If something essential is missing "
            "(the time for a reminder, which list), ask one short question. Suggest one helpful next step when "
            "it comes naturally; do not offer menus of options."
        )
    else:
        if assistant:
            lines.append(f"Your name is {assistant}.")
        if name:
            lines.append(f"You are talking with {name}.")
    if tone:
        joiner = "\n" if "\n" in tone else " "
        lines.append(f"From the person who set this account up, about how to talk with {person}:{joiner}{tone}")
    return "\n".join(lines)


def privilege_catalogue() -> List[Dict[str, Any]]:
    from core.auth import DEFAULT_PRIVILEGES
    out = []
    for key, default in DEFAULT_PRIVILEGES.items():
        label, description = _PRIVILEGE_LABELS.get(key, (key, ""))
        kind = "switch" if isinstance(default, bool) else ("number" if isinstance(default, int) else "list")
        if key in _PRIVILEGE_LABELS:
            out.append({"key": key, "label": label, "description": description, "kind": kind,
                        "admin_only": key == "can_use_bash"})
    out.append({"key": "max_messages_per_day", "label": "Messages per day", "kind": "number",
                "description": "0 means no limit.", "admin_only": False})
    return out


# ── account activity ──────────────────────────────────────────────────────

def _audit_path() -> str:
    return os.path.join(_data_dir(), "accounts_audit.jsonl")


def _state_path() -> str:
    return os.path.join(_data_dir(), "accounts_state.json")


def audit(event: str, user: Optional[str] = None, by: Optional[str] = None, ip: Optional[str] = None,
          detail: Optional[str] = None) -> None:
    """Append one line to the account activity log. Never raises.

    Events: sign-ins (ok, failed, locked out, refused), sign-outs, password
    changes, and everything an administrator does to an account or a role.
    Failed sign-ins record the name that was typed; never a password.
    """
    if os.environ.get("AEGIS_ACCOUNT_LOG_DISABLED"):       # the test suite: never write into a real log
        return
    entry: Dict[str, Any] = {"ts": time.time(), "event": event}
    if user:
        entry["user"] = str(user)[:64]
    if by:
        entry["by"] = str(by)[:64]
    if ip:
        entry["ip"] = str(ip)[:64]
    if detail:
        entry["detail"] = str(detail)[:300]
    try:
        path = _audit_path()
        new = not os.path.exists(path)
        with _lock:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if new:
                os.chmod(path, 0o600)
            elif os.path.getsize(path) > 4 * 1024 * 1024:
                with open(path, "r", encoding="utf-8") as handle:
                    lines = handle.readlines()
                with open(path, "w", encoding="utf-8") as handle:
                    handle.writelines(lines[len(lines) // 2:])
    except OSError as exc:
        logger.warning("account activity not logged: %s", exc)


def read_audit(limit: int = 200, user: Optional[str] = None, events: Optional[Iterable[str]] = None) -> List[dict]:
    """Newest first."""
    try:
        with open(_audit_path(), "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    wanted = set(events) if events else None
    out: List[dict] = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if user and entry.get("user") != user and entry.get("by") != user:
            continue
        if wanted and entry.get("event") not in wanted:
            continue
        out.append(entry)
        if len(out) >= max(1, int(limit)):
            break
    return out


def note_sign_in(user: str) -> None:
    """Remember when an account last signed in (shown in the admin console)."""
    if os.environ.get("AEGIS_ACCOUNT_LOG_DISABLED"):
        return
    try:
        from core.atomic_io import atomic_write_json
        with _lock:
            try:
                with open(_state_path(), "r", encoding="utf-8") as handle:
                    state = json.load(handle)
            except (OSError, ValueError):
                state = {}
            entry = state.setdefault(user, {})
            entry["last_sign_in"] = time.time()
            entry["sign_ins"] = int(entry.get("sign_ins") or 0) + 1
            atomic_write_json(_state_path(), state, mode=0o600)
    except Exception as exc:
        logger.debug("sign-in time not recorded: %s", exc)


def sign_in_state() -> Dict[str, dict]:
    try:
        with open(_state_path(), "r", encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _change_sign_in_state(change) -> bool:
    """Apply ``change(state) -> bool`` to the sign-in record and save it when it says so."""
    if os.environ.get("AEGIS_ACCOUNT_LOG_DISABLED"):
        return False
    from core.atomic_io import atomic_write_json
    with _lock:
        state = sign_in_state()
        if not change(state):
            return False
        atomic_write_json(_state_path(), state, mode=0o600)
        return True


def forget_sign_ins(user: str) -> bool:
    """Drop an account's last-sign-in record (the account was deleted)."""
    return _change_sign_in_state(lambda state: state.pop(user, None) is not None)


def rename_sign_ins(old: str, new: str) -> bool:
    """Carry an account's sign-in record over to its new name."""
    def move(state: dict) -> bool:
        if old not in state or new in state:
            return False
        state[new] = state.pop(old)
        return True
    return _change_sign_in_state(move)
