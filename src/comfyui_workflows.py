"""ComfyUI workflow builders — Aegis's video/image graphs as code.

Each builder returns an API-format prompt graph (node_id -> {class_type,
inputs}) ready for POST /prompt. Templates were derived from the workflow
templates bundled with ComfyUI 0.27 (comfyui_workflow_templates_json), with
safetensors loaders swapped for the ComfyUI-GGUF node pack's loaders since
this install's diffusion models are GGUF quants.

COMFY_VIDEO_MODELS maps the Aegis-facing model ids (what /video and the
pickers show) to a builder + the model files it needs — a workflow is only
offered when every file exists in the models/ drop folder, so a missing
companion shows up as an absent option instead of a mid-render crash.
"""

import os
from typing import Any, Dict, List, Optional

# The models/ drop folder — same tree llama-swap/sd-server serve from.
def _models_dir() -> str:
    from core.constants import BASE_DIR
    return os.path.join(BASE_DIR, "models")


# lightx2v 4-step distillation LoRAs for Wan2.2 T2V — shared between this
# module's ComfyUI graph and the native sd-server path (video_generation
# splices them into the prompt via sd.cpp's <lora:...> syntax).
WAN_LIGHTNING_HIGH_LORA = "wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors"
WAN_LIGHTNING_LOW_LORA = "wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors"

# fp8_scaled Wan2.2 experts (Comfy-Org repackage) — the quality upgrade over
# the Q3_K_M GGUFs, whose aggressive quantization is what mangles hands and
# anatomy. When present they take over the T2V graph automatically.
WAN_T2V_FP8_HIGH = "video/wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors"
WAN_T2V_FP8_LOW = "video/wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors"
WAN_I2V_FP8_HIGH = "video/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"
WAN_I2V_FP8_LOW = "video/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"
# I2V Lightning pair is the Seko-V1 release — filenames differ from both the
# T2V LoRAs above and the names the bundled template references.
WAN_I2V_LIGHTNING_HIGH_LORA = "wan2.2_i2v_lightx2v_4steps_seko_v1_high_noise.safetensors"
WAN_I2V_LIGHTNING_LOW_LORA = "wan2.2_i2v_lightx2v_4steps_seko_v1_low_noise.safetensors"


def _model_exists(rel: str) -> bool:
    return os.path.exists(os.path.join(_models_dir(), rel))


def wan_t2v_fp8_available() -> bool:
    return _model_exists(WAN_T2V_FP8_HIGH) and _model_exists(WAN_T2V_FP8_LOW)


def wan_i2v_lightning_available() -> bool:
    return (_model_exists(os.path.join("loras", WAN_I2V_LIGHTNING_HIGH_LORA))
            and _model_exists(os.path.join("loras", WAN_I2V_LIGHTNING_LOW_LORA)))

# Wan's standard negative prompt (from the reference workflows) — most of it
# targets exactly the anatomy failures users notice: 多余的手指 (extra
# fingers), 画得不好的手部 (badly drawn hands).
WAN_DEFAULT_NEGATIVE = (
    "色调艳丽，过曝，静态，细节模糊不清，最差质量，低质量，多余的手指，画得不好的手部"
)


def wan_lightning_available() -> bool:
    """Both Wan2.2 Lightning expert LoRAs present in models/loras."""
    loras_dir = os.path.join(_models_dir(), "loras")
    return (os.path.exists(os.path.join(loras_dir, WAN_LIGHTNING_HIGH_LORA))
            and os.path.exists(os.path.join(loras_dir, WAN_LIGHTNING_LOW_LORA)))


def wan22_t2v_graph(
    prompt: str,
    negative_prompt: str = "",
    width: int = 640,
    height: int = 640,
    video_frames: int = 33,
    fps: int = 16,
    seed: int = 0,
    steps: int = 20,
    cfg: float = 3.5,
) -> Dict[str, Any]:
    """Wan2.2 T2V A14B (dual high/low-noise GGUF experts), per the bundled
    video_wan2_2_14B_t2v template: the high-noise expert denoises the first
    half of the schedule, the low-noise expert finishes.

    When the lightx2v 4-step distillation LoRAs are present in models/loras
    they're wired in automatically (steps 4, cfg 1 per the template's LoRA
    branch) — same quality class, ~5x faster."""
    lightning = wan_lightning_available()
    if lightning:
        steps, cfg = 4, 1.0
    half = max(1, steps // 2)
    negative = negative_prompt or WAN_DEFAULT_NEGATIVE
    if wan_t2v_fp8_available():
        # fp8_scaled experts (safetensors, core UNETLoader) — markedly cleaner
        # hands/anatomy than the Q3 GGUFs; same dual-expert wiring.
        high_loader = {"class_type": "UNETLoader",
                       "inputs": {"unet_name": "video\\wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors",
                                  "weight_dtype": "default"}}
        low_loader = {"class_type": "UNETLoader",
                      "inputs": {"unet_name": "video\\wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors",
                                 "weight_dtype": "default"}}
    else:
        high_loader = {"class_type": "UnetLoaderGGUF",
                       "inputs": {"unet_name": "Wan2.2-T2V-A14B-HighNoise-Q3_K_M.gguf"}}
        low_loader = {"class_type": "UnetLoaderGGUF",
                      "inputs": {"unet_name": "Wan2.2-T2V-A14B-LowNoise-Q3_K_M.gguf"}}
    graph = {
        "1": {"class_type": "CLIPLoaderGGUF",
              "inputs": {"clip_name": "video\\umt5-xxl-encoder-Q8_0.gguf", "type": "wan"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": prompt}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": negative}},
        "4": high_loader,
        "5": low_loader,
        "6": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["4", 0], "shift": 5.0}},
        "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["5", 0], "shift": 5.0}},
        "8": {"class_type": "VAELoader", "inputs": {"vae_name": "video\\wan_2.1_vae.safetensors"}},
        "9": {"class_type": "EmptyHunyuanLatentVideo",
              "inputs": {"width": width, "height": height, "length": video_frames, "batch_size": 1}},
        "10": {"class_type": "KSamplerAdvanced", "inputs": {
            "model": ["6", 0], "positive": ["2", 0], "negative": ["3", 0],
            "latent_image": ["9", 0],
            "add_noise": "enable", "noise_seed": seed, "steps": steps, "cfg": cfg,
            "sampler_name": "euler", "scheduler": "simple",
            "start_at_step": 0, "end_at_step": half, "return_with_leftover_noise": "enable",
        }},
        "11": {"class_type": "KSamplerAdvanced", "inputs": {
            "model": ["7", 0], "positive": ["2", 0], "negative": ["3", 0],
            "latent_image": ["10", 0],
            "add_noise": "disable", "noise_seed": 0, "steps": steps, "cfg": cfg,
            "sampler_name": "euler", "scheduler": "simple",
            "start_at_step": half, "end_at_step": 10000, "return_with_leftover_noise": "disable",
        }},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["8", 0]}},
        "13": {"class_type": "CreateVideo", "inputs": {"images": ["12", 0], "fps": fps}},
        "14": {"class_type": "SaveVideo", "inputs": {
            "video": ["13", 0], "filename_prefix": "aegis/wan22", "format": "mp4", "codec": "h264",
        }},
    }
    if lightning:
        graph["21"] = {"class_type": "LoraLoaderModelOnly",
                       "inputs": {"model": ["4", 0], "lora_name": WAN_LIGHTNING_HIGH_LORA, "strength_model": 1.0}}
        graph["22"] = {"class_type": "LoraLoaderModelOnly",
                       "inputs": {"model": ["5", 0], "lora_name": WAN_LIGHTNING_LOW_LORA, "strength_model": 1.0}}
        graph["6"]["inputs"]["model"] = ["21", 0]
        graph["7"]["inputs"]["model"] = ["22", 0]
    return graph


