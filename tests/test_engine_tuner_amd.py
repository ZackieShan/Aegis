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
