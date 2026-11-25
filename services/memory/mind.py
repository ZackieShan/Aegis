"""Deep memory: what is known about each subject in a person's life, a digest
of every conversation, and the rules the person has set for all of it.

Single facts (``src/memory.py``) and the profile written from them
(``profile.py``) are the surface. This is the depth underneath:

* **Topics** — one dossier per subject the person keeps coming back to (a
  hobby, a project, a person, a health matter…). Every conversation that
  touches it adds dated notes; now and then the model rewrites the dossier's
  digest — summary, where things stand, a timeline, open questions — from
  those notes. A topic is recalled in full when a message is about it.
* **Conversations** — a short digest of each chat (what it was about, what
  was decided). The most recent are always in view; older ones are recalled
  when they bear on what is being asked.
* **Rules** — kinds of things the person does not want kept, lines they
  wrote into their profile themselves, and lines they removed for good.

All of it is per account, stored as plain JSON beside the memory file
(see ``store.py``), journaled (``journal.py``) and exported by
:func:`export_all`. Nothing here is used in Nobody mode.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services.memory import journal, store

logger = logging.getLogger(__name__)

TOPIC_KINDS = ("interest", "project", "person", "health", "home", "work", "place", "other")
SENSITIVE_KINDS = ("health", "money", "location", "relationships", "work", "beliefs")
SENSITIVE_LABELS = {
    "health": "Health and medical", "money": "Money and finances", "location": "Where I am and live",
    "relationships": "Family and relationships", "work": "Work and employer", "beliefs": "Politics and religion",
}

MAX_TOPICS = 150
MAX_NOTES_PER_TOPIC = 200
MAX_EPISODES = 600
NOTE_CHARS = 280

DIGEST_MIN_NEW_NOTES = 2             # rewrite a topic's digest once this many notes are new to it
# One-word topic names that are also everyday words: seeing the word in a
# message is not, by itself, a sign the message is about the topic.
_EVERYDAY = frozenset("work home health family life money food car house job school kid kids travel music "
                      "garden cooking reading fitness sleep".split())
DIGEST_MIN_INTERVAL_S = 10 * 60
_last_digest_attempt: Dict[str, float] = {}

_STOP = frozenset("""
a about above after again all also am an and any are as at be because been before being below between both but by can
could did do does doing don down during each few for from further had has have having he her here hers him his how i if
in into is it its just like me more most my no nor not now of off on once only or other our out over own same she should
so some such than that the their them then there these they this those through to too under until up very was we were
what when where which while who why will with would you your yours get got make made need want know think going really
thing things good new use used using one two also much many lot way well still even back see say said tell ask asked
""".split())


# ── small helpers ──────────────────────────────────────────────────────────

def _squash(value: Any) -> str:
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _stem(word: str) -> str:
    """Enough to make "hives" meet "hive" and "bees" meet "bee"."""
    for suffix in ("ing", "ies", "es", "ed"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str) -> List[str]:
    """Content words of a text, lightly stemmed."""
    return [_stem(w) for w in re.findall(r"[a-z0-9][a-z0-9'-]{2,}", str(text or "").lower())
            if w not in _STOP]


def similar(a: str, b: str, threshold: float = 0.8) -> bool:
    """Same statement, give or take wording (word overlap)."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return len(ta & tb) / max(len(ta | tb), 1) >= threshold


def _file(memory_manager, owner: Optional[str], name: str):
    folder = store.mind_dir(memory_manager, owner)
    return folder / name if folder is not None else None


# ── rules ──────────────────────────────────────────────────────────────────

def get_rules(memory_manager, owner: Optional[str]) -> dict:
    raw = store.read_json(_file(memory_manager, owner, "rules.json"), {})
    locked = raw.get("profile_locked") if isinstance(raw.get("profile_locked"), dict) else {}
    return {
        "blocked_kinds": [k for k in raw.get("blocked_kinds") or [] if k in SENSITIVE_KINDS],
        "profile_locked": {str(section): [_squash(t) for t in texts if _squash(t)]
                           for section, texts in locked.items() if isinstance(texts, list)},
        "profile_suppressed": [_squash(t) for t in raw.get("profile_suppressed") or [] if _squash(t)],
        "updated": raw.get("updated"),
    }


def _save_rules(memory_manager, owner: Optional[str], rules: dict) -> dict:
    rules = dict(rules, updated=time.time())
    store.write_json(_file(memory_manager, owner, "rules.json"), rules)
    return rules


def set_blocked_kinds(memory_manager, owner: Optional[str], kinds: Iterable[str]) -> dict:
    rules = get_rules(memory_manager, owner)
    before = list(rules["blocked_kinds"])
    rules["blocked_kinds"] = [k for k in SENSITIVE_KINDS if k in set(kinds or [])]
    if rules["blocked_kinds"] != before:
        journal.record(memory_manager, owner, "rules", "blocked_kinds", None,
                       before={"blocked_kinds": before}, after={"blocked_kinds": rules["blocked_kinds"]})
    return _save_rules(memory_manager, owner, rules)


