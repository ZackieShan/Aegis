"""Unit tests for the local coding agent (Aider) wrapper — src/coding_agent.py.

Covers the pure logic (engine discovery, endpoint ranking, workspace safety
fence, output parsing) and a fully-mocked run so nothing here needs Aider, a
network, or a real model.
"""
import asyncio
import os

import pytest

from src import coding_agent as ca


# ── engine discovery ──────────────────────────────────────────────────────────
def test_aider_python_honors_env(monkeypatch, tmp_path):
    py = tmp_path / "python.exe"
    py.write_text("")
    monkeypatch.setenv("AIDER_PYTHON", str(py))
    assert ca._aider_python() == str(py)
    assert ca.is_available() is True


def test_aider_python_missing_env_and_venv(monkeypatch, tmp_path):
    monkeypatch.delenv("AIDER_PYTHON", raising=False)
    monkeypatch.setenv("AEGIS_ENGINE_DIR", str(tmp_path))  # empty → no venv
    assert ca._aider_python() is None
    assert ca.is_available() is False


# ── endpoint / model ranking ──────────────────────────────────────────────────
def test_is_local():
    assert ca._is_local("http://127.0.0.1:9090/v1")
    assert ca._is_local("http://localhost:1234/v1")
    assert not ca._is_local("https://api.openai.com/v1")


def test_aider_base_appends_v1_only_when_pathless():
    # Ollama bare host → needs /v1 for the OpenAI-compat API
    assert ca._aider_base("http://127.0.0.1:11434") == "http://127.0.0.1:11434/v1"
    assert ca._aider_base("http://127.0.0.1:11434/") == "http://127.0.0.1:11434/v1"
    # already has /v1 (llama-swap) or a custom path → left alone
    assert ca._aider_base("http://127.0.0.1:9090/v1") == "http://127.0.0.1:9090/v1"
    assert ca._aider_base("https://api.openai.com/v1") == "https://api.openai.com/v1"


def test_rank_prefers_local_then_coder():
    local_coder = {"model": "qwen3-coder-30b", "local": True, "supports_tools": True}
    local_plain = {"model": "llama3", "local": True, "supports_tools": True}
    remote_coder = {"model": "qwen3-coder-30b", "local": False, "supports_tools": True}
    ranked = sorted([remote_coder, local_plain, local_coder], key=ca._rank)
    assert ranked[0] is local_coder          # local + coder wins
    assert ranked[1] is local_plain          # local beats remote
    assert ranked[2] is remote_coder


def test_pick_endpoint_default_and_hint(monkeypatch):
    cands = [
        {"model": "llama3", "local": True, "supports_tools": True, "base_url": "b", "api_key": "k", "endpoint": "E"},
        {"model": "qwen3-coder-30b", "local": True, "supports_tools": True, "base_url": "b", "api_key": "k", "endpoint": "E"},
    ]
    monkeypatch.setattr(ca, "_list_candidates", lambda owner="": list(cands))
    # default picks the coder model
    assert ca._pick_endpoint()["model"] == "qwen3-coder-30b"
    # an explicit hint overrides the ranking
    assert ca._pick_endpoint(model_hint="llama3")["model"] == "llama3"
    # unknown hint falls back to best available
    assert ca._pick_endpoint(model_hint="does-not-exist")["model"] == "qwen3-coder-30b"


def test_pick_endpoint_none_when_no_candidates(monkeypatch):
    monkeypatch.setattr(ca, "_list_candidates", lambda owner="": [])
    assert ca._pick_endpoint() is None


def test_list_models_strips_keys(monkeypatch):
    monkeypatch.setattr(ca, "_list_candidates", lambda owner="": [
        {"model": "m", "local": True, "supports_tools": False, "base_url": "b", "api_key": "SECRET", "endpoint": "E"},
    ])
    out = ca.list_models()
    assert out and "api_key" not in out[0] and "base_url" not in out[0]
    assert out[0]["model"] == "m"


# ── workspace safety fence ─────────────────────────────────────────────────────
def test_validate_workspace_requires_existing_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("AEGIS_CODE_ROOT", raising=False)
    with pytest.raises(ValueError):
        ca._validate_workspace("")
    with pytest.raises(ValueError):
        ca._validate_workspace(str(tmp_path / "nope"))
    assert ca._validate_workspace(str(tmp_path)) == os.path.abspath(str(tmp_path))


def test_validate_workspace_code_root_fence(monkeypatch, tmp_path):
    root = tmp_path / "allowed"
    inside = root / "repo"
    outside = tmp_path / "elsewhere"
    inside.mkdir(parents=True)
    outside.mkdir()
    monkeypatch.setenv("AEGIS_CODE_ROOT", str(root))
    assert ca._validate_workspace(str(inside)).startswith(os.path.abspath(str(root)))
    with pytest.raises(ValueError):
        ca._validate_workspace(str(outside))


def test_in_git_repo_walks_up(tmp_path):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert ca._in_git_repo(str(nested)) is True
    assert ca._in_git_repo(str(tmp_path.parent)) is False


# ── output parsing ─────────────────────────────────────────────────────────────
def test_applied_edit_regex():
    out = "blah\nApplied edit to calc.py\nApplied edit to src/util.py\ndone"
    assert ca._APPLIED_RE.findall(out) == ["calc.py", "src/util.py"]


