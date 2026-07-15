"""Video generation + image-edit wiring (2026-07-12).

Covers: video model-id detection, the gallery endpoint widening that lets an
image model served under an llm-typed aggregator (llama-swap) drive the
editor's inpaint/style routes, the edit-capable model routing gate, local
edit size capping, and the video job finish path (webm on disk + job state).
"""
import asyncio
import base64
import types

import pytest


# ── video model detection ────────────────────────────────────────────────────
def test_is_video_model_positive():
    from src.video_generation import is_video_model
    for mid in ("wan2.2-t2v", "ltx2-video-nsfw", "ltx2-video-sfw", "wan2.2-animate",
                "hunyuan-t2v", "cog-i2v", "some/video-model"):
        assert is_video_model(mid), mid


def test_is_video_model_negative():
    from src.video_generation import is_video_model
    for mid in ("qwen-image", "qwen-image-edit", "qwen3.5-4b-uncensored",
                "wanda-chat", "supergemma4-26b", "qwen2.5-vl-3b", "gpt-image-1"):
        assert not is_video_model(mid), mid


# ── model-id URL-path guard ──────────────────────────────────────────────────
def test_upstream_model_id_charset_guard(monkeypatch):
    import src.video_generation as vg

    def fake_resolve(spec, owner=None):
        return ("http://127.0.0.1:9090/v1/chat/completions", "../evil/model", {})

    monkeypatch.setattr("src.ai_interaction._resolve_model", fake_resolve)
    with pytest.raises(ValueError):
        vg._upstream_root("whatever", None)


def test_upstream_root_strips_chat_suffix(monkeypatch):
    import src.video_generation as vg

    def fake_resolve(spec, owner=None):
        return ("http://127.0.0.1:9090/v1/chat/completions", "wan2.2-t2v", {"h": "1"})

    monkeypatch.setattr("src.ai_interaction._resolve_model", fake_resolve)
    root, mid, headers = vg._upstream_root("wan2.2-t2v", None)
    assert root == "http://127.0.0.1:9090"
    assert mid == "wan2.2-t2v"


# ── gallery endpoint widening ────────────────────────────────────────────────
def test_endpoint_lists_image_model():
    from routes.gallery.gallery_routes import _endpoint_lists_image_model
    ep = types.SimpleNamespace(cached_models='["qwen3-coder-30b", "qwen-image", "qwen-vl"]')
    assert _endpoint_lists_image_model(ep)
    ep_chat_only = types.SimpleNamespace(cached_models='["qwen3-coder-30b", "supergemma4-26b"]')
    assert not _endpoint_lists_image_model(ep_chat_only)
    ep_broken = types.SimpleNamespace(cached_models="not-json")
    assert not _endpoint_lists_image_model(ep_broken)
    ep_none = types.SimpleNamespace(cached_models=None)
    assert not _endpoint_lists_image_model(ep_none)


def test_first_visible_image_endpoint_prefers_owner(monkeypatch):
    import routes.gallery.gallery_routes as gr

    mine = types.SimpleNamespace(owner="me", base_url="http://a")
    other = types.SimpleNamespace(owner=None, base_url="http://b")
    monkeypatch.setattr(gr, "_visible_gallery_endpoints", lambda db, owner: [other, mine])
    assert gr._first_visible_image_endpoint(None, "me") is mine
    assert gr._first_visible_image_endpoint(None, None) is other


def test_visible_image_endpoint_for_base_matches_llm_typed(monkeypatch):
    import routes.gallery.gallery_routes as gr

    swap = types.SimpleNamespace(owner=None, base_url="http://127.0.0.1:9090/v1")
    monkeypatch.setattr(gr, "_visible_gallery_endpoints", lambda db, owner: [swap])
    assert gr._visible_image_endpoint_for_base(None, "http://127.0.0.1:9090", None) is swap
    assert gr._visible_image_endpoint_for_base(None, "http://127.0.0.1:9999", None) is None


# ── edit-capable model gate + size cap ───────────────────────────────────────
def test_edit_model_regex():
    from routes.gallery.gallery_routes import _EDIT_MODEL_ID_RE
    assert _EDIT_MODEL_ID_RE.search("qwen-image-edit")
    assert _EDIT_MODEL_ID_RE.search("sd-xl-inpaint-1.0")
    assert _EDIT_MODEL_ID_RE.search("flux-fill-dev")
    assert not _EDIT_MODEL_ID_RE.search("qwen-image")
    assert not _EDIT_MODEL_ID_RE.search("qwen-image-rapid-nsfw")


def test_local_edit_size_caps_and_keeps_aspect():
    from routes.gallery.gallery_routes import _local_edit_size
    assert _local_edit_size(1536, 1024) == "768x512"
    assert _local_edit_size(1024, 1536) == "512x768"
    assert _local_edit_size(800, 800) == "512x512"
    assert _local_edit_size(None, None) == "512x512"


