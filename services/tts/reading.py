"""Reading answers aloud: whose voice, for whom, and how long it takes to get ready.

Reading aloud is a choice each person makes for themselves:

    the voice     "device" (the phone's or browser's own voice: it starts at once and needs no
                  server), a voice recorded in the Studio, or "" for whatever the house uses
    by itself     whether every answer is read without being asked

A voice recorded in the Studio is the sound of a real person. It belongs to whoever recorded it
and reads only to them until they share it with the household; a voice from before owners were
kept (no record) is the administrators'. The record is ``voices.json`` beside the samples.

A cloned voice can be slower than speech on a server without a graphics card for it: measured on
one, the standard Chatterbox makes a second of speech in about two, and Chatterbox Turbo two seconds
of speech in one. The player on the screen prepares ahead so that the reading does not stop and
start, and to plan that it needs to know the pace. ``note_pace`` keeps the timings of the pieces
actually made for each voice; ``pace_for`` gives back their middle (speech seconds per second of
work; above 1 the voice keeps up with itself).
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEVICE = "device"
# One piece of text is a sentence or two; anything longer is cut to this. The screen sends pieces,
# not whole answers, so that it can start with the first while the rest is still being made.
PIECE_CHARS = 600
# Until any voice of its kind has been timed here: what the slower of the cloning engines managed on
# a server without a graphics card for it. Guessing low costs a few seconds of preparing ahead on the
# first reading; guessing high would start a reading that then stops and starts.
DEFAULT_PACE = 0.55

_lock = threading.Lock()


# ── whose voice ────────────────────────────────────────────────────────────

def _voices_dir() -> Path:
    from src.constants import VOICES_DIR
    return Path(VOICES_DIR)


def _records_path() -> Path:
    return _voices_dir() / "voices.json"


def _records() -> Dict[str, Dict[str, Any]]:
    try:
        with open(_records_path(), encoding="utf-8") as handle:
            found = json.load(handle)
        return found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_records(records: Dict[str, Dict[str, Any]]) -> None:
    path = _records_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".json.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(records, handle, indent=2)
    os.replace(tmp, path)


def record(voice: str) -> Dict[str, Any]:
    """What is kept about a voice: ``{"owner", "shared", "made"}``; {} for one from before owners were kept."""
    found = _records().get(str(voice or ""))
    return dict(found) if isinstance(found, dict) else {}


def claim(voice: str, owner: Optional[str], shared: Optional[bool] = None) -> Dict[str, Any]:
    """Note who recorded a voice (a new recording, or the first person to take charge of an old one)."""
    with _lock:
        records = _records()
        kept = records.get(voice) if isinstance(records.get(voice), dict) else {}
        kept = {"owner": owner or kept.get("owner") or "", "shared": bool(kept.get("shared")) if shared is None else bool(shared),
                "made": kept.get("made") or time.strftime("%Y-%m-%dT%H:%M:%S")}
        records[voice] = kept
        _save_records(records)
        return dict(kept)


def share(voice: str, shared: bool) -> Dict[str, Any]:
    with _lock:
        records = _records()
        kept = records.get(voice) if isinstance(records.get(voice), dict) else {"owner": "", "made": ""}
        kept["shared"] = bool(shared)
        records[voice] = kept
        _save_records(records)
        return dict(kept)


def forget(voice: str) -> None:
    with _lock:
        records = _records()
        if records.pop(voice, None) is not None:
            _save_records(records)
        paces = _paces()
        if paces.pop(voice, None) is not None:          # its timings go with it: another voice may take the name
            _save_paces(paces)
        _spoke_at.pop(voice, None)


def may_use(voice: str, user: Optional[str], is_admin: bool) -> bool:
    """Whether this person may be read to in this voice (and may speak with it in the Studio)."""
    if not user:
        return True                                  # no accounts on this server: one person, their voices
    kept = record(voice)
    if not kept or not kept.get("owner"):
        return bool(is_admin) or bool(kept.get("shared"))
    return kept.get("owner") == user or bool(kept.get("shared"))


def may_change(voice: str, user: Optional[str], is_admin: bool) -> bool:
    """Whether this person may share, replace or delete this voice: its owner, or an administrator."""
    if not user or is_admin:
        return True
    return record(voice).get("owner") == user


def cloned_voices(user: Optional[str], is_admin: bool) -> List[Dict[str, Any]]:
    """The recorded voices this person may use, their own first."""
    from services.tts.tts_service import cloned_voice_catalog
    out = []
    for voice in cloned_voice_catalog():
        name = voice.get("id")
        if not name or not may_use(name, user, is_admin):
            continue
        kept = record(name)
        mine = (not user) or kept.get("owner") == user or (not kept.get("owner") and bool(is_admin))
        out.append({"id": name, "name": name, "label": name, "kind": "recorded", "mine": bool(mine), "shared": bool(kept.get("shared")),
                    "owner": kept.get("owner") or "", "can_change": may_change(name, user, is_admin)})
    out.sort(key=lambda v: (not v["mine"], v["id"]))
    return out


# ── each person's choice ───────────────────────────────────────────────────

def prefs(user: Optional[str]) -> Dict[str, Any]:
    """``{"voice": "", "auto": False}`` unless the person chose otherwise."""
    try:
        from routes.prefs_routes import _load_for_user
        kept = (_load_for_user(user) or {}).get("reading")
    except Exception:
        kept = None
    kept = kept if isinstance(kept, dict) else {}
    return {"voice": str(kept.get("voice") or ""), "auto": bool(kept.get("auto"))}


def save_prefs(user: Optional[str], voice: Optional[str] = None, auto: Optional[bool] = None) -> Dict[str, Any]:
    from routes.prefs_routes import _load_for_user, _save_for_user
    with _lock:
        everything = _load_for_user(user) or {}
        kept = everything.get("reading") if isinstance(everything.get("reading"), dict) else {}
        if voice is not None:
            kept["voice"] = str(voice)[:80]
        if auto is not None:
            kept["auto"] = bool(auto)
        everything["reading"] = kept
        _save_for_user(user, everything)
    return prefs(user)


def house_voice(tts_service) -> str:
    """The voice the administrator set for the house, when the server can speak in it; else ''."""
    try:
        settings = tts_service._load_settings()
        if settings.get("tts_enabled") is False or settings.get("tts_provider") in ("disabled", "browser"):
            return ""
        voice = str(settings.get("tts_voice") or "")
        if voice and (tts_service.available or tts_service.is_routable_cloned_voice(voice)):
            return voice
    except Exception:
        logger.debug("house voice not read", exc_info=True)
    return ""


def server_can_speak(tts_service) -> bool:
    """False when speech is switched off for the whole server (then only a device's own voice is left)."""
    try:
        settings = tts_service._load_settings()
        return settings.get("tts_enabled") is not False and settings.get("tts_provider") != "disabled"
    except Exception:
        return False


def choices(tts_service, user: Optional[str], is_admin: bool) -> List[Dict[str, Any]]:
    """Every voice this person can be read to in, the device's own first."""
    out: List[Dict[str, Any]] = [{"id": DEVICE, "label": "This device's own voice", "kind": "device", "pace": None}]
    if not server_can_speak(tts_service):
        return out
    seen = set()
    for voice in cloned_voices(user, is_admin):
        if not tts_service.is_routable_cloned_voice(voice["id"]):
            continue                                 # recorded, but no engine is there to speak it
        seen.add(voice["id"])
        out.append({**voice, "pace": pace_for(voice["id"])})
    try:
        if tts_service.available:                    # the house's own engine has voices of its own (not recordings)
            for voice in tts_service.list_voices() or []:
                name = voice.get("id")
                if name and name not in seen and not _is_recording(name):
                    out.append({"id": name, "label": voice.get("label") or name, "kind": "built-in", "pace": pace_for(name)})
    except Exception:
        logger.debug("built-in voices not listed", exc_info=True)
    return out


def _is_recording(name: str) -> bool:
    from services.tts.tts_service import cloned_voice_catalog
    return any(v.get("id") == name for v in cloned_voice_catalog())


def resolve(tts_service, user: Optional[str], is_admin: bool, asked: str = "") -> str:
    """The voice to read to this person in: ``"device"`` or a server voice they may use.

    ``asked`` (a preview, or the Studio's "read to me in this") wins when it is allowed; then the
    person's own choice; then the house's voice; then the device.
    """
    allowed = {choice["id"] for choice in choices(tts_service, user, is_admin)}
    for wanted in (str(asked or ""), prefs(user)["voice"]):
        if wanted and wanted in allowed:
            return wanted
    house = house_voice(tts_service)
    if house and house in allowed:
        return house
    return DEVICE


# ── how long it takes ──────────────────────────────────────────────────────

def _pace_path() -> Path:
    from src.constants import DATA_DIR
    return Path(str(DATA_DIR)) / "tts_pace.json"


PACE_SAMPLES = 9            # how many timings are kept for a voice


def _paces() -> Dict[str, List[float]]:
    """The timings kept for each voice (speech seconds per second of work), oldest first."""
    try:
        with open(_pace_path(), encoding="utf-8") as handle:
            found = json.load(handle)
    except (OSError, ValueError):
        return {}
    out: Dict[str, List[float]] = {}
    if isinstance(found, dict):
        for voice, kept in found.items():
            samples = kept if isinstance(kept, list) else [kept]        # an older file kept one number
            try:
                out[str(voice)] = [float(v) for v in samples if float(v) > 0][-PACE_SAMPLES:]
            except (TypeError, ValueError):
                continue
    return out


def _middle(values: List[float]) -> float:
    values = sorted(values)
    half = len(values) // 2
    return values[half] if len(values) % 2 else (values[half - 1] + values[half]) / 2


def _usual_pace(paces: Dict[str, List[float]], voice: str) -> float:
    """What to expect of a voice before it has been timed: what the other voices of its kind have
    managed here lately. Recorded voices are all made by the same engine, so a new recording goes
    as the others do; a built-in voice says nothing about a recorded one, nor the other way round.
    0.55 when there is nothing to go by."""
    try:
        from services.tts.tts_service import cloned_voice_catalog
        recorded = {v.get("id") for v in cloned_voice_catalog()}
    except Exception:
        return DEFAULT_PACE
    latest = [pace for name, samples in paces.items() if name != voice and (name in recorded) == (voice in recorded)
              for pace in samples[-3:]]
    return _middle(latest) if latest else DEFAULT_PACE


def pace_for(voice: str) -> float:
    """Speech seconds made per second of work for this voice, as measured here.

    The middle of the timings kept, not their average: the first piece after the engine has been idle
    includes the seconds it takes to load, and one such timing should not make a voice look twice as
    slow as it is. Until there are five, the usual pace (of the other voices of its kind; 0.55 if
    none has been timed) is counted in twice.
    """
    voice = str(voice or "")
    paces = _paces()
    samples = list(paces.get(voice, []))
    if len(samples) < 5:
        usual = _usual_pace(paces, voice)
        samples += [usual, usual]
    return round(_middle(samples), 2)


def _save_paces(paces: Dict[str, List[float]]) -> None:
    try:
        path = _pace_path()
        tmp = path.with_suffix(f".json.tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(paces, handle)
        os.replace(tmp, path)
    except OSError:
        logger.debug("pace not kept", exc_info=True)


def note_pace(voice: str, speech_seconds: float, took_seconds: float) -> None:
    """Keep the timing of one freshly made piece. A piece under a second of speech, or one that came
    back at once (it was already made), says nothing about the pace and is left out."""
    if not voice or speech_seconds < 1.0 or took_seconds < 0.5:
        return
    with _lock:
        paces = _paces()
        paces[voice] = (paces.get(voice, []) + [round(speech_seconds / took_seconds, 3)])[-PACE_SAMPLES:]
        _save_paces(paces)


# ── having the voice ready ─────────────────────────────────────────────────

# The engine behind a recorded voice is put away after a few idle minutes (the model swapper's
# ``ttl``: 300 s for the voice engines as shipped), and the first piece after that waits five to ten
# seconds for it to load. When the answer on its way is going to be read aloud, the screen says so
# as the question is sent, and the engine loads while the answer is being thought about.
WARM_FOR = 240.0            # seconds after a voice last spoke in which its engine is taken to be loaded still
WARM_WORDS = "Ready."       # what is made to get it loaded (and thrown away)

_spoke_at: Dict[str, float] = {}       # voice -> when anything was last made in it (a piece, or a warm-up)
_warming: set = set()                  # voices being warmed right now


def warm(tts_service, voice: str, wait: bool = False) -> bool:
    """Get the engine behind a recorded voice loaded, unless it spoke a moment ago.

    Returns whether a warm-up was started. Only a recording is warmed: its engine runs on this
    server and loading it costs a few seconds of work, where a built-in voice may be an outside
    service that charges for every word. Nothing is kept or timed: a warm-up says nothing about
    the pace, because most of it is the loading.
    """
    voice = str(voice or "")
    try:
        if not voice or voice == DEVICE or not _is_recording(voice) or not tts_service.is_routable_cloned_voice(voice):
            return False
    except Exception:
        return False
    with _lock:
        if voice in _warming or time.time() - _spoke_at.get(voice, 0.0) < WARM_FOR:
            return False
        _warming.add(voice)

    def work():
        try:
            if tts_service.synthesize(WARM_WORDS, use_cache=False, voice=voice):
                _spoke_at[voice] = time.time()
        except Exception:
            logger.debug("voice not warmed", exc_info=True)
        finally:
            with _lock:
                _warming.discard(voice)

    thread = threading.Thread(target=work, name="voice-warm", daemon=True)
    thread.start()
    if wait:
        thread.join()
    return True


def speech_seconds(audio: bytes) -> float:
    """How long a WAV lasts; 0 for anything else (an MP3 from an outside service — the screen measures it itself)."""
    try:
        with wave.open(io.BytesIO(audio)) as sound:
            return sound.getnframes() / float(sound.getframerate() or 1)
    except Exception:
        return 0.0


def say(tts_service, text: str, voice: str) -> Dict[str, Any]:
    """Make one piece of speech. Returns ``{"audio", "seconds", "took", "fresh"}``; audio is None when it could not be made."""
    words = " ".join(str(text or "").split())[:PIECE_CHARS]
    if not words:
        return {"audio": None, "seconds": 0.0, "took": 0.0, "fresh": False}
    began = time.time()
    audio = tts_service.synthesize(words, voice=voice)
    took = time.time() - began
    seconds = speech_seconds(audio) if audio else 0.0
    fresh = took >= 0.5
    if audio and fresh:
        note_pace(voice, seconds, took)
        _spoke_at[voice] = time.time()
    return {"audio": audio, "seconds": round(seconds, 2), "took": round(took, 2), "fresh": fresh}
