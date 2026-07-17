"""Cloned-voice auto-routing (2026-07-18).

A cloned voice only exists on a Chatterbox-serving endpoint, but the user's
saved TTS provider is usually Kokoro ('local'). Requesting a cloned voice must
re-route that one synthesis to the Chatterbox endpoint (with its model id) —
otherwise Voice Lab's Test button and read-aloud in "your" voice silently
produce the wrong voice or nothing.
"""
from unittest.mock import patch

import services.tts.tts_service as tts_mod
from services.tts.tts_service import get_tts_service


def _settings(provider="local"):
    return {
        "tts_enabled": True,
        "tts_provider": provider,
        "tts_model": "kokoro",
        "tts_voice": "af_heart",
        "tts_speed": "1",
        "tts_language": "",
    }


def test_cloned_voice_reroutes_to_chatterbox_endpoint():
    svc = get_tts_service()
    calls = {}

    def fake_api(text, endpoint_id, model, voice, speed=1.0):
        calls.update(endpoint=endpoint_id, model=model, voice=voice)
        return b"ID3fakeaudio"

    with patch.object(svc, "_load_settings", return_value=_settings("local")), \
         patch.object(tts_mod, "cloned_voice_catalog", return_value=[{"id": "zac", "label": "zac"}]), \
         patch.object(type(svc), "_find_chatterbox_endpoint", staticmethod(lambda: ("ep-cb", "chatterbox-tts"))), \
         patch.object(svc, "_synthesize_api", side_effect=fake_api):
        out = svc.synthesize("hello", use_cache=False, voice="zac")

    assert out == b"ID3fakeaudio"
    assert calls["endpoint"] == "ep-cb"
    assert calls["model"] == "chatterbox-tts"
    assert calls["voice"] == "zac"


def test_cloned_voice_without_chatterbox_returns_none():
    svc = get_tts_service()
    with patch.object(svc, "_load_settings", return_value=_settings("local")), \
         patch.object(tts_mod, "cloned_voice_catalog", return_value=[{"id": "zac"}]), \
         patch.object(type(svc), "_find_chatterbox_endpoint", staticmethod(lambda: (None, None))):
        assert svc.synthesize("hello", use_cache=False, voice="zac") is None


def test_normal_voice_untouched_by_autoroute():
    svc = get_tts_service()
    with patch.object(svc, "_load_settings", return_value=_settings("local")), \
         patch.object(tts_mod, "cloned_voice_catalog", return_value=[{"id": "zac"}]), \
         patch.object(svc, "_get_kokoro") as gk:
        gk.return_value.available = True
        gk.return_value.synthesize_raw.return_value = b"ID3kokoro"
        out = svc.synthesize("hello", use_cache=False, voice="af_heart")
    assert out == b"ID3kokoro"
    gk.return_value.synthesize_raw.assert_called_once_with("hello", "af_heart")
