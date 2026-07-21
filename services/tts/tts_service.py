# src/tts_service.py
"""Multi-provider TTS service — dispatches to local Kokoro, OpenAI-compatible API, or browser."""

import io
import wave
import logging
import hashlib
import httpx
from pathlib import Path
from typing import Optional, Dict, Any

from src.constants import TTS_CACHE_DIR

logger = logging.getLogger(__name__)

# Kokoro-82M v1.0 voice catalog (hexgrad/Kokoro-82M voices/). Prefix encodes
# G2P language + gender: a=American b=British, f=female m=male. Grades from
# the model card — the top of each group sounds best.
KOKORO_VOICES = [
    {"id": "af_heart",    "label": "Heart — American female (best)"},
    {"id": "af_bella",    "label": "Bella — American female"},
    {"id": "af_nicole",   "label": "Nicole — American female (whisper)"},
    {"id": "af_aoede",    "label": "Aoede — American female"},
    {"id": "af_kore",     "label": "Kore — American female"},
    {"id": "af_sarah",    "label": "Sarah — American female"},
    {"id": "af_nova",     "label": "Nova — American female"},
    {"id": "af_alloy",    "label": "Alloy — American female"},
    {"id": "af_sky",      "label": "Sky — American female"},
    {"id": "af_jessica",  "label": "Jessica — American female"},
    {"id": "af_river",    "label": "River — American female"},
    {"id": "am_michael",  "label": "Michael — American male"},
    {"id": "am_fenrir",   "label": "Fenrir — American male"},
    {"id": "am_puck",     "label": "Puck — American male"},
    {"id": "am_echo",     "label": "Echo — American male"},
    {"id": "am_eric",     "label": "Eric — American male"},
    {"id": "am_liam",     "label": "Liam — American male"},
    {"id": "am_onyx",     "label": "Onyx — American male"},
    {"id": "am_adam",     "label": "Adam — American male"},
    {"id": "am_santa",    "label": "Santa — American male (ho ho)"},
    {"id": "bf_emma",     "label": "Emma — British female"},
    {"id": "bf_isabella", "label": "Isabella — British female"},
    {"id": "bf_alice",    "label": "Alice — British female"},
    {"id": "bf_lily",     "label": "Lily — British female"},
    {"id": "bm_george",   "label": "George — British male"},
    {"id": "bm_fable",    "label": "Fable — British male"},
    {"id": "bm_lewis",    "label": "Lewis — British male"},
    {"id": "bm_daniel",   "label": "Daniel — British male"},
]

# The OpenAI-compatible /audio/speech voice set (endpoint provider).
OPENAI_VOICES = [
    {"id": v, "label": v} for v in
    ("alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer")
]

CLONED_SAMPLE_EXTS = (".wav", ".mp3", ".flac", ".ogg")

# Model ids that speak the user's cloned voices. Chatterbox came first; the
# Qwen3-TTS engine serves the same samples faster. Substring match — llama-swap
# ids are exact, but API providers may prefix/namespace theirs.
CLONE_CAPABLE_TTS_MODELS = ("chatterbox", "qwen3-tts")


def _is_clone_capable_model(model_id) -> bool:
    m = str(model_id or "").lower()
    return any(marker in m for marker in CLONE_CAPABLE_TTS_MODELS)


def cloned_voice_catalog() -> list:
    """The user's cloned voices — one per sample file in VOICES_DIR (the same
    directory the Chatterbox engine server reads)."""
    from src.constants import VOICES_DIR
    seen = []
    try:
        for p in sorted(Path(VOICES_DIR).glob("*")):
            if p.suffix.lower() in CLONED_SAMPLE_EXTS:
                seen.append({"id": p.stem, "label": f"{p.stem} — your cloned voice"})
    except Exception:
        pass
    return seen


def _safe_speed(value, default: float = 1.0) -> float:
    """Parse the stored tts_speed defensively. The settings layer tolerates
    corrupt/agent-written config, so a non-numeric or empty value (e.g. an agent
    setting "speech speed" = "fast", or a hand-edited settings.json) must not
    crash synthesis or the stats endpoint with a ValueError."""
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return default
    return speed if speed > 0 else default