def wan22_i2v_graph(
    prompt: str,
    negative_prompt: str = "",
    width: int = 640,
    height: int = 640,
    video_frames: int = 81,
    fps: int = 16,
    seed: int = 0,
    steps: int = 20,
    cfg: float = 3.5,
    init_image: str = "",
) -> Dict[str, Any]:
    """Wan2.2 I2V A14B (fp8_scaled experts) — animate a still, per the bundled
    video_wan2_2_14B_i2v template. `init_image` names a file inside ComfyUI's
    input dir (the client uploads it there first). WanImageToVideo builds the
    image-conditioned latent; the dual experts split the schedule exactly like
    the T2V graph. Seko-V1 4-step LoRAs wire in when present (steps 4, cfg 1,
    split at 2)."""
    if not init_image:
        raise ValueError("Wan2.2 I2V needs an init image")
    lightning = wan_i2v_lightning_available()
    if lightning:
        steps, cfg = 4, 1.0
    half = max(1, steps // 2)
    negative = negative_prompt or WAN_DEFAULT_NEGATIVE
    graph = {
        "1": {"class_type": "CLIPLoaderGGUF",
              "inputs": {"clip_name": "video\\umt5-xxl-encoder-Q8_0.gguf", "type": "wan"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": prompt}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": negative}},
        "4": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "video\\wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors",
                         "weight_dtype": "default"}},
        "5": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "video\\wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors",
                         "weight_dtype": "default"}},
        "6": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["4", 0], "shift": 5.0}},
        "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["5", 0], "shift": 5.0}},
        "8": {"class_type": "VAELoader", "inputs": {"vae_name": "video\\wan_2.1_vae.safetensors"}},
        "9": {"class_type": "LoadImage", "inputs": {"image": init_image}},
        "10": {"class_type": "WanImageToVideo", "inputs": {
            "positive": ["2", 0], "negative": ["3", 0], "vae": ["8", 0],
            "start_image": ["9", 0],
            "width": width, "height": height, "length": video_frames, "batch_size": 1,
        }},
        "11": {"class_type": "KSamplerAdvanced", "inputs": {
            "model": ["6", 0], "positive": ["10", 0], "negative": ["10", 1],
            "latent_image": ["10", 2],
            "add_noise": "enable", "noise_seed": seed, "steps": steps, "cfg": cfg,
            "sampler_name": "euler", "scheduler": "simple",
            "start_at_step": 0, "end_at_step": half, "return_with_leftover_noise": "enable",
        }},
        "12": {"class_type": "KSamplerAdvanced", "inputs": {
            "model": ["7", 0], "positive": ["10", 0], "negative": ["10", 1],
            "latent_image": ["11", 0],
            "add_noise": "disable", "noise_seed": 0, "steps": steps, "cfg": cfg,
            "sampler_name": "euler", "scheduler": "simple",
            "start_at_step": half, "end_at_step": 10000, "return_with_leftover_noise": "disable",
        }},
        "13": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["8", 0]}},
        "14": {"class_type": "CreateVideo", "inputs": {"images": ["13", 0], "fps": fps}},
        "15": {"class_type": "SaveVideo", "inputs": {
            "video": ["14", 0], "filename_prefix": "aegis/wan22_i2v", "format": "mp4", "codec": "h264",
        }},
    }
    if lightning:
        graph["21"] = {"class_type": "LoraLoaderModelOnly",
                       "inputs": {"model": ["4", 0], "lora_name": WAN_I2V_LIGHTNING_HIGH_LORA, "strength_model": 1.0}}
        graph["22"] = {"class_type": "LoraLoaderModelOnly",
                       "inputs": {"model": ["5", 0], "lora_name": WAN_I2V_LIGHTNING_LOW_LORA, "strength_model": 1.0}}
        graph["6"]["inputs"]["model"] = ["21", 0]
        graph["7"]["inputs"]["model"] = ["22", 0]
    return graph


def hunyuanvideo15_i2v_graph(
    prompt: str,
    negative_prompt: str = "",
    width: int = 832,
    height: int = 480,
    video_frames: int = 121,
    fps: int = 24,
    seed: int = 0,
    steps: int = 20,
    cfg: float = 1.0,
    init_image: str = "",
) -> Dict[str, Any]:
    """HunyuanVideo 1.5 720p I2V, cfg-distilled fp8_scaled (no CFG pass →
    ~2x faster per step than the T2V base). Per the bundled i2v template:
    sigclip vision encodes the still alongside the VAE-conditioned latent."""
    if not init_image:
        raise ValueError("HunyuanVideo 1.5 I2V needs an init image")
    width = max(256, int(width) // 16 * 16)
    height = max(256, int(height) // 16 * 16)
    return {
        "1": {"class_type": "DualCLIPLoader", "inputs": {
            "clip_name1": "video\\qwen_2.5_vl_7b_fp8_scaled.safetensors",
            "clip_name2": "video\\byt5_small_glyphxl_fp16.safetensors",
            "type": "hunyuan_video_15",
        }},
        "2": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "video\\hunyuanvideo1.5_720p_i2v_cfg_distilled_fp8_scaled.safetensors",
            "weight_dtype": "default",
        }},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "video\\hunyuanvideo15_vae_fp16.safetensors"}},
        "4": {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": "video\\sigclip_vision_patch14_384.safetensors"}},
        "5": {"class_type": "LoadImage", "inputs": {"image": init_image}},
        "6": {"class_type": "CLIPVisionEncode", "inputs": {"clip_vision": ["4", 0], "image": ["5", 0], "crop": "center"}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": prompt}},
        "8": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": negative_prompt or ""}},
        "9": {"class_type": "HunyuanVideo15ImageToVideo", "inputs": {
            "positive": ["7", 0], "negative": ["8", 0], "vae": ["3", 0],
            "width": width, "height": height, "length": video_frames, "batch_size": 1,
            "start_image": ["5", 0], "clip_vision_output": ["6", 0],
        }},
        "10": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["2", 0], "shift": 7}},
        "11": {"class_type": "BasicScheduler",
               "inputs": {"model": ["2", 0], "scheduler": "simple", "steps": int(steps), "denoise": 1}},
        "12": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "13": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "14": {"class_type": "CFGGuider",
               "inputs": {"model": ["10", 0], "positive": ["9", 0], "negative": ["9", 1], "cfg": float(cfg)}},
        "15": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["12", 0], "guider": ["14", 0], "sampler": ["13", 0],
            "sigmas": ["11", 0], "latent_image": ["9", 2],
        }},
        "16": {"class_type": "VAEDecodeTiled", "inputs": {
            "samples": ["15", 0], "vae": ["3", 0],
            "tile_size": 512, "overlap": 64, "temporal_size": 64, "temporal_overlap": 8,
        }},
        "17": {"class_type": "CreateVideo", "inputs": {"images": ["16", 0], "fps": float(fps)}},
        "18": {"class_type": "SaveVideo", "inputs": {
            "video": ["17", 0], "filename_prefix": "aegis/hunyuan15_i2v", "format": "mp4", "codec": "h264",
        }},
    }


# The distilled LTX-2 sigma schedule from the bundled template — the phr00t
# 19B merges are step-distilled, so the fixed 8-step schedule replaces a
# steps/cfg sweep.
_LTX2_DISTILLED_SIGMAS = "1., 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"


