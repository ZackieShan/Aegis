"""Unit tests for the AMD sysfs VRAM fallback in src/engine_tuner.py.

gpu_vram_mb() must keep preferring nvidia-smi but fall back to the kernel's
/sys/class/drm amdgpu memory counters, with the Strix Halo rule: a tiny BIOS
VRAM carveout means GTT is the real GPU memory pool.
"""
import pytest

from src import engine_tuner as t


MiB = 1024 ** 2
GiB = 1024 ** 3


def _make_card(root, name, vendor=None, vram=None, vis=None, gtt=None):
    """Build a fake /sys/class/drm/<name>/device tree. Values are bytes;
    None leaves the file out (the probe must treat a missing file as 0)."""
    dev = root / name / "device"
    dev.mkdir(parents=True)
    if vendor is not None:
        (dev / "vendor").write_text(vendor + "\n", encoding="utf-8")
    for fname, val in (("mem_info_vram_total", vram),
                       ("mem_info_vis_vram_total", vis),
                       ("mem_info_gtt_total", gtt)):
        if val is not None:
            (dev / fname).write_text(str(val) + "\n", encoding="utf-8")
    return dev


# -- _amd_vram_mb ---------------------------------------------------------------
def test_amd_vram_discrete_card(tmp_path):
    # Real VRAM pool on a discrete card -- GTT (RAM spill space) is ignored.
    _make_card(tmp_path, "card0", vendor="0x1002",
               vram=24 * GiB, vis=256 * MiB, gtt=16 * GiB)
    assert t._amd_vram_mb(str(tmp_path)) == 24576


def test_amd_vram_strix_halo_apu_uses_gtt(tmp_path):
    # Strix Halo shape: 512MB BIOS carveout, GTT is the real 120GiB pool.
    _make_card(tmp_path, "card1", vendor="0x1002",
               vram=512 * MiB, vis=512 * MiB, gtt=120 * GiB)
    assert t._amd_vram_mb(str(tmp_path)) == 120 * 1024


def test_amd_vram_non_amd_vendor(tmp_path):
    _make_card(tmp_path, "card0", vendor="0x10de", vram=24 * GiB)
    assert t._amd_vram_mb(str(tmp_path)) is None


def test_amd_vram_empty_root(tmp_path):
    assert t._amd_vram_mb(str(tmp_path)) is None


def test_amd_vram_skips_connectors_and_bare_cards(tmp_path):
    # cardN-DP-1 connector entries aren't GPUs, and an AMD card with no
    # mem_info_* files at all (missing file = 0) must not produce a value.
    _make_card(tmp_path, "card0-DP-1", vendor="0x1002", gtt=120 * GiB)
    _make_card(tmp_path, "card0", vendor="0x1002")
    assert t._amd_vram_mb(str(tmp_path)) is None


def test_amd_vram_max_across_cards(tmp_path):
    _make_card(tmp_path, "card0", vendor="0x1002", vram=8 * GiB)
    _make_card(tmp_path, "card1", vendor="0x1002", vram=24 * GiB)
    assert t._amd_vram_mb(str(tmp_path)) == 24576


# -- gpu_vram_mb integration ----------------------------------------------------
def test_gpu_vram_mb_amd_fallback(monkeypatch, tmp_path):
    # No nvidia-smi anywhere -> gpu_vram_mb falls through to the sysfs scan.
    _make_card(tmp_path, "card0", vendor="0x1002",
               vram=512 * MiB, vis=512 * MiB, gtt=96 * GiB)
    monkeypatch.setattr(t, "_which", lambda name: None)
    real = t._amd_vram_mb
    monkeypatch.setattr(t, "_amd_vram_mb", lambda: real(str(tmp_path)))
    assert t.gpu_vram_mb() == 96 * 1024


def test_gpu_vram_mb_prefers_nvidia(monkeypatch):
    # nvidia-smi present and answering -> sysfs is never consulted.
    class _Out:
        stdout = "24564\n"
    monkeypatch.setattr(t, "_which", lambda name: "nvidia-smi")
    monkeypatch.setattr(t.subprocess, "run", lambda *a, **k: _Out())
    monkeypatch.setattr(t, "_amd_vram_mb",
                        lambda: pytest.fail("sysfs must not run when nvidia-smi answers"))
    assert t.gpu_vram_mb() == 24564


