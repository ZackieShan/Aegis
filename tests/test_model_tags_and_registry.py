"""Model capability tagging + registry helpers (Media Studio panel, 2026-07-14)."""
import types

import pytest

from src.model_tags import classify, is_companion


# ── capability detection over the real install's names ──────────────────────
@pytest.mark.parametrize("name,expected_cap", [
    ("qwen-image-2512-Q8_0.gguf", "text-to-image"),
    ("qwen-image", "text-to-image"),
    ("flux-2-klein-9b-BF16.gguf", "text-to-image"),
    ("Qwen-Rapid-NSFW-v18.1_Q8_0.gguf", "text-to-image"),
    ("qwen-image-edit-2511-Q8_0.gguf", "image-to-image"),
    ("Wan2.2-T2V-A14B-LowNoise-Q3_K_M.gguf", "text-to-video"),
    ("wan2.2-t2v", "text-to-video"),
    ("Wan2.2-Animate-14B-Q4_0.gguf", "video-to-video"),
    ("ltx2.3-video", "text-to-video"),
    ("ltx2-phr00tmerge-sfw-v5-Q5_0.gguf", "text-to-video"),
    ("Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf", "vision"),
    ("qwen-vl", "vision"),
    ("Qwen3-Coder-30B-A3B-Instruct-Q4_0.gguf", "coding"),
    ("supergemma4-26b-uncensored-fast-v2-Q4_K_M.gguf", "chat"),
    ("gpt-oss-120b-Distill-Phi-4-14B.Q8_0.gguf", "chat"),
])
def test_capability_tags(name, expected_cap):
    assert expected_cap in classify(name)["capabilities"], name


def test_ltx_is_also_image_to_video():
    caps = classify("ltx2.3-video")["capabilities"]
    assert "image-to-video" in caps


@pytest.mark.parametrize("name", [
    "mmproj-Qwen2.5-VL-3B-Instruct-f16.gguf",
    "qwen_image_vae.safetensors",
    "umt5-xxl-encoder-Q8_0.gguf",
    "ltx-2.3-22b-dev_embeddings_connectors.safetensors",
    "wan_2.1_vae.safetensors",
])
def test_companions_detected(name):
    tags = classify(name)
    assert is_companion(tags), name


# ── 2026-11 model refresh: the new big chat models and their companions ──────
@pytest.mark.parametrize("name", [
    "mistral_3_small_flux2_bf16.safetensors",     # FLUX.2-dev text encoder — NOT an image model
    "mistral_3_small_flux2_fp8.safetensors",
    "qwen_2.5_vl_7b.safetensors",                 # HunyuanVideo 1.5 text encoder, full precision
    "qwen_2.5_vl_7b_fp8_scaled.safetensors",
    "qwen_3_4b.safetensors",                      # Z-Image text encoder
    "qwen_0.6b_ace15.safetensors",                # ACE-Step text encoders
    "qwen3vl_4b_fp8_scaled.safetensors",          # Krea 2 text encoder
])
def test_text_encoder_repacks_are_companions(name):
    tags = classify(name)
    assert is_companion(tags), name
    assert tags["capabilities"] == ["companion"]  # never offered as a chat/image/vision model


@pytest.mark.parametrize("name", [
    "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf",         # a real vision LLM keeps its tags
    "Qwen3.6-35B-A3B-Q8_0.gguf",
    "gemma-3-12b-it-UD-Q4_K_XL.gguf",
    "mistralai_Mistral-Small-4-119B-2603-Q4_K_M-00001-of-00002.gguf",
    "mistral-small-4",
    "qwen-vl",
])
def test_real_llms_are_not_mistaken_for_text_encoders(name):
    assert not is_companion(classify(name)), name


def test_new_chat_models_get_useful_tags():
    oss = classify("kldzj_gpt-oss-120b-heretic-MXFP4_MOE-00001-of-00002.gguf")
    assert {"chat", "uncensored", "moe"} <= set(oss["capabilities"])       # "heretic" = decensored
    assert "step-by-step reasoning" in oss["best_for"]
    assert "uncensored" not in classify("gpt-oss-120b")["capabilities"]     # the bare alias claims nothing

    mm = classify("MiniMax-M2.1-REAP-139B-A10B.Q4_K_M.gguf")
    assert {"coding", "chat", "moe"} <= set(mm["capabilities"])              # A10B = MoE
    assert "coding & agent tool use" in classify("minimax-m2.1-reap")["best_for"]

    steam = classify("GLM-Steam-106B-A12B-v1b-Q4_K_M-00001-of-00002.gguf")
    assert steam["best_for"][0] == "creative writing & roleplay" and "moe" in steam["capabilities"]
    assert "creative writing & roleplay" not in classify("glm-4.7-flash-q4")["best_for"]

    assert "translation & multilingual chat" in classify("mistral-small-4")["best_for"]
    assert "uncensored" in classify("Huihui-Qwen3.8-Flash-Next-abliterated")["capabilities"]


