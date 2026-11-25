"""
engine_tuner.py — auto-tune llama.cpp context size per model from the model's
own GGUF metadata + the machine's GPU VRAM, so nobody has to hand-edit YAML.

The context window (`-c`) is a llama-server launch flag baked into
engine/llama-swap.yaml. Too small → "request exceeds the available context size"
400s during agentic use; too large → VRAM OOM / spill to system RAM. The right
value depends on the model (layers × KV-heads × head-dim → bytes/token) and the
card (free VRAM after weights). This computes it exactly and applies it by
rewriting the YAML, which llama-swap hot-reloads (`-watch-config`) — no restart.

Everything is guarded and text-surgical: we only touch the `-c N` token inside a
model's block, preserving comments and formatting.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# VRAM we hold back from weights+KV: CUDA context, compute/graph buffers, and a
# safety margin so a full context doesn't tip into system-RAM spill. Empirically
# a 30B-Q4 at 32K sat ~2.1GB above weights+KV, so 2500 + 1000 is comfortable.
_RESERVE_MB = 2500
_SAFETY_MB = 1000
_GRANULARITY = 4096      # round contexts down to a clean multiple
_FLOOR = 8192            # never recommend below this
_HARD_CAP = 131072       # practical ceiling regardless of what the math allows
# On a unified-memory APU (Strix Halo) the "GPU pool" is GTT — ordinary system
# RAM the kernel lets the GPU page in, and the ttm limit is usually set to
# nearly all of it (120GiB of 128). Planning weights + KV against that full
# figure leaves nothing for the OS, Aegis itself and the other engines, and
# the machine goes into swap long before llama.cpp reports an allocation
# failure. Hold this much system RAM back when sizing contexts there
# (override: AEGIS_UMA_HOST_RESERVE_MB).
_UMA_HOST_RESERVE_MB = 16384
_UMA_FREE_CUSHION_MB = 4096    # kept free when measuring live headroom (gpu_free_mb)


def _engine_dir() -> str:
    env = os.getenv("AEGIS_ENGINE_DIR")
    if env:
        return env
    from src.constants import BASE_DIR
    return os.path.abspath(os.path.join(BASE_DIR, os.pardir, "engine"))


def _config_path() -> str:
    return os.getenv("LLAMA_SWAP_CONFIG") or os.path.join(_engine_dir(), "llama-swap.yaml")


# ── hardware ──────────────────────────────────────────────────────────────────
def gpu_vram_mb() -> Optional[int]:
    """Usable GPU memory in MiB — NVIDIA via nvidia-smi, else AMD via sysfs
    (/sys/class/drm). None when neither reports a GPU."""
    exe = _which("nvidia-smi")
    if exe:
        try:
            out = subprocess.run([exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10)
            line = (out.stdout or "").strip().splitlines()
            if line:
                return int(line[0].strip())
        except Exception:
            pass
    return _amd_vram_mb()


def _uma_host_reserve_mb() -> int:
    try:
        return max(0, int(os.getenv("AEGIS_UMA_HOST_RESERVE_MB", "") or _UMA_HOST_RESERVE_MB))
    except ValueError:
        return _UMA_HOST_RESERVE_MB


def gpu_budget_mb() -> Optional[int]:
    """Memory a model's weights + KV cache may plan to occupy, in MiB.

    gpu_vram_mb() for a card with its own VRAM. On a unified-memory AMD APU
    the pool is system RAM, so the budget is additionally capped at
    (system RAM - host reserve): see _UMA_HOST_RESERVE_MB.
    """
    total = gpu_vram_mb()
    if not total:
        return total
    pool = _amd_pool()
    if pool and pool[1] and pool[0] == total:
        ram = system_ram_mb()
        if ram:
            total = min(total, max(ram - _uma_host_reserve_mb(), 0))
    return total


def gpu_free_mb() -> Optional[int]:
    """GPU memory a NEW workload could claim right now, in MiB (None when
    unknown). NVIDIA: nvidia-smi memory.free. AMD discrete: VRAM total - used.
    AMD unified-memory APU: the smaller of (GTT limit - GTT in use) and
    (MemAvailable - host reserve) — the pool is system RAM, so RAM that other
    processes hold is just as unavailable as GTT another engine holds.
    """
    exe = _which("nvidia-smi")
    if exe:
        try:
            out = subprocess.run([exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10)
            line = (out.stdout or "").strip().splitlines()
            if line:
                return int(line[0].strip())
        except Exception:
            pass
    usage = _amd_usage()
    if not usage:
        return None
    total, used, unified = usage
    free = max(total - used, 0)
    if unified:
        avail = _mem_available_mb()
        if avail is not None:
            # MemAvailable already excludes what every other process holds, so
            # only a small cushion is kept here — not the planning-time host
            # reserve gpu_budget_mb() uses for a machine with nothing loaded.
            free = min(free, max(avail - _UMA_FREE_CUSHION_MB, 0))
    return free


def _mem_available_mb(meminfo: str = "/proc/meminfo") -> Optional[int]:
    """MemAvailable from /proc/meminfo in MiB (counts reclaimable page cache
    as available, which is what matters before a model load)."""
    try:
        with open(meminfo, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _amd_usage(sysfs_root: str = "/sys/class/drm") -> Optional[tuple]:
    """(pool MiB, in-use MiB, unified) for the AMD GPU _amd_pool picks."""
    import glob
    mib = 1024 * 1024

    def _read_int(path: str) -> int:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read().strip()
            return int(raw) if raw.isdigit() else 0
        except OSError:
            return 0

    best = None
    for dev in glob.glob(os.path.join(sysfs_root, "card*", "device")):
        if "-" in os.path.basename(os.path.dirname(dev)):
            continue
        try:
            with open(os.path.join(dev, "vendor"), "r", encoding="utf-8", errors="replace") as f:
                if f.read().strip() != "0x1002":
                    continue
        except OSError:
            continue
        vram = _read_int(os.path.join(dev, "mem_info_vram_total"))
        vram_used = _read_int(os.path.join(dev, "mem_info_vram_used"))
        if vram >= 4 * 1024 * mib:
            total, used, unified = vram // mib, vram_used // mib, False
        else:
            total = max(_read_int(os.path.join(dev, "mem_info_vis_vram_total")),
                        _read_int(os.path.join(dev, "mem_info_gtt_total"))) // mib
            used = (_read_int(os.path.join(dev, "mem_info_gtt_used")) + vram_used) // mib
            unified = True
        if total > 0 and (best is None or total > best[0]):
            best = (total, used, unified)
    return best


def _amd_vram_mb(sysfs_root: str = "/sys/class/drm") -> Optional[int]:
    """AMD GPU memory in MiB straight from the kernel — no rocm-smi needed.

    Mirrors services/hwfit/hardware._detect_amd: a discrete card reports its
    real pool in mem_info_vram_total, while an APU like Strix Halo carves out
    a tiny BIOS VRAM slice and pages GPU allocations from GTT, so there the
    usable budget is max(vis_vram, gtt). Returns the max across cards, None
    when no AMD card is found.
    """
    pool = _amd_pool(sysfs_root)
    return pool[0] if pool else None


def _amd_pool(sysfs_root: str = "/sys/class/drm") -> Optional[tuple]:
    """(pool MiB, unified) for the largest AMD GPU: `unified` is True when
    the pool is GTT system RAM (an APU), False for a discrete card's VRAM."""
    import glob
    mib = 1024 * 1024

    def _read_int(path: str) -> int:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read().strip()
            return int(raw) if raw.isdigit() else 0
        except OSError:
            return 0

    best = None
    best_unified = False
    for dev in glob.glob(os.path.join(sysfs_root, "card*", "device")):
        if "-" in os.path.basename(os.path.dirname(dev)):
            continue  # cardN-DP-1 etc. are connectors, not GPUs
        try:
            with open(os.path.join(dev, "vendor"), "r", encoding="utf-8", errors="replace") as f:
                vendor = f.read().strip()
        except OSError:
            continue
        if vendor != "0x1002":
            continue
        vram = _read_int(os.path.join(dev, "mem_info_vram_total"))
        if vram >= 4 * 1024 * mib:
            total = vram // mib  # discrete card — VRAM is the real pool
            unified = False
        else:
            # APU with a small carveout: GTT (or vis_vram) is the real pool.
            total = max(_read_int(os.path.join(dev, "mem_info_vis_vram_total")),
                        _read_int(os.path.join(dev, "mem_info_gtt_total"))) // mib
            unified = True
        if total > 0 and (best is None or total > best):
            best, best_unified = total, unified
    return (best, best_unified) if best is not None else None


