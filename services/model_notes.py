"""What each model is and what to use it for, in a sentence — shown under the
model's name in the picker.

Two sources, the second on top of the first:

* **What the name says.** With nothing written for a model, a note is put
  together from its name: its family traits (coding, vision, speech, image
  generation), its size, whether it is an uncensored build or a CPU fallback.
  Plain facts a name states; no claims about quality.
* **What the administrator wrote** (``data/model_notes.json``): a summary and
  a use for the models installed on this server, where the measured speed and
  the role each one plays belong.

    {"gpt-oss-120b": {"summary": "…", "use": "…"}}
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Dict, Iterable, Optional

_lock = threading.Lock()
MAX_LEN = 220

# (pattern on the lower-cased name, what it is, what it is for). First match wins.
_KINDS = [
    (r"(^|[^a-z])(tts|speech|voice|chatterbox|kokoro|piper)([^a-z]|$)", "Text-to-speech voice", "Reading replies aloud; not a chat model"),
    (r"(whisper|parakeet|stt|transcri)", "Speech-to-text model", "Turning recordings into text; not a chat model"),
    (r"(embed|bge-|e5-|nomic|minilm)", "Embedding model", "Search and recall behind the scenes; not a chat model"),
    (r"(rerank)", "Reranking model", "Ordering search results; not a chat model"),
    (r"(t2v|i2v|wan[0-9.]|ltx|hunyuan-?video|video)", "Video generation model", "Making short clips from text or images; not a chat model"),
    (r"(image-edit|img2img|kontext|inpaint)", "Image editing model", "Changing an existing picture from an instruction; not a chat model"),
    (r"(flux|sdxl|stable-diffusion|z-image|qwen-image|imagen|dall-?e|image)", "Image generation model", "Making pictures from text; not a chat model"),
    (r"(species|bioclip|classif)", "Image classifier", "Naming what is in a photo; not a chat model"),
    (r"(coder|codestral|devstral|code-?llama|starcoder|deepseek-coder|-code)", "Coding model", "Writing, reviewing and refactoring code"),
    (r"(vision|-vl|vl-|llava|pixtral|moondream|multimodal|-mm)", "Vision model: reads images as well as text", "Questions about photos, screenshots and scanned pages"),
    (r"(reason|thinking|-r1|o[0-9]-|deep-?think)", "Reasoning model: thinks before answering", "Hard problems where a slower, more careful answer is worth it"),
]
_TRAITS = [
    (r"(uncensored|abliterat|heretic|unfilter|unhinged)", "uncensored build"),
    (r"(-cpu|cpu-)", "runs on the CPU only"),
    (r"(-fast|turbo|flash|lite|mini|nano|small)", "tuned for speed"),
]
_SIZE = re.compile(r"(?<![a-z0-9.])(\d{1,3}(?:\.\d)?)b(?![a-z0-9])")


def _path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "model_notes.json")


def _read() -> Dict[str, dict]:
    try:
        with open(_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _tidy(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:MAX_LEN]


def from_name(model_id: str) -> Dict[str, str]:
    """What the name alone says about a model. Empty strings when it says nothing."""
    name = str(model_id or "").lower().rsplit("/", 1)[-1]
    summary, use = "", ""
    for pattern, what, purpose in _KINDS:
        if re.search(pattern, name):
            summary, use = what, purpose
            break
    extras = [label for pattern, label in _TRAITS if re.search(pattern, name)]
    size = _SIZE.search(name)
    if size:
        billions = float(size.group(1))
        extras.insert(0, f"{size.group(1)}B parameters")
        if not summary:
            summary = ("Large general model" if billions >= 60 else "Mid-size general model" if billions >= 12
                       else "Small general model")
            use = ("Demanding work: long documents, careful reasoning, agent tasks" if billions >= 60
                   else "Everyday questions and drafting" if billions >= 12
                   else "Quick, simple tasks; limited knowledge and reasoning")
    if not summary and extras:
        summary = "General model"
    if summary and extras:
        summary += " (" + ", ".join(extras) + ")"
    return {"summary": summary, "use": use}


def note_for(model_id: str, local: Optional[Dict[str, dict]] = None) -> Dict[str, str]:
    """The note shown for one model: the administrator's, else what its name says."""
    local = _read() if local is None else local
    mid = str(model_id or "")
    written = local.get(mid) or local.get(mid.rsplit("/", 1)[-1]) or {}
    guessed = from_name(mid)
    summary = _tidy(written.get("summary")) or guessed["summary"]
    use = _tidy(written.get("use")) or guessed["use"]
    return {"summary": summary, "use": use, "written": bool(written.get("summary") or written.get("use"))}


def notes_for(model_ids: Iterable[str]) -> Dict[str, Dict[str, str]]:
    local = _read()
    out = {}
    for mid in list(dict.fromkeys(str(m) for m in model_ids if m))[:1000]:
        note = note_for(mid, local)
        if note["summary"] or note["use"]:
            out[mid] = note
    return out


def set_note(model_id: str, summary: str, use: str) -> Dict[str, str]:
    """Write (or, with both empty, remove) the administrator's note for a model."""
    mid = _tidy(model_id)
    if not mid:
        raise ValueError("a model name is required")
    from core.atomic_io import atomic_write_json
    with _lock:
        data = _read()
        summary, use = _tidy(summary), _tidy(use)
        if summary or use:
            data[mid] = {"summary": summary, "use": use}
        else:
            data.pop(mid, None)
        atomic_write_json(_path(), data)
    return note_for(mid)
