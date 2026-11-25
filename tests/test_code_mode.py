"""Code mode: which machine a chat codes on, and the coding tools carried out
on another machine.

What must hold: this server is the default and is "no target"; only an
administrator's chat can be pointed at another machine; on another machine
the shell and file tools behave like the local ones (same inputs, same
answers, the file exactly as written — line endings and all); nothing a
tool sends can break out of the quoting of the script that carries it.
"""
import asyncio
import base64
import json
import re
import subprocess

import pytest


@pytest.fixture
def bench(tmp_path, monkeypatch):
    from services.workbench import common, hosts, remote_tools, targets

    monkeypatch.setattr(common, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(common, "enabled", lambda: True)
    monkeypatch.setattr(common, "_cache", None)
    monkeypatch.setattr(common, "audit", lambda kind, line: None)

    def register(data):
        data["hosts"]["devbox"] = {"name": "devbox", "address": "10.0.0.5", "user": "me", "os": "linux", "shell": "bash"}
        data["hosts"]["desktop"] = {"name": "desktop", "address": "10.0.0.6", "user": "me", "os": "windows", "shell": "powershell"}

    common.update(register)
    work = tmp_path / "remote"
    work.mkdir()
    sent = []

    def run_there(name, command, shell="", timeout=120, cwd="", audit_line=None, output_limit=8000):
        """Stands in for SSH: a POSIX script really runs, in a folder of its
        own; a PowerShell script is recorded (there is no PowerShell here)."""
        sent.append({"host": name, "shell": shell, "command": command, "cwd": cwd, "audit": audit_line})
        if shell == "powershell":
            return {"host": name, "shell": shell, "exit_code": 0, "output": getattr(run_there, "answer", "")}
        script = (f"cd -- '{cwd}' || exit 1\n" if cwd else "") + command + "\n"
        done = subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, cwd=str(work), timeout=60)
        result = {"host": name, "shell": shell or "bash", "exit_code": done.returncode, "output": done.stdout[:output_limit]}
        if done.stderr.strip():
            result["stderr"] = done.stderr
        return result

    monkeypatch.setattr(hosts, "run", run_there)
    linux = {"name": "devbox", "os": "linux", "shell": "bash", "folder": str(work)}
    windows = {"name": "desktop", "os": "windows", "shell": "powershell", "folder": ""}

    def call(tool, content, target=linux):
        return asyncio.run(remote_tools.execute(tool, content if isinstance(content, str) else json.dumps(content), target))

    return type("Bench", (), {"targets": targets, "tools": remote_tools, "common": common, "work": work, "sent": sent,
                              "call": staticmethod(call), "linux": linux, "windows": windows, "run_there": run_there})


# ── which machine ──────────────────────────────────────────────────────────

def test_this_server_is_the_default_and_another_machine_is_a_per_chat_choice(bench):
    targets = bench.targets
    assert targets.remote("chat-1") is None
    assert targets.describe("chat-1") == {"host": "local", "label": "This server", "os": "linux", "shell": "bash",
                                           "folder": "", "remote": False}
    assert [m["name"] for m in targets.machines()] == ["local", "desktop", "devbox"]          # this server first
    assert targets.machines()[0]["label"] == "This server" and targets.machines()[0]["builtin"]

    picked = targets.bind("chat-1", "desktop")
    assert picked["remote"] and picked["host"] == "desktop" and picked["shell"] == "powershell"
    assert targets.remote("chat-1") == {"name": "desktop", "os": "windows", "shell": "powershell", "folder": ""}
    assert targets.remote("chat-2") is None                                                   # another chat is untouched

    targets.set_folder("chat-1", r"C:\Users\me\app")
    assert targets.bind("chat-1", "desktop")["folder"] == r"C:\Users\me\app"                  # the same machine keeps its folder
    assert targets.bind("chat-1", "devbox")["folder"] == ""                                   # a different one starts fresh
    assert targets.bind("chat-1", "local") == targets.describe("no-such-chat")                # back on this server
    assert targets.remote("chat-1") is None
    with pytest.raises(bench.common.WorkbenchError):
        targets.set_folder("chat-1", "/srv/app")                                              # nothing to set a folder for


def test_an_unknown_machine_falls_back_to_this_server_and_says_so(bench):
    targets = bench.targets
    targets.bind("chat-1", "devbox")
    info = targets.for_turn("chat-1", "laptop")
    assert info["remote"] is False and "laptop" in info["note"] and targets.remote("chat-1") is None
    assert "works on this server" in targets.directive(info)
    assert targets.for_turn("chat-1", "devbox")["note"] == ""
    # A machine removed from the registry stops being anyone's target.
    bench.common.update(lambda data: data["hosts"].pop("devbox"))
    assert targets.remote("chat-1") is None


