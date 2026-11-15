"""Keep the local engines alive.

On 7/26 llama-swap, ChromaDB and ComfyUI all died and stayed dead for six
days — nothing probed them, so chat silently degraded to the tiny Ollama
fallback and vector search fell back to keywords. This watchdog closes that
gap: a single background task health-probes each engine and respawns it after
two consecutive failed probes, with exponential backoff so a crash-looping
engine doesn't spin, and a per-engine boot grace so a slow-booting child
(ComfyUI imports torch for minutes before binding its port) isn't doubled
by the next probe window.

Children are spawned fully detached (own process group, no console) with
output appended to a per-engine log in the temp dir, so a restarted engine
survives Aegis itself restarting — the exact opposite of the launcher's
PowerShell.Exiting handlers, which killed engines when the console closed.

ChromaDB is deliberately spawned via run_chroma.py: chromadb 1.5.9's console
script (chroma.exe) and ``python -m chromadb.cli.cli`` both exit silently
because the rust CLI mis-parses argv when argv[0] isn't the program name.

There is no user-facing "stop this engine" mechanism today, so any dead
engine is treated as a crash and restarted; the ``engine_watchdog`` setting
("off" to disable, re-read every cycle) is the only opt-out.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import subprocess
import tempfile
import threading
import time
from typing import Dict, Optional

from core.platform_compat import IS_WINDOWS, detached_popen_kwargs

logger = logging.getLogger(__name__)

# First probe waits out launcher-started engines' boot; llama-swap with a big
# model can take a while to answer /v1/models.
FIRST_PROBE_DELAY_S = 20
PROBE_INTERVAL_S = 30
PROBE_TIMEOUT_S = 5.0

# Restart only after two consecutive failures: a single timed-out probe under
# load (model loading pins the disk and GPU) must not bounce a live engine.
FAILURES_BEFORE_RESTART = 2
BACKOFF_BASE_S = 30
BACKOFF_CAP_S = 600
# Backoff resets only after the engine holds healthy this long — a boot that
# survives one probe and dies again is still a crash loop.
HEALTHY_RESET_S = 300

# Setting key: "off" disables the loop. Re-read every cycle so flipping it
# doesn't require an app restart.
SETTING_ENABLED = "engine_watchdog"

_watchdog_task = None
_lock = threading.Lock()
_state: Dict[str, dict] = {}
# Anchors the boot grace for launcher-started engines (sections 6b-6e spawn
# them just before uvicorn): until an engine has answered once, failed probes
# within boot_grace_s of the first cycle mean "still booting", not "dead".
_first_cycle_ts: Optional[float] = None


def _setting(key: str, default: str = "") -> str:
    try:
        from src.settings import get_user_setting, load_settings
        settings = load_settings()
        return (get_user_setting(key, "", settings.get(key, default)) or "").strip()
    except Exception:
        return default


def is_enabled() -> bool:
    """On by default — the failure it prevents (six days of silently degraded
    chat) is worse than the cost when it misfires (one redundant respawn)."""
    return _setting(SETTING_ENABLED, "on").lower() not in ("off", "false", "0", "no")


def _now() -> float:
    return time.monotonic()


# ── engine definitions ───────────────────────────────────────────────────────
def _app_root() -> str:
    from src.constants import BASE_DIR
    return os.path.abspath(BASE_DIR)


def _engine_dir() -> str:
    env = os.getenv("AEGIS_ENGINE_DIR")
    if env:
        return env
    return os.path.abspath(os.path.join(_app_root(), os.pardir, "engine"))


def _venv_python(root: str) -> str:
    if IS_WINDOWS:
        return os.path.join(root, "venv", "Scripts", "python.exe")
    return os.path.join(root, "venv", "bin", "python")


def _engine_defs() -> Dict[str, dict]:
    """Health URL + spawn argv per engine, resolved fresh each cycle so an
    engine installed after startup is picked up without a restart. Mirrors
    launch-windows.ps1 sections 6b/6d/6e exactly — same flags, same ports."""
    root = _app_root()
    engine = _engine_dir()
    swap_port = os.getenv("LLAMA_SWAP_PORT", "9090")
    chroma_port = os.getenv("CHROMADB_PORT", "8100")
    comfy_port = os.getenv("COMFYUI_PORT", "8188")

    swap_exe = os.path.join(engine, "llama-swap", "llama-swap.exe" if IS_WINDOWS else "llama-swap")
    swap_cfg = os.path.join(engine, "llama-swap.yaml")

    app_py = _venv_python(root)
    run_chroma = os.path.join(root, "run_chroma.py")
    # A remote CHROMADB_HOST means the server isn't ours to restart.
    chroma_is_ours = os.getenv("CHROMADB_HOST", "localhost") in ("localhost", "127.0.0.1")

    comfy_py = _venv_python(os.path.join(engine, "comfyui"))
    comfy_main = os.path.join(engine, "comfyui", "ComfyUI", "main.py")

    # boot_grace_s: how long a freshly spawned (or launcher-started) child may
    # stay unresponsive before failed probes count toward a respawn. ComfyUI
    # cold-boots through torch + custom-node imports for minutes before its
    # port binds; respawning inside that window stacks duplicate processes.
    return {
        "llama-swap": {
            "health_url": f"http://127.0.0.1:{swap_port}/v1/models",
            "argv": [swap_exe, "-config", swap_cfg, "-watch-config",
                     "-listen", f"127.0.0.1:{swap_port}"],
            "cwd": os.path.dirname(swap_exe),
            "installed": os.path.isfile(swap_exe) and os.path.isfile(swap_cfg),
            "boot_grace_s": 30,
        },
        "chromadb": {
            "health_url": f"http://127.0.0.1:{chroma_port}/api/v2/heartbeat",
            # MUST go through run_chroma.py — see module docstring for why the
            # chroma.exe CLI cannot be used.
            "argv": [app_py, run_chroma, "--host", "127.0.0.1", "--port", chroma_port,
                     "--path", os.path.join(root, "data", "chroma")],
            "cwd": root,
            "installed": chroma_is_ours and os.path.isfile(app_py) and os.path.isfile(run_chroma),
            "boot_grace_s": 30,
        },
        "comfyui": {
            "health_url": f"http://127.0.0.1:{comfy_port}/system_stats",
            "argv": [comfy_py, comfy_main, "--listen", "127.0.0.1",
                     "--port", comfy_port, "--disable-auto-launch"],
            "cwd": os.path.dirname(comfy_main),
            "installed": os.path.isfile(comfy_py) and os.path.isfile(comfy_main),
            "boot_grace_s": 180,
        },
    }


# ── state / status ───────────────────────────────────────────────────────────
def _blank_state(installed: bool = False) -> dict:
    return {
        "installed": installed,
        "healthy": None,          # None until first probe
        "last_probe_ts": None,
        "consecutive_failures": 0,
        "restarts": 0,
        "last_restart_ts": None,
        "backoff_seconds": 0,     # 0 = next restart is not gated
        "_healthy_since": None,
        "_was_healthy": False,    # once True, boot-at-launch grace no longer applies
    }


def _ensure_state(name: str) -> dict:
    with _lock:
        if name not in _state:
            _state[name] = _blank_state()
        return _state[name]


def get_status() -> Dict[str, dict]:
    """Per-engine snapshot for a status endpoint (underscore keys are
    internal and omitted). Timestamps are time.monotonic() internally, so
    they're exposed as ages — an epoch-looking *_ts field invites wall-clock
    math on the consumer side."""
    now = _now()
    with _lock:
        out: Dict[str, dict] = {}
        for name, st in _state.items():
            snap = {k: copy.copy(v) for k, v in st.items() if not k.startswith("_")}
            for ts_key, age_key in (("last_probe_ts", "seconds_since_probe"),
                                    ("last_restart_ts", "seconds_since_restart")):
                ts = snap.pop(ts_key)
                snap[age_key] = None if ts is None else round(now - ts, 1)
            out[name] = snap
        return out


def _reset_for_tests() -> None:
    global _watchdog_task, _first_cycle_ts
    with _lock:
        _state.clear()
    _watchdog_task = None
    _first_cycle_ts = None


# ── probe / spawn ────────────────────────────────────────────────────────────
async def _probe(url: str) -> bool:
    """Any HTTP response counts as alive — a 500 is a sick engine, not a dead
    one, and restarting mid-request would make it worse."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            await client.get(url)
        return True
    except Exception:
        return False


