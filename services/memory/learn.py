"""The learning pass: bring one person's memory up to date after a conversation.

One background model call per pass looks at the latest messages next to what
is already known and returns everything the conversation changes:

* **add** — new lasting facts, each with the user's own words as evidence;
* **update** — known facts the conversation changes or sharpens;
* **remove** — known facts the user says are no longer true;
* **topics** — detailed, dated notes for the dossier of each subject touched
  (see ``mind.py``);
* **episode** — a digest of the conversation itself.

Guards: only this account's memories are shown and touched; facts the person
wrote or pinned themselves are never rewritten or removed by the model; kinds
of information the person has blocked are dropped; every change is journaled
with the conversation it came from and can be undone. If the model cannot be
reached, the plain pattern-matching fallback still catches the obvious
statements ("my name is…", "I prefer…").
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

from services.memory import journal, mind

logger = logging.getLogger(__name__)

WINDOW = 8                      # messages looked at per pass, at least
MAX_WINDOW = 24                 # …and at most, after a long stretch with no pass
# Passes wait for a lull in chatting. Per chat: is one already waiting, and
# how many messages had the chat when the last one ran.
_waiting: set = set()
_seen_messages: Dict[str, int] = {}
MAX_ADD, MAX_UPDATE, MAX_REMOVE = 5, 3, 2
MAX_TOPICS, MAX_NOTES = 3, 6
KNOWN_ALWAYS, KNOWN_RELATED = 12, 18
CATEGORIES = ("identity", "preference", "interest", "contact", "project", "goal", "fact")
# A topic of this kind is itself of a sensitive kind.
_KIND_SENSITIVE = {"health": "health", "work": "work", "person": "relationships", "place": "location"}

LEARN_SYSTEM_PROMPT = (
    "You keep one person's long-term memory up to date. You are shown what is already known about them and "
    "the latest part of a conversation between them (user) and an AI assistant. Work out what the "
    "conversation changes.\n\n"
    "MOST CONVERSATIONS CHANGE NOTHING. An errand (add this to a list, set a reminder, log something), a "
    "lookup or how-to question, a request to write or fix something: these reveal nothing lasting about the "
    "person. For them return empty lists and a null episode. Storing noise is worse than storing nothing.\n\n"
    "Only the user's own words are evidence. What the assistant said, did or answered is never a fact about "
    "the user. Never store: what is on their lists, which reminders are set, what was just added or done, "
    "what they asked, or what the answer was.\n\n"
    "Return ONE JSON object and nothing else:\n"
    "{\n"
    '  "update": [{"id": "m3", "text": "…", "quote": "…"}],\n'
    '  "remove": [{"id": "m7", "reason": "…"}],\n'
    '  "add": [{"text": "…", "category": "…", "quote": "…", "sensitive": null}],\n'
    '  "topics": [{"name": "…", "kind": "…", "keywords": ["…"], "notes": ["…"]}],\n'
    '  "episode": {"title": "…", "summary": "…", "outcomes": ["…"]}\n'
    "}\n\n"
    "FIRST go through KNOWN. For each known fact that the user's words touch:\n"
    "update — the same fact with a newer value or more precision (three hives is now four; a new city; a "
    "changed plan). Give its id and the complete new sentence. Never add a second fact about something KNOWN "
    "already covers — update the one that is there. An update stays about the same thing; a different event "
    "is not an update. At most 3.\n"
    "remove — a known fact the user says is wrong or no longer true, when nothing replaces it. At most 2.\n\n"
    "THEN what is new:\n"
    "add — things the person tells you about THEMSELVES that will still be true and useful in a month: who "
    "they are, where they live, their work, the people in their life, what they are into, long-running "
    "projects, goals — and how they want to be answered (length, tone, units, format): always keep those, "
    "each as a preference of its own. At most 5. One question about a subject does not make it an interest; what they say "
    "they do, own, practise, follow or plan does.\n"
    "  text: one short sentence under 20 words, written without a subject — start with the verb or the "
    "thing ('Works nights as an ER nurse', 'Prefers metric units'). No 'the user', no name as the subject, "
    "no he/she. Their own name, when they give it, is a fact of its own: 'Name: …'. Other people are "
    "recorded by how they relate to the person, exactly as said: 'Sister: Jenna, marrying Tom next month'.\n"
    "  category: identity (their own name, home, work — nothing else) | contact (people in their life) | "
    "preference | interest | project | goal | fact\n"
    "  quote: the user's own words that show it, copied exactly (a phrase is enough)\n"
    "  sensitive: health | money | location | relationships | work | beliefs when the fact is of that kind, "
    "otherwise null\n"
    "topics — subjects the person is personally involved in over time: a hobby, a long project, a person "
    "close to them, a health matter, their home, their work. For each, the specific things learned about "
    "THEIR involvement — details, numbers, gear, events, problems, plans, decisions — as separate short "
    "notes written like the facts above. If one of KNOWN TOPICS covers the subject, use that exact name. "
    "Not a topic: an errand, a list, a single call or appointment, something they only asked about, or "
    "anything where all that was learned fits in one fact. At most 3 topics, 6 notes each.\n"
    "  kind: interest | project | person | health | home | work | place | other\n"
    "  keywords: up to 6 single words people use when talking about it\n"
    "episode — only when the conversation had substance worth recalling later (they worked something "
    "through, decided something, told you about their life): a short title, a summary of two or three "
    "sentences, and outcomes (decisions made, things they said they would do). If an EARLIER SUMMARY is "
    "given, extend it rather than starting over. null for errands, lookups and greetings.\n\n"
    "Examples of the right answer being nothing:\n"
    "user: Add milk and eggs to my shopping list -> "
    '{"update":[],"remove":[],"add":[],"topics":[],"episode":null}\n'
    "user: What's on my list? / assistant: It is empty. -> "
    '{"update":[],"remove":[],"add":[],"topics":[],"episode":null}\n'
    "user: How long do I boil an egg? -> "
    '{"update":[],"remove":[],"add":[],"topics":[],"episode":null}\n'
    "user: Remind me to call the vet tomorrow at 3 -> "
    '{"update":[],"remove":[],"add":[],"topics":[],"episode":null}'
)


def _squash(value: Any) -> str:
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def _session_key(session) -> Optional[str]:
    for name in ("id", "session_id"):
        value = getattr(session, name, None)
        if isinstance(value, str) and value:
            return value
    return None


def _text_of(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return content if isinstance(content, str) else ""


def _transcript(session, window: int = WINDOW) -> tuple:
    """(lines for the model, the user's own words joined, message count)."""
    try:
        messages = session.get_context_messages()
    except Exception:
        return [], "", 0
    recent = [m for m in messages[-window:] if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    lines, said = [], []
    for message in recent:
        text = _text_of(message).strip()
        if not text:
            continue
        if message["role"] == "user":
            said.append(text)
            lines.append("user: " + text[:3000])
        else:
            lines.append("assistant: " + text[:1500])
    return lines, "\n".join(said), len(messages)


def is_protected(memory: dict) -> bool:
    """Written or pinned by the person themselves: the model never rewrites
    or removes it. (Facts pinned automatically because they are identity
    facts are not protected.)"""
    if memory.get("source") == "user" or memory.get("pinned_by") == "user":
        return True
    return bool(memory.get("pinned")) and memory.get("pinned_by") is None and memory.get("source") != "auto"


def _known_for_prompt(mine: List[dict], said: str) -> List[dict]:
    """The memories worth showing the model: who the person is, plus the
    ones that share words with what they just said."""
    always = [m for m in mine if m.get("pinned") or m.get("category") == "identity"]
    always = sorted(always, key=lambda m: -(m.get("timestamp") or 0))[:KNOWN_ALWAYS]
    chosen = {m["id"] for m in always}
    query = set(mind.tokens(said))
    scored = []
    for memory in mine:
        if memory["id"] in chosen:
            continue
        shared = len(query & set(mind.tokens(memory.get("text", ""))))
        if shared:
            scored.append((shared, memory.get("timestamp") or 0, memory))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return always + [memory for _, _, memory in scored[:KNOWN_RELATED]]


def _quote_ok(quote: Any, said: str) -> str:
    """The quote, if the user really said it (case and spacing aside)."""
    quote = _squash(quote).strip("\"'“”‘’ ")[:240]
    if len(quote) < 4:
        return ""
    flat = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return quote if flat(quote) and flat(quote) in flat(said) else ""


_NUMBER_WORDS = frozenset("zero one two three four five six seven eight nine ten eleven twelve twenty thirty "
                          "forty fifty hundred thousand first second third fourth fifth".split())


def _without_numbers(text: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split()
    return " ".join(w for w in words if not w.isdigit() and w not in _NUMBER_WORDS and not w[:1].isdigit())


def _find_known(text: str, mine: List[dict], memory_manager, memory_vector) -> tuple:
    """How a new statement relates to what is already known:

    ("same", memory)   it says what that memory says — reaffirm it;
    ("newer", memory)  it restates that memory with a different value — the
                       new statement replaces it;
    (None, None)       it is new.
    """
    from services.memory.memory_extractor import _is_text_duplicate

    by_id = {m["id"]: m for m in mine}
    exact = memory_manager.find_duplicates(text, mine)
    if exact:
        return "same", exact[0]
    bare = _without_numbers(text)
    for memory in mine:
        # Identical but for the numbers: "three hives" has become "four hives".
        if bare and len(bare.split()) >= 2 and bare == _without_numbers(memory.get("text", "")):
            return "newer", memory
    if memory_vector is not None and getattr(memory_vector, "healthy", False):
        try:
            match = memory_vector.find_similar(text, threshold=0.92)
            if match in by_id:
                return "same", by_id[match]
            # Close in meaning but not the same statement. Only a very close
            # match counts as a restatement; anything looser is kept as a
            # fact of its own rather than risk overwriting a different one.
            match = memory_vector.find_similar(text, threshold=0.86)
            if match in by_id:
                return "newer", by_id[match]
        except Exception as exc:
            logger.warning("memory dedup (vector) unavailable, using text: %s", exc)
    for memory in mine:
        # Three quarters of the words in common: the same statement reworded.
        # (Looser than that and "…about bees" would swallow "…about wasps".)
        if _is_text_duplicate(text, [memory], threshold=0.75):
            return "same", memory
    return None, None


async def _wait_until_quiet(label: str) -> None:
    """Background memory work waits for a lull: no reply streaming, nobody
    typing. It shares the model with the chat, and the chat comes first."""
    try:
        from src.interactive_gate import wait_for_interactive_quiet
        await wait_for_interactive_quiet(label)
    except Exception:
        pass


async def learn_from_session(session, memory_manager, memory_vector, endpoint_url: str, model: str,
                             headers: Optional[dict] = None) -> Dict[str, Any]:
    """Run one learning pass for the session's owner. Designed for the
    background queue: logs, never raises. Returns what it did."""
    result: Dict[str, Any] = {"added": 0, "updated": 0, "removed": 0, "confirmed": 0, "notes": 0,
                              "topics": [], "episode": False}
    chat = _session_key(session) or f"session-{id(session)}"
    if chat in _waiting:
        # A pass for this chat is already waiting for a lull; when it runs it
        # reads the chat as it is then, these messages included.
        result["skipped"] = "a pass for this chat is already waiting"
        return result
    _waiting.add(chat)
    try:
        await _wait_until_quiet("memory learning")
    finally:
        _waiting.discard(chat)
    try:
        return await _learn(session, memory_manager, memory_vector, endpoint_url, model, headers, result)
    except Exception as exc:
        logger.error("memory learning pass failed: %s", exc, exc_info=True)
        return result


async def _learn(session, memory_manager, memory_vector, endpoint_url, model, headers, result) -> Dict[str, Any]:
    from services.memory.memory_extractor import _fallback_memory_candidates, _parse_extraction_json
    from services.memory.profile import _parse_object

    # Look at everything since the last pass on this chat (passes can be far
    # apart when the person chats without pausing), within limits.
    chat = _session_key(session) or f"session-{id(session)}"
    try:
        total = len(session.get_context_messages())
    except Exception:
        total = 0
    since = total - _seen_messages.get(chat, max(total - WINDOW, 0))
    lines, said, message_count = _transcript(session, window=min(max(since + 2, WINDOW), MAX_WINDOW))
    if len(lines) < 2 or not said.strip():
        return result
    owner = getattr(session, "owner", None)
    owner = owner if isinstance(owner, str) and owner else None
    session_id = _session_key(session)
    everything = memory_manager.load_all()
    mine = [m for m in everything if m.get("owner") == owner] if owner else list(everything)
    rules = mind.get_rules(memory_manager, owner)
    blocked = set(rules["blocked_kinds"])

    known = _known_for_prompt(mine, said)
    handles = {f"m{index + 1}": memory for index, memory in enumerate(known)}
    earlier = mind.get_episode(memory_manager, owner, session_id) if session_id else None

    parts = []
    if handles:
        parts.append("KNOWN (id [category] text):\n" + "\n".join(
            f"{handle} [{memory.get('category') or 'fact'}] {_squash(memory.get('text'))[:180]}"
            for handle, memory in handles.items()))
    else:
        parts.append("KNOWN: nothing yet.")
    names = mind.topic_names(memory_manager, owner)[:40]
    if names:
        parts.append("KNOWN TOPICS: " + "; ".join(names))
    if blocked:
        parts.append("NEVER STORE anything about: " + "; ".join(mind.SENSITIVE_LABELS[k] for k in sorted(blocked)))
    if earlier and earlier.get("summary"):
        parts.append("EARLIER SUMMARY OF THIS CONVERSATION: " + earlier["summary"])
    parts.append("LATEST MESSAGES:\n" + "\n\n".join(lines))
    parts.append("Return the JSON object now.")

    plan: dict = {}
    if endpoint_url and model:
        try:
            from src.llm_core import llm_call_async

            raw = await llm_call_async(
                endpoint_url, model,
                [{"role": "system", "content": LEARN_SYSTEM_PROMPT}, {"role": "user", "content": "\n\n".join(parts)}],
                temperature=0.1, max_tokens=6000, headers=headers,
            )
            plan = _parse_object(raw)
            if not any(key in plan for key in ("add", "update", "remove", "topics", "episode")):
                listed = _parse_extraction_json(raw)          # an older-style bare list of facts
                plan = {"add": listed} if listed else {}
            # The model has read these messages. (When it could not be reached
            # they stay unread, so the next pass picks them up.)
            _seen_messages[chat] = total
            if len(_seen_messages) > 2000:
                for stale in list(_seen_messages)[:1000]:
                    _seen_messages.pop(stale, None)
        except Exception as exc:
            logger.warning("memory learning: model call failed, using the pattern fallback: %s", exc)
    adds = [a for a in plan.get("add") or [] if isinstance(a, (dict, str))] if isinstance(plan, dict) else []
    try:
        adds += _fallback_memory_candidates([{"role": "user", "content": said}]) or []
    except Exception:
        pass

    now = int(time.time())
    touched_vector: Dict[str, Optional[str]] = {}       # memory id -> text to (re)index, None to remove
    new_entries: List[dict] = []
    removed_ids: set = set()

    def add_fact(text: str, category: str, quote: str = "", sensitive: Optional[str] = None) -> bool:
        if len(new_entries) >= MAX_ADD:
            return False
        text = _squash(text)[:220]
        if len(text) < 5:
            return False
        relation, known_one = _find_known(text, [m for m in mine if m["id"] not in removed_ids] + new_entries,
                                          memory_manager, memory_vector)
        if relation == "newer" and known_one not in new_entries and not is_protected(known_one) \
                and known_one["id"] not in touched_vector:
            # A known fact, restated with a new value: the fact moves on
            # instead of being kept twice.
            known_one["text"] = text
            known_one["updated"] = now
            if session_id:
                known_one["session_id"] = session_id
            if quote:
                known_one["quote"] = quote
            else:
                known_one.pop("quote", None)
            touched_vector[known_one["id"]] = text
            result["updated"] += 1
            return False
        if relation == "same":
            if known_one not in new_entries:
                known_one["confirmed"] = now
                known_one["mentions"] = int(known_one.get("mentions") or 1) + 1
                result["confirmed"] += 1
            return False
        entry = memory_manager.add_entry(text, source="auto", category=category if category in CATEGORIES else "fact",
                                         owner=owner)
        if category == "identity":
            entry["pinned"] = True
            entry["pinned_by"] = "auto"
        if session_id:
            entry["session_id"] = session_id
        if quote:
            entry["quote"] = quote
        if sensitive:
            entry["sensitive"] = sensitive
        new_entries.append(entry)
        touched_vector[entry["id"]] = text
        return True

    # Changes to what is already known come first, so a fact the user just
    # corrected is not also kept in its old form.
    updates = [u for u in plan.get("update") or [] if isinstance(u, dict)] if isinstance(plan, dict) else []
    for item in updates[:MAX_UPDATE]:
        memory = handles.get(str(item.get("id") or "").strip())
        text = _squash(item.get("text"))[:220]
        if memory is None or len(text) < 5 or mind.similar(text, memory.get("text", ""), 0.95):
            continue
        quote = _quote_ok(item.get("quote"), said)
        if is_protected(memory):
            # Theirs to change, not the model's: keep it, and record what they said now beside it.
            if add_fact(text, memory.get("category") or "fact", quote):
                result["added"] += 1
            continue
        memory["text"] = text
        memory["updated"] = now
        if session_id:
            memory["session_id"] = session_id
        if quote:
            memory["quote"] = quote
        else:
            memory.pop("quote", None)
        touched_vector[memory["id"]] = text
        result["updated"] += 1

    removals = [r for r in plan.get("remove") or [] if isinstance(r, dict)] if isinstance(plan, dict) else []
    for item in removals[:MAX_REMOVE]:
        memory = handles.get(str(item.get("id") or "").strip())
        if memory is None or is_protected(memory) or memory["id"] in touched_vector:
            continue
        removed_ids.add(memory["id"])
        touched_vector[memory["id"]] = None
        result["removed"] += 1

    for item in adds:
        if isinstance(item, str):
            item = {"text": item}
        sensitive = item.get("sensitive") if item.get("sensitive") in mind.SENSITIVE_KINDS else None
        if sensitive in blocked:
            continue
        if add_fact(item.get("text"), str(item.get("category") or "fact"), _quote_ok(item.get("quote"), said), sensitive):
            result["added"] += 1

    if new_entries or result["updated"] or removed_ids or result["confirmed"]:
        final = [m for m in everything if m.get("id") not in removed_ids] + new_entries
        with journal.cause("auto", session_id=session_id):
            memory_manager.save(final)
        if memory_vector is not None and getattr(memory_vector, "healthy", False):
            for memory_id, text in touched_vector.items():
                try:
                    if memory_id not in {e["id"] for e in new_entries}:
                        memory_vector.remove(memory_id)
                    if text is not None:
                        memory_vector.add(memory_id, text)
                except Exception as exc:
                    logger.warning("memory vector sync failed for %s: %s", memory_id, exc)
        try:
            from src.event_bus import fire_event
            for _ in range(result["added"]):
                fire_event("memory_added", owner)
        except Exception:
            logger.debug("memory_added event dispatch failed", exc_info=True)

    # Dossier notes and the conversation digest.
    with journal.cause("auto", session_id=session_id):
        topics = [t for t in plan.get("topics") or [] if isinstance(t, dict)] if isinstance(plan, dict) else []
        for topic in topics[:MAX_TOPICS]:
            kind = topic.get("kind") if topic.get("kind") in mind.TOPIC_KINDS else "other"
            if _KIND_SENSITIVE.get(kind) in blocked:
                continue
            notes = [n for n in topic.get("notes") or [] if isinstance(n, str)][:MAX_NOTES]
            keywords = [k for k in topic.get("keywords") or [] if isinstance(k, str)][:6]
            saved, count = mind.add_notes(memory_manager, owner, str(topic.get("name") or ""), notes, kind=kind,
                                          keywords=keywords, session_id=session_id)
            if saved is not None:
                result["topics"].append(saved["name"])
                result["notes"] += count
        episode = plan.get("episode") if isinstance(plan, dict) else None
        if isinstance(episode, dict) and session_id and _squash(episode.get("summary")):
            saved = mind.save_episode(
                memory_manager, owner, session_id, _squash(episode.get("title")), _squash(episode.get("summary")),
                outcomes=[o for o in episode.get("outcomes") or [] if isinstance(o, str)],
                topics=result["topics"], messages=message_count,
            )
            result["episode"] = saved is not None

    logger.info("Memory learning for %s: +%d facts, %d updated, %d removed, %d reaffirmed, %d notes in %d topics%s",
                owner or "single user", result["added"], result["updated"], result["removed"], result["confirmed"],
                result["notes"], len(result["topics"]), ", conversation digest" if result["episode"] else "")

    if result["added"]:
        # Same duplicate-merging cadence as before: every few additions.
        try:
            from services.memory import memory_extractor as _extractor

            _extractor._extractions_since_audit += result["added"]
            if _extractor._extractions_since_audit >= _extractor.AUDIT_INTERVAL and endpoint_url and model:
                _extractor._extractions_since_audit = 0
                with journal.cause("tidy"):
                    await _extractor.audit_memories(memory_manager, memory_vector, endpoint_url, model, headers,
                                                    owner=owner)
        except Exception as exc:
            logger.warning("memory tidy after learning failed: %s", exc)
    return result
