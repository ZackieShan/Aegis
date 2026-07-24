"""Managed subprocess for the vendored Organizer app (photo / cinema / music).

Aegis runs the Organizer's zero-dependency stdlib HTTP server as a
loopback-only child process on 127.0.0.1:<port> and reverse-proxies to it
behind auth (see routes/organizer_routes.py). This module owns that child's
lifecycle: start-once (or adopt an already-running one, so multiple uvicorn
workers don't double-spawn), restart-on-crash with capped backoff, a health
check, and a clean shutdown. Runtime data (DBs, thumbs, undo logs) is
redirected to <Aegis DATA_DIR>/organizer via the ORGANIZER_DATA_DIR env var
so the vendored code directory stays clean.

The Organizer child has NO auth of its own and binds loopback only; every
external request must arrive through the authenticated Aegis proxy.
"""
from __future__ import annotations

import os
import sys
import threading
import subprocess
import urllib.request
import urllib.error

try:
    from src.constants import DATA_DIR as _AEGIS_DATA_DIR
except Exception:  # pragma: no cover - defensive fallback
    _AEGIS_DATA_DIR = os.path.join(os.path.expanduser("~"), ".aegis")

try:
    from core.platform_compat import kill_process_tree, pid_alive
except Exception:  # pragma: no cover - defensive fallback
    def pid_alive(pid):  # type: ignore
        return False

    def kill_process_tree(pid):  # type: ignore
        pass

# .../aegis/aegis/src/organizer_bridge.py -> repo root .../aegis
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORGANIZER_HOME = os.environ.get("ORGANIZER_HOME") \
    or os.path.join(_REPO_ROOT, "organizer")
if not os.environ.get("ORGANIZER_HOME") and not os.path.isdir(ORGANIZER_HOME):
    # public-repo layout: the app lives at the repo root with organizer/
    # directly inside it (dev layout keeps organizer as a sibling of the
    # app package instead)
    _alt = os.path.join(_APP_ROOT, "organizer")
    if os.path.isdir(_alt):
        ORGANIZER_HOME = _alt
ORGANIZER_HOST = "127.0.0.1"
# Dedicated port (not the Organizer's standalone default of 7100) so the
# Aegis-managed child never collides with a separately-launched standalone
# instance. Override with ORGANIZER_PORT if needed.
ORGANIZER_PORT = int(os.environ.get("ORGANIZER_PORT") or "7126")
ORGANIZER_DATA_DIR = os.path.join(_AEGIS_DATA_DIR, "organizer")
BASE_URL = f"http://{ORGANIZER_HOST}:{ORGANIZER_PORT}"

_CREATE_NO_WINDOW = 0x08000000  # Windows: no console window for the child

_lock = threading.Lock()
_proc: "subprocess.Popen | None" = None
_monitor: "threading.Thread | None" = None
_stopping = False
_log_fh = None


def _log_path() -> str:
    return os.path.join(ORGANIZER_DATA_DIR, "organizer_server.log")


def health(base: str = BASE_URL, timeout: float = 1.0) -> bool:
    """True if the Organizer server answers its status endpoint."""
    try:
        with urllib.request.urlopen(
                base + "/api/scan/status", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _spawn() -> "subprocess.Popen":
    global _proc, _log_fh
    os.makedirs(ORGANIZER_DATA_DIR, exist_ok=True)
    env = dict(os.environ)
    env["ORGANIZER_DATA_DIR"] = ORGANIZER_DATA_DIR
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    server_py = os.path.join(ORGANIZER_HOME, "server.py")
    creationflags = _CREATE_NO_WINDOW if os.name == "nt" else 0
    # Close a prior handle before reopening so crash-restarts don't leak fds.
    if _log_fh is not None:
        try:
            _log_fh.close()
        except Exception:
            pass
    _log_fh = open(_log_path(), "ab", buffering=0)
    _proc = subprocess.Popen(
        [sys.executable, server_py,
         "--host", ORGANIZER_HOST, "--port", str(ORGANIZER_PORT)],
        cwd=ORGANIZER_HOME, env=env,
        stdout=_log_fh, stderr=_log_fh, stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    return _proc


def _monitor_loop() -> None:
    """Restart the child if it dies unexpectedly, with capped backoff."""
    import time as _time
    backoff = 1.0
    while not _stopping:
        with _lock:
            proc = _proc
        if proc is None:
            return
        proc.wait()
        if _stopping:
            return
        started = _time.monotonic()
        _time.sleep(backoff)
        with _lock:
            if _stopping:
                return
            try:
                _spawn()
            except Exception:
                pass
        # If the previous instance had stayed up a while, reset backoff.
        backoff = 1.0 if (_time.monotonic() - started) > 60 \
            else min(backoff * 2, 30.0)


def ensure_started(wait: float = 8.0) -> bool:
    """Start the Organizer child if it isn't already running/healthy.

    Idempotent and multi-worker safe: if something already answers on the
    port (another uvicorn worker, or a manually-started standalone server),
    we adopt it instead of spawning a competitor. Returns True if healthy
    within `wait` seconds.
    """
    global _monitor, _stopping
    import time as _time
    with _lock:
        _stopping = False
        ours_running = _proc is not None and _proc.poll() is None
        if not ours_running and not health(timeout=0.5):
            _spawn()
            if _monitor is None or not _monitor.is_alive():
                _monitor = threading.Thread(
                    target=_monitor_loop, name="organizer-bridge",
                    daemon=True)
                _monitor.start()
    deadline = _time.monotonic() + wait
    while _time.monotonic() < deadline:
        if health(timeout=1.0):
            return True
        _time.sleep(0.4)
    return health(timeout=1.0)


def stop() -> None:
    """Terminate the child (called on Aegis shutdown)."""
    global _stopping, _proc, _log_fh
    _stopping = True
    with _lock:
        proc = _proc
        _proc = None
    if proc is not None and proc.poll() is None:
        try:
            kill_process_tree(proc.pid)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    if _log_fh is not None:
        try:
            _log_fh.close()
        except Exception:
            pass
        _log_fh = None
