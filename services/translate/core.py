"""Translating: text, long text in parts, and the words in a picture.

The wording of the request to the model is TranslateGemma's own (it is in
the model's template): name both languages, ask for the translation and
nothing else. The model works on about two thousand tokens at a time, so a
long text goes paragraph by paragraph and comes back with its paragraphs
where they were.

Nothing here keeps anything; :mod:`.history` does, when a page asks it to.
"""

from __future__ import annotations

import base64
import re
import time
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from . import detect as detector
from . import engine, languages

MAX_CHARS = 20_000               # one request
PART_CHARS = 1_400               # one piece handed to the model
MAX_IMAGE_BYTES = 12 * 1024 * 1024
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
_SURE_ENOUGH = 0.35              # below this the model is asked what language it is


class TranslateError(Exception):
    """What went wrong, in words for the person."""


# ── the words of the request ───────────────────────────────────────────────

def text_prompt(text: str, source: str, target: str) -> str:
    to_name = languages.plain_name(target) or languages.name(target)
    to_label = f"{to_name} ({target})"
    if source:
        from_name = languages.plain_name(source) or languages.name(source)
        return (f"You are a professional {from_name} ({source}) to {to_label} translator. Your goal is to accurately convey the "
                f"meaning and nuances of the original {from_name} text while adhering to {to_name} grammar, vocabulary, and "
                f"cultural sensitivities.\nProduce only the {to_name} translation, without any additional explanations or "
                f"commentary. Please translate the following {from_name} text into {to_name}:\n\n\n{text.strip()}")
    return (f"You are a professional translator into {to_label}. Your goal is to accurately convey the meaning and nuances of "
            f"the original text while adhering to {to_name} grammar, vocabulary, and cultural sensitivities.\nProduce only "
            f"the {to_name} translation, without any additional explanations or commentary. Please translate the following "
            f"text into {to_name}:\n\n\n{text.strip()}")


def image_prompt(source: str, target: str) -> str:
    to_name = languages.plain_name(target) or languages.name(target)
    if source:
        from_name = languages.plain_name(source) or languages.name(source)
        lead = (f"You are a professional {from_name} ({source}) to {to_name} ({target}) translator. Your goal is to accurately "
                f"convey the meaning and nuances of the original {from_name} text while adhering to {to_name} grammar, "
                f"vocabulary, and cultural sensitivities.\nPlease translate the {from_name} text in the provided image into {to_name}. ")
    else:
        lead = (f"You are a professional translator into {to_name} ({target}). Your goal is to accurately convey the meaning "
                f"and nuances of the original text while adhering to {to_name} grammar, vocabulary, and cultural "
                f"sensitivities.\nPlease translate the text in the provided image into {to_name}. ")
    return lead + (f"Produce only the {to_name} translation, without any additional explanations, alternatives or commentary. "
                   "Focus only on the text, do not output where the text is located, surrounding objects or any other "
                   "explanation about the picture. Ignore symbols, pictogram, and arrows!")


# ── long text, in parts ────────────────────────────────────────────────────

_SENTENCE_END = re.compile(r"(?<=[.!?。！？…;:])\s+")


