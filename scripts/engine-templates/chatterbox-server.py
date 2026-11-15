"""Chatterbox voice-cloning TTS server — an OpenAI-compatible /v1/audio/speech
endpoint over Resemble AI's Chatterbox (MIT), designed to sit behind
llama-swap exactly like Aegis's other engine components.

Voices are the user's own cloned samples: every <name>.wav (or .mp3/.flac)
in --voices-dir is a voice named <name>. On first use of a voice its
conditioning is baked and cached next to the sample as <name>.voice.pt, so
later requests skip re-embedding the reference clip.

Chatterbox's generate() caps at ~40s of audio per call (max_new_tokens is
hardcoded upstream), so text is chunked by sentence and the waveforms are
concatenated before encoding. Output is WAV (24 kHz mono) regardless of the
requested response_format — Aegis's TTS service sniffs magic bytes, and every
browser plays WAV.

Run:  python server.py --port 5860 --voices-dir C:/path/to/voices
"""

import argparse
import io
import logging
import re
import threading
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chatterbox-server")

SAMPLE_EXTS = (".wav", ".mp3", ".flac", ".ogg")
MAX_CHUNK_CHARS = 400  # well under the ~40s/1000-token ceiling at speech pace

_model = None
_model_lock = threading.Lock()
_active_voice = None       # (name, sample mtime) currently loaded — the mtime
                           # makes a re-recorded sample invalidate in-memory conds
_args = None


def _voices_dir() -> Path:
    return Path(_args.voices_dir)


def list_voices():
    seen = {}
    for p in sorted(_voices_dir().glob("*")):
        if p.suffix.lower() in SAMPLE_EXTS and not p.name.endswith(".voice.pt"):
            seen.setdefault(p.stem, p)
    return seen  # name -> sample path


def _get_model():
    global _model
    if _model is None:
        import torch
        from chatterbox.tts import ChatterboxTTS
        device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info("Loading Chatterbox (device=%s)…", device)
        _model = ChatterboxTTS.from_pretrained(device=device)
        logger.info("Chatterbox ready (sr=%s)", _model.sr)
    return _model


def _prepare_voice(model, name: str, sample: Path):
    """Load or bake the voice conditionals for `name`."""
    global _active_voice
    sample_mtime = sample.stat().st_mtime
    if _active_voice == (name, sample_mtime) and model.conds is not None:
        return
    from chatterbox.tts import Conditionals
    baked = sample.with_suffix(sample.suffix + ".voice.pt")  # <name>.wav.voice.pt
    if baked.exists() and baked.stat().st_mtime >= sample.stat().st_mtime:
        model.conds = Conditionals.load(baked, map_location=model.device)
        # Conditionals.load may leave tensors on CPU depending on version —
        # push to the model device defensively.
        try:
            model.conds = model.conds.to(model.device)
        except Exception:
            pass
    else:
        model.prepare_conditionals(str(sample), exaggeration=0.5)
        try:
            model.conds.save(baked)
        except Exception:
            logger.warning("Could not cache voice conditionals for %r", name, exc_info=True)
    _active_voice = (name, sample_mtime)


def _chunk_text(text: str):
    """Sentence-ish chunks under MAX_CHUNK_CHARS (generate() caps ~40s)."""
    parts = re.split(r"(?<=[.!?…])\s+|\n+", text)
    chunks, cur = [], ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) > MAX_CHUNK_CHARS:
            # A single monster sentence — flush pending text FIRST so the
            # spoken order matches the written order, then hard-split it.
            if cur:
                chunks.append(cur)
                cur = ""
            while len(p) > MAX_CHUNK_CHARS:
                chunks.append(p[:MAX_CHUNK_CHARS])
                p = p[MAX_CHUNK_CHARS:]
        if not p:
            continue
        if len(cur) + len(p) + 1 <= MAX_CHUNK_CHARS:
            cur = f"{cur} {p}".strip()
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks or [""]


def synthesize(text: str, voice: str) -> bytes:
    import torch
    import torchaudio

    voices = list_voices()
    if voice not in voices:
        raise ValueError(f"Unknown voice {voice!r} — available: {', '.join(voices) or '(none — add a sample to the voices dir)'}")
    with _model_lock:
        model = _get_model()
        _prepare_voice(model, voice, voices[voice])
        waves = []
        for chunk in _chunk_text(text):
            if not chunk:
                continue
            wav = model.generate(chunk)  # conds already prepared; (1, N) @ model.sr
            waves.append(wav)
        if not waves:
            raise ValueError("No text to synthesize")
        full = torch.cat(waves, dim=1)
        buf = io.BytesIO()
        torchaudio.save(buf, full.cpu(), model.sr, format="wav")
        return buf.getvalue()


def build_app():
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import Response
    from pydantic import BaseModel

    app = FastAPI(title="Aegis Chatterbox TTS")

    class SpeechRequest(BaseModel):
        model: str = "chatterbox-tts"
        input: str
        voice: str = ""
        response_format: str = "wav"  # advisory; output is always WAV
        speed: float = 1.0            # accepted for API parity; not applied

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": "chatterbox-tts", "object": "model"}]}

    @app.get("/v1/voices")
    async def voices():
        return {"voices": sorted(list_voices().keys())}

    @app.post("/v1/audio/speech")
    async def speech(req: SpeechRequest):
        text = (req.input or "").strip()
        if not text:
            raise HTTPException(400, "input is required")
        if len(text) > 5000:
            text = text[:5000]
        voice = (req.voice or "").strip()
        if not voice:
            available = sorted(list_voices().keys())
            if not available:
                raise HTTPException(400, "No cloned voices yet — add a sample in Settings → Text to Speech → My Voices.")
            voice = available[0]
        try:
            import anyio
            audio = await anyio.to_thread.run_sync(synthesize, text, voice)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception:
            logger.exception("synthesis failed")
            raise HTTPException(500, "Synthesis failed")
        return Response(content=audio, media_type="audio/wav")

    return app


def main():
    global _args
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5860)
    parser.add_argument("--voices-dir", required=True)
    parser.add_argument("--preload", action="store_true", help="load the model at startup instead of first request")
    _args = parser.parse_args()
    Path(_args.voices_dir).mkdir(parents=True, exist_ok=True)

    if _args.preload:
        _get_model()

    import uvicorn
    uvicorn.run(build_app(), host=_args.host, port=_args.port, log_level="info")


if __name__ == "__main__":
    main()