def lock_profile_line(memory_manager, owner: Optional[str], section: str, text: str,
                      replaces: Optional[str] = None) -> dict:
    """The person wrote (or rewrote) this line of their profile: keep it."""
    text = _squash(text)[:200]
    rules = get_rules(memory_manager, owner)
    lines = rules["profile_locked"].setdefault(section, [])
    if replaces:
        lines[:] = [line for line in lines if not similar(line, replaces, 0.9)]
        if not any(similar(replaces, s, 0.9) for s in rules["profile_suppressed"]):
            rules["profile_suppressed"].append(_squash(replaces)[:200])
    if text and not any(similar(text, line, 0.9) for line in lines):
        lines.append(text)
    rules["profile_suppressed"] = [s for s in rules["profile_suppressed"] if not similar(s, text, 0.9)]
    journal.record(memory_manager, owner, "profile", "edit_line" if replaces else "add_line", section,
                   before={"text": replaces} if replaces else None, after={"text": text}, label=section)
    return _save_rules(memory_manager, owner, rules)


def suppress_profile_line(memory_manager, owner: Optional[str], section: str, text: str) -> dict:
    """The person removed this line: never put it back."""
    text = _squash(text)[:200]
    rules = get_rules(memory_manager, owner)
    if section in rules["profile_locked"]:
        rules["profile_locked"][section] = [l for l in rules["profile_locked"][section] if not similar(l, text, 0.9)]
    if text and not any(similar(text, s, 0.9) for s in rules["profile_suppressed"]):
        rules["profile_suppressed"].append(text)
    rules["profile_suppressed"] = rules["profile_suppressed"][-200:]
    journal.record(memory_manager, owner, "profile", "remove_line", section, before={"text": text}, label=section)
    return _save_rules(memory_manager, owner, rules)


def apply_rules_to_profile(profile: dict, rules: dict) -> dict:
    """A profile with the person's own edits enforced: lines they removed are
    out, lines they wrote are in (first). Works on a copy."""
    if not profile and not any((rules.get("profile_locked") or {}).values()):
        return profile
    suppressed = rules.get("profile_suppressed") or []
    locked = rules.get("profile_locked") or {}
    if not suppressed and not any(locked.values()):
        return profile

    def gone(text: str) -> bool:
        return any(similar(text, s, 0.85) for s in suppressed)

    out = dict(profile or {})
    for section in ("identity", "people", "preferences", "projects", "goals", "patterns"):
        items = [item for item in out.get(section) or [] if not gone(item)]
        mine = [line for line in locked.get(section) or [] if not gone(line)]
        items = mine + [item for item in items if not any(similar(item, line, 0.85) for line in mine)]
        out[section] = items
    interests = []
    for item in out.get("interests") or []:
        if gone(item.get("name", "")):
            continue
        interests.append(dict(item, notes=[n for n in item.get("notes") or [] if not gone(n)]))
    known = {_norm(i["name"]) for i in interests}
    for name in locked.get("interests") or []:
        if _norm(name) not in known and not gone(name):
            interests.insert(0, {"name": name, "notes": []})
    out["interests"] = interests
    if out.get("summary") and any(_norm(s) and _norm(s) in _norm(out["summary"]) for s in suppressed):
        out["summary"] = ""                      # it repeats something the person removed
    return out


# ── topics ─────────────────────────────────────────────────────────────────

def topic_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:48]
    return slug or uuid.uuid4().hex[:8]


def _load_topics(memory_manager, owner: Optional[str]) -> Dict[str, dict]:
    raw = store.read_json(_file(memory_manager, owner, "topics.json"), {})
    topics = raw.get("topics") if isinstance(raw.get("topics"), dict) else {}
    return {key: value for key, value in topics.items() if isinstance(value, dict) and value.get("name")}


def _save_topics(memory_manager, owner: Optional[str], topics: Dict[str, dict]) -> None:
    store.write_json(_file(memory_manager, owner, "topics.json"), {"version": 1, "topics": topics})


def _find_topic(topics: Dict[str, dict], name: str) -> Optional[dict]:
    """The existing topic a name refers to: same name give or take case,
    plural and punctuation, or a recorded alias."""
    wanted = _norm(name)
    if not wanted:
        return None
    wanted_stems = " ".join(_stem(w) for w in wanted.split())
    for topic in topics.values():
        names = [topic.get("name", "")] + list(topic.get("aliases") or [])
        for candidate in names:
            cand = _norm(candidate)
            if cand == wanted or " ".join(_stem(w) for w in cand.split()) == wanted_stems:
                return topic
    return None


