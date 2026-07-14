"""ComfyUI engine integration (2026-07-14): workflow builders + client glue."""
import types

import pytest

from src import comfyui_workflows as cw


def test_wan22_graph_structure(tmp_path, monkeypatch):
    # bare models dir → no lightning LoRAs → full-step two-stage schedule
    monkeypatch.setattr(cw, "_models_dir", lambda: str(tmp_path))
    g = cw.wan22_t2v_graph("a fox", width=480, height=272, video_frames=17, fps=16, seed=7, steps=12)
    classes = [n["class_type"] for n in g.values()]
    # GGUF loaders (this install's models are quantized), dual-expert sampling
    assert classes.count("UnetLoaderGGUF") == 2
    assert "CLIPLoaderGGUF" in classes
    assert classes.count("KSamplerAdvanced") == 2
    assert "SaveVideo" in classes
    assert "LoraLoaderModelOnly" not in classes
    # the two sampler stages hand off at half the schedule
    stages = [n["inputs"] for n in g.values() if n["class_type"] == "KSamplerAdvanced"]
    first = next(s for s in stages if s["add_noise"] == "enable")
    second = next(s for s in stages if s["add_noise"] == "disable")
    assert first["end_at_step"] == second["start_at_step"] == 6
    assert first["noise_seed"] == 7
    latent = next(n["inputs"] for n in g.values() if n["class_type"] == "EmptyHunyuanLatentVideo")
    assert (latent["width"], latent["height"], latent["length"]) == (480, 272, 17)


def test_wan22_graph_lightning_autowires(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "_models_dir", lambda: str(tmp_path))
    loras = tmp_path / "loras"
    loras.mkdir()
    (loras / "wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors").write_bytes(b"x")
    (loras / "wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors").write_bytes(b"x")
    g = cw.wan22_t2v_graph("a fox", seed=7, steps=20, cfg=3.5)
    classes = [n["class_type"] for n in g.values()]
    assert classes.count("LoraLoaderModelOnly") == 2
    # distillation LoRAs force the template's 4-step / cfg-1 branch
    stages = [n["inputs"] for n in g.values() if n["class_type"] == "KSamplerAdvanced"]
    first = next(s for s in stages if s["add_noise"] == "enable")
    assert first["steps"] == 4 and first["cfg"] == 1.0 and first["end_at_step"] == 2
    # ModelSamplingSD3 now feeds from the LoRA-wrapped models
    ms_inputs = [n["inputs"]["model"] for n in g.values() if n["class_type"] == "ModelSamplingSD3"]
    assert sorted(m[0] for m in ms_inputs) == ["21", "22"]


def test_ltx2_fast_graph_structure():
    g = cw.ltx2_fast_t2v_graph("a fox", width=500, height=300, video_frames=50, fps=24, seed=3)
    classes = [n["class_type"] for n in g.values()]
    assert "UnetLoaderGGUF" in classes and "LTXAVTextEncoderLoader" in classes
    assert "LTXVConcatAVLatent" in classes and "LTXVSeparateAVLatent" in classes
    assert "LTXVAudioVAEDecode" in classes  # audio+video joint model
    lat = next(n["inputs"] for n in g.values() if n["class_type"] == "EmptyLTXVLatentVideo")
    # dims snapped to /32, frames to 8k+1
    assert lat["width"] % 32 == 0 and lat["height"] % 32 == 0
    assert (lat["length"] - 1) % 8 == 0
    save = next(n["inputs"] for n in g.values() if n["class_type"] == "SaveVideo")
    assert save["format"] == "mp4"


def test_flux2_klein_graph_structure():
    g = cw.flux2_klein_t2i_graph("a fox", width=1024, height=1024, seed=3, steps=20)
    classes = [n["class_type"] for n in g.values()]
    assert "UnetLoaderGGUF" in classes and "Flux2Scheduler" in classes and "SaveImage" in classes
    guider = next(n["inputs"] for n in g.values() if n["class_type"] == "CFGGuider")
    assert guider["cfg"] == 1.0  # klein is guidance-distilled
    clip = next(n["inputs"] for n in g.values() if n["class_type"] == "CLIPLoader")
    assert clip["type"] == "flux2"


def test_available_video_models_gated_on_files(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "_models_dir", lambda: str(tmp_path))
    assert cw.available_video_models() == []
    for f in cw.COMFY_VIDEO_MODELS["comfy-wan2.2-t2v"]["required_files"]:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    assert cw.available_video_models() == ["comfy-wan2.2-t2v"]


def test_build_video_graph_unknown_model():
    assert cw.build_video_graph("comfy-nope", prompt="x") is None


def test_first_video_output_prefers_video_extensions():
    from src.comfyui_client import _first_video_output
    outputs = {
        "14": {"images": [
            {"filename": "wan22_00001_.png", "subfolder": "aegis", "type": "output"},
            {"filename": "wan22_00001_.mp4", "subfolder": "aegis", "type": "output"},
        ]},
    }
    got = _first_video_output(outputs)
    assert got and got["filename"].endswith(".mp4")
    assert _first_video_output({"14": {"images": [{"filename": "a.png"}]}}) is None


def test_comfy_model_ids_classify_correctly():
    """The registry/routing tags must see these as video models, not chat."""
    from src.model_tags import classify
    caps = classify("comfy-wan2.2-t2v")["capabilities"]
    assert "text-to-video" in caps and "image-to-video" not in caps
