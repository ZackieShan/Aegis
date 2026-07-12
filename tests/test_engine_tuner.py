"""Unit tests for the llama.cpp context auto-tuner — src/engine_tuner.py.

Covers the VRAM/KV math and the surgical YAML edit (which must change only the
target model's -c and preserve everything else, including comments).
"""
import os

import pytest

from src import engine_tuner as t


_CONFIG = '''# llama-swap config
healthCheckTimeout: 300
logLevel: info

models:
  # a coder model
  "qwen3-coder-30b":
    cmd: |
      "engine/llama-server.exe"
      -m "models/coder.gguf"
      --host 127.0.0.1 --port ${PORT}
      -ngl 99 -c 8192 -fa on --jinja --no-webui
    ttl: 600

  "big-27b":
    cmd: |
      "engine/llama-server.exe"
      -m "models/big.gguf"
      --host 127.0.0.1 --port ${PORT}
      -ngl 52 -c 8192 --jinja --no-webui
    ttl: 600

  # image pipeline — not a llama-server, must be skipped
  "qwen-image":
    cmd: |
      "engine/sd-server.exe"
      --diffusion-model "models/img.gguf"
      --listen-port ${PORT}
    ttl: 300
'''


# ── KV / recommendation math ──────────────────────────────────────────────────
def test_kv_bytes_per_token():
    meta = {"n_layers": 48, "n_kv_heads": 4, "key_length": 128, "value_length": 128}
    # 48 * 4 * 256 * 2 (fp16) = 98304 bytes/token
    assert t.kv_bytes_per_token(meta, 16) == 98304
    # q8 halves it
    assert t.kv_bytes_per_token(meta, 8) == 49152


def test_kv_bytes_per_token_incomplete():
    assert t.kv_bytes_per_token({"n_layers": 48}, 16) is None


def test_recommend_context_fits(monkeypatch, tmp_path):
    f = tmp_path / "coder.gguf"
    f.write_bytes(b"\0" * 1024)  # size mocked below
    monkeypatch.setattr(t, "gguf_meta", lambda p: {
        "arch": "qwen3moe", "n_layers": 48, "n_kv_heads": 4,
        "key_length": 128, "value_length": 128, "n_ctx_train": 262144})
    monkeypatch.setattr(os.path, "getsize", lambda p: 16575 * 1024 * 1024)  # 16.5 GB
    rec = t.recommend_context(str(f), vram_mb=24564)
    # 4096-aligned, above the floor, below the trained ceiling
    assert rec["recommended"] % 4096 == 0
    assert rec["recommended"] >= t._FLOOR
    assert rec["recommended"] <= 262144
    # ~45K for these exact numbers (matches the real 30B on a 24GB card)
    assert 40960 <= rec["recommended"] <= 49152


def test_recommend_context_capped_at_trained(monkeypatch, tmp_path):
    f = tmp_path / "tiny.gguf"
    f.write_bytes(b"\0")
    monkeypatch.setattr(t, "gguf_meta", lambda p: {
        "arch": "x", "n_layers": 4, "n_kv_heads": 1,
        "key_length": 64, "value_length": 64, "n_ctx_train": 32768})
    monkeypatch.setattr(os.path, "getsize", lambda p: 500 * 1024 * 1024)
    rec = t.recommend_context(str(f), vram_mb=24564)
    assert rec["recommended"] <= 32768  # never exceeds what the model was trained for


def test_recommend_context_weights_fill_vram(monkeypatch, tmp_path):
    f = tmp_path / "huge.gguf"
    f.write_bytes(b"\0")
    monkeypatch.setattr(t, "gguf_meta", lambda p: {
        "arch": "x", "n_layers": 80, "n_kv_heads": 8,
        "key_length": 128, "value_length": 128, "n_ctx_train": 8192})
    monkeypatch.setattr(os.path, "getsize", lambda p: 24000 * 1024 * 1024)  # ~all VRAM
    rec = t.recommend_context(str(f), vram_mb=24564)
    assert rec["recommended"] is None
    assert "VRAM" in rec["reason"]


# ── config parse + surgical edit ─────────────────────────────────────────────
def _use_config(monkeypatch, tmp_path):
    cfg = tmp_path / "llama-swap.yaml"
    cfg.write_text(_CONFIG, encoding="utf-8")
    monkeypatch.setattr(t, "_config_path", lambda: str(cfg))
    return cfg


