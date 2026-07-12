"""Regression tests for three real-world local-serving bugs found bug-smashing:

1. Deep Research probed local models with a 15s timeout — too short for a
   llama-swap cold-load — so a healthy big model failed the whole run.
2. Every web search hammered a down SearXNG (connect timeout) before falling
   back; a circuit breaker now skips it fast.
3. Aegis reported a llama-swap model's *trained* max context instead of the
   served `-c`, so a long chat could overflow the smaller window and 400.
"""
import asyncio

import httpx
import pytest


# ── 1. research probe timeout is local-aware ─────────────────────────────────
def test_probe_timeout_local_vs_remote(monkeypatch):
    captured = {}

    async def fake_llm(**kwargs):
        captured.update(kwargs)

    import src.llm_core as llm_core
    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm)
    from src.research_handler import ResearchHandler

    asyncio.run(ResearchHandler._probe_endpoint("http://127.0.0.1:9090/v1/chat/completions", "m"))
    assert captured["timeout"] == 180  # local cold-load gets a generous window

    captured.clear()
    asyncio.run(ResearchHandler._probe_endpoint("https://api.openai.com/v1/chat/completions", "m"))
    assert captured["timeout"] == 20   # remote stays snappy


# ── 2. SearXNG circuit breaker ───────────────────────────────────────────────
def test_is_conn_error():
    from services.search import providers as p
    assert p._is_conn_error(httpx.ConnectError("refused"))
    assert p._is_conn_error(httpx.ConnectTimeout("t"))
    assert p._is_conn_error(Exception("[WinError 10061] actively refused"))
    assert not p._is_conn_error(Exception("HTTP 500 server error"))


def test_searxng_skips_when_circuit_open(monkeypatch):
    from services.search import providers as p
    # If the circuit is open, no network call happens and it returns [] instantly.
    called = {"n": 0}
    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("should not hit the network while cooling")
    monkeypatch.setattr(p.httpx, "get", boom)
    p._mark_searxng_down()
    try:
        assert p.searxng_search_api("q", count=3) == []
        assert called["n"] == 0
    finally:
        p._mark_searxng_up()


def test_searxng_conn_error_opens_circuit(monkeypatch):
    from services.search import providers as p
    p._mark_searxng_up()

    def refuse(*a, **k):
        raise httpx.ConnectError("[WinError 10061] actively refused")
    monkeypatch.setattr(p.httpx, "get", refuse)
    # First failure returns [] AND opens the circuit (so the next call is skipped).
    assert p.searxng_search_api("q", count=3) == []
    assert p._searxng_is_cooling() is True
    p._mark_searxng_up()


# ── 3. served context (-c) beats the trained max for llama-swap models ───────
def test_context_uses_configured_c_when_slots_404(monkeypatch):
    import src.model_context as mc

    # llama-swap has no /slots → 404; /v1/models has no context field.
    class _Resp:
        is_success = False
        def json(self): return {}
    monkeypatch.setattr(mc.httpx, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(mc, "is_local_endpoint", lambda url: True)
    monkeypatch.setattr(mc, "_configured_endpoint_kind", lambda url: "local")
    # trained max for this name would be large; the served -c is smaller.
    monkeypatch.setattr(mc, "_lookup_known", lambda model: 131072)

    import src.engine_tuner as et
    monkeypatch.setattr(et, "configured_context", lambda model: 45056)

    ctx, known = mc._query_context_length("http://127.0.0.1:9090/v1/chat/completions", "qwen3-coder-30b")
    assert ctx == 45056 and known is True
