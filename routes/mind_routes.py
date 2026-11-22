"""The memory inspector's API: see, trace, change and remove everything Aegis
keeps about the signed-in person.

Every route works on the caller's own data only. Nothing here lets one
account read or touch another's — administrators included.

    GET    /api/mind/overview                 counts, profile, switches, rules
    POST   /api/mind/preview                  exactly what the model would be given for a message
    GET    /api/mind/memories                 single facts with where each came from
    POST   /api/mind/memories                 add one
    PUT    /api/mind/memories/{id}            edit / recategorise / pin
    DELETE /api/mind/memories/{id}
    GET    /api/mind/related?text=            facts that say much the same as a text
    POST   /api/mind/profile/line             add, edit or remove a line of the profile
    GET    /api/mind/topics[/{id}]            dossiers
    POST   /api/mind/topics                   start one / add a note by name
    PUT    /api/mind/topics/{id}              rename, re-kind, edit the digest
    POST   /api/mind/topics/{id}/merge        fold it into another topic
    POST   /api/mind/topics/{id}/notes        add a note
    DELETE /api/mind/topics/{id}/notes/{nid}
    POST   /api/mind/topics/{id}/rewrite      have the model rewrite the digest now
    DELETE /api/mind/topics/{id}
    GET    /api/mind/conversations            digests of past chats
    DELETE /api/mind/conversations/{sid}
    GET    /api/mind/history                  the journal, newest first
    POST   /api/mind/history/{id}/undo
    PUT    /api/mind/rules                    kinds of information never to keep
    GET    /api/mind/export?format=json|md    everything, as one file
    DELETE /api/mind/everything?confirm=yes   forget this account entirely
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response

from services.memory import journal, mind, profile as profile_mod
from services.memory.learn import is_protected
from src.auth_helpers import get_current_user
from src.task_endpoint import resolve_task_endpoint

logger = logging.getLogger(__name__)

CATEGORIES = ("fact", "identity", "preference", "interest", "contact", "project", "goal", "task", "event")
PROFILE_SECTIONS = ("identity", "people", "preferences", "projects", "goals", "patterns", "interests")


def _by_user(handler):
    """Whatever this route changes is the signed-in person acting on their
    own memory, and the journal says so."""
    if inspect.iscoroutinefunction(handler):
        @functools.wraps(handler)
        async def wrapped(*args, **kwargs):
            with journal.cause("you"):
                return await handler(*args, **kwargs)
    else:
        @functools.wraps(handler)
        def wrapped(*args, **kwargs):
            with journal.cause("you"):
                return handler(*args, **kwargs)
    return wrapped


def setup_mind_routes(memory_manager, session_manager=None, memory_vector=None, chat_processor=None) -> APIRouter:
    router = APIRouter(prefix="/api/mind", tags=["mind"])

    def _owner(request: Request) -> Optional[str]:
        return get_current_user(request)

    def _mine(owner: Optional[str]) -> List[dict]:
        return memory_manager.load(owner=owner)

    def _vector(action: str, memory_id: str, text: Optional[str] = None) -> None:
        if memory_vector is None or not getattr(memory_vector, "healthy", False):
            return
        try:
            if action in ("remove", "update"):
                memory_vector.remove(memory_id)
            if action in ("add", "update") and text:
                memory_vector.add(memory_id, text)
        except Exception:
            logger.warning("memory vector sync failed for %s", memory_id, exc_info=True)

    def _session_name(session_id: Optional[str], owner: Optional[str], cache: dict) -> Optional[dict]:
        if not session_id:
            return None
        if session_id in cache:
            return cache[session_id]
        origin = {"session_id": session_id, "name": None, "exists": False}
        try:
            session = session_manager.get_session(session_id) if session_manager is not None else None
            if session is not None and (owner is None or getattr(session, "owner", None) == owner):
                origin.update(name=getattr(session, "name", None) or "Chat", exists=True)
        except Exception:
            pass
        if not origin["exists"]:
            episode = mind.get_episode(memory_manager, owner, session_id)
            if episode:
                origin["name"] = episode.get("title")
            elif not _looks_like_id(session_id):
                origin["name"] = session_id            # older entries stored the chat's name here
        cache[session_id] = origin
        return origin

    def _looks_like_id(value: str) -> bool:
        return len(value) >= 16 and value.replace("-", "").isalnum() and " " not in value

    def _memory_view(memory: dict, owner: Optional[str], cache: dict) -> dict:
        view = {
            "id": memory.get("id"), "text": memory.get("text"), "category": memory.get("category") or "fact",
            "source": memory.get("source") or "unknown", "pinned": bool(memory.get("pinned")),
            "learned": memory.get("timestamp"), "updated": memory.get("updated"),
            "confirmed": memory.get("confirmed"), "mentions": int(memory.get("mentions") or 1),
            "uses": int(memory.get("uses") or 0), "quote": memory.get("quote") or "",
            "sensitive": memory.get("sensitive"), "protected": is_protected(memory),
            "origin": _session_name(memory.get("session_id"), owner, cache),
        }
        return view

    def _switches(owner: Optional[str]) -> dict:
        from routes.prefs_routes import _load_for_user
        prefs = _load_for_user(owner) or {}
        return {"using": bool(prefs.get("memory_enabled", True)), "learning": bool(prefs.get("auto_memory", True))}

    def _task_model(owner: Optional[str]):
        return resolve_task_endpoint(None, None, None, owner=owner)

    # ── overview ─────────────────────────────────────────────────────────

    @router.get("/overview")
    def overview(request: Request):
        owner = _owner(request)
        memories = _mine(owner)
        current = profile_mod.get_profile(owner, memory_manager, memories=memories)
        by_category: Dict[str, int] = {}
        by_source: Dict[str, int] = {}
        for memory in memories:
            by_category[memory.get("category") or "fact"] = by_category.get(memory.get("category") or "fact", 0) + 1
            by_source[memory.get("source") or "unknown"] = by_source.get(memory.get("source") or "unknown", 0) + 1
        latest = journal.read(memory_manager, owner, limit=1)
        counts = mind.counts(memory_manager, owner)
        counts.update(memories=len(memories), pinned=sum(1 for m in memories if m.get("pinned")),
                      by_category=by_category, by_source=by_source)
        return {
            "account": owner or "",
            "profile": profile_mod.public_view(current),
            "markdown": profile_mod.to_markdown(current),
            "counts": counts,
            "switches": _switches(owner),
            "rules": mind.get_rules(memory_manager, owner),
            "sensitive_kinds": [{"id": k, "label": mind.SENSITIVE_LABELS[k]} for k in mind.SENSITIVE_KINDS],
            "categories": list(CATEGORIES),
            "topic_kinds": list(mind.TOPIC_KINDS),
            "last_change": latest[0].get("ts") if latest else None,
        }

    @router.post("/preview")
    @_by_user
    def preview(request: Request, body: dict = Body(default={})):
        """What the model would be told about the caller for this message —
        the real thing, built by the same code a chat turn uses."""
        owner = _owner(request)
        message = str(body.get("message") or "").strip()[:4000]
        if chat_processor is None:
            raise HTTPException(503, "Preview is not available")
        switches = _switches(owner)
        if not switches["using"]:
            return {"blocks": [], "used": [], "chars": 0, "note": "Memory is switched off, so nothing is given."}
        session = SimpleNamespace(id=None, history=[], owner=owner)
        messages, used = chat_processor.memory_messages(
            message or "hello", session, owner=owner, use_memory=bool(message), use_profile=True,
            incognito=False, count_uses=False,
        )
        from src.prompt_security import GUARD_CLOSE, GUARD_OPEN

        blocks = []
        for item in messages:
            label = (item.get("metadata") or {}).get("source") or "memory"
            text = str(item.get("content") or "")
            # Each block reaches the model wrapped as untrusted reference
            # data; show the person the content, not the wrapper.
            if GUARD_OPEN in text:
                text = text.split(GUARD_OPEN, 1)[1].split(GUARD_CLOSE, 1)[0]
            lines = text.strip().splitlines()
            if lines and lines[0].startswith("Source:"):
                lines = lines[1:]
            blocks.append({"label": label, "text": "\n".join(lines).strip()})
        chars = sum(len(b["text"]) for b in blocks)
        return {"blocks": blocks, "used": used, "chars": chars, "tokens_estimate": chars // 4}

    # ── single facts ─────────────────────────────────────────────────────

    @router.get("/memories")
    def list_memories(request: Request):
        owner = _owner(request)
        cache: dict = {}
        items = [_memory_view(m, owner, cache) for m in _mine(owner)]
        items.sort(key=lambda m: -(m.get("updated") or m.get("learned") or 0))
        return {"memories": items}

    @router.post("/memories")
    @_by_user
    def add_memory(request: Request, body: dict = Body(...)):
        owner = _owner(request)
        text = " ".join(str(body.get("text") or "").split())[:5000]
        if not text:
            raise HTTPException(400, "Nothing to remember")
        category = body.get("category") if body.get("category") in CATEGORIES else "fact"
        mine = _mine(owner)
        if memory_manager.find_duplicates(text, mine):
            return {"ok": True, "duplicate": True}
        entry = memory_manager.add_entry(text, source="user", category=category, owner=owner)
        if body.get("pinned"):
            entry["pinned"] = True
            entry["pinned_by"] = "user"
        memory_manager.save(memory_manager.load_all() + [entry])
        _vector("add", entry["id"], text)
        return {"ok": True, "memory": _memory_view(entry, owner, {})}

    def _find(owner: Optional[str], memory_id: str):
        everything = memory_manager.load_all()
        for memory in everything:
            if memory.get("id") == memory_id and (owner is None or memory.get("owner") == owner):
                return everything, memory
        raise HTTPException(404, "Memory not found")

    @router.put("/memories/{memory_id}")
    @_by_user
    def edit_memory(request: Request, memory_id: str, body: dict = Body(...)):
        owner = _owner(request)
        everything, memory = _find(owner, memory_id)
        changed_text = False
        if isinstance(body.get("text"), str):
            text = " ".join(body["text"].split())[:5000]
            if not text:
                raise HTTPException(400, "A memory cannot be empty — delete it instead")
            if text != memory.get("text"):
                memory["text"] = text
                memory["updated"] = int(time.time())
                memory.pop("quote", None)            # the evidence was for the old wording
                memory["edited_by"] = "user"
                changed_text = True
        if body.get("category") in CATEGORIES:
            memory["category"] = body["category"]
        if isinstance(body.get("pinned"), bool):
            if body["pinned"]:
                memory["pinned"] = True
                memory["pinned_by"] = "user"
            else:
                memory.pop("pinned", None)
                memory.pop("pinned_by", None)
        memory_manager.save(everything)
        if changed_text:
            _vector("update", memory_id, memory["text"])
        return {"ok": True, "memory": _memory_view(memory, owner, {})}

    @router.delete("/memories/{memory_id}")
    @_by_user
    def delete_memory(request: Request, memory_id: str):
        owner = _owner(request)
        everything, memory = _find(owner, memory_id)
        memory_manager.save([m for m in everything if m is not memory])
        _vector("remove", memory_id)
        return {"ok": True}

    def _related(owner: Optional[str], text: str) -> List[dict]:
        wanted = set(mind.tokens(text))
        if not wanted:
            return []
        out = []
        for memory in _mine(owner):
            have = set(mind.tokens(memory.get("text", "")))
            if not have:
                continue
            shared = len(wanted & have)
            # Most of the shorter statement's content words appear in the other.
            if shared >= 2 and shared / min(len(wanted), len(have)) >= 0.6 or mind.similar(text, memory.get("text", ""), 0.6):
                out.append(memory)
        return out

    @router.get("/related")
    def related(request: Request, text: str = ""):
        owner = _owner(request)
        cache: dict = {}
        return {"memories": [_memory_view(m, owner, cache) for m in _related(owner, text)]}

    # ── the profile ──────────────────────────────────────────────────────

    @router.post("/profile/line")
    @_by_user
    def profile_line(request: Request, body: dict = Body(...)):
        """Add, edit or remove one line of the caller's profile. An edit or a
        removal sticks: later rewrites keep the person's line and never bring
        the removed one back. ``forget`` lists facts to delete with it."""
        owner = _owner(request)
        action = body.get("action")
        section = body.get("section")
        text = " ".join(str(body.get("text") or "").split())
        if section not in PROFILE_SECTIONS or action not in ("add", "edit", "remove") or not text:
            raise HTTPException(400, "Need action (add, edit, remove), section and text")
        if action == "add":
            mind.lock_profile_line(memory_manager, owner, section, text)
        elif action == "edit":
            new_text = " ".join(str(body.get("new_text") or "").split())
            if not new_text:
                raise HTTPException(400, "new_text is required")
            mind.lock_profile_line(memory_manager, owner, section, new_text, replaces=text)
        else:
            mind.suppress_profile_line(memory_manager, owner, section, text)
        forgotten = 0
        wanted = {str(i) for i in body.get("forget") or []}
        if wanted:
            everything = memory_manager.load_all()
            keep = [m for m in everything
                    if not (m.get("id") in wanted and (owner is None or m.get("owner") == owner))]
            forgotten = len(everything) - len(keep)
            if forgotten:
                memory_manager.save(keep)
                for memory_id in wanted:
                    _vector("remove", memory_id)
        current = profile_mod.get_profile(owner, memory_manager)
        return {"ok": True, "forgotten": forgotten, "profile": profile_mod.public_view(current),
                "markdown": profile_mod.to_markdown(current)}

    @router.post("/profile/rewrite")
    @_by_user
    async def profile_rewrite(request: Request):
        owner = _owner(request)
        url, model, headers = _task_model(owner)
        current = await profile_mod.rebuild_profile(owner, memory_manager, url or "", model or "", headers)
        return {"ok": True, "written_by_model": current.get("source") == "model",
                "profile": profile_mod.public_view(current), "markdown": profile_mod.to_markdown(current)}

    # ── topics ───────────────────────────────────────────────────────────

    @router.get("/topics")
    def topics(request: Request):
        return {"topics": mind.list_topics(memory_manager, _owner(request))}

    def _topic_or_404(owner: Optional[str], topic_id: str, cache: Optional[dict] = None) -> dict:
        topic = mind.get_topic(memory_manager, owner, topic_id)
        if topic is None:
            raise HTTPException(404, "Topic not found")
        cache = {} if cache is None else cache
        for note in topic.get("notes") or []:
            note["origin"] = _session_name(note.get("session_id"), owner, cache)
        return topic

    @router.get("/topics/{topic_id}")
    def topic(request: Request, topic_id: str):
        return {"topic": _topic_or_404(_owner(request), topic_id)}

    @router.post("/topics")
    @_by_user
    def topic_create(request: Request, body: dict = Body(...)):
        owner = _owner(request)
        name = " ".join(str(body.get("name") or "").split())
        note = " ".join(str(body.get("note") or "").split())
        if len(name) < 2:
            raise HTTPException(400, "Give the topic a name")
        saved, _ = mind.add_notes(memory_manager, owner, name, [note or f"Added {name} as a topic."],
                                  kind=body.get("kind") or "other", by="you")
        if saved is None:
            raise HTTPException(400, "Could not create the topic")
        return {"ok": True, "topic": _topic_or_404(owner, saved["id"])}

    @router.put("/topics/{topic_id}")
    @_by_user
    def topic_edit(request: Request, topic_id: str, body: dict = Body(...)):
        owner = _owner(request)
        try:
            saved = mind.update_topic(memory_manager, owner, topic_id, body or {})
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        if saved is None:
            raise HTTPException(404, "Topic not found")
        profile_mod.invalidate(owner, memory_manager)
        return {"ok": True, "topic": _topic_or_404(owner, topic_id)}

    @router.post("/topics/{topic_id}/notes")
    @_by_user
    def topic_note_add(request: Request, topic_id: str, body: dict = Body(...)):
        owner = _owner(request)
        existing = _topic_or_404(owner, topic_id)
        text = " ".join(str(body.get("text") or "").split())
        if len(text) < 5:
            raise HTTPException(400, "The note is too short")
        mind.add_notes(memory_manager, owner, existing["name"], [text], by="you")
        return {"ok": True, "topic": _topic_or_404(owner, topic_id)}

    @router.delete("/topics/{topic_id}/notes/{note_id}")
    @_by_user
    def topic_note_delete(request: Request, topic_id: str, note_id: str):
        owner = _owner(request)
        if not mind.delete_note(memory_manager, owner, topic_id, note_id):
            raise HTTPException(404, "Note not found")
        profile_mod.invalidate(owner, memory_manager)
        return {"ok": True, "topic": _topic_or_404(owner, topic_id)}

    @router.post("/topics/{topic_id}/rewrite")
    @_by_user
    async def topic_rewrite(request: Request, topic_id: str, body: dict = Body(default={})):
        owner = _owner(request)
        _topic_or_404(owner, topic_id)
        if body.get("unlock"):
            mind.unlock_topic(memory_manager, owner, topic_id)
        url, model, headers = _task_model(owner)
        if not url or not model:
            raise HTTPException(400, "No model is set up to write with")
        await mind.consolidate_topic(memory_manager, owner, topic_id, url, model, headers)
        return {"ok": True, "topic": _topic_or_404(owner, topic_id)}

    @router.post("/topics/{topic_id}/merge")
    @_by_user
    def topic_merge(request: Request, topic_id: str, body: dict = Body(...)):
        """Fold this topic into another one of the caller's (``into``)."""
        owner = _owner(request)
        merged = mind.merge_topics(memory_manager, owner, topic_id, str(body.get("into") or ""))
        if merged is None:
            raise HTTPException(404, "Topic not found")
        profile_mod.invalidate(owner, memory_manager)
        return {"ok": True, "topic": _topic_or_404(owner, merged["id"])}

    @router.delete("/topics/{topic_id}")
    @_by_user
    def topic_delete(request: Request, topic_id: str):
        owner = _owner(request)
        if not mind.delete_topic(memory_manager, owner, topic_id):
            raise HTTPException(404, "Topic not found")
        profile_mod.invalidate(owner, memory_manager)
        return {"ok": True}

    # ── conversations ────────────────────────────────────────────────────

    @router.get("/conversations")
    def conversations(request: Request, limit: int = 200):
        owner = _owner(request)
        cache: dict = {}
        items = []
        for episode in mind.list_episodes(memory_manager, owner, limit=min(max(limit, 1), 600)):
            origin = _session_name(episode.get("session_id"), owner, cache) or {}
            items.append(dict(episode, chat_exists=bool(origin.get("exists"))))
        return {"conversations": items}

    @router.delete("/conversations/{session_id}")
    @_by_user
    def conversation_delete(request: Request, session_id: str):
        owner = _owner(request)
        if not mind.delete_episode(memory_manager, owner, session_id):
            raise HTTPException(404, "Not found")
        profile_mod.invalidate(owner, memory_manager)
        return {"ok": True}

    # ── history ──────────────────────────────────────────────────────────

    @router.get("/history")
    def history(request: Request, limit: int = 100, before: Optional[float] = None, kind: Optional[str] = None,
                by: Optional[str] = None, target: Optional[str] = None):
        owner = _owner(request)
        kinds = [kind] if kind in journal.KINDS else None
        entries = journal.read(memory_manager, owner, limit=min(max(limit, 1), 500), before_ts=before, kinds=kinds,
                               by=by or None, target=target or None)
        cache: dict = {}
        for entry in entries:
            session_id = (entry.get("cause") or {}).get("session_id")
            if session_id:
                entry["origin"] = _session_name(session_id, owner, cache)
            entry["can_undo"] = entry.get("kind") == "memory" and entry.get("op") in ("add", "delete", "edit", "pin", "unpin")
        return {"entries": entries, "total": journal.count(memory_manager, owner),
                "next_before": entries[-1]["ts"] if len(entries) >= min(max(limit, 1), 500) else None}

    @router.post("/history/{entry_id}/undo")
    @_by_user
    def undo(request: Request, entry_id: str):
        owner = _owner(request)
        result = journal.undo_memory_change(memory_manager, owner, entry_id)
        if not result.get("ok"):
            raise HTTPException(409, result.get("error") or "Could not undo that")
        if result["action"] == "removed":
            _vector("remove", result["memory_id"])
        elif result["action"] == "restored":
            _vector("add", result["memory_id"], result.get("text"))
        else:
            _vector("update", result["memory_id"], result.get("text"))
        return result

    # ── rules, export, forget ────────────────────────────────────────────

    @router.put("/rules")
    @_by_user
    def rules(request: Request, body: dict = Body(...)):
        """Kinds of information never to keep. With ``purge`` the facts and
        topics already stored that are marked as one of those kinds go too."""
        owner = _owner(request)
        saved = mind.set_blocked_kinds(memory_manager, owner, body.get("blocked_kinds") or [])
        purged = 0
        if body.get("purge") and saved["blocked_kinds"]:
            blocked = set(saved["blocked_kinds"])
            everything = memory_manager.load_all()
            gone = [m for m in everything
                    if m.get("sensitive") in blocked and (owner is None or m.get("owner") == owner)]
            if gone:
                ids = {m["id"] for m in gone}
                memory_manager.save([m for m in everything if m.get("id") not in ids])
                for memory_id in ids:
                    _vector("remove", memory_id)
                purged = len(ids)
            kinds = {"health": "health", "work": "work", "relationships": "person", "location": "place"}
            for topic in mind.list_topics(memory_manager, owner):
                if topic.get("kind") in {kinds[k] for k in blocked if k in kinds}:
                    if mind.delete_topic(memory_manager, owner, topic["id"]):
                        purged += 1
            if purged:
                profile_mod.invalidate(owner, memory_manager)
        return {"ok": True, "rules": saved, "purged": purged}

    @router.get("/export")
    def export(request: Request, format: str = "json"):
        owner = _owner(request)
        stamp = time.strftime("%Y-%m-%d")
        if format == "md":
            body = mind.export_markdown(memory_manager, owner)
            return Response(body, media_type="text/markdown; charset=utf-8", headers={
                "Content-Disposition": f'attachment; filename="aegis-memory-{stamp}.md"'})
        body = json.dumps(mind.export_all(memory_manager, owner), indent=1, ensure_ascii=False)
        return Response(body, media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="aegis-memory-{stamp}.json"'})

    @router.delete("/everything")
    @_by_user
    def forget_everything(request: Request, confirm: str = ""):
        """Delete every fact, the profile, all topics, all conversation
        digests, the rules and the journal for the caller's account."""
        if confirm != "yes":
            raise HTTPException(400, "Add ?confirm=yes to delete everything")
        owner = _owner(request)
        mine = {m["id"] for m in _mine(owner)}
        if mine:
            memory_manager.save([m for m in memory_manager.load_all() if m.get("id") not in mine])
            for memory_id in mine:
                _vector("remove", memory_id)
        profile_mod.delete_profile(owner, memory_manager)
        mind.wipe(memory_manager, owner)
        return {"ok": True, "memories_removed": len(mine)}

    return router
