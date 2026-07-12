"""
tracing.py — local-first LLM/agent observability.

Every model call and agent turn is recorded to a small self-contained SQLite
store (data/traces.db) — no Docker, no external service, your data stays local.
This gives immediate "what did the agent actually do" visibility: model,
endpoint, latency, tokens, tool calls, per session.

opik (the richer eval/trace UI) is supported as an OPTIONAL export sink: if the
`opik` package is installed and `opik_export` is enabled in settings, each trace
is also forwarded there. It is never required and never on the critical path.

Design rules:
  - Opt-out, not opt-in for the local store (cheap; the whole point is to see
    calls) — but `tracing_enabled=false` in settings turns it off entirely.
  - record() is fire-and-forget and MUST NEVER raise into a caller. A broken
    trace must never break a chat.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(DATA_DIR, "traces.db")
_MAX_ROWS = 5000          # ring-buffer cap; oldest pruned beyond this
_PREVIEW_CHARS = 2000
_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_opik_disabled = False    # flips true after the first opik failure


def _db() -> Optional[sqlite3.Connection]:
    global _conn
    if _conn is not None:
        return _conn
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        c = sqlite3.connect(_DB_PATH, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""
            CREATE TABLE IF NOT EXISTS traces (
                id TEXT PRIMARY KEY,
                ts REAL,
                kind TEXT,
                session_id TEXT,
                model TEXT,
                endpoint TEXT,
                latency_ms INTEGER,
                first_token_ms INTEGER,
                input_tokens INTEGER,
                output_tokens INTEGER,
                tool_calls TEXT,
                gen_tps REAL,
                workload TEXT,
                error TEXT,
                prompt_preview TEXT,
                response_preview TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_traces_ts ON traces(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id)")
        c.commit()
        _conn = c
        return _conn
    except Exception as e:
        logger.debug(f"tracing: db init failed: {e}")
        return None


def is_enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("tracing_enabled", True))
    except Exception:
        return True


def _preview(x: Any) -> str:
    try:
        if isinstance(x, (list, dict)):
            x = json.dumps(x, ensure_ascii=False)
        s = str(x or "")
        return s[:_PREVIEW_CHARS]
    except Exception:
        return ""


def record(
    kind: str,
    model: str = "",
    endpoint: str = "",
    session_id: str = "",
    latency_ms: Optional[int] = None,
    first_token_ms: Optional[int] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    tool_calls: Optional[List[str]] = None,
    gen_tps: Optional[float] = None,
    workload: str = "",
    error: str = "",
    prompt: Any = None,
    response: Any = None,
) -> None:
    """Record one trace. Fire-and-forget; never raises into the caller."""
    if not is_enabled():
        return
    try:
        db = _db()
        if db is None:
            return
        tid = uuid.uuid4().hex[:12]
        row = (
            tid, time.time(), kind or "call", session_id or "", model or "",
            endpoint or "", latency_ms, first_token_ms, input_tokens, output_tokens,
            json.dumps(tool_calls or []), gen_tps, workload or "", (error or "")[:500],
            _preview(prompt), _preview(response),
        )
        with _lock:
            db.execute(
                "INSERT INTO traces (id,ts,kind,session_id,model,endpoint,latency_ms,"
                "first_token_ms,input_tokens,output_tokens,tool_calls,gen_tps,workload,"
                "error,prompt_preview,response_preview) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            db.commit()
            # Prune to the ring-buffer cap occasionally (cheap, only when over).
            n = db.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
            if n > _MAX_ROWS:
                db.execute(
                    "DELETE FROM traces WHERE id IN "
                    "(SELECT id FROM traces ORDER BY ts ASC LIMIT ?)",
                    (n - _MAX_ROWS,),
                )
                db.commit()
        _maybe_opik(kind, model, session_id, input_tokens, output_tokens,
                    tool_calls, latency_ms, error, prompt, response)
    except Exception as e:
        logger.debug(f"tracing.record failed: {e}")


def _maybe_opik(kind, model, session_id, itok, otok, tools, latency_ms, error, prompt, response):
    """Optional export to opik if installed + enabled. Fully guarded."""
    global _opik_disabled
    if _opik_disabled:
        return
    try:
        from src.settings import get_setting
        if not get_setting("opik_export", False):
            return
        import opik  # noqa
        client = opik.Opik()
        client.trace(
            name=f"aegis:{kind}",
            input={"messages_preview": _preview(prompt)},
            output={"response_preview": _preview(response)},
            metadata={"model": model, "session_id": session_id,
                      "input_tokens": itok, "output_tokens": otok,
                      "tools": tools or [], "latency_ms": latency_ms, "error": error},
        )
    except Exception as e:
        # Disable after first failure so we don't spam or slow calls down.
        _opik_disabled = True
        logger.info(f"opik export disabled (not reachable/configured): {e}")


# ── read API (for /api/traces) ───────────────────────────────────────────────
_COLS = ["id", "ts", "kind", "session_id", "model", "endpoint", "latency_ms",
         "first_token_ms", "input_tokens", "output_tokens", "tool_calls",
         "gen_tps", "workload", "error", "prompt_preview", "response_preview"]


def list_traces(limit: int = 100, session_id: Optional[str] = None) -> List[Dict]:
    db = _db()
    if db is None:
        return []
    try:
        cols = "id,ts,kind,session_id,model,endpoint,latency_ms,first_token_ms,input_tokens,output_tokens,tool_calls,gen_tps,workload,error"
        if session_id:
            rows = db.execute(f"SELECT {cols} FROM traces WHERE session_id=? ORDER BY ts DESC LIMIT ?",
                              (session_id, limit)).fetchall()
        else:
            rows = db.execute(f"SELECT {cols} FROM traces ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        keys = cols.split(",")
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            try:
                d["tool_calls"] = json.loads(d.get("tool_calls") or "[]")
            except Exception:
                d["tool_calls"] = []
            out.append(d)
        return out
    except Exception as e:
        logger.debug(f"list_traces failed: {e}")
        return []


def get_trace(trace_id: str) -> Optional[Dict]:
    db = _db()
    if db is None:
        return None
    try:
        r = db.execute(f"SELECT {','.join(_COLS)} FROM traces WHERE id=?", (trace_id,)).fetchone()
        if not r:
            return None
        d = dict(zip(_COLS, r))
        try:
            d["tool_calls"] = json.loads(d.get("tool_calls") or "[]")
        except Exception:
            d["tool_calls"] = []
        return d
    except Exception:
        return None


def stats() -> Dict[str, Any]:
    db = _db()
    if db is None:
        return {"total": 0}
    try:
        total = db.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        by_model = db.execute(
            "SELECT model, COUNT(*), AVG(latency_ms), SUM(output_tokens) "
            "FROM traces GROUP BY model ORDER BY COUNT(*) DESC LIMIT 10"
        ).fetchall()
        return {
            "total": total,
            "by_model": [
                {"model": m or "(unknown)", "calls": n,
                 "avg_latency_ms": round(a) if a else None, "output_tokens": t or 0}
                for (m, n, a, t) in by_model
            ],
        }
    except Exception:
        return {"total": 0}
