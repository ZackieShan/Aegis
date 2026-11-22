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


def _touch(root, rel):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


def test_available_video_models_gated_on_files(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "_models_dir", lambda: str(tmp_path))
    assert cw.available_video_models() == []
    spec = cw.COMFY_VIDEO_MODELS["comfy-wan2.2-t2v"]
    for f in spec["required_files"]:
        _touch(tmp_path, f)
    # companions alone are not enough — an expert pair is needed too …
    assert cw.available_video_models() == []
    # … and half a pair is still no pair
    _touch(tmp_path, cw.WAN_T2V_GGUF_HIGH)
    assert cw.available_video_models() == []
    _touch(tmp_path, cw.WAN_T2V_GGUF_LOW)
    assert cw.available_video_models() == ["comfy-wan2.2-t2v"]
    assert set(cw.spec_files(spec)) == set(spec["required_files"]) | {
        cw.WAN_T2V_GGUF_HIGH, cw.WAN_T2V_GGUF_LOW}


@pytest.mark.parametrize("pair", cw.WAN_T2V_EXPERT_PAIRS)
def test_any_wan_expert_tier_unlocks_the_workflow(tmp_path, monkeypatch, pair):
    """Q8_0 GGUF, fp8_scaled and Q3_K_M GGUF are interchangeable tiers: any
    one complete pair makes comfy-wan2.2-t2v available, and the graph loads
    exactly that pair with the loader its file type needs."""
    monkeypatch.setattr(cw, "_models_dir", lambda: str(tmp_path))
    for f in cw.COMFY_VIDEO_MODELS["comfy-wan2.2-t2v"]["required_files"] + list(pair):
        _touch(tmp_path, f)
    assert cw.available_video_models() == ["comfy-wan2.2-t2v"]
    g = cw.wan22_t2v_graph("a fox")
    for node, rel in (("4", pair[0]), ("5", pair[1])):
        assert g[node]["inputs"]["unet_name"] == cw._mp(rel)
        assert g[node]["class_type"] == ("UnetLoaderGGUF" if rel.endswith(".gguf") else "UNETLoader")


def test_wan_q8_gguf_preferred_over_fp8_and_q3(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "_models_dir", lambda: str(tmp_path))
    for pair in cw.WAN_T2V_EXPERT_PAIRS:
        for f in pair:
            _touch(tmp_path, f)
    g = cw.wan22_t2v_graph("a fox")
    assert g["4"]["inputs"]["unet_name"] == cw.WAN_T2V_GGUF_HIGH
    assert g["5"]["inputs"]["unet_name"] == cw.WAN_T2V_GGUF_LOW
    assert "Q8_0" in cw.WAN_T2V_GGUF_HIGH


def _model_path_inputs(graph):
    """Every string input that names a model file in a subfolder."""
    out = []
    for node in graph.values():
        for v in node["inputs"].values():
            if isinstance(v, str) and v.lower().endswith((".safetensors", ".gguf")) \
                    and ("/" in v or "\\" in v):
                out.append(v)
    return out


@pytest.mark.parametrize("sep", ["/", "\\"])
def test_subfolder_model_paths_follow_the_host_separator(monkeypatch, tmp_path, sep):
    """ComfyUI lists models with the host's os.sep and rejects any other
    spelling ("value_not_in_list"), so every builder must emit "video/x" on
    Linux/macOS and "video\\x" on Windows. The graphs were first written with
    hard-coded backslashes, which broke every workflow on the Linux port."""
    monkeypatch.setattr(cw, "_models_dir", lambda: str(tmp_path))
    monkeypatch.setattr(cw, "_SEP", sep)
    other = "\\" if sep == "/" else "/"
    graphs = [
        cw.wan22_t2v_graph("p"),
        cw.wan22_i2v_graph("p", init_image="in.png"),
        cw.hunyuanvideo15_t2v_graph("p"),
        cw.hunyuanvideo15_i2v_graph("p", init_image="in.png"),
        cw.ltx2_fast_t2v_graph("p"),
        cw.flux2_dev_t2i_graph("p"),
        cw.zimage_turbo_t2i_graph("p"),
        cw.chroma_hd_t2i_graph("p"),
        cw.noobai_vpred_t2i_graph("p"),
        cw.krea2_turbo_t2i_graph("p"),
        cw.ideogram4_t2i_graph("p"),
        cw.acestep15_song_graph("pop"),
        cw.supir_restore_graph("in.png"),
        cw.qwen_controlnet_redraw_graph("in.png"),
    ]
    seen = [v for g in graphs for v in _model_path_inputs(g)]
    assert len(seen) >= 30            # the walk really found the loaders
    assert all(sep in v and other not in v for v in seen), [v for v in seen if other in v]


