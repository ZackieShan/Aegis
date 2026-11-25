"""Who does the translating.

The dedicated engine is a translation model (TranslateGemma) behind the local
model server, addressed by an alias like any other model there. It is tried
first. When it is not installed, or the server does not answer, the ordinary
chat model is asked instead with the same words — slower to load and less
even across languages, but it means the feature works on any install.

``AEGIS_TRANSLATE_URL``   the engine's chat-completions address (default: the
                          local model server on ``LLAMA_SWAP_PORT``, 9090)
setting ``translate_model``   its alias there (default ``translate``); an
                          empty string switches the dedicated engine off
"""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

DEFAULT_ALIAS = "translate"
_RETRY_AFTER_S = 300.0            # how long a missing engine is left alone
_missing_until = 0.0


class EngineError(Exception):
    """No model could be asked, in words for the person."""


def url() -> str:
    return (os.environ.get("AEGIS_TRANSLATE_URL", "").strip()
            or f"http://127.0.0.1:{os.getenv('LLAMA_SWAP_PORT', '9090')}/v1/chat/completions")


def alias() -> str:
    try:
        from src.settings import load_settings
        value = (load_settings() or {}).get("translate_model")
    except Exception:
        value = None
    return DEFAULT_ALIAS if value is None else str(value).strip()


def reset() -> None:
    global _missing_until
    _missing_until = 0.0


def dedicated_known_missing() -> bool:
    return not alias() or time.monotonic() < _missing_until


async def _dedicated(messages: List[dict], max_tokens: int, timeout: float) -> Optional[str]:
    """The dedicated engine's answer, or None when there is no such engine
    here (so the caller can use the chat model). A slow first answer is
    normal: the model server loads the model on demand."""
    global _missing_until
    model = alias()
    if not model or time.monotonic() < _missing_until:
        return None
    payload = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens, "stream": False}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=4.0), trust_env=False) as client:
            response = await client.post(url(), json=payload)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _missing_until = time.monotonic() + 30.0          # the server itself is away: look again soon
        return None
    except httpx.HTTPError as exc:
        logger.warning("translate: the engine did not answer (%s)", type(exc).__name__)
        return None
    if response.status_code in (400, 404) and any(word in response.text.lower() for word in ("model", "not found", "no such")):
        _missing_until = time.monotonic() + _RETRY_AFTER_S     # not installed here
        logger.info("translate: no model called %r on the model server; using the chat model", model)
        return None
    if response.status_code != 200:
        logger.warning("translate: the engine answered %s", response.status_code)
        return None
    try:
        return str(response.json()["choices"][0]["message"]["content"] or "")
    except (ValueError, KeyError, IndexError, TypeError):
        return None


async def ask(messages: List[dict], max_tokens: int = 1200, timeout: float = 180.0, owner: Optional[str] = None,
              needs_sight: bool = False) -> Tuple[str, str]:
    """``(the answer, who gave it)`` — ``"translator"`` for the dedicated
    engine, else the name of the chat model that stood in. ``needs_sight``:
    the message carries a picture, which only the dedicated engine is asked
    to read."""
    answer = await _dedicated(messages, max_tokens, timeout)
    if answer is not None and answer.strip():
        return answer, "translator"
    if needs_sight:
        raise EngineError("Reading a picture needs the translation model, and it is not answering on this server.")
    try:
        from src.llm_core import llm_call_async_with_fallback
        from src.task_endpoint import resolve_task_candidates

        candidates = resolve_task_candidates(owner=owner)
        if not candidates:
            raise EngineError("No model is set up to translate with.")
        text = await llm_call_async_with_fallback(candidates, messages, temperature=0.1, max_tokens=max_tokens,
                                                  timeout=int(timeout))
        return str(text or ""), str(candidates[0][1])
    except EngineError:
        raise
    except Exception as exc:
        raise EngineError(f"No model answered ({type(exc).__name__}).")