def system_ram_mb() -> Optional[int]:
    try:
        import psutil
        return int(psutil.virtual_memory().total / (1024 * 1024))
    except Exception:
        pass
    # psutil is optional (and absent from the Linux venv) — POSIX can answer
    # from sysconf alone.
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 * 1024))
    except (AttributeError, ValueError, OSError):
        return None


def _which(name: str) -> Optional[str]:
    import shutil
    return shutil.which(name) or shutil.which(name + ".exe")


# ── model metadata ────────────────────────────────────────────────────────────
# GGUFReader parses the full tensor index — 10-30s on a 20GB+ file, and far
# worse while another model is streaming off the same disk. That latency made
# GET /api/engine/status exceed the browser's fetch patience ("/engine fails
# to fetch"). Model files rarely change, so cache the parsed header per
# (size, mtime) and persist across restarts.
_META_CACHE: Dict[str, Dict[str, Any]] = {}
_META_CACHE_LOADED = False


def _meta_cache_path() -> str:
    try:
        from src.constants import DATA_DIR
        return os.path.join(DATA_DIR, "cache", "gguf_meta.json")
    except Exception:
        return os.path.join(_engine_dir(), ".gguf_meta_cache.json")


def _file_key(path: str) -> Optional[List[Any]]:
    try:
        st = os.stat(path)
        return [st.st_size, int(st.st_mtime)]
    except OSError:
        return None


