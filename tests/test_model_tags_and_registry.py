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
