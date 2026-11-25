"""Hosted apps: a project's start command kept running as a service, with an
address the user's devices can open.

Each app is a **systemd user unit** (``aegis-app-<name>.service``): it starts
at boot (user lingering is enabled once, by the installer), restarts when it
crashes, and is independent of Aegis — restarting Aegis does not touch it. No
root is involved at any point, which matters because a language model is the
one calling these functions.

Web apps get a port from a fixed range and are published on the tailnet with
``tailscale serve`` (HTTPS, tailnet devices only — never Funnel). The app
itself listens on loopback; the convention is that its command honours
``$PORT`` (and ``$HOST``, 127.0.0.1).
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import common
from .common import WorkbenchError
from .projects import PROTECTED, resolve

UNIT_PREFIX = "aegis-app-"
PORT_RANGE = (8300, 8899)
# Ports already spoken for on a typical Aegis host (Aegis, Kiwix + its tailnet
# port, ChromaDB, ComfyUI, llama-swap, Ollama).
RESERVED_PORTS = frozenset({7000, 8095, 8100, 8188, 8443, 9090, 11434})
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_tailnet: Optional[tuple] = None


def unit_name(name: str) -> str:
    return f"{UNIT_PREFIX}{name}.service"


def _unit_dir() -> Path:
    return common.real_home() / ".config" / "systemd" / "user"


def _script_path(name: str) -> Path:
    folder = common.work_dir() / "apps"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{name}.sh"


def _systemctl(*args: str, timeout: float = 40):
    return common.run(["systemctl", "--user", *args], timeout=timeout)


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _free_port(taken: set) -> int:
    for port in range(PORT_RANGE[0], PORT_RANGE[1] + 1):
        if port in taken or port in RESERVED_PORTS or port_open(port):
            continue
        return port
    raise WorkbenchError(f"No free port left in {PORT_RANGE[0]}–{PORT_RANGE[1]}.")


def tailnet_host() -> str:
    """This machine's tailnet DNS name ("box.tail1234.ts.net"), or ''."""
    global _tailnet
    if _tailnet and time.time() - _tailnet[0] < 300:
        return _tailnet[1]
    name = ""
    rc, out, _ = common.run(["tailscale", "status", "--json"], timeout=10)
    if rc == 0:
        try:
            name = (json.loads(out).get("Self") or {}).get("DNSName", "").rstrip(".")
        except Exception:
            name = ""
    _tailnet = (time.time(), name)
    return name


def _serve(port: int, on: bool) -> str:
    """Publish (or withdraw) a loopback port on the tailnet. Returns '' on
    success, else the reason."""
    if on:
        rc, out, err = common.run(
            ["tailscale", "serve", "--bg", f"--https={port}", f"http://127.0.0.1:{port}"], timeout=40)
    else:
        rc, out, err = common.run(["tailscale", "serve", f"--https={port}", "off"], timeout=40)
    return "" if rc == 0 else (err or out).strip()[:300]


def _url(app: dict) -> str:
    host = tailnet_host()
    if app.get("exposed") and host and app.get("port"):
        return f"https://{host}:{app['port']}/"
    return ""