def _load_meta_cache() -> None:
    global _META_CACHE_LOADED
    if _META_CACHE_LOADED:
        return
    _META_CACHE_LOADED = True
    try:
        import json
        with open(_meta_cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _META_CACHE.update(data)
    except Exception:
        pass


def _save_meta_cache() -> None:
    try:
        import json
        p = _meta_cache_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(_META_CACHE, f)
    except Exception as e:
        logger.debug(f"gguf meta cache save failed: {e}")


def gguf_meta(path: str) -> Dict[str, Any]:
    """Layers / KV-heads / head-dim / trained-context from a GGUF header.

    Uses the `gguf` package when available (exact); returns {} otherwise so the
    caller falls back to a heuristic. Results are cached per (size, mtime).
    """
    _load_meta_cache()
    key = _file_key(path)
    if key:
        cached = _META_CACHE.get(path)
        if cached and cached.get("key") == key and isinstance(cached.get("meta"), dict):
            return dict(cached["meta"])
    meta = _gguf_meta_uncached(path)
    # Only cache real parses — a missing `gguf` package or unreadable file
    # returns {} and must be retried, not remembered.
    if key and meta:
        _META_CACHE[path] = {"key": key, "meta": meta}
        _save_meta_cache()
    return meta


def _gguf_meta_uncached(path: str) -> Dict[str, Any]:
    """Parse the header: the built-in key/value reader first, the `gguf`
    package as the fallback.

    GGUFReader indexes every tensor — seconds per file even from a warm page
    cache — and raises on a tensor type it does not know, so a model
    quantized with a scheme newer than the installed package (which llama.cpp
    itself loads fine) is unreadable through it. The header reader stops
    before the tensor index: ~0.1s, and independent of quant types."""
    return _gguf_meta_via_header(path) or _gguf_meta_via_reader(path)


def _meta_from_lookup(val) -> Dict[str, Any]:
    """Build the tuner's metadata dict from a `val(key) -> value|None`."""
    arch = val("general.architecture")
    if not arch or not isinstance(arch, str):
        return {}

    def a(suffix: str):
        return val(f"{arch}.{suffix}")

    n_layers = a("block_count")
    n_head = a("attention.head_count")
    n_kv = a("attention.head_count_kv")
    # head_count(_kv) can be a per-layer array on some arches — take the max.
    if isinstance(n_head, (list, tuple)):
        n_head = max(int(x) for x in n_head) if n_head else None
    if isinstance(n_kv, (list, tuple)):
        n_kv = max(int(x) for x in n_kv) if n_kv else None
    k_len = a("attention.key_length")
    v_len = a("attention.value_length")
    n_embd = a("embedding_length")
    # Fall back to embedding_length / head_count for head dim when absent.
    if not k_len and n_embd and n_head:
        k_len = int(n_embd) // int(n_head)
    if not v_len:
        v_len = k_len
    return {
        "arch": arch,
        "n_layers": int(n_layers) if n_layers else None,
        "n_kv_heads": int(n_kv) if n_kv else None,
        "key_length": int(k_len) if k_len else None,
        "value_length": int(v_len) if v_len else None,
        "n_ctx_train": int(a("context_length")) if a("context_length") else None,
    }


def _gguf_meta_via_reader(path: str) -> Dict[str, Any]:
    try:
        import gguf
    except Exception:
        return {}
    try:
        r = gguf.GGUFReader(path)

        def val(key: str):
            f = r.get_field(key)
            if not f:
                return None
            try:
                return f.contents()
            except Exception:
                return None

        return _meta_from_lookup(val)
    except Exception as e:
        logger.debug(f"gguf_meta (GGUFReader) failed for {path}: {e}")
        return {}


def _gguf_meta_via_header(path: str) -> Dict[str, Any]:
    try:
        kv = gguf_header_kv(path)
    except Exception as e:
        logger.debug(f"gguf_meta (header) failed for {path}: {e}")
        return {}
    return _meta_from_lookup(kv.get)


# GGUF metadata value types (gguf-py GGUFValueType) → struct codes.
_GGUF_SCALARS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f",
                 7: "?", 10: "Q", 11: "q", 12: "d"}
