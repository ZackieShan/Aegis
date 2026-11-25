"""Keep what the model reads first the same from one message to the next in a chat.

A local model reads its prompt from the top every time, except for the part that
is byte-for-byte what it read last time: that part it still has (the server's
prompt cache). On this kind of server reading costs about a second per thousand
tokens, and an agent prompt opens with thousands of tokens of instructions and
tool descriptions before the first word of the chat. So when that opening is the
same as on the turn before, the model starts answering in about a second; when one
tool in it has changed, it reads everything again and the person waits.

The tools offered are picked afresh for every message by relevance, so they used
to change with almost every message (measured: 10 to 37 tools over one day's
messages, never the same list twice) and every message paid the full read.

This module is the memory that stops that. For each chat it remembers which tools
have been offered so far, and whether the administrator tools and the e-mail
writing rules were part of the prompt. The agent loop adds what is remembered to
what the new message calls for, so within a chat the list only ever grows — and
on the many turns where it does not grow, the opening is unchanged and stays
cached. Nothing here grants a tool: what is switched off or not allowed for the
person is still removed afterwards, exactly as before.

Kept in memory only, per process: after a restart the first message of a chat
picks its tools afresh, which costs one full read.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Dict, Iterable, Optional, Set

# A chat that has gathered more tools than this starts again from what the new
# message calls for: past this many, the descriptions crowd the prompt and the
# model picks worse.
MAX_TOOLS = 44
MAX_CHATS = 400
# In a chat that already has its tools, a tool found only by likeness to the
# message joins the list when it scores at least this. Below it the match is
# noise: every message, "thanks" included, comes back with its eight nearest
# tools, and adding those would change the prompt on every turn for nothing.
# (A tool the message names by a keyword, or that belongs to its subject —
# e-mail, files, the web — is added whatever its score.)
ADD_FLOOR = 0.2

_lock = threading.Lock()
_chats: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def enabled() -> bool:
    """On unless the setting ``keep_prompt_stable`` is switched off (there to compare the two, or to go back)."""
    try:
        from src.settings import get_setting
        return bool(get_setting("keep_prompt_stable", True))
    except Exception:
        return True


def recall(session_id: Optional[str]) -> Dict[str, Any]:
    """What this chat's prompt has held so far: ``{"tools": set, "admin": bool, "email_style": bool}``."""
    if not session_id or not enabled():
        return {"tools": set(), "admin": False, "email_style": False}
    with _lock:
        kept = _chats.get(str(session_id))
        if kept is None:
            return {"tools": set(), "admin": False, "email_style": False}
        _chats.move_to_end(str(session_id))
        return {"tools": set(kept["tools"]), "admin": bool(kept["admin"]), "email_style": bool(kept["email_style"])}


def under_way(session_id: Optional[str]) -> bool:
    """True once this chat has had a turn with tools, so the top of its prompt is something the model
    still has. From then on Auto does not hand a message to the quick lane (a different prompt
    altogether) or lower the reasoning effort (for gpt-oss, a word in the prompt's first lines):
    either would make a local model read the whole chat again, which costs more than it saves."""
    if not session_id or not enabled():
        return False
    with _lock:
        kept = _chats.get(str(session_id))
        return bool(kept and kept["tools"])


def floor_for(session_id: Optional[str]) -> Optional[float]:
    """The score a likeness-only tool needs to join this chat's list: ``ADD_FLOOR`` once the chat has
    tools, ``None`` (no floor, as always) for a chat's first message."""
    if not session_id or not enabled():
        return None
    with _lock:
        kept = _chats.get(str(session_id))
        return ADD_FLOOR if kept and kept["tools"] else None


def settle(session_id: Optional[str], wanted: Optional[Iterable[str]], blocked: Optional[Iterable[str]] = None) -> Optional[Set[str]]:
    """The tools to offer for this message: what it calls for, plus what this chat was already offered.

    ``wanted`` is the fresh pick; ``None`` (no picking in force — everything is on
    offer) is returned as it is. Tools in ``blocked`` are never brought back. When
    the two together would pass ``MAX_TOOLS`` the fresh pick stands alone and the
    chat starts gathering again.
    """
    if wanted is None:
        return None
    fresh = set(wanted)
    if not session_id or not enabled():
        return fresh
    off = set(blocked or ())
    kept = recall(session_id)["tools"] - off
    if not kept or kept <= fresh:
        return fresh
    both = fresh | kept
    if len(both) > MAX_TOOLS:
        return fresh
    return both


def remember(session_id: Optional[str], tools: Optional[Iterable[str]] = None, admin: Optional[bool] = None,
             email_style: Optional[bool] = None) -> None:
    """Record what this message's prompt held. ``tools`` replaces what was kept (the caller passes the
    settled list); a flag once set stays set for the chat."""
    if not session_id:
        return
    key = str(session_id)
    with _lock:
        kept = _chats.get(key)
        if kept is None:
            kept = {"tools": set(), "admin": False, "email_style": False}
            _chats[key] = kept
        if tools is not None:
            kept["tools"] = set(tools)
        if admin:
            kept["admin"] = True
        if email_style:
            kept["email_style"] = True
        _chats.move_to_end(key)
        while len(_chats) > MAX_CHATS:
            _chats.popitem(last=False)


def forget(session_id: Optional[str] = None) -> None:
    """Drop one chat's record, or every chat's when called with nothing."""
    with _lock:
        if session_id is None:
            _chats.clear()
        else:
            _chats.pop(str(session_id), None)
