"""Phase-1 foundation fixes (2026-07-12 multi-agent review).

Covers: browser fallback ladder, pinned playwright spec, engine-node PATH env,
Windows Git-Bash foreground shell, frozen-build python guard, circular-import
break, PDF pypdf fallback.
"""
import asyncio
import os
import sys

import pytest


# ── browser fallback ladder (src/builtin_mcp) ────────────────────────────────
def test_playwright_mcp_spec_is_pinned():
    import src.builtin_mcp as bm
    spec = bm._playwright_mcp_spec()
    assert spec.startswith("@playwright/mcp@")
    assert "latest" not in spec, "spec must be pinned, never @latest (defeats the cache guard)"


def test_browser_args_prefers_headless_shell(monkeypatch):
    import src.builtin_mcp as bm
    monkeypatch.setattr(bm, "_find_playwright_headless_shell", lambda: r"C:\shell\chrome-headless-shell.exe")
    monkeypatch.setattr(bm, "_find_playwright_chromium", lambda: pytest.fail("should not reach rung 2"))
    args = bm._browser_server_args()
    assert "--executable-path" in args
    assert args[args.index("--executable-path") + 1].endswith("chrome-headless-shell.exe")


def test_browser_args_falls_back_to_chromium(monkeypatch):
    import src.builtin_mcp as bm
    monkeypatch.setattr(bm, "_find_playwright_headless_shell", lambda: None)
    monkeypatch.setattr(bm, "_find_playwright_chromium", lambda: r"C:\pw\chrome.exe")
    monkeypatch.setattr(bm, "_find_browser_channel", lambda: pytest.fail("should not reach rung 3"))
    args = bm._browser_server_args()
    assert args[args.index("--executable-path") + 1] == r"C:\pw\chrome.exe"


def test_browser_args_falls_back_to_system_channel(monkeypatch):
    import src.builtin_mcp as bm
    monkeypatch.setattr(bm, "_find_playwright_headless_shell", lambda: None)
    monkeypatch.setattr(bm, "_find_playwright_chromium", lambda: None)
    monkeypatch.setattr(bm, "_find_browser_channel", lambda: "msedge")
    args = bm._browser_server_args()
    assert "--browser" in args and args[args.index("--browser") + 1] == "msedge"
    assert "--executable-path" not in args


def test_browser_args_last_rung_still_returns_args(monkeypatch):
    import src.builtin_mcp as bm
    monkeypatch.setattr(bm, "_find_playwright_headless_shell", lambda: None)
    monkeypatch.setattr(bm, "_find_playwright_chromium", lambda: None)
    monkeypatch.setattr(bm, "_find_browser_channel", lambda: None)
    args = bm._browser_server_args()
    assert args[:1] == ["-y"] and any(a.startswith("@playwright/mcp@") for a in args)


def test_engine_node_env_prepends_node_dir(monkeypatch):
    import src.builtin_mcp as bm
    monkeypatch.setattr(bm, "_find_npx", lambda: os.path.join("X:", "engine", "node", "npx.cmd"))
    env = bm._engine_node_env()
    assert "PATH" in env
    assert env["PATH"].split(os.pathsep)[0] == os.path.join("X:", "engine", "node")


def test_npx_servers_use_args_factory():
    import src.builtin_mcp as bm
    cfg = bm._BUILTIN_NPX_SERVERS["builtin_browser"]
    assert "args_factory" in cfg and callable(cfg["args_factory"])


# ── frozen-build python guard (src/runtime_paths) ────────────────────────────
def test_get_python_exe_source_run():
    from src.runtime_paths import get_python_exe
    # Not frozen in the test process → real interpreter.
    assert get_python_exe() == sys.executable


def test_get_python_exe_frozen_without_system_python(monkeypatch):
    import shutil
    import src.runtime_paths as rp
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\App\Aegis.exe", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert rp.get_python_exe() is None


def test_get_python_exe_frozen_with_system_python(monkeypatch):
    import shutil
    import src.runtime_paths as rp
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\App\Aegis.exe", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: r"C:\Python\python.exe")
    assert rp.get_python_exe() == r"C:\Python\python.exe"


# ── circular import cluster is broken (lazy re-exports) ──────────────────────
@pytest.mark.parametrize("mod", [
    "src.tool_schemas", "src.tool_parsing", "src.tool_policy",
    "src.tool_security", "src.tool_execution",
])
def test_cold_import_each_cluster_module(mod):
    import importlib
    # A fresh subprocess proves the module imports with NO prior import of
    # src.agent_tools priming the cycle.
    import subprocess
    r = subprocess.run(
        [sys.executable, "-c", f"import {mod}"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert r.returncode == 0, f"cold import of {mod} failed: {r.stderr[-500:]}"


def test_agent_tools_reexports_still_resolve():
    import importlib
    at = importlib.import_module("src.agent_tools")
    assert callable(at.parse_tool_blocks)
    assert callable(at.execute_tool_block)
    assert len(at.FUNCTION_TOOL_SCHEMAS) > 50
    assert at.ToolBlock is not None  # own attribute, not lazy


# ── Windows foreground bash routes through Git Bash ──────────────────────────
def test_bash_tool_uses_git_bash_on_windows(monkeypatch):
    import src.agent_tools.subprocess_tools as st
    calls = {}

    async def _fake_exec(program, *args, **kwargs):
        calls["program"] = program
        calls["args"] = args
        raise RuntimeError("stop-after-spawn")  # we only need to see the spawn args

    monkeypatch.setattr("core.platform_compat.IS_WINDOWS", True, raising=False)
    monkeypatch.setattr("core.platform_compat.find_bash", lambda: r"C:\Git\bash.exe", raising=False)
    monkeypatch.setattr(st.asyncio, "create_subprocess_exec", _fake_exec)

    with pytest.raises(RuntimeError, match="stop-after-spawn"):
        asyncio.run(st.BashTool._spawn("echo $HOME"))
    assert calls["program"] == r"C:\Git\bash.exe"
    assert calls["args"][:2] == ("-c", "echo $HOME")


def test_bash_tool_passes_powershell_through(monkeypatch):
    import src.agent_tools.subprocess_tools as st
    calls = {}

    async def _fake_shell(content, **kwargs):
        calls["content"] = content
        raise RuntimeError("stop")

    monkeypatch.setattr("core.platform_compat.IS_WINDOWS", True, raising=False)
    monkeypatch.setattr("core.platform_compat.find_bash", lambda: r"C:\Git\bash.exe", raising=False)
    monkeypatch.setattr(st.asyncio, "create_subprocess_shell", _fake_shell)

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(st.BashTool._spawn("powershell -c Get-Date"))
    assert calls["content"] == "powershell -c Get-Date"  # untouched, not wrapped in bash -c


# ── PDF pypdf fallback (services/search/content) ─────────────────────────────
def test_content_module_has_pypdf_fallback():
    import services.search.content as content
    assert hasattr(content, "_pypdf_extract_text")