_GGUF_STRING, _GGUF_ARRAY = 8, 9
_GGUF_MAX_KV = 100_000          # sanity bound on the key/value count
_GGUF_MAX_STRING = 64 << 20     # sanity bound on one string (64 MiB)
_GGUF_KEEP_STRING = 4096        # longer strings (chat templates) are skipped
_GGUF_KEEP_ARRAY = 4096         # longer arrays (vocab, merges) are skipped


def gguf_header_kv(path: str) -> Dict[str, Any]:
    """Metadata key/values read straight from a GGUF (v2/v3) header.

    Stops before the tensor index, so it is independent of which tensor /
    quantization types the installed `gguf` package knows, and fast on any
    file size. Scalars, short strings and short numeric arrays are returned;
    long strings and big arrays (tokenizer vocab) are stepped over. Raises
    ValueError on anything that is not a well-formed GGUF header.
    """
    import struct

    out: Dict[str, Any] = {}
    with open(path, "rb") as f:
        def take(n: int) -> bytes:
            b = f.read(n)
            if len(b) != n:
                raise ValueError("truncated GGUF header")
            return b

        def num(code: str):
            return struct.unpack("<" + code, take(struct.calcsize("<" + code)))[0]

        def string(keep_up_to: int) -> Optional[str]:
            n = num("Q")
            if n > _GGUF_MAX_STRING:
                raise ValueError("implausible GGUF string length")
            if n > keep_up_to:
                f.seek(n, os.SEEK_CUR)
                return None
            return take(n).decode("utf-8", "replace")

        def value(vtype: int):
            if vtype in _GGUF_SCALARS:
                return num(_GGUF_SCALARS[vtype])
            if vtype == _GGUF_STRING:
                return string(_GGUF_KEEP_STRING)
            if vtype == _GGUF_ARRAY:
                etype, count = num("I"), num("Q")
                if etype in _GGUF_SCALARS:
                    code = _GGUF_SCALARS[etype]
                    size = struct.calcsize("<" + code)
                    if count <= _GGUF_KEEP_ARRAY:
                        return list(struct.unpack(f"<{count}{code}", take(count * size)))
                    f.seek(count * size, os.SEEK_CUR)
                    return None
                if etype == _GGUF_STRING:
                    for _ in range(count):
                        string(0)
                    return None
                raise ValueError(f"unsupported GGUF array element type {etype}")
            raise ValueError(f"unknown GGUF value type {vtype}")

        if take(4) != b"GGUF":
            raise ValueError("not a GGUF file")
        version = num("I")
        if version not in (2, 3):
            raise ValueError(f"unsupported GGUF version {version}")
        num("Q")                      # tensor count — not needed
        n_kv = num("Q")
        if n_kv > _GGUF_MAX_KV:
            raise ValueError("implausible GGUF metadata count")
        for _ in range(n_kv):
            key = string(_GGUF_MAX_STRING)
            v = value(num("I"))
            if key is not None and v is not None:
                out[key] = v
    return out


# llama.cpp split naming: <stem>-00001-of-00003.gguf
_SPLIT_RE = re.compile(r"^(?P<stem>.+)-(?P<idx>\d{5})-of-(?P<total>\d{5})\.gguf$", re.I)


