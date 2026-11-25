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

Two models speak the same voices (--model):

  standard   Chatterbox, the original. Fetched on first use.
  turbo      Chatterbox Turbo (ResembleAI/chatterbox-turbo, MIT, ~4 GB): a
             smaller token model and a two-step decoder. Fetched on first use
             when asked for by name.
  auto       (the default) Turbo when its files are already in the Hugging
             Face cache, otherwise the standard model. Nothing is fetched to
             decide.

On a processor, Turbo is run with one change that makes it faster than speech
(see RowAtATime below): measured on a 16-core machine, the standard model made
0.57 s of speech per second, Turbo 0.72, and Turbo with the change 2.4. The
change is tried on one layer when the model is loaded and kept only where it
is both quicker and agrees with the exact arithmetic; it is not made on a GPU,
and --exact switches it off.

Every piece is brought to the same loudness before it is sent (--loudness, in
LUFS; -22 by default). Turbo levels the reference recording down before it
reads the voice from it and then speaks at about -28 LUFS, half as loud as the
standard model does from the same recording (-22); with the levelling the two
sound alike, and one sentence is as loud as the next.

Run:  python server.py --port 5860 --voices-dir C:/path/to/voices
"""

import argparse
import io
import logging
import math
import os
import re
import threading
import time
from pathlib import Path

# Both models draw a progress bar for every sentence; behind llama-swap that is
# forty lines of log per sentence and nobody is watching.
os.environ.setdefault("TQDM_DISABLE", "1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chatterbox-server")

SAMPLE_EXTS = (".wav", ".mp3", ".flac", ".ogg")
MAX_CHUNK_CHARS = 400  # well under the ~40s/1000-token ceiling at speech pace

TURBO_REPO = "ResembleAI/chatterbox-turbo"
TURBO_FILES = ("t3_turbo_v1.safetensors", "s3gen_meanflow.safetensors", "ve.safetensors")
TURBO_PATTERNS = ["*.safetensors", "*.json", "*.txt", "*.pt", "*.model"]   # what the package itself asks for
# Each model bakes a voice its own way; the baked file sits beside the sample.
BAKED_SUFFIX = {"standard": ".voice.pt", "turbo": ".turbo.pt"}
MIN_TURBO_SAMPLE_SECONDS = 5.5   # Turbo refuses a reference clip of five seconds or less

# Loudness: a piece is never turned up so far that its loudest moment would clip, nor by more than
# MAX_GAIN_DB (a piece that is nearly silence is not turned into hiss).
PEAK_CEILING = 0.97
MAX_GAIN_DB = 15.0

# RowAtATime is kept only if, on this machine, it is at least this much quicker than the exact
# product and its result is within this much of it (relative to the largest value).
ROW_SPEEDUP_NEEDED = 1.5
ROW_ERROR_ALLOWED = 0.01

_model = None
_model_kind = None         # "standard" or "turbo": which one _model is
_fast_rows = 0             # how many layers of the token model were changed to RowAtATime
_fast_rows_why = ""        # one line: what the try-out measured, or why the change was not made
_model_lock = threading.Lock()
_active_voice = None       # (name, sample mtime) currently loaded — the mtime
                           # makes a re-recorded sample invalidate in-memory conds
_args = None


def _voices_dir() -> Path:
    return Path(_args.voices_dir)


def list_voices():
    seen = {}
    for p in sorted(_voices_dir().glob("*")):
        if p.suffix.lower() in SAMPLE_EXTS and not p.name.endswith(".pt"):
            seen.setdefault(p.stem, p)
    return seen  # name -> sample path


def turbo_files():
    """The folder holding Turbo's files when they are already on this machine, else None. Never fetches."""
    try:
        from huggingface_hub import snapshot_download
        path = Path(snapshot_download(repo_id=TURBO_REPO, local_files_only=True, allow_patterns=TURBO_PATTERNS))
    except Exception:
        return None
    return path if all((path / name).exists() for name in TURBO_FILES) else None


def which_model(asked: str, have_turbo: bool) -> str:
    """The model to load for --model: "standard" or "turbo". "auto" is Turbo when it is already here."""
    asked = (asked or "auto").lower()
    if asked in ("standard", "turbo"):
        return asked
    return "turbo" if have_turbo else "standard"


