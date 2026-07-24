#!/usr/bin/env python3
"""Local-LLM filename cracker for the media-organizer suite.

Talks to a local LLM server (stdlib urllib only) to turn cryptic
scene-release filenames into structured title guesses. The LLM is NEVER
trusted alone: callers must verify the guess against TMDB before using it
(see llm_reidentify.py).

Two server dialects are supported, selected by the "api" config key:
  * "openai" — an OpenAI-compatible server (Aegis serves its local models
    through llama-swap at http://127.0.0.1:9090/v1); uses /models and
    /chat/completions. This is the default.
  * "ollama" — a native Ollama server; uses /api/tags and /api/generate.

Config: llm_config.json next to this file:
    {"api": "openai",
     "endpoint": "http://127.0.0.1:9090/v1",
     "model": "qwen2.5-14b-aclarc",
     "timeoutMs": 120000,
     "minConfidence": 0.5}

Public API:
    available(cfg=None, fetcher=None) -> bool
    list_models(cfg=None, fetcher=None) -> [str]
    crack_filename(name, hint="movie", cfg=None, fetcher=None)
        -> {"title": str, "year": int|None, "kind": "movie"|"tv",
            "confidence": float} | None

Every function is total-failure-safe: any network/parse problem returns a
falsy value instead of raising, so batch callers can treat it as
"no answer" and move on.
"""
import json
import os
import re
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")

DEFAULTS = {
    "api": "openai",                          # "openai" (llama-swap) | "ollama"
    "endpoint": "http://127.0.0.1:9090/v1",   # Aegis llama-swap OpenAI surface
    "model": "qwen2.5-14b-aclarc",
    "timeoutMs": 120000,
    "minConfidence": 0.5,
}

# caps on generation: the answer is one small JSON object
_GEN_OPTIONS = {"temperature": 0.1, "num_predict": 160}


# =================================================================== config

def load_config(path=CONFIG_PATH):
    """llm_config.json merged over DEFAULTS; unreadable file -> defaults."""
    cfg = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in DEFAULTS:
                if k in data and data[k] is not None:
                    cfg[k] = data[k]
    except Exception:
        pass
    cfg["api"] = str(cfg.get("api", "openai")).strip().lower()
    if cfg["api"] not in ("openai", "ollama"):
        cfg["api"] = "openai"
    cfg["endpoint"] = str(cfg["endpoint"]).rstrip("/")
    try:
        cfg["timeoutMs"] = max(1000, int(cfg["timeoutMs"]))
    except Exception:
        cfg["timeoutMs"] = DEFAULTS["timeoutMs"]
    try:
        cfg["minConfidence"] = min(1.0, max(0.0, float(cfg["minConfidence"])))
    except Exception:
        cfg["minConfidence"] = DEFAULTS["minConfidence"]
    return cfg


# =================================================================== http

def _http_json(url, payload=None, timeout=30):
    """Default fetcher. GET when payload is None, else POST a JSON body.
    Module-level so tests can inject a fake via the fetcher parameter."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"User-Agent": "PhotoOrganizer/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _resolve(cfg, fetcher):
    return (cfg or load_config()), (fetcher or _http_json)


def list_models(cfg=None, fetcher=None):
    """Model names known to the server; [] when unreachable. Handles both the
    OpenAI (/models) and Ollama (/api/tags) surfaces."""
    cfg, fetcher = _resolve(cfg, fetcher)
    try:
        t = min(15, cfg["timeoutMs"] / 1000)
        if cfg.get("api") == "openai":
            data = fetcher(cfg["endpoint"] + "/models", None, timeout=t)
            arr = data.get("data") if isinstance(data, dict) else None
            return [m["id"] for m in arr or [] if isinstance(m, dict)
                    and m.get("id")]
        data = fetcher(cfg["endpoint"] + "/api/tags", None, timeout=t)
        models = data.get("models") if isinstance(data, dict) else None
        return [m["name"] for m in models or [] if isinstance(m, dict)
                and m.get("name")]
    except Exception:
        return []


def _openai_content(data):
    """Assistant text from an OpenAI chat response, with any <think>…</think>
    reasoning block stripped so _extract_json sees the JSON, not the thought."""
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(text, str):
        return None
    return re.sub(r"<think>.*?</think>", " ", text, flags=re.S)


def available(cfg=None, fetcher=None):
    """True when the server answers /api/tags AND the configured model is
    actually installed (otherwise every generate call would 404)."""
    cfg, fetcher = _resolve(cfg, fetcher)
    names = list_models(cfg, fetcher)
    want = cfg["model"]
    return any(n == want or n.split(":")[0] == want.split(":")[0]
               for n in names)


# =================================================================== prompt

_PROMPT = """You identify cryptic video filenames from a private media collection.
Reply with ONLY one JSON object, no prose, no markdown.

Filename anatomy: [group-prefix-]title[.words][-quality-tags][-group].
- Release groups prefix or suffix the name: "lchd-batb-720p" is group "lchd",
  title code "batb", quality "720p".
- Title codes are abbreviations, acronyms, leetspeak, dotted words, or
  foreign/original titles of well-known {kind}s.
