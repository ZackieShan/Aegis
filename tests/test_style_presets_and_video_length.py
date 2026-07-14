"""Style presets + seconds-based video length + native sdcpp image path (2026-07-14).

Covers: preset save/load field cleaning, LoRA tag application, style
resolution precedence, the duration→frames math (4k+1 snapping, per-model
native fps), and the sd-server native img_gen client (nested images[] result
shape, 410 reaping, fallback signaling).
"""
import asyncio
import json

import pytest


# ── style preset storage ─────────────────────────────────────────────────────
@pytest.fixture
def styles_dir(tmp_path, monkeypatch):
    import src.style_presets as sp
    monkeypatch.setattr(sp, "STYLES_DIR", str(tmp_path))
    return sp


def test_style_id_slugs():
    from src.style_presets import style_id
    assert style_id("Neon Noir") == "neon-noir"
    assert style_id("  Oil_Painting-2 ") == "oil_painting-2"
    with pytest.raises(ValueError):
        style_id("///")


def test_save_get_list_delete_roundtrip(styles_dir):
    sp = styles_dir
    rec = sp.save_style({
        "name": "Neon Noir",
        "image_model": "qwen-image",
        "video_model": "ltx2.3-video",
        "prompt_prefix": "neon noir film still",
        "seed": "42",             # string in → int out
        "steps": 20,
        "cfg_scale": "4.5",
        "size": "768x768",
        "loras": ["detail-tweaker:0.8"],
    }, owner="me")
    assert rec["id"] == "neon-noir"
    assert rec["seed"] == 42
    assert rec["cfg_scale"] == 4.5
    assert rec["loras"] == [{"name": "detail-tweaker", "weight": 0.8}]

    got = sp.get_style("Neon Noir")
    assert got and got["image_model"] == "qwen-image"
    assert [s["id"] for s in sp.list_styles()] == ["neon-noir"]
    assert sp.delete_style("neon-noir")
    assert sp.get_style("neon-noir") is None


def test_save_drops_invalid_fields(styles_dir):
    sp = styles_dir
    rec = sp.save_style({
        "name": "x",
        "size": "gigantic",              # not WxH
        "seed": "not-a-number",
        "loras": [{"name": "../evil<lora", "weight": 1}],  # unsafe name
    })
    assert "size" not in rec
    assert "seed" not in rec
    assert "loras" not in rec


def test_styled_prompt_affixes_and_loras():
    from src.style_presets import styled_prompt
    style = {
        "prompt_prefix": "oil painting",
        "prompt_suffix": "warm light",
        "loras": [{"name": "brushstrokes", "weight": 0.7}],
    }
    out = styled_prompt(style, "a fox in snow")
    assert out == "oil painting, a fox in snow, warm light <lora:brushstrokes:0.7>"
    # Cloud backends don't parse lora tags — with_loras=False drops them.
    assert "<lora:" not in styled_prompt(style, "a fox", with_loras=False)
    assert styled_prompt(None, "bare") == "bare"


def test_resolve_style_precedence(styles_dir, monkeypatch):
    sp = styles_dir
    sp.save_style({"name": "active-one", "prompt_prefix": "AAA"})
    sp.save_style({"name": "explicit-one", "prompt_prefix": "BBB"})
    monkeypatch.setattr(sp, "active_style_name", lambda owner: "active-one")
    assert sp.resolve_style("explicit-one", None)["id"] == "explicit-one"
    assert sp.resolve_style(None, None)["id"] == "active-one"
    # "none"/"off" disables even when an active style exists
    assert sp.resolve_style("none", None) is None
    assert sp.resolve_style("off", None) is None


# ── duration → frames math ───────────────────────────────────────────────────
def test_native_fps_per_model():
    from routes.video_routes import _native_fps
    assert _native_fps("ltx2.3-video") == 24
    assert _native_fps("wan2.2-t2v") == 16
    assert _native_fps("") == 16


@pytest.mark.parametrize("body,fps,expected", [
    ({"duration": 5}, 16, 81),      # Wan native 5s
    ({"duration": 5}, 24, 121),     # LTX native 5s
    ({"duration": 10}, 16, 161),
    ({"duration": 10}, 24, 241),
    ({"seconds": 2}, 16, 33),       # alias key, the old default length
    ({"duration": 999}, 24, 257),   # clamped to the ceiling
    ({"duration": "nope"}, 16, 33), # junk → default
    ({}, 16, 33),                   # no length → 2s default
    ({"video_frames": 80}, 16, 81), # explicit frames snap to 4k+1
    ({"video_frames": 161}, 16, 161),
    ({"video_frames": 100000}, 16, 257),
])
def test_resolve_frames(body, fps, expected):
    from routes.video_routes import _resolve_frames
    assert _resolve_frames(body, fps) == expected


def test_frames_always_4k_plus_1():
    from routes.video_routes import _resolve_frames
    for f in range(1, 260):
        got = _resolve_frames({"video_frames": f}, 16)
        assert (got - 1) % 4 == 0 and 1 <= got <= 257, (f, got)


