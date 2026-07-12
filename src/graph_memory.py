"""
graph_memory.py — local-first knowledge-graph memory (the cognee pattern, done
locally with no extra services).

Aegis already has vector memory (fuzzy recall of past facts). This adds a
STRUCTURED layer on top: (subject, relation, object) triples extracted from text
by the local model and stored in a small SQLite graph (data/graph_memory.db).
That lets you ask "what do I know about X" and get the connected facts, not just
similar sentences.

Local-first, like tracing/opik: no Docker, no external graph DB. cognee /
supermemory can be layered on later as optional providers; this is the built-in.

Guarded everywhere — a broken extraction or write must never break a chat.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(DATA_DIR, "graph_memory.db")
_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _db() -> Optional[sqlite3.Connection]:
    global _conn
    if _conn is not None:
        return _conn
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        c = sqlite3.connect(_DB_PATH, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""
            CREATE TABLE IF NOT EXISTS triples (
                id TEXT PRIMARY KEY, subject TEXT, relation TEXT, object TEXT,
                source TEXT, owner TEXT, ts REAL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_tri_subj ON triples(subject)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tri_obj ON triples(object)")
        c.commit()
        _conn = c
        return _conn
    except Exception as e:
        logger.debug(f"graph_memory db init failed: {e}")
        return None


def is_enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("graph_memory_enabled", False))
    except Exception:
        return False


# ── write ────────────────────────────────────────────────────────────────────
def add_triples(triples: List[Tuple[str, str, str]], source: str = "", owner: str = "") -> int:
    db = _db()
    if db is None:
        return 0
    added = 0
    try:
        with _lock:
            for t in triples:
                try:
                    s, r, o = (str(t[0]).strip(), str(t[1]).strip(), str(t[2]).strip())
                except Exception:
                    continue
                if not s or not o:
                    continue
                # de-dup on (subject, relation, object) case-insensitively
                exists = db.execute(
                    "SELECT 1 FROM triples WHERE lower(subject)=? AND lower(relation)=? AND lower(object)=? LIMIT 1",
                    (s.lower(), r.lower(), o.lower())).fetchone()
                if exists:
                    continue
                db.execute(
                    "INSERT INTO triples (id,subject,relation,object,source,owner,ts) VALUES (?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex[:12], s[:200], r[:100], o[:200], source[:120], owner or "", time.time()))
                added += 1
            db.commit()
    except Exception as e:
        logger.debug(f"add_triples failed: {e}")
    return added


_JSON_RE = re.compile(r"\[.*\]", re.S)


def _parse_triples(raw: str) -> List[Tuple[str, str, str]]:
    """Parse an LLM response into (s, r, o) triples. Accepts [[s,r,o],...] or
    [{"s":..,"r":..,"o":..},...] and tolerates surrounding prose/fences."""
    m = _JSON_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, list) and len(item) >= 3:
            out.append((item[0], item[1], item[2]))
        elif isinstance(item, dict):
            s = item.get("s") or item.get("subject")
            r = item.get("r") or item.get("relation") or item.get("predicate")
            o = item.get("o") or item.get("object")
            if s and o:
                out.append((s, r or "related to", o))
    return out


async def extract_from_text(text: str, model_spec: str, owner: str = "", source: str = "chat") -> int:
    """Use the local model to pull triples from text and store them. Returns count."""
    text = (text or "").strip()
    if not text or not (model_spec or "").strip():
        return 0
    from src.ai_interaction import _resolve_model
    from src.llm_core import llm_call_async
    import asyncio
    system = ("You extract a knowledge graph. Return ONLY a JSON array of "
              '[subject, relation, object] triples of the clear factual relationships in the text. '
              "Keep entities short and canonical. No prose, just the JSON array.")
    try:
        url, model, headers = await asyncio.to_thread(_resolve_model, model_spec, owner=owner)
        raw = await llm_call_async(url, model, [
            {"role": "system", "content": system},
            {"role": "user", "content": text[:6000]},
        ], headers=headers, timeout=120)
    except Exception as e:
        logger.debug(f"extract_from_text llm failed: {e}")
        return 0
    return add_triples(_parse_triples(raw), source=source, owner=owner)


async def build_from_memories(model_spec: str, owner: str = "", limit: int = 60) -> Dict[str, Any]:
    """Extract triples from the user's existing saved memories."""
    try:
        from src.memory import MemoryManager
        mm = MemoryManager(DATA_DIR)
        mems = mm.load(owner=owner or None) or []
    except Exception as e:
        return {"ok": False, "error": f"could not load memories: {e}"}
    mems = mems[:limit]
    processed = 0
    added = 0
    for m in mems:
        txt = (m.get("text") or "").strip()
        if not txt:
            continue
        added += await extract_from_text(txt, model_spec, owner=owner, source="memory")
        processed += 1
    return {"ok": True, "processed": processed, "triples_added": added, "total": stats().get("total", 0)}


# ── read ─────────────────────────────────────────────────────────────────────
def query(term: str, limit: int = 50, owner: str = "") -> List[Dict]:
    db = _db()
    if db is None or not (term or "").strip():
        return []
    try:
        like = f"%{term.strip().lower()}%"
        rows = db.execute(
            "SELECT subject,relation,object,source FROM triples "
            "WHERE lower(subject) LIKE ? OR lower(object) LIKE ? ORDER BY ts DESC LIMIT ?",
            (like, like, limit)).fetchall()
        return [{"subject": s, "relation": r, "object": o, "source": src} for (s, r, o, src) in rows]
    except Exception:
        return []


def stats() -> Dict[str, Any]:
    db = _db()
    if db is None:
        return {"total": 0, "entities": []}
    try:
        total = db.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        rows = db.execute(
            "SELECT e, COUNT(*) c FROM ("
            "  SELECT subject e FROM triples UNION ALL SELECT object e FROM triples"
            ") GROUP BY lower(e) ORDER BY c DESC LIMIT 12").fetchall()
        return {"total": total, "entities": [{"name": e, "count": c} for (e, c) in rows]}
    except Exception:
        return {"total": 0, "entities": []}


def clear() -> int:
    db = _db()
    if db is None:
        return 0
    try:
        with _lock:
            n = db.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
            db.execute("DELETE FROM triples")
            db.commit()
        return n
    except Exception:
        return 0
