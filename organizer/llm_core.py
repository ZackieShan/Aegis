#!/usr/bin/env python3
"""Shared local-LLM helpers for the organizer's Phase-4 assist features.

Thin layer over an OpenAI-compatible server (Aegis serves its local models
through llama-swap at http://127.0.0.1:9090/v1). Reuses llm_assist for config
loading, the injectable fetcher seam (so everything is testable offline), JSON
extraction, and the OpenAI response reader.

Roles map to specific wired models so callers don't hardcode names:
  * TEXT_MODEL    — chat-lite-cpu: a zero-VRAM CPU model that NEVER evicts a
                    GPU render. Use for background/batch narration & summaries.
  * SMART_MODEL   — a stronger text model for harder reasoning/classification.
  * SQL_MODEL     — a coder model for text-to-SQL.
  * VISION_MODEL  — qwen-vl: multimodal, for image captioning/tagging (GPU).

Every call is total-failure-safe: any network/parse problem returns None (or a
falsy value) instead of raising, matching llm_assist's contract.
"""
import base64
import io
import json
import os

import llm_assist

# Role → wired llama-swap model id. Overridable via env for tuning.
TEXT_MODEL = os.environ.get("ORGANIZER_LLM_TEXT") or "chat-lite-cpu"
SMART_MODEL = os.environ.get("ORGANIZER_LLM_SMART") or "qwen2.5-14b-aclarc"
SQL_MODEL = os.environ.get("ORGANIZER_LLM_SQL") or "qwen3-coder-30b"
VISION_MODEL = os.environ.get("ORGANIZER_LLM_VISION") or "qwen-vl"


def _resolve(cfg, fetcher):
    return (cfg or llm_assist.load_config()), (fetcher or llm_assist._http_json)


def model_available(model, cfg=None, fetcher=None):
    """True if `model` is listed by the server."""
    cfg, fetcher = _resolve(cfg, fetcher)
    names = llm_assist.list_models(cfg, fetcher)
    return any(n == model or n.split(":")[0] == model.split(":")[0]
               for n in names)


def chat(messages, model=TEXT_MODEL, temperature=0.2, max_tokens=800,
         cfg=None, fetcher=None, response_format=None):
    """One OpenAI chat completion. Returns assistant text (with any
    <think> block stripped) or None on any failure."""
    cfg, fetcher = _resolve(cfg, fetcher)
    if cfg.get("api") != "openai":
        # Phase-4 features assume the OpenAI/llama-swap surface.
        return None
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        payload["response_format"] = response_format
    try:
        data = fetcher(cfg["endpoint"] + "/chat/completions", payload,
                       timeout=cfg["timeoutMs"] / 1000)
    except Exception:
        return None
    return llm_assist._openai_content(data)


def chat_json(messages, model=TEXT_MODEL, temperature=0.1, max_tokens=800,
              cfg=None, fetcher=None):
    """chat() whose reply is parsed as a JSON object (first balanced object
    found anywhere in the text). Returns a dict or None."""
    text = chat(messages, model=model, temperature=temperature,
                max_tokens=max_tokens, cfg=cfg, fetcher=fetcher)
    return llm_assist._extract_json(text)


def encode_image(path, long_edge=768, quality=85):
    """Downscaled JPEG data-URI payload for a local image, base64-encoded.
    Uses Pillow (already a hard dep of the organizer). Returns (b64, mime) or
    (None, None) if the image can't be read (e.g. HEIC that Pillow can't open)."""
    try:
        from PIL import Image
    except Exception:
        return None, None
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            scale = min(1.0, float(long_edge) / max(w, h)) if max(w, h) else 1.0
            if scale < 1.0:
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality)
            return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
    except Exception:
        return None, None


def describe_image(image_path, prompt, model=VISION_MODEL, max_tokens=400,
                   long_edge=768, cfg=None, fetcher=None):
    """Send one downscaled image + prompt to the vision model. Returns the
    model's text (caption/tags) or None. Never raises."""
    b64, mime = encode_image(image_path, long_edge=long_edge)
    if not b64:
        return None
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ],
    }]
    return chat(messages, model=model, temperature=0.1, max_tokens=max_tokens,
                cfg=cfg, fetcher=fetcher)