def make_row_at_a_time():
    """The class is built on first use so that importing this file does not need torch."""
    import torch

    class RowAtATime(torch.nn.Module):
        """GPT-2's Conv1D (y = x W + b) for the token loop.

        Speech is made one token at a time, and every token is one row pushed
        through 96 of these layers: a single row times a matrix of a few million
        numbers. In single precision that product runs on ONE core here whatever
        the thread count (measured: 0.44 ms each, 76% of all the time spent), so
        the model made 20 tokens a second where speech needs 25. The same product
        through the matrix-times-vector routine with the weights held in half
        precision is spread across the cores and is seven times quicker (175
        tokens a second for the products alone, 75 for the whole loop).

        What it costs: the weights are rounded to half precision for these
        products (the result goes straight back to single). Measured against the
        exact arithmetic over one sentence, with the same past at every step: the
        likeliest next token was the same in 245 of 245 steps, and the chance of
        any other choice moved by 0.03% on average. Anything longer than one row
        (the first pass over the prompt) is computed exactly as before, and so is
        any row whose half-precision result is not a finite number (half
        precision stops at 65,504; a value beyond it would otherwise turn the
        rest of the sentence into noise).
        """

        def __init__(self, conv):
            super().__init__()
            self.nf = conv.nf
            self.weight = conv.weight
            self.bias = conv.bias
            self.register_buffer("weight_rows", conv.weight.detach().t().contiguous().half(), persistent=False)
            self.exact_rows = 0          # rows redone exactly because half precision overflowed

        def exact(self, x):
            out = torch.addmm(self.bias, x.reshape(-1, x.size(-1)), self.weight)
            return out.view(*x.shape[:-1], self.nf)

        def forward(self, x):
            if x.numel() != x.shape[-1]:
                return self.exact(x)
            y = torch.mv(self.weight_rows, x.reshape(-1).half()).float()
            if not bool(torch.isfinite(y).all()):
                self.exact_rows += 1
                return self.exact(x)
            return (y + self.bias).view(*x.shape[:-1], self.nf)

    return RowAtATime


def try_row_at_a_time(conv, rounds: int = 30):
    """Is RowAtATime worth having on this machine? Tries it on one layer.

    Returns ``(keep, speedup, error, line)``. The half-precision product is quick only where this
    build of PyTorch spreads it over the cores; where it does not it can be a hundred times SLOWER
    than what it replaces, so the change is measured, not assumed.
    """
    import torch
    fast = make_row_at_a_time()(conv)
    with torch.inference_mode():
        row = torch.randn(1, 1, conv.weight.shape[0], dtype=conv.weight.dtype)
        want = fast.exact(row)
        got = fast(row)
        error = float((got - want).abs().max() / (want.abs().max() + 1e-12))
        timings = []
        for run in (fast.exact, fast):
            run(row)                                 # once to warm up
            began = time.perf_counter()
            for _ in range(rounds):
                run(row)
            timings.append((time.perf_counter() - began) / rounds)
    speedup = timings[0] / max(timings[1], 1e-9)
    keep = speedup >= ROW_SPEEDUP_NEEDED and error <= ROW_ERROR_ALLOWED
    line = (f"one row through the widest layer: {timings[0] * 1e6:.0f} µs exactly, {timings[1] * 1e6:.0f} µs a row at a time "
            f"({speedup:.1f}x), difference {error:.1e}: {'kept' if keep else 'not kept'}")
    return keep, speedup, error, line


def speed_up_token_loop(model):
    """Swap the token model's Conv1D layers for RowAtATime, if a try-out on its widest layer says it
    helps here. Returns ``(how many were changed, one line saying what was measured)``."""
    from transformers.pytorch_utils import Conv1D
    places = [(parent, name, child) for parent in list(model.t3.tfmr.modules())
              for name, child in list(parent.named_children()) if isinstance(child, Conv1D)]
    if not places:
        return 0, "no layers of the expected kind in the token model"
    widest = max((child for _, _, child in places), key=lambda conv: conv.weight.numel())
    keep, _, _, line = try_row_at_a_time(widest)
    if not keep:
        return 0, line
    RowAtATime = make_row_at_a_time()
    for parent, name, child in places:
        setattr(parent, name, RowAtATime(child))
    return len(places), line