def _same_subject(topics: Dict[str, dict], name: str, keywords: Iterable[str]) -> Optional[dict]:
    """A topic under another name that is plainly the same subject: the names
    share a word and most of the words used to talk about them coincide
    ("car restoration" and "Toyota pickup restoration", both with toyota,
    pickup, brakes). Deliberately strict — two projects of the same kind
    stay two topics."""
    name_words = set(tokens(name))
    words = {_stem(w) for kw in keywords or [] for w in _norm(kw).split()} | name_words
    best, best_shared = None, 0
    for topic in topics.values():
        other_name = set(tokens(topic.get("name", "")))
        if not (name_words & other_name):
            continue
        other = {_stem(w) for kw in topic.get("keywords") or [] for w in kw.split()} | other_name
        shared = len(words & other)
        if shared >= 3 and shared / max(min(len(words), len(other)), 1) >= 0.5 and shared > best_shared:
            best, best_shared = topic, shared
    return best


def _unseen_notes(topic: dict) -> List[dict]:
    seen = set((topic.get("digest") or {}).get("notes_seen") or [])
    return [n for n in topic.get("notes") or [] if n.get("id") not in seen]


def _public_topic(topic: dict, full: bool = False) -> dict:
    notes = topic.get("notes") or []
    view = {
        "id": topic.get("id"), "name": topic.get("name"), "kind": topic.get("kind") or "other",
        "summary": topic.get("summary") or "", "state": topic.get("state") or [], "open": topic.get("open") or [],
        "keywords": topic.get("keywords") or [], "created": topic.get("created"), "updated": topic.get("updated"),
        "note_count": len(notes), "new_notes": len(_unseen_notes(topic)),
        "digest_updated": (topic.get("digest") or {}).get("updated"),
        "locked": topic.get("locked") or [],
    }
    if full:
        view["timeline"] = topic.get("timeline") or []
        view["notes"] = sorted(notes, key=lambda n: -(n.get("ts") or 0))
    return view


def list_topics(memory_manager, owner: Optional[str]) -> List[dict]:
    topics = _load_topics(memory_manager, owner)
    return [_public_topic(t) for t in sorted(topics.values(), key=lambda t: -(t.get("updated") or 0))]


def get_topic(memory_manager, owner: Optional[str], topic_key: str) -> Optional[dict]:
    topic = _load_topics(memory_manager, owner).get(topic_key)
    return _public_topic(topic, full=True) if topic else None


def topic_names(memory_manager, owner: Optional[str]) -> List[str]:
    topics = _load_topics(memory_manager, owner)
    return [t["name"] for t in sorted(topics.values(), key=lambda t: -(t.get("updated") or 0))]


def add_notes(memory_manager, owner: Optional[str], name: str, notes: Iterable[str], kind: str = "other",
              keywords: Optional[Iterable[str]] = None, session_id: Optional[str] = None,
              by: str = "auto", source: Optional[dict] = None) -> Tuple[Optional[dict], int]:
    """Add dated notes to a topic, creating it if needed. Notes that say what
    an existing note already says are skipped. Returns (topic, how many added).

    ``source`` says where a note came from when that is not a chat — a passage
    highlighted in the reader carries ``{"kind": "reading", "title", "url"}``
    so it can be traced back to the page."""
    name = _squash(name)[:60]
    cleaned = [_squash(n)[:NOTE_CHARS] for n in notes or []]
    cleaned = [n for n in cleaned if len(n) >= 5]
    path = _file(memory_manager, owner, "topics.json")
    if path is None or len(name) < 2 or not cleaned:
        return None, 0
    with store.lock_for(path):
        topics = _load_topics(memory_manager, owner)
        topic = _find_topic(topics, name)
        if topic is None and by == "auto":
            topic = _same_subject(topics, name, keywords or [])
            if topic is not None and _norm(name) not in [_norm(a) for a in topic.get("aliases") or []]:
                topic.setdefault("aliases", []).append(name)
        now = time.time()
        created = False
        if topic is None:
            if len(topics) >= MAX_TOPICS:
                return None, 0
            key = topic_id(name)
            while key in topics:
                key = f"{key[:44]}-{uuid.uuid4().hex[:3]}"
            topic = topics[key] = {"id": key, "name": name, "kind": kind if kind in TOPIC_KINDS else "other",
                                   "keywords": [], "created": now, "updated": now, "summary": "", "state": [],
                                   "timeline": [], "open": [], "notes": [], "digest": {}}
            created = True
        elif topic.get("kind") in (None, "other") and kind in TOPIC_KINDS:
            topic["kind"] = kind
        added: List[dict] = []
        existing = [n.get("text", "") for n in topic.get("notes") or []]
        for text in cleaned:
            if any(similar(text, old) for old in existing):
                continue
            note = {"id": uuid.uuid4().hex[:10], "text": text, "ts": now, "by": by}
            if session_id:
                note["session_id"] = str(session_id)
            if isinstance(source, dict) and source.get("kind"):
                note["source"] = {key: str(source.get(key) or "")[:400] for key in ("kind", "title", "url")}
            topic.setdefault("notes", []).append(note)
            existing.append(text)
            added.append(note)
        merged = list(topic.get("keywords") or [])
        for word in keywords or []:
            word = _norm(word)[:30]
            if word and word not in merged:
                merged.append(word)
        topic["keywords"] = merged[:24]
        if not added and not created:
            _save_topics(memory_manager, owner, topics)          # keywords / kind may have changed
            return _public_topic(topic), 0
        topic["notes"] = topic["notes"][-MAX_NOTES_PER_TOPIC:]
        topic["updated"] = now
        _save_topics(memory_manager, owner, topics)
    if created:
        journal.record(memory_manager, owner, "topic", "create", topic["id"], after={"name": name}, label=name)
    for note in added:
        journal.record(memory_manager, owner, "topic", "note_add", topic["id"],
                       after={"note_id": note["id"], "text": note["text"]}, label=topic["name"])
    return _public_topic(topic), len(added)