def ltx2_fast_t2v_graph(
    prompt: str,
    negative_prompt: str = "",
    width: int = 768,
    height: int = 512,
    video_frames: int = 121,
    fps: int = 24,
    seed: int = 0,
    unet: str = "ltx2-phr00tmerge-sfw-v5-Q5_0.gguf",
) -> Dict[str, Any]:
    """LTX-2 19B phr00t merge (GGUF), single-stage text-to-video WITH audio.

    Adapted from the video_ltx2_t2v_distilled template minus the two-stage
    latent-upscale pass (render at target size directly): joint audio+video
    latents are concatenated, sampled once over the distilled schedule, then
    split and decoded separately. Dims snap to /32, frames to the 8k+1
    pattern LTX is trained on.
    """
    width = max(256, int(width) // 32 * 32)
    height = max(256, int(height) // 32 * 32)
    video_frames = max(9, (int(video_frames) + 3) // 8 * 8 + 1)
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": unet}},
        "2": {"class_type": "LTXAVTextEncoderLoader", "inputs": {
            "text_encoder": "video\\gemma_3_12B_it_fp4_mixed.safetensors",
            "ckpt_name": "video\\ltx-2-19b-embeddings_connector_distill_bf16.safetensors",
            "device": "default",
        }},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "video\\LTX2_video_vae_bf16.safetensors"}},
        "4": {"class_type": "LTXVAudioVAELoader", "inputs": {"ckpt_name": "video\\LTX2_audio_vae_bf16.safetensors"}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": negative_prompt or ""}},
        "7": {"class_type": "LTXVConditioning",
              "inputs": {"positive": ["5", 0], "negative": ["6", 0], "frame_rate": float(fps)}},
        "8": {"class_type": "EmptyLTXVLatentVideo",
              "inputs": {"width": width, "height": height, "length": video_frames, "batch_size": 1}},
        "9": {"class_type": "LTXVEmptyLatentAudio", "inputs": {
            "frames_number": video_frames, "frame_rate": fps, "batch_size": 1, "audio_vae": ["4", 0],
        }},
        "10": {"class_type": "LTXVConcatAVLatent",
               "inputs": {"video_latent": ["8", 0], "audio_latent": ["9", 0]}},
        "11": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "12": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler_ancestral"}},
        "13": {"class_type": "ManualSigmas", "inputs": {"sigmas": _LTX2_DISTILLED_SIGMAS}},
        "14": {"class_type": "CFGGuider",
               "inputs": {"model": ["1", 0], "positive": ["7", 0], "negative": ["7", 1], "cfg": 1.0}},
        "15": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["11", 0], "guider": ["14", 0], "sampler": ["12", 0],
            "sigmas": ["13", 0], "latent_image": ["10", 0],
        }},
        "16": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["15", 0]}},
        "17": {"class_type": "VAEDecodeTiled", "inputs": {
            "samples": ["16", 0], "vae": ["3", 0],
            "tile_size": 512, "overlap": 64, "temporal_size": 4096, "temporal_overlap": 8,
        }},
        "18": {"class_type": "LTXVAudioVAEDecode", "inputs": {"samples": ["16", 1], "audio_vae": ["4", 0]}},
        "19": {"class_type": "CreateVideo",
               "inputs": {"images": ["17", 0], "fps": float(fps), "audio": ["18", 0]}},
        "20": {"class_type": "SaveVideo", "inputs": {
            "video": ["19", 0], "filename_prefix": "aegis/ltx2fast", "format": "mp4", "codec": "h264",
        }},
    }


def flux2_klein_t2i_graph(
    prompt: str,
    negative_prompt: str = "",
    width: int = 1024,
    height: int = 1024,
    seed: int = 0,
    steps: int = 20,
) -> Dict[str, Any]:
    """FLUX.2-klein 9B (GGUF diffusion + Qwen3-8B text encoder), per the
    image_flux2_klein_text_to_image template's distilled branch (CFG 1)."""
    width = max(256, int(width) // 16 * 16)
    height = max(256, int(height) // 16 * 16)
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "flux-2-klein-9b-BF16.gguf"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "qwen_3_8b_fp8mixed.safetensors", "type": "flux2"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2-vae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": negative_prompt or ""}},
        "6": {"class_type": "EmptyFlux2LatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "7": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "9": {"class_type": "Flux2Scheduler", "inputs": {"steps": int(steps), "width": width, "height": height}},
        "10": {"class_type": "CFGGuider",
               "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "cfg": 1.0}},
        "11": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["7", 0], "guider": ["10", 0], "sampler": ["8", 0],
            "sigmas": ["9", 0], "latent_image": ["6", 0],
        }},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "13": {"class_type": "SaveImage", "inputs": {"images": ["12", 0], "filename_prefix": "aegis/klein"}},
    }


def zimage_turbo_t2i_graph(
    prompt: str,
    negative_prompt: str = "",
    width: int = 1024,
    height: int = 1024,
    seed: int = 0,
    steps: int = 8,
) -> Dict[str, Any]:
    """Z-Image-Turbo 6B (8-step distilled) per the bundled template: lumina2
    CLIP type on the qwen_3_4b encoder, res_multistep sampler, AuraFlow shift
    3, cfg 1 with a zeroed negative (the template has no negative-text path —
    negative_prompt is accepted but unused)."""
    width = max(256, int(width) // 16 * 16)
    height = max(256, int(height) // 16 * 16)
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "image\\z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "image\\qwen_3_4b.safetensors", "type": "lumina2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "image\\ae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "EmptySD3LatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "7": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3}},
        "8": {"class_type": "KSampler", "inputs": {
            "model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0],
            "latent_image": ["6", 0],
            "seed": seed, "steps": int(steps), "cfg": 1.0,
            "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 1.0,
        }},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "aegis/zimage"}},
    }


# The template ships a long quality-negative that materially helps Chroma.
_CHROMA_DEFAULT_NEGATIVE = (
    "This low quality greyscale unfinished sketch is inaccurate and flawed. The image is "
    "very blurred and lacks detail with excessive chromatic aberrations and artifacts. The "
    "image is overly saturated with excessive bloom. It has a toony aesthetic with bold "
    "outlines and flat colors."
)


def chroma_hd_t2i_graph(
    prompt: str,
    negative_prompt: str = "",
    width: int = 1024,
    height: int = 1024,
    seed: int = 0,
    steps: int = 26,
) -> Dict[str, Any]:
    """Chroma1-HD 8.9B (uncensored FLUX-lineage) per the bundled
    image_chroma_text_to_image template: t5xxl-only conditioning (CLIPLoader
    type 'chroma'), AuraFlow shift 1 (the model creator's intended value),
    beta scheduler, real CFG 3.5 — Chroma is NOT cfg-distilled. VAE is the
    standard FLUX autoencoder (shared with Z-Image as image/ae.safetensors).
    The bf16 checkpoint (~17.8GB) leans on Comfy's offload alongside the fp8
    t5xxl on a 24GB card."""
    width = max(256, int(width) // 16 * 16)
    height = max(256, int(height) // 16 * 16)
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "image\\Chroma1-HD.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "image\\t5xxl_fp8_e4m3fn_scaled.safetensors", "type": "chroma"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "image\\ae.safetensors"}},
        "4": {"class_type": "T5TokenizerOptions",
              "inputs": {"clip": ["2", 0], "min_padding": 0, "min_length": 0}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["4", 0], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["4", 0], "text": negative_prompt or _CHROMA_DEFAULT_NEGATIVE}},
        "7": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 1.0}},
        "8": {"class_type": "EmptySD3LatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "10": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "11": {"class_type": "BasicScheduler",
               "inputs": {"model": ["7", 0], "scheduler": "beta", "steps": int(steps), "denoise": 1.0}},
        "12": {"class_type": "CFGGuider",
               "inputs": {"model": ["7", 0], "positive": ["5", 0], "negative": ["6", 0], "cfg": 3.5}},
        "13": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["9", 0], "guider": ["12", 0], "sampler": ["10", 0],
            "sigmas": ["11", 0], "latent_image": ["8", 0],
        }},
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["3", 0]}},
        "15": {"class_type": "SaveImage", "inputs": {"images": ["14", 0], "filename_prefix": "aegis/chroma"}},
    }


_NOOBAI_DEFAULT_NEGATIVE = (
    "worst quality, low quality, bad anatomy, bad hands, missing fingers, "
    "extra digit, fewer digits, watermark, signature"
)


