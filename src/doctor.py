"""
doctor.py — capability self-check + guarded self-heal.

The idea: when a feature breaks because something in the environment is missing
(a Python package, an npx package, a service that isn't running), don't fail
silently. Each check knows how to (a) probe itself and (b) — where it's safe —
its ONE specific remedy. The UI shows the problem + cause, and a "Fix" button
runs only that allowlisted remedy, with the user's explicit click as consent.

Security model: fixes are NOT arbitrary. `apply_fix(check_id)` looks the remedy
up in this registry by id and runs exactly that — it never takes a command from
the caller. Only `pip install <pinned package>` and `npx warm <pinned package>`
remedies are auto-runnable; anything touching the system or needing judgment is
returned as guidance for the user to do themselves.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── remedy kinds ─────────────────────────────────────────────────────────────
# "pip":    pip install a pinned package into the running venv (auto-runnable)
# "npx":    warm an npx package into the cache (auto-runnable)
# "manual": guidance only — the user does it (start a service, add an API key…)


@dataclass
class Remedy:
    kind: str                 # "pip" | "npx" | "manual"
    package: str = ""         # pinned package name for pip/npx
    hint: str = ""            # human guidance (always shown)
    restart: bool = False     # does the fix need an app restart to take effect?


@dataclass
class Check:
    id: str
    label: str
    category: str
    probe: Callable[[], "CheckResult"]
    remedy: Optional[Remedy] = None


@dataclass
class CheckResult:
    ok: bool
    detail: str = ""


# ── probes ───────────────────────────────────────────────────────────────────
def _probe_ddgs() -> CheckResult:
    try:
        import ddgs  # noqa: F401
        return CheckResult(True, "ddgs package installed")
    except Exception:
        return CheckResult(False, "the `ddgs` package is not installed")


def _probe_search() -> CheckResult:
    """Does a live web search actually return results with the current provider?"""
    try:
        from services.search.providers import duckduckgo_search
    except Exception as e:
        return CheckResult(False, f"search module import failed: {e}")
    try:
        import ddgs  # noqa: F401
    except Exception:
        return CheckResult(False, "no results — the `ddgs` package is missing, so DuckDuckGo falls back to a scraper that is being rate-limited")
    try:
        res = duckduckgo_search("aegis connectivity test", count=2)
        if res:
            return CheckResult(True, f"web search returned {len(res)} result(s)")
        return CheckResult(False, "the provider returned 0 results (DuckDuckGo may be throttling this IP — add a keyed provider in Settings → Search)")
    except Exception as e:
        return CheckResult(False, f"search failed: {e}")


def _probe_ollama() -> CheckResult:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    try:
        import httpx
        r = httpx.get(f"{host}/api/tags", timeout=4)
        if r.status_code == 200:
            n = len((r.json() or {}).get("models", []))
            return CheckResult(True, f"Ollama reachable at {host} ({n} models)")
        return CheckResult(False, f"Ollama at {host} returned HTTP {r.status_code}")
    except Exception:
        return CheckResult(False, f"Ollama is not reachable at {host}")


def _probe_module(mod: str, label: str) -> CheckResult:
    try:
        __import__(mod)
        return CheckResult(True, f"{label} installed")
    except Exception:
        return CheckResult(False, f"{label} is not installed")


def _probe_npx() -> CheckResult:
    # Use the same resolver the MCP manager does — it also finds Aegis's portable
    # Node (engine/node) even when it isn't on the system PATH.
    exe = shutil.which("npx") or shutil.which("npx.cmd")
    if not exe:
        try:
            from src.builtin_mcp import _find_npx
            cand = _find_npx()
            if cand and os.path.isfile(cand):
                exe = cand
        except Exception:
            pass
    if exe:
        where = " (engine/node)" if "engine" in (exe or "").replace("\\", "/").lower() else ""
        return CheckResult(True, f"npx is available{where}")
    return CheckResult(False, "npx (Node.js) is not on PATH — npx-based MCP servers, incl. the built-in Browser, can't run")


def _probe_stt() -> CheckResult:
    """Local speech-to-text — only needs faster-whisper when provider is 'local'."""
    try:
        from src.settings import get_setting
        provider = get_setting("stt_provider", "disabled")
    except Exception:
        provider = "disabled"
    if provider != "local":
        return CheckResult(True, f"STT provider is '{provider}' (local Whisper not required)")
    try:
        import faster_whisper  # noqa: F401
        return CheckResult(True, "faster-whisper installed (local transcription ready)")
    except Exception:
        return CheckResult(False, "STT provider is 'local' but faster-whisper is not installed")


def _probe_browser() -> CheckResult:
    """Full browser-automation readiness: npx present AND a Playwright browser installed."""
    npx = _probe_npx()
    if not npx.ok:
        return npx
    try:
        from src.builtin_mcp import _find_playwright_headless_shell
        shell = _find_playwright_headless_shell()
    except Exception:
        shell = None
    if shell:
        return CheckResult(True, "Playwright MCP ready (npx + chromium headless shell)")
    return CheckResult(False, "npx is available, but no Playwright browser is installed yet — "
                              "run `npx @playwright/mcp@latest install-browser chromium`")


def _probe_coding_agent() -> CheckResult:
    """Is the local coding agent (Aider, in its isolated venv) available?"""
    try:
        from src import coding_agent
        if coding_agent.is_available():
            v = coding_agent.version()
            return CheckResult(True, f"Aider {v or ''} ready (engine/aider-venv)".replace("  ", " "))
        return CheckResult(False, "Aider isn't installed — the coding agent (/code) can't run")
    except Exception as e:
        return CheckResult(False, f"coding agent check errored: {e}")


# ── registry ─────────────────────────────────────────────────────────────────
def _checks() -> List[Check]:
    return [
        Check("search", "Web search", "core", _probe_search,
              Remedy("pip", "ddgs",
                     "If a key-free fix isn't enough, open Settings → Search and add a Brave/Tavily/Serper key (free tiers) or point at a SearXNG instance.",
                     restart=False)),
        Check("ddgs", "DuckDuckGo search backend", "core", _probe_ddgs,
              Remedy("pip", "ddgs", "Keyless DuckDuckGo search backend.", restart=False)),
        Check("ollama", "Local model server (Ollama)", "core", _probe_ollama,
              Remedy("manual", "", "Start Ollama: run `ollama serve` (launch-windows.ps1 auto-starts it if the CLI is on PATH). Install from ollama.com if missing.")),
        Check("fastembed", "Embeddings (RAG / semantic memory)", "features",
              lambda: _probe_module("fastembed", "fastembed"),
              Remedy("pip", "fastembed", "Local embedding model for RAG, semantic memory, and tool retrieval.", restart=True)),
        Check("pymupdf", "PDF viewer & forms", "features",
              lambda: _probe_module("fitz", "PyMuPDF"),
              Remedy("pip", "PyMuPDF", "Page rendering for the PDF viewer and form-filling.", restart=True)),
        Check("browser", "Browser automation (Playwright MCP)", "features", _probe_browser,
              Remedy("npx", "@playwright/mcp@latest",
                     "Warms the Playwright MCP package. If npx works but no browser is installed, also run "
                     "`npx @playwright/mcp@latest install-browser chromium`. Requires Node.js (Aegis bundles a portable one at engine/node).",
                     restart=True)),
        Check("opik", "Trace export to opik (optional)", "features",
              lambda: _probe_module("opik", "opik"),
              Remedy("pip", "opik", "Optional: exports local traces to an opik instance for richer eval/monitoring UI. Local tracing works without it.", restart=True)),
        Check("stt", "Voice input (local Whisper)", "features", _probe_stt,
              Remedy("pip", "faster-whisper", "Local speech-to-text (CPU/GPU via CTranslate2) for the mic / voice notes.", restart=True)),
        Check("coding_agent", "Coding agent (Aider)", "features", _probe_coding_agent,
              Remedy("manual", "",
                     "Aider runs in an isolated venv (kept out of Aegis's environment on purpose). "
                     "Create it: `python -m venv engine/aider-venv` then "
                     "`engine/aider-venv/Scripts/pip install aider-chat` (use `bin/pip` on macOS/Linux). "
                     "Then use `/code` in chat.")),
    ]


# Checks that hit the live network and are too slow for a snappy dashboard.
# `quick=True` skips them (the Control Center uses the fast `ddgs` import check
# as the web-search signal instead of the live `search` probe).
_SLOW_CHECK_IDS = {"search"}


def run_checks(quick: bool = False) -> List[Dict]:
    out = []
    for chk in _checks():
        if quick and chk.id in _SLOW_CHECK_IDS:
            continue
        try:
            res = chk.probe()
        except Exception as e:  # a probe must never crash the doctor
            res = CheckResult(False, f"check errored: {e}")
        rem = chk.remedy
        out.append({
            "id": chk.id,
            "label": chk.label,
            "category": chk.category,
            "ok": res.ok,
            "detail": res.detail,
            "remedy": None if res.ok or not rem else {
                "kind": rem.kind,
                "package": rem.package,
                "hint": rem.hint,
                "restart": rem.restart,
                "auto": rem.kind in ("pip", "npx"),
            },
        })
    return out


# ── guarded fix ──────────────────────────────────────────────────────────────
def apply_fix(check_id: str) -> Dict:
    """Run the ONE allowlisted remedy registered for `check_id`. Never runs a
    command supplied by the caller — the package/kind come from the registry."""
    chk = next((c for c in _checks() if c.id == check_id), None)
    if not chk or not chk.remedy:
        return {"ok": False, "error": "no remedy for this check"}
    rem = chk.remedy
    if rem.kind == "pip":
        return _run(["pip install", rem.package],
                    [sys.executable, "-m", "pip", "install", rem.package], rem)
    if rem.kind == "npx":
        exe = shutil.which("npx") or shutil.which("npx.cmd")
        if not exe:
            return {"ok": False, "error": "npx (Node.js) is not installed — install Node.js first, then retry."}
        return _run([exe, rem.package], [exe, "-y", rem.package, "--version"], rem)
    return {"ok": False, "error": "this problem has no automatic fix — follow the guidance shown."}


def _run(label_parts: List[str], cmd: List[str], rem: Remedy) -> Dict:
    logger.info("doctor: applying fix: %s", " ".join(label_parts))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "the fix timed out (300s)."}
    except Exception as e:
        return {"ok": False, "error": f"could not run the fix: {e}"}
    tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-8:]
    return {
        "ok": p.returncode == 0,
        "error": "" if p.returncode == 0 else f"the fix exited with code {p.returncode}.",
        "output": "\n".join(tail),
        "restart": rem.restart,
    }