# ── native sdcpp image client ────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _fake_client_factory(post_resp, get_resps):
    """AsyncClient stand-in: fixed submit response, scripted poll responses."""
    class FakeClient:
        def __init__(self, *a, **k):
            self._gets = list(get_resps)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return post_resp

        async def get(self, url, headers=None):
            return self._gets.pop(0) if self._gets else _FakeResp(500, {})
    return FakeClient


def _run_native(monkeypatch, post_resp, get_resps, **kw):
    import httpx
    from src.ai_interaction import _sdcpp_native_image
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(post_resp, get_resps))
    return asyncio.run(_sdcpp_native_image(
        "http://127.0.0.1:9090/v1", kw.pop("model_id", "qwen-image"), {},
        prompt="p", negative_prompt="", width=512, height=512,
        seed=kw.pop("seed", 7), steps=None, cfg_scale=None, **kw,
    ))


def test_native_image_nested_images_result(monkeypatch):
    submit = _FakeResp(202, {"id": "job_1", "poll_url": "/sdcpp/v1/jobs/job_1"})
    polls = [
        _FakeResp(200, {"status": "generating", "result": None}),
        _FakeResp(200, {"status": "completed", "result": {"images": [{"b64_json": "QUJD"}]}}),
    ]
    # keep the 2s inter-poll sleep out of the test
    async def _nosleep(_): pass
    monkeypatch.setattr("src.ai_interaction.asyncio.sleep", _nosleep)
    out = _run_native(monkeypatch, submit, polls)
    assert out == {"b64_json": "QUJD"}


def test_native_image_flat_result_shape(monkeypatch):
    submit = _FakeResp(202, {"id": "job_1", "poll_url": "/sdcpp/v1/jobs/job_1"})
    polls = [_FakeResp(200, {"status": "completed", "result": {"b64_json": "RkxBVA=="}})]
    out = _run_native(monkeypatch, submit, polls)
    assert out == {"b64_json": "RkxBVA=="}


def test_native_image_410_reaped_job(monkeypatch):
    submit = _FakeResp(202, {"id": "job_1", "poll_url": "/sdcpp/v1/jobs/job_1"})
    polls = [_FakeResp(410, {})]
    out = _run_native(monkeypatch, submit, polls)
    assert "vanished" in out["error"]


def test_native_image_404_submit_falls_back(monkeypatch):
    submit = _FakeResp(404, {})
    out = _run_native(monkeypatch, submit, [])
    assert out == {"fallback": True}


def test_native_image_unsafe_model_id_falls_back(monkeypatch):
    out = _run_native(monkeypatch, _FakeResp(202, {}), [], model_id="../evil")
    assert out == {"fallback": True}


# ── image-to-video (init image) ──────────────────────────────────────────────
def test_is_i2v_model_gate():
    from routes.video_routes import _is_i2v
    assert _is_i2v("ltx2.3-video")
    assert not _is_i2v("wan2.2-t2v")
    assert not _is_i2v("")


def test_fit_video_dims_keeps_aspect_within_budget():
    from routes.video_routes import _fit_video_dims
    assert _fit_video_dims(512, 512) == (512, 512)          # under budget: untouched
    w, h = _fit_video_dims(2048, 1152)                       # 16:9, way over budget
    assert w * h <= 400_000 and w % 16 == 0 and h % 16 == 0
    assert abs((w / h) - (2048 / 1152)) < 0.15               # aspect preserved


def test_resolve_init_image_passthrough_and_absent():
    from routes.video_routes import _resolve_init_image
    assert _resolve_init_image({"init_image": "QUJD"}, "me") == ("QUJD", None, None)
    assert _resolve_init_image({}, "me") == (None, None, None)


def test_start_video_job_payload_includes_init_image(monkeypatch):
    import httpx
    import src.video_generation as vg

    captured = {}

    class _Resp:
        status_code = 202
        def json(self):
            return {"id": "r1", "poll_url": "/sdcpp/v1/jobs/r1"}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["payload"] = json
            return _Resp()

    async def _noop_poll(job_id, poll_url, headers):
        return None

    monkeypatch.setattr(vg, "_upstream_root", lambda spec, owner: ("http://127.0.0.1:9090", "ltx2.3-video", {}))
    monkeypatch.setattr(vg, "_poll_job", _noop_poll)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    job_id = asyncio.run(vg.start_video_job(
        prompt="p", model="ltx2.3-video", width=512, height=512,
        video_frames=25, fps=24, init_image_b64="QUJD",
    ))
    assert captured["payload"]["init_image"] == "QUJD"
    assert vg.get_job(job_id)["has_init_image"] is True
    # a plain t2v submit must NOT carry the field
    captured.clear()
    asyncio.run(vg.start_video_job(prompt="p", model="ltx2.3-video"))
    assert "init_image" not in captured["payload"]
