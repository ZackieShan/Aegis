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


# ── split GGUFs (…-00001-of-0000N.gguf) ──────────────────────────────────────
def test_gguf_total_bytes_sums_every_shard(tmp_path):
    for i, size in ((1, 400), (2, 250), (3, 50)):
        (tmp_path / f"big-Q4_K_M-{i:05d}-of-00003.gguf").write_bytes(b"\0" * size)
    (tmp_path / "big-Q4_K_M-00001-of-00002.gguf").write_bytes(b"\0" * 9999)   # a different split set
    first = str(tmp_path / "big-Q4_K_M-00001-of-00003.gguf")
    assert t.gguf_total_bytes(first) == 700
    # any shard resolves to the same whole-model size
    assert t.gguf_total_bytes(str(tmp_path / "big-Q4_K_M-00003-of-00003.gguf")) == 700
    # an unsplit file is just its own size
    single = tmp_path / "single.gguf"
    single.write_bytes(b"\0" * 123)
    assert t.gguf_total_bytes(str(single)) == 123


def test_gguf_total_bytes_missing_shard_raises(tmp_path):
    (tmp_path / "m-00001-of-00002.gguf").write_bytes(b"\0" * 10)
    with pytest.raises(OSError):
        t.gguf_total_bytes(str(tmp_path / "m-00001-of-00002.gguf"))


def test_recommend_context_counts_all_shards_of_a_split_model(monkeypatch, tmp_path):
    """llama-swap's -m names only shard 1. Sizing weights from it alone made a
    60GB model look like 40GB and oversized the context by the difference."""
    mib = 1024 * 1024
    sizes = {"m-00001-of-00002.gguf": 40000 * mib, "m-00002-of-00002.gguf": 20000 * mib}
    for name in sizes:
        (tmp_path / name).write_bytes(b"\0")
    monkeypatch.setattr(os.path, "getsize", lambda p: sizes[os.path.basename(p)])
    monkeypatch.setattr(t, "gguf_meta", lambda p: {
        "arch": "x", "n_layers": 36, "n_kv_heads": 8,
        "key_length": 64, "value_length": 64, "n_ctx_train": 1_000_000})
    rec = t.recommend_context(str(tmp_path / "m-00001-of-00002.gguf"), vram_mb=70000)
    assert rec["weights_mb"] == 60000
    kv_mb_per_tok = 36 * 8 * 128 * 2 / mib
    budget = 70000 - 60000 - t._RESERVE_MB - t._SAFETY_MB
    assert rec["recommended"] == int(budget / kv_mb_per_tok) // 4096 * 4096
    # the old first-shard-only figure would have been far larger
    assert rec["recommended"] < int((budget + 20000) / kv_mb_per_tok) // 4096 * 4096

    # a split model with a shard missing is "insufficient info", not a guess
    (tmp_path / "m-00002-of-00002.gguf").unlink()
    def _getsize(p):
        if os.path.basename(p) == "m-00002-of-00002.gguf":
            raise FileNotFoundError(p)
        return sizes[os.path.basename(p)]
    monkeypatch.setattr(os.path, "getsize", _getsize)
    assert t.recommend_context(str(tmp_path / "m-00001-of-00002.gguf"), vram_mb=70000)["recommended"] is None


# ── built-in GGUF header reader (no `gguf` package, no tensor index) ─────────
import struct as _struct

_T = {"u8": 0, "i8": 1, "u16": 2, "i16": 3, "u32": 4, "i32": 5, "f32": 6,
      "bool": 7, "str": 8, "arr": 9, "u64": 10, "i64": 11, "f64": 12}
_CODE = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}


def _gs(text):
    raw = text.encode("utf-8")
    return _struct.pack("<Q", len(raw)) + raw


def _gv(kind, value):
    tid = _T[kind]
    if kind == "str":
        return _struct.pack("<I", tid) + _gs(value)
    if kind == "arr":
        ekind, items = value
        etid = _T[ekind]
        body = b"".join(_gs(x) for x in items) if ekind == "str" else \
            _struct.pack(f"<{len(items)}{_CODE[etid]}", *items)
        return _struct.pack("<IIQ", tid, etid, len(items)) + body
    return _struct.pack("<I", tid) + _struct.pack("<" + _CODE[tid], value)


def _gguf_file(path, kvs, version=3, n_tensors=7, tail=b"\xff" * 64):
    """A GGUF header with the given [(key, kind, value)] followed by `tail`
    standing in for the tensor index + data the reader must never touch."""
    blob = b"GGUF" + _struct.pack("<IQQ", version, n_tensors, len(kvs))
    blob += b"".join(_gs(k) + _gv(kind, v) for k, kind, v in kvs)
    path.write_bytes(blob + tail)
    return str(path)