class TTSService:
    """Multi-provider TTS service.

    Reads provider config from data/settings.json on each call.
    Providers:
      "disabled"        — no TTS
      "browser"         — client-side Web Speech API (no server synthesis)
      "local"           — Kokoro-82M on GPU
      "endpoint:<id>"   — OpenAI-compatible /audio/speech via ModelEndpoint
    """

    def __init__(self, cache_dir: str = TTS_CACHE_DIR):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._kokoro = None  # lazy-init

    # ── Settings ──

    def _load_settings(self) -> dict:
        from src.settings import load_settings
        saved = load_settings()
        return {
            "tts_enabled": saved.get("tts_enabled", True),
            "tts_provider": saved.get("tts_provider", "disabled"),
            "tts_model": saved.get("tts_model", "tts-1"),
            "tts_voice": saved.get("tts_voice", "alloy"),
            "tts_speed": saved.get("tts_speed", "1"),
        }

    @property
    def available(self) -> bool:
        settings = self._load_settings()
        if settings.get("tts_enabled") is False:
            return False
        provider = settings["tts_provider"]
        if provider == "disabled":
            return False
        if provider == "browser":
            return True  # handled client-side
        if provider == "local":
            kokoro = self._get_kokoro()
            return kokoro is not None and kokoro.available
        if provider.startswith("endpoint:"):
            return True  # assume reachable; errors surface at synthesis time
        return False

    # ── Cache ──

    def _cache_key(self, text: str, provider: str, model: str, voice: str, speed: float = 1.0) -> str:
        raw = f"{provider}|{model}|{voice}|{speed}|{text}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _get_cached(self, key: str) -> Optional[bytes]:
        for ext in (".mp3", ".wav"):
            path = self.cache_dir / f"{key}{ext}"
            if path.exists():
                return path.read_bytes()
        return None

    def _put_cache(self, key: str, data: bytes):
        ext = ".mp3" if (len(data) >= 3 and (data[:3] == b'ID3' or (data[0] == 0xff and (data[1] & 0xe0) == 0xe0))) else ".wav"
        (self.cache_dir / f"{key}{ext}").write_bytes(data)

    def clear_cache(self):
        count = 0
        for f in self.cache_dir.glob("*.*"):
            f.unlink()
            count += 1
        logger.info(f"Cleared {count} cached TTS files")

    # ── Kokoro (local) ──

    def _get_kokoro(self):
        if self._kokoro is None:
            self._kokoro = _KokoroPipeline()
        return self._kokoro

    # ── API endpoint ──

    def _synthesize_api(self, text: str, endpoint_id: str, model: str, voice: str, speed: float = 1.0) -> Optional[bytes]:
        from src.database import SessionLocal, ModelEndpoint

        db = SessionLocal()
        try:
            ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
            if not ep:
                logger.error(f"TTS endpoint {endpoint_id} not found")
                return None
            base_url = ep.base_url.rstrip("/")
            api_key = ep.api_key
        finally:
            db.close()

        url = base_url + "/audio/speech"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "input": text,
            "voice": voice,
            "response_format": "mp3",
            "speed": speed,
        }

        # Local engine endpoints (llama-swap) may cold-load a multi-GB model
        # inside the first request — 60s cuts a legitimate first synthesis off.
        is_local = any(h in base_url for h in ("127.0.0.1", "localhost", "0.0.0.0"))
        if is_local:
            # A voice request would swap the engine's GPU models — never let a
            # play-button click evict a live render. (Kokoro/browser voices
            # are unaffected; they don't touch the GPU engine.)
            try:
                from src import job_queue
                busy = job_queue.gpu_busy()
                if busy:
                    logger.warning("TTS endpoint synthesis refused: GPU busy with %s render", busy.get("kind"))
                    return None
            except Exception:
                pass
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=300 if is_local else 60)
            r.raise_for_status()
            logger.info(f"API TTS: {len(r.content)} bytes from {base_url}")
            return r.content
        except Exception as e:
            logger.error(f"API TTS synthesis failed: {e}")
            return None

    # ── Public interface ──

    def synthesize(self, text: str, use_cache: bool = True, voice: Optional[str] = None) -> Optional[bytes]:
        settings = self._load_settings()
        if settings.get("tts_enabled") is False:
            return None
        provider = settings["tts_provider"]
        model = settings["tts_model"]
        # Explicit voice (the settings picker's preview button) wins over the
        # saved setting.
        voice = (voice or "").strip() or settings["tts_voice"]
        speed = _safe_speed(settings.get("tts_speed", "1"))

        # Cloned voices exist only on a cloning endpoint (Chatterbox or
        # Qwen3-TTS). If the requested voice is one of the user's clones and
        # the saved provider is something else (Kokoro, browser, another
        # endpoint), auto-route this request — so testing or reading aloud in
        # "your" voice Just Works without flipping providers in Settings first.
        if voice and any(v.get("id") == voice for v in cloned_voice_catalog()):
            already_cb = provider.startswith("endpoint:") and \
                self._endpoint_serves_chatterbox(provider.split(":", 1)[1])
            if not already_cb:
                cb_ep, cb_model = self._find_chatterbox_endpoint()
                if cb_ep:
                    provider, model = f"endpoint:{cb_ep}", cb_model
                    logger.info(f"TTS: cloned voice '{voice}' — auto-routing to Chatterbox endpoint {cb_ep}")
                else:
                    logger.warning(f"TTS: cloned voice '{voice}' requested but no Chatterbox endpoint is serving")
                    return None

        if provider in ("disabled", "browser"):
            return None

        if len(text) > 5000:
            text = text[:5000]

        if use_cache:
            key = self._cache_key(text, provider, model, voice, speed)
            cached = self._get_cached(key)
            if cached:
                logger.info(f"TTS cache hit ({len(text)} chars)")
                return cached

        audio_data = None

        if provider == "local":
            kokoro = self._get_kokoro()
            if kokoro and kokoro.available:
                audio_data = kokoro.synthesize_raw(text, voice)
            else:
                logger.warning("Kokoro TTS not available")
                return None
        elif provider.startswith("endpoint:"):
            endpoint_id = provider.split(":", 1)[1]
            audio_data = self._synthesize_api(text, endpoint_id, model, voice, speed)
        else:
            logger.error(f"Unknown TTS provider: {provider}")
            return None

        if audio_data and use_cache:
            key = self._cache_key(text, provider, model, voice, speed)
            self._put_cache(key, audio_data)

        return audio_data

    def synthesize_to_base64(self, text: str, voice: Optional[str] = None) -> Optional[str]:
        import base64
        audio = self.synthesize(text, voice=voice)
        if audio:
            return base64.b64encode(audio).decode("utf-8")
        return None

    def list_voices(self, provider: Optional[str] = None) -> list:
        """Voice catalog for a provider (default: the active one)."""
        provider = provider or self._load_settings()["tts_provider"]
        if provider == "local":
            return self._with_cloned(KOKORO_VOICES)
        if provider.startswith("endpoint:"):
            # A Chatterbox-serving endpoint speaks in the user's own cloned
            # voices, not the OpenAI names.
            if self._endpoint_serves_chatterbox(provider.split(":", 1)[1]):
                return cloned_voice_catalog()
            # Append rather than prepend: the settings picker falls back to
            # the FIRST entry when switching providers, and defaulting a
            # just-chosen API endpoint to a clone would silently auto-route
            # every synthesis away from that endpoint.
            return self._with_cloned(OPENAI_VOICES, prepend=False)
        # Browser voices enumerate client-side, and read-aloud there never
        # reaches the server — a cloned id would silently fall back to the OS
        # default voice, so don't offer clones. Disabled has none.
        return []

    def _with_cloned(self, base: list, prepend: bool = True) -> list:
        """Cloned voices merged into a provider's catalog. synthesize()
        auto-routes any cloned-voice id to the Chatterbox endpoint whatever
        the saved provider, so the picker should offer them wherever server
        synthesis happens — but only while an endpoint actually serves
        Chatterbox (otherwise the entry would promise a voice that errors).
        The router matches by id, so a cloned id shadows a same-named
        built-in; drop the shadowed entry rather than list it unreachable."""
        cloned = cloned_voice_catalog()
        if not cloned or not self._find_chatterbox_endpoint()[0]:
            return base
        cloned_ids = {v["id"] for v in cloned}
        rest = [v for v in base if v["id"] not in cloned_ids]
        return cloned + rest if prepend else rest + cloned

    def is_routable_cloned_voice(self, voice: Optional[str]) -> bool:
        """True when `voice` is a cloned sample that synthesize() can
        auto-route to a Chatterbox endpoint even though the saved provider
        can't speak on its own (e.g. 'local' without the optional kokoro
        package). The global off switches still win — this must never
        resurrect disabled TTS."""
        settings = self._load_settings()
        if settings.get("tts_enabled") is False or settings["tts_provider"] == "disabled":
            return False
        voice = (voice or "").strip()
        if not voice or all(v.get("id") != voice for v in cloned_voice_catalog()):
            return False
        return bool(self._find_chatterbox_endpoint()[0])

    @staticmethod
    def _endpoint_serves_chatterbox(endpoint_id: str) -> bool:
        """True when the endpoint serves any voice-cloning model (Chatterbox
        or Qwen3-TTS) — the name is historical; other modules import it."""
        # Retry once: a transient DB error (e.g. sqlite locked during app
        # startup) must not silently demote a cloned voice to "not serving" —
        # that surfaced as opaque 500s on the chat play button (2026-07-17).
        import time as _time
        for attempt in (1, 2):
            try:
                import json
                from core.database import SessionLocal, ModelEndpoint
                db = SessionLocal()
                try:
                    ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
                    cached = json.loads(getattr(ep, "cached_models", None) or "[]") if ep else []
                finally:
                    db.close()
                return any(_is_clone_capable_model(m) for m in cached)
            except Exception:
                logger.warning("Chatterbox endpoint check failed (attempt %d)", attempt, exc_info=True)
                _time.sleep(0.25)
        return False

    @staticmethod
    def _find_chatterbox_endpoint():
        """(endpoint_id, model_id) of the best enabled endpoint serving a
        voice-cloning model (Chatterbox or Qwen3-TTS), or (None, None). Used
        to auto-route cloned voices. The name is historical; other modules
        import it. When several clone-capable models are served, the user's
        saved endpoint/model choice wins (exact match), then the first
        marker hit in endpoint order — so picking 'qwen3-tts' in Settings
        actually routes clones through Qwen rather than whichever engine the
        DB scan happens to hit first."""
        import time as _time
        for attempt in (1, 2):
            try:
                import json
                from core.database import SessionLocal, ModelEndpoint

                preferred_ep = preferred_model = None
                try:
                    from src.settings import load_settings
                    saved = load_settings()
                    prov = str(saved.get("tts_provider") or "")
                    if prov.startswith("endpoint:"):
                        preferred_ep = prov.split(":", 1)[1]
                    saved_model = str(saved.get("tts_model") or "").strip().lower()
                    if _is_clone_capable_model(saved_model):
                        preferred_model = saved_model
                except Exception:
                    pass

                db = SessionLocal()
                try:
                    best = None  # ((model_rank, endpoint_rank), endpoint_id, model_id)
                    for ep in db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all():  # noqa: E712
                        cached = json.loads(getattr(ep, "cached_models", None) or "[]") or []
                        for m in cached:
                            if not _is_clone_capable_model(m):
                                continue
                            rank = (
                                0 if str(m).lower() == preferred_model else 1,
                                0 if ep.id == preferred_ep else 1,
                            )
                            if best is None or rank < best[0]:
                                best = (rank, ep.id, str(m))
                    if best:
                        return best[1], best[2]
                    # Query succeeded and genuinely found nothing — no retry.
                    return None, None
                finally:
                    db.close()
            except Exception:
                logger.warning("Chatterbox endpoint lookup failed (attempt %d)", attempt, exc_info=True)
                _time.sleep(0.25)
        return None, None

    def set_voice(self, voice: str):
        """Legacy no-op — voice is now managed via admin settings."""

    def get_stats(self) -> Dict[str, Any]:
        settings = self._load_settings()
        provider = settings["tts_provider"]
        tts_enabled = settings.get("tts_enabled", True)

        cache_files = list(self.cache_dir.glob("*.wav")) + list(self.cache_dir.glob("*.mp3"))
        cache_size = sum(f.stat().st_size for f in cache_files)

        is_available = self.available and tts_enabled
        stats = {
            "available": is_available,
            "ready": is_available,
            "provider": provider,
            "model": settings["tts_model"],
            "voice": settings["tts_voice"],
            "speed": _safe_speed(settings.get("tts_speed", "1")),
            "cache_entries": len(cache_files),
            "cache_size_mb": round(cache_size / (1024 * 1024), 2),
        }

        if provider == "local":
            kokoro = self._get_kokoro()
            stats["model"] = "Kokoro-82M (GPU)" if (kokoro and kokoro.available) else "Kokoro (not loaded)"
        elif provider == "browser":
            stats["model"] = "Browser (Web Speech API)"
        elif provider.startswith("endpoint:"):
            stats["endpoint_id"] = provider.split(":", 1)[1]

        return stats