def noobai_vpred_t2i_graph(
    prompt: str,
    negative_prompt: str = "",
    width: int = 1024,
    height: int = 1024,
    seed: int = 0,
    steps: int = 28,
) -> Dict[str, Any]:
    """NoobAI-XL V-Pred 1.0 (anime/illustration, danbooru-tag prompting).
    V-prediction + ZTSNR checkpoint: ModelSamplingDiscrete(v_prediction,
    zsnr=True) is REQUIRED (omitting zsnr silently fries the output) and
    RescaleCFG 0.7 per the model card; euler (ancestral samplers misbehave on
    vpred+ZTSNR), cfg 5, clip skip 2. SDXL buckets like /64 dims."""
    width = max(512, int(width) // 64 * 64)
    height = max(512, int(height) // 64 * 64)
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "image\\NoobAI-XL-Vpred-v1.0.safetensors"}},
        "2": {"class_type": "ModelSamplingDiscrete",
              "inputs": {"model": ["1", 0], "sampling": "v_prediction", "zsnr": True}},
        "3": {"class_type": "RescaleCFG", "inputs": {"model": ["2", 0], "multiplier": 0.7}},
        "4": {"class_type": "CLIPSetLastLayer", "inputs": {"clip": ["1", 1], "stop_at_clip_layer": -2}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["4", 0], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["4", 0], "text": negative_prompt or _NOOBAI_DEFAULT_NEGATIVE}},
        "7": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {
            "model": ["3", 0], "positive": ["5", 0], "negative": ["6", 0],
            "latent_image": ["7", 0],
            "seed": seed, "steps": int(steps), "cfg": 5.0,
            "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
        }},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["1", 2]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "aegis/noobai"}},
    }


def krea2_turbo_t2i_graph(
    prompt: str,
    negative_prompt: str = "",
    width: int = 1024,
    height: int = 1024,
    seed: int = 0,
    steps: int = 8,
) -> Dict[str, Any]:
    """Krea 2 Turbo (12.9B, 8-step distilled, 2K-native aesthetic) per the
    bundled template: no model-sampling node at all — the UNet feeds KSampler
    directly; euler/simple, cfg 1 with zeroed negative; Qwen-Image VAE.
    The local checkpoint is the Winnougan mxfp8 repack."""
    width = max(256, int(width) // 16 * 16)
    height = max(256, int(height) // 16 * 16)
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "image\\krea2_turbo_mxfp8.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "image\\qwen3vl_4b_fp8_scaled.safetensors", "type": "krea2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen-image\\qwen_image_vae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "7": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0],
            "latent_image": ["6", 0],
            "seed": seed, "steps": int(steps), "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
        }},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "aegis/krea2"}},
    }


def ideogram4_t2i_graph(
    prompt: str,
    negative_prompt: str = "",
    width: int = 1024,
    height: int = 1024,
    seed: int = 0,
    steps: int = 20,
) -> Dict[str, Any]:
    """Ideogram 4.0 fp8 (typography/design specialist) per the bundled
    template: TWO diffusion models — DualModelGuider runs the unconditional
    pass on the second — plus a Qwen3-VL-8B text encoder (CLIPLoader type
    'ideogram4') and the FLUX.2 VAE. CFGOverride ramps cfg 3.0 over the last
    30%; Ideogram4Scheduler defaults (20 steps, mu 0, std 1.75). Prompts may
    be structured JSON captions for layout control. Non-commercial license."""
    width = max(256, (int(width) + 15) // 16 * 16)
    height = max(256, (int(height) + 15) // 16 * 16)
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "image\\ideogram4_fp8_scaled.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "image\\ideogram4_unconditional_fp8_scaled.safetensors", "weight_dtype": "default"}},
        "3": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "image\\qwen3vl_8b_fp8_scaled.safetensors", "type": "ideogram4", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2-vae.safetensors"}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["3", 0], "text": prompt}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "7": {"class_type": "EmptyFlux2LatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "8": {"class_type": "CFGOverride",
              "inputs": {"model": ["1", 0], "cfg": 3.0, "start_percent": 0.7, "end_percent": 1.0}},
        "9": {"class_type": "DualModelGuider", "inputs": {
            "model": ["8", 0], "model_negative": ["2", 0],
            "positive": ["5", 0], "negative": ["6", 0], "cfg": 7.0,
        }},
        "10": {"class_type": "Ideogram4Scheduler",
               "inputs": {"steps": int(steps), "width": width, "height": height, "mu": 0.0, "std": 1.75}},
        "11": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "12": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "13": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["11", 0], "guider": ["9", 0], "sampler": ["12", 0],
            "sigmas": ["10", 0], "latent_image": ["7", 0],
        }},
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["4", 0]}},
        "15": {"class_type": "SaveImage", "inputs": {"images": ["14", 0], "filename_prefix": "aegis/ideogram4"}},
    }


def hunyuanvideo15_t2v_graph(
    prompt: str,
    negative_prompt: str = "",
    width: int = 832,
    height: int = 480,
    video_frames: int = 121,
    fps: int = 24,
    seed: int = 0,
    steps: int = 20,
    cfg: float = 6.0,
) -> Dict[str, Any]:
    """HunyuanVideo 1.5 720p T2V (8.3B DiT), per the bundled
    video_hunyuan_video_1.5_720p_t2v template's base pass — the optional
    1080p latent-super-resolution second stage is skipped, same as the Wan
    and LTX builders render at target size directly.

    weight_dtype fp8_e4m3fn casts the fp16 checkpoint at load (the
    template's own guidance for sub-32GB cards): 8.3GB resident instead of
    16.6GB, leaving 720p×121-frame activations comfortable on the 24GB 4090.
    The template ships 20 steps as its speed/quality default (50 = original
    quality); shift 7 / cfg 6 / euler+simple per its sampling chain."""
    width = max(256, int(width) // 16 * 16)
    height = max(256, int(height) // 16 * 16)
    return {
        "1": {"class_type": "DualCLIPLoader", "inputs": {
            "clip_name1": "video\\qwen_2.5_vl_7b_fp8_scaled.safetensors",
            "clip_name2": "video\\byt5_small_glyphxl_fp16.safetensors",
            "type": "hunyuan_video_15",
        }},
        "2": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "video\\hunyuanvideo1.5_720p_t2v_fp16.safetensors",
            "weight_dtype": "fp8_e4m3fn",
        }},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "video\\hunyuanvideo15_vae_fp16.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": prompt}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": negative_prompt or ""}},
        "6": {"class_type": "EmptyHunyuanVideo15Latent",
              "inputs": {"width": width, "height": height, "length": video_frames, "batch_size": 1}},
        "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["2", 0], "shift": 7}},
        "8": {"class_type": "BasicScheduler",
              "inputs": {"model": ["7", 0], "scheduler": "simple", "steps": int(steps), "denoise": 1}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "10": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "11": {"class_type": "CFGGuider",
               "inputs": {"model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0], "cfg": float(cfg)}},
        "12": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["9", 0], "guider": ["11", 0], "sampler": ["10", 0],
            "sigmas": ["8", 0], "latent_image": ["6", 0],
        }},
        "13": {"class_type": "VAEDecodeTiled", "inputs": {
            "samples": ["12", 0], "vae": ["3", 0],
            "tile_size": 512, "overlap": 64, "temporal_size": 64, "temporal_overlap": 8,
        }},
        "14": {"class_type": "CreateVideo", "inputs": {"images": ["13", 0], "fps": float(fps)}},
        "15": {"class_type": "SaveVideo", "inputs": {
            "video": ["14", 0], "filename_prefix": "aegis/hunyuan15", "format": "mp4", "codec": "h264",
        }},
    }


