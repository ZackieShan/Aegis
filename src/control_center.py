"""
control_center.py — one aggregated snapshot of every Aegis capability, with a
live status and a one-click "try it" action for each.

The point: Phases 0-5 built a lot of capability reachable only via slash commands
and admin corners. This assembles it all into a single structure the Control
Center panel renders — so "what's here / does it work / how do I use it" has one
answer. Every probe is guarded: one broken source degrades to a warn, never
takes down the snapshot.

Status vocabulary: "ok" (green, working), "warn" (amber, installed but needs a
step — restart/enable/config), "off" (grey, not set up), "error" (red, broke).
Action: {"type": "command"|"panel"|"chat"|"settings"|"none", "value": ...} tells
the frontend how the "try it" button behaves.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Short cache so re-opening the panel is instant; probes (nvidia-smi, aider
# --version, ollama ping) don't need to re-run every few seconds.
_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_CACHE_TTL = 20.0


def _item(key: str, name: str, status: str, detail: str = "",
          action: Optional[Dict[str, Any]] = None, fix: str = "") -> Dict[str, Any]:
    return {"key": key, "name": name, "status": status, "detail": detail,
            "action": action or {"type": "none"}, "fix": fix}


def _cmd(value: str) -> Dict[str, Any]:
    return {"type": "command", "value": value}


def _panel(button_id: str) -> Dict[str, Any]:
    return {"type": "panel", "value": button_id}


def _chat(prompt: str) -> Dict[str, Any]:
    return {"type": "chat", "value": prompt}


def _doctor_map() -> Dict[str, Dict[str, Any]]:
    """id -> doctor check result. quick=True skips the live web-search probe so
    the dashboard opens instantly (we use the fast `ddgs` check for search)."""
    try:
        from src import doctor
        return {c["id"]: c for c in doctor.run_checks(quick=True)}
    except Exception as e:
        logger.debug(f"control_center doctor map failed: {e}")
        return {}


def _mcp_connected_ids() -> set:
    """server_ids that currently have live tools (actually connected)."""
    try:
        from src.tool_utils import get_mcp_manager
        mcp = get_mcp_manager()
        return {t.get("server_id") for t in (mcp.get_all_tools() or [])}
    except Exception:
        return set()


def _enabled_mcp_ids() -> Dict[str, bool]:
    try:
        from core.database import McpServer, SessionLocal
        db = SessionLocal()
        try:
            return {s.id: bool(s.is_enabled) for s in db.query(McpServer).all()}
        finally:
            db.close()
    except Exception:
        return {}


def _setting(key: str, default: Any = None) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


# ── capability groups ─────────────────────────────────────────────────────────
def _models_group(doc: Dict) -> Dict[str, Any]:
    items = []
    # Local model engine (llama-swap) + context tuner
    try:
        from src import engine_tuner
        # recommend=False → no per-model GGUF reads, so the dashboard stays fast.
        models = engine_tuner.list_models(recommend=False)
        vram = engine_tuner.gpu_vram_mb()
        if models:
            names = ", ".join(m["model"] for m in models[:4])
            vtxt = f" · {round(vram/1024)}GB VRAM" if vram else ""
            items.append(_item("engine", "Local model engine (llama.cpp)", "ok",
                               f"{len(models)} models{vtxt}: {names}", _cmd("/engine")))
            ctxs = ", ".join(f"{m['model'].split('-')[0]} {m['current_ctx']//1024 or m['current_ctx']}K"
                             for m in models[:3])
            items.append(_item("context", "Context auto-tuner", "ok",
                               f"windows sized to your GPU ({ctxs}) — /engine to re-tune", _cmd("/engine")))
        else:
            items.append(_item("engine", "Local model engine (llama.cpp)", "off",
                               "no llama-swap models configured", _cmd("/engine")))
    except Exception as e:
        items.append(_item("engine", "Local model engine (llama.cpp)", "error", str(e)[:80]))
    # Ollama
    oll = doc.get("ollama")
    if oll:
        items.append(_item("ollama", "Ollama server", "ok" if oll["ok"] else "off", oll["detail"]))
    return {"title": "Models & engine", "items": items}


def _automation_group(doc: Dict) -> Dict[str, Any]:
    items = []
    br = doc.get("browser") or {}
    connected = "builtin_browser" in _mcp_connected_ids()
    if not br.get("ok"):
        items.append(_item("browser", "Browser automation (Playwright)", "off",
                           br.get("detail", "not installed"), _cmd("/doctor"),
                           fix="install Node + the Playwright browser (see /doctor)"))
    elif connected:
        items.append(_item("browser", "Browser automation (Playwright)", "ok",
                           "connected — the agent can browse", _chat("Open example.com and tell me the main heading.")))
    else:
        items.append(_item("browser", "Browser automation (Playwright)", "warn",
                           "installed but not connected — restart Aegis, then enable it in Admin → MCP Servers",
                           _panel("rail-settings")))
    return {"title": "Automation", "items": items}


def _voice_group(doc: Dict) -> Dict[str, Any]:
    items = []
    stt = doc.get("stt") or {}
    provider = _setting("stt_provider", "disabled")
    if provider == "local" and stt.get("ok"):
        items.append(_item("stt", "Voice mode (speak to your PC)", "ok",
                           "hands-free: speak → the agent acts", _panel("rail-voice")))
    elif provider not in ("disabled", None):
        items.append(_item("stt", "Voice mode", "ok", f"provider: {provider}", _panel("rail-voice")))
    else:
        items.append(_item("stt", "Voice input (speech-to-text)", "off",
                           "turn on 'local' in Settings → Voice (faster-whisper)", _panel("rail-settings")))
    # TTS
    tts_provider = _setting("tts_provider", "disabled")
    if tts_provider and tts_provider != "disabled":
        items.append(_item("tts", "Voice output (text-to-speech)", "ok", f"provider: {tts_provider}"))
    else:
        items.append(_item("tts", "Voice output (text-to-speech)", "off",
                           "enable in Settings → Voice", _panel("rail-settings")))
    return {"title": "Voice", "items": items}


def _creative_group() -> Dict[str, Any]:
    items = []
    try:
        from core.database import ModelEndpoint, SessionLocal
        import json as _json
        has_img = False
        db = SessionLocal()
        try:
            for ep in db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all():  # noqa: E712
                for m in (_json.loads(ep.cached_models) if ep.cached_models else []) or []:
                    if any(k in m.lower() for k in ("image", "dall-e", "gpt-image", "flux", "sd")):
                        has_img = True
        finally:
            db.close()
        enabled = bool(_setting("image_gen_enabled", True))
        if has_img and enabled:
            items.append(_item("image", "Image generation", "ok", "local diffusion ready",
                               _chat("Generate an image of a mountain lake at sunrise, photorealistic.")))
        elif has_img and not enabled:
            items.append(_item("image", "Image generation", "warn",
                               "model present but disabled — enable in Settings", _panel("rail-settings")))
        else:
            items.append(_item("image", "Image generation", "off",
                               "no image model — drop a diffusion GGUF in models/ or add a hosted key",
                               _panel("rail-gallery")))
    except Exception as e:
        items.append(_item("image", "Image generation", "error", str(e)[:80]))
    return {"title": "Creative", "items": items}


def _coding_group(doc: Dict) -> Dict[str, Any]:
    items = []
    ca = doc.get("coding_agent") or {}
    items.append(_item("coding", "Coding agent (Aider)", "ok" if ca.get("ok") else "off",
                       ca.get("detail", ""), _cmd("/code status") if ca.get("ok") else _cmd("/doctor")))
    # Code canvas — always available (works on any enabled model)
    items.append(_item("canvas", "Code canvas", "ok",
                       "write & edit code with inline AI, then run it", _panel("rail-canvas")))
    # Repo wiki
    try:
        from src import repo_wiki
        n = len(repo_wiki.list_wikis())
        items.append(_item("wiki", "Repo → wiki", "ok",
                           f"{n} saved" if n else "turn any repo into a wiki", _cmd("/wiki list")))
    except Exception:
        pass
    return {"title": "Coding", "items": items}


def _knowledge_group(doc: Dict) -> Dict[str, Any]:
    items = []
    # Use the fast `ddgs` import check (not the live `search` probe) so the
    # dashboard stays instant; a keyless DuckDuckGo backend means search works.
    ddgs = doc.get("ddgs") or {}
    items.append(_item("search", "Web search", "ok" if ddgs.get("ok") else "warn",
                       "search backend ready" if ddgs.get("ok") else ddgs.get("detail", "no search backend"),
                       _chat("Search the web for today's top technology news.")))
    items.append(_item("research", "Deep Research", "ok",
                       "multi-source research reports", _panel("rail-research")))
    # RAG / embeddings
    fe = doc.get("fastembed") or {}
    items.append(_item("rag", "Documents & RAG", "ok" if fe.get("ok") else "off",
                       "semantic search over your files" if fe.get("ok") else fe.get("detail", ""),
                       _panel("rail-archive")))
    # Graph memory
    try:
        from src import graph_memory
        st = graph_memory.stats()
        on = graph_memory.is_enabled()
        items.append(_item("graph", "Knowledge graph memory",
                           "ok" if (on and st.get("total")) else "off",
                           f"{st.get('total', 0)} facts" if on else "off — enable in Settings, then /graph build",
                           _cmd("/graph stats")))
    except Exception:
        pass
    return {"title": "Knowledge", "items": items}


def _toolboxes_group() -> Dict[str, Any]:
    items = []
    try:
        from src.builtin_mcp import TOOLBOX_MCP_IDS
        enabled = _enabled_mcp_ids()
        live = _mcp_connected_ids()
        labels = {"osint": "OSINT — Intel & Recon", "market": "Market analysis",
                  "troubleshoot": "Network & systems", "web": "Web crawl & extract"}
        for tid in sorted(TOOLBOX_MCP_IDS):
            on = enabled.get(tid, True)
            status = "ok" if (on and tid in live) else ("off" if not on else "warn")
            items.append(_item(f"tb_{tid}", f"Toolbox: {labels.get(tid, tid)}",
                               status, "" if status == "ok" else ("disabled" if not on else "restart to connect"),
                               _cmd(f"/{tid if tid != 'web' else 'web'} ")))
    except Exception as e:
        logger.debug(f"toolboxes group failed: {e}")
    return {"title": "Toolboxes", "items": items}


def _workflow_group(doc: Dict) -> Dict[str, Any]:
    items = []
    # Recipes
    try:
        from src import recipes
        n = len(recipes.list_recipes() or [])
        items.append(_item("recipes", "Recipes (visual workflows)", "ok",
                           f"{n} saved" if n else "chain tools + models on a canvas", _panel("rail-recipes")))
    except Exception:
        pass
    # Tracing
    try:
        from src import tracing
        st = tracing.stats()
        on = tracing.is_enabled()
        items.append(_item("tracing", "Observability (traces)", "ok" if on else "off",
                           f"{st.get('total', 0)} calls recorded" if on else "off", _cmd("/traces")))
    except Exception:
        pass
    return {"title": "Workflow & observability", "items": items}


def snapshot(force: bool = False) -> Dict[str, Any]:
    """The full Control Center payload: capability groups with live status.
    Cached for a few seconds so rapid re-opens don't re-run every probe."""
    if not force and _CACHE["data"] is not None and (time.time() - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["data"]
    data = _build_snapshot()
    _CACHE["data"] = data
    _CACHE["ts"] = time.time()
    return data


def _build_snapshot() -> Dict[str, Any]:
    try:
        doc = _doctor_map()
    except Exception:
        doc = {}
    groups = []
    for fn in (
        lambda: _models_group(doc),
        lambda: _automation_group(doc),
        lambda: _coding_group(doc),
        lambda: _creative_group(),
        lambda: _voice_group(doc),
        lambda: _knowledge_group(doc),
        lambda: _toolboxes_group(),
        lambda: _workflow_group(doc),
    ):
        try:
            g = fn()
            if g and g.get("items"):
                groups.append(g)
        except Exception as e:
            logger.debug(f"control_center group failed: {e}")
    # headline counts for the panel header
    total = sum(len(g["items"]) for g in groups)
    ok = sum(1 for g in groups for i in g["items"] if i["status"] == "ok")
    return {"groups": groups, "summary": {"total": total, "ok": ok,
                                          "needs_attention": total - ok}}
