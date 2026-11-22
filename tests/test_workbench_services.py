"""Workbench services: projects, hosted apps, remote hosts, agent hand-offs.

Nothing here starts a service, opens an SSH connection or runs another agent:
systemd, tailscale, ssh and the agent CLIs are stubbed at ``common.run`` (or
one level above). Git is real — it runs in a temp folder.
"""
import json
import os
import subprocess

import pytest

from services.workbench import apps, common, delegates, hosts, projects
from services.workbench.common import WorkbenchError


@pytest.fixture
def wb(tmp_path, monkeypatch):
    """An enabled workbench rooted in a temp folder."""
    monkeypatch.delenv("AEGIS_WORKBENCH_DISABLED", raising=False)
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519.pub").write_text("ssh-ed25519 AAAATESTKEY test-server\n")
    monkeypatch.setattr(common, "real_home", lambda: home)
    monkeypatch.setattr(common, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(common, "settings", lambda: {"projects_dir": str(tmp_path / "apps")})
    monkeypatch.setattr(common, "_cache", None)
    for key, value in {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                       "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    return tmp_path


# ── common ─────────────────────────────────────────────────────────────────

def test_store_round_trip_and_defaults(wb):
    assert common.load()["apps"] == {} and common.load()["hosts"] == {}
    common.update(lambda data: data["apps"].__setitem__("x", {"name": "x"}))
    assert common.load()["apps"]["x"] == {"name": "x"}
    snapshot = common.load()
    snapshot["apps"]["x"]["name"] = "mutated"                 # callers get copies
    assert common.load()["apps"]["x"]["name"] == "x"
    assert json.loads((wb / "data" / "workbench.json").read_text())["version"] == 1


def test_user_env_restores_the_real_home_and_session(wb, monkeypatch):
    monkeypatch.setenv("HOME", "/somewhere/aegis/data")       # what the agent's shell has
    env = common.user_env({"EXTRA": "1"})
    assert env["HOME"] == str(wb / "home") and env["EXTRA"] == "1"
    assert env["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert str(wb / "home" / ".local/share/mise/shims") in env["PATH"].split(os.pathsep)


def test_agent_shell_env_points_git_and_gh_at_the_real_config(wb, monkeypatch):
    assert "GIT_CONFIG_GLOBAL" not in common.agent_shell_env()
    (wb / "home" / ".config" / "git").mkdir(parents=True)
    (wb / "home" / ".config" / "git" / "config").write_text("[user]\nname = T\n")
    (wb / "home" / ".config" / "gh").mkdir()
    env = common.agent_shell_env()
    assert env["GIT_CONFIG_GLOBAL"].endswith(".config/git/config") and env["GH_CONFIG_DIR"].endswith(".config/gh")
    assert "HOME" not in env                                  # the relocated HOME stays as it is
    monkeypatch.setenv("AEGIS_WORKBENCH_DISABLED", "1")
    assert common.agent_shell_env() == {}


@pytest.mark.parametrize("bad", ["", "Has Space", "../up", "-dash", "x" * 60, "semi;colon", "UPPER/slash"])
def test_names_are_strict(bad):
    with pytest.raises(WorkbenchError):
        common.valid_name(bad)


def test_run_never_raises(wb):
    assert common.run(["definitely-not-a-program-xyz"])[0] == 127
    rc, _out, err = common.run(["sleep", "5"], timeout=0.2)
    assert rc == 124 and "timed out" in err
    assert common.clip("x" * 100, 30).count("x") == 30 and "omitted" in common.clip("x" * 100, 30)


# ── projects ───────────────────────────────────────────────────────────────

def test_create_makes_a_committed_git_repo(wb):
    info = projects.create("fitness-tracker", "Workout log")
    path = wb / "apps" / "fitness-tracker"
    assert info["path"] == str(path) and info["git"] and info["branch"] == "main" and info["uncommitted"] == 0
    assert "Initial commit" in info["last_commit"]
    assert "Workout log" in (path / "README.md").read_text() and ".env" in (path / ".gitignore").read_text()
    with pytest.raises(WorkbenchError, match="already exists"):
        projects.create("fitness-tracker")
    for reserved in projects.PROTECTED:
        with pytest.raises(WorkbenchError, match="reserved"):
            projects.create(reserved)
    assert [p["name"] for p in projects.list_projects()] == ["fitness-tracker"]


@pytest.mark.parametrize("url", [
    "ext::sh -c 'touch /tmp/pwned'", "--upload-pack=touch /tmp/pwned", "file:///etc", "/etc/passwd",
    "http://insecure.example/repo.git", "fd::17", "https://ok.example/repo.git --config core.x=y", "",
])
def test_clone_takes_only_plain_git_transports(wb, monkeypatch, url):
    monkeypatch.setattr(common, "run", lambda *a, **k: pytest.fail("git must not run for a rejected URL"))
    with pytest.raises(WorkbenchError):
        projects.clone(url)


def test_clone_expands_github_shorthand_and_names_the_folder(wb, monkeypatch):
    seen = {}

    def fake_run(argv, cwd=None, timeout=60, input_text=None, env=None):
        if argv[:2] == ["git", "clone"]:
            seen["argv"] = argv
            os.makedirs(argv[-1])
            return 0, "", ""
        return 1, "", ""

    monkeypatch.setattr(common, "run", fake_run)
    info = projects.clone("Octo-Org/Hello.World")
    assert seen["argv"][:3] == ["git", "clone", "--"]                       # "--" ends option parsing
    assert seen["argv"][3] == "https://github.com/Octo-Org/Hello.World.git"
    assert info["name"] == "hello.world"
    with pytest.raises(WorkbenchError, match="reserved"):
        projects.clone("https://github.com/someone/aegis.git")


def test_opening_a_project_binds_it_to_one_chat(wb):
    projects.create("alpha")
    projects.create("beta")
    assert projects.bound_workspace("chat-1") is None
    info = projects.open_project("chat-1", "alpha")
    assert info["bound_to_chat"] and projects.bound_workspace("chat-1") == str(wb / "apps" / "alpha")
    assert projects.bound_workspace("chat-2") is None                        # per chat, not global
    projects.open_project("chat-1", str(wb / "apps" / "beta"))              # absolute path works too
    assert projects.bound_workspace("chat-1").endswith("/beta")
    assert projects.close_project("chat-1") is True and projects.close_project("chat-1") is False
    with pytest.raises(WorkbenchError, match="No project folder"):
        projects.open_project("chat-1", "missing")
    with pytest.raises(WorkbenchError):
        projects.open_project("", "alpha")


def test_binding_disappears_with_the_folder_or_the_switch(wb, monkeypatch):
    projects.create("gone")
    projects.open_project("chat-1", "gone")
    import shutil
    shutil.rmtree(wb / "apps" / "gone")
    assert projects.bound_workspace("chat-1") is None
    assert projects.forget_missing() == 1
    projects.create("kept")
    projects.open_project("chat-1", "kept")
    monkeypatch.setenv("AEGIS_WORKBENCH_DISABLED", "1")
    assert projects.bound_workspace("chat-1") is None


def test_sensitive_folders_cannot_become_a_workspace(wb):
    with pytest.raises(WorkbenchError):
        projects.open_project("chat-1", str(wb / "home" / ".ssh"))
    with pytest.raises(WorkbenchError):
        projects.open_project("chat-1", "/")


def test_status_reports_changes(wb, monkeypatch):
    monkeypatch.setattr(apps, "list_apps", lambda: [])
    projects.create("delta")
    (wb / "apps" / "delta" / "new.py").write_text("print(1)\n")
    status = projects.status("delta")
    assert status["uncommitted"] == 1 and "new.py" in status["changes"]


# ── apps ───────────────────────────────────────────────────────────────────

@pytest.fixture
def systemd(wb, monkeypatch):
    """Record systemctl/tailscale calls; pretend the app comes up."""
    calls = {"systemctl": [], "serve": [], "listening": set()}

    def fake_systemctl(*args, timeout=40):
        calls["systemctl"].append(args)
        if args[0] == "show":
            return 0, "ActiveState=active\nSubState=running\nMainPID=42\nMemoryCurrent=20000000\nNRestarts=0\n", ""
        if args[0] == "restart":
            name = args[1][len(apps.UNIT_PREFIX):-len(".service")]
            port = common.load()["apps"].get(name, {}).get("port")
            if port:
                calls["listening"].add(port)
        if args[0] == "is-active":
            return 0, "active\n", ""
        return 0, "", ""

    monkeypatch.setattr(apps, "_systemctl", fake_systemctl)
    monkeypatch.setattr(apps, "port_open", lambda port, host="127.0.0.1", timeout=0.6: port in calls["listening"])
    monkeypatch.setattr(apps, "_serve", lambda port, on: calls["serve"].append((port, on)) or "")
    monkeypatch.setattr(apps, "tailnet_host", lambda: "box.example.ts.net")
    monkeypatch.setattr(apps, "logs", lambda name, lines=60: "log line")
    return calls


def test_deploy_writes_a_user_unit_and_publishes_on_the_tailnet(wb, systemd):
    projects.create("fitness")
    result = apps.deploy("fitness", "uv run uvicorn app:app --port $PORT", env={"GREETING": 'a "b" 100%'})
    assert result["state"] == "running" and result["port"] == apps.PORT_RANGE[0] and result["listening"]
    assert result["url"] == f"https://box.example.ts.net:{apps.PORT_RANGE[0]}/"
    assert systemd["serve"] == [(apps.PORT_RANGE[0], True)]

    unit = (wb / "home" / ".config/systemd/user/aegis-app-fitness.service").read_text()
    assert f"WorkingDirectory={wb / 'apps' / 'fitness'}" in unit
    assert f'Environment="PORT={apps.PORT_RANGE[0]}"' in unit and 'Environment="HOST=127.0.0.1"' in unit
    assert 'Environment="GREETING=a \\"b\\" 100%%"' in unit              # quotes and % escaped for systemd
    assert f"EnvironmentFile=-{wb / 'apps' / 'fitness'}/.env" in unit
    assert "Restart=always" in unit and "WantedBy=default.target" in unit
    assert oct((wb / "home" / ".config/systemd/user/aegis-app-fitness.service").stat().st_mode)[-3:] == "600"
    script = (wb / "data" / "workbench" / "apps" / "fitness.sh").read_text()
    assert "uv run uvicorn app:app --port $PORT" in script and f'cd "{wb / "apps" / "fitness"}"' in script

    stored = common.load()["apps"]["fitness"]
    assert stored["env_keys"] == ["GREETING"] and "100%" not in json.dumps(stored)    # values are not kept in the registry
    verbs = [call[0] for call in systemd["systemctl"]]
    assert verbs[:3] == ["daemon-reload", "enable", "restart"]


def test_redeploy_keeps_the_port_and_second_app_gets_the_next(wb, systemd):
    projects.create("one")
    projects.create("two")
    first = apps.deploy("one", "python app.py")
    again = apps.deploy("one", "python app.py --verbose")
    second = apps.deploy("two", "python app.py")
    assert first["port"] == again["port"] and second["port"] == first["port"] + 1
    assert common.load()["apps"]["one"]["command"].endswith("--verbose")
    with pytest.raises(WorkbenchError, match="already belongs"):
        apps.deploy("two", "python app.py", port=first["port"])


def test_deploy_refuses_reserved_names_ports_and_bad_env(wb, systemd, monkeypatch):
    projects.create("ok")
    with pytest.raises(WorkbenchError, match="reserved"):
        apps.deploy("aegis", "python app.py", directory="ok")
    (wb / "apps" / "engine").mkdir()
    with pytest.raises(WorkbenchError, match="Aegis itself"):
        apps.deploy("sneaky", "python app.py", directory="engine")
    with pytest.raises(WorkbenchError, match="not available"):
        apps.deploy("ok", "python app.py", port=7000)
    with pytest.raises(WorkbenchError, match="start"):
        apps.deploy("ok", "   ")
    with pytest.raises(WorkbenchError, match="Bad environment"):
        apps.deploy("ok", "python app.py", env={"BAD NAME": "x"})
    with pytest.raises(WorkbenchError, match="set by the host"):
        apps.deploy("ok", "python app.py", env={"PORT": "1"})
    with pytest.raises(WorkbenchError, match="line breaks"):
        apps.deploy("ok", "python app.py", env={"KEY": "a\nb"})
    monkeypatch.setattr(apps, "port_open", lambda port, host="127.0.0.1", timeout=0.6: port == 8444)
    with pytest.raises(WorkbenchError, match="already listening"):
        apps.deploy("ok", "python app.py", port=8444)


def test_app_that_never_listens_is_reported_with_its_log(wb, systemd, monkeypatch):
    projects.create("slow")
    monkeypatch.setattr(apps, "port_open", lambda *a, **k: False)
    monkeypatch.setattr(apps.time, "sleep", lambda s: None)
    clock = iter(range(0, 10_000, 5))
    monkeypatch.setattr(apps.time, "time", lambda: next(clock))
    result = apps.deploy("slow", "python app.py")
    assert "nothing is listening" in result["notes"][0] and result["recent_log"] == "log line"


def test_worker_without_a_port_is_not_published(wb, systemd):
    projects.create("bot")
    result = apps.deploy("bot", "python bot.py", web=False)
    assert result["port"] is None and "url" not in result and systemd["serve"] == []
    assert "PORT=" not in (wb / "home" / ".config/systemd/user/aegis-app-bot.service").read_text()


def test_remove_stops_unpublishes_and_keeps_the_code(wb, systemd):
    projects.create("temp")
    port = apps.deploy("temp", "python app.py")["port"]
    result = apps.remove("temp")
    assert result == {"removed": "temp", "kept_folder": str(wb / "apps" / "temp")}
    assert (wb / "apps" / "temp" / "README.md").exists()
    assert not (wb / "home" / ".config/systemd/user/aegis-app-temp.service").exists()
    assert not (wb / "data" / "workbench" / "apps" / "temp.sh").exists()
    assert systemd["serve"][-1] == (port, False) and "temp" not in common.load()["apps"]
    assert ("disable", "--now", "aegis-app-temp.service") in systemd["systemctl"]
    with pytest.raises(WorkbenchError, match="No app named"):
        apps.remove("temp")


def test_control_and_exposure(wb, systemd):
    projects.create("ctl")
    port = apps.deploy("ctl", "python app.py")["port"]
    assert apps.control("ctl", "restart")["state"] == "running"
    with pytest.raises(WorkbenchError):
        apps.control("ctl", "reboot-the-machine")
    assert "url" not in apps.set_exposed("ctl", False) and systemd["serve"][-1] == (port, False)
    assert apps.set_exposed("ctl", True)["url"].endswith(f":{port}/")


# ── hosts ──────────────────────────────────────────────────────────────────

@pytest.fixture
def ssh(wb, monkeypatch):
    """Capture every command; answer like a Windows box by default."""
    calls = []
    state = {"os": "windows", "login": True, "rc": 0, "out": "ok\n", "err": ""}

    def fake_run(argv, cwd=None, timeout=60, input_text=None, env=None):
        calls.append({"argv": list(argv), "stdin": input_text, "timeout": timeout})
        if argv[0] == "ssh":
            remote = argv[-1]
            if remote == "echo aegis-ok":
                return (0, "aegis-ok\n", "") if state["login"] else (255, "", "Permission denied (publickey).")
            if remote == "echo %OS%":
                return 0, ("Windows_NT\r\n" if state["os"] == "windows" else "%OS%\n"), ""
            if remote == "uname -s":
                return (0, "Linux\n", "") if state["os"] == "linux" else (1, "", "not recognized")
        return state["rc"], state["out"], state["err"]

    monkeypatch.setattr(common, "run", fake_run)
    return calls, state


def test_add_detects_windows_and_defaults_to_powershell(wb, ssh):
    calls, _state = ssh
    host = hosts.add("desk", "100.64.0.9", "me", description="desktop")
    assert host["os"] == "windows" and host["shell"] == "powershell" and host["port"] == 22
    options = calls[0]["argv"]
    assert "BatchMode=yes" in options and "ControlMaster=auto" in options and "StrictHostKeyChecking=accept-new" in options
    assert "me@100.64.0.9" in options
    assert [h["name"] for h in hosts.list_hosts()] == ["local", "desk"]


def test_add_detects_linux_and_explains_a_failed_login(wb, ssh):
    _calls, state = ssh
    state["os"] = "linux"
    assert hosts.add("nas", "nas.lan", "admin")["shell"] == "bash"
    state["login"] = False
    with pytest.raises(WorkbenchError) as err:
        hosts.add("other", "10.0.0.9", "me")
    assert "Permission denied" in str(err.value) and "ssh-ed25519 AAAATESTKEY" in str(err.value)
    for address, user in (("bad host", "me"), ("ok.lan", "me;rm -rf"), ("-oProxyCommand=x", "me")):
        with pytest.raises(WorkbenchError):
            hosts.add("x", address, user)
    with pytest.raises(WorkbenchError):
        hosts.add("local", "ok.lan", "me")


def test_powershell_scripts_travel_on_stdin_never_the_command_line(wb, ssh):
    calls, state = ssh
    hosts.add("desk", "100.64.0.9", "me")
    calls.clear()
    state["out"] = "Windows 11\r\nRTX 4090\r\n"
    script = 'if ($true) {\n  "it\'s; | & > fine"\n}'
    result = hosts.run("desk", script, cwd="C:\\Users\\me\\it's")
    call = calls[0]
    assert call["argv"][-1] == ('powershell -NoLogo -NoProfile -NonInteractive -Command '
                                '"[Console]::In.ReadToEnd() | Invoke-Expression"')
    assert script not in " ".join(call["argv"]) and script in call["stdin"]
    assert "Set-Location -LiteralPath 'C:\\Users\\me\\it''s'" in call["stdin"]
    assert call["stdin"].rstrip().endswith("if ($LASTEXITCODE) { exit $LASTEXITCODE }")
    assert "EncodedCommand" not in " ".join(call["argv"])
    assert result == {"host": "desk", "shell": "powershell", "exit_code": 0, "output": "Windows 11\nRTX 4090"}


def test_bash_hosts_local_runs_and_failures(wb, ssh):
    calls, state = ssh
    state["os"] = "linux"
    hosts.add("nas", "nas.lan", "admin")
    calls.clear()
    hosts.run("nas", "df -h /", cwd="/srv/it's", timeout=99999)
    assert calls[0]["argv"][-1] == "bash -s" and calls[0]["stdin"].startswith("cd -- '/srv/it'\\''s' || exit 1\ndf -h /")
    assert calls[0]["timeout"] == hosts.MAX_TIMEOUT

    hosts.run("local", "echo hi")
    assert calls[1]["argv"] == ["bash", "-s"] and calls[1]["stdin"] == "echo hi\n"

    state.update(rc=255, out="", err="ssh: connect to host nas.lan port 22: No route to host")
    result = hosts.run("nas", "uptime")
    assert result["exit_code"] == 255 and "SSH itself failing" in result["note"] and "No route" in result["stderr"]

    with pytest.raises(WorkbenchError, match="Known hosts: local, nas"):
        hosts.run("nope", "uptime")
    with pytest.raises(WorkbenchError, match="Nothing to run"):
        hosts.run("nas", "   ")
    with pytest.raises(WorkbenchError, match="one line"):
        hosts.run("nas", "a\nb", shell="cmd")


def test_every_remote_command_is_audited(wb, ssh):
    hosts.add("desk", "100.64.0.9", "me")
    hosts.run("desk", "Remove-Item C:\\temp\\x\nGet-Date")
    log = (wb / "data" / "workbench" / "audit.log").read_text()
    assert "host-add desk me@100.64.0.9:22 windows/powershell" in log
    assert "host-run desk [powershell] Remove-Item C:\\temp\\x ⏎ Get-Date" in log


def test_copy_and_remove(wb, ssh):
    calls, state = ssh
    hosts.add("desk", "100.64.0.9", "me")
    calls.clear()
    hosts.copy("desk", "/srv/build.zip", "C:/Users/me/Downloads/", direction="put")
    argv = calls[0]["argv"]
    assert argv[0] == "scp" and argv[-3:] == ["--", "/srv/build.zip", "me@100.64.0.9:C:/Users/me/Downloads/"]
    hosts.copy("desk", "C:/logs/app.log", "/tmp/app.log", direction="get")
    assert calls[1]["argv"][-2:] == ["me@100.64.0.9:C:/logs/app.log", "/tmp/app.log"]
    state.update(rc=1, err="scp: no such file")
    with pytest.raises(WorkbenchError, match="copy failed"):
        hosts.copy("desk", "/nope", "C:/x")
    with pytest.raises(WorkbenchError):
        hosts.copy("local", "/a", "/b")
    assert hosts.remove("desk") == {"removed": "desk"}
    with pytest.raises(WorkbenchError):
        hosts.remove("local")


def test_tailnet_peers_are_listed_with_registration(wb, monkeypatch):
    status = {"Self": {"DNSName": "box.example.ts.net."}, "Peer": {
        "k1": {"HostName": "localhost", "DNSName": "iphone.example.ts.net.", "TailscaleIPs": ["100.1.1.1"], "OS": "iOS", "Online": True},
        "k2": {"HostName": "DESK", "DNSName": "desk.example.ts.net.", "TailscaleIPs": ["100.64.0.9"], "OS": "windows", "Online": False},
    }}
    monkeypatch.setattr(common, "run", lambda argv, **k: (0, json.dumps(status), ""))
    common.update(lambda data: data["hosts"].__setitem__("desk", {"name": "desk", "address": "100.64.0.9", "user": "u"}))
    peers = hosts.tailnet_peers()
    assert [(p["name"], p["os"], p["online"], p["registered_as"]) for p in peers] == [
        ("desk", "windows", False, "desk"), ("iphone", "iOS", True, "")]


# ── hand-offs ──────────────────────────────────────────────────────────────

def test_command_lines_per_agent_and_autonomy():
    assert delegates.build_argv("claude", "do it") == [
        "claude", "-p", "do it", "--output-format", "text", "--permission-mode", "acceptEdits"]
    assert delegates.build_argv("Claude Code", "do it", autonomy="full", resume=True)[-2:] == [
        "--dangerously-skip-permissions", "--continue"]
    # Kimi's prompt mode rejects --yolo/--auto, so neither is ever passed.
    assert delegates.build_argv("kimi", "do it", autonomy="full", resume=True) == [
        "kimi", "-p", "do it", "--output-format", "text", "--continue"]
    assert delegates.build_argv("codex", "do it")[-3:] == ["--sandbox", "workspace-write", "do it"]
    assert "--dangerously-bypass-approvals-and-sandbox" in delegates.build_argv("codex", "do it", autonomy="full")
    assert delegates.build_argv("opencode", "do it") == ["opencode", "run", "do it"]
    assert delegates.build_argv("opencode", "do it", autonomy="full") == ["opencode", "run", "--auto", "do it"]
    with pytest.raises(WorkbenchError, match="Unknown agent"):
        delegates.build_argv("hal9000", "open the pod bay doors")


def test_start_launches_a_background_job_in_the_project(wb, monkeypatch):
    import src.bg_jobs as bg_jobs

    projects.create("site")
    cwd = str(wb / "apps" / "site")
    launched = {}

    def fake_launch(command, session_id, cwd=None, max_runtime_s=3600):
        launched.update(command=command, session_id=session_id, cwd=cwd, max_runtime_s=max_runtime_s)
        return {"id": "job123", "status": "running"}

    monkeypatch.setattr(bg_jobs, "launch", fake_launch)
    monkeypatch.setattr(common, "which", lambda name: f"/usr/bin/{name}")
    task = "Add a login page; it's \"important\" && $(dangerous)"
    result = delegates.start("claude", task, cwd, "chat-9")
    assert result["job"] == "job123" and result["autonomy"] == "edits" and "do not poll" in result["note"]
    script = launched["command"]
    assert f"export HOME={wb / 'home'}" in script and f"cd {cwd} || exit 1" in script
    # The task is one shell-quoted argument: nothing in it is interpreted by bash.
    assert "'Add a login page; it'\"'\"'s \"important\" && $(dangerous)'" in script
    assert script.rstrip().endswith("< /dev/null")
    assert launched["session_id"] == "chat-9" and launched["max_runtime_s"] == delegates.MAX_RUNTIME_S
    assert common.load()["delegations"]["job123"]["agent"] == "claude"

    assert delegates.start("kimi", "x", cwd, "chat-9", autonomy="full")["autonomy"] == "the agent's own default"
    for kwargs in (dict(agent="claude", task="", cwd=cwd, session_id="c"),
                   dict(agent="claude", task="x", cwd="", session_id="c"),
                   dict(agent="claude", task="x", cwd=cwd, session_id=""),
                   dict(agent="claude", task="x", cwd=cwd, session_id="c", autonomy="godmode")):
        with pytest.raises(WorkbenchError):
            delegates.start(**kwargs)
    monkeypatch.setattr(common, "which", lambda name: None)
    with pytest.raises(WorkbenchError, match="not installed"):
        delegates.start("codex", "x", cwd, "chat-9")