def test_insufficient_info_reason_mentions_amd(monkeypatch, tmp_path):
    f = tmp_path / "m.gguf"
    f.write_bytes(b"\0")
    monkeypatch.setattr(t, "gguf_meta", lambda p: {})
    monkeypatch.setattr(t, "gpu_vram_mb", lambda: None)
    rec = t.recommend_context(str(f))
    assert rec["recommended"] is None
    assert "AMD sysfs" in rec["reason"]


# -- gpu_budget_mb: unified-memory host reserve ---------------------------------
def _budget_env(monkeypatch, tmp_path, *, ram_mb, nvidia=None):
    monkeypatch.setattr(t, "_which", lambda name: "nvidia-smi" if nvidia else None)
    if nvidia:
        class _Out:
            stdout = f"{nvidia}\n"
        monkeypatch.setattr(t.subprocess, "run", lambda *a, **k: _Out())
    real = t._amd_pool
    monkeypatch.setattr(t, "_amd_pool", lambda: real(str(tmp_path)))
    monkeypatch.setattr(t, "_amd_vram_mb", lambda: (real(str(tmp_path)) or (None,))[0])
    monkeypatch.setattr(t, "system_ram_mb", lambda: ram_mb)
    monkeypatch.delenv("AEGIS_UMA_HOST_RESERVE_MB", raising=False)


def test_budget_on_strix_halo_holds_back_host_ram(monkeypatch, tmp_path):
    """GTT limit 120GiB on a 128GB box: the pool IS system RAM, so planning
    against all of it leaves the OS nothing. Budget = RAM - host reserve."""
    _make_card(tmp_path, "card1", vendor="0x1002",
               vram=512 * MiB, vis=512 * MiB, gtt=120 * GiB)
    _budget_env(monkeypatch, tmp_path, ram_mb=127900)
    assert t.gpu_vram_mb() == 120 * 1024                 # the hardware fact is unchanged
    assert t.gpu_budget_mb() == 127900 - t._UMA_HOST_RESERVE_MB
    monkeypatch.setenv("AEGIS_UMA_HOST_RESERVE_MB", "8000")
    assert t.gpu_budget_mb() == 119900
    monkeypatch.setenv("AEGIS_UMA_HOST_RESERVE_MB", "not-a-number")
    assert t.gpu_budget_mb() == 127900 - t._UMA_HOST_RESERVE_MB


def test_budget_never_exceeds_a_small_gtt_limit(monkeypatch, tmp_path):
    # Stock 96GiB GTT limit on the same box: RAM - reserve is larger, GTT wins.
    _make_card(tmp_path, "card1", vendor="0x1002",
               vram=512 * MiB, vis=512 * MiB, gtt=96 * GiB)
    _budget_env(monkeypatch, tmp_path, ram_mb=127900)
    assert t.gpu_budget_mb() == 96 * 1024


def test_budget_discrete_amd_card_is_its_vram(monkeypatch, tmp_path):
    _make_card(tmp_path, "card0", vendor="0x1002",
               vram=24 * GiB, vis=256 * MiB, gtt=16 * GiB)
    _budget_env(monkeypatch, tmp_path, ram_mb=32000)
    assert t.gpu_budget_mb() == 24576                    # dedicated VRAM: no host reserve


def test_budget_nvidia_card_ignores_an_amd_igpu(monkeypatch, tmp_path):
    # A 4090 next to an AMD iGPU: the budget is the NVIDIA card's VRAM, and the
    # iGPU's (unified) pool must not trigger the reserve.
    _make_card(tmp_path, "card1", vendor="0x1002",
               vram=512 * MiB, vis=512 * MiB, gtt=30 * GiB)
    _budget_env(monkeypatch, tmp_path, ram_mb=64000, nvidia=24564)
    assert t.gpu_budget_mb() == 24564


def test_recommend_context_defaults_to_the_budget(monkeypatch, tmp_path):
    f = tmp_path / "m.gguf"
    f.write_bytes(b"\0")
    monkeypatch.setattr(t, "gguf_meta", lambda p: {
        "arch": "x", "n_layers": 60, "n_kv_heads": 16,
        "key_length": 128, "value_length": 128, "n_ctx_train": 262144})
    monkeypatch.setattr(t.os.path, "getsize", lambda p: 80000 * MiB)
    monkeypatch.setattr(t, "gpu_vram_mb", lambda: pytest.fail("must size against the budget"))
    monkeypatch.setattr(t, "gpu_budget_mb", lambda: 111000)
    rec = t.recommend_context(str(f))
    assert rec["vram_mb"] == 111000
    kv_mb_per_tok = 60 * 16 * 256 * 2 / MiB
    expected = int((111000 - 80000 - t._RESERVE_MB - t._SAFETY_MB) / kv_mb_per_tok) // 4096 * 4096
    assert t._FLOOR < expected < t._HARD_CAP          # the numbers exercise the math, not a clamp
    assert rec["recommended"] == expected


