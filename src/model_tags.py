"""Model capability classifier — "what is this file / alias good at?".

Pure name-based heuristics (no file reads) that turn a model id or filename
into two tag lists the Media Studio panel renders as chips:

- capabilities: the modality contract (text-to-image, image-to-video, vision,
  chat, coding, embedding, companion, ...)
- best_for: honest "reach for this when ..." hints, tuned to the models that
  actually live in this install (TableLLM, CIC-ACLARC classifier, Wan/LTX
  video, Qwen-Image family, uncensored merges, ...).

Name-based means a mislabeled file gets mislabeled tags — the chips are a
navigation aid, not a gate; nothing downstream enforces them.
"""

import os
import re
from typing import Any, Dict, List

# Quant/precision suffix noise stripped before matching ("-Q4_K_M", ".bf16").
_QUANT_RE = re.compile(
    r"(?:[-_.](?:i?q\d[\w]*|bf16|fp16|f16|f32|gguf|safetensors|\(\d+\)))+$", re.I
)


def _norm(name: str) -> str:
    base = os.path.basename(str(name or "")).strip().lower()
    base = re.sub(r"\.(gguf|safetensors)$", "", base)
    base = _QUANT_RE.sub("", base)
    return base


def _has(n: str, *words: str) -> bool:
    return any(w in n for w in words)


def _word(n: str, *words: str) -> bool:
    """Separator-anchored match so 'wanda-chat' never matches 'wan'."""
    return any(re.search(rf"(?:^|[/\-_. ]){re.escape(w)}(?:$|[/\-_. ])", n) for w in words)


