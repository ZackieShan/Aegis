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


def test_local_catalog_lists_cloned_voices_first():
    """The picker must show cloned voices even when the saved provider is
    Kokoro — synthesize() auto-routes them, so hiding them from the dropdown
    made 'zac' look nonexistent (2026-07-17 user report)."""
    svc = get_tts_service()
    with patch.object(svc, "_load_settings", return_value=_settings("local")), \
         patch.object(tts_mod, "cloned_voice_catalog", return_value=[{"id": "zac", "label": "zac — your cloned voice"}]), \
         patch.object(type(svc), "_find_chatterbox_endpoint", staticmethod(lambda: ("ep-cb", "chatterbox-tts"))):
        voices = svc.list_voices("local")
    assert voices[0]["id"] == "zac"
    assert any(v["id"] == "af_heart" for v in voices)


def test_local_catalog_hides_clones_without_chatterbox():
    svc = get_tts_service()
    with patch.object(svc, "_load_settings", return_value=_settings("local")), \
         patch.object(tts_mod, "cloned_voice_catalog", return_value=[{"id": "zac", "label": "zac"}]), \
         patch.object(type(svc), "_find_chatterbox_endpoint", staticmethod(lambda: (None, None))):
        voices = svc.list_voices("local")
    assert all(v["id"] != "zac" for v in voices)


def test_is_routable_cloned_voice_gates():
    """The /synthesize 503 gate defers to this: a cloned voice must pass even
    when the saved provider can't speak by itself, but never for non-clones."""
    svc = get_tts_service()
    with patch.object(svc, "_load_settings", return_value=_settings("local")), \
         patch.object(tts_mod, "cloned_voice_catalog", return_value=[{"id": "zac"}]), \
         patch.object(type(svc), "_find_chatterbox_endpoint", staticmethod(lambda: ("ep-cb", "chatterbox-tts"))):
        assert svc.is_routable_cloned_voice("zac") is True
        assert svc.is_routable_cloned_voice("af_heart") is False
        assert svc.is_routable_cloned_voice("") is False
        assert svc.is_routable_cloned_voice(None) is False


def test_is_routable_cloned_voice_respects_off_switches():
    svc = get_tts_service()
    with patch.object(tts_mod, "cloned_voice_catalog", return_value=[{"id": "zac"}]), \
         patch.object(type(svc), "_find_chatterbox_endpoint", staticmethod(lambda: ("ep-cb", "chatterbox-tts"))):
        with patch.object(svc, "_load_settings", return_value=_settings("disabled")):
            assert svc.is_routable_cloned_voice("zac") is False
        off = dict(_settings("local"), tts_enabled=False)
        with patch.object(svc, "_load_settings", return_value=off):
            assert svc.is_routable_cloned_voice("zac") is False
        with patch.object(svc, "_load_settings", return_value=_settings("local")), \
             patch.object(type(svc), "_find_chatterbox_endpoint", staticmethod(lambda: (None, None))):
            assert svc.is_routable_cloned_voice("zac") is False


def test_openai_endpoint_catalog_includes_clones_and_shadows_collisions():
    """A cloned id always wins routing, so a same-named built-in is
    unreachable and must not be listed twice."""
    svc = get_tts_service()
    with patch.object(svc, "_load_settings", return_value=_settings("endpoint:ep-api")), \
         patch.object(tts_mod, "cloned_voice_catalog", return_value=[{"id": "alloy", "label": "alloy — your cloned voice"}]), \
         patch.object(type(svc), "_endpoint_serves_chatterbox", staticmethod(lambda _eid: False)), \
         patch.object(type(svc), "_find_chatterbox_endpoint", staticmethod(lambda: ("ep-cb", "chatterbox-tts"))):
        voices = svc.list_voices("endpoint:ep-api")
    alloys = [v for v in voices if v["id"] == "alloy"]
    assert alloys == [{"id": "alloy", "label": "alloy — your cloned voice"}]
    assert any(v["id"] == "nova" for v in voices)
