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


def test_acestep_cover_mode_wiring():
    """Reference audio flips the graph into ACE's cover path: LoadAudio →
    VAEEncodeAudio (ACE 1.5 VAE) → ReferenceTimbreAudio on the POSITIVE
    branch only, LLM code-gen off (the model discards codes with a reference
    present — running the 4B pass would be pure waste)."""
    g = comfyui_workflows.acestep15_song_graph(tags="jazz", seconds=30, reference_audio="aegis_ref_abc.mp3")
    ref = next(n for n in g.values() if n["class_type"] == "ReferenceTimbreAudio")
    enc_id = next(k for k, n in g.items() if n["class_type"] == "TextEncodeAceStepAudio1.5")
    vae_id = next(k for k, n in g.items() if n["class_type"] == "VAELoader")
    load = next(n for n in g.values() if n["class_type"] == "LoadAudio")
    venc = next(n for n in g.values() if n["class_type"] == "VAEEncodeAudio")
    ks = next(n for n in g.values() if n["class_type"] == "KSampler")
    neg = next(n for n in g.values() if n["class_type"] == "ConditioningZeroOut")

    assert load["inputs"]["audio"] == "aegis_ref_abc.mp3"
    assert venc["inputs"]["vae"] == [vae_id, 0]          # ACE 1.5 VAE, never another
    assert ref["inputs"]["conditioning"] == [enc_id, 0]
    ref_id = next(k for k, n in g.items() if n["class_type"] == "ReferenceTimbreAudio")
    assert ks["inputs"]["positive"] == [ref_id, 0]
    assert neg["inputs"]["conditioning"] == [enc_id, 0]  # negative stays RAW text encode
    assert g[enc_id]["inputs"]["generate_audio_codes"] is False
    # Sampler untouched: cover still denoises from the empty latent.
    assert ks["inputs"]["denoise"] == 1
    # No reference → no cover nodes, codes on.
    g2 = comfyui_workflows.acestep15_song_graph(tags="jazz", seconds=30)
    assert not any(n["class_type"] == "ReferenceTimbreAudio" for n in g2.values())
    assert next(n for n in g2.values() if n["class_type"] == "TextEncodeAceStepAudio1.5")["inputs"]["generate_audio_codes"] is True


def test_audio_reference_name_gate():
    """/api/audio/generate only accepts names its own upload route minted —
    a caller can never point ComfyUI's LoadAudio at an arbitrary path."""
    from routes.audio_routes import _AUDIO_REF_NAME_RE
    assert _AUDIO_REF_NAME_RE.fullmatch("aegis_ref_0123456789ab.mp3")
    assert _AUDIO_REF_NAME_RE.fullmatch("aegis_ref_deadbeef1234.wav")
    for bad in ("../secrets.mp3", "aegis_ref_0123456789ab.exe", "song.mp3",
                "aegis_ref_XYZ.mp3", "aegis_ref_0123456789ab.mp3/../x"):
        assert not _AUDIO_REF_NAME_RE.fullmatch(bad), bad


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

def test_kokoro_voice_catalog(monkeypatch):
    from services.tts import tts_service as ts
    ids = [v["id"] for v in ts.KOKORO_VOICES]
    assert "af_heart" in ids and "bm_george" in ids
    assert len(ids) == len(set(ids))
    svc = ts.TTSService.__new__(ts.TTSService)  # no cache-dir side effects needed
    # No clones → catalogs are the pure constants. Patched because
    # _with_cloned otherwise reads the real VOICES_DIR + endpoint table,
    # making this test depend on developer-machine state.
    monkeypatch.setattr(ts, "cloned_voice_catalog", lambda: [])
    assert svc.list_voices("local") == ts.KOKORO_VOICES
    assert svc.list_voices("endpoint:abc")[0]["id"] == "alloy"
    assert svc.list_voices("browser") == []


def test_cloned_voice_catalog_and_endpoint_detection(tmp_path, monkeypatch):
    import src.constants as sconstants
    from services.tts import tts_service as ts
    monkeypatch.setattr(sconstants, "VOICES_DIR", str(tmp_path))
    (tmp_path / "zac.wav").write_bytes(b"RIFFxxxx")
    (tmp_path / "grandma.mp3").write_bytes(b"ID3xxxx")
    (tmp_path / "zac.wav.voice.pt").write_bytes(b"not-a-voice")  # baked cache, not a voice
    ids = [v["id"] for v in ts.cloned_voice_catalog()]
    assert ids == ["grandma", "zac"]

    svc = ts.TTSService.__new__(ts.TTSService)
    monkeypatch.setattr(ts.TTSService, "_endpoint_serves_chatterbox", staticmethod(lambda eid: eid == "cb"))
    monkeypatch.setattr(ts.TTSService, "_find_chatterbox_endpoint", staticmethod(lambda: ("cb", "chatterbox-tts")))
    assert [v["id"] for v in svc.list_voices("endpoint:cb")] == ["grandma", "zac"]
    others = svc.list_voices("endpoint:other")
    assert others[0]["id"] == "alloy"  # native endpoint voices stay first
    assert [v["id"] for v in others[-2:]] == ["grandma", "zac"]  # clones appended