def _quote_env(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise WorkbenchError("Environment values cannot contain line breaks — put multi-line secrets in the project's .env file.")
    # systemd expands %-specifiers in Environment= (not $variables).
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def _write_unit(app: dict, env: Dict[str, str]) -> None:
    name = app["name"]
    script = _script_path(name)
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"# Start command of the hosted app '{name}'. Managed by Aegis (manage_apps deploy).\n"
        f"cd {json.dumps(app['dir'])} || exit 1\n"
        f"{app['command']}\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    lines = [
        "[Unit]",
        f"Description=Aegis app: {name}",
        "After=network-online.target",
        # A crash loop backs off instead of spinning forever at full speed.
        "StartLimitIntervalSec=120",
        "StartLimitBurst=12",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={app['dir']}",
        f'Environment="PATH={_quote_env(common.tool_path())}"',
        'Environment="HOST=127.0.0.1"',
    ]
    if app.get("port"):
        lines.append(f'Environment="PORT={app["port"]}"')
    for key, value in sorted(env.items()):
        lines.append(f'Environment="{key}={_quote_env(str(value))}"')
    lines += [
        # Secrets belong in the project's .env (git-ignored by the project template).
        f"EnvironmentFile=-{app['dir']}/.env",
        f"ExecStart=/usr/bin/bash {script}",
        "Restart=always",
        "RestartSec=3",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    unit_dir = _unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / unit_name(name)
    unit.write_text("\n".join(lines), encoding="utf-8")
    unit.chmod(0o600)                       # it may carry environment values


def _live(app: dict) -> dict:
    """Registry entry + what systemd and the port say right now."""
    out = dict(app)
    out.pop("env_keys", None)
    rc, text, _ = _systemctl("show", unit_name(app["name"]), "-p",
                             "ActiveState,SubState,MainPID,MemoryCurrent,ActiveEnterTimestamp,NRestarts", timeout=15)
    props = dict(line.split("=", 1) for line in text.splitlines() if "=" in line) if rc == 0 else {}
    state = props.get("ActiveState", "unknown")
    out["state"] = state if state != "active" else props.get("SubState", "running")
    if props.get("ActiveEnterTimestamp"):
        out["since"] = props["ActiveEnterTimestamp"]
    if props.get("NRestarts", "0") not in ("", "0"):
        out["restarts"] = int(props["NRestarts"])
    memory = props.get("MemoryCurrent", "")
    if memory.isdigit() and int(memory) < 1 << 50:
        out["memory_mb"] = round(int(memory) / 1e6, 1)
    if app.get("port"):
        out["listening"] = port_open(app["port"])
        out["local_url"] = f"http://127.0.0.1:{app['port']}/"
    url = _url(app)
    if url:
        out["url"] = url
    return out


def list_apps() -> List[dict]:
    return [_live(app) for _name, app in sorted(common.load()["apps"].items())]


def _get(name: str) -> dict:
    name = common.valid_name(name)
    app = common.load()["apps"].get(name)
    if not app:
        known = ", ".join(sorted(common.load()["apps"])) or "none"
        raise WorkbenchError(f"No app named '{name}'. Deployed apps: {known}.")
    return app


def logs(name: str, lines: int = 60) -> str:
    _get(name)
    lines = max(5, min(int(lines or 60), 400))
    rc, out, err = common.run(
        ["journalctl", "--user", "-u", unit_name(common.valid_name(name)), "-n", str(lines), "--no-pager", "-o", "cat"],
        timeout=20)
    return common.clip(out if rc == 0 else (err or out), 8000)


def deploy(
    name: str,
    command: str,
    directory: str = "",
    port: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
    expose: bool = True,
    description: str = "",
    web: bool = True,
) -> dict:
    """Create or update an app and (re)start it.

    ``command`` is run by bash from the project folder; it should start the
    app in the foreground and listen on ``$PORT``. ``web=False`` is for
    workers and bots that have no port. Re-deploying an existing name keeps
    its port and address.
    """
    name = common.valid_name(name)
    if name in PROTECTED:
        raise WorkbenchError(f"'{name}' is reserved.")
    command = (command or "").strip()
    if not command:
        raise WorkbenchError("Give the command that starts the app in the foreground (it should listen on $PORT).")
    path = resolve(directory or name)
    if Path(path).name in PROTECTED:
        raise WorkbenchError("That folder is Aegis itself (or its engines); it is not deployed through this tool.")
    env = dict(env or {})
    for key in env:
        if not _ENV_KEY.match(str(key)):
            raise WorkbenchError(f"Bad environment variable name: {key!r}")
        if key in ("PORT", "HOST", "PATH"):
            raise WorkbenchError(f"{key} is set by the host; use the port argument instead.")

    registry = common.load()["apps"]
    previous = registry.get(name)
    if web:
        if port is None:
            port = previous.get("port") if previous and previous.get("port") else None
        if port is None:
            port = _free_port({a.get("port") for a in registry.values() if a.get("port")})
        port = int(port)
        if not (1024 <= port <= 65535) or port in RESERVED_PORTS:
            raise WorkbenchError(f"Port {port} is not available for apps.")
        clash = [a["name"] for a in registry.values() if a.get("port") == port and a["name"] != name]
        if clash:
            raise WorkbenchError(f"Port {port} already belongs to the app '{clash[0]}'.")
        if not previous and port_open(port):
            raise WorkbenchError(f"Something else is already listening on port {port}.")
    else:
        port = None
        expose = False

    app = {
        "name": name,
        "dir": str(path),
        "command": command,
        "port": port,
        "web": bool(web),
        "exposed": bool(expose and web),
        "description": description.strip()[:200],
        "env_keys": sorted(env),
        "created": (previous or {}).get("created") or common.now(),
        "updated": common.now(),
    }
    _write_unit(app, env)
    common.update(lambda data: data["apps"].__setitem__(name, app))
    common.audit("deploy", f"{name} dir={path} port={port} cmd={command}")

    _systemctl("daemon-reload")
    rc, _, err = _systemctl("enable", unit_name(name))
    if rc != 0:
        raise WorkbenchError(f"Could not enable the service: {err.strip()[:300]}")
    rc, _, err = _systemctl("restart", unit_name(name))
    if rc != 0:
        raise WorkbenchError(f"Could not start the service: {err.strip()[:300]}")

    notes: List[str] = []
    if web:
        deadline = time.time() + 30
        while time.time() < deadline and not port_open(port):
            state = _systemctl("is-active", unit_name(name), timeout=10)[1].strip()
            if state == "failed":
                break
            time.sleep(0.7)
        if not port_open(port):
            notes.append(
                f"The service started but nothing is listening on 127.0.0.1:{port} yet. "
                "Check that the command starts the app in the foreground and listens on $PORT."
            )
        if app["exposed"]:
            problem = _serve(port, True)
            if problem:
                notes.append(f"Could not publish it on the tailnet: {problem}")
                app["exposed"] = False
                common.update(lambda data: data["apps"][name].__setitem__("exposed", False))
    else:
        time.sleep(1.5)

    result = _live(app)
    if notes:
        result["notes"] = notes
    if result.get("state") not in ("running", "start", "activating") or notes:
        result["recent_log"] = logs(name, 25)
    return result


def status(name: str) -> dict:
    app = _get(name)
    result = _live(app)
    result["recent_log"] = logs(app["name"], 20)
    return result


def control(name: str, action: str) -> dict:
    app = _get(name)
    if action not in ("start", "stop", "restart"):
        raise WorkbenchError("action must be start, stop or restart")
    rc, _, err = _systemctl(action, unit_name(app["name"]))
    if rc != 0:
        raise WorkbenchError(f"{action} failed: {err.strip()[:300]}")
    common.audit(action, app["name"])
    if action != "stop" and app.get("port"):
        deadline = time.time() + 20
        while time.time() < deadline and not port_open(app["port"]):
            time.sleep(0.7)
    return _live(app)


def set_exposed(name: str, exposed: bool) -> dict:
    app = _get(name)
    if not app.get("port"):
        raise WorkbenchError("That app has no port to publish.")
    problem = _serve(app["port"], bool(exposed))
    if problem and exposed:
        raise WorkbenchError(f"tailscale serve failed: {problem}")
    common.update(lambda data: data["apps"][app["name"]].__setitem__("exposed", bool(exposed)))
    app["exposed"] = bool(exposed)
    common.audit("expose" if exposed else "unexpose", app["name"])
    return _live(app)


def remove(name: str) -> dict:
    """Stop and unregister an app. The project folder and its code are left alone."""
    app = _get(name)
    unit = unit_name(app["name"])
    _systemctl("disable", "--now", unit)
    for path in (_unit_dir() / unit, _script_path(app["name"])):
        try:
            path.unlink()
        except OSError:
            pass
    _systemctl("daemon-reload")
    _systemctl("reset-failed", unit)
    if app.get("port") and app.get("exposed"):
        _serve(app["port"], False)
    common.update(lambda data: data["apps"].pop(app["name"], None))
    common.audit("remove", app["name"])
    return {"removed": app["name"], "kept_folder": app["dir"]}