_ARCH_KVS = [
    ("general.architecture", "str", "qwen4exp"),
    ("general.name", "str", "Synthetic"),
    ("tokenizer.chat_template", "str", "x" * 9000),                 # long string: skipped
    ("tokenizer.ggml.tokens", "arr", ("str", [f"tok{i}" for i in range(6000)])),   # vocab: skipped
    ("tokenizer.ggml.scores", "arr", ("f32", [0.0] * 6000)),        # big numeric array: skipped
    ("qwen4exp.block_count", "u32", 48),
    ("qwen4exp.context_length", "u64", 262144),
    ("qwen4exp.embedding_length", "u32", 4096),
    ("qwen4exp.attention.head_count", "u32", 32),
    ("qwen4exp.attention.head_count_kv", "arr", ("i32", [2, 2, 4, 2])),   # per-layer → max
    ("qwen4exp.attention.key_length", "u32", 256),
    ("qwen4exp.attention.value_length", "u32", 256),
    ("general.quantization_version", "u32", 2),
]


def test_header_reader_returns_small_values_and_steps_over_big_ones(tmp_path):
    kv = t.gguf_header_kv(_gguf_file(tmp_path / "m.gguf", _ARCH_KVS))
    assert kv["general.architecture"] == "qwen4exp"
    assert kv["qwen4exp.block_count"] == 48 and kv["qwen4exp.context_length"] == 262144
    assert kv["qwen4exp.attention.head_count_kv"] == [2, 2, 4, 2]
    assert kv["general.quantization_version"] == 2          # keys AFTER the skipped blobs still parse
    for skipped in ("tokenizer.chat_template", "tokenizer.ggml.tokens", "tokenizer.ggml.scores"):
        assert skipped not in kv


def test_header_reader_all_scalar_types_round_trip(tmp_path):
    kvs = [("a.u8", "u8", 200), ("a.i8", "i8", -5), ("a.u16", "u16", 60000), ("a.i16", "i16", -300),
           ("a.u32", "u32", 4_000_000_000), ("a.i32", "i32", -7), ("a.f32", "f32", 0.5),
           ("a.bool", "bool", True), ("a.u64", "u64", 2 ** 40), ("a.i64", "i64", -(2 ** 40)),
           ("a.f64", "f64", 0.25), ("a.s", "str", "héllo")]
    kv = t.gguf_header_kv(_gguf_file(tmp_path / "s.gguf", kvs, version=2))
    assert kv == {k: v for k, _, v in kvs}


def test_meta_via_header_matches_tuner_shape(tmp_path):
    meta = t._gguf_meta_via_header(_gguf_file(tmp_path / "m.gguf", _ARCH_KVS))
    assert meta == {"arch": "qwen4exp", "n_layers": 48, "n_kv_heads": 4,
                    "key_length": 256, "value_length": 256, "n_ctx_train": 262144}
    # head dim falls back to embedding_length / head_count when key_length is absent
    kvs = [kv for kv in _ARCH_KVS if "key_length" not in kv[0] and "value_length" not in kv[0]]
    meta = t._gguf_meta_via_header(_gguf_file(tmp_path / "n.gguf", kvs))
    assert meta["key_length"] == meta["value_length"] == 4096 // 32


@pytest.mark.parametrize("blob", [
    b"",                                                        # empty file
    b"GGML" + b"\0" * 40,                                       # wrong magic
    b"GGUF" + _struct.pack("<IQQ", 9, 0, 0),                    # unknown version
    b"GGUF" + _struct.pack("<IQQ", 3, 0, 2) + _gs("general.architecture"),   # truncated mid-KV
    b"GGUF" + _struct.pack("<IQQ", 3, 0, 1) + _gs("k") + _struct.pack("<I", 77),   # unknown value type
    b"GGUF" + _struct.pack("<IQQ", 3, 0, 10 ** 9),              # absurd KV count
    b"GGUF" + _struct.pack("<IQQ", 3, 0, 1) + _struct.pack("<Q", 2 ** 60),   # absurd key length
])
def test_header_reader_rejects_malformed_files(tmp_path, blob):
    f = tmp_path / "bad.gguf"
    f.write_bytes(blob)
    with pytest.raises(ValueError):
        t.gguf_header_kv(str(f))
    assert t._gguf_meta_via_header(str(f)) == {}               # the tuner sees "no metadata", not a crash