def acestep15_song_graph(
    tags: str,
    lyrics: str = "",
    seconds: float = 60.0,
    seed: int = 0,
    bpm: int = 120,
    language: str = "en",
    reference_audio: str = "",
) -> Dict[str, Any]:
    """ACE-Step 1.5 XL turbo text-to-song (music + vocals when lyrics are
    given), per the bundled audio_ace_step1_5_xl_turbo template: 8-step euler
    at cfg 1 with AuraFlow shift 3, negative from ConditioningZeroOut, MP3
    out. `tags` is the style line ("dreamy indie pop, female vocals, 90 BPM");
    empty `lyrics` yields an instrumental. The encoder's duration must match
    the latent seconds or the structure planner writes past the clip.

    `reference_audio` (a filename inside ComfyUI/input) switches on COVER
    mode: LoadAudio → VAEEncodeAudio (the ACE 1.5 VAE) → ReferenceTimbreAudio
    appended to the positive conditioning — the model then follows the
    reference's melody/structure/timbre (is_covers=True in model_base) and
    DISCARDS LLM audio codes, so generate_audio_codes flips off (the 4B
    code-gen pass would be pure wasted VRAM/time). The negative stays
    ConditioningZeroOut of the RAW text encode, per the model's learned
    null-embedding path. Keep `seconds` ≈ the reference's duration: the
    reference latent is truncated to the target length and silence-padded
    past it (a much longer target trails into unguided audio)."""
    seconds = max(5.0, min(600.0, float(seconds)))
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "audio\\acestep_v1.5_xl_turbo_bf16.safetensors",
            "weight_dtype": "default",
        }},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": "audio\\ace_1.5_vae.safetensors"}},
        "3": {"class_type": "DualCLIPLoader", "inputs": {
            "clip_name1": "audio\\qwen_0.6b_ace15.safetensors",
            "clip_name2": "audio\\qwen_4b_ace15.safetensors",
            "type": "ace",
        }},
        "4": {"class_type": "TextEncodeAceStepAudio1.5", "inputs": {
            "clip": ["3", 0],
            "tags": tags,
            "lyrics": lyrics or "",
            "seed": seed,
            "bpm": int(bpm),
            "duration": seconds,
            "timesignature": "4",
            "language": language or "en",
            "keyscale": "C major",
            "generate_audio_codes": not reference_audio,
            "cfg_scale": 2.0,
            "temperature": 0.85,
            "top_p": 0.9,
            "top_k": 0,
            "min_p": 0.0,
        }},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "EmptyAceStep1.5LatentAudio",
              "inputs": {"seconds": seconds, "batch_size": 1}},
        "7": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3}},
        "8": {"class_type": "KSampler", "inputs": {
            "model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0],
            "latent_image": ["6", 0],
            "seed": seed, "steps": 8, "cfg": 1,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1,
        }},
        "9": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["8", 0], "vae": ["2", 0]}},
        "10": {"class_type": "SaveAudioMP3", "inputs": {
            "audio": ["9", 0], "filename_prefix": "aegis/song", "quality": "V0",
        }},
    }
    if reference_audio:
        graph["11"] = {"class_type": "LoadAudio", "inputs": {"audio": reference_audio}}
        graph["12"] = {"class_type": "VAEEncodeAudio",
                       "inputs": {"audio": ["11", 0], "vae": ["2", 0]}}
        graph["13"] = {"class_type": "ReferenceTimbreAudio",
                       "inputs": {"conditioning": ["4", 0], "latent": ["12", 0]}}
        graph["8"]["inputs"]["positive"] = ["13", 0]
    return graph


def vibevoice_narrate_graph(
    script: str,
    speaker_voices: Optional[Dict[int, str]] = None,
    seed: int = 42,
    diffusion_steps: int = 20,
    cfg_scale: float = 1.3,
    quantize_llm: str = "4bit",
) -> Dict[str, Any]:
    """VibeVoice-Large long-form multi-speaker narration (Enemyx-net
    VibeVoice-ComfyUI node pack; weights in models/vibevoice/VibeVoice-Large).

    `script` uses "[N]: line" speaker labels (N = 1-4; plain text = one
    speaker). `speaker_voices` maps speaker number → a wav/mp3 filename
    ALREADY inside ComfyUI/input (the client stages data/voices samples) —
    that speaker is voice-cloned from it; unmapped speakers get a synthetic
    voice. One pass can run to ~45 minutes of audio.

    quantize_llm "4bit" (nf4, LLM only — diffusion head/connectors stay bf16)
    holds the 18.7GB bf16 checkpoint at ~7GB resident, which is what lets it
    share the 24GB card with everything else; "full precision" is possible
    but leaves no headroom. Attention is pinned to sdpa: the ComfyUI venv has
    neither flash-attn nor sage, and "auto" resolving elsewhere would crash.
    free_memory_after_generate drops the node's model cache after the pass
    (the client's /free then clears the rest)."""
    if not (script or "").strip():
        raise ValueError("Narration needs a script")
    graph: Dict[str, Any] = {
        "1": {"class_type": "VibeVoiceMultipleSpeakersNode", "inputs": {
            "text": script,
            "model": "VibeVoice-Large",
            "attention_type": "sdpa",
            "quantize_llm": quantize_llm,
            "free_memory_after_generate": True,
            "diffusion_steps": int(diffusion_steps),
            "seed": max(0, int(seed)),
            "cfg_scale": float(cfg_scale),
            "use_sampling": False,
        }},
        "2": {"class_type": "SaveAudioMP3", "inputs": {
            "audio": ["1", 0], "filename_prefix": "aegis/narrate", "quality": "V0",
        }},
    }
    for num, staged in sorted((speaker_voices or {}).items()):
        n = int(num)
        if not (1 <= n <= 4) or not staged:
            continue
        node_id = str(10 + n)
        graph[node_id] = {"class_type": "LoadAudio", "inputs": {"audio": staged}}
        graph["1"]["inputs"][f"speaker{n}_voice"] = [node_id, 0]
    return graph


# ── video post-ops: graphs that transform an EXISTING clip ────────────────────
# (foley, restoration/upscale, frame interpolation). The source video must
# already be a file inside ComfyUI's input dir — the client copies it there.

def mmaudio_foley_graph(
    video_file: str,
    prompt: str = "",
    negative_prompt: str = "",
    duration: float = 8.0,
    seed: int = 0,
) -> Dict[str, Any]:
    """MMAudio v2 44kHz video-to-audio foley: sample a soundtrack for a silent
    clip and mux it back at the source framerate. `duration` must be the
    clip's true length in seconds (the sampler slices raw frames by it, it
    does not resample by fps). The nvidia bigvgan vocoder auto-downloads on
    the first 44k run."""
    return {
        "1": {"class_type": "LoadVideo", "inputs": {"file": video_file}},
        "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
        "3": {"class_type": "MMAudioModelLoader",
              "inputs": {"mmaudio_model": "mmaudio_large_44k_v2_fp16.safetensors", "base_precision": "fp16"}},
        "4": {"class_type": "MMAudioFeatureUtilsLoader", "inputs": {
            "vae_model": "mmaudio_vae_44k_fp16.safetensors",
            "synchformer_model": "mmaudio_synchformer_fp16.safetensors",
            "clip_model": "apple_DFN5B-CLIP-ViT-H-14-384_fp16.safetensors",
            "mode": "44k", "precision": "fp16",
        }},
        "5": {"class_type": "MMAudioSampler", "inputs": {
            "mmaudio_model": ["3", 0], "feature_utils": ["4", 0], "images": ["2", 0],
            "duration": float(duration), "steps": 25, "cfg": 4.5, "seed": seed,
            "prompt": prompt or "", "negative_prompt": negative_prompt or "",
            "mask_away_clip": False, "force_offload": True,
        }},
        "6": {"class_type": "CreateVideo",
              "inputs": {"images": ["2", 0], "fps": ["2", 2], "audio": ["5", 0]}},
        "7": {"class_type": "SaveVideo", "inputs": {
            "video": ["6", 0], "filename_prefix": "aegis/mmaudio", "format": "mp4", "codec": "h264",
        }},
    }