def test_only_the_most_recent_choices_are_kept(bench, monkeypatch):
    monkeypatch.setattr(bench.targets, "_KEEP", 3)
    stamps = iter(range(1, 100))
    monkeypatch.setattr(bench.common, "now", lambda: next(stamps))
    for n in range(5):
        bench.targets.bind(f"chat-{n}", "devbox")
    assert sorted(bench.common.load()["session_targets"]) == ["chat-2", "chat-3", "chat-4"]


def test_only_an_administrators_chat_reaches_another_machine(bench, monkeypatch):
    from src import tool_execution

    bench.targets.bind("chat-1", "devbox")
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: owner == "zed")
    assert tool_execution._session_remote_target("chat-1", "zed")["name"] == "devbox"
    assert tool_execution._session_remote_target("chat-1", "mara") is None
    assert tool_execution._session_remote_target("chat-other", "zed") is None
    assert tool_execution._session_remote_target(None, "zed") is None


def test_what_a_code_turn_is_told(bench):
    here = bench.targets.directive({"host": "local", "remote": False})
    assert here.startswith("## Code mode\n") and "this server (Linux)" in here and "manage_projects" in here
    windows = bench.targets.directive({"host": "desktop", "remote": True, "os": "windows", "shell": "powershell", "folder": ""})
    assert "on desktop (Windows)" in windows and "PowerShell 5.1" in windows and "already connected to desktop" in windows
    assert "Do NOT use `manage_hosts` run for desktop" in windows          # the shell tool is already there
    assert '"action": "workdir"' in windows and "this server (Linux)" not in windows
    linux = bench.targets.directive({"host": "devbox", "remote": True, "os": "linux", "shell": "bash", "folder": "/srv/app"})
    assert "on devbox (Linux)" in linux and "`/srv/app`" in linux and "PowerShell" not in linux


# ── the tools, on a POSIX machine (really run, in a folder of their own) ──

def test_files_are_written_read_and_edited_exactly(bench):
    wrote = bench.call("write_file", "src/app.py\nprint('héllo — “quoted” $HOME `x`')\nx = 1\n")
    assert wrote["exit_code"] == 0 and wrote["diff"]["new_file"] and "on devbox" in wrote["output"]
    on_disk = (bench.work / "src" / "app.py").read_text(encoding="utf-8")
    assert on_disk == "print('héllo — “quoted” $HOME `x`')\nx = 1\n"                           # nothing expanded or mangled
    assert bench.call("read_file", "src/app.py")["output"] == on_disk
    assert bench.call("read_file", {"path": "src/app.py", "offset": 2, "limit": 1})["output"] == "x = 1\n"

    edited = bench.call("edit_file", {"path": "src/app.py", "old_string": "x = 1", "new_string": "x = 2"})
    assert edited["exit_code"] == 0 and edited["diff"]["added"] == 1 and "(1 replacement)" in edited["output"]
    assert (bench.work / "src" / "app.py").read_text(encoding="utf-8").endswith("x = 2\n")
    # The log names the file, never its contents.
    assert all("héllo" not in (entry["audit"] or "") for entry in bench.sent)
    assert any(entry["audit"] == f"write_file src/app.py ({len(on_disk.encode('utf-8'))} bytes)" for entry in bench.sent)


def test_windows_line_endings_and_a_byte_order_mark_survive_an_edit(bench):
    (bench.work / "settings.ini").write_bytes(b"\xef\xbb\xbf[main]\r\nname = old\r\nmode = fast\r\n")
    assert bench.call("read_file", "settings.ini")["output"] == "[main]\nname = old\nmode = fast\n"       # shown plainly
    edited = bench.call("edit_file", {"path": "settings.ini", "old_string": "name = old\nmode = fast", "new_string": "name = new\nmode = slow"})
    assert edited["exit_code"] == 0
    assert (bench.work / "settings.ini").read_bytes() == b"\xef\xbb\xbf[main]\r\nname = new\r\nmode = slow\r\n"
    bench.call("write_file", "settings.ini\n[main]\nname = rewritten\n")
    assert (bench.work / "settings.ini").read_bytes() == b"\xef\xbb\xbf[main]\r\nname = rewritten\r\n"