def classify(name: str, *, is_lora: bool = False) -> Dict[str, List[str]]:
    """Tags for one model id / filename: {"capabilities": [...], "best_for": [...]}."""
    n = _norm(name)
    caps: List[str] = []
    best: List[str] = []

    def cap(t):
        if t not in caps:
            caps.append(t)

    def bf(t):
        if t not in best:
            best.append(t)

    if is_lora:
        return {"capabilities": ["lora"], "best_for": ["style add-on — use via <lora:name:weight> or a style preset"]}

    # ── companions: parts of a pipeline, not directly usable models ──────────
    if _has(n, "mmproj"):
        return {"capabilities": ["companion"], "best_for": ["vision projector — pairs with its LLM"]}
    if _has(n, "esrgan", "ultrasharp", "upscal", "swinir"):
        return {"capabilities": ["upscaler"], "best_for": ["sharpening & enlarging images/video frames"]}
    if _has(n, "vae"):
        return {"capabilities": ["companion"], "best_for": ["VAE — pairs with a diffusion model"]}
    if _has(n, "connector") or _word(n, "umt5", "t5xxl") or _has(n, "encoder"):
        return {"capabilities": ["companion"], "best_for": ["text encoder — pairs with a diffusion model"]}
    # Comfy-Org single-file TE repacks: gemma_3_12B_it_fp4_mixed, qwen_3_8b_fp8mixed
    if re.search(r"(?:gemma|qwen)_\d[\w.]*fp[48]", n):
        return {"capabilities": ["companion"], "best_for": ["text encoder — pairs with a diffusion model"]}

    uncensored = _has(n, "uncensored", "nsfw", "abliterated", "unfiltered")

    # ── video pipelines ──────────────────────────────────────────────────────
    if _word(n, "wan", "wan2", "wan2.1", "wan2.2") or re.search(r"wan[\d.]*[-_]", n):
        if _has(n, "animate", "vace", "v2v"):
            cap("video-to-video")
            bf("character animation & motion transfer")
        elif _has(n, "i2v", "flf2v"):
            cap("image-to-video")
            bf("animating a still image")
        else:
            cap("text-to-video")
            bf("cinematic clips (16fps)")
    if _word(n, "ltx", "ltx2", "ltx-2") or n.startswith("ltx"):
        cap("text-to-video")
        cap("image-to-video")
        bf("video with audio (24fps)")
    if _has(n, "hunyuan-video", "hunyuanvideo", "cogvideo", "mochi") or _word(n, "t2v"):
        cap("text-to-video")
    if _word(n, "i2v"):
        cap("image-to-video")

    # ── image pipelines ──────────────────────────────────────────────────────
    if _has(n, "edit", "inpaint", "fill") and _has(n, "image", "flux", "sd", "diffusion", "kontext"):
        cap("image-to-image")
        bf("instruction edits & style transfer")
    elif _has(n, "qwen-image", "qwen_image") or _word(n, "flux", "flux2", "flux-2") \
            or _has(n, "sdxl", "sd3", "stable-diffusion", "kandinsky", "pixart", "playground") \
            or (_has(n, "rapid") and _has(n, "qwen")):
        cap("text-to-image")
        if _has(n, "rapid", "lightning", "turbo", "lcm", "schnell", "klein"):
            bf("fast image drafts (step-distilled)")
        else:
            bf("photoreal images & text rendering")
    elif _has(n, "z-image", "z_image", "zimage"):
        cap("text-to-image")
        bf("fast photoreal drafts (8-step)")
    elif _word(n, "chroma"):
        cap("text-to-image")
        bf("uncensored art & photoreal")
    elif _has(n, "noobai", "illustrious"):
        cap("text-to-image")
        bf("anime & illustration (danbooru tags)")
    elif _word(n, "krea", "krea2"):
        cap("text-to-image")
        bf("2K stills with a natural aesthetic")
    elif _has(n, "ideogram"):
        cap("text-to-image")
        bf("posters, logos & legible in-image text")

    # ── perception / understanding ───────────────────────────────────────────
    if _word(n, "vl") or _has(n, "vision", "llava", "minicpm-v", "pixtral", "moondream"):
        cap("vision")
        cap("chat")
        bf("image & video classification")
        bf("OCR & data extraction from documents")

    # ── task specialists (the reason 'best used for' exists) ─────────────────
    if _has(n, "tablellm", "table-llm"):
        cap("chat")
        bf("tables & data extraction")
        bf("chart & data interpretation")
    if _has(n, "aclarc", "cic-") or _word(n, "classifier", "classification"):
        cap("chat")
        bf("text classification")
    if _word(n, "coder", "code") or _has(n, "starcoder", "codestral", "deepseek-coder", "neo-code"):
        cap("coding")
        cap("chat")
        bf("coding & agent tool use")
        bf("charts & data analysis (via code)")
    if _has(n, "whisper"):
        cap("speech-to-text")
    if _word(n, "tts") or _has(n, "kokoro", "bark", "piper"):
        cap("text-to-speech")
    if _word(n, "embed", "bge", "minilm", "e5", "nomic") or _has(n, "embedding"):
        cap("embedding")
        bf("RAG & semantic search")

    # ── generic LLM fallback ─────────────────────────────────────────────────
    diffusion_or_av = any(c in caps for c in (
        "text-to-image", "image-to-image", "text-to-video", "image-to-video",
        "video-to-video", "speech-to-text", "text-to-speech", "embedding",
    ))
    if not caps or (not diffusion_or_av and "chat" not in caps):
        cap("chat")
    if "chat" in caps and not diffusion_or_av:
        # Qwen/Gemma/GLM instruct families are strong multilingual models.
        if _word(n, "qwen", "qwen2", "qwen3", "gemma", "glm") or n.startswith(("qwen", "gemma", "glm", "supergemma")):
            bf("translation & multilingual chat")
        if _has(n, "distill", "r1", "qwq") or _word(n, "thinking", "think"):
            bf("step-by-step reasoning")

    if uncensored:
        cap("uncensored")
    if _word(n, "a3b", "moe") or _has(n, "mixtral"):
        cap("moe")

    return {"capabilities": caps, "best_for": best}


def classify_many(names: List[str]) -> Dict[str, Dict[str, List[str]]]:
    return {str(x): classify(x) for x in names}


def is_companion(tags: Dict[str, Any]) -> bool:
    return "companion" in (tags.get("capabilities") or [])