def test_flux2_dev_graph_structure(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "_models_dir", lambda: str(tmp_path))
    g = cw.flux2_dev_t2i_graph("a fox", width=1030, height=770, seed=3)
    classes = [n["class_type"] for n in g.values()]
    assert "UnetLoaderGGUF" in classes and "Flux2Scheduler" in classes and "SaveImage" in classes
    # dev is guidance-distilled: FluxGuidance → BasicGuider, no CFG/negative pass
    assert "BasicGuider" in classes and "CFGGuider" not in classes
    assert g["5"]["class_type"] == "FluxGuidance" and g["5"]["inputs"]["guidance"] == 4.0
    assert g["10"]["inputs"] == {"model": ["1", 0], "conditioning": ["5", 0]}
    clip = next(n["inputs"] for n in g.values() if n["class_type"] == "CLIPLoader")
    assert clip["type"] == "flux2" and "mistral_3_small_flux2" in clip["clip_name"]
    lat = next(n["inputs"] for n in g.values() if n["class_type"] == "EmptyFlux2LatentImage")
    assert lat["width"] % 16 == 0 and lat["height"] % 16 == 0
    sched = next(n["inputs"] for n in g.values() if n["class_type"] == "Flux2Scheduler")
    assert sched["steps"] == 20 and "LoraLoaderModelOnly" not in classes


def test_flux2_dev_turbo_is_an_explicit_choice(tmp_path, monkeypatch):
    """The Turbo LoRA on disk must NOT silently change comfy-flux2-dev (the
    full-schedule quality tier); the 8-step path is its own catalog entry."""
    monkeypatch.setattr(cw, "_models_dir", lambda: str(tmp_path))
    spec = cw.COMFY_IMAGE_MODELS["comfy-flux2-dev-turbo"]
    for f in cw.COMFY_IMAGE_MODELS["comfy-flux2-dev"]["required_files"] + [cw.FLUX2_DEV_TEXT_ENCODERS[0]]:
        _touch(tmp_path, f)
    assert "comfy-flux2-dev" in cw.available_image_models()
    assert "comfy-flux2-dev-turbo" not in cw.available_image_models()     # no LoRA yet
    _touch(tmp_path, f"loras/{cw.FLUX2_TURBO_LORA}")
    assert "comfy-flux2-dev-turbo" in cw.available_image_models()
    assert f"loras/{cw.FLUX2_TURBO_LORA}" in spec["required_files"]

    full = cw.build_image_graph("comfy-flux2-dev", prompt="a fox")
    assert full["9"]["inputs"]["steps"] == 20
    assert "LoraLoaderModelOnly" not in [n["class_type"] for n in full.values()]

    turbo = cw.build_image_graph("comfy-flux2-dev-turbo", prompt="a fox")
    lora = next(n["inputs"] for n in turbo.values() if n["class_type"] == "LoraLoaderModelOnly")
    assert lora["lora_name"] == cw.FLUX2_TURBO_LORA and lora["model"] == ["1", 0]
    assert turbo["10"]["inputs"]["model"] == ["20", 0]     # guider samples the LoRA'd model
    assert turbo["9"]["inputs"]["steps"] == 8              # turbo schedule
    # an explicit step count is still honored
    assert cw.build_image_graph("comfy-flux2-dev-turbo", prompt="a fox", steps=12)["9"]["inputs"]["steps"] == 12


def test_flux2_dev_catalog_needs_one_text_encoder(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "_models_dir", lambda: str(tmp_path))
    spec = cw.COMFY_IMAGE_MODELS["comfy-flux2-dev"]
    for f in spec["required_files"]:
        _touch(tmp_path, f)
    assert "comfy-flux2-dev" not in cw.available_image_models()
    _touch(tmp_path, cw.FLUX2_DEV_TEXT_ENCODERS[1])       # only the fp8 TE
    assert "comfy-flux2-dev" in cw.available_image_models()
    assert cw.flux2_dev_t2i_graph("x")["2"]["inputs"]["clip_name"] == cw._mp(cw.FLUX2_DEV_TEXT_ENCODERS[1])
    _touch(tmp_path, cw.FLUX2_DEV_TEXT_ENCODERS[0])       # bf16 wins when both exist
    assert cw.flux2_dev_t2i_graph("x")["2"]["inputs"]["clip_name"] == cw._mp(cw.FLUX2_DEV_TEXT_ENCODERS[0])


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