def rows_redone_exactly(model) -> int:
    """How many rows RowAtATime has had to compute the exact way since the model was loaded (0 is expected)."""
    try:
        return sum(int(getattr(m, "exact_rows", 0)) for m in model.t3.tfmr.modules())
    except Exception:
        return 0


def _level_in_single_precision(model) -> None:
    """Turbo levels the loudness of a reference clip before reading the voice from it. With NumPy 2 that
    turns the clip into double precision (a numpy scalar keeps its type), and the tokenizer then refuses
    it: "expected scalar type Double but found Float". The same levelling, handed on as float32."""
    import numpy as np
    level = model.norm_loudness

    def levelled(wav, sr, target_lufs=-27):
        return np.asarray(level(wav, sr, target_lufs), dtype=np.float32)

    model.norm_loudness = levelled


def _load_turbo(device: str):
    global _fast_rows, _fast_rows_why
    from chatterbox.tts_turbo import ChatterboxTurboTTS
    here = turbo_files()
    model = ChatterboxTurboTTS.from_local(here, device) if here else ChatterboxTurboTTS.from_pretrained(device=device)
    _level_in_single_precision(model)
    _fast_rows, _fast_rows_why = 0, "on a graphics card" if device != "cpu" else "switched off (--exact)"
    if device == "cpu" and not _args.exact:
        try:
            _fast_rows, _fast_rows_why = speed_up_token_loop(model)
        except Exception as exc:                     # the model still speaks, only slower
            _fast_rows, _fast_rows_why = 0, f"could not be tried: {exc}"
            logger.warning("Token loop left as it is", exc_info=True)
    return model


def _get_model():
    global _model, _model_kind
    if _model is None:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        kind = which_model(_args.model, turbo_files() is not None)
        if kind == "turbo":
            try:
                logger.info("Loading Chatterbox Turbo (device=%s)…", device)
                _model = _load_turbo(device)
                logger.info("Chatterbox Turbo ready (sr=%s). Token loop: %s layers a row at a time — %s", _model.sr, _fast_rows, _fast_rows_why)
            except Exception:
                # Speech must not stop working because the faster model would not load.
                logger.exception("Chatterbox Turbo could not be loaded; using the standard model")
                _model, kind = None, "standard"
        if _model is None:
            from chatterbox.tts import ChatterboxTTS
            logger.info("Loading Chatterbox (device=%s)…", device)
            _model = ChatterboxTTS.from_pretrained(device=device)
            logger.info("Chatterbox ready (sr=%s)", _model.sr)
        _model_kind = kind
    return _model