def gguf_total_bytes(path: str) -> int:
    """On-disk size of a GGUF model, counting every shard of a split one.

    llama-swap's `-m` names only the first shard of a split model
    (…-00001-of-00002.gguf); sizing the weights from that file alone
    undercounts a 63GB model as 40GB and the recommended context then
    overshoots the GPU by the difference. Raises OSError when the file — or
    any sibling shard — is missing.
    """
    m = _SPLIT_RE.match(os.path.basename(path))
    if not m:
        return os.path.getsize(path)
    folder = os.path.dirname(path)
    width = len(m.group("total"))
    return sum(
        os.path.getsize(os.path.join(
            folder, f"{m.group('stem')}-{i:0{width}d}-of-{m.group('total')}.gguf"))
        for i in range(1, int(m.group("total")) + 1)
    )


def kv_bytes_per_token(meta: Dict[str, Any], cache_bits: int = 16) -> Optional[float]:
    """KV-cache bytes per token = layers × kv_heads × (k_len+v_len) × dtype_bytes."""
    try:
        n_layers = meta["n_layers"]
        n_kv = meta["n_kv_heads"]
        dims = meta["key_length"] + meta["value_length"]
    except Exception:
        return None
    if not (n_layers and n_kv and dims):
        return None
    dtype_bytes = 1.0 if cache_bits == 8 else 2.0  # q8_0 ≈ 1 byte/elem, fp16 = 2
    return float(n_layers) * float(n_kv) * float(dims) * dtype_bytes


