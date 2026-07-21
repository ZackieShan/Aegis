"""2026-07-17 media expansion: Wan fp8 preference, I2V workflows, new image
models, and video post-ops (foley / upscale / interpolate)."""
import os

import pytest

from src import comfyui_workflows as cw
from src import model_tags


def _touch(root, rel):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(b"x")


@pytest.fixture
def models_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "_models_dir", lambda: str(tmp_path))
    return str(tmp_path)


# ── Wan T2V fp8 preference ──

def test_wan_t2v_prefers_fp8_when_present(models_root):
    for f in ("Wan2.2-T2V-A14B-HighNoise-Q3_K_M.gguf", "Wan2.2-T2V-A14B-LowNoise-Q3_K_M.gguf"):
        _touch(models_root, f)
    g = cw.wan22_t2v_graph("a cat")
    assert g["4"]["class_type"] == "UnetLoaderGGUF"
    _touch(models_root, cw.WAN_T2V_FP8_HIGH)
    _touch(models_root, cw.WAN_T2V_FP8_LOW)
    g = cw.wan22_t2v_graph("a cat")
    assert g["4"]["class_type"] == "UNETLoader"
    assert "fp8_scaled" in g["4"]["inputs"]["unet_name"]
    assert g["5"]["class_type"] == "UNETLoader"


# ── Wan I2V ──

def test_wan_i2v_requires_init_image(models_root):
    with pytest.raises(ValueError):
        cw.wan22_i2v_graph("move it")


def test_wan_i2v_lightning_gates_on_seko_loras(models_root):
    g = cw.wan22_i2v_graph("move it", init_image="in.png")
    assert g["11"]["inputs"]["steps"] == 20  # no LoRAs on disk → full schedule
    _touch(models_root, os.path.join("loras", cw.WAN_I2V_LIGHTNING_HIGH_LORA))
    _touch(models_root, os.path.join("loras", cw.WAN_I2V_LIGHTNING_LOW_LORA))
    g = cw.wan22_i2v_graph("move it", init_image="in.png")
    assert g["11"]["inputs"]["steps"] == 4 and g["11"]["inputs"]["cfg"] == 1.0
    assert g["11"]["inputs"]["end_at_step"] == 2
    assert g["21"]["inputs"]["lora_name"] == cw.WAN_I2V_LIGHTNING_HIGH_LORA
    # image conditioning flows from WanImageToVideo, not raw text encodes
    assert g["11"]["inputs"]["positive"] == ["10", 0]
    assert g["11"]["inputs"]["latent_image"] == ["10", 2]


def test_i2v_catalog_gated_and_classified(models_root):
    assert "comfy-wan2.2-i2v" not in cw.available_video_models()
    for f in cw.COMFY_VIDEO_MODELS["comfy-wan2.2-i2v"]["required_files"]:
        _touch(models_root, f)
    assert "comfy-wan2.2-i2v" in cw.available_video_models()
    assert "image-to-video" in model_tags.classify("comfy-wan2.2-i2v")["capabilities"]
    assert "image-to-video" in model_tags.classify("comfy-hunyuanvideo1.5-i2v")["capabilities"]


def test_hv15_i2v_graph_uses_sigclip_and_i2v_node():
    g = cw.hunyuanvideo15_i2v_graph("wave", init_image="in.png")
    assert g["4"]["class_type"] == "CLIPVisionLoader"
    assert g["9"]["class_type"] == "HunyuanVideo15ImageToVideo"
    assert g["9"]["inputs"]["start_image"] == ["5", 0]
    assert g["14"]["inputs"]["cfg"] == 1.0  # cfg-distilled


# ── image models ──

def test_new_image_models_gated_on_files(models_root):
    assert cw.available_image_models() == []
    for f in cw.COMFY_IMAGE_MODELS["comfy-z-image-turbo"]["required_files"]:
        _touch(models_root, f)
    assert cw.available_image_models() == ["comfy-z-image-turbo"]


def test_image_builders_produce_valid_graphs():
    z = cw.zimage_turbo_t2i_graph("a fox", seed=7)
    assert z["8"]["inputs"]["sampler_name"] == "res_multistep"
    assert z["2"]["inputs"]["type"] == "lumina2"
    c = cw.chroma_hd_t2i_graph("a fox", seed=7)
    assert c["2"]["inputs"]["type"] == "chroma"
    assert c["12"]["inputs"]["cfg"] == 3.5  # not cfg-distilled
    n = cw.noobai_vpred_t2i_graph("1girl", seed=7)
    assert n["2"]["inputs"]["sampling"] == "v_prediction"
    assert n["2"]["inputs"]["zsnr"] is True  # omitting zsnr fries output
    assert n["7"]["inputs"]["width"] % 64 == 0
    k = cw.krea2_turbo_t2i_graph("a fox", seed=7)
    assert k["2"]["inputs"]["type"] == "krea2"
    assert k["7"]["inputs"]["cfg"] == 1.0


