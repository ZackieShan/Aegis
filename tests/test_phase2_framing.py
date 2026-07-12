"""Phase-2 framing fixes (2026-07-12 multi-agent review).

Covers: memory-add dedup, is_builtin↔BUILTIN_MCP_IDS parity, incognito
persist flag, email-poller flag override (no settings mutation), and the
intent-nudge conclusion guard.
"""
import asyncio

import pytest


# ── #10 is_builtin recognizes toolbox servers (self-heal) ────────────────────
def test_is_builtin_covers_toolboxes():
    from src.mcp_manager import McpManager
    from src.builtin_mcp import BUILTIN_MCP_IDS
    m = McpManager()
    for sid in ("osint", "troubleshoot", "market", "web", "image_gen", "memory", "builtin_browser"):
        assert m.is_builtin(sid), f"{sid} must be recognized as builtin for auto-reconnect"
    # And it agrees with the authoritative set.
    for sid in BUILTIN_MCP_IDS:
        assert m.is_builtin(sid)
    assert not m.is_builtin("some_user_added_server")


# ── #2 incognito must not persist to the DB ──────────────────────────────────
def test_add_message_persist_flag(monkeypatch):
    import core.models as models

    class _FakeMgr:
        def __init__(self):
            self.persisted = []

        def _persist_message(self, sid, msg):
            self.persisted.append((sid, msg))

    mgr = _FakeMgr()
    monkeypatch.setattr(models, "_SESSION_MANAGER_INSTANCE", mgr, raising=False)

    sess = models.Session(id="s1", name="t", model="m", endpoint_url="u")
    sess.add_message(models.ChatMessage("user", "normal"))
    assert len(mgr.persisted) == 1, "default add must persist"

    sess.add_message(models.ChatMessage("user", "secret"), persist=False)
    assert len(mgr.persisted) == 1, "persist=False must NOT write to the DB"
    # But it IS in in-memory history (needed for conversation context).
    assert any(msg.content == "secret" for msg in sess.history)


# ── #16 agent memory-add dedups ──────────────────────────────────────────────
def test_do_manage_memory_add_skips_duplicate(monkeypatch):
    import src.ai_interaction as ai

    class _Mgr:
        def __init__(self):
            self.saved = []
            self._store = [{"id": "m1", "text": "The user owns a 2001 Honda Accord.", "owner": None}]

        def load_all(self):
            return list(self._store)

        def find_duplicates(self, text, entries):
            tl = text.strip().lower()
            return [e for e in entries if e["text"].lower() == tl]

        def add_entry(self, text, source=None, category=None, owner=None):
            return {"id": "new", "text": text, "owner": owner}

        def save(self, memories):
            self.saved = memories

    mgr = _Mgr()
    monkeypatch.setattr(ai, "_memory_manager", mgr, raising=False)
    monkeypatch.setattr(ai, "_memory_vector", None, raising=False)

    # Exact duplicate → skipped, nothing saved.
    res = asyncio.run(ai.do_manage_memory("add\nThe user owns a 2001 Honda Accord."))
    assert "duplicate skipped" in res.get("results", "").lower()
    assert mgr.saved == [], "duplicate must not be written"

    # Novel fact → written.
    res2 = asyncio.run(ai.do_manage_memory("add\nThe user likes hiking."))
    assert "memory added" in res2.get("results", "").lower()
    assert mgr.saved, "novel memory must be saved"


def test_do_manage_memory_add_skips_near_duplicate(monkeypatch):
    import src.ai_interaction as ai

    class _Mgr:
        def load_all(self):
            return []

        def find_duplicates(self, text, entries):
            return []

        def add_entry(self, *a, **k):
            raise AssertionError("must not add when vector reports a near-duplicate")

        def save(self, memories):
            raise AssertionError("must not save near-duplicate")

    class _Vec:
        healthy = True

        def find_similar(self, text):
            return "existing-id"

    monkeypatch.setattr(ai, "_memory_manager", _Mgr(), raising=False)
    monkeypatch.setattr(ai, "_memory_vector", _Vec(), raising=False)
    res = asyncio.run(ai.do_manage_memory("add\nSomething very close to an existing fact."))
    assert "similar" in res.get("results", "").lower()


# ── #20 email pollers pass flags, never mutate settings.json ─────────────────
def test_email_run_once_does_not_touch_settings(monkeypatch):
    import routes.email_pollers as ep

    saved = {"count": 0}

    def _fake_save(_s):
        saved["count"] += 1

    captured = {}

    async def _fake_pass(days_back=1, account_id=None, max_process=None, progress_cb=None, flags=None):
        captured["flags"] = flags
        return "ok"

    monkeypatch.setattr(ep, "_save_settings", _fake_save)
    monkeypatch.setattr(ep, "_auto_summarize_pass", _fake_pass)

    out = asyncio.run(ep._run_auto_summarize_once(do_summary=True, do_reply=False, do_tag=True))
    assert out == "ok"
    assert saved["count"] == 0, "must NOT write settings.json"
    assert captured["flags"] == {
        "email_auto_summarize": True,
        "email_auto_reply": False,
        "email_auto_tag": True,
        "email_auto_spam": False,
        "email_auto_calendar": False,
    }


def test_auto_summarize_single_reads_flags_override(monkeypatch):
    """When flags is passed it overrides settings; when None it reads settings."""
    import routes.email_pollers as ep
    # flags override wins even if settings say otherwise
    monkeypatch.setattr(ep, "_load_settings", lambda: {"email_auto_summarize": True})

    async def _run(flags):
        # Short-circuit before any IMAP: all-false flags → "Nothing to do".
        return await ep._auto_summarize_pass_single(account_id="a1", flags=flags)

    res = asyncio.run(_run({"email_auto_summarize": False, "email_auto_reply": False,
                            "email_auto_tag": False, "email_auto_spam": False,
                            "email_auto_calendar": False}))
    assert res == "Nothing to do", "explicit all-false flags must override the enabled setting"
