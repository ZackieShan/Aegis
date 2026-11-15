"""Qwen3-TTS voice-cloning TTS server — an OpenAI-compatible /v1/audio/speech
endpoint over Qwen/Qwen3-TTS-12Hz-1.7B-Base (Apache-2.0), designed to sit
behind llama-swap exactly like Aegis's Chatterbox engine component.

Voices are the user's own cloned samples: every <name>.wav (or .mp3/.flac)
in --voices-dir is a voice named <name>. Qwen3-TTS clones zero-shot from a
~3-10s reference clip. If a <name>.txt sidecar exists its contents are used
as the reference transcript (best quality, ICL mode); otherwise only the
speaker embedding is used (x_vector_only_mode — no transcript needed). On
first use of a voice its clone prompt is baked and cached next to the sample
as <name>.<ext>.qwen3.pt, so later requests skip re-embedding the clip.

Text is chunked by sentence (the talker's quality drifts on very long single
generations) and the waveforms are concatenated before encoding. Output is
WAV (24 kHz mono) regardless of the requested response_format — Aegis's TTS
service sniffs magic bytes, and every browser plays WAV.

Run:  python server.py --port 5861 --voices-dir C:/path/to/voices
"""

import argparse
import io
import logging
import re
import threading
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("qwen3tts-server")

SAMPLE_EXTS = (".wav", ".mp3", ".flac", ".ogg")
MAX_CHUNK_CHARS = 400  # sentence batches; quality/latency sweet spot

DEFAULT_MODEL_DIR = "E:/AI/aegis/aegis/models/audio/qwen3-tts-1.7b-base"

_model = None
_model_lock = threading.Lock()
_prompt_cache = {}  # name -> (sample mtime, voice_clone_prompt) — a re-recorded
                    # sample invalidates the in-memory prompt
_args = None


def _voices_dir() -> Path:
    return Path(_args.voices_dir)


def list_voices():
    seen = {}
    for p in sorted(_voices_dir().glob("*")):
        if p.suffix.lower() in SAMPLE_EXTS:
            seen.setdefault(p.stem, p)
    return seen  # name -> sample path


def _get_model():
    global _model
    if _model is None:
        import torch
        from qwen_tts import Qwen3TTSModel
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
        logger.info("Loading Qwen3-TTS from %s (device=%s dtype=%s)…", _args.model_dir, device, dtype)
        _model = Qwen3TTSModel.from_pretrained(
            _args.model_dir,
            device_map=device,
            dtype=dtype,
        )
        logger.info("Qwen3-TTS ready")
    return _model


def _ref_text_for(sample: Path):
    """Optional transcript sidecar: <name>.txt next to the sample. With it the
    clone runs in full ICL mode; without it we fall back to x-vector-only."""
    txt = sample.with_suffix(".txt")
    if txt.exists():
        try:
            content = txt.read_text(encoding="utf-8").strip()
            if content:
                return content
        except Exception:
            logger.warning("Could not read transcript sidecar %s", txt, exc_info=True)
    return None


def _prepare_voice(model, name: str, sample: Path):
    """Load or bake the reusable voice-clone prompt for `name`."""
    sample_mtime = sample.stat().st_mtime
    cached = _prompt_cache.get(name)
    if cached and cached[0] == sample_mtime:
        return cached[1]
    import torch
    baked = sample.with_suffix(sample.suffix + ".qwen3.pt")  # <name>.wav.qwen3.pt
    if baked.exists() and baked.stat().st_mtime >= sample_mtime:
        try:
            prompt = torch.load(baked, map_location="cpu", weights_only=False)
            _prompt_cache[name] = (sample_mtime, prompt)
            return prompt
        except Exception:
            logger.warning("Stale/unreadable baked prompt for %r — re-baking", name, exc_info=True)
    ref_text = _ref_text_for(sample)
    prompt = model.create_voice_clone_prompt(
        ref_audio=str(sample),
        ref_text=ref_text,
        x_vector_only_mode=ref_text is None,
    )
    try:
        torch.save(prompt, baked)
    except Exception:
        logger.warning("Could not cache voice-clone prompt for %r", name, exc_info=True)
    _prompt_cache[name] = (sample_mtime, prompt)
    return prompt


def _chunk_text(text: str):
    """Sentence-ish chunks under MAX_CHUNK_CHARS."""
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


def synthesize(text: str, voice: str, language: str = "Auto") -> bytes:
    import numpy as np
    import soundfile as sf

    voices = list_voices()
    if voice not in voices:
        raise ValueError(f"Unknown voice {voice!r} — available: {', '.join(voices) or '(none — add a sample to the voices dir)'}")
    with _model_lock:
        model = _get_model()
        prompt = _prepare_voice(model, voice, voices[voice])
        waves, sr = [], 24000
        for chunk in _chunk_text(text):
            if not chunk:
                continue
            wavs, sr = model.generate_voice_clone(
                text=chunk,
                language=language,
                voice_clone_prompt=prompt,
            )
            waves.append(np.asarray(wavs[0], dtype=np.float32))
        if not waves:
            raise ValueError("No text to synthesize")
        full = np.concatenate(waves)
        buf = io.BytesIO()
        sf.write(buf, full, sr, format="WAV", subtype="PCM_16")
        return buf.getvalue()


def build_app():
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import Response
    from pydantic import BaseModel

    app = FastAPI(title="Aegis Qwen3-TTS")

    class SpeechRequest(BaseModel):
        model: str = "qwen3-tts"
        input: str
        voice: str = ""
        response_format: str = "wav"  # advisory; output is always WAV
        speed: float = 1.0            # accepted for API parity; not applied
        language: str = "Auto"        # Qwen3-TTS extra: target language hint

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": "qwen3-tts", "object": "model"}]}

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
            audio = await anyio.to_thread.run_sync(synthesize, text, voice, req.language or "Auto")
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
    parser.add_argument("--port", type=int, default=5861)
    parser.add_argument("--voices-dir", required=True)
    parser.add_argument("--model-dir", default=DEFAULT_MODEL_DIR)
    parser.add_argument("--preload", action="store_true", help="load the model at startup instead of first request")
    _args = parser.parse_args()
    Path(_args.voices_dir).mkdir(parents=True, exist_ok=True)

    if _args.preload:
        _get_model()

    import uvicorn
    uvicorn.run(build_app(), host=_args.host, port=_args.port, log_level="info")


if __name__ == "__main__":
    main()
