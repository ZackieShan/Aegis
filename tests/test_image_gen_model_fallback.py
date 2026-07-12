"""Image generation must survive a broken image_model setting, and the two
former forks (do_generate_image + the MCP image server) must stay consolidated.

Regression for 2026-07-12: the admin image_model setting pointed at
'stable-diffusion-3.5-medium' (a registry model never actually served), so
every generate_image call hard-failed with "Model ... not found". The single
implementation (src.ai_interaction.do_generate_image) now falls back to the
autodetect chain (qwen-image first) and names the broken model; the MCP server
(mcp_servers/image_gen_server) delegates to it so the two can't drift.
"""
import asyncio
import base64

import mcp_servers.image_gen_server as igs
import src.ai_interaction as ai


class _FakeResp:
    status_code = 200

    def json(self):
        return {"data": [{"b64_json": base64.b64encode(b"fake-png").decode()}]}


class _FakeClient:
    last_payload = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.last_payload = json
        return _FakeResp()


def _patch(monkeypatch, tmp_path, image_model):
    import httpx
    import src.settings as st
    monkeypatch.setattr(st, "load_settings", lambda: {"image_model": image_model})
    monkeypatch.setattr(st, "get_setting",
                        lambda k, d=None: True if k == "image_gen_enabled" else d)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    # do_generate_image writes the decoded image + gallery row; point the
    # image dir at tmp and no-op the gallery DB insert.
    monkeypatch.setattr(ai, "GENERATED_IMAGES_DIR", str(tmp_path), raising=False)
    _FakeClient.last_payload = None


def test_do_generate_image_falls_back_on_broken_model(monkeypatch, tmp_path):
    def _resolve(spec, owner=None):
        if spec == "qwen-image":
            return ("http://127.0.0.1:9090/v1/chat/completions", "qwen-image", {})
        raise ValueError(f"Model '{spec}' not found on any configured endpoint")

    monkeypatch.setattr(ai, "_resolve_model", _resolve)
    _patch(monkeypatch, tmp_path, "stable-diffusion-3.5-medium")

    res = asyncio.run(ai.do_generate_image("a mountain lake"))
    assert not res.get("error"), res
    assert res["image_model"] == "qwen-image"
    assert _FakeClient.last_payload["model"] == "qwen-image"
    # local-diffusion size cap kicked in (default 1024 → 768).
    assert res["image_size"] == "768x768"
    # The fallback note names the broken configured model.
    assert "stable-diffusion-3.5-medium" in res["results"]


def test_do_generate_image_nothing_available(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_resolve_model", lambda spec, owner=None: (_ for _ in ()).throw(ValueError("nope")))
    _patch(monkeypatch, tmp_path, "stable-diffusion-3.5-medium")
    res = asyncio.run(ai.do_generate_image("a mountain lake"))
    assert res.get("error")
    assert "no fallback image" in res["error"].lower()


def test_mcp_server_delegates_and_surfaces_note(monkeypatch, tmp_path):
    """The MCP image server must delegate to do_generate_image (not its own
    fork) and surface the fallback note."""
    def _resolve(spec, owner=None):
        if spec == "qwen-image":
            return ("http://127.0.0.1:9090/v1/chat/completions", "qwen-image", {})
        raise ValueError("nope")

    monkeypatch.setattr(ai, "_resolve_model", _resolve)
    _patch(monkeypatch, tmp_path, "stable-diffusion-3.5-medium")

    out = asyncio.run(igs.call_tool("generate_image", {"prompt": "a mountain lake"}))
    text = out[0].text
    assert "Direct link:" in text
    assert "model: qwen-image" in text
    assert "stable-diffusion-3.5-medium" in text  # note surfaced through delegation
    assert _FakeClient.last_payload["model"] == "qwen-image"


def test_mcp_server_disabled_gate(monkeypatch):
    import src.settings as st
    monkeypatch.setattr(st, "get_setting", lambda k, d=None: False if k == "image_gen_enabled" else d)
    out = asyncio.run(igs.call_tool("generate_image", {"prompt": "x"}))
    assert "disabled" in out[0].text.lower()
