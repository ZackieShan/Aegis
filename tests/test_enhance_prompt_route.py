"""POST /api/image/enhance-prompt — rough intent → diffusion-ready scene.

The route exists because instruction-style prompts fail visually (a "movie
trailer" ask literally renders a TV screen). What matters here: auth gating,
graceful degradation when no utility model is configured, and that the model's
output is passed through cleaned rather than parsed.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import image_gen_routes


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(image_gen_routes, "require_privilege", lambda request, priv: "alice")
    app = FastAPI()
    app.include_router(image_gen_routes.setup_image_gen_routes())
    return TestClient(app)


def _fake_resolver(url="http://127.0.0.1:9090/v1", model="qwen3.5-4b"):
    def resolve(prefix, **kw):
        return (url, model, {}) if url else (None, None, None)
    return resolve


def test_rejects_empty_prompt(client):
    assert client.post("/api/image/enhance-prompt", json={"prompt": ""}).status_code == 400


def test_no_model_configured_is_inband_error_not_500(client, monkeypatch):
    import src.endpoint_resolver as er
    monkeypatch.setattr(er, "resolve_endpoint", _fake_resolver(url=None))
    d = client.post("/api/image/enhance-prompt", json={"prompt": "a fox"}).json()
    assert d["ok"] is False
    assert "utility model" in d["error"].lower()


def test_returns_cleaned_model_output(client, monkeypatch):
    import src.endpoint_resolver as er
    import src.llm_core as llm
    monkeypatch.setattr(er, "resolve_endpoint", _fake_resolver())

    async def fake_llm(url, model, messages, **kw):
        # The system prompt must warn about instruction words — that's the
        # entire reason this endpoint exists.
        assert "instruction words" in messages[0]["content"]
        return '  "A lone rider crossing red-rock desert at dusk."  '

    monkeypatch.setattr(llm, "llm_call_async", fake_llm)
    d = client.post("/api/image/enhance-prompt",
                    json={"prompt": "ghibli john wayne trailer"}).json()
    assert d["ok"] is True
    assert d["prompt"] == "A lone rider crossing red-rock desert at dusk."


def test_video_kind_asks_for_motion(client, monkeypatch):
    import src.endpoint_resolver as er
    import src.llm_core as llm
    monkeypatch.setattr(er, "resolve_endpoint", _fake_resolver())
    seen = {}

    async def fake_llm(url, model, messages, **kw):
        seen["system"] = messages[0]["content"]
        return "ok"

    monkeypatch.setattr(llm, "llm_call_async", fake_llm)
    client.post("/api/image/enhance-prompt", json={"prompt": "x", "kind": "video"})
    assert "motion" in seen["system"].lower()
    client.post("/api/image/enhance-prompt", json={"prompt": "x", "kind": "image"})
    assert "motion" not in seen["system"].lower()


def test_llm_failure_is_inband_error(client, monkeypatch):
    import src.endpoint_resolver as er
    import src.llm_core as llm
    monkeypatch.setattr(er, "resolve_endpoint", _fake_resolver())

    async def boom(*a, **kw):
        raise RuntimeError("model fell over")

    monkeypatch.setattr(llm, "llm_call_async", boom)
    d = client.post("/api/image/enhance-prompt", json={"prompt": "a fox"}).json()
    assert d["ok"] is False
    assert "model fell over" in d["error"]