@pytest.mark.parametrize("name,is_moe", [
    ("Qwen3.6-35B-A3B-Q8_0.gguf", True),
    ("Qwopus3.5-122B-A10B-Kimi-K2.6-destill-healed-abliterated-Q4_K_M.gguf", True),
    ("Qwen3.6-27B-Fable-Fus-711-UnHeretic-NM-DAU-NEO-MAX-NEO-Q5_K_M.gguf", False),   # dense
    ("supergemma4-26b-uncensored-fast-v2-Q4_K_M.gguf", False),
    ("llama-3.1-8b-instruct", False),             # "a" + digits elsewhere must not trip it
])
def test_moe_detected_from_active_param_token(name, is_moe):
    assert ("moe" in classify(name)["capabilities"]) is is_moe, name


def test_wanda_chat_never_matches_wan():
    caps = classify("wanda-chat")["capabilities"]
    assert caps == ["chat"]


def test_uncensored_and_moe_qualifiers():
    caps = classify("Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf")["capabilities"]
    assert "uncensored" in caps and "moe" in caps and "chat" in caps


def test_lora_hint_overrides():
    tags = classify("whatever.safetensors", is_lora=True)
    assert tags["capabilities"] == ["lora"]


# ── best-for tags cover the user's stated use cases ──────────────────────────
def test_best_for_specialists():
    assert "tables & data extraction" in classify("TableLLM-13b.Q4_K_M.gguf")["best_for"]
    assert "text classification" in classify("Qwen2.5-14B-CIC-ACLARC-Q8_0.gguf")["best_for"]
    vl = classify("Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf")["best_for"]
    assert "image & video classification" in vl
    assert "OCR & data extraction from documents" in vl
    assert "translation & multilingual chat" in classify("Qwen3.6-27B-Q6_K.gguf")["best_for"]
    coder = classify("qwen3-coder-30b")["best_for"]
    assert "charts & data analysis (via code)" in coder


def test_diffusion_models_are_not_chat():
    for name in ("qwen-image", "wan2.2-t2v", "ltx2.3-video", "qwen-image-edit"):
        assert "chat" not in classify(name)["capabilities"], name


# ── registry helpers ─────────────────────────────────────────────────────────
def test_engine_alias_files_parses_blocks(monkeypatch, tmp_path):
    import routes.model_registry_routes as mr
    yaml_text = '''models:
  "qwen-image":
    cmd: |
      "C:/x/sd-server.exe"
      --diffusion-model "C:/models/qwen-image-2512-Q8_0.gguf"
      --vae "C:/models/qwen-image/qwen_image_vae.safetensors"
      --listen-port ${PORT}
  "chatty":
    cmd: |
      "C:/x/llama-server.exe"
      -m "C:/models/Chatty-7B-Q4.gguf"
'''
    monkeypatch.setattr("src.engine_tuner._read_config", lambda: yaml_text)
    m = mr._engine_alias_files()
    assert m["qwen-image"] == ["qwen-image-2512-q8_0.gguf", "qwen_image_vae.safetensors"]
    assert m["chatty"] == ["chatty-7b-q4.gguf"]


def test_walk_model_files_flags_loras(monkeypatch, tmp_path):
    import routes.model_registry_routes as mr
    (tmp_path / "loras").mkdir()
    (tmp_path / "sub").mkdir()
    (tmp_path / "big-model-Q4.gguf").write_bytes(b"x" * 10)
    (tmp_path / "loras" / "style-lora.safetensors").write_bytes(b"x")
    (tmp_path / "sub" / "notes.txt").write_bytes(b"x")  # ignored
    monkeypatch.setattr(mr, "_models_dir", lambda: str(tmp_path))
    files = mr._walk_model_files()
    names = {f["file"]: f for f in files}
    assert "big-model-Q4.gguf" in names and not names["big-model-Q4.gguf"]["is_lora"]
    assert names["loras/style-lora.safetensors"]["is_lora"]
    assert len(files) == 2