def _spawn(name: str, spec: dict) -> bool:
    """Popen the engine fully detached (blocking — run via to_thread).

    Detached + own process group so the child outlives Aegis restarts and
    console closes; stdout/stderr append to a temp-dir log because a detached
    child on Windows has no console to inherit."""
    log_path = os.path.join(tempfile.gettempdir(), f"aegis-{name}-watchdog.log")
    kwargs = detached_popen_kwargs()
    if "creationflags" in kwargs:
        kwargs["creationflags"] |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                spec["argv"], cwd=spec.get("cwd") or None,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)
    except OSError as e:
        logger.warning("engine %s: spawn failed: %s", name, e)
        return False
    # Brief liveness peek only — never wait on a detached child.
    time.sleep(1.5)
    if proc.poll() is not None:
        logger.warning("engine %s exited immediately (code %s) — see %s",
                       name, proc.returncode, log_path)
        return False
    logger.info("engine %s spawned (pid %s, log %s)", name, proc.pid, log_path)
    return True


# ── the loop ─────────────────────────────────────────────────────────────────
async def _run_cycle() -> None:
    """One probe pass over all engines."""
    global _first_cycle_ts
    if _first_cycle_ts is None:
        _first_cycle_ts = _now()
    for name, spec in _engine_defs().items():
        st = _ensure_state(name)
        with _lock:
            st["installed"] = spec["installed"]
        if not spec["installed"]:
            continue

        ok = await _probe(spec["health_url"])
        now = _now()
        with _lock:
            st["last_probe_ts"] = now
            st["healthy"] = ok
            if ok:
                st["consecutive_failures"] = 0
                st["_was_healthy"] = True
                if st["_healthy_since"] is None:
                    st["_healthy_since"] = now
                elif st["backoff_seconds"] and now - st["_healthy_since"] >= HEALTHY_RESET_S:
                    st["backoff_seconds"] = 0
                continue
            st["_healthy_since"] = None
            st["consecutive_failures"] += 1
            failures = st["consecutive_failures"]
            boot_grace = spec.get("boot_grace_s", 0)
            if st["last_restart_ts"] is not None:
                # Our own child may still be booting: gate on the larger of
                # backoff and boot grace, or every backoff expiry would stack
                # a duplicate onto a child that hasn't bound its port yet.
                gated = now - st["last_restart_ts"] < max(st["backoff_seconds"], boot_grace)
            elif not st["_was_healthy"]:
                # Never answered yet: likely the launcher's own child still
                # booting (it spawns engines just before uvicorn starts).
                gated = now - _first_cycle_ts < boot_grace
            else:
                gated = False
            due = failures >= FAILURES_BEFORE_RESTART and not gated
        if not due:
            continue

        logger.warning("engine %s: %d consecutive failed health probes (%s) — restarting",
                       name, failures, spec["health_url"])
        await asyncio.to_thread(_spawn, name, spec)
        with _lock:
            st["restarts"] += 1
            st["last_restart_ts"] = _now()
            # The fresh child is booting, not failing: clear the streak and
            # the verdict so a respawn needs two new consecutive failures on
            # top of the boot-grace gate — otherwise only the FIRST restart
            # gets the two-failure protection and each backoff expiry fires
            # on a single failed probe.
            st["consecutive_failures"] = 0
            st["healthy"] = None
            # 30 → 60 → 120 … cap 600; counts even when the spawn itself
            # failed, so a broken binary can't hot-loop either.
            st["backoff_seconds"] = min(max(BACKOFF_BASE_S, st["backoff_seconds"] * 2),
                                        BACKOFF_CAP_S)


async def _watchdog_loop() -> None:
    await asyncio.sleep(FIRST_PROBE_DELAY_S)
    while True:
        if is_enabled():
            try:
                await _run_cycle()
            except Exception as e:
                logger.warning("engine watchdog cycle error: %s", e)
        await asyncio.sleep(PROBE_INTERVAL_S)


def start_engine_watchdog():
    """Idempotent — start the engine watchdog loop."""
    global _watchdog_task
    if _watchdog_task and not _watchdog_task.done():
        return _watchdog_task
    _watchdog_task = asyncio.create_task(_watchdog_loop())
    logger.info("Engine watchdog started (first probe in %ds, then every %ds)",
                FIRST_PROBE_DELAY_S, PROBE_INTERVAL_S)
    return _watchdog_task