def parts(text: str, limit: int = PART_CHARS) -> List[Tuple[str, str]]:
    """``[(piece, what followed it)]``: the text as the pieces handed to the
    model — one paragraph each, with the blank line that came after it, so
    the translation goes back together exactly as the original was laid out.
    (Paragraphs are not packed together: the model does not reliably keep a
    blank line that sits inside a piece.) Lines of one paragraph — a list, an
    address — stay in one piece. A paragraph longer than ``limit`` is cut at
    sentence ends."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    out: List[Tuple[str, str]] = []
    blocks = re.split(r"(\n[ \t]*\n+)", text)
    for index in range(0, len(blocks), 2):
        body = blocks[index].strip()
        gap = "\n\n" if index + 1 < len(blocks) else ""
        if not body:
            continue
        if len(body) <= limit:
            out.append((body, gap))
            continue
        piece = ""
        for sentence in _SENTENCE_END.split(body):
            while len(sentence) > limit:                          # one endless sentence
                cut = sentence.rfind(" ", 0, limit)
                cut = cut if cut > limit // 2 else limit
                if piece:
                    out.append((piece, " "))
                    piece = ""
                out.append((sentence[:cut].strip(), " "))
                sentence = sentence[cut:].strip()
            if piece and len(piece) + len(sentence) + 1 > limit:
                out.append((piece, " "))
                piece = sentence
            else:
                piece = f"{piece} {sentence}".strip()
        if piece:
            out.append((piece, gap))
        elif out:
            out[-1] = (out[-1][0], gap)
    return out


def _echoes_the_request(answer: str) -> bool:
    """Did the model hand the instructions back instead of a translation?"""
    head = str(answer or "").lstrip()[:80].lower()
    return head.startswith("you are a professional") or "produce only the" in str(answer or "")[:400].lower()


def _tidy(answer: str) -> str:
    """The translation alone: models sometimes label it or wrap it."""
    text = str(answer or "").strip()
    text = re.sub(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>", "", text).strip()
    text = re.sub(r"^(?:translation|here is the translation|translated text)\s*[:：]\s*", "", text, flags=re.I)
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"“”'`" and text.count(text[0]) == 2:
        text = text[1:-1].strip()
    return text


# ── which language ─────────────────────────────────────────────────────────

async def language_of(text: str, owner: Optional[str] = None) -> Tuple[str, str]:
    """``(code, how it was told)``: by looking (``"detected"``), by asking the
    model (``"asked"``), or ``("", "")`` when neither could say."""
    code, sure = detector.detect(text)
    if code and sure >= _SURE_ENOUGH:
        return code, "detected"
    sample = text.strip()[:400]
    if not sample:
        return "", ""
    try:
        answer, _who = await engine.ask(
            [{"role": "user", "content": "Which language is the following text written in? Answer with the English name of "
                                         f"the language only, one or two words.\n\n{sample}"}],
            max_tokens=12, timeout=60.0, owner=owner)
    except engine.EngineError:
        return (code, "detected") if code else ("", "")
    said = re.sub(r"[^\w\s()-]", " ", answer).strip().split("\n")[0][:40].strip()
    named = languages.resolve(said, codes=False) or (languages.resolve(said) if len(said) > 3 else "")
    if named:
        return named, "asked"
    return (code, "detected") if code else ("", "")


def _want(code_or_name: str, what: str) -> str:
    code = languages.resolve(code_or_name)
    if not code:
        raise TranslateError(f"“{str(code_or_name)[:40]}” is not a language I can translate {what}. Name one such as Spanish, "
                             "French, German, Japanese or Arabic.")
    return code


# ── translating ────────────────────────────────────────────────────────────

async def translate_text(text: str, target: str, source: str = "auto", owner: Optional[str] = None,
                         on_part: Optional[Callable[[int, int, str], Awaitable[None]]] = None) -> Dict[str, object]:
    """Translate ``text`` into ``target`` (a code or a name). ``source`` is a
    language, or ``auto`` to work it out. ``on_part(n, of, translated)`` is
    called as each piece of a long text is done.

    Returns ``text`` (the translation), ``source`` / ``target`` (codes),
    their names, ``detected`` (how the source was told, "" when it was
    given), ``parts``, ``engine`` and ``seconds``."""
    original = str(text or "")
    if not original.strip():
        raise TranslateError("There is nothing to translate.")
    if len(original) > MAX_CHARS:
        raise TranslateError(f"That is {len(original):,} characters; one translation takes up to {MAX_CHARS:,}. Send it in pieces.")
    to_code = _want(target, "into")
    how = ""
    if str(source or "auto").strip().lower() in ("", "auto", "detect", "auto-detect", "unknown"):
        from_code, how = await language_of(original, owner)
    else:
        from_code = _want(source, "from")
    started = time.monotonic()
    if from_code and from_code.split("-")[0] == to_code.split("-")[0] and from_code == to_code:
        return _result(original, from_code, to_code, how, 0, "", 0.0, same=True)
    pieces = parts(original)
    done: List[str] = []
    who = ""
    for number, (piece, gap) in enumerate(pieces, start=1):
        answer, who = await engine.ask([{"role": "user", "content": text_prompt(piece, from_code, to_code)}],
                                       max_tokens=min(2000, 200 + len(piece)), owner=owner)
        if _echoes_the_request(answer) and from_code:
            # Told the wrong language for a word or two, a model may repeat its
            # instructions. Asked without a source language, it just translates.
            answer, who = await engine.ask([{"role": "user", "content": text_prompt(piece, "", to_code)}],
                                           max_tokens=min(2000, 200 + len(piece)), owner=owner)
        translated = "" if _echoes_the_request(answer) else _tidy(answer)
        if not translated:
            raise TranslateError("The model gave no translation for part of that. Try again, or try a shorter piece.")
        done.append(translated + gap)
        if on_part is not None:
            await on_part(number, len(pieces), translated + gap)
    return _result("".join(done).strip(), from_code, to_code, how, len(pieces), who, time.monotonic() - started)


async def translate_image(data: bytes, mime: str, target: str, source: str = "auto",
                          owner: Optional[str] = None) -> Dict[str, object]:
    """Translate the words in a picture (a sign, a label, a page). The
    translation model reads the picture itself."""
    mime = str(mime or "").split(";")[0].strip().lower()
    if mime not in IMAGE_TYPES:
        raise TranslateError("That is not a picture I can read. Use a JPEG, PNG or WebP photo.")
    if not data:
        raise TranslateError("That picture is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise TranslateError(f"That picture is over {MAX_IMAGE_BYTES // (1024 * 1024)} MB. Take it at a lower resolution.")
    to_code = _want(target, "into")
    from_code = ""
    if str(source or "auto").strip().lower() not in ("", "auto", "detect", "auto-detect", "unknown"):
        from_code = _want(source, "from")
    started = time.monotonic()
    picture = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": picture}},
                                             {"type": "text", "text": image_prompt(from_code, to_code)}]}]
    try:
        answer, who = await engine.ask(messages, max_tokens=1200, owner=owner, needs_sight=True)
    except engine.EngineError as exc:
        raise TranslateError(str(exc))
    translated = _tidy(answer)
    if not translated:
        raise TranslateError("No words could be read in that picture.")
    return _result(translated, from_code, to_code, "", 1, who, time.monotonic() - started)


def _result(text: str, source: str, target: str, how: str, count: int, who: str, seconds: float, same: bool = False) -> Dict[str, object]:
    return {"text": text, "source": source, "source_name": languages.name(source), "target": target,
            "target_name": languages.name(target), "detected": how, "parts": count, "engine": who,
            "seconds": round(seconds, 1), "same_language": same}