def test_clean_strips_prompt_toolkit_noise():
    raw = ("Can't initialize prompt toolkit: Found xterm-256color\n"
           "Windows console. Maybe try winpty\n"
           "in case of Cygwin, use the Python\n"
           "https://aider.chat/HISTORY.html\n"
           "Applied edit to calc.py")
    cleaned = ca._clean(raw)
    assert "Applied edit to calc.py" in cleaned
    assert "prompt toolkit" not in cleaned
    assert "Cygwin" not in cleaned
    assert "HISTORY" not in cleaned


# ── _prepare (command assembly) ────────────────────────────────────────────────
def _fake_ep():
    return {"model": "qwen3-coder-30b", "base_url": "http://127.0.0.1:9090/v1",
            "api_key": "sk-x", "endpoint": "llama-swap", "local": True, "supports_tools": True}


def test_prepare_builds_command_and_env(monkeypatch, tmp_path):
    monkeypatch.setattr(ca, "_aider_python", lambda: "PY")
    monkeypatch.setattr(ca, "_pick_endpoint", lambda owner="", model_hint=None: _fake_ep())
    monkeypatch.delenv("AEGIS_CODE_ROOT", raising=False)
    cmd, ws, env, ep, use_git = ca._prepare(
        str(tmp_path), "add a flag", model=None, files=["cli.py"], owner="", use_git=False)
    assert cmd[0] == "PY"
    assert "--model" in cmd and "openai/qwen3-coder-30b" in cmd
    assert "--no-git" in cmd            # tmp_path is not a git repo / forced off
    assert cmd[cmd.index("--message") + 1] == "add a flag"
    assert cmd[-1] == "cli.py"          # file appended after the message
    assert env["OPENAI_API_BASE"] == "http://127.0.0.1:9090/v1"
    assert env["OPENAI_API_KEY"] == "sk-x"
    assert use_git is False


def test_prepare_uses_git_when_repo_present(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(ca, "_aider_python", lambda: "PY")
    monkeypatch.setattr(ca, "_pick_endpoint", lambda owner="", model_hint=None: _fake_ep())
    monkeypatch.delenv("AEGIS_CODE_ROOT", raising=False)
    cmd, ws, env, ep, use_git = ca._prepare(
        str(tmp_path), "task", model=None, files=None, owner="", use_git=None)
    assert use_git is True
    assert "--no-git" not in cmd


def test_prepare_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(ca, "_aider_python", lambda: None)
    with pytest.raises(ValueError, match="not installed"):
        ca._prepare(str(tmp_path), "task", None, None, "", None)
    monkeypatch.setattr(ca, "_aider_python", lambda: "PY")
    with pytest.raises(ValueError, match="task is required"):
        ca._prepare(str(tmp_path), "  ", None, None, "", None)
    monkeypatch.setattr(ca, "_pick_endpoint", lambda owner="", model_hint=None: None)
    with pytest.raises(ValueError, match="No enabled model endpoint"):
        ca._prepare(str(tmp_path), "task", None, None, "", None)


# ── run_collect (fully mocked subprocess) ──────────────────────────────────────
class _FakePopen:
    output = ""
    returncode = 0

    def __init__(self, cmd, **kw):
        _FakePopen.last_cmd = cmd
        _FakePopen.last_kw = kw
        self.returncode = _FakePopen.returncode

    def communicate(self, timeout=None):
        return (_FakePopen.output, "")

    def kill(self):
        pass


def test_run_collect_happy_path(monkeypatch, tmp_path):
    monkeypatch.setattr(ca, "_aider_python", lambda: "PY")
    monkeypatch.setattr(ca, "_pick_endpoint", lambda owner="", model_hint=None: _fake_ep())
    monkeypatch.delenv("AEGIS_CODE_ROOT", raising=False)
    _FakePopen.output = "some log\nApplied edit to calc.py\nTokens: 10 sent"
    _FakePopen.returncode = 0
    monkeypatch.setattr(ca.subprocess, "Popen", _FakePopen)

    res = asyncio.run(ca.run_collect(str(tmp_path), "add subtract()", use_git=False))
    assert res["ok"] is True
    assert res["changed"] == ["calc.py"]
    assert res["model"] == "qwen3-coder-30b"
    assert "Applied edit to calc.py" in res["output"]


def test_run_collect_reports_bad_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(ca, "_aider_python", lambda: "PY")
    monkeypatch.setattr(ca, "_pick_endpoint", lambda owner="", model_hint=None: _fake_ep())
    res = asyncio.run(ca.run_collect(str(tmp_path / "missing"), "task"))
    assert res["ok"] is False
    assert "not a directory" in res["error"]


def test_run_collect_nonzero_returncode(monkeypatch, tmp_path):
    monkeypatch.setattr(ca, "_aider_python", lambda: "PY")
    monkeypatch.setattr(ca, "_pick_endpoint", lambda owner="", model_hint=None: _fake_ep())
    monkeypatch.delenv("AEGIS_CODE_ROOT", raising=False)
    _FakePopen.output = "boom"
    _FakePopen.returncode = 1
    monkeypatch.setattr(ca.subprocess, "Popen", _FakePopen)
    res = asyncio.run(ca.run_collect(str(tmp_path), "task", use_git=False))
    assert res["ok"] is False
    assert res["returncode"] == 1