def test_my_voice_name_validation():
    from routes.tts_routes import _VOICE_NAME_RE, _voice_slug
    assert _VOICE_NAME_RE.fullmatch("Zac")
    assert _VOICE_NAME_RE.fullmatch("Grandma June")
    assert not _VOICE_NAME_RE.fullmatch("../evil")
    assert not _VOICE_NAME_RE.fullmatch("")
    assert _voice_slug("Grandma June") == "grandma-june"
    assert _voice_slug("  Zac!!  ") == "zac"


def test_kokoro_british_voices_use_b_pipeline():
    from services.tts.tts_service import _KokoroPipeline
    p = _KokoroPipeline.__new__(_KokoroPipeline)
    p._pipes = {"a": "PIPE_A", "b": "PIPE_B"}
    assert p._pipe_for("bm_george") == "PIPE_B"
    assert p._pipe_for("bf_emma") == "PIPE_B"
    assert p._pipe_for("af_heart") == "PIPE_A"
    assert p._pipe_for("") == "PIPE_A"


# ── VibeVoice narration (2026-07-17) ──

def test_vibevoice_graph_shape():
    g = comfyui_workflows.vibevoice_narrate_graph(
        "[1]: Hello.\n[2]: Hi there.", speaker_voices={1: "a.wav", 2: "b.wav"}, seed=7)
    node = next(n for n in g.values() if n["class_type"] == "VibeVoiceMultipleSpeakersNode")
    assert node["inputs"]["model"] == "VibeVoice-Large"
    # sdpa is pinned (no flash-attn/sage in the ComfyUI venv) and the LLM runs
    # 4-bit — full bf16 wouldn't share the 24GB card with anything else.
    assert node["inputs"]["attention_type"] == "sdpa"
    assert node["inputs"]["quantize_llm"] == "4bit"
    assert node["inputs"]["free_memory_after_generate"] is True
    assert node["inputs"]["use_sampling"] is False
    assert node["inputs"]["cfg_scale"] == 1.3
    node_id = next(k for k, n in g.items() if n["class_type"] == "VibeVoiceMultipleSpeakersNode")
    save = next(n for n in g.values() if n["class_type"] == "SaveAudioMP3")
    assert save["inputs"]["audio"] == [node_id, 0]
    # Both speakers cloned through LoadAudio nodes.
    loads = {k: n for k, n in g.items() if n["class_type"] == "LoadAudio"}
    assert {n["inputs"]["audio"] for n in loads.values()} == {"a.wav", "b.wav"}
    assert node["inputs"]["speaker1_voice"][1] == 0
    assert g[node["inputs"]["speaker1_voice"][0]]["inputs"]["audio"] == "a.wav"
    assert g[node["inputs"]["speaker2_voice"][0]]["inputs"]["audio"] == "b.wav"


def test_vibevoice_graph_speaker_edges():
    # Unmapped speakers stay synthetic (no LoadAudio, no speakerN_voice input);
    # out-of-range or empty mappings are dropped, not crashed on.
    g = comfyui_workflows.vibevoice_narrate_graph(
        "[1]: solo", speaker_voices={2: "b.wav", 5: "x.wav", 3: ""}, seed=1)
    node = next(n for n in g.values() if n["class_type"] == "VibeVoiceMultipleSpeakersNode")
    assert "speaker1_voice" not in node["inputs"]
    assert "speaker2_voice" in node["inputs"]
    assert "speaker3_voice" not in node["inputs"]
    assert "speaker5_voice" not in node["inputs"]
    assert sum(1 for n in g.values() if n["class_type"] == "LoadAudio") == 1
    # Negative seeds clamp to >= 0 (the node's INT floor).
    g2 = comfyui_workflows.vibevoice_narrate_graph("[1]: hi", seed=-1)
    n2 = next(n for n in g2.values() if n["class_type"] == "VibeVoiceMultipleSpeakersNode")
    assert n2["inputs"]["seed"] == 0


def test_vibevoice_graph_requires_script():
    with pytest.raises(ValueError):
        comfyui_workflows.vibevoice_narrate_graph("   ")


