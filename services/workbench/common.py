"""Shared plumbing for the workbench: the JSON store, subprocess helpers and
the environment powerful tools run in.

The agent's own shell runs with ``HOME`` pointed at Aegis's data folder (so a
stray ``rm -rf ~`` cannot reach the real home). The workbench does the
opposite on purpose: git, gh, ssh and the agent CLIs need the *user's* home —
their identity, credentials and config — so every command here runs with the
real ``HOME`` and a ``PATH`` that includes the user's tool managers.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")

_lock = threading.RLock()
_cache: Optional[Tuple[float, dict]] = None

_EMPTY = {"version": 1, "session_projects": {}, "apps": {}, "hosts": {}, "delegations": {}}


class WorkbenchError(Exception):
    """A problem the user (or the model) can act on; the message is shown as-is."""


def settings() -> dict:
    try:
        from src.settings import load_settings
        return load_settings() or {}
    except Exception:
        return {}


def enabled() -> bool:
    if os.environ.get("AEGIS_WORKBENCH_DISABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    return bool(settings().get("workbench_enabled", True))


def real_home() -> Path:
    """The account's home directory, whatever ``$HOME`` currently says."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path(os.path.expanduser("~"))


def data_dir() -> Path:
    try:
        from src.constants import DATA_DIR
        return Path(DATA_DIR)
    except Exception:
        return real_home() / ".aegis"


def store_path() -> Path:
    return data_dir() / "workbench.json"


def work_dir() -> Path:
    path = data_dir() / "workbench"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── store ──────────────────────────────────────────────────────────────────

def load() -> dict:
    """The workbench registry (projects bound to chats, apps, hosts)."""
    global _cache
    path = store_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return json.loads(json.dumps(_EMPTY))
    with _lock:
        if _cache and _cache[0] == mtime:
            return json.loads(json.dumps(_cache[1]))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        for key, default in _EMPTY.items():
            data.setdefault(key, json.loads(json.dumps(default)))
        _cache = (mtime, data)
        return json.loads(json.dumps(data))


def update(mutate: Callable[[dict], Any]) -> Any:
    """Read-modify-write under a lock; returns whatever ``mutate`` returns."""
    global _cache
    with _lock:
        data = load()
        result = mutate(data)
        path = store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        _cache = None
        return result


# ── environment + subprocess ───────────────────────────────────────────────

def tool_path() -> str:
    home = real_home()
    parts = [
        str(home / ".local/share/mise/command-wrappers/bin"),
        str(home / ".local/share/mise/shims"),
        str(home / ".local/bin"),
        str(home / ".cargo/bin"),
        "/usr/local/bin", "/usr/bin", "/bin",
    ]
    for extra in os.environ.get("PATH", "").split(os.pathsep):
        if extra and extra not in parts:
            parts.append(extra)
    return os.pathsep.join(parts)


def user_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for commands that act as the user: real HOME, full PATH,
    and the user's systemd/D-Bus session (``systemctl --user``, keyring)."""
    home = real_home()
    runtime = f"/run/user/{os.getuid()}"
    env = {
        key: value for key, value in os.environ.items()
        if key not in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "PYTHONPATH", "VIRTUAL_ENV")
    }
    env.update({
        "HOME": str(home),
        "USER": home.name,
        "LOGNAME": home.name,
        "PATH": tool_path(),
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "GIT_TERMINAL_PROMPT": "0",          # never hang waiting for a password
    })
    if extra:
        env.update(extra)
    return env


def agent_shell_env() -> Dict[str, str]:
    """Additions to the environment of the agent's own shell tool.

    That shell keeps ``HOME`` on Aegis's data folder, which also hides the
    user's git identity, GitHub login and tool managers — so a plain
    ``git commit`` or ``npm install`` fails. Point git and gh at the real
    config and put the user's tools on PATH, without moving HOME.
    """
    if not enabled():
        return {}
    home = real_home()
    env = {"PATH": tool_path(), "GIT_TERMINAL_PROMPT": "0",
           "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    git_config = next((p for p in (home / ".config/git/config", home / ".gitconfig") if p.exists()), None)
    if git_config:
        env["GIT_CONFIG_GLOBAL"] = str(git_config)
    if (home / ".config/gh").is_dir():
        env["GH_CONFIG_DIR"] = str(home / ".config/gh")
    return env


def which(name: str) -> Optional[str]:
    import shutil
    return shutil.which(name, path=tool_path())


def run(
    argv: Sequence[str],
    cwd: Optional[str] = None,
    timeout: float = 60,
    input_text: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """Run a command as the user. Returns ``(exit_code, stdout, stderr)``;
    124 on timeout, 127 when the program is missing. Never raises."""
    try:
        done = subprocess.run(
            list(argv), cwd=cwd, env=env or user_env(), input=input_text,
            capture_output=True, text=True, timeout=timeout, errors="replace",
        )
        return done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        err = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        return 124, out, (err + f"\n[timed out after {int(timeout)}s]").strip()
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not installed"
    except Exception as exc:  # permission problems, bad cwd, …
        return 1, "", f"{type(exc).__name__}: {exc}"


def clip(text: str, limit: int = 6000) -> str:
    """Keep the head and the tail of long output — the error is usually last."""
    text = (text or "").rstrip()
    if len(text) <= limit:
        return text
    head = limit // 3
    return text[:head].rstrip() + f"\n… [{len(text) - limit} characters omitted] …\n" + text[-(limit - head):].lstrip()


def valid_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise WorkbenchError(
            "Names are lower-case letters, digits, dot, dash or underscore, starting with a letter or "
            f"digit, at most 48 characters — got {name!r}."
        )
    return name


def now() -> float:
    return time.time()


def audit(kind: str, line: str) -> None:
    """Append one line to the workbench audit log (remote commands, deploys)."""
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(work_dir() / "audit.log", "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {kind} {line[:600]}\n")
    except OSError:
        pass