def update_topic(memory_manager, owner: Optional[str], topic_key: str, fields: dict) -> Optional[dict]:
    """The person edits a topic. Digest fields they touch are locked: later
    rewrites by the model leave them alone until :func:`unlock_topic`."""
    path = _file(memory_manager, owner, "topics.json")
    if path is None:
        return None
    with store.lock_for(path):
        topics = _load_topics(memory_manager, owner)
        topic = topics.get(topic_key)
        if topic is None:
            return None
        before, after = {}, {}
        locked = set(topic.get("locked") or [])
        if isinstance(fields.get("name"), str) and _squash(fields["name"]) and _squash(fields["name"]) != topic["name"]:
            new_name = _squash(fields["name"])[:60]
            other = _find_topic(topics, new_name)
            if other is not None and other is not topic:
                raise ValueError(f"There is already a topic called “{other['name']}”.")
            before["name"], after["name"] = topic["name"], new_name
            topic.setdefault("aliases", []).append(topic["name"])
            topic["name"] = new_name
        if fields.get("kind") in TOPIC_KINDS and fields["kind"] != topic.get("kind"):
            before["kind"], after["kind"] = topic.get("kind"), fields["kind"]
            topic["kind"] = fields["kind"]
        if isinstance(fields.get("summary"), str) and _squash(fields["summary"]) != (topic.get("summary") or ""):
            before["summary"], after["summary"] = topic.get("summary") or "", _squash(fields["summary"])[:900]
            topic["summary"] = after["summary"]
            locked.add("summary")
        for key, cap in (("state", 12), ("open", 10)):
            if isinstance(fields.get(key), list):
                items = [_squash(i)[:220] for i in fields[key] if _squash(i)][:cap]
                if items != (topic.get(key) or []):
                    before[key], after[key] = topic.get(key) or [], items
                    topic[key] = items
                    locked.add(key)
        if not after:
            return _public_topic(topic, full=True)
        topic["locked"] = sorted(locked)
        topic["updated"] = time.time()
        _save_topics(memory_manager, owner, topics)
    journal.record(memory_manager, owner, "topic", "edit", topic_key, before=before, after=after, label=topic["name"])
    return _public_topic(topic, full=True)


def unlock_topic(memory_manager, owner: Optional[str], topic_key: str) -> Optional[dict]:
    path = _file(memory_manager, owner, "topics.json")
    if path is None:
        return None
    with store.lock_for(path):
        topics = _load_topics(memory_manager, owner)
        topic = topics.get(topic_key)
        if topic is None:
            return None
        topic["locked"] = []
        topic["digest"] = dict(topic.get("digest") or {}, notes_seen=[])      # due for a rewrite
        _save_topics(memory_manager, owner, topics)
    return _public_topic(topic, full=True)


def delete_topic(memory_manager, owner: Optional[str], topic_key: str) -> bool:
    path = _file(memory_manager, owner, "topics.json")
    if path is None:
        return False
    with store.lock_for(path):
        topics = _load_topics(memory_manager, owner)
        topic = topics.pop(topic_key, None)
        if topic is None:
            return False
        _save_topics(memory_manager, owner, topics)
    journal.record(memory_manager, owner, "topic", "delete", topic_key,
                   before={"name": topic["name"], "notes": len(topic.get("notes") or []),
                           "summary": topic.get("summary") or ""}, label=topic["name"])
    return True


def merge_topics(memory_manager, owner: Optional[str], source_key: str, target_key: str) -> Optional[dict]:
    """Fold one topic into another: its notes move over, its name becomes
    an alias, and the target's digest is due for a rewrite."""
    path = _file(memory_manager, owner, "topics.json")
    if path is None or source_key == target_key:
        return None
    with store.lock_for(path):
        topics = _load_topics(memory_manager, owner)
        source, target = topics.get(source_key), topics.get(target_key)
        if source is None or target is None:
            return None
        have = [n.get("text", "") for n in target.get("notes") or []]
        moved = 0
        for note in source.get("notes") or []:
            if not any(similar(note.get("text", ""), old) for old in have):
                target.setdefault("notes", []).append(note)
                have.append(note.get("text", ""))
                moved += 1
        target["notes"] = sorted(target["notes"], key=lambda n: n.get("ts") or 0)[-MAX_NOTES_PER_TOPIC:]
        target["aliases"] = list(dict.fromkeys((target.get("aliases") or []) + [source["name"]]
                                               + (source.get("aliases") or [])))
        target["keywords"] = list(dict.fromkeys((target.get("keywords") or []) + (source.get("keywords") or [])))[:24]
        target["digest"] = dict(target.get("digest") or {}, notes_seen=[])
        target["updated"] = time.time()
        del topics[source_key]
        _save_topics(memory_manager, owner, topics)
    journal.record(memory_manager, owner, "topic", "merge", target_key,
                   before={"name": source["name"], "notes": len(source.get("notes") or [])},
                   after={"name": target["name"], "moved": moved}, label=target["name"])
    return _public_topic(target, full=True)