- Quality/codec/source tags (720p, 1080p, hdrip, webrip, bluray, x264, xvid,
  dvdrip, hdtv, proper, repack) are NEVER part of the title.
- Give the canonical English title and release year if reasonably sure.
- If the code is opaque (e.g. a scene group's internal coding you cannot
  expand), answer with title null and confidence 0.0.
- confidence is 0.0-1.0; never above 0.9 for abbreviated titles.

Examples (hint: movie):
"dAA-Turner.and.Hooch-1080p" -> {{"title": "Turner & Hooch", "year": 1989, "kind": "movie", "confidence": 0.85}}
"rushhour720p-shk" -> {{"title": "Rush Hour", "year": 1998, "kind": "movie", "confidence": 0.9}}
"lchd-batb-720p" -> {{"title": "Beauty and the Beast", "year": 1991, "kind": "movie", "confidence": 0.7}}
"s4a-the.brass.teapot.hdrip.xvid-s4a" -> {{"title": "The Brass Teapot", "year": 2012, "kind": "movie", "confidence": 0.8}}
"pfa-poca.720p" -> {{"title": "Pocahontas", "year": 1995, "kind": "movie", "confidence": 0.6}}
"japhson-faff" -> {{"title": null, "year": null, "kind": "movie", "confidence": 0.0}}

Example (hint: tv):
"003-mr.denton.on.doomsday" -> {{"title": "The Twilight Zone", "year": 1959, "kind": "tv", "confidence": 0.5}}

Now identify this {kind} filename: "{name}"
JSON:"""


def build_prompt(name, hint="movie"):
    kind = "tv" if hint == "tv" else "movie"
    # {{ }} are literal JSON braces in the template; {kind}/{name} are slots
    return (_PROMPT.replace("{kind}", kind).replace("{name}", name)
            .replace("{{", "{").replace("}}", "}"))


# =================================================================== parsing

def _extract_json(text):
    """First balanced JSON object found anywhere in the reply (tolerates
    markdown fences, leading prose, trailing chatter). None if none parse."""
    if not text or not isinstance(text, str):
        return None
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _year_ok(y):
    return 1870 <= y <= datetime.now().year + 2


def _validate(obj, hint):
    """Shape-check a parsed LLM reply -> canonical dict or None (no answer).
    Never raises on malformed input."""
    if not isinstance(obj, dict):
        return None
    title = obj.get("title")
    if title is not None:
        if not isinstance(title, str):
            return None
        title = re.sub(r"\s{2,}", " ", title).strip(" .-_")
        if not title or len(title) > 200:
            return None
    if title is None:
        return None                      # explicit "cannot identify"
    year = obj.get("year")
    if isinstance(year, str) and year.strip().isdigit():
        year = int(year.strip())
    if not isinstance(year, int) or isinstance(year, bool) or not _year_ok(year):
        year = None
    kind = obj.get("kind")
    kind = kind if kind in ("movie", "tv") else ("tv" if hint == "tv" else "movie")
    conf = obj.get("confidence")
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 0.0
    conf = min(1.0, max(0.0, conf))
    return {"title": title, "year": year, "kind": kind, "confidence": conf}


# =================================================================== crack

def crack_filename(name, hint="movie", cfg=None, fetcher=None):
    """Ask the LLM to identify one cryptic filename.

    Returns {"title", "year", "kind", "confidence"} or None when the server
    is unreachable, the reply is malformed, the model declines
    (title null), or the answer falls below the confidence gate.
    """
    cfg, fetcher = _resolve(cfg, fetcher)
    base = os.path.basename(name or "")
    # strip only a plausible file extension (".mkv"); never amputate dotted
    # scene names ("daa-turner.and.hooch-1080p" is a STEM, splitext would
    # eat ".hooch-1080p")
    m = re.match(r"^(.*)(\.[A-Za-z0-9]{1,5})$", base)
    stem = (m.group(1) if m else base).strip()
    if not stem:
        return None
    if cfg.get("api") == "openai":
        payload = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content":
                 "Reply with ONLY one JSON object — no prose, no markdown, "
                 "no reasoning. /no_think"},
                {"role": "user", "content": build_prompt(stem, hint)},
            ],
            "temperature": _GEN_OPTIONS["temperature"],
            "max_tokens": 300,
        }
        url = cfg["endpoint"] + "/chat/completions"
    else:
        payload = {
            "model": cfg["model"],
            "prompt": build_prompt(stem, hint),
            "stream": False,
            "format": "json",
            "options": dict(_GEN_OPTIONS),
        }
        url = cfg["endpoint"] + "/api/generate"
    try:
        data = fetcher(url, payload, timeout=cfg["timeoutMs"] / 1000)
    except Exception:
        return None
    if cfg.get("api") == "openai":
        text = _openai_content(data)
    else:
        text = data.get("response") if isinstance(data, dict) else None
    ans = _validate(_extract_json(text), hint)
    if ans is None:
        return None
    if ans["confidence"] < cfg["minConfidence"]:
        return None
    return ans