def test_new_image_models_classify_as_t2i():
    for mid in ("comfy-z-image-turbo", "comfy-chroma-hd", "comfy-noobai-xl", "comfy-krea2-turbo"):
        assert "text-to-image" in model_tags.classify(mid)["capabilities"], mid
        assert "chat" not in model_tags.classify(mid)["capabilities"], mid


# ── video post-ops ──

def test_video_ops_gated_on_files(models_root):
    assert cw.available_video_ops() == []
    for f in cw.COMFY_VIDEO_OPS["interpolate"]["required_files"]:
        _touch(models_root, f)
    assert cw.available_video_ops() == ["interpolate"]
    assert cw.build_video_op_graph("nope") is None


def test_foley_graph_muxes_source_frames_at_source_fps():
    g = cw.mmaudio_foley_graph("clip.mp4", prompt="rain", duration=6.5, seed=1)
    assert g["5"]["inputs"]["duration"] == 6.5
    assert g["6"]["inputs"]["images"] == ["2", 0]   # source frames, not re-rendered
    assert g["6"]["inputs"]["fps"] == ["2", 2]      # probed source fps
    assert g["6"]["inputs"]["audio"] == ["5", 0]


def test_seedvr2_graph_batch_snaps_to_4n_plus_1():
    g = cw.seedvr2_upscale_graph("clip.mp4", batch_size=7)
    assert g["5"]["inputs"]["batch_size"] == 5
    g = cw.seedvr2_upscale_graph("clip.mp4", batch_size=9)
    assert g["5"]["inputs"]["batch_size"] == 9
    assert g["6"]["inputs"]["audio"] == ["2", 1]    # source audio carried through


def test_gimmvfi_graph_carries_audio_and_fps_out():
    g = cw.gimmvfi_interpolate_graph("clip.mp4", factor=2, fps_out=32.0)
    assert g["4"]["inputs"]["interpolation_factor"] == 2
    assert g["5"]["inputs"]["fps"] == 32.0
    assert g["5"]["inputs"]["audio"] == ["2", 1]


# ── image post-ops + second wave (2026-07-17 late) ──

def test_image_ops_gated_on_files(models_root):
    assert cw.available_image_ops() == []
    for f in cw.COMFY_IMAGE_OPS["upscale4x"]["required_files"]:
        _touch(models_root, f)
    assert cw.available_image_ops() == ["upscale4x"]
    assert cw.build_image_op_graph("nope") is None


def test_supir_graph_uses_eps_photoreal_base():
    """SUPIR's wrapper is eps-only — fusing the v-pred NoobAI produces
    garbage, so the builder must pin the RealVisXL eps checkpoint."""
    g = cw.supir_restore_graph("photo.png", seed=1)
    assert "RealVisXL" in g["4"]["inputs"]["sdxl_model"]
    # conditioner latents from first_stage, sampler latents from encode
    assert g["7"]["inputs"]["latents"] == ["5", 2]
    assert g["8"]["inputs"]["latents"] == ["6", 0]


def test_redraw_graph_derives_canny_and_matches_source_latent():
    g = cw.qwen_controlnet_redraw_graph("photo.png", prompt="a castle", seed=1)
    assert g["16"]["class_type"] == "Canny"
    assert g["11"]["inputs"]["image"] == ["16", 0]      # control = edge map
    assert g["11"]["inputs"]["vae"] == ["7", 0]         # InstantX needs the vae wired
    assert g["13"]["inputs"]["latent_image"] == ["12", 0]  # render latent = VAEEncode of source
    assert g["13"]["inputs"]["steps"] == 8 and g["13"]["inputs"]["cfg"] == 1.0


def test_hunyuan_foley_pins_explicit_quantization():
    """'auto' would pick e5m2 on this GPU and lossily re-quantize the
    e4m3fn checkpoint — the builder must pin fp8_e4m3fn."""
    g = cw.hunyuan_foley_graph("clip.mp4", prompt="rain", duration=3.0)
    assert g["3"]["inputs"]["quantization"] == "fp8_e4m3fn"
    assert g["5"]["inputs"]["frame_rate"] == ["2", 2]
    assert g["5"]["inputs"]["duration"] == 3.0
    assert g["6"]["inputs"]["images"] == ["2", 0]


def test_ideogram_gated_and_dual_model():
    g = cw.ideogram4_t2i_graph("SALE poster", seed=1)
    assert g["9"]["class_type"] == "DualModelGuider"
    assert g["9"]["inputs"]["model_negative"] == ["2", 0]
    assert g["3"]["inputs"]["type"] == "ideogram4"
    assert "text-to-image" in model_tags.classify("comfy-ideogram4")["capabilities"]
