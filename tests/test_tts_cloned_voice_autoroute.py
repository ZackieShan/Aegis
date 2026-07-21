"""Cloned-voice auto-routing (2026-07-18).

A cloned voice only exists on a Chatterbox-serving endpoint, but the user's
saved TTS provider is usually Kokoro ('local'). Requesting a cloned voice must
re-route that one synthesis to the Chatterbox endpoint (with its model id) —
otherwise Voice Lab's Test button and read-aloud in "your" voice silently
produce the wrong voice or nothing.
"""
from types import SimpleNamespace
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


def _fake_session_local(endpoints):
    """A SessionLocal stand-in whose queries return `endpoints` — lets the
    REAL _find_chatterbox_endpoint / _endpoint_serves_chatterbox run."""
    class _Query:
        def __init__(self, items):
            self._items = items

        def filter(self, *a, **k):
            return self

        def all(self):
            return self._items

        def first(self):
            return self._items[0] if self._items else None

    class _DB:
        def query(self, *a, **k):
            return _Query(endpoints)

        def close(self):
            pass

    return _DB


def test_qwen3_tts_only_endpoint_satisfies_autoroute():
    """Qwen3-TTS is the second cloning engine (2026-07-17): with ONLY a
    qwen3-tts-serving endpoint in the DB, the real endpoint finder must
    still route a cloned voice there."""
    svc = get_tts_service()
    ep = SimpleNamespace(id="ep-qwen", is_enabled=True, cached_models='["kokoro", "qwen3-tts"]')
    calls = {}

    def fake_api(text, endpoint_id, model, voice, speed=1.0):
        calls.update(endpoint=endpoint_id, model=model, voice=voice)
        return b"RIFFfakeaudio"

    with patch.object(svc, "_load_settings", return_value=_settings("local")), \
         patch.object(tts_mod, "cloned_voice_catalog", return_value=[{"id": "zac", "label": "zac"}]), \
         patch("core.database.SessionLocal", _fake_session_local([ep])), \
         patch.object(svc, "_synthesize_api", side_effect=fake_api):
        out = svc.synthesize("hello", use_cache=False, voice="zac")

    assert out == b"RIFFfakeaudio"
    assert calls["endpoint"] == "ep-qwen"
    assert calls["model"] == "qwen3-tts"
    assert calls["voice"] == "zac"


def test_endpoint_serves_chatterbox_matches_qwen3_tts():
    """The serving check (name is historical) must accept either cloning
    engine's model id."""
    svc = get_tts_service()
    ep = SimpleNamespace(id="ep-qwen", is_enabled=True, cached_models='["qwen3-tts"]')
    with patch("core.database.SessionLocal", _fake_session_local([ep])):
        assert type(svc)._endpoint_serves_chatterbox("ep-qwen") is True
    ep_plain = SimpleNamespace(id="ep-api", is_enabled=True, cached_models='["tts-1"]')
    with patch("core.database.SessionLocal", _fake_session_local([ep_plain])):
        assert type(svc)._endpoint_serves_chatterbox("ep-api") is False


def test_saved_clone_model_wins_over_scan_order():
    """With BOTH engines served, the user's saved tts_model (exact match)
    must decide the route — picking qwen3-tts in Settings routes clones
    through Qwen even though chatterbox-tts scans first."""
    svc = get_tts_service()
    ep = SimpleNamespace(id="ep-swap", is_enabled=True,
                         cached_models='["chatterbox-tts", "qwen3-tts"]')
    with patch("core.database.SessionLocal", _fake_session_local([ep])), \
         patch("src.settings.load_settings",
               return_value={"tts_provider": "local", "tts_model": "qwen3-tts"}):
        assert type(svc)._find_chatterbox_endpoint() == ("ep-swap", "qwen3-tts")
    with patch("core.database.SessionLocal", _fake_session_local([ep])), \
         patch("src.settings.load_settings",
               return_value={"tts_provider": "local", "tts_model": "chatterbox-tts"}):
        assert type(svc)._find_chatterbox_endpoint() == ("ep-swap", "chatterbox-tts")
    # A non-clone saved model falls back to scan order.
    with patch("core.database.SessionLocal", _fake_session_local([ep])), \
         patch("src.settings.load_settings",
               return_value={"tts_provider": "local", "tts_model": "kokoro"}):
        assert type(svc)._find_chatterbox_endpoint() == ("ep-swap", "chatterbox-tts")


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
