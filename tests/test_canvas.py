"""Tests for the code canvas backend — src/canvas.py.

Covers code-block extraction and the generate/edit flows with a mocked model,
so nothing here needs a live LLM.
"""
import asyncio

from src import canvas


# ── code extraction ───────────────────────────────────────────────────────────
def test_extract_code_basic():
    raw = "Here you go:\n```python\nprint('hi')\n```\nThat prints hi."
    out = canvas._extract_code(raw, "python")
    assert out["code"] == "print('hi')"
    assert out["language"] == "python"
    assert out["explanation"] == "That prints hi."


def test_extract_code_prefers_longest_block():
    raw = "```py\nx=1\n```\nand the file:\n```py\ndef f():\n    return 42\n```\ndone"
    out = canvas._extract_code(raw)
    assert "def f()" in out["code"]
    assert out["language"] == "python"  # 'py' aliased


def test_extract_code_no_fence_is_all_code():
    out = canvas._extract_code("just some code\nline2", "python")
    assert out["code"] == "just some code\nline2"


# ── generate / edit (mocked model) ────────────────────────────────────────────
def _mock_model(monkeypatch, response: str):
    async def fake(system, user, model_spec, owner, max_tokens=6000):
        return response
    monkeypatch.setattr(canvas, "_run_model", fake)
    monkeypatch.setattr(canvas, "_pick_model", lambda owner, hint=None: hint or "qwen3-coder-30b")


def test_generate_happy(monkeypatch):
    _mock_model(monkeypatch, "```python\ndef fib(n):\n    return n\n```\nA fib stub.")
    res = asyncio.run(canvas.generate("a fibonacci function", "python", owner="admin"))
    assert res["ok"] and res["model"] == "qwen3-coder-30b"
    assert "def fib" in res["code"] and res["language"] == "python"
    assert res["explanation"] == "A fib stub."


def test_generate_requires_prompt(monkeypatch):
    _mock_model(monkeypatch, "x")
    res = asyncio.run(canvas.generate("   ", "python"))
    assert not res["ok"] and "describe" in res["error"]


def test_generate_no_model(monkeypatch):
    monkeypatch.setattr(canvas, "_pick_model", lambda owner, hint=None: "")
    res = asyncio.run(canvas.generate("something"))
    assert not res["ok"] and "no model" in res["error"].lower()


def test_edit_rewrites_code(monkeypatch):
    _mock_model(monkeypatch, "```python\ndef fib(n: int) -> int:\n    return n\n```\nAdded type hints.")
    res = asyncio.run(canvas.edit("def fib(n):\n    return n", "add type hints", "python", owner="admin"))
    assert res["ok"] and "n: int" in res["code"]
    assert res["explanation"] == "Added type hints."


def test_edit_empty_code_delegates_to_generate(monkeypatch):
    _mock_model(monkeypatch, "```python\nprint('new')\n```\nMade it.")
    res = asyncio.run(canvas.edit("", "print new", "python"))
    assert res["ok"] and "print('new')" in res["code"]


def test_edit_requires_instruction(monkeypatch):
    _mock_model(monkeypatch, "x")
    res = asyncio.run(canvas.edit("code here", "  ", "python"))
    assert not res["ok"] and "change" in res["error"]
