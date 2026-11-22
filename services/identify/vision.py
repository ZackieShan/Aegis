"""(prompt, image) → text with a vision model.

``src.document_processor.analyze_image_with_vl`` hard-codes "Describe this
image in detail"; identification needs its own prompts, a size-capped image,
JSON output and — above all — reasoning switched off, because a local vision
model that "thinks" first spends a minute before its first useful token.

Model choice, in order: the ``identify_model`` setting (a model picked for
this job — on a multi-model box the fastest vision-capable one), then the
general ``vision_model``, then ``vision_model_fallbacks``.

Local OpenAI-compatible endpoints (llama-swap / llama.cpp / Ollama) are
called directly so the thinking switch and JSON mode can be set per request;
anything else goes through ``llm_call`` unchanged.
"""

from __future__ import annotations

import base64
import io
import logging
import threading
from typing import List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

MAX_SIDE = 1568          # long edge sent to the model; plenty for field marks
JPEG_QUALITY = 90


class VisionUnavailable(RuntimeError):
    """No vision model is configured, enabled or answering."""


def prepare_image(path: str, max_side: int = MAX_SIDE) -> bytes:
    """Load a photo, honour its EXIF rotation, cap the long edge and return
    JPEG bytes. Phone photos are 12 MP and sideways-by-metadata; neither helps."""
    from PIL import Image, ImageOps

    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        if max(image.size) > max_side:
            image.thumbnail((max_side, max_side), Image.LANCZOS)
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY)
        return buffer.getvalue()


def _candidates(owner: Optional[str]) -> List[Tuple[str, str, dict]]:
    """``(chat_url, model_id, headers)`` to try, best first."""
    from src.ai_interaction import _resolve_model
    from src.document_processor import _load_vl_settings, _resolve_vl_model

    settings = _load_vl_settings()
    if not settings.get("vision_enabled", True):
        raise VisionUnavailable("Vision is disabled (Settings → Vision).")
    out: List[Tuple[str, str, dict]] = []
    preferred = str(settings.get("identify_model") or "").strip()
    if preferred:
        try:
            out.append(_resolve_model(preferred, owner=owner))
        except Exception as exc:
            logger.info("identify_model %r is not available (%s); using the vision model", preferred, exc)
    try:
        out.append(_resolve_vl_model(settings.get("vision_model", ""), owner=owner))
    except Exception:
        pass
    try:
        from src.endpoint_resolver import resolve_vision_fallback_candidates
        out += resolve_vision_fallback_candidates(owner=owner)
    except Exception:
        pass
    seen = set()
    unique = []
    for candidate in out:
        if not candidate or not candidate[0] or not candidate[1]:
            continue
        key = (candidate[0], candidate[1])
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    if not unique:
        raise VisionUnavailable("No vision model is configured (Settings → Vision).")
    return unique


def _is_local(url: str) -> bool:
    try:
        from src.llm_core import is_local_endpoint
        return bool(is_local_endpoint(url))
    except Exception:
        return False


def _local_call(url: str, model: str, messages: list, headers: Optional[dict], max_tokens: int,
                temperature: float, timeout: int, think: Optional[bool], json_mode: bool) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if think is not None:
        # llama.cpp reads chat_template_kwargs.enable_thinking, Ollama reads
        # `think`; each ignores the other's knob.
        payload["chat_template_kwargs"] = {"enable_thinking": bool(think)}
        payload["think"] = bool(think)
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    request_headers = {"Content-Type": "application/json"}
    if isinstance(headers, dict):
        request_headers.update({k: v for k, v in headers.items() if isinstance(v, str)})
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0), trust_env=False) as client:
        response = client.post(url, json=payload, headers=request_headers)
        if response.status_code == 400 and json_mode:
            # An engine without JSON mode: ask again in plain mode; the caller
            # parses whatever comes back.
            payload.pop("response_format", None)
            response = client.post(url, json=payload, headers=request_headers)
        response.raise_for_status()
        message = (response.json().get("choices") or [{}])[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content.strip() and (message.get("reasoning_content") or "").strip():
        raise RuntimeError("the model spent its whole budget reasoning and gave no answer")
    return content


def vision_call(
    image_jpeg: bytes,
    prompt: str,
    system: str = "",
    owner: Optional[str] = None,
    max_tokens: int = 900,
    temperature: float = 0.2,
    timeout: int = 300,
    think: Optional[bool] = False,
    json_mode: bool = False,
) -> dict:
    """Ask a vision model about one image. Returns ``{"text", "model"}``.

    ``think=False`` switches reasoning off where the engine allows it;
    ``json_mode`` asks for a JSON object (local engines enforce it). Blocking —
    call it from a worker thread.
    """
    data = base64.b64encode(image_jpeg).decode("ascii")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}},
        ],
    })
    last_error: Optional[Exception] = None
    for url, model, headers in _candidates(owner):
        try:
            if _is_local(url):
                text = _local_call(url, model, messages, headers, max_tokens, temperature,
                                   timeout, think, json_mode)
            else:
                from src.llm_core import llm_call
                text = llm_call(url, model, messages, temperature=temperature, max_tokens=max_tokens,
                                headers=headers, timeout=timeout)
            if text and text.strip():
                return {"text": text, "model": model}
            last_error = RuntimeError("empty reply")
        except Exception as exc:
            last_error = exc
            logger.warning("vision model %s failed (%s: %s); trying the next one", model, type(exc).__name__, exc)
    raise VisionUnavailable(f"The vision model did not answer: {last_error}")


def warm_up(owner: Optional[str] = None) -> None:
    """Start loading the first-choice model in the background.

    Behind llama-swap a model that is not resident takes tens of seconds to
    load; kicking that off before the classifier runs lets the two waits
    overlap. Fire-and-forget: errors are ignored, the real call follows."""
    try:
        url, model, headers = _candidates(owner)[0]
    except Exception:
        return
    if not _is_local(url):
        return

    def _ping() -> None:
        try:
            _local_call(url, model, [{"role": "user", "content": "ok"}], headers, 1, 0.0, 240, False, False)
        except Exception:
            pass

    threading.Thread(target=_ping, name="identify-warmup", daemon=True).start()