def test_vibevoice_catalog_gated_and_kinds(monkeypatch):
    spec = comfyui_workflows.COMFY_AUDIO_MODELS["comfy-vibevoice-narrate"]
    assert spec["builder"] is comfyui_workflows.vibevoice_narrate_graph
    assert any(f.startswith("vibevoice/VibeVoice-Large/") for f in spec["required_files"])
    assert any(f.startswith("vibevoice/tokenizer/") for f in spec["required_files"])
    monkeypatch.setattr(comfyui_workflows.os.path, "exists", lambda p: False)
    assert comfyui_workflows.available_audio_models() == []
    monkeypatch.setattr(comfyui_workflows.os.path, "exists", lambda p: True)
    assert "comfy-vibevoice-narrate" in comfyui_workflows.available_audio_models()
    assert comfyui_workflows.audio_model_kind("comfy-vibevoice-narrate") == "narration"
    assert comfyui_workflows.audio_model_kind("comfy-acestep1.5-song") == "song"
    assert comfyui_workflows.audio_model_kind("comfy-nope") == "song"


@pytest.mark.asyncio
async def test_song_route_rejects_narration_model(monkeypatch):
    """A narration model routed into the tags/lyrics call shape would
    TypeError inside the builder — the route must bounce it up front."""
    from fastapi import HTTPException
    import routes.audio_routes as ar
    monkeypatch.setattr(ar, "require_privilege", lambda req, priv: "")
    call = _audio_route("/api/audio/generate")
    with pytest.raises(HTTPException) as e:
        await call(_Req({"tags": "jazz", "model": "comfy-vibevoice-narrate"}))
    assert e.value.status_code == 400
    assert "narrate" in str(e.value.detail)


@pytest.mark.asyncio
async def test_narrate_route_requires_script(monkeypatch):
    from fastapi import HTTPException
    import routes.audio_routes as ar
    monkeypatch.setattr(ar, "require_privilege", lambda req, priv: "")
    call = _audio_route("/api/audio/narrate")
    with pytest.raises(HTTPException) as e:
        await call(_Req({"speakers": {"1": "zac"}}))
    assert e.value.status_code == 400
    assert "script" in str(e.value.detail).lower()


@pytest.mark.asyncio
async def test_narrate_route_rejects_song_model(monkeypatch):
    from fastapi import HTTPException
    import routes.audio_routes as ar
    monkeypatch.setattr(ar, "require_privilege", lambda req, priv: "")
    call = _audio_route("/api/audio/narrate")
    with pytest.raises(HTTPException) as e:
        await call(_Req({"script": "[1]: hi", "model": "comfy-acestep1.5-song"}))
    assert e.value.status_code == 400
    assert "not a narration model" in str(e.value.detail)


@pytest.mark.asyncio
async def test_narrate_route_speaker_validation(tmp_path, monkeypatch):
    """Voice names resolve through the Voice Lab slug inside VOICES_DIR only;
    unknown names 404, bad speaker keys 400."""
    from fastapi import HTTPException
    import routes.audio_routes as ar
    import src.constants as sconstants
    from src import comfyui_workflows as cw
    monkeypatch.setattr(ar, "require_privilege", lambda req, priv: "")
    monkeypatch.setattr(sconstants, "VOICES_DIR", str(tmp_path))
    monkeypatch.setattr(cw, "available_audio_models",
                        lambda: ["comfy-vibevoice-narrate"])
    call = _audio_route("/api/audio/narrate")

    with pytest.raises(HTTPException) as e:
        await call(_Req({"script": "[1]: hi", "speakers": {"1": "ghost"}}))
    assert e.value.status_code == 404

    with pytest.raises(HTTPException) as e:
        await call(_Req({"script": "[1]: hi", "speakers": {"9": "zac"}}))
    assert e.value.status_code == 400

    with pytest.raises(HTTPException) as e:
        await call(_Req({"script": "[1]: hi", "speakers": {"one": "zac"}}))
    assert e.value.status_code == 400

    # Happy path: a real sample resolves and the job is queued.
    (tmp_path / "zac.wav").write_bytes(b"RIFFxxxx")
    from src import comfyui_client as cc
    monkeypatch.setattr(cc, "is_up", lambda timeout=2.0: True)
    seen = {}

    async def fake_start(**kw):
        seen.update(kw)
        return "job123"

    monkeypatch.setattr(cc, "start_narrate_job", fake_start)
    resp = await call(_Req({"script": "[1]: hi", "speakers": {"1": "Zac"}, "seed": 5}))
    assert resp["job_id"] == "job123" and resp["speakers"] == [1]
    assert seen["speaker_voice_paths"][1].endswith("zac.wav")
    assert seen["seed"] == 5
