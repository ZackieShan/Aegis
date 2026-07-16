"""Wan2.2 Lightning on the native sd-server path + the HunyuanVideo 1.5
ComfyUI workflow (2026-07-16: 'the hands don't seem right').

The Lightning LoRAs only reached the ComfyUI Wan graph; the default native
path rendered the base model at 10+8 steps, which under-converges anatomy.
apply_wan_lightning splices sd.cpp's per-expert LoRA prompt syntax into
native Wan T2V renders (paired with the 4+4-step llama-swap entry)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import comfyui_workflows, video_generation
from src.video_generation import apply_wan_lightning


def _force_lightning(monkeypatch, present: bool):
    monkeypatch.setattr(comfyui_workflows, "wan_lightning_available", lambda: present)


def test_wan_t2v_gets_lora_tags_and_default_negative(monkeypatch):
    _force_lightning(monkeypatch, True)
    prompt, negative = apply_wan_lightning("wan2.2-t2v", "a lovely cat", "")
    assert prompt.startswith("a lovely cat")
    assert "<lora:wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise:1>" in prompt
    assert "<lora:|high_noise|wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise:1>" in prompt
    assert negative == comfyui_workflows.WAN_DEFAULT_NEGATIVE


def test_explicit_negative_is_preserved(monkeypatch):
    _force_lightning(monkeypatch, True)
    _, negative = apply_wan_lightning("wan2.2-t2v", "a cat", "my negative")
    assert negative == "my negative"


def test_non_wan_models_untouched(monkeypatch):
    _force_lightning(monkeypatch, True)
    for model in ("ltx2.3-video", "wan2.2-i2v", "qwen-image"):
        prompt, negative = apply_wan_lightning(model, "a cat", "")
        assert prompt == "a cat", model
        assert negative == "", model


def test_missing_loras_leave_prompt_alone(monkeypatch):
    _force_lightning(monkeypatch, False)
    prompt, negative = apply_wan_lightning("wan2.2-t2v", "a cat", "")
    assert prompt == "a cat"
    assert negative == ""


def test_existing_lightx2v_tag_not_duplicated(monkeypatch):
    _force_lightning(monkeypatch, True)
    styled = "a cat <lora:wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise:0.8>"
    prompt, _ = apply_wan_lightning("wan2.2-t2v", styled, "")
    assert prompt == styled
    assert prompt.count("lightx2v") == 1


def test_comfy_wan_graph_uses_shared_lora_constants(monkeypatch):
    _force_lightning(monkeypatch, True)
    # Force the graph's own file check on, matching the helper.
    monkeypatch.setattr(
        comfyui_workflows.os.path, "exists", lambda p: True)
    graph = comfyui_workflows.wan22_t2v_graph("a cat")
    loras = {n["inputs"]["lora_name"] for n in graph.values()
             if n["class_type"] == "LoraLoaderModelOnly"}
    assert loras == {comfyui_workflows.WAN_LIGHTNING_HIGH_LORA,
                     comfyui_workflows.WAN_LIGHTNING_LOW_LORA}


# ── HunyuanVideo 1.5 workflow ──

def test_hunyuan_graph_shape():
    g = comfyui_workflows.hunyuanvideo15_t2v_graph(
        "a cat", width=645, height=369, video_frames=33, fps=24, seed=7)
    latent = next(n for n in g.values() if n["class_type"] == "EmptyHunyuanVideo15Latent")
    assert latent["inputs"]["width"] % 16 == 0
    assert latent["inputs"]["height"] % 16 == 0
    clip = next(n for n in g.values() if n["class_type"] == "DualCLIPLoader")
    assert clip["inputs"]["type"] == "hunyuan_video_15"
    unet = next(n for n in g.values() if n["class_type"] == "UNETLoader")
    assert unet["inputs"]["weight_dtype"] == "fp8_e4m3fn"
    save = next(n for n in g.values() if n["class_type"] == "SaveVideo")
    assert save["inputs"]["codec"] == "h264"


def test_hunyuan_catalog_entry_gated_on_files(monkeypatch):
    spec = comfyui_workflows.COMFY_VIDEO_MODELS["comfy-hunyuanvideo1.5-t2v"]
    assert spec["builder"] is comfyui_workflows.hunyuanvideo15_t2v_graph
    assert any("720p_t2v" in f for f in spec["required_files"])
    monkeypatch.setattr(comfyui_workflows.os.path, "exists", lambda p: False)
    assert "comfy-hunyuanvideo1.5-t2v" not in comfyui_workflows.available_video_models()
    monkeypatch.setattr(comfyui_workflows.os.path, "exists", lambda p: True)
    assert "comfy-hunyuanvideo1.5-t2v" in comfyui_workflows.available_video_models()


def test_native_fps_knows_hunyuan():
    from routes.video_routes import _native_fps
    assert _native_fps("comfy-hunyuanvideo1.5-t2v") == 24
    assert _native_fps("ltx2.3-video") == 24
    assert _native_fps("wan2.2-t2v") == 16


def test_job_record_keeps_clean_prompt(monkeypatch):
    """The <lora:...> tags are transport detail — gallery/movie-maker prompts
    must stay what the user typed. Guarded here at the seam: the helper's
    output is only handed to the payload, never the job dict (see
    start_video_job's gen_prompt local)."""
    import inspect
    src = inspect.getsource(video_generation.start_video_job)
    assert '"prompt": gen_prompt' in src        # payload gets the tagged prompt
    assert '"prompt": prompt' in src            # job record keeps the clean one