def _long_enough_for_turbo(sample: Path) -> str:
    """Turbo needs more than five seconds of reference. A shorter sample is played twice over (or more)
    into a temporary file and that is used instead; returns the path to read the voice from."""
    import soundfile as sf
    try:
        info = sf.info(str(sample))
        seconds = info.frames / float(info.samplerate or 1)
    except Exception:
        return str(sample)                      # not a format soundfile reads: let the model try it as it is
    if seconds >= MIN_TURBO_SAMPLE_SECONDS or seconds <= 0:
        return str(sample)
    import tempfile
    import numpy as np
    data, rate = sf.read(str(sample), dtype="float32", always_2d=False)
    repeats = int(MIN_TURBO_SAMPLE_SECONDS // seconds) + 1
    longer = np.concatenate([data] * repeats)
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    sf.write(tmp.name, longer, rate)
    logger.info("Sample %s is %.1fs; repeated %sx for Turbo", sample.name, seconds, repeats)
    return tmp.name


def _prepare_voice(model, name: str, sample: Path):
    """Load or bake the voice conditionals for `name`."""
    global _active_voice
    sample_mtime = sample.stat().st_mtime
    if _active_voice == (name, sample_mtime) and model.conds is not None:
        return
    turbo = _model_kind == "turbo"
    if turbo:
        from chatterbox.tts_turbo import Conditionals
    else:
        from chatterbox.tts import Conditionals
    baked = sample.with_suffix(sample.suffix + BAKED_SUFFIX[_model_kind or "standard"])  # <name>.wav.voice.pt / .turbo.pt
    if baked.exists() and baked.stat().st_mtime >= sample.stat().st_mtime:
        model.conds = Conditionals.load(baked, map_location=model.device)
        # Conditionals.load may leave tensors on CPU depending on version —
        # push to the model device defensively.
        try:
            model.conds = model.conds.to(model.device)
        except Exception:
            pass
    else:
        if turbo:
            source = _long_enough_for_turbo(sample)
            try:
                model.prepare_conditionals(source)
            finally:
                if source != str(sample):
                    try:
                        Path(source).unlink()
                    except OSError:
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


def loudness_of(samples, rate: int):
    """How loud a piece of speech sounds, in LUFS (ITU-R BS.1770: pauses are left out), or None for
    silence. A piece under half a second, or an install without the meter, is judged by its plain
    level instead, which reads about a decibel low when there are pauses in it."""
    import numpy as np
    x = np.asarray(samples, dtype=np.float64).reshape(-1)
    if x.size == 0 or float(np.abs(x).max()) < 1e-4:
        return None
    if x.size >= int(rate * 0.5):
        try:
            import pyloudnorm
            value = float(pyloudnorm.Meter(rate).integrated_loudness(x))
            if math.isfinite(value):
                return value
        except Exception:
            pass
    mean_square = float(np.mean(x ** 2))
    return -0.691 + 10.0 * math.log10(mean_square) if mean_square > 0 else None


def level(samples, rate: int, target):
    """One piece at the wanted loudness: ``(samples, the gain applied in dB)``.

    A plain change of volume, the same for the whole piece, so nothing about the voice changes. It is
    held back where the loudest moment would otherwise clip. ``target`` of None or 0 and above leaves
    the piece as the model made it.
    """
    import numpy as np
    x = np.asarray(samples, dtype=np.float32)
    if target is None or target >= 0:
        return x, 0.0
    loud = loudness_of(x, rate)
    if loud is None:
        return x, 0.0
    peak = float(np.abs(x).max())
    gain = 10.0 ** (min(float(target) - loud, MAX_GAIN_DB) / 20.0)
    gain = min(gain, PEAK_CEILING / peak)
    return (x * np.float32(gain)), 20.0 * math.log10(gain)


def _write_wav(buf, wav, sr: int) -> None:
    """Write a (1, N) float tensor as 16-bit PCM WAV.

    soundfile first: torchaudio >= 2.9 routes save() through torchcodec,
    which this venv does not ship — synthesis succeeded and the request still
    died with "TorchCodec is required". torchaudio stays as the fallback for
    installs without soundfile."""
    try:
        import soundfile as sf
    except ImportError:
        import torchaudio
        torchaudio.save(buf, wav, sr, format="wav")
        return
    sf.write(buf, wav.squeeze(0).numpy(), sr, format="WAV", subtype="PCM_16")


def synthesize(text: str, voice: str) -> bytes:
    import torch

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
        full = torch.cat(waves, dim=1).cpu()
        levelled, gain_db = level(full.squeeze(0).numpy(), model.sr, getattr(_args, "loudness", None))
        if gain_db:
            full = torch.from_numpy(levelled).unsqueeze(0)
        buf = io.BytesIO()
        _write_wav(buf, full, model.sr)
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
        # "model" is None until the first request has loaded one.
        return {"ok": True, "model": _model_kind, "row_at_a_time_layers": _fast_rows, "row_at_a_time": _fast_rows_why,
                "rows_redone_exactly": rows_redone_exactly(_model) if _fast_rows else 0}

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
    parser.add_argument("--model", choices=("auto", "standard", "turbo"), default="auto",
                        help="which Chatterbox to speak with; auto = Turbo when its files are already on this machine")
    parser.add_argument("--exact", action="store_true",
                        help="Turbo on a processor: do the token loop in single precision throughout (about three times slower)")
    parser.add_argument("--loudness", type=float, default=-22.0,
                        help="how loud every piece is made, in LUFS (default -22, what the standard model gives from a "
                             "recording made in the app); 0 leaves each piece as the model made it")
    _args = parser.parse_args()
    Path(_args.voices_dir).mkdir(parents=True, exist_ok=True)

    if _args.preload:
        _get_model()

    import uvicorn
    uvicorn.run(build_app(), host=_args.host, port=_args.port, log_level="info")


if __name__ == "__main__":
    main()
