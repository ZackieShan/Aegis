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
    _lightning_high = "wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors"
    _lightning_low = "wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors"
    loras_dir = os.path.join(_models_dir(), "loras")
    lightning = (os.path.exists(os.path.join(loras_dir, _lightning_high))
                 and os.path.exists(os.path.join(loras_dir, _lightning_low)))
    if lightning:
        steps, cfg = 4, 1.0
    half = max(1, steps // 2)
    negative = negative_prompt or (
        "色调艳丽，过曝，静态，细节模糊不清，最差质量，低质量，多余的手指，画得不好的手部"
    )
    graph = {
        "1": {"class_type": "CLIPLoaderGGUF",
              "inputs": {"clip_name": "video\\umt5-xxl-encoder-Q8_0.gguf", "type": "wan"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": prompt}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 0], "text": negative}},
        "4": {"class_type": "UnetLoaderGGUF",
              "inputs": {"unet_name": "Wan2.2-T2V-A14B-HighNoise-Q3_K_M.gguf"}},
        "5": {"class_type": "UnetLoaderGGUF",
              "inputs": {"unet_name": "Wan2.2-T2V-A14B-LowNoise-Q3_K_M.gguf"}},
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
                       "inputs": {"model": ["4", 0], "lora_name": _lightning_high, "strength_model": 1.0}}
        graph["22"] = {"class_type": "LoraLoaderModelOnly",
                       "inputs": {"model": ["5", 0], "lora_name": _lightning_low, "strength_model": 1.0}}
        graph["6"]["inputs"]["model"] = ["21", 0]
        graph["7"]["inputs"]["model"] = ["22", 0]
    return graph


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