def test_file_errors_read_like_the_local_tools(bench):
    (bench.work / "pkg").mkdir()
    (bench.work / "twice.txt").write_text("same\nsame\n")
    (bench.work / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    assert bench.call("read_file", "missing.txt") == {"error": "read_file: missing.txt: not found", "exit_code": 1}
    assert "is a directory (use ls)" in bench.call("read_file", "pkg")["error"]
    assert "not a text file" in bench.call("read_file", "blob.bin")["error"]
    assert "use write_file to create it" in bench.call("edit_file", {"path": "missing.txt", "old_string": "a", "new_string": "b"})["error"]
    assert "not found in" in bench.call("edit_file", {"path": "twice.txt", "old_string": "nope", "new_string": "b"})["error"]
    assert "not unique" in bench.call("edit_file", {"path": "twice.txt", "old_string": "same", "new_string": "b"})["error"]
    assert bench.call("edit_file", {"path": "twice.txt", "old_string": "same", "new_string": "b", "replace_all": True})["exit_code"] == 0
    assert (bench.work / "twice.txt").read_text() == "b\nb\n"
    assert "identical" in bench.call("edit_file", {"path": "twice.txt", "old_string": "b", "new_string": "b"})["error"]
    assert "is a directory" in bench.call("write_file", "pkg\ncontent")["error"]
    too_big = bench.call("write_file", "big.txt\n" + "A" * (bench.tools.MAX_WRITE_BYTES + 1))
    assert "manage_hosts copy" in too_big["error"] and not (bench.work / "big.txt").exists()
    (bench.work / "huge.log").write_bytes(b"x" * (bench.tools.MAX_READ_BYTES + 1))
    assert "too large to read whole" in bench.call("read_file", "huge.log")["error"]


def test_a_file_name_cannot_break_out_of_the_script_that_carries_it(bench):
    name = "it's a $(touch pwned) `touch pwned2` ; rm -rf x \".txt"
    assert bench.call("write_file", name + "\nharmless")["exit_code"] == 0
    assert (bench.work / name).read_text() == "harmless"
    assert bench.call("read_file", name)["output"] == "harmless"
    assert bench.call("grep", {"pattern": "'; touch pwned3; echo '", "path": "."})["exit_code"] == 0
    assert bench.call("ls", "$(touch pwned4)")["exit_code"] == 1
    assert not list(bench.work.glob("pwned*"))


def test_finding_things_there(bench):
    (bench.work / "src").mkdir()
    (bench.work / "src" / "app.py").write_text("def main():\n    port = 8080\n")
    (bench.work / "src" / "util.py").write_text("PORT = 9090\n")
    (bench.work / "README.md").write_text("Runs on port 8080.\n")
    (bench.work / "node_modules" / "dep").mkdir(parents=True)
    (bench.work / "node_modules" / "dep" / "index.py").write_text("port = 1\n")

    listing = bench.call("ls", "")["output"]
    assert str(bench.work) in listing and "src/" in listing and "README.md" in listing
    assert bench.call("ls", "nowhere")["error"] == "ls: nowhere: not found"
    found = bench.call("glob", "**/*.py")["output"]
    assert "src/app.py" in found and "src/util.py" in found and "node_modules" not in found          # dependencies are skipped
    assert "No files matching" in bench.call("glob", {"pattern": "*.rs"})["output"]
    assert bench.call("glob", "")["exit_code"] == 1

    hits = bench.call("grep", {"pattern": r"port = \d+", "path": "."})["output"]
    assert "src/app.py:2:" in hits and "node_modules" not in hits and "util.py" not in hits
    loose = bench.call("grep", {"pattern": "port", "ignore_case": True, "glob": "*.py"})["output"]
    assert "util.py:1:PORT = 9090" in loose and "README" not in loose
    assert "No matches" in bench.call("grep", "nothing-like-this")["output"]


def test_the_shell_runs_there_in_the_working_folder(bench):
    ran = bench.call("bash", "pwd; echo err >&2; exit 3")
    assert ran["exit_code"] == 3 and str(bench.work) in ran["output"] and "STDERR: err" in ran["output"]
    assert bench.call("bash", "true")["output"] == "(no output)"
    assert "nothing to run" in bench.call("bash", "  ")["error"]
    assert "background jobs run on this server only" in bench.call("bash", "#!bg\nsleep 100")["error"]
    assert "runs on this server" in bench.call("python", "print(1)")["error"]
    assert "Background jobs run on this server only" in bench.call("manage_bg_jobs", "{}")["error"]
    assert "devbox" in bench.call("get_workspace", "")["output"]
    assert bench.call("web_search", "anything") is None                     # not a tool that moves: it runs here as usual


# ── the scripts sent to a Windows machine (inspected; there is no PowerShell here) ──

def test_what_is_sent_to_a_windows_machine(bench):
    target = {**bench.windows, "folder": r"C:\Users\me\it's here"}
    text = "línea one\nline two\n"
    bench.run_there.answer = "AEGIS:OK C:\\Users\\me\\it's here\\notes.txt\n"
    result = bench.call("write_file", "notes.txt\n" + text, target)
    assert result["exit_code"] == 0 and r"C:\Users\me\it's here\notes.txt on desktop" in result["output"]
    write = bench.sent[-1]
    assert write["shell"] == "powershell" and write["audit"].startswith("write_file notes.txt (")
    script = write["command"]
    assert "Set-Location -LiteralPath 'C:\\Users\\me\\it''s here'" in script                    # the quote is doubled, not left open
    assert "GetUnresolvedProviderPathFromPSPath('notes.txt')" in script and script.startswith("$ErrorActionPreference = 'Stop'")
    payload = re.search(r"FromBase64String\('([A-Za-z0-9+/=]*)'\)", script).group(1)
    assert base64.b64decode(payload).decode("utf-8") == text                                      # the file travels intact

    bench.run_there.answer = "AEGIS:OK C:\\app\\main.py\n" + base64.b64encode("a = 1\r\nb = 2\r\n".encode()).decode()
    assert bench.call("read_file", r"C:\app\main.py", target)["output"] == "a = 1\nb = 2\n"
    bench.run_there.answer = "AEGIS:NOFILE"
    assert bench.call("read_file", r"C:\app\nope.py", target)["error"] == r"read_file: C:\app\nope.py: not found"
    bench.run_there.answer = "AEGIS:ERROR Access to the path is denied."
    assert "Access to the path is denied" in bench.call("read_file", r"C:\Windows\x", target)["error"]
    bench.run_there.answer = "ssh: connect to host 10.0.0.6 port 22: No route to host"
    assert "did not carry that out" in bench.call("read_file", "x.txt", target)["error"]

    bench.run_there.answer = "Build succeeded."
    ran = bench.call("bash", "dotnet build", target)
    assert ran == {"output": "Build succeeded.", "exit_code": 0}
    assert bench.sent[-1]["command"] == "Set-Location -LiteralPath 'C:\\Users\\me\\it''s here' -ErrorAction Stop\ndotnet build"
    grep = bench.call("grep", {"pattern": "it's", "glob": "src/*.cs"}, target)
    assert "Select-String -Pattern 'it''s' -CaseSensitive" in bench.sent[-1]["command"] and "-Include '*.cs'" in bench.sent[-1]["command"]
    assert grep["exit_code"] in (0, 1)


# ── the agent's shell and Aegis's own repository ───────────────────────────

def test_git_cannot_find_aegis_own_repository_from_below(tmp_path):
    """The agent's shell starts in the data folder, inside the checkout Aegis
    runs from. `git add -A && git commit` there once committed all of Aegis."""
    import os
    from pathlib import Path
    from src import tool_execution

    root = Path(tool_execution._OWN_CHECKOUT)
    assert (root / "app.py").is_file()
    assert str(root) in os.environ["GIT_CEILING_DIRECTORIES"].split(os.pathsep)
    if not (root / ".git").exists():
        pytest.skip("not running from a git checkout")
    env = dict(os.environ)
    below = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(root / "tests"), env=env, capture_output=True, text=True)
    assert below.returncode != 0 and "not a git repository" in below.stderr.lower()
    on_purpose = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(root), env=env, capture_output=True, text=True)
    assert on_purpose.returncode == 0 and Path(on_purpose.stdout.strip()) == root
    # A project of its own, anywhere else, is unaffected.
    subprocess.run(["git", "init", "-q", str(tmp_path / "proj")], env=env, check=True)
    (tmp_path / "proj" / "sub").mkdir()
    inside = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(tmp_path / "proj" / "sub"), env=env, capture_output=True, text=True)
    assert inside.returncode == 0 and inside.stdout.strip().endswith("proj")


def test_a_run_without_a_chat_is_told_the_project_is_not_open(bench, monkeypatch):
    from services.workbench import projects
    from src.agent_tools.workbench_tools import ManageProjectsTool

    monkeypatch.setattr(projects, "create", lambda name, description="": {"name": name, "path": f"/srv/apps/{name}"})
    opened = []
    monkeypatch.setattr(projects, "open_project", lambda session_id, path: opened.append((session_id, path)) or {"path": path})
    tool = ManageProjectsTool()
    no_chat = asyncio.run(tool.execute(json.dumps({"action": "create", "name": "demoapp"}), {"session_id": "", "owner": "zed"}))
    assert no_chat["exit_code"] == 0 and opened == []
    assert "does NOT start there" in no_chat["output"] and "cd /srv/apps/demoapp &&" in no_chat["output"]
    in_chat = asyncio.run(tool.execute(json.dumps({"action": "create", "name": "demoapp"}), {"session_id": "chat-1", "owner": "zed"}))
    assert opened == [("chat-1", "/srv/apps/demoapp")] and "the shell starts there" in in_chat["output"]
