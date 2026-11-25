"""MoE-aware context tuner (2026-07-18).

A `--cpu-moe` model keeps its expert weights in system RAM, so the full GGUF
size is NOT its VRAM footprint. recommend_context (which subtracts the whole
file from VRAM) wrongly concludes "weights fill VRAM" and refuses to size the
context — which is why a 35B-A3B was stuck at 16K with 21GB of VRAM free.
list_models must flag these as MoE (cpu_moe=True) with an honest reason and the
trained ceiling, so the UI directs the user to set the context manually.
"""
from src import engine_tuner as t


_CFG = '''models:
  "moe-35b":
    cmd: |
      "llama-server.exe"
      -m "models/moe-35b-Q8.gguf"
      -ngl 99 --cpu-moe -c 16384 -fa on
    ttl: 600

  "dense-14b":
    cmd: |
      "llama-server.exe"
      -m "models/dense-14b-Q8.gguf"
      -ngl 99 -c 16384 -fa on
    ttl: 600
'''


def test_cpu_moe_flagged_and_not_rejected(monkeypatch, tmp_path):
    cfg = tmp_path / "llama-swap.yaml"
    cfg.write_text(_CFG, encoding="utf-8")
    monkeypatch.setattr(t, "_config_path", lambda: str(cfg))
    monkeypatch.setattr(t, "gpu_vram_mb", lambda: 24564)
    monkeypatch.setattr(t, "gguf_meta", lambda p: {
        "arch": "qwen35moe", "n_layers": 40, "n_kv_heads": 2,
        "key_length": 256, "value_length": 256, "n_ctx_train": 262144})
    # The MoE GGUF is huge (would "fill VRAM" on the dense path); the dense
    # model is small enough to fully offload and get a real recommendation.
    monkeypatch.setattr("os.path.getsize",
                        lambda p: (36900 if "moe" in p else 14400) * 1024 * 1024)

    models = {m["model"]: m for m in t.list_models()}

    moe = models["moe-35b"]
    assert moe["cpu_moe"] is True
    assert moe["recommended"] is None            # not auto-sized
    assert "RAM" in moe["reason"]                # honest reason, not "weights fill VRAM"
    assert "fill VRAM" not in moe["reason"]
    assert moe["n_ctx_train"] == 262144          # trained ceiling surfaced for the UI

    dense = models["dense-14b"]
    assert dense["cpu_moe"] is False
    # The dense 14.4GB-in-VRAM path still runs recommend_context (fits, sizes it).
    assert dense["recommended"] is not None


# The Linux/Strix Halo config documents every former --cpu-moe entry with a
# "# --cpu-moe removed" comment. A model block's text runs up to the NEXT
# model's key, so it contains the next model's leading comments.
_CFG_COMMENTED = '''models:
  # --cpu-moe removed: unified memory, fully GPU-resident on Strix Halo
  "first-moe":
    cmd: |
      "llama-server"
      -m "models/first-moe-Q4.gguf"
      -ngl 99 -c 32768 -fa on
    ttl: 600

  # The big one. Do not expect GLM speeds.
  # --cpu-moe removed: unified memory, fully GPU-resident on Strix Halo
  "second-moe":
    cmd: |
      "llama-server"
      -m "models/second-moe-Q4.gguf"
      -ngl 99 -c 32768 -fa on
    ttl: 600

  "really-cpu-moe":
    cmd: |
      "llama-server"
      -m "models/third-moe-Q4.gguf"
      -ngl 99 --n-cpu-moe 12 -c 32768 -fa on
    ttl: 600
'''


def test_comment_mentioning_cpu_moe_is_not_the_flag(monkeypatch, tmp_path):
    """Every model followed by a '# --cpu-moe removed' comment used to be
    reported as CPU-offloaded ("set it manually") and skipped by autotune —
    on the Linux config that was most of the catalog."""
    cfg = tmp_path / "llama-swap.yaml"
    cfg.write_text(_CFG_COMMENTED, encoding="utf-8")
    monkeypatch.setattr(t, "_config_path", lambda: str(cfg))
    monkeypatch.setattr(t, "gpu_budget_mb", lambda: 110000)
    monkeypatch.setattr(t, "gguf_meta", lambda p: {
        "arch": "qwen35moe", "n_layers": 40, "n_kv_heads": 2,
        "key_length": 256, "value_length": 256, "n_ctx_train": 262144})
    monkeypatch.setattr("os.path.getsize", lambda p: 24000 * 1024 * 1024)

    models = {m["model"]: m for m in t.list_models()}
    for name in ("first-moe", "second-moe"):
        assert models[name]["cpu_moe"] is False, name
        assert models[name]["recommended"], name           # sized like any full-offload model
        assert "reason" not in models[name] or "RAM" not in (models[name]["reason"] or "")
    assert models["really-cpu-moe"]["cpu_moe"] is True     # the real flag still counts
    assert models["really-cpu-moe"]["recommended"] is None


def test_strip_yaml_comments_keeps_flags_and_inline_hashes():
    block = ('  "m":\n    cmd: |\n      -m "a#b.gguf" -c 8192\n'
             '  # --cpu-moe removed\n    # indented comment\n    ttl: 600\n')
    out = t._strip_yaml_comments(block)
    assert "--cpu-moe" not in out and "indented comment" not in out
    assert '-m "a#b.gguf" -c 8192' in out and "ttl: 600" in out   # only whole-line comments go