def test_meta_prefers_header_reader_and_survives_unknown_tensor_types(monkeypatch, tmp_path):
    """The reason this reader exists: GGUFReader raises on a tensor type newer
    than the installed gguf package (Qwen3.8 'Swift' quants: type 42), which
    left that model with no context recommendation at all."""
    path = _gguf_file(tmp_path / "new-quant.gguf", _ARCH_KVS)
    monkeypatch.setattr(t, "_gguf_meta_via_reader",
                        lambda p: pytest.fail("GGUFReader must not be needed when the header parses"))
    assert t._gguf_meta_uncached(path)["arch"] == "qwen4exp"


def test_meta_falls_back_to_gguf_package_when_header_unreadable(monkeypatch, tmp_path):
    f = tmp_path / "odd.gguf"
    f.write_bytes(b"GGUF" + _struct.pack("<IQQ", 9, 0, 0))
    monkeypatch.setattr(t, "_gguf_meta_via_reader", lambda p: {"arch": "from-reader"})
    assert t._gguf_meta_uncached(str(f)) == {"arch": "from-reader"}


# ── aliases and setParamsByID variants share their model's served context ────
_CONFIG_VARIANTS = '''models:

  # the big one, with a low-reasoning variant served by the SAME process
  "gpt-oss-120b":
    cmd: |
      "engine/llama-server"
      -m "models/gpt-oss-00001-of-00002.gguf"
      -ngl 99 -c 65536 -fa on --jinja
    aliases:
      - "oss"
      - big-oss
    filters:
      setParamsByID:
        "${MODEL_ID}":
          reasoning_effort: medium
        "gpt-oss-120b-fast":
          reasoning_effort: low
        "${MODEL_ID}:high":
          reasoning_effort: high
    ttl: 600

  "small":
    cmd: |
      "engine/llama-server"
      -m "models/small.gguf"
      -ngl 99 -c 8192
    aliases: ["tiny", "wee"]
    ttl: 600

groups:
  gpu:
    members:
      - gpt-oss-120b
      - small
'''


def _use_variants(monkeypatch, tmp_path):
    cfg = tmp_path / "llama-swap.yaml"
    cfg.write_text(_CONFIG_VARIANTS, encoding="utf-8")
    monkeypatch.setattr(t, "_config_path", lambda: str(cfg))
    return cfg


def test_nested_quoted_keys_do_not_split_a_model_block(monkeypatch, tmp_path):
    """setParamsByID keys look just like model keys ("id":) — treating them as
    models cut the real block in two and hid its -c from every variant."""
    _use_variants(monkeypatch, tmp_path)
    blocks = t._model_blocks(t._read_config())
    assert [b["name"] for b in blocks] == ["gpt-oss-120b", "small"]
    assert "setParamsByID" in blocks[0]["text"] and "-c 65536" in blocks[0]["text"]


def test_block_alias_ids_cover_aliases_and_param_variants(monkeypatch, tmp_path):
    _use_variants(monkeypatch, tmp_path)
    big, small = t._model_blocks(t._read_config())
    assert t._block_alias_ids(big) == ["oss", "big-oss", "gpt-oss-120b-fast", "gpt-oss-120b:high"]
    assert t._block_alias_ids(small) == ["tiny", "wee"]          # flow-style list


@pytest.mark.parametrize("model,ctx", [
    ("gpt-oss-120b", 65536),
    ("gpt-oss-120b-fast", 65536),      # setParamsByID variant
    ("gpt-oss-120b:high", 65536),      # ${MODEL_ID} macro expanded
    ("oss", 65536), ("big-oss", 65536),
    ("tiny", 8192), ("small", 8192),
    ("nope", None),
])
def test_configured_context_resolves_variants(monkeypatch, tmp_path, model, ctx):
    """Aegis trims chat history to the served window; a variant id that
    resolved to "unknown" fell back to a default and could overflow -c."""
    _use_variants(monkeypatch, tmp_path)
    assert t.configured_context(model) == ctx


def test_set_context_still_surgical_with_variants(monkeypatch, tmp_path):
    cfg = _use_variants(monkeypatch, tmp_path)
    res = t.set_context("gpt-oss-120b", 32768)
    assert res["ok"] and res["old_ctx"] == 65536
    text = cfg.read_text(encoding="utf-8")
    assert "-c 32768" in text and "-c 8192" in text and 'reasoning_effort: low' in text
    assert t.configured_context("gpt-oss-120b-fast") == 32768
    assert [m["model"] for m in t.list_models(recommend=False)] == ["gpt-oss-120b", "small"]


def test_key_indent_ignores_blank_lines_above_a_key():
    """`\\s*` in the key pattern swallows preceding blank lines; counting those
    as indentation let a top-level quoted key pass for a model id."""
    text = 'models:\n\n\n"top-level-quoted":\n  x: 1\n\n  "real-model":\n    cmd: |\n      -m "a.gguf" -c 4096\n'
    assert [b["name"] for b in t._model_blocks(text)] == ["real-model"]