# ── video job finish path ────────────────────────────────────────────────────
def test_finish_job_writes_video_and_marks_done(tmp_path, monkeypatch):
    import src.video_generation as vg

    monkeypatch.setattr(vg, "GENERATED_IMAGES_DIR", str(tmp_path))

    # The gallery insert may run against the real DB in dev; stub it out so
    # the unit test never touches sqlite.
    class _FakeDb:
        def add(self, row):
            self.row = row

        def commit(self):
            pass

        def close(self):
            pass

    import src.database as sdb
    monkeypatch.setattr(sdb, "SessionLocal", lambda: _FakeDb())

    # Renders are stored as MP4 now: finish_job_bytes transcodes webm/avi via
    # ffmpeg. Mock the transcode — the real binary on garbage bytes would make
    # this "unit" test pass only via the failure fallback, proving nothing.
    import src.video_editing as ve
    monkeypatch.setattr(ve, "transcode_to_mp4", lambda raw, ext: b"mp4-bytes-out")

    job = {
        "id": "j1", "status": "running", "prompt": "a cat", "model": "wan2.2-t2v",
        "owner": "me", "session_id": None, "width": 832, "height": 480,
        "video_frames": 33, "fps": 16,
    }
    payload = b"\x1aE\xdf\xa3fake-webm-bytes"
    vg._finish_job(job, {"output_format": "webm", "b64_json": base64.b64encode(payload).decode()})

    assert job["status"] == "done"
    assert job["video_url"].startswith("/api/generated-image/")
    fname = job["video_url"].rsplit("/", 1)[-1]
    assert fname.endswith(".mp4"), "a webm render must be stored as mp4"
    assert (tmp_path / fname).read_bytes() == b"mp4-bytes-out"


def test_finish_job_keeps_webm_when_transcode_fails(tmp_path, monkeypatch):
    """A failed transcode must never lose the render — original bytes, .webm."""
    import src.video_generation as vg
    import src.video_editing as ve

    monkeypatch.setattr(vg, "GENERATED_IMAGES_DIR", str(tmp_path))
    monkeypatch.setattr(ve, "transcode_to_mp4", lambda raw, ext: None)

    class _FakeDb:
        def add(self, row):
            pass

        def commit(self):
            pass

        def close(self):
            pass

    import src.database as sdb
    monkeypatch.setattr(sdb, "SessionLocal", lambda: _FakeDb())

    job = {
        "id": "j2", "status": "running", "prompt": "a dog", "model": "wan2.2-t2v",
        "owner": "me", "session_id": None, "width": 832, "height": 480,
        "video_frames": 33, "fps": 16,
    }
    payload = b"\x1aE\xdf\xa3fake-webm-bytes"
    vg._finish_job(job, {"output_format": "webm", "b64_json": base64.b64encode(payload).decode()})

    assert job["status"] == "done"
    fname = job["video_url"].rsplit("/", 1)[-1]
    assert fname.endswith(".webm")
    assert (tmp_path / fname).read_bytes() == payload


# ── default video model preference (Settings → AI → Video Generation) ───────
_DETECTED = [
    {"model": "wan2.2-t2v", "endpoint": "llama-swap"},
    {"model": "ltx2.3-video", "endpoint": "llama-swap"},
]


def test_pick_video_model_prefers_served_setting(monkeypatch):
    import routes.video_routes as vr

    monkeypatch.setattr(vr, "get_user_setting", lambda key, owner, default=None: "ltx2.3-video")
    assert vr._pick_video_model("me", _DETECTED) == "ltx2.3-video"


def test_pick_video_model_falls_back_when_setting_unserved(monkeypatch):
    import routes.video_routes as vr

    monkeypatch.setattr(vr, "get_user_setting", lambda key, owner, default=None: "hunyuan-t2v")
    assert vr._pick_video_model("me", _DETECTED) == "wan2.2-t2v"


def test_pick_video_model_defaults_to_first_detected(monkeypatch):
    import routes.video_routes as vr

    monkeypatch.setattr(vr, "get_user_setting", lambda key, owner, default=None: "")
    assert vr._pick_video_model("me", _DETECTED) == "wan2.2-t2v"


def test_pick_video_model_reads_global_fallback(monkeypatch):
    import routes.video_routes as vr

    # get_user_setting's default arg carries the global setting — a user with
    # no per-user pref should land on the admin-configured global value.
    monkeypatch.setattr(vr, "get_setting", lambda key, default="": "ltx2.3-video")
    monkeypatch.setattr(vr, "get_user_setting", lambda key, owner, default=None: default)
    assert vr._pick_video_model("me", _DETECTED) == "ltx2.3-video"


def test_video_model_setting_registered():
    from src.settings import DEFAULT_SETTINGS, _PER_USER_KEYS

    assert DEFAULT_SETTINGS.get("video_model") == ""
    assert "video_model" in _PER_USER_KEYS


def test_finish_job_bad_b64_marks_error(tmp_path, monkeypatch):
    import src.video_generation as vg
    monkeypatch.setattr(vg, "GENERATED_IMAGES_DIR", str(tmp_path))
    job = {"id": "j2", "status": "running", "width": 1, "height": 1,
           "video_frames": 1, "fps": 1, "prompt": "", "model": "m",
           "owner": None, "session_id": None}
    vg._finish_job(job, {"b64_json": "!!!not-base64!!!"})
    assert job["status"] == "error"


def test_poll_job_error_status_terminates(monkeypatch):
    import src.video_generation as vg

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "failed", "error": "OOM"}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    job = {"id": "j3", "status": "queued"}
    vg._JOBS["j3"] = job
    try:
        asyncio.run(vg._poll_job("j3", "http://x/poll", {}))
        assert job["status"] == "error"
        assert "OOM" in job["error"]
    finally:
        vg._JOBS.pop("j3", None)


# ── route param clamping ─────────────────────────────────────────────────────
def test_video_route_clamp():
    from routes.video_routes import _clamp
    assert _clamp("100", 4, 30, 16) == 30
    assert _clamp(None, 4, 30, 16) == 16
    assert _clamp("junk", 4, 30, 16) == 16
    assert _clamp(8, 4, 30, 16) == 8
