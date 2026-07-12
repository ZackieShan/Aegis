"""
canvas.py — the code canvas backend: generate code from a description, and edit
a code buffer in place with a natural-language instruction.

This is the "inline AI support" for the code canvas — the same idea as the doc
editor's AI, but for code. It works on a single in-memory buffer (no workspace
or git), so it's instant and stateless; the heavier file/repo path is the /code
(Aider) agent. Runs on a strong LOCAL coder (auto-picked, Qwen3-Coder-30B).

Guarded — a model hiccup returns a clear error, never raises.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# last fenced code block in a response (models often add prose around it)
_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.S)

_LANG_ALIAS = {
    "js": "javascript", "ts": "typescript", "py": "python", "sh": "bash",
    "": "", "text": "", "plaintext": "",
}


def _pick_model(owner: str, hint: Optional[str] = None) -> str:
    """A strong local coder to drive the canvas (reuses the coding-agent picker)."""
    if hint:
        return hint
    try:
        from src import coding_agent
        ep = coding_agent._pick_endpoint(owner)
        if ep:
            return ep["model"]
    except Exception:
        pass
    # last resort: any enabled model
    try:
        import json as _json
        from core.database import ModelEndpoint, SessionLocal
        db = SessionLocal()
        try:
            for e in db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all():  # noqa: E712
                for m in (_json.loads(e.cached_models) if e.cached_models else []) or []:
                    return m
        finally:
            db.close()
    except Exception:
        pass
    return ""


def _extract_code(raw: str, want_lang: str = "") -> Dict[str, str]:
    """Pull the code block + a short summary out of a model response."""
    raw = raw or ""
    blocks = _FENCE_RE.findall(raw)
    if blocks:
        # prefer the longest block (the file), not a tiny inline snippet
        lang, code = max(blocks, key=lambda b: len(b[1]))
        lang = _LANG_ALIAS.get(lang.lower(), lang.lower())
        # summary = prose after the last fence, else before the first
        after = raw.rsplit("```", 1)[-1].strip()
        summary = after or raw.split("```", 1)[0].strip()
        return {"code": code.rstrip("\n"), "language": lang or want_lang,
                "explanation": _one_line(summary)}
    # no fence — treat the whole thing as code (fallback)
    return {"code": raw.strip(), "language": want_lang, "explanation": ""}


def _one_line(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[:240]


async def _run_model(system: str, user: str, model_spec: str, owner: str,
                     max_tokens: int = 6000) -> str:
    from src.ai_interaction import _resolve_model
    from src.llm_core import llm_call_async
    url, model, headers = await asyncio.to_thread(_resolve_model, model_spec, owner=owner or None)
    return await llm_call_async(
        url, model,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        headers=headers, temperature=0.2, max_tokens=max_tokens, timeout=240,
    )


async def generate(prompt: str, language: str = "", model: str = "", owner: str = "") -> Dict[str, Any]:
    """Generate a complete program from a natural-language description."""
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "describe what to build"}
    model = _pick_model(owner, model.strip() or None)
    if not model:
        return {"ok": False, "error": "no model available — add one in Settings first."}
    lang_txt = f"{language} " if language else ""
    system = ("You are an expert programmer. Write complete, correct, runnable code. "
              "Return EXACTLY ONE fenced code block containing the full file (no ellipses, "
              "no omitted sections), then a single short sentence describing it. No other prose.")
    user = f"Write a complete {lang_txt}program for:\n\n{prompt}"
    try:
        raw = await _run_model(system, user, model, owner)
    except Exception as e:
        logger.debug(f"canvas.generate failed: {e}")
        return {"ok": False, "error": f"generation failed: {e}"}
    out = _extract_code(raw, language)
    if not out["code"]:
        return {"ok": False, "error": "the model returned no code"}
    return {"ok": True, "model": model, **out}


async def edit(code: str, instruction: str, language: str = "", model: str = "", owner: str = "") -> Dict[str, Any]:
    """Rewrite a code buffer in place per a natural-language instruction."""
    instruction = (instruction or "").strip()
    if not instruction:
        return {"ok": False, "error": "say what to change"}
    if not (code or "").strip():
        # nothing to edit yet — treat as a generate
        return await generate(instruction, language, model, owner)
    model = _pick_model(owner, model.strip() or None)
    if not model:
        return {"ok": False, "error": "no model available — add one in Settings first."}
    system = ("You are a precise code editor. Apply the user's change to their code and return "
              "EXACTLY ONE fenced code block with the COMPLETE updated file (never abbreviate or "
              "omit unchanged parts), then a single short sentence summarizing what you changed. "
              "No other prose.")
    lang = language or ""
    user = (f"Current code:\n```{lang}\n{code}\n```\n\n"
            f"Change to apply: {instruction}")
    try:
        raw = await _run_model(system, user, model, owner)
    except Exception as e:
        logger.debug(f"canvas.edit failed: {e}")
        return {"ok": False, "error": f"edit failed: {e}"}
    out = _extract_code(raw, language)
    if not out["code"]:
        return {"ok": False, "error": "the model returned no code"}
    return {"ok": True, "model": model, **out}
