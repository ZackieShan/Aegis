"""Studio stylize/animate flow (2026-07-16): /api/image/edit + the i2v flag
on /api/video/models + local-first vision auto-detect.

Calls the async route handlers directly (the repo's TestClient-free pattern —
see test_caldav_writeback_route.py) with a temp DB and fake upstream server.
"""

import base64
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.database as cdb
from core.database import GalleryImage, ModelEndpoint

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _route(path):
    from routes.image_gen_routes import setup_image_gen_routes
    router = setup_image_gen_routes()
    for r in router.routes:
        if getattr(r, "path", "") == path:
            return r.endpoint
    raise AssertionError(f"route {path} not found")


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def edit_env(monkeypatch, tmp_path):
    """Temp DB with one owned gallery image + one image endpoint, fake file
    on disk, fake async sd-server (202 envelope + completed job poll)."""
    import src.generated_images as sgen
    import src.video_generation as vg
    import routes.image_gen_routes as igr

    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    monkeypatch.setattr(igr, "require_privilege", lambda req, priv: "")
    monkeypatch.setattr(vg, "_upstream_root",
                        lambda model, owner: ("http://fake:9090", model, {}))

    src_file = tmp_path / "src.png"
    src_file.write_bytes(_PNG)
    monkeypatch.setattr(sgen, "resolve_generated_image_path", lambda fn: src_file)
    out_dir = tmp_path / "generated"
    monkeypatch.setattr(vg, "GENERATED_IMAGES_DIR", str(out_dir))

    db = _TS()
    db.query(GalleryImage).delete()
    db.query(ModelEndpoint).delete()
    img_id = str(uuid.uuid4())
    db.add(GalleryImage(id=img_id, filename="src.png", prompt="kid on a bike", owner=""))
    db.add(ModelEndpoint(
        id="ep1", name="llama-swap", base_url="http://127.0.0.1:9090/v1",
        is_enabled=True,
        cached_models='["qwen-image", "qwen-image-edit", "qwen3-coder-30b"]',
    ))
    db.commit()
    db.close()

    state = {"posted": None}

    class _SubmitResponse:
        status_code = 202
        text = ""

        @staticmethod
        def json():
            return {"id": "remote-job-1"}

    class _PollResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "completed", "result": {
                "output_format": "png",
                "images": [{"index": 0, "b64_json": base64.b64encode(_PNG).decode()}],
            }}

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            state["posted"] = {"url": url, **kw}
            return _SubmitResponse()

        async def get(self, url, **kw):
            return _PollResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    call = _route("/api/image/edit")
    return call, img_id, state, out_dir


@pytest.mark.asyncio
async def test_edit_requires_prompt_and_image(edit_env):
    from fastapi import HTTPException
    call, img_id, _, _ = edit_env
    with pytest.raises(HTTPException) as e:
        await call(_FakeRequest({"image_id": img_id}))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        await call(_FakeRequest({"prompt": "watercolor"}))
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_edit_rejects_non_edit_model(edit_env):
    from fastapi import HTTPException
    call, img_id, _, _ = edit_env
    with pytest.raises(HTTPException) as e:
        await call(_FakeRequest({"image_id": img_id, "prompt": "watercolor", "model": "qwen-image"}))
    assert e.value.status_code == 400
    assert "edit-capable" in str(e.value.detail)


@pytest.mark.asyncio
async def test_edit_defaults_to_served_edit_model_and_queues(edit_env):
    import asyncio
    import src.video_generation as vg

    call, img_id, state, out_dir = edit_env
    out = await call(_FakeRequest({"image_id": img_id, "prompt": "make it a watercolor painting"}))
    # Auto-picked the endpoint's edit model and submitted an async job.
    assert out["status"] == "queued"
    assert out["model"] == "qwen-image-edit"
    assert out["source_image_id"] == img_id
    assert "/upstream/qwen-image-edit/sdcpp/v1/img_gen" in state["posted"]["url"]
    # The source is downscaled to the render size and rides as BOTH
    # ref_images[0] and init_image — mirroring sd-server's own edits mapping
    # (a full-res ref runs the vision encoder at source resolution: 25+ min
    # instead of ~30s).
    payload = state["posted"]["json"]
    assert (payload["width"], payload["height"]) == (512, 512)
    assert payload["init_image"] == payload["ref_images"][0]
    assert payload["auto_resize_ref_image"] is True
    import io
    from PIL import Image
    sent = Image.open(io.BytesIO(base64.b64decode(payload["ref_images"][0])))
    assert sent.size == (512, 512)

    # The background poller finishes against the fake server: photo lands on
    # disk + in the gallery, job marked done in the shared registry.
    job = vg.get_job(out["job_id"])
    assert job is not None and job["kind"] == "image"
    for _ in range(200):
        if job["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.05)
    assert job["status"] == "done", job.get("error")
    assert job["video_url"].startswith("/api/generated-image/")
    saved = list(out_dir.glob("*.png"))
    assert len(saved) == 1 and saved[0].read_bytes() == _PNG
    db = _TS()
    row = db.query(GalleryImage).filter(GalleryImage.id == job["image_id"]).first()
    db.close()
    assert row is not None and row.quality == "edit"


@pytest.mark.asyncio
async def test_edit_unknown_image_404s(edit_env):
    from fastapi import HTTPException
    call, _, _, _ = edit_env
    with pytest.raises(HTTPException) as e:
        await call(_FakeRequest({"image_id": "nope", "prompt": "watercolor"}))
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_video_models_carry_i2v_flag(monkeypatch):
    from routes.video_routes import setup_video_routes
    from src import comfyui_client, video_generation
    import routes.video_routes as vr

    monkeypatch.setattr(comfyui_client, "is_up", lambda *a, **k: False)
    monkeypatch.setattr(video_generation, "list_video_models",
                        lambda owner=None: [{"model": "wan2.2-t2v", "endpoint": "e"},
                                            {"model": "ltx2.3-video", "endpoint": "e"}])
    monkeypatch.setattr(vr, "get_current_user", lambda req: "")
    router = setup_video_routes()
    endpoint = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/video/models")
    out = await endpoint(SimpleNamespace())
    flags = {m["model"]: m["i2v"] for m in out["models"]}
    assert flags["ltx2.3-video"] is True   # image-conditioning pipeline
    assert flags["wan2.2-t2v"] is False    # text-to-video only


def test_vision_autodetect_prefers_local_aliases(monkeypatch):
    """A self-hosted install with qwen-vl served must never say "no vision
    model" — local engine aliases are tried before cloud ids."""
    import src.ai_interaction as ai
    from src.document_processor import _resolve_vl_model

    tried = []

    def _fake_resolve(model, owner=None):
        tried.append(model)
        if model == "qwen-vl":
            return ("http://127.0.0.1:9090/v1/chat/completions", "qwen-vl", {})
        raise ValueError("not served")

    monkeypatch.setattr(ai, "_resolve_model", _fake_resolve)
    url, model, _ = _resolve_vl_model("", owner=None)
    assert model == "qwen-vl"
    assert tried[0] == "qwen-vl"