class _KokoroPipeline:
    """Encapsulates the Kokoro-82M local pipeline (GPU when available, else
    CPU — 82M params synthesizes faster than realtime on CPU, and CPU keeps
    the GPU free for image/video renders; speech is bursty and tiny)."""

    def __init__(self):
        self.pipeline = None       # the American-English pipeline (default)
        self._pipes = {}           # lang code -> KPipeline ('a'=US, 'b'=UK)
        self.available = False
        self.device = None
        self._init()

    def _make_pipe(self, lang_code: str):
        import torch
        from kokoro import KPipeline

        if self.device.type == "cuda":
            with torch.cuda.device(0):
                pipe = KPipeline(lang_code=lang_code)
                if hasattr(pipe, "model"):
                    pipe.model = pipe.model.to(self.device)
        else:
            pipe = KPipeline(lang_code=lang_code)
        return pipe

    def _init(self):
        try:
            import torch

            self.device = torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")
            self.pipeline = self._make_pipe("a")
            self._pipes["a"] = self.pipeline
            self.available = True
            logger.info(f"Kokoro-82M TTS pipeline loaded ({'GPU' if self.device.type == 'cuda' else 'CPU'})")
        except ImportError as e:
            logger.warning(f"Kokoro TTS not available: {e}")
            logger.warning("Install with: pip install kokoro soundfile")
        except Exception as e:
            logger.error(f"Kokoro init failed: {e}", exc_info=True)

    def _pipe_for(self, voice: str):
        """British voices (bf_*/bm_*) need the 'b' G2P pipeline or they read
        with American phonemes; anything else uses the default American one."""
        lang = "b" if str(voice or "").lower().startswith(("bf_", "bm_")) else "a"
        pipe = self._pipes.get(lang)
        if pipe is None:
            pipe = self._make_pipe(lang)
            self._pipes[lang] = pipe
        return pipe

    def synthesize_raw(self, text: str, voice: str = "af_heart") -> Optional[bytes]:
        if not self.available:
            return None
        try:
            import contextlib
            import torch
            import numpy as np

            pipeline = self._pipe_for(voice)
            ctx = torch.cuda.device(self.device) if self.device.type == "cuda" else contextlib.nullcontext()
            with ctx:
                chunks = []
                for _, _, audio in pipeline(text, voice=voice):
                    chunks.append(audio)

            if not chunks:
                return None

            full = np.concatenate(chunks)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes((full * 32767).astype(np.int16).tobytes())
            return buf.getvalue()
        except Exception as e:
            logger.error(f"Kokoro synthesis failed: {e}", exc_info=True)
            return None


# Module-level singleton
_tts_service = None

def get_tts_service() -> TTSService:
    global _tts_service
    if _tts_service is None:
        _tts_service = TTSService()
    return _tts_service