def test_system_ram_mb_without_psutil(monkeypatch):
    """psutil is optional; the host reserve must still work without it."""
    import builtins
    real_import = builtins.__import__

    def _no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    ram = t.system_ram_mb()
    assert isinstance(ram, int) and ram > 256        # any real machine has >256MiB


# -- gpu_free_mb: live headroom ---------------------------------------------------
def _make_used(dev, vram_used=None, gtt_used=None):
    for fname, val in (("mem_info_vram_used", vram_used), ("mem_info_gtt_used", gtt_used)):
        if val is not None:
            (dev / fname).write_text(str(val) + "\n", encoding="utf-8")


_REAL_AMD_USAGE = t._amd_usage      # captured once: _free_env may run several times per test


def _free_env(monkeypatch, tmp_path, *, mem_available_mb):
    monkeypatch.setattr(t, "_which", lambda name: None)
    monkeypatch.setattr(t, "_amd_usage", lambda: _REAL_AMD_USAGE(str(tmp_path)))
    monkeypatch.setattr(t, "_mem_available_mb", lambda: mem_available_mb)


def test_free_on_unified_memory_is_capped_by_available_ram(monkeypatch, tmp_path):
    """A 63GB chat model resident: GTT says 57GiB are free, but RAM held by
    other processes is just as gone on a unified-memory box."""
    dev = _make_card(tmp_path, "card1", vendor="0x1002", vram=512 * MiB, vis=512 * MiB, gtt=120 * GiB)
    _make_used(dev, vram_used=300 * MiB, gtt_used=63 * GiB)
    _free_env(monkeypatch, tmp_path, mem_available_mb=40000)
    assert t._amd_usage() == (120 * 1024, 63 * 1024 + 300, True)
    assert t.gpu_free_mb() == 40000 - t._UMA_FREE_CUSHION_MB            # RAM is the tighter bound
    _free_env(monkeypatch, tmp_path, mem_available_mb=110000)
    assert t.gpu_free_mb() == 120 * 1024 - (63 * 1024 + 300)            # GTT is the tighter bound
    _free_env(monkeypatch, tmp_path, mem_available_mb=None)
    assert t.gpu_free_mb() == 120 * 1024 - (63 * 1024 + 300)            # no meminfo → GTT only


def test_free_on_discrete_amd_card_ignores_system_ram(monkeypatch, tmp_path):
    dev = _make_card(tmp_path, "card0", vendor="0x1002", vram=24 * GiB, vis=256 * MiB, gtt=16 * GiB)
    _make_used(dev, vram_used=20 * GiB, gtt_used=1 * GiB)
    _free_env(monkeypatch, tmp_path, mem_available_mb=1000)
    assert t.gpu_free_mb() == 4 * 1024


def test_free_never_negative_and_none_without_a_gpu(monkeypatch, tmp_path):
    dev = _make_card(tmp_path, "card1", vendor="0x1002", vram=512 * MiB, vis=512 * MiB, gtt=96 * GiB)
    _make_used(dev, gtt_used=96 * GiB)
    _free_env(monkeypatch, tmp_path, mem_available_mb=2000)
    assert t.gpu_free_mb() == 0
    monkeypatch.setattr(t, "_amd_usage", lambda: None)            # no AMD card at all
    assert t.gpu_free_mb() is None


def test_free_prefers_nvidia_smi(monkeypatch):
    class _Out:
        stdout = "9000\n"
    monkeypatch.setattr(t, "_which", lambda name: "nvidia-smi")
    monkeypatch.setattr(t.subprocess, "run", lambda *a, **k: _Out())
    monkeypatch.setattr(t, "_amd_usage", lambda: pytest.fail("sysfs must not run when nvidia-smi answers"))
    assert t.gpu_free_mb() == 9000


def test_mem_available_parses_meminfo(tmp_path):
    f = tmp_path / "meminfo"
    f.write_text("MemTotal:       130993004 kB\nMemFree:  1 kB\nMemAvailable:   81920000 kB\n", encoding="utf-8")
    assert t._mem_available_mb(str(f)) == 80000
    assert t._mem_available_mb(str(tmp_path / "missing")) is None