def delete_note(memory_manager, owner: Optional[str], topic_key: str, note_id: str) -> bool:
    path = _file(memory_manager, owner, "topics.json")
    if path is None:
        return False
    with store.lock_for(path):
        topics = _load_topics(memory_manager, owner)
        topic = topics.get(topic_key)
        if topic is None:
            return False
        note = next((n for n in topic.get("notes") or [] if n.get("id") == note_id), None)
        if note is None:
            return False
        topic["notes"] = [n for n in topic["notes"] if n is not note]
        # The digest may repeat what the note said: it is due for a rewrite
        # from the notes that remain, and until then it is not shown as current.
        topic["digest"] = {}
        if "summary" not in (topic.get("locked") or []):
            topic["summary"] = ""
        for key in ("state", "open"):
            if key not in (topic.get("locked") or []):
                topic[key] = []
        topic["timeline"] = []
        topic["updated"] = time.time()
        _save_topics(memory_manager, owner, topics)
    journal.record(memory_manager, owner, "topic", "note_delete", topic_key,
                   before={"note_id": note_id, "text": note.get("text")}, label=topic["name"])
    return True


def _digest_due(topic: dict, now: float) -> bool:
    unseen = len(_unseen_notes(topic))
    if not topic.get("notes"):
        return False
    if not (topic.get("digest") or {}).get("updated"):
        return len(topic["notes"]) >= 2
    return unseen >= DIGEST_MIN_NEW_NOTES


TOPIC_SYSTEM_PROMPT = (
    "You keep a dossier on ONE subject in a person's life, from dated notes taken while talking with them. "
    "Rewrite the dossier as JSON.\n\n"
    "Rules:\n"
    "- Use only what the notes say. Never invent or assume.\n"
    "- The notes are numbered in the order they were taken: a higher number is newer. When two notes "
    "disagree (a count, a status, a plan), the higher-numbered one is the truth NOW; the older value "
    "belongs only in the timeline, as what used to be.\n"
    "- summary and state describe the present. Read every note to the end before writing them.\n"
    "- Plain short statements written without a subject (no 'the user', no name, no he/she).\n\n"
    "Return ONE JSON object and nothing else:\n"
    "{\n"
    '  "summary": "two or three sentences: what this is to them and where it stands",\n'
    '  "state": ["what is true right now — equipment, numbers, people involved, routines — at most 8"],\n'
    '  "timeline": [{"when": "date or period as given, e.g. 2026-11 or last winter", "text": "what happened"}],\n'
    '  "open": ["questions they are working on, things they plan or want — at most 6"],\n'
    '  "keywords": ["single words someone would use when talking about this — at most 10"]\n'
    "}\n"
    "At most 12 timeline entries, oldest first. Use empty lists where the notes say nothing."
)


def _day(ts: Optional[float]) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return ""


