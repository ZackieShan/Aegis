"""Audio generation (2026-07-17): ACE-Step songs via ComfyUI + Kokoro TTS
voice selection.

Graph/catalog/route validation and the audio job plumbing — the render
itself is exercised live (a real 45s song with vocals verified end-to-end)."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import comfyui_client, comfyui_workflows, job_queue


# ── ACE-Step graph ──

def test_acestep_graph_shape():
    g = comfyui_workflows.acestep15_song_graph(
        tags="lofi hip hop, mellow", lyrics="[Verse]\nla la", seconds=45, seed=7, bpm=90)
    enc = next(n for n in g.values() if n["class_type"] == "TextEncodeAceStepAudio1.5")
    latent = next(n for n in g.values() if n["class_type"] == "EmptyAceStep1.5LatentAudio")
    # The encoder's structure planner must agree with the latent length.
    assert enc["inputs"]["duration"] == latent["inputs"]["seconds"] == 45.0
    assert enc["inputs"]["lyrics"].startswith("[Verse]")
    assert enc["inputs"]["bpm"] == 90
    ks = next(n for n in g.values() if n["class_type"] == "KSampler")
    assert (ks["inputs"]["steps"], ks["inputs"]["cfg"]) == (8, 1)  # turbo schedule
    clip = next(n for n in g.values() if n["class_type"] == "DualCLIPLoader")
    assert clip["inputs"]["type"] == "ace"
    save = next(n for n in g.values() if n["class_type"] == "SaveAudioMP3")
    assert save["inputs"]["quality"] == "V0"


def test_acestep_seconds_clamped():
    g = comfyui_workflows.acestep15_song_graph(tags="x", seconds=99999)
    latent = next(n for n in g.values() if n["class_type"] == "EmptyAceStep1.5LatentAudio")
    assert latent["inputs"]["seconds"] == 600.0


def test_audio_catalog_gated_on_files(monkeypatch):
    spec = comfyui_workflows.COMFY_AUDIO_MODELS["comfy-acestep1.5-song"]
    assert spec["builder"] is comfyui_workflows.acestep15_song_graph
    monkeypatch.setattr(comfyui_workflows.os.path, "exists", lambda p: False)
    assert comfyui_workflows.available_audio_models() == []
    monkeypatch.setattr(comfyui_workflows.os.path, "exists", lambda p: True)
    assert "comfy-acestep1.5-song" in comfyui_workflows.available_audio_models()


# ── Job plumbing ──

def test_audio_is_a_gpu_kind():
    """A song render missing from GPU_KINDS is invisible to the chat guard —
    chat would evict ComfyUI mid-render."""
    assert "audio" in job_queue.GPU_KINDS


def test_first_audio_output_extraction():
    outputs = {
        "10": {"audio": [{"filename": "song_00001_.mp3", "subfolder": "aegis", "type": "output"}]},
        "9": {"images": [{"filename": "preview.png"}]},
    }
    f = comfyui_client._first_audio_output(outputs)
    assert f and f["filename"] == "song_00001_.mp3"
    assert comfyui_client._first_audio_output({"1": {"images": []}}) is None


def test_finish_job_bytes_audio(tmp_path, monkeypatch):
    import core.database as cdb
    import src.video_generation as vg
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", poolclass=NullPool)
    cdb.Base.metadata.create_all(eng)
    monkeypatch.setattr(cdb, "SessionLocal", sessionmaker(bind=eng))
    monkeypatch.setattr(vg, "GENERATED_IMAGES_DIR", str(tmp_path / "gen"))

    job = {"id": "j1", "kind": "audio", "prompt": "lofi", "model": "comfy-acestep1.5-song",
           "seconds": 45.0, "owner": "", "session_id": None, "status": "running"}
    vg.finish_job_bytes(job, b"ID3fakebytes", "mp3")
    assert job["status"] == "done"
    assert job["video_url"].endswith(".mp3")
    db = cdb.SessionLocal()
    row = db.query(cdb.GalleryImage).filter(cdb.GalleryImage.id == job["image_id"]).first()
    db.close()
    assert row.quality == "song" and row.size == "45s"


def test_finish_job_bytes_audio_rejects_video_ext(tmp_path, monkeypatch):
    import core.database as cdb
    import src.video_generation as vg
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", poolclass=NullPool)
    cdb.Base.metadata.create_all(eng)
    monkeypatch.setattr(cdb, "SessionLocal", sessionmaker(bind=eng))
    monkeypatch.setattr(vg, "GENERATED_IMAGES_DIR", str(tmp_path / "gen"))
    job = {"id": "j2", "kind": "audio", "prompt": "x", "model": "m", "seconds": 10.0,
           "owner": "", "session_id": None, "status": "running"}
    vg.finish_job_bytes(job, b"bytes", "exe")
    assert job["video_url"].endswith(".mp3")  # unknown ext coerced, never .exe


# ── Route validation ──

def _audio_route(path):
    from routes.audio_routes import setup_audio_routes
    router = setup_audio_routes()
    return next(r.endpoint for r in router.routes if getattr(r, "path", "") == path)


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_audio_generate_requires_tags(monkeypatch):
    from fastapi import HTTPException
    import routes.audio_routes as ar
    monkeypatch.setattr(ar, "require_privilege", lambda req, priv: "")
    call = _audio_route("/api/audio/generate")
    with pytest.raises(HTTPException) as e:
        await call(_Req({"lyrics": "words but no style"}))
    assert e.value.status_code == 400
    assert "tags" in str(e.value.detail)


@pytest.mark.asyncio
async def test_audio_generate_rejects_unknown_model(monkeypatch):
    from fastapi import HTTPException
    import routes.audio_routes as ar
    monkeypatch.setattr(ar, "require_privilege", lambda req, priv: "")
    call = _audio_route("/api/audio/generate")
    with pytest.raises(HTTPException) as e:
        await call(_Req({"tags": "jazz", "model": "comfy-nope"}))
    assert e.value.status_code == 400


# ── TTS voices ──

def test_kokoro_voice_catalog():
    from services.tts.tts_service import KOKORO_VOICES, TTSService
    ids = [v["id"] for v in KOKORO_VOICES]
    assert "af_heart" in ids and "bm_george" in ids
    assert len(ids) == len(set(ids))
    svc = TTSService.__new__(TTSService)  # no cache-dir side effects needed
    assert svc.list_voices("local") == KOKORO_VOICES
    assert svc.list_voices("endpoint:abc")[0]["id"] == "alloy"
    assert svc.list_voices("browser") == []


def test_kokoro_british_voices_use_b_pipeline():
    from services.tts.tts_service import _KokoroPipeline
    p = _KokoroPipeline.__new__(_KokoroPipeline)
    p._pipes = {"a": "PIPE_A", "b": "PIPE_B"}
    assert p._pipe_for("bm_george") == "PIPE_B"
    assert p._pipe_for("bf_emma") == "PIPE_B"
    assert p._pipe_for("af_heart") == "PIPE_A"
    assert p._pipe_for("") == "PIPE_A"
