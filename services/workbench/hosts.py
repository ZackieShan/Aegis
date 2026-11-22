"""Remote hosts: run commands and copy files on other machines over SSH.

A host is a name for ``user@address`` plus what kind of machine it is. The
transport is the system ``ssh`` with the server's own key (no passwords, no
prompts — ``BatchMode``), reached over the tailnet or the LAN.

Three details carry most of the weight:

* **One connection per host, reused** (``ControlMaster``). Windows' OpenSSH
  server in particular dislikes a burst of fresh logins.
* **Scripts travel on stdin, never on the command line.** A multi-line
  PowerShell script quoted through ``cmd.exe`` is a coin toss, and
  ``-EncodedCommand`` is what malware does. ``[Console]::In.ReadToEnd() |
  Invoke-Expression`` evaluates the whole script as one unit; ``bash -s`` does
  the same on Unix.
* **Every command is written to the audit log** before it runs.
"""

from __future__ import annotations

import json
import re
import socket
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import common
from .common import WorkbenchError

_ADDRESS = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.:-]{0,251}[A-Za-z0-9])?$")
_USER = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._\\ -]{0,63}$")
OS_KINDS = ("linux", "windows", "macos")
SHELLS = ("bash", "powershell", "cmd")
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 3600

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(name: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(name, threading.Lock())


def _control_dir() -> Path:
    path = common.real_home() / ".ssh" / "cm"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _ssh_options() -> List[str]:
    options = {
        "BatchMode": "yes",                          # keys only; never wait on a prompt
        "ConnectTimeout": "10",
        "LogLevel": "ERROR",
        "StrictHostKeyChecking": "accept-new",       # trust on first use, refuse a changed key
        "ControlMaster": "auto",
        "ControlPath": str(_control_dir() / "aegis-%C"),
        "ControlPersist": "120",
        "ServerAliveInterval": "15",
        "ServerAliveCountMax": "4",
    }
    out: List[str] = []
    for key, value in options.items():
        out += ["-o", f"{key}={value}"]
    return out


def _target(host: dict) -> str:
    return f"{host['user']}@{host['address']}"


def list_hosts() -> List[dict]:
    hosts = [{"name": "local", "address": "this server", "os": "linux", "shell": "bash", "builtin": True}]
    hosts += [dict(entry) for _name, entry in sorted(common.load()["hosts"].items())]
    return hosts


def get(name: str) -> dict:
    name = (name or "").strip().lower()
    if name in ("local", "localhost", "this", "", socket.gethostname().lower()):
        return {"name": "local", "os": "linux", "shell": "bash", "builtin": True}
    entry = common.load()["hosts"].get(name)
    if not entry:
        known = ", ".join(["local"] + sorted(common.load()["hosts"]))
        raise WorkbenchError(f"No host named '{name}'. Known hosts: {known}. Add one with action add.")
    return entry


def _raw_ssh(host: dict, remote_command: str, stdin: Optional[str], timeout: float) -> Tuple[int, str, str]:
    argv = ["ssh", *_ssh_options(), "-p", str(host.get("port") or 22), _target(host), remote_command]
    return common.run(argv, timeout=timeout, input_text=stdin if stdin is not None else "")


def _detect_os(host: dict) -> str:
    # cmd.exe expands %OS% to "Windows_NT"; a Unix shell echoes it back
    # untouched. (Asking Windows for `cmd /c ver` is not reliable: the SSH
    # session's PATH does not always include System32.)
    rc, out, _ = _raw_ssh(host, "echo %OS%", None, 25)
    if rc == 0 and "windows_nt" in out.lower():
        return "windows"
    rc, out, _ = _raw_ssh(host, "uname -s", None, 25)
    kind = out.strip().lower()
    if rc == 0 and kind:
        return "macos" if "darwin" in kind else "linux"
    return ""


def add(name: str, address: str, user: str, port: int = 22, os_kind: str = "", shell: str = "",
        description: str = "") -> dict:
    """Register a machine. It must already accept this server's SSH key."""
    name = common.valid_name(name)
    if name in ("local", "localhost"):
        raise WorkbenchError("'local' is this server.")
    address, user = (address or "").strip(), (user or "").strip()
    if not _ADDRESS.match(address):
        raise WorkbenchError("address must be a host name or an IP address.")
    if not _USER.match(user):
        raise WorkbenchError("user is the account to log in as on that machine.")
    port = int(port or 22)
    host = {"name": name, "address": address, "user": user, "port": port}

    with _lock_for(name):
        rc, out, err = _raw_ssh(host, "echo aegis-ok", None, 25)
        if rc != 0 or "aegis-ok" not in out:
            public_key = ""
            try:
                public_key = (common.real_home() / ".ssh" / "id_ed25519.pub").read_text(encoding="utf-8").strip()
            except OSError:
                pass
            where = "authorized_keys"
            if (os_kind or "windows") == "windows":
                where += " (for a Windows administrator account: C:/ProgramData/ssh/administrators_authorized_keys)"
            detail = (err or out).strip()[:200] or "no answer"
            raise WorkbenchError(
                f"Could not log in to {user}@{address}: {detail}. That machine must run an SSH server and "
                f"list this server's public key in its {where}" + (f":\n{public_key}" if public_key else ".")
            )
        os_kind = (os_kind or "").strip().lower() or _detect_os(host)
    if os_kind not in OS_KINDS:
        raise WorkbenchError("Could not tell what the machine runs — pass os: linux, windows or macos.")
    shell = (shell or "").strip().lower() or ("powershell" if os_kind == "windows" else "bash")
    if shell not in SHELLS:
        raise WorkbenchError("shell must be bash, powershell or cmd.")
    host.update(os=os_kind, shell=shell, description=description.strip()[:200], added=common.now())
    common.update(lambda data: data["hosts"].__setitem__(name, host))
    common.audit("host-add", f"{name} {user}@{address}:{port} {os_kind}/{shell}")
    return host


def remove(name: str) -> dict:
    host = get(name)
    if host.get("builtin"):
        raise WorkbenchError("'local' cannot be removed.")
    common.update(lambda data: data["hosts"].pop(host["name"], None))
    common.audit("host-remove", host["name"])
    return {"removed": host["name"]}


def _powershell_script(command: str, cwd: str) -> str:
    lines = [
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8",
        "$ProgressPreference = 'SilentlyContinue'",
    ]
    if cwd:
        lines.append("Set-Location -LiteralPath '" + cwd.replace("'", "''") + "'")
    lines.append(command)
    lines.append("if ($LASTEXITCODE) { exit $LASTEXITCODE }")
    return "\n".join(lines) + "\n"


def _bash_script(command: str, cwd: str) -> str:
    prefix = ""
    if cwd:
        prefix = "cd -- '" + cwd.replace("'", "'\\''") + "' || exit 1\n"
    return prefix + command + "\n"


def run(name: str, command: str, shell: str = "", timeout: float = DEFAULT_TIMEOUT, cwd: str = "",
        audit_line: Optional[str] = None, output_limit: int = 8000) -> dict:
    """Run a command or a whole script on a host. Returns exit code and output.

    ``audit_line`` replaces the command in the audit log (a file write would
    otherwise log the file); ``output_limit`` raises the cap on what comes
    back, for callers that read files through this."""
    host = get(name)
    command = (command or "").strip("\n")
    if not command.strip():
        raise WorkbenchError("Nothing to run — give the command.")
    timeout = max(5, min(float(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    shell = (shell or host.get("shell") or "bash").strip().lower()
    if shell not in SHELLS:
        raise WorkbenchError("shell must be bash, powershell or cmd.")
    common.audit("host-run", f"{host['name']} [{shell}] " + (audit_line or ' ⏎ '.join(command.splitlines())))

    with _lock_for(host["name"]):
        if host.get("builtin"):
            if shell != "bash":
                raise WorkbenchError("This server runs bash.")
            rc, out, err = common.run(["bash", "-s"], input_text=_bash_script(command, cwd), timeout=timeout,
                                      cwd=str(common.real_home()))
        elif shell == "powershell":
            rc, out, err = _raw_ssh(
                host,
                # The script arrives on stdin as UTF-8. Say so before the first
                # read, or Windows PowerShell decodes it with the console's code
                # page and every accented letter or curly quote in a command or
                # a path turns into mojibake.
                'powershell -NoLogo -NoProfile -NonInteractive -Command '
                '"[Console]::InputEncoding = [Text.Encoding]::UTF8; [Console]::In.ReadToEnd() | Invoke-Expression"',
                _powershell_script(command, cwd), timeout)
        elif shell == "cmd":
            if "\n" in command:
                raise WorkbenchError("cmd takes one line; use shell powershell for a script.")
            line = f'cd /d "{cwd}" && {command}' if cwd else command
            rc, out, err = _raw_ssh(host, line, None, timeout)
        else:
            rc, out, err = _raw_ssh(host, "bash -s", _bash_script(command, cwd), timeout)

    out, err = out.replace("\r\n", "\n"), err.replace("\r\n", "\n")
    result = {"host": host["name"], "shell": shell, "exit_code": rc, "output": common.clip(out, output_limit)}
    if err.strip():
        result["stderr"] = common.clip(err, 3000)
    if rc == 255 and not out.strip():
        result["note"] = "exit 255 is SSH itself failing (host asleep, off the network, or the key no longer accepted)."
    return result


def test(name: str) -> dict:
    host = get(name)
    probe = "hostname" if host.get("shell") != "powershell" else "$env:COMPUTERNAME + ' ' + [Environment]::OSVersion.VersionString"
    result = run(host["name"], probe, timeout=30)
    return {"host": host["name"], "reachable": result["exit_code"] == 0,
            "answer": (result.get("output") or result.get("stderr") or "").strip()[:200]}


def copy(name: str, source: str, destination: str, direction: str = "put", timeout: float = 900) -> dict:
    """Copy a file or folder. ``put``: this server → host. ``get``: host → this server."""
    host = get(name)
    if host.get("builtin"):
        raise WorkbenchError("Copying to this server itself is just a file operation.")
    source, destination = (source or "").strip(), (destination or "").strip()
    if not source or not destination:
        raise WorkbenchError("Give both source and destination paths.")
    if direction not in ("put", "get"):
        raise WorkbenchError("direction is put (to the host) or get (from the host).")
    remote = lambda path: f"{_target(host)}:{path}"          # noqa: E731
    pair = [source, remote(destination)] if direction == "put" else [remote(source), destination]
    common.audit("host-copy", f"{host['name']} {direction} {source} -> {destination}")
    argv = ["scp", *_ssh_options(), "-P", str(host.get("port") or 22), "-r", "-p", "--", *pair]
    with _lock_for(host["name"]):
        rc, out, err = common.run(argv, timeout=max(30, min(float(timeout), MAX_TIMEOUT)))
    if rc != 0:
        raise WorkbenchError(f"copy failed: {(err or out).strip()[:400]}")
    return {"host": host["name"], "direction": direction, "source": source, "destination": destination, "ok": True}


def tailnet_peers() -> List[dict]:
    """Machines on the tailnet, for 'what can I reach?'."""
    rc, out, _ = common.run(["tailscale", "status", "--json"], timeout=10)
    if rc != 0:
        return []
    try:
        status = json.loads(out)
    except Exception:
        return []
    registered = {entry.get("address"): entry["name"] for entry in common.load()["hosts"].values()}
    peers = []
    for peer in (status.get("Peer") or {}).values():
        addresses = peer.get("TailscaleIPs") or []
        dns = (peer.get("DNSName") or "").rstrip(".")
        # iOS reports its HostName as "localhost"; the tailnet name is the useful one.
        name = (dns.split(".")[0] or peer.get("HostName") or "").lower()
        known = next((registered[a] for a in [dns, name, *addresses] if a in registered), "")
        peers.append({
            "name": name, "dns": dns, "ip": addresses[0] if addresses else "",
            "os": peer.get("OS", ""), "online": bool(peer.get("Online")), "registered_as": known,
        })
    return sorted(peers, key=lambda p: p["name"])
