"""
coding_agent.py — local coding agent (Aider) wrapper for Aegis (Phase 3).

Drives Aider against the local llama-swap OpenAI-compatible endpoint, scoped to
a workspace directory, and streams its output. Git-native: inside a git repo
Aider auto-commits every edit (so changes are revertible with `git revert`);
outside one it edits files in place (`--no-git`).

Isolated on purpose. Aider pulls a large dependency tree (litellm, tree-sitter,
…) that could clash with Aegis's pinned libs, so it lives in its own virtualenv
(engine/aider-venv, OUTSIDE the repo) and Aegis shells out to it. The same
pattern as the llama.cpp / llama-swap engine binaries: heavy third-party pieces
stay out of the app's environment and out of the git export.

Guarded everywhere — a broken run must never crash the server. Safety:
  * the task and file paths are passed as argv elements (never a shell string),
    so there is no shell-injection surface;
  * the workspace must be an existing directory, and if AEGIS_CODE_ROOT is set
    it must live inside that root (a hard fence for locking the agent down).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import threading
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from src.constants import BASE_DIR

logger = logging.getLogger(__name__)

# Aider prints one of these per file it rewrites — the cheapest reliable signal
# of what actually changed.
_APPLIED_RE = re.compile(r"Applied edit to (.+?)\s*$", re.M)

# Terminal / prompt-toolkit chatter that only appears when a TTY is (mis)detected
# — never useful in a captured run, so we drop it from the surfaced output.
_NOISE = (
    "Can't initialize prompt toolkit",
    "Terminal does not support",
    "Windows console",
    "winpty",
    "Cygwin",
    "https://aider.chat/HISTORY",
)

# Coder-tuned models first, then strong general tool-callers. Used to auto-pick
# the best available model when the caller doesn't name one.
_CODER_PREFS = (
    "qwen3-coder", "qwen2.5-coder", "qwen-coder", "deepseek-coder",
    "codestral", "codellama", "starcoder", "coder",
    "qwen3", "qwen", "deepseek", "llama-3", "llama3", "mistral", "gemma",
)


# ── engine (isolated aider venv) discovery ────────────────────────────────────
def _engine_dir() -> str:
    env = os.getenv("AEGIS_ENGINE_DIR")
    if env:
        return env
    # engine/ sits alongside the repo (…/aegis/engine, repo is …/aegis/aegis)
    return os.path.abspath(os.path.join(BASE_DIR, os.pardir, "engine"))


def _aider_python() -> Optional[str]:
    """Path to the python inside the isolated aider venv, or None if absent."""
    env = os.getenv("AIDER_PYTHON")
    if env and os.path.exists(env):
        return env
    base = os.path.join(_engine_dir(), "aider-venv")
    for cand in (os.path.join(base, "Scripts", "python.exe"),   # Windows
                 os.path.join(base, "bin", "python")):           # POSIX
        if os.path.exists(cand):
            return cand
    return None


def is_available() -> bool:
    return _aider_python() is not None


def version() -> Optional[str]:
    py = _aider_python()
    if not py:
        return None
    try:
        out = subprocess.run([py, "-m", "aider", "--version"],
                             capture_output=True, text=True, timeout=30)
        for line in reversed((out.stdout or "").splitlines()):
            line = line.strip()
            if line and any(ch.isdigit() for ch in line):
                return line.split()[-1]
    except Exception as e:
        logger.debug(f"aider version probe failed: {e}")
    return None


# ── endpoint / model selection ────────────────────────────────────────────────
def _is_local(base: str) -> bool:
    b = (base or "").lower()
    return any(h in b for h in ("127.0.0.1", "localhost", "0.0.0.0", "::1"))


def _aider_base(base: str) -> str:
    """The OpenAI-style base Aider/litellm expects. Ollama and other servers
    store a bare host (…:11434); their OpenAI-compat API lives at /v1. Append
    /v1 only when the path is empty, leaving explicit paths (…/v1, custom
    prefixes) untouched — same rule as endpoint_resolver.build_models_url."""
    from urllib.parse import urlparse
    base = (base or "").rstrip("/")
    try:
        if urlparse(base).path in ("", "/"):
            return base + "/v1"
    except Exception:
        pass
    return base


def _list_candidates(owner: str = "") -> List[Dict[str, Any]]:
    """Every (endpoint, model) pair we could drive Aider with — local first."""
    out: List[Dict[str, Any]] = []
    try:
        import json as _json

        from core.database import ModelEndpoint, SessionLocal
        from src.endpoint_resolver import resolve_endpoint_runtime
        db = SessionLocal()
        try:
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
            if owner:
                try:
                    from src.auth_helpers import owner_filter
                    q = owner_filter(q, ModelEndpoint, owner)
                except Exception:
                    pass
            for ep in q.all():
                try:
                    base, api_key = resolve_endpoint_runtime(ep, owner=owner or None)
                except Exception:
                    continue
                if not base:
                    continue
                for m in (_json.loads(ep.cached_models) if ep.cached_models else []) or []:
                    out.append({
                        "endpoint": ep.name, "model": m, "base_url": base,
                        "api_key": api_key or "dummy", "local": _is_local(base),
                        "supports_tools": bool(ep.supports_tools),
                    })
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"coding_agent candidate scan failed: {e}")
    return out


def _rank(c: Dict[str, Any]) -> Tuple[int, int, int]:
    m = (c.get("model") or "").lower()
    pref = len(_CODER_PREFS)
    for i, p in enumerate(_CODER_PREFS):
        if p in m:
            pref = i
            break
    # local first → coder-ness → declared tool support
    return (0 if c.get("local") else 1, pref, 0 if c.get("supports_tools") else 1)


def list_models(owner: str = "") -> List[Dict[str, Any]]:
    """Public candidate list for the UI/status (keys stripped)."""
    cands = sorted(_list_candidates(owner), key=_rank)
    return [{"endpoint": c["endpoint"], "model": c["model"],
             "local": c["local"], "supports_tools": c["supports_tools"]} for c in cands]


def _pick_endpoint(owner: str = "", model_hint: Optional[str] = None) -> Optional[Dict[str, Any]]:
    cands = _list_candidates(owner)
    if not cands:
        return None
    if model_hint:
        h = model_hint.strip().lower()
        pool = [c for c in cands if c["model"].lower() == h] or \
               [c for c in cands if h in c["model"].lower()]
        if pool:
            return sorted(pool, key=_rank)[0]
    return sorted(cands, key=_rank)[0]


# ── workspace safety + git detection ──────────────────────────────────────────
def _validate_workspace(workspace: str) -> str:
    if not workspace or not str(workspace).strip():
        raise ValueError("workspace is required")
    ws = os.path.abspath(os.path.expanduser(str(workspace).strip()))
    if not os.path.isdir(ws):
        raise ValueError(f"workspace is not a directory: {ws}")
    root = os.getenv("AEGIS_CODE_ROOT")
    if root:
        root_abs = os.path.abspath(os.path.expanduser(root))
        try:
            inside = os.path.commonpath([root_abs, ws]) == root_abs
        except ValueError:  # different drives on Windows
            inside = False
        if not inside:
            raise ValueError(f"workspace must be inside AEGIS_CODE_ROOT ({root_abs})")
    return ws


def _in_git_repo(ws: str) -> bool:
    p = ws
    while True:
        if os.path.isdir(os.path.join(p, ".git")):
            return True
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent


def _clean(out: str) -> str:
    keep = [ln for ln in (out or "").splitlines() if not any(n in ln for n in _NOISE)]
    return "\n".join(keep).strip()


# ── command assembly ──────────────────────────────────────────────────────────
def _prepare(workspace: str, task: str, model: Optional[str], files: Optional[List[str]],
             owner: str, use_git: Optional[bool]) -> Tuple[List[str], str, Dict[str, str], Dict[str, Any], bool]:
    """Validate inputs and build (argv, cwd, env, endpoint, use_git). Raises ValueError."""
    py = _aider_python()
    if not py:
        raise ValueError("Aider is not installed. Expected the isolated venv at engine/aider-venv "
                         "(create it and `pip install aider-chat`).")
    if not (task or "").strip():
        raise ValueError("task is required")
    ws = _validate_workspace(workspace)
    ep = _pick_endpoint(owner, model)
    if not ep:
        raise ValueError("No enabled model endpoint found. Add one in Settings first.")
    if use_git is None:
        use_git = _in_git_repo(ws)

    cmd = [py, "-m", "aider",
           "--model", f"openai/{ep['model']}",
           "--yes-always", "--no-check-update", "--no-analytics",
           "--no-show-model-warnings", "--no-pretty", "--no-stream",
           "--encoding", "utf-8"]
    if not use_git:
        cmd.append("--no-git")
    cmd += ["--message", task]
    for f in (files or []):
        f = str(f).strip()
        if f:
            cmd.append(f)

    env = dict(os.environ)
    env["OPENAI_API_BASE"] = _aider_base(ep["base_url"])
    env["OPENAI_API_KEY"] = ep["api_key"] or "dummy"
    env["AIDER_ANALYTICS"] = "false"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return cmd, ws, env, ep, use_git


def _trace(result: Dict[str, Any], task: str) -> None:
    try:
        from src import tracing
        tracing.record(
            kind="code",
            model=result.get("model") or "",
            endpoint=result.get("endpoint") or "",
            latency_ms=int((result.get("seconds") or 0) * 1000),
            tool_calls=result.get("changed") or [],
            workload="coding",
            error="" if result.get("ok") else (result.get("output") or result.get("error") or "")[:300],
            prompt=task,
            response=(result.get("output") or "")[:4000],
        )
    except Exception:
        pass


# ── run (blocking-collect, used by the JSON route + tests) ────────────────────
async def run_collect(workspace: str, task: str, model: Optional[str] = None,
                      files: Optional[List[str]] = None, owner: str = "",
                      use_git: Optional[bool] = None, timeout: int = 600) -> Dict[str, Any]:
    t0 = time.time()
    try:
        cmd, ws, env, ep, use_git = _prepare(workspace, task, model, files, owner, use_git)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    def _run() -> Tuple[int, str, bool]:
        p = subprocess.Popen(cmd, cwd=ws, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")
        try:
            out, _ = p.communicate(timeout=timeout)
            return (p.returncode, out or "", False)
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
            return (p.returncode if p.returncode is not None else -1, out or "", True)

    try:
        rc, raw, timed_out = await asyncio.to_thread(_run)
    except Exception as e:
        logger.debug(f"aider run failed: {e}")
        return {"ok": False, "error": f"failed to run aider: {e}"}

    changed = sorted(set(_APPLIED_RE.findall(raw)))
    result = {
        "ok": rc == 0 and not timed_out,
        "returncode": rc,
        "timed_out": timed_out,
        "output": _clean(raw),
        "changed": changed,
        "model": ep["model"],
        "endpoint": ep["endpoint"],
        "workspace": ws,
        "used_git": use_git,
        "seconds": round(time.time() - t0, 1),
    }
    if timed_out:
        result["error"] = f"timed out after {timeout}s"
    _trace(result, task)
    return result


# ── run (streaming, used by the SSE route) ────────────────────────────────────
async def run(workspace: str, task: str, model: Optional[str] = None,
              files: Optional[List[str]] = None, owner: str = "",
              use_git: Optional[bool] = None, timeout: int = 600
              ) -> AsyncGenerator[Dict[str, Any], None]:
    t0 = time.time()
    try:
        cmd, ws, env, ep, use_git = _prepare(workspace, task, model, files, owner, use_git)
    except ValueError as e:
        yield {"type": "done", "ok": False, "error": str(e)}
        return

    yield {"type": "meta", "model": ep["model"], "endpoint": ep["endpoint"],
           "workspace": ws, "used_git": use_git}

    loop = asyncio.get_event_loop()
    q: "asyncio.Queue[Tuple[str, Any]]" = asyncio.Queue()

    def _worker() -> None:
        try:
            p = subprocess.Popen(cmd, cwd=ws, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace")
            for line in p.stdout:  # type: ignore[union-attr]
                loop.call_soon_threadsafe(q.put_nowait, ("line", line.rstrip("\n")))
            p.wait()
            loop.call_soon_threadsafe(q.put_nowait, ("done", p.returncode))
        except Exception as e:  # pragma: no cover - defensive
            loop.call_soon_threadsafe(q.put_nowait, ("error", str(e)))

    threading.Thread(target=_worker, daemon=True).start()

    changed: List[str] = []
    output_lines: List[str] = []
    while True:
        try:
            kind, val = await asyncio.wait_for(q.get(), timeout=timeout)
        except asyncio.TimeoutError:
            yield {"type": "done", "ok": False, "error": f"timed out after {timeout}s",
                   "changed": sorted(set(changed))}
            return
        if kind == "line":
            if any(n in val for n in _NOISE):
                continue
            m = _APPLIED_RE.search(val)
            if m:
                changed.append(m.group(1))
            output_lines.append(val)
            yield {"type": "line", "text": val}
        elif kind == "error":
            res = {"ok": False, "error": val, "changed": sorted(set(changed)),
                   "model": ep["model"], "endpoint": ep["endpoint"],
                   "seconds": round(time.time() - t0, 1), "output": "\n".join(output_lines)}
            _trace(res, task)
            yield {"type": "done", **res}
            return
        else:  # done
            res = {"ok": val == 0, "returncode": val, "changed": sorted(set(changed)),
                   "model": ep["model"], "endpoint": ep["endpoint"],
                   "seconds": round(time.time() - t0, 1), "output": "\n".join(output_lines)}
            _trace(res, task)
            yield {"type": "done", **res}
            return