async def consolidate_topic(memory_manager, owner: Optional[str], topic_key: str, endpoint_url: str, model: str,
                            headers: Optional[dict] = None) -> Optional[dict]:
    """Have the model rewrite one topic's digest from its notes."""
    topics = _load_topics(memory_manager, owner)
    topic = topics.get(topic_key)
    if topic is None or not topic.get("notes") or not endpoint_url or not model:
        return _public_topic(topic, full=True) if topic else None
    _last_digest_attempt[f"{store.mind_dir(memory_manager, owner)}|{topic_key}"] = time.time()
    notes = sorted(topic["notes"], key=lambda n: n.get("ts") or 0)[-80:]
    listing = "\n".join(f"{index}. ({_day(n.get('ts'))}) {n.get('text')}" for index, n in enumerate(notes, 1))
    locked = set(topic.get("locked") or [])
    try:
        from services.memory.profile import _parse_object
        from src.llm_core import llm_call_async

        raw = await llm_call_async(
            endpoint_url, model,
            [{"role": "system", "content": TOPIC_SYSTEM_PROMPT},
             {"role": "user", "content": f"Subject: {topic['name']} ({topic.get('kind') or 'other'})\n\n"
                                         f"Notes, oldest first (highest number = newest):\n{listing}\n\n"
                                         "Return the dossier JSON now."}],
            temperature=0.1, max_tokens=4000, headers=headers,
        )
        written = _parse_object(raw)
    except Exception as exc:
        logger.warning("topic digest for %s failed: %s", topic.get("name"), exc)
        return _public_topic(topic, full=True)
    if not isinstance(written, dict) or not (written.get("summary") or written.get("state")):
        return _public_topic(topic, full=True)

    path = _file(memory_manager, owner, "topics.json")
    with store.lock_for(path):
        topics = _load_topics(memory_manager, owner)
        topic = topics.get(topic_key)
        if topic is None:
            return None
        still_there = {n.get("id") for n in topic.get("notes") or []}
        if any(n.get("id") not in still_there for n in notes):
            return _public_topic(topic, full=True)       # a note went while the model wrote: try again later
        if "summary" not in locked:
            topic["summary"] = _squash(written.get("summary"))[:900]
        for key, cap in (("state", 8), ("open", 6)):
            if key not in locked and isinstance(written.get(key), list):
                topic[key] = [_squash(i)[:220] for i in written[key] if _squash(i)][:cap]
        timeline = []
        for item in written.get("timeline") if isinstance(written.get("timeline"), list) else []:
            if isinstance(item, dict) and _squash(item.get("text")):
                timeline.append({"when": _squash(item.get("when"))[:40], "text": _squash(item["text"])[:220]})
        topic["timeline"] = timeline[:12]
        merged = list(topic.get("keywords") or [])
        for word in written.get("keywords") if isinstance(written.get("keywords"), list) else []:
            word = _norm(word)[:30] if isinstance(word, str) else ""
            if word and word not in merged:
                merged.append(word)
        topic["keywords"] = merged[:24]
        topic["digest"] = {"updated": time.time(), "notes_seen": [n.get("id") for n in notes], "source": "model"}
        _save_topics(memory_manager, owner, topics)
    journal.record(memory_manager, owner, "topic", "digest", topic_key,
                   after={"summary": topic.get("summary") or "", "notes": len(notes)}, label=topic["name"])
    return _public_topic(topic, full=True)


async def maybe_consolidate(memory_manager, owner: Optional[str], endpoint_url: str, model: str,
                            headers: Optional[dict] = None, limit: int = 2) -> int:
    """Background-queue entry point: rewrite the digests that are due, the
    ones with the most new notes first. Logs, never raises."""
    done = 0
    try:
        now = time.time()
        base = store.mind_dir(memory_manager, owner)
        due = []
        for key, topic in _load_topics(memory_manager, owner).items():
            if not _digest_due(topic, now):
                continue
            if now - _last_digest_attempt.get(f"{base}|{key}", 0.0) < DIGEST_MIN_INTERVAL_S:
                continue
            due.append((len(_unseen_notes(topic)), key))
        with journal.cause("auto"):
            for _, key in sorted(due, reverse=True)[:limit]:
                try:                                   # the chat comes first: wait for a lull
                    from src.interactive_gate import wait_for_interactive_quiet
                    await wait_for_interactive_quiet("topic digest")
                except Exception:
                    pass
                await consolidate_topic(memory_manager, owner, key, endpoint_url, model, headers)
                done += 1
    except Exception as exc:
        logger.warning("topic digests skipped: %s", exc)
    return done


def relevant_topics(memory_manager, owner: Optional[str], text: str, k: int = 2) -> List[dict]:
    """Topics a message is about: its name (or a keyword) is mentioned, or its
    content shares enough uncommon words with the message."""
    query = set(tokens(text))
    if not query:
        return []
    topics = list(_load_topics(memory_manager, owner).values())
    if not topics:
        return []
    bodies = []
    frequency: Dict[str, int] = {}
    for topic in topics:
        recent = sorted(topic.get("notes") or [], key=lambda n: -(n.get("ts") or 0))[:20]
        body = set(tokens(" ".join([topic.get("summary") or ""] + (topic.get("state") or []) + (topic.get("open") or [])
                                   + [n.get("text") or "" for n in recent])))
        bodies.append(body)
        for token in body:
            frequency[token] = frequency.get(token, 0) + 1
    scored = []
    for topic, body in zip(topics, bodies):
        score = 0.0
        weak: set = set()                # everyday name words: no second helping from the body below
        for candidate in [topic["name"]] + list(topic.get("aliases") or []):
            name = set(tokens(candidate))
            if name and name <= query:
                everyday = len(name) == 1 and (_norm(candidate) in _EVERYDAY or next(iter(name)) in _EVERYDAY)
                score = max(score, 1.0 if everyday else 3.0)
                if everyday:
                    weak |= name
            elif name and len(name) > 1 and len(name & query) >= len(name) - 1:
                score = max(score, 1.0)
        # A keyword is a word people use when talking about this subject:
        # one of them in the message is enough to bring the dossier along.
        keys = {_stem(w) for kw in topic.get("keywords") or [] for w in kw.split()}
        hits = len(keys & query)
        if hits:
            score += 1.25 + 0.5 * min(hits - 1, 2)
        for token in (body & query) - weak:
            score += 0.5 / frequency.get(token, 1) + 0.15
        if score >= 1.2:
            scored.append((score, topic.get("updated") or 0, topic))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [_public_topic(topic, full=True) for _, _, topic in scored[:k]]