def hunyuan_foley_graph(
    video_file: str,
    prompt: str = "",
    negative_prompt: str = "",
    duration: float = 8.0,
    seed: int = 0,
) -> Dict[str, Any]:
    """HunyuanVideo-Foley (XXL fp8) — the quality tier above MMAudio for
    video-to-audio. quantization must EXPLICITLY match the checkpoint
    ('auto' picks e5m2 on this GPU and would lossily re-quantize the e4m3fn
    file). `duration` is the clip's true seconds (1-60; shorter clips get a
    freeze-frame pad). First run auto-downloads siglip2 + CLAP (~3GB) to the
    HF cache. force_offload returns VRAM after the pass."""
    return {
        "1": {"class_type": "LoadVideo", "inputs": {"file": video_file}},
        "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
        "3": {"class_type": "HunyuanModelLoader", "inputs": {
            "model_name": "hunyuanvideo_foley_fp8_e4m3fn.safetensors",
            "precision": "auto", "quantization": "fp8_e4m3fn",
        }},
        "4": {"class_type": "HunyuanDependenciesLoader", "inputs": {
            "vae_name": "vae_128d_48k_fp16.safetensors",
            "synchformer_name": "synchformer_state_dict_fp16.safetensors",
        }},
        "5": {"class_type": "HunyuanFoleySampler", "inputs": {
            "hunyuan_model": ["3", 0], "hunyuan_deps": ["4", 0],
            "image": ["2", 0], "frame_rate": ["2", 2],
            "duration": float(duration),
            "prompt": prompt or "", "negative_prompt": negative_prompt or "noisy, harsh",
            "cfg_scale": 4.5, "steps": 50, "sampler": "euler",
            "batch_size": 1, "seed": seed, "force_offload": True,
        }},
        "6": {"class_type": "CreateVideo",
              "inputs": {"images": ["2", 0], "fps": ["2", 2], "audio": ["5", 0]}},
        "7": {"class_type": "SaveVideo", "inputs": {
            "video": ["6", 0], "filename_prefix": "aegis/hyfoley", "format": "mp4", "codec": "h264",
        }},
    }


def seedvr2_upscale_graph(
    video_file: str,
    resolution: int = 1080,
    seed: int = 0,
    batch_size: int = 5,
    blocks_to_swap: int = 16,
) -> Dict[str, Any]:
    """SeedVR2 7B fp8 diffusion video restore/upscale. `resolution` is the
    TARGET SHORT EDGE. batch_size must be 4n+1 (bigger = better temporal
    consistency, more VRAM); blocks_to_swap ~16 keeps the 7B fp8 inside 24GB.
    Source audio and framerate are carried through the remux."""
    batch_size = max(1, (int(batch_size) - 1) // 4 * 4 + 1)
    return {
        "1": {"class_type": "LoadVideo", "inputs": {"file": video_file}},
        "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
        "3": {"class_type": "SeedVR2LoadDiTModel", "inputs": {
            "model": "seedvr2_ema_7b_fp8_e4m3fn.safetensors", "device": "cuda:0",
            "blocks_to_swap": int(blocks_to_swap), "swap_io_components": False,
            "offload_device": "cpu", "cache_model": False, "attention_mode": "sdpa",
        }},
        "4": {"class_type": "SeedVR2LoadVAEModel", "inputs": {
            "model": "ema_vae_fp16.safetensors", "device": "cuda:0",
            "encode_tiled": True, "encode_tile_size": 1024, "encode_tile_overlap": 128,
            "decode_tiled": True, "decode_tile_size": 768, "decode_tile_overlap": 128,
            "tile_debug": "false", "offload_device": "cpu", "cache_model": False,
        }},
        "5": {"class_type": "SeedVR2VideoUpscaler", "inputs": {
            "image": ["2", 0], "dit": ["3", 0], "vae": ["4", 0],
            "seed": seed, "resolution": int(resolution), "max_resolution": 0,
            "batch_size": batch_size, "uniform_batch_size": True, "temporal_overlap": 2,
            "prepend_frames": 0, "color_correction": "lab",
            "input_noise_scale": 0.0, "latent_noise_scale": 0.0,
            "offload_device": "cpu", "enable_debug": False,
        }},
        "6": {"class_type": "CreateVideo",
              "inputs": {"images": ["5", 0], "fps": ["2", 2], "audio": ["2", 1]}},
        "7": {"class_type": "SaveVideo", "inputs": {
            "video": ["6", 0], "filename_prefix": "aegis/seedvr2", "format": "mp4", "codec": "h264",
        }},
    }


def gimmvfi_interpolate_graph(
    video_file: str,
    factor: int = 2,
    fps_out: float = 32.0,
    seed: int = 0,
) -> Dict[str, Any]:
    """GIMM-VFI frame interpolation: factor N inserts N-1 frames per pair, so
    fps_out must be source_fps * factor (core CreateVideo can't multiply the
    probed fps — the caller computes it). Audio stays in sync (duration is
    unchanged). Model files are hardlinked into ComfyUI/models/interpolation/
    gimm-vfi (the loader hardcodes that path)."""
    return {
        "1": {"class_type": "LoadVideo", "inputs": {"file": video_file}},
        "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
        "3": {"class_type": "DownloadAndLoadGIMMVFIModel", "inputs": {
            "model": "gimmvfi_r_arb_lpips_fp32.safetensors", "precision": "fp32", "torch_compile": False,
        }},
        "4": {"class_type": "GIMMVFI_interpolate", "inputs": {
            "gimmvfi_model": ["3", 0], "images": ["2", 0],
            "ds_factor": 1.0, "interpolation_factor": int(factor), "seed": seed, "output_flows": False,
        }},
        "5": {"class_type": "CreateVideo",
              "inputs": {"images": ["4", 0], "fps": float(fps_out), "audio": ["2", 1]}},
        "6": {"class_type": "SaveVideo", "inputs": {
            "video": ["5", 0], "filename_prefix": "aegis/gimmvfi", "format": "mp4", "codec": "h264",
        }},
    }


# ── image post-ops: transforms over an EXISTING gallery still ────────────────

def nomos_upscale4x_graph(image_file: str, seed: int = 0) -> Dict[str, Any]:
    """4xNomos8kDAT transformer upscaler — fast 4x detail upscale for stills
    (core nodes only; near-zero VRAM). `seed` accepted for API symmetry."""
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_file}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": "4xNomos8kDAT.safetensors"}},
        "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "aegis/upscale4x"}},
    }


def supir_restore_graph(
    image_file: str,
    seed: int = 0,
    prompt: str = "",
    scale_by: float = 2.0,
    steps: int = 30,
) -> Dict[str, Any]:
    """SUPIR v0Q diffusion restoration (kijai wrapper): rebuild detail/faces
    while upscaling `scale_by`x. The wrapper FUSES an SDXL checkpoint into
    SUPIR's own sampler — it is eps-only, so the base MUST be an epsilon
    photoreal SDXL (RealVisXL); a v-pred checkpoint (NoobAI) produces garbage.
    Conditioner latents come from the first-stage pre-pass, sampler latents
    from the encode — the two must not be crossed. Tiled VAE keeps 24GB safe
    to ~2-3K output. SUPIR unloads every other ComfyUI model when it runs.
    Non-commercial license."""
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_file}},
        "2": {"class_type": "ImageScaleBy",
              "inputs": {"image": ["1", 0], "upscale_method": "lanczos", "scale_by": float(scale_by)}},
        # v1 loader reads the SDXL checkpoint itself — the v2 fused path fails
        # to extract CLIP-L from RealVisXL ("Failed to load first clip model").
        "4": {"class_type": "SUPIR_model_loader", "inputs": {
            "supir_model": "image\\SUPIR-v0Q_fp16.safetensors",
            "sdxl_model": "image\\RealVisXL_V4.0.safetensors",
            "fp8_unet": False, "diffusion_dtype": "auto",
        }},
        "5": {"class_type": "SUPIR_first_stage", "inputs": {
            "SUPIR_VAE": ["4", 1], "image": ["2", 0],
            "use_tiled_vae": True, "encoder_tile_size": 512, "decoder_tile_size": 512,
            "encoder_dtype": "auto",
        }},
        "6": {"class_type": "SUPIR_encode", "inputs": {
            "SUPIR_VAE": ["5", 0], "image": ["5", 1],
            "use_tiled_vae": True, "encoder_tile_size": 512, "encoder_dtype": "auto",
        }},
        "7": {"class_type": "SUPIR_conditioner", "inputs": {
            "SUPIR_model": ["4", 0], "latents": ["5", 2],
            "positive_prompt": prompt or "high quality, sharp, detailed photograph",
            "negative_prompt": "bad quality, blurry, messy, lowres, artifacts",
        }},
        "8": {"class_type": "SUPIR_sample", "inputs": {
            "SUPIR_model": ["4", 0], "latents": ["6", 0],
            "positive": ["7", 0], "negative": ["7", 1],
            "seed": seed, "steps": int(steps),
            "cfg_scale_start": 4.0, "cfg_scale_end": 4.0,
            "EDM_s_churn": 5, "s_noise": 1.003, "DPMPP_eta": 1.0,
            "control_scale_start": 1.0, "control_scale_end": 1.0,
            "restore_cfg": -1.0, "keep_model_loaded": False,
            "sampler": "RestoreEDMSampler",
            "sampler_tile_size": 1024, "sampler_tile_stride": 512,
        }},
        "9": {"class_type": "SUPIR_decode", "inputs": {
            "SUPIR_VAE": ["5", 0], "latents": ["8", 0],
            "use_tiled_vae": True, "decoder_tile_size": 512, "decoder_dtype": "auto",
        }},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "aegis/supir"}},
    }


