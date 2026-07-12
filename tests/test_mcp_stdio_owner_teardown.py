"""stdio MCP servers must connect + disconnect without crossing anyio cancel
scopes across tasks.

Regression for the shutdown "Attempted to exit cancel scope in a different task
than it was entered in" RuntimeError (and half-killed child processes): the
AsyncExitStack for a stdio server used to be entered in the connecting task but
aclose()'d from the shutdown task. Now a per-server OWNER task both enters and
exits the stack, so the lifecycle stays within one task.
"""
import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mgr():
    from src.mcp_manager import McpManager
    return McpManager()


def test_stdio_connect_then_disconnect_is_clean():
    """Connect a real stdio server (the troubleshoot toolbox — the one that
    logged the cancel-scope error) and disconnect. No RuntimeError, tools
    discovered, state cleaned up."""
    mgr = _mgr()

    async def go():
        ok = await mgr.connect_server(
            "t_owner", "test-troubleshoot", "stdio",
            command=sys.executable,
            args=[os.path.join("mcp_servers", "troubleshoot_server.py")],
            env={"PYTHONPATH": REPO},
        )
        assert ok is True
        assert len(mgr._tools.get("t_owner", [])) > 0
        # An owner task + close event are tracked for the stdio server.
        assert "t_owner" in mgr._owner_tasks
        assert "t_owner" in mgr._close_events
        owner = mgr._owner_tasks["t_owner"]

        await mgr.disconnect_server("t_owner")
        # Owner task has finished and cleaned up, no exception escaped.
        assert owner.done()
        assert "t_owner" not in mgr._owner_tasks
        assert "t_owner" not in mgr._sessions
        assert "t_owner" not in mgr._connections

    # If the cancel scope were crossed, this would raise inside the loop.
    asyncio.run(_run_in_fresh_cwd(go))


def test_disconnect_all_loops_cleanly():
    """disconnect_all (what lifespan shutdown calls) tears down every stdio
    server without error."""
    mgr = _mgr()

    async def go():
        for sid in ("a", "b"):
            ok = await mgr.connect_server(
                sid, f"srv-{sid}", "stdio",
                command=sys.executable,
                args=[os.path.join("mcp_servers", "troubleshoot_server.py")],
                env={"PYTHONPATH": REPO},
            )
            assert ok
        await mgr.disconnect_all()
        assert not mgr._owner_tasks
        assert not mgr._sessions

    asyncio.run(_run_in_fresh_cwd(go))


async def _run_in_fresh_cwd(coro_fn):
    # The server script path is repo-relative; run from the repo root.
    prev = os.getcwd()
    os.chdir(REPO)
    try:
        await coro_fn()
    finally:
        os.chdir(prev)