def test_model_blocks_finds_all(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    names = [b["name"] for b in t._model_blocks(t._read_config())]
    assert names == ["qwen3-coder-30b", "big-27b", "qwen-image"]


def test_list_models_filters_and_classifies(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(t, "gpu_vram_mb", lambda: 24564)
    monkeypatch.setattr(t, "recommend_context", lambda *a, **k: {"recommended": 40960})
    models = {m["model"]: m for m in t.list_models()}
    assert "qwen-image" not in models          # sd-server skipped
    assert models["qwen3-coder-30b"]["full_offload"] is True
    assert models["big-27b"]["full_offload"] is False   # -ngl 52
    assert models["big-27b"]["recommended"] is None      # partial offload → manual


def test_set_context_surgical(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    res = t.set_context("qwen3-coder-30b", 45056)
    assert res["ok"] and res["old_ctx"] == 8192 and res["new_ctx"] == 45056
    text = cfg.read_text(encoding="utf-8")
    assert "-c 45056 -fa on" in text          # only the coder changed
    assert text.count("-c 8192") == 1          # big-27b's -c untouched
    assert "# a coder model" in text           # comments preserved
    assert "sd-server.exe" in text             # image block intact


def test_set_context_unknown_model(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    res = t.set_context("does-not-exist", 16384)
    assert not res["ok"] and "not found" in res["error"]


def test_set_context_rejects_out_of_range(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    assert not t.set_context("qwen3-coder-30b", 100)["ok"]


def test_configured_context(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    assert t.configured_context("qwen3-coder-30b") == 8192   # from the fixture
    assert t.configured_context("big-27b") == 8192
    assert t.configured_context("qwen-image") is None         # no -c
    assert t.configured_context("nope") is None


def test_autotune_applies_and_skips(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(t, "gpu_vram_mb", lambda: 24564)
    monkeypatch.setattr(t, "recommend_context", lambda *a, **k: {"recommended": 40960})
    res = t.autotune()
    by = {a["model"]: a for a in res["applied"]}
    assert by["qwen3-coder-30b"]["new_ctx"] == 40960   # applied
    assert "skipped" in by["big-27b"]                   # partial offload skipped


# ── GGUF metadata cache (the "/engine fails to fetch" fix) ───────────────────
def test_gguf_meta_cached_by_size_and_mtime(monkeypatch, tmp_path):
    f = tmp_path / "m.gguf"
    f.write_bytes(b"\0" * 64)
    monkeypatch.setattr(t, "_meta_cache_path", lambda: str(tmp_path / "cache.json"))
    monkeypatch.setattr(t, "_META_CACHE", {}, raising=False)
    monkeypatch.setattr(t, "_META_CACHE_LOADED", True, raising=False)
    calls = {"n": 0}

    def _fake_parse(path):
        calls["n"] += 1
        return {"arch": "x", "n_layers": 1, "n_kv_heads": 1,
                "key_length": 8, "value_length": 8, "n_ctx_train": 4096}
    monkeypatch.setattr(t, "_gguf_meta_uncached", _fake_parse)

    m1 = t.gguf_meta(str(f))
    m2 = t.gguf_meta(str(f))
    assert m1 == m2 and m1["arch"] == "x"
    assert calls["n"] == 1, "second read must come from the cache"

    # Changing the file invalidates the cached entry.
    f.write_bytes(b"\0" * 128)
    t.gguf_meta(str(f))
    assert calls["n"] == 2


def test_gguf_meta_empty_parse_not_cached(monkeypatch, tmp_path):
    f = tmp_path / "m.gguf"
    f.write_bytes(b"\0" * 64)
    monkeypatch.setattr(t, "_meta_cache_path", lambda: str(tmp_path / "cache.json"))
    monkeypatch.setattr(t, "_META_CACHE", {}, raising=False)
    monkeypatch.setattr(t, "_META_CACHE_LOADED", True, raising=False)
    calls = {"n": 0}

    def _fake_parse(path):
        calls["n"] += 1
        return {}  # e.g. `gguf` package missing
    monkeypatch.setattr(t, "_gguf_meta_uncached", _fake_parse)

    assert t.gguf_meta(str(f)) == {}
    assert t.gguf_meta(str(f)) == {}
    assert calls["n"] == 2, "empty parses must be retried, not remembered"


def test_gguf_meta_cache_persists(monkeypatch, tmp_path):
    f = tmp_path / "m.gguf"
    f.write_bytes(b"\0" * 64)
    cache_file = tmp_path / "cache.json"
    monkeypatch.setattr(t, "_meta_cache_path", lambda: str(cache_file))
    monkeypatch.setattr(t, "_META_CACHE", {}, raising=False)
    monkeypatch.setattr(t, "_META_CACHE_LOADED", True, raising=False)
    monkeypatch.setattr(t, "_gguf_meta_uncached", lambda p: {"arch": "x", "n_layers": 2})
    t.gguf_meta(str(f))
    assert cache_file.exists()

    # Fresh process: empty in-memory cache, loads from disk, no re-parse.
    monkeypatch.setattr(t, "_META_CACHE", {}, raising=False)
    monkeypatch.setattr(t, "_META_CACHE_LOADED", False, raising=False)
    monkeypatch.setattr(t, "_gguf_meta_uncached",
                        lambda p: pytest.fail("should have been served from the persisted cache"))
    assert t.gguf_meta(str(f))["arch"] == "x"