def qwen_controlnet_redraw_graph(
    image_file: str,
    prompt: str = "",
    seed: int = 0,
    strength: float = 0.85,
) -> Dict[str, Any]:
    """Structure-preserving re-render: canny edges from the source photo drive
    the InstantX ControlNet Union over Qwen-Image-2512 (Lightning 8-step).
    The render latent is a VAEEncode of the scaled source, so the output
    matches its resolution; FluxKontextImageScale normalizes to ~1MP. The
    negative prompt is moot at cfg 1 (distilled)."""
    return {
        "1": {"class_type": "CLIPLoaderGGUF",
              "inputs": {"clip_name": "qwen-image\\Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf", "type": "qwen_image"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": prompt or "a high quality detailed photograph"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": ""}},
        "4": {"class_type": "UnetLoaderGGUF",
              "inputs": {"unet_name": "qwen-image\\qwen-image-2512-Q8_0.gguf"}},
        "5": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["4", 0], "lora_name": "Qwen-Image-Lightning-8steps-V2.0-bf16.safetensors",
            "strength_model": 1.0,
        }},
        "6": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["5", 0], "shift": 3.1}},
        "7": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen-image\\qwen_image_vae.safetensors"}},
        "8": {"class_type": "LoadImage", "inputs": {"image": image_file}},
        "9": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["8", 0]}},
        "16": {"class_type": "Canny",
               "inputs": {"image": ["9", 0], "low_threshold": 0.33, "high_threshold": 0.35}},
        "10": {"class_type": "ControlNetLoader",
               "inputs": {"control_net_name": "Qwen-Image-InstantX-ControlNet-Union.safetensors"}},
        "11": {"class_type": "ControlNetApplyAdvanced", "inputs": {
            "positive": ["2", 0], "negative": ["3", 0], "control_net": ["10", 0],
            "image": ["16", 0], "vae": ["7", 0],
            "strength": float(strength), "start_percent": 0.0, "end_percent": 1.0,
        }},
        "12": {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 0], "vae": ["7", 0]}},
        "13": {"class_type": "KSampler", "inputs": {
            "model": ["6", 0], "positive": ["11", 0], "negative": ["11", 1],
            "latent_image": ["12", 0],
            "seed": seed, "steps": 8, "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
        }},
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["7", 0]}},
        "15": {"class_type": "SaveImage", "inputs": {"images": ["14", 0], "filename_prefix": "aegis/qwen_cn"}},
    }


# Image post-ops catalog — what /api/image/enhance accepts.
COMFY_IMAGE_OPS: Dict[str, Dict[str, Any]] = {
    "upscale4x": {
        "builder": nomos_upscale4x_graph,
        "label": "Upscale 4x (Nomos DAT)",
        "required_files": ["upscale_models/4xNomos8kDAT.safetensors"],
    },
    "restore": {
        "builder": supir_restore_graph,
        "label": "Restore & enhance (SUPIR)",
        "required_files": [
            "image/SUPIR-v0Q_fp16.safetensors",
            "image/RealVisXL_V4.0.safetensors",
        ],
    },
    "redraw": {
        "builder": qwen_controlnet_redraw_graph,
        "label": "Redraw — same structure, new render (ControlNet)",
        "required_files": [
            "qwen-image/qwen-image-2512-Q8_0.gguf",
            "qwen-image/Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf",
            "qwen-image/qwen_image_vae.safetensors",
            "controlnet/Qwen-Image-InstantX-ControlNet-Union.safetensors",
            "loras/Qwen-Image-Lightning-8steps-V2.0-bf16.safetensors",
        ],
    },
}


def available_image_ops() -> List[str]:
    return _available(COMFY_IMAGE_OPS)


def build_image_op_graph(op_id: str, **params) -> Optional[Dict[str, Any]]:
    spec = COMFY_IMAGE_OPS.get(op_id)
    if not spec:
        return None
    return spec["builder"](**params)


# Video post-ops catalog — file-gated like the model catalogs. The op ids are
# what /api/video/enhance accepts.
COMFY_VIDEO_OPS: Dict[str, Dict[str, Any]] = {
    "foley": {
        "builder": mmaudio_foley_graph,
        "label": "Add sound (MMAudio foley)",
        "required_files": [
            "audio/mmaudio_large_44k_v2_fp16.safetensors",
            "audio/mmaudio_vae_44k_fp16.safetensors",
            "audio/mmaudio_synchformer_fp16.safetensors",
            "audio/apple_DFN5B-CLIP-ViT-H-14-384_fp16.safetensors",
        ],
    },
    "foley-hq": {
        "builder": hunyuan_foley_graph,
        "label": "Add sound HQ (HunyuanVideo-Foley)",
        "required_files": [
            "audio/hunyuanvideo_foley_fp8_e4m3fn.safetensors",
            "audio/vae_128d_48k_fp16.safetensors",
            "audio/synchformer_state_dict_fp16.safetensors",
        ],
    },
    "upscale": {
        "builder": seedvr2_upscale_graph,
        "label": "Restore & upscale (SeedVR2)",
        "required_files": [
            "video/seedvr2_ema_7b_fp8_e4m3fn.safetensors",
            "video/ema_vae_fp16.safetensors",
        ],
    },
    "interpolate": {
        "builder": gimmvfi_interpolate_graph,
        "label": "Smooth motion 2x (GIMM-VFI)",
        "required_files": [
            "video/gimmvfi_r_arb_lpips_fp32.safetensors",
            "video/raft-things_fp32.safetensors",
        ],
    },
}


def available_video_ops() -> List[str]:
    return _available(COMFY_VIDEO_OPS)


def build_video_op_graph(op_id: str, **params) -> Optional[Dict[str, Any]]:
    spec = COMFY_VIDEO_OPS.get(op_id)
    if not spec:
        return None
    return spec["builder"](**params)


# Aegis-facing ComfyUI music catalog — same file-gated pattern as video.
# "kind" separates song models (the /api/audio/generate + tags/lyrics call
# shape) from narration models (/api/audio/narrate + script/speaker-voices):
# their builders take disjoint kwargs, so a request routed to the wrong
# endpoint must be rejected up front instead of TypeError-ing in the builder.
COMFY_AUDIO_MODELS: Dict[str, Dict[str, Any]] = {
    "comfy-acestep1.5-song": {
        "builder": acestep15_song_graph,
        "label": "ACE-Step 1.5 XL turbo (song / music)",
        "kind": "song",
        "required_files": [
            "audio/acestep_v1.5_xl_turbo_bf16.safetensors",
            "audio/qwen_0.6b_ace15.safetensors",
            "audio/qwen_4b_ace15.safetensors",
            "audio/ace_1.5_vae.safetensors",
        ],
    },
    "comfy-vibevoice-narrate": {
        "builder": vibevoice_narrate_graph,
        "label": "VibeVoice — long-form narration (up to 4 speakers)",
        "kind": "narration",
        # The whole 10-shard snapshot is hardlinked under models/vibevoice/
        # (the node pack scans that dir); gating on the index + config + the
        # Qwen tokenizer the pack's processor requires catches a half-staged
        # install without stat-ing all 18.7GB of shards.
        "required_files": [
            "vibevoice/VibeVoice-Large/model.safetensors.index.json",
            "vibevoice/VibeVoice-Large/config.json",
            "vibevoice/tokenizer/tokenizer_config.json",
            "vibevoice/tokenizer/vocab.json",
            "vibevoice/tokenizer/merges.txt",
        ],
    },
}


