"""Workbench agent tools, their admin gates, the chat-to-project binding in
the dispatcher, the HTTP endpoints and the chat commands.

The services underneath are covered in test_workbench_services.py; here they
are stubbed so the wiring itself is what is tested.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from services.workbench import apps, common, delegates, hosts, projects

TOOLS = ("manage_projects", "manage_apps", "manage_hosts", "delegate_agent")


@pytest.fixture
def wb(tmp_path, monkeypatch):
    monkeypatch.delenv("AEGIS_WORKBENCH_DISABLED", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(common, "real_home", lambda: home)
    monkeypatch.setattr(common, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(common, "settings", lambda: {"projects_dir": str(tmp_path / "apps")})
    monkeypatch.setattr(common, "_cache", None)
    for key, value in {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                       "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    return tmp_path


def _call(tool, content, **ctx):
    from src.agent_tools import TOOL_HANDLERS
    return asyncio.run(TOOL_HANDLERS[tool](content if isinstance(content, str) else json.dumps(content), ctx))


# ── registration and gates ─────────────────────────────────────────────────

def test_tools_are_registered_everywhere_and_admin_only():
    from src.agent_loop import TOOL_SECTIONS, _DOMAIN_RULES, _DOMAIN_TOOL_MAP
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_execution import _ADMIN_TOOLS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_policy import WEB_TOOL_NAMES
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
    from src.tool_security import (NON_ADMIN_BLOCKED_TOOLS, PLAN_MODE_READONLY_TOOLS,
                                   _PLAN_MODE_KNOWN_MUTATORS, is_public_blocked_tool)

    schemas = {t["function"]["name"]: t["function"] for t in FUNCTION_TOOL_SCHEMAS}
    for name in TOOLS:
        assert name in TOOL_HANDLERS and name in TOOL_TAGS and name in schemas
        assert name in BUILTIN_TOOL_DESCRIPTIONS and name in TOOL_SECTIONS
        assert schemas[name]["parameters"]["required"] == ["action"]
        # They start services, reach other machines and steer other agents.
        assert name in NON_ADMIN_BLOCKED_TOOLS and name in _ADMIN_TOOLS and is_public_blocked_tool(name)
        assert name in _PLAN_MODE_KNOWN_MUTATORS and name not in PLAN_MODE_READONLY_TOOLS
        assert name not in WEB_TOOL_NAMES
    assert _DOMAIN_TOOL_MAP["workbench"] == set(TOOLS) and "manage_hosts" in _DOMAIN_RULES["workbench"]
    block = function_call_to_tool_block("manage_hosts", json.dumps({"action": "run", "host": "desk", "command": "hostname"}))
    assert block.tool_type == "manage_hosts" and json.loads(block.content)["host"] == "desk"


def test_dispatcher_refuses_non_admins(wb, monkeypatch):
    from src import tool_execution
    from src.tool_schemas import function_call_to_tool_block

    monkeypatch.setattr(hosts, "run", lambda *a, **k: pytest.fail("a non-admin must never reach the host"))
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: False)
    block = function_call_to_tool_block("manage_hosts", json.dumps({"action": "run", "host": "desk", "command": "whoami"}))
    _desc, result = asyncio.run(tool_execution.execute_tool_block(block, session_id="chat-1", owner="guest"))
    assert result.get("exit_code") != 0 and "manage_hosts" in json.dumps(result)


def test_switch_turns_every_tool_off(wb, monkeypatch):
    monkeypatch.setenv("AEGIS_WORKBENCH_DISABLED", "1")
    for name in TOOLS:
        result = _call(name, {"action": "list"})
        assert result["exit_code"] == 1 and "switched off" in result["error"]


def test_keyword_intent_offers_the_right_tool(monkeypatch):
    import src.tool_index as tool_index

    index = tool_index.ToolIndex.__new__(tool_index.ToolIndex)
    monkeypatch.setattr(tool_index.ToolIndex, "retrieve", lambda self, query, k=8: [])
    monkeypatch.setattr(tool_index, "_library_ready", lambda: False)
    pick = index.get_tools_for_query
    assert "manage_hosts" in pick("restart the print spooler on my windows pc")
    assert "manage_apps" in pick("deploy it so I can open it from my phone")
    assert "manage_projects" in pick("clone that repo and take a look")
    assert "delegate_agent" in pick("ask kimi to refactor the parser")
    assert not set(TOOLS) & pick("what's a good dinner recipe")


# ── the binding in the dispatcher ──────────────────────────────────────────

def test_open_project_becomes_the_workspace_of_that_chat_only(wb, monkeypatch):
    from src import tool_execution

    projects.create("alpha")
    projects.open_project("chat-1", "alpha")
    seen = {}

    async def fake_impl(block, **kwargs):
        seen[kwargs["session_id"]] = tool_execution.get_active_workspace()
        return "desc", {"exit_code": 0}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", fake_impl)
    run = lambda sid, ws=None: asyncio.run(tool_execution.execute_tool_block(object(), session_id=sid, workspace=ws))  # noqa: E731
    run("chat-1")
    run("chat-1b", "/tmp")
    run("chat-2")
    assert seen == {"chat-1": str(wb / "apps" / "alpha"), "chat-1b": "/tmp", "chat-2": None}
    # The binding outranks the browser-wide workspace, and is dropped cleanly.
    seen.clear()
    asyncio.run(tool_execution.execute_tool_block(object(), session_id="chat-1", workspace="/tmp"))
    assert seen["chat-1"] == str(wb / "apps" / "alpha")
    assert tool_execution.get_active_workspace() is None            # reset after the call
    projects.close_project("chat-1")
    run("chat-1", "/tmp")
    assert seen["chat-1"] == "/tmp"


def test_agent_shell_gets_git_identity_but_keeps_its_relocated_home(wb, monkeypatch):
    from src import tool_execution

    (wb / "home" / ".config" / "git").mkdir(parents=True)
    (wb / "home" / ".config" / "git" / "config").write_text("[user]\nname = T\n")
    captured = {}

    async def fake_handler(content, ctx):
        captured.update(ctx["subproc_env"])
        return {"output": "ok", "exit_code": 0}

    monkeypatch.setitem(__import__("src.agent_tools", fromlist=["TOOL_HANDLERS"]).TOOL_HANDLERS, "bash", fake_handler)
    asyncio.run(tool_execution._direct_fallback("bash", "git status"))
    assert captured["GIT_CONFIG_GLOBAL"] == str(wb / "home" / ".config/git/config")
    assert captured["HOME"] == tool_execution._AGENT_WORKDIR != str(wb / "home")


# ── tools ──────────────────────────────────────────────────────────────────

def test_projects_tool_creates_binds_and_reports(wb, monkeypatch):
    monkeypatch.setattr(apps, "list_apps", lambda: [])
    made = _call("manage_projects", {"action": "create", "name": "fitness", "description": "Workout log"}, session_id="chat-1")
    assert made["exit_code"] == 0 and "This chat now works in that folder" in made["output"]
    assert projects.bound_workspace("chat-1") == str(wb / "apps" / "fitness")

    listed = json.loads(_call("manage_projects", "list", session_id="chat-1")["output"])
    assert listed["open_in_this_chat"].endswith("/fitness") and [p["name"] for p in listed["projects"]] == ["fitness"]
    status = json.loads(_call("manage_projects", {"action": "status"}, session_id="chat-1")["output"])
    assert status["name"] == "fitness"                               # defaults to the open project
    assert "no longer tied" in _call("manage_projects", {"action": "close"}, session_id="chat-1")["output"]
    bad = _call("manage_projects", {"action": "create", "name": "Bad Name"}, session_id="chat-1")
    assert bad["exit_code"] == 1 and "lower-case" in bad["error"]
    assert _call("manage_projects", {"action": "explode"})["exit_code"] == 1


def test_apps_tool_deploys_the_open_project_and_leads_with_the_address(wb, monkeypatch):
    projects.create("fitness")
    projects.open_project("chat-1", "fitness")
    seen = {}

    def fake_deploy(name, command, directory="", port=None, env=None, expose=True, description="", web=True):
        seen.update(name=name, command=command, directory=directory, port=port, env=env, expose=expose, web=web)
        return {"name": name, "state": "running", "port": 8300, "url": "https://box.example.ts.net:8300/"}

    monkeypatch.setattr(apps, "deploy", fake_deploy)
    result = _call("manage_apps", {"action": "deploy", "command": "python app.py", "port": "8301", "env": {"A": "1"}},
                   session_id="chat-1")
    assert seen == {"name": "fitness", "command": "python app.py", "directory": str(wb / "apps" / "fitness"),
                    "port": 8301, "env": {"A": "1"}, "expose": True, "web": True}
    assert result["output"].startswith("'fitness' is running. Open it at https://box.example.ts.net:8300/")
    assert "restarts by itself" in result["output"]

    monkeypatch.setattr(apps, "logs", lambda name, lines=60: "")
    assert _call("manage_apps", {"action": "logs", "name": "fitness"})["output"] == "(no log lines yet)"
    monkeypatch.setattr(apps, "remove", lambda name: {"removed": name, "kept_folder": "/x"})
    assert "was not touched" in _call("manage_apps", {"action": "remove", "name": "fitness"})["output"]


def test_hosts_tool_reports_remote_failures_as_failures(wb, monkeypatch):
    calls = []

    def fake_run(name, command, shell="", timeout=120, cwd=""):
        calls.append((name, command, shell, timeout, cwd))
        code = 1 if "fail" in command else 0
        return {"host": name, "shell": "powershell", "exit_code": code, "output": "out"}

    monkeypatch.setattr(hosts, "run", fake_run)
    progress = []

    async def go(payload):
        async def cb(event):
            progress.append(event["tail"])
        from src.agent_tools import TOOL_HANDLERS
        return await TOOL_HANDLERS["manage_hosts"](json.dumps(payload), {"progress_cb": cb})

    ok = asyncio.run(go({"host": "desk", "command": "Get-Date\nGet-Process", "timeout": 30, "cwd": "C:/x"}))
    assert ok["exit_code"] == 0 and calls[0] == ("desk", "Get-Date\nGet-Process", "", 30.0, "C:/x")   # action defaults to run
    assert progress == ["desk: Get-Date"]
    failed = asyncio.run(go({"action": "run", "host": "desk", "command": "fail"}))
    assert failed["exit_code"] == 1 and '"exit_code": 1' in failed["error"] and "output" not in failed
    monkeypatch.setattr(hosts, "list_hosts", lambda: [{"name": "local"}])
    assert json.loads(_call("manage_hosts", "list")["output"]) == {"hosts": [{"name": "local"}]}


def test_delegate_tool_uses_the_open_project_and_passes_the_owner(wb, monkeypatch):
    projects.create("site")
    projects.open_project("chat-1", "site")
    seen = {}

    def fake_start(agent, task, cwd, session_id, autonomy="", resume=False, owner=""):
        seen.update(agent=agent, task=task, cwd=cwd, session_id=session_id, autonomy=autonomy, resume=resume, owner=owner)
        return {"job": "j1", "agent": agent, "label": "Claude Code", "folder": cwd, "autonomy": "edits", "status": "running"}

    monkeypatch.setattr(delegates, "start", fake_start)
    result = _call("delegate_agent", {"agent": "claude", "task": "Add tests", "continue": "true"},
                   session_id="chat-1", owner="alice")
    assert seen == {"agent": "claude", "task": "Add tests", "cwd": str(wb / "apps" / "site"), "session_id": "chat-1",
                    "autonomy": "", "resume": True, "owner": "alice"}
    assert result["output"].startswith(f"Claude Code is working in {wb / 'apps' / 'site'} (job j1).")
    monkeypatch.setattr(delegates, "available", lambda: [{"agent": "kimi", "installed": True}])
    assert json.loads(_call("delegate_agent", {})["output"]) == {"agents": [{"agent": "kimi", "installed": True}]}


# ── endpoints ──────────────────────────────────────────────────────────────

@pytest.fixture
def client(wb, monkeypatch):
    from routes import workbench_routes

    monkeypatch.setattr(workbench_routes, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(workbench_routes.setup_workbench_routes())
    return TestClient(app)


def test_endpoints_are_admin_only(wb, monkeypatch):
    from routes import workbench_routes

    def deny(request):
        raise HTTPException(403, "Admin required")

    monkeypatch.setattr(workbench_routes, "require_admin", deny)
    monkeypatch.setattr(hosts, "run", lambda *a, **k: pytest.fail("must not run"))
    app = FastAPI()
    app.include_router(workbench_routes.setup_workbench_routes())
    guest = TestClient(app)
    assert guest.get("/api/workbench/overview").status_code == 403
    assert guest.post("/api/workbench/run", json={"host": "desk", "command": "whoami"}).status_code == 403
    assert guest.post("/api/workbench/project", json={"action": "open", "name": "x"}).status_code == 403
    assert guest.post("/api/workbench/app", json={"action": "remove", "name": "x"}).status_code == 403
    assert guest.get("/api/workbench/app/x/logs").status_code == 403


def test_overview_and_project_endpoints(client, wb, monkeypatch):
    monkeypatch.setattr(apps, "list_apps", lambda: [{"name": "fitness", "state": "running"}])
    monkeypatch.setattr(apps, "tailnet_host", lambda: "box.example.ts.net")
    monkeypatch.setattr(hosts, "tailnet_peers", lambda: [])
    monkeypatch.setattr(delegates, "available", lambda: [{"agent": "claude", "installed": True}])
    created = client.post("/api/workbench/project", json={"action": "create", "name": "fitness", "session_id": "chat-1"})
    assert created.status_code == 200 and created.json()["branch"] == "main"
    data = client.get("/api/workbench/overview", params={"session_id": "chat-1"}).json()
    assert data["open_project"].endswith("/fitness") and data["apps"][0]["name"] == "fitness"
    assert [h["name"] for h in data["hosts"]] == ["local"] and data["agents"][0]["agent"] == "claude"
    assert client.post("/api/workbench/project", json={"action": "close", "session_id": "chat-1"}).json() == {"closed": True}
    missing = client.post("/api/workbench/project", json={"action": "open", "name": "nope", "session_id": "chat-1"})
    assert missing.status_code == 400 and "No project folder" in missing.json()["detail"]
    assert client.post("/api/workbench/project", content=b"not json").status_code == 400


def test_run_and_app_endpoints(client, wb, monkeypatch):
    monkeypatch.setattr(hosts, "run", lambda host, command, shell="", timeout=120, cwd="": {
        "host": host, "exit_code": 0, "output": f"ran {command} ({timeout:.0f}s)"})
    out = client.post("/api/workbench/run", json={"host": "desk", "command": "Get-Date", "timeout": 30}).json()
    assert out["output"] == "ran Get-Date (30s)"
    monkeypatch.setattr(apps, "control", lambda name, action: {"name": name, "state": action})
    assert client.post("/api/workbench/app", json={"action": "restart", "name": "fitness"}).json()["state"] == "restart"
    assert client.post("/api/workbench/app", json={"action": "format-disk", "name": "fitness"}).status_code == 400
    monkeypatch.setattr(apps, "logs", lambda name, lines=60: f"{lines} lines of {name}")
    assert client.get("/api/workbench/app/fitness/logs", params={"lines": 5}).json() == {"name": "fitness", "log": "5 lines of fitness"}


def test_switch_turns_the_endpoints_off(client, monkeypatch):
    monkeypatch.setenv("AEGIS_WORKBENCH_DISABLED", "1")
    assert client.get("/api/workbench/overview").status_code == 503


# ── chat commands (source-level, per the repo's DOM-coupled convention) ────

SRC = Path("static/js/slashCommands.js").read_text(encoding="utf-8")


def test_commands_are_registered_without_collisions():
    registry = SRC[SRC.index("const COMMANDS = {"):SRC.index("const LEGACY_ALIASES")]
    names = re.findall(r"^  ['\"]?([a-z0-9][a-z0-9_-]*)['\"]?: \{", registry, flags=re.M)
    used = {}
    for match in re.finditer(r"alias: \[([^\]]*)\]", registry):
        for alias in re.findall(r"'([^']+)'", match.group(1)):
            used[alias] = used.get(alias, 0) + 1
    mine = {"project": ["projects", "proj"], "apps": ["app", "hosted", "deployed"],
            "hosts": ["machines", "pcs"], "remote": ["rc", "onhost"]}
    for name, aliases in mine.items():
        assert names.count(name) == 1
        for alias in aliases:
            assert used.get(alias) == 1 and alias not in names, alias
    # /run stays the local shell (an alias of /sh); the remote one is /remote.
    assert "'run'" in registry[registry.index("\n  sh: {"):registry.index("\n  sh: {") + 300]


def test_commands_call_the_admin_endpoints_and_stay_out_of_model_context():
    for needle in ("/api/workbench/overview", "/api/workbench/project", "/api/workbench/app", "/api/workbench/run"):
        assert needle in SRC
    remote = SRC[SRC.index("async function _cmdRemote(args)"):]
    remote = remote[:remote.index("\n}\n") + 3]
    assert "_persistMsg('assistant', md, { source: 'slash' })" in remote
    chat = Path("static/js/chat.js").read_text(encoding="utf-8")
    assert "'manage_hosts': 'Remote machine'" in chat and "'delegate_agent': 'Handing off'" in chat