def render_topics_for_prompt(topics: List[dict], budget: int = 1500) -> str:
    if not topics:
        return ""
    lines = ["What is known about subjects this message touches (from earlier conversations with this "
             "person; use what is relevant, do not recite it):"]
    for topic in topics:
        head = f"- {topic['name']}"
        if topic.get("summary"):
            head += f": {topic['summary']}"
        lines.append(head)
        if topic.get("state"):
            lines.append("  Now: " + "; ".join(topic["state"][:6]))
        if topic.get("open"):
            lines.append("  Open: " + "; ".join(topic["open"][:4]))
        notes = topic.get("notes") or []
        if notes:
            take = notes[:5] if topic.get("summary") else notes[:10]
            lines.append("  Latest notes: " + " | ".join(f"({_day(n.get('ts'))}) {n.get('text')}" for n in take))
    out, used = [], 0
    for line in lines:
        if used + len(line) + 1 > budget and len(out) > 1:
            break
        out.append(line[: max(budget - used, 80)])
        used += len(out[-1]) + 1
    return "\n".join(out)


# ── conversations ──────────────────────────────────────────────────────────

def _load_episodes(memory_manager, owner: Optional[str]) -> Dict[str, dict]:
    raw = store.read_json(_file(memory_manager, owner, "episodes.json"), {})
    episodes = raw.get("episodes") if isinstance(raw.get("episodes"), dict) else {}
    return {key: value for key, value in episodes.items() if isinstance(value, dict)}


def _save_episodes(memory_manager, owner: Optional[str], episodes: Dict[str, dict]) -> None:
    if len(episodes) > MAX_EPISODES:
        keep = sorted(episodes.values(), key=lambda e: -(e.get("updated") or 0))[:MAX_EPISODES]
        episodes = {e["session_id"]: e for e in keep}
    store.write_json(_file(memory_manager, owner, "episodes.json"), {"version": 1, "episodes": episodes})


def save_episode(memory_manager, owner: Optional[str], session_id: str, title: str, summary: str,
                 outcomes: Optional[Iterable[str]] = None, topics: Optional[Iterable[str]] = None,
                 messages: int = 0) -> Optional[dict]:
    summary = _squash(summary)[:600]
    path = _file(memory_manager, owner, "episodes.json")
    if path is None or not session_id or len(summary) < 10:
        return None
    with store.lock_for(path):
        episodes = _load_episodes(memory_manager, owner)
        now = time.time()
        previous = episodes.get(str(session_id))
        episode = dict(previous or {"session_id": str(session_id), "created": now})
        episode.update(
            title=_squash(title)[:90] or episode.get("title") or "Conversation",
            summary=summary,
            outcomes=[_squash(o)[:200] for o in outcomes or [] if _squash(o)][:6],
            topics=sorted({_squash(t)[:60] for t in list(topics or []) + list(episode.get("topics") or [])
                           if _squash(t)})[:8],
            updated=now, messages=int(messages or episode.get("messages") or 0),
        )
        episodes[str(session_id)] = episode
        _save_episodes(memory_manager, owner, episodes)
    journal.record(memory_manager, owner, "episode", "update" if previous else "add", str(session_id),
                   before={"summary": previous.get("summary")} if previous else None,
                   after={"summary": summary}, label=episode["title"])
    return episode


def get_episode(memory_manager, owner: Optional[str], session_id: str) -> Optional[dict]:
    return _load_episodes(memory_manager, owner).get(str(session_id))


def list_episodes(memory_manager, owner: Optional[str], limit: int = 200) -> List[dict]:
    episodes = _load_episodes(memory_manager, owner)
    return sorted(episodes.values(), key=lambda e: -(e.get("updated") or 0))[: max(1, int(limit))]


def delete_episode(memory_manager, owner: Optional[str], session_id: str) -> bool:
    path = _file(memory_manager, owner, "episodes.json")
    if path is None:
        return False
    with store.lock_for(path):
        episodes = _load_episodes(memory_manager, owner)
        episode = episodes.pop(str(session_id), None)
        if episode is None:
            return False
        _save_episodes(memory_manager, owner, episodes)
    journal.record(memory_manager, owner, "episode", "delete", str(session_id),
                   before={"summary": episode.get("summary")}, label=episode.get("title"))
    return True


def recent_episodes(memory_manager, owner: Optional[str], exclude_session: Optional[str] = None,
                    k: int = 3) -> List[dict]:
    return [e for e in list_episodes(memory_manager, owner, limit=k + 1)
            if e.get("session_id") != str(exclude_session or "")][:k]