def recommend_context(model_path: str, mmproj_path: Optional[str] = None,
                      vram_mb: Optional[int] = None, cache_bits: int = 16) -> Dict[str, Any]:
    """Largest safe context for a fully-offloaded model on this GPU."""
    if vram_mb is None:
        vram_mb = gpu_budget_mb()
    meta = gguf_meta(model_path)
    try:
        weights_mb = gguf_total_bytes(model_path) / (1024 * 1024)
        if mmproj_path and os.path.exists(mmproj_path):
            weights_mb += os.path.getsize(mmproj_path) / (1024 * 1024)
    except OSError:
        weights_mb = None

    kv_ptok = kv_bytes_per_token(meta, cache_bits)
    n_train = meta.get("n_ctx_train")
    result: Dict[str, Any] = {
        "arch": meta.get("arch"),
        "weights_mb": round(weights_mb) if weights_mb else None,
        "n_ctx_train": n_train,
        "kv_mb_per_1k": round(kv_ptok * 1000 / (1024 * 1024), 1) if kv_ptok else None,
        "vram_mb": vram_mb,
        "cache_bits": cache_bits,
    }

    if not (vram_mb and weights_mb and kv_ptok):
        result["recommended"] = None
        result["reason"] = "insufficient info (need nvidia-smi or AMD sysfs + gguf metadata)"
        return result

    kv_ptok_mb = kv_ptok / (1024 * 1024)
    budget_mb = vram_mb - weights_mb - _RESERVE_MB - _SAFETY_MB
    if budget_mb <= 0:
        result["recommended"] = None
        result["reason"] = "weights already fill VRAM — use a smaller quant or partial offload (-ngl)"
        return result

    raw = int(budget_mb / kv_ptok_mb)
    ctx = (raw // _GRANULARITY) * _GRANULARITY
    ctx = min(ctx, _HARD_CAP)
    if n_train:
        ctx = min(ctx, (n_train // _GRANULARITY) * _GRANULARITY or n_train)
    if ctx < _FLOOR:
        # Fits less than the floor at fp16 — suggest q8 KV (halves per-token cost).
        result["recommended"] = _FLOOR
        result["tight"] = True
        result["reason"] = ("only ~%d tokens fit at fp16 KV; recommending the %d floor. "
                            "Enable q8 KV cache to roughly double it." % (raw, _FLOOR))
        return result

    result["recommended"] = ctx
    if cache_bits == 16 and ctx < _HARD_CAP and n_train and ctx < n_train:
        result["note"] = "q8 KV cache would roughly double this"
    return result


# ── config (llama-swap.yaml) parse + surgical edit ───────────────────────────
_MODEL_KEY_RE = re.compile(r'^(\s*)"([^"]+)"\s*:\s*$', re.M)
_CTX_RE = re.compile(r'(-c|--ctx-size)\s+(\d+)')
_M_RE = re.compile(r'-m\s+"([^"]+)"')
_MMPROJ_RE = re.compile(r'--mmproj\s+"([^"]+)"')
_NGL_RE = re.compile(r'-ngl\s+(\d+)')


def _read_config() -> Optional[str]:
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.debug(f"read config failed: {e}")
        return None


def _model_blocks(text: str) -> List[Dict[str, Any]]:
    """Split the `models:` map into per-model text spans (name, start, end)."""
    keys = list(_MODEL_KEY_RE.finditer(text))

    def _key_indent(m) -> int:
        # group(1) is `\s*`, which also swallows blank lines above the key —
        # only the spaces on the key's own line are its indentation.
        return len(m.group(1).rsplit("\n", 1)[-1])

    # Only keys indented under models: (indent > 0) — skip top-level keys.
    keys = [m for m in keys if _key_indent(m) >= 2]
    # Model ids all sit at ONE indent. Quoted keys nested deeper belong to a
    # model (e.g. its filters.setParamsByID variants) and must not start a
    # block of their own — that would cut the real block in two.
    if keys:
        base = min(_key_indent(m) for m in keys)
        keys = [m for m in keys if _key_indent(m) == base]
    blocks = []
    for i, m in enumerate(keys):
        start = m.start()
        end = keys[i + 1].start() if i + 1 < len(keys) else len(text)
        blocks.append({"name": m.group(2), "start": start, "end": end,
                       "text": text[start:end]})
    return blocks


def _strip_yaml_comments(block: str) -> str:
    """A model block without its whole-line `#` comments."""
    return "\n".join(ln for ln in block.splitlines() if not ln.lstrip().startswith("#"))


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _block_alias_ids(block: Dict[str, Any]) -> List[str]:
    """Other model ids that are served by this block's process: its
    `aliases:` entries and the keys of `filters.setParamsByID` (llama-swap
    creates an alias per key; `${MODEL_ID}` expands to the block's own id).
    They share the block's llama-server, so they share its `-c`."""
    ids: List[str] = []
    lines = _strip_yaml_comments(block["text"]).splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("aliases:"):
            inline = stripped[len("aliases:"):].strip()
            if inline.startswith("["):
                ids += [x.strip().strip("\"'") for x in inline.strip("[]").split(",") if x.strip()]
                continue
            for nxt in lines[i + 1:]:
                item = nxt.strip()
                if not item:
                    continue
                if not item.startswith("- "):
                    break
                ids.append(item[2:].strip().strip("\"'"))
        elif stripped == "setParamsByID:":
            key_indent = None
            for nxt in lines[i + 1:]:
                if not nxt.strip():
                    continue
                ind = _indent(nxt)
                if ind <= _indent(line):
                    break
                if key_indent is None:
                    key_indent = ind
                if ind == key_indent and nxt.rstrip().endswith(":"):
                    ids.append(nxt.strip()[:-1].strip().strip("\"'"))
    name = block["name"]
    return [i.replace("${MODEL_ID}", name) for i in ids if i and i.replace("${MODEL_ID}", name) != name]


def _block_for(model: str, blocks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The config block that serves `model` — by its own id, else as one of
    another model's alias / setParamsByID ids."""
    for b in blocks:
        if b["name"] == model:
            return b
    for b in blocks:
        if model in _block_alias_ids(b):
            return b
    return None


def configured_context(model: str) -> Optional[int]:
    """The `-c` a model is actually served with (llama-swap doesn't expose /slots,
    so this is how Aegis learns the real window for context trimming). None if the
    model isn't in the config or has no -c. Alias ids and setParamsByID variants
    resolve to the model whose process serves them."""
    text = _read_config()
    if not text:
        return None
    b = _block_for(model, _model_blocks(text))
    if not b:
        return None
    m = _CTX_RE.search(_strip_yaml_comments(b["text"]))
    return int(m.group(2)) if m else None


def list_models(recommend: bool = True) -> List[Dict[str, Any]]:
    """Every tunable llama-server model in the config, with current (+ recommended) ctx.

    recommend=False skips the per-model GGUF metadata read (which is slow on big
    files) — use it when you only need names/current contexts (e.g. a dashboard),
    not the tuning recommendation.
    """
    text = _read_config()
    if not text:
        return []
    vram = gpu_budget_mb() if recommend else None
    out = []
    for b in _model_blocks(text):
        body = b["text"]
        if "llama-server" not in body:
            continue  # skip sd-server / non-llama entries (e.g. qwen-image)
        cur = _CTX_RE.search(body)
        mpath = _M_RE.search(body)
        ngl = _NGL_RE.search(body)
        if not (cur and mpath):
            continue  # only manage entries that actually have -c and -m
        full_offload = not ngl or int(ngl.group(1)) >= 60
        # --cpu-moe / --n-cpu-moe keep an MoE's expert weights in system RAM, so
        # only attention + shared layers sit in VRAM. The full GGUF size is then
        # NOT the VRAM footprint, and recommend_context (which subtracts the whole
        # file from VRAM) would wrongly conclude "weights fill VRAM". Treat these
        # as manually-tuned: the KV cache is the only real VRAM cost, so there's
        # usually lots of room to raise the context.
        # Flags only: a block's text runs to the next model's key, so it also
        # holds that model's leading comments — and a comment that merely
        # mentions the flag ("# --cpu-moe removed: unified memory") must not
        # mark this model as CPU-offloaded.
        flags = _strip_yaml_comments(body)
        cpu_moe = ("--cpu-moe" in flags) or ("--n-cpu-moe" in flags)
        entry = {
            "model": b["name"],
            "current_ctx": int(cur.group(2)),
            "ngl": int(ngl.group(1)) if ngl else 99,
            "full_offload": full_offload,
            "cpu_moe": cpu_moe,
        }
        if recommend and cpu_moe:
            entry["recommended"] = None
            entry["reason"] = ("MoE experts run in RAM (--cpu-moe) — the KV cache is the only "
                               "real VRAM cost, so you can usually raise this a lot; set it manually")
            try:
                entry["n_ctx_train"] = gguf_meta(mpath.group(1)).get("n_ctx_train")
            except Exception:
                pass
        elif recommend and full_offload:
            mm = _MMPROJ_RE.search(body)
            rec = recommend_context(mpath.group(1), mm.group(1) if mm else None, vram_mb=vram)
            entry.update(rec)
        elif recommend:
            entry["recommended"] = None
            entry["reason"] = "partial GPU offload (-ngl %s) — tune manually" % entry["ngl"]
        out.append(entry)
    return out


def set_context(model: str, ctx: int) -> Dict[str, Any]:
    """Rewrite `-c N` for one model in the config. llama-swap hot-reloads it."""
    ctx = int(ctx)
    if ctx < 512 or ctx > 1_048_576:
        return {"ok": False, "error": "context must be between 512 and 1048576"}
    text = _read_config()
    if text is None:
        return {"ok": False, "error": "could not read llama-swap.yaml"}
    blocks = _model_blocks(text)
    target = next((b for b in blocks if b["name"] == model), None)
    if not target:
        return {"ok": False, "error": f"model '{model}' not found in config"}
    body = target["text"]
    if not _CTX_RE.search(body):
        return {"ok": False, "error": f"model '{model}' has no -c flag to tune"}
    old = int(_CTX_RE.search(body).group(2))
    new_body = _CTX_RE.sub(lambda m: f"{m.group(1)} {ctx}", body, count=1)
    new_text = text[:target["start"]] + new_body + text[target["end"]:]
    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            f.write(new_text)
    except Exception as e:
        return {"ok": False, "error": f"write failed: {e}"}
    return {"ok": True, "model": model, "old_ctx": old, "new_ctx": ctx,
            "note": "llama-swap will hot-reload on the model's next (re)load"}


def autotune(model: Optional[str] = None) -> Dict[str, Any]:
    """Apply the recommended context to one model, or every tunable model."""
    models = list_models()
    if model:
        models = [m for m in models if m["model"] == model]
        if not models:
            return {"ok": False, "error": f"model '{model}' not found or not tunable"}
    applied = []
    for m in models:
        rec = m.get("recommended")
        if not rec or not m.get("full_offload"):
            applied.append({"model": m["model"], "skipped": m.get("reason", "no recommendation")})
            continue
        if rec == m["current_ctx"]:
            applied.append({"model": m["model"], "unchanged": rec})
            continue
        res = set_context(m["model"], rec)
        applied.append({"model": m["model"], **({"old_ctx": res.get("old_ctx"), "new_ctx": res.get("new_ctx")}
                                                if res.get("ok") else {"error": res.get("error")})})
    return {"ok": True, "applied": applied}