def available_audio_models() -> List[str]:
    """Comfy audio model ids whose required files all exist on disk."""
    return _available(COMFY_AUDIO_MODELS)


def audio_model_kind(model_id: str) -> str:
    """'song' | 'narration' for a catalog id (unknown ids default to song)."""
    return COMFY_AUDIO_MODELS.get(model_id, {}).get("kind", "song")


def build_audio_graph(model_id: str, **params) -> Optional[Dict[str, Any]]:
    spec = COMFY_AUDIO_MODELS.get(model_id)
    if not spec:
        return None
    return spec["builder"](**params)


_LTX2_COMPANIONS = [
    "video/gemma_3_12B_it_fp4_mixed.safetensors",
    "video/ltx-2-19b-embeddings_connector_distill_bf16.safetensors",
    "video/LTX2_video_vae_bf16.safetensors",
    "video/LTX2_audio_vae_bf16.safetensors",
]

# Aegis-facing ComfyUI model catalog. required_files are relative to models/ —
# an entry is only offered when every file exists, so a missing companion
# shows up as an absent option instead of a mid-render crash.
COMFY_VIDEO_MODELS: Dict[str, Dict[str, Any]] = {
    "comfy-wan2.2-t2v": {
        "builder": wan22_t2v_graph,
        "label": "Wan2.2 T2V 14B (ComfyUI)",
        "required_files": [
            "Wan2.2-T2V-A14B-HighNoise-Q3_K_M.gguf",
            "Wan2.2-T2V-A14B-LowNoise-Q3_K_M.gguf",
            "video/umt5-xxl-encoder-Q8_0.gguf",
            "video/wan_2.1_vae.safetensors",
        ],
    },
    "comfy-wan2.2-i2v": {
        "builder": wan22_i2v_graph,
        "label": "Wan2.2 I2V 14B fp8 — animate a still (ComfyUI)",
        "required_files": [
            WAN_I2V_FP8_HIGH,
            WAN_I2V_FP8_LOW,
            "video/umt5-xxl-encoder-Q8_0.gguf",
            "video/wan_2.1_vae.safetensors",
        ],
    },
    "comfy-hunyuanvideo1.5-t2v": {
        "builder": hunyuanvideo15_t2v_graph,
        "label": "HunyuanVideo 1.5 720p T2V (ComfyUI)",
        "required_files": [
            "video/hunyuanvideo1.5_720p_t2v_fp16.safetensors",
            "video/qwen_2.5_vl_7b_fp8_scaled.safetensors",
            "video/byt5_small_glyphxl_fp16.safetensors",
            "video/hunyuanvideo15_vae_fp16.safetensors",
        ],
    },
    "comfy-hunyuanvideo1.5-i2v": {
        "builder": hunyuanvideo15_i2v_graph,
        "label": "HunyuanVideo 1.5 720p I2V — animate a still (ComfyUI)",
        "required_files": [
            "video/hunyuanvideo1.5_720p_i2v_cfg_distilled_fp8_scaled.safetensors",
            "video/qwen_2.5_vl_7b_fp8_scaled.safetensors",
            "video/byt5_small_glyphxl_fp16.safetensors",
            "video/hunyuanvideo15_vae_fp16.safetensors",
            "video/sigclip_vision_patch14_384.safetensors",
        ],
    },
    "comfy-ltx2-19b-fast": {
        "builder": lambda **kw: ltx2_fast_t2v_graph(unet="ltx2-phr00tmerge-sfw-v5-Q5_0.gguf", **kw),
        "label": "LTX-2 19B phr00t fast (ComfyUI, video+audio)",
        "required_files": ["ltx2-phr00tmerge-sfw-v5-Q5_0.gguf"] + _LTX2_COMPANIONS,
    },
    "comfy-ltx2-19b-fast-nsfw": {
        "builder": lambda **kw: ltx2_fast_t2v_graph(unet="ltx2-phr00tmerge-nsfw-v62-Q4_0.gguf", **kw),
        "label": "LTX-2 19B phr00t fast NSFW (ComfyUI, video+audio)",
        "required_files": ["ltx2-phr00tmerge-nsfw-v62-Q4_0.gguf"] + _LTX2_COMPANIONS,
    },
}

COMFY_IMAGE_MODELS: Dict[str, Dict[str, Any]] = {
    "comfy-flux2-klein": {
        "builder": flux2_klein_t2i_graph,
        "label": "FLUX.2-klein 9B (ComfyUI)",
        "required_files": [
            "flux-2-klein-9b-BF16.gguf",
            "qwen_3_8b_fp8mixed.safetensors",
            "flux2-vae.safetensors",
        ],
    },
    "comfy-z-image-turbo": {
        "builder": zimage_turbo_t2i_graph,
        "label": "Z-Image-Turbo 6B — fast photoreal (ComfyUI)",
        "required_files": [
            "image/z_image_turbo_bf16.safetensors",
            "image/qwen_3_4b.safetensors",
            "image/ae.safetensors",
        ],
    },
    "comfy-chroma-hd": {
        "builder": chroma_hd_t2i_graph,
        "label": "Chroma1-HD 8.9B — uncensored FLUX-lineage (ComfyUI)",
        "required_files": [
            "image/Chroma1-HD.safetensors",
            "image/t5xxl_fp8_e4m3fn_scaled.safetensors",
            "image/ae.safetensors",
        ],
    },
    "comfy-noobai-xl": {
        "builder": noobai_vpred_t2i_graph,
        "label": "NoobAI-XL V-Pred — anime/illustration (ComfyUI)",
        "required_files": ["image/NoobAI-XL-Vpred-v1.0.safetensors"],
    },
    "comfy-krea2-turbo": {
        "builder": krea2_turbo_t2i_graph,
        "label": "Krea 2 Turbo — 2K aesthetic (ComfyUI)",
        "required_files": [
            "image/krea2_turbo_mxfp8.safetensors",
            "image/qwen3vl_4b_fp8_scaled.safetensors",
            "qwen-image/qwen_image_vae.safetensors",
        ],
    },
    "comfy-ideogram4": {
        "builder": ideogram4_t2i_graph,
        "label": "Ideogram 4.0 — typography & design (ComfyUI, non-commercial)",
        "required_files": [
            "image/ideogram4_fp8_scaled.safetensors",
            "image/ideogram4_unconditional_fp8_scaled.safetensors",
            "image/qwen3vl_8b_fp8_scaled.safetensors",
            "flux2-vae.safetensors",
        ],
    },
}


def _available(catalog: Dict[str, Dict[str, Any]]) -> List[str]:
    root = _models_dir()
    return [
        mid for mid, spec in catalog.items()
        if all(os.path.exists(os.path.join(root, f)) for f in spec["required_files"])
    ]


def available_video_models() -> List[str]:
    """Comfy video model ids whose required files all exist on disk."""
    return _available(COMFY_VIDEO_MODELS)


def available_image_models() -> List[str]:
    """Comfy image model ids whose required files all exist on disk."""
    return _available(COMFY_IMAGE_MODELS)


def build_video_graph(model_id: str, **params) -> Optional[Dict[str, Any]]:
    spec = COMFY_VIDEO_MODELS.get(model_id)
    if not spec:
        return None
    return spec["builder"](**params)


def build_image_graph(model_id: str, **params) -> Optional[Dict[str, Any]]:
    spec = COMFY_IMAGE_MODELS.get(model_id)
    if not spec:
        return None
    return spec["builder"](**params)