def relevant_episodes(memory_manager, owner: Optional[str], text: str, exclude: Iterable[str] = (),
                      k: int = 2) -> List[dict]:
    query = set(tokens(text))
    if len(query) < 2:
        return []
    skip = {str(s) for s in exclude if s}
    scored = []
    for episode in _load_episodes(memory_manager, owner).values():
        if episode.get("session_id") in skip:
            continue
        body = set(tokens(" ".join([episode.get("title") or "", episode.get("summary") or ""]
                                   + (episode.get("outcomes") or []) + (episode.get("topics") or []))))
        shared = len(body & query)
        if shared >= 2 and shared / max(len(query), 1) >= 0.34:
            scored.append((shared, episode.get("updated") or 0, episode))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [episode for _, _, episode in scored[:k]]


def render_episodes_for_prompt(episodes: List[dict], heading: str, budget: int = 700) -> str:
    if not episodes:
        return ""
    lines = [heading]
    for episode in episodes:
        line = f"- ({_day(episode.get('updated'))}) {episode.get('title') or 'Conversation'}: {episode.get('summary')}"
        if episode.get("outcomes"):
            line += " Outcome: " + "; ".join(episode["outcomes"][:3])
        lines.append(line[:420])
    out, used = [], 0
    for line in lines:
        if used + len(line) + 1 > budget and len(out) > 1:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


RECENT_HEADING = ("The latest other conversations with this person, for continuity. They record what was said "
                  "then, not how things stand now: check anything current with a tool rather than quoting them.")
EARLIER_HEADING = ("Earlier conversations that bear on this message:")


# ── everything, at once ────────────────────────────────────────────────────

def counts(memory_manager, owner: Optional[str]) -> dict:
    topics = _load_topics(memory_manager, owner)
    return {
        "topics": len(topics),
        "topic_notes": sum(len(t.get("notes") or []) for t in topics.values()),
        "conversations": len(_load_episodes(memory_manager, owner)),
        "changes": journal.count(memory_manager, owner),
    }


def export_all(memory_manager, owner: Optional[str]) -> dict:
    """Everything stored about one account, in one document."""
    from services.memory import profile as _profile

    topics = _load_topics(memory_manager, owner)
    return {
        "exported": time.time(),
        "account": owner or "",
        "profile": _profile.public_view(_profile.get_profile(owner, memory_manager)),
        "memories": memory_manager.load(owner=owner),
        "topics": [_public_topic(t, full=True) for t in topics.values()],
        "conversations": list_episodes(memory_manager, owner, limit=MAX_EPISODES),
        "rules": get_rules(memory_manager, owner),
        "history": journal.read(memory_manager, owner, limit=10 ** 9),
    }


def export_markdown(memory_manager, owner: Optional[str]) -> str:
    from services.memory import profile as _profile

    data = export_all(memory_manager, owner)
    parts = [f"# Everything Aegis knows about {owner or 'you'}",
             f"_Exported {time.strftime('%Y-%m-%d %H:%M')}_",
             "## Profile", _profile.to_markdown(_profile.get_profile(owner, memory_manager))]
    parts.append("## Facts")
    for memory in sorted(data["memories"], key=lambda m: (m.get("category") or "", -(m.get("timestamp") or 0))):
        line = f"- [{memory.get('category') or 'fact'}] {memory.get('text')}"
        line += f" _(learned {_day(memory.get('timestamp'))}, {memory.get('source') or 'unknown'})_"
        parts.append(line)
    parts.append("## Topics")
    for topic in data["topics"]:
        parts.append(f"### {topic['name']}")
        if topic.get("summary"):
            parts.append(topic["summary"])
        for label, key in (("Now", "state"), ("Open", "open")):
            if topic.get(key):
                parts.append(f"**{label}**\n" + "\n".join(f"- {item}" for item in topic[key]))
        if topic.get("timeline"):
            parts.append("**Timeline**\n" + "\n".join(f"- {t.get('when')}: {t.get('text')}" for t in topic["timeline"]))
        if topic.get("notes"):
            parts.append("**Notes**\n" + "\n".join(f"- ({_day(n.get('ts'))}) {n.get('text')}" for n in topic["notes"]))
    parts.append("## Conversations")
    for episode in data["conversations"]:
        parts.append(f"- ({_day(episode.get('updated'))}) **{episode.get('title')}** — {episode.get('summary')}")
    rules = data["rules"]
    if rules["blocked_kinds"] or rules["profile_suppressed"]:
        parts.append("## Rules")
        if rules["blocked_kinds"]:
            parts.append("Never keep: " + ", ".join(SENSITIVE_LABELS[k] for k in rules["blocked_kinds"]))
        if rules["profile_suppressed"]:
            parts.append("Removed from the profile for good:\n" + "\n".join(f"- {t}" for t in rules["profile_suppressed"]))
    return "\n\n".join(parts) + "\n"


def wipe(memory_manager, owner: Optional[str]) -> None:
    """Delete everything in this module's care for one account, journal
    included. (Facts and the profile are deleted by their own owners.)"""
    folder = store.mind_dir(memory_manager, owner)
    if folder is None or not folder.exists():
        return
    for name in ("topics.json", "episodes.json", "rules.json", "journal.jsonl"):
        try:
            (folder / name).unlink()
        except OSError:
            pass
    try:
        folder.rmdir()
    except OSError:
        pass
