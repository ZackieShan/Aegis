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
    """Total VRAM of the first CUDA GPU, in MiB (None if no nvidia-smi)."""
    exe = _which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10)
        line = (out.stdout or "").strip().splitlines()
        return int(line[0].strip()) if line else None
    except Exception:
        return None


def system_ram_mb() -> Optional[int]:
    try:
        import psutil
        return int(psutil.virtual_memory().total / (1024 * 1024))
    except Exception:
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

        arch = val("general.architecture")
        if not arch:
            return {}

        def a(suffix: str):
            return val(f"{arch}.{suffix}")

        n_layers = a("block_count")
        n_head = a("attention.head_count")
        n_kv = a("attention.head_count_kv")
        # head_count_kv can be a per-layer array on some arches — take the max.
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
    except Exception as e:
        logger.debug(f"gguf_meta failed for {path}: {e}")
        return {}


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
        vram_mb = gpu_vram_mb()
    meta = gguf_meta(model_path)
    try:
        weights_mb = os.path.getsize(model_path) / (1024 * 1024)
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
        result["reason"] = "insufficient info (need nvidia-smi + gguf metadata)"
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
    # Only keys indented under models: (indent > 0) — skip top-level keys.
    keys = [m for m in keys if len(m.group(1)) >= 2]
    blocks = []
    for i, m in enumerate(keys):
        start = m.start()
        end = keys[i + 1].start() if i + 1 < len(keys) else len(text)
        blocks.append({"name": m.group(2), "start": start, "end": end,
                       "text": text[start:end]})
    return blocks


def configured_context(model: str) -> Optional[int]:
    """The `-c` a model is actually served with (llama-swap doesn't expose /slots,
    so this is how Aegis learns the real window for context trimming). None if the
    model isn't in the config or has no -c."""
    text = _read_config()
    if not text:
        return None
    for b in _model_blocks(text):
        if b["name"] == model:
            m = _CTX_RE.search(b["text"])
            return int(m.group(2)) if m else None
    return None


def list_models(recommend: bool = True) -> List[Dict[str, Any]]:
    """Every tunable llama-server model in the config, with current (+ recommended) ctx.

    recommend=False skips the per-model GGUF metadata read (which is slow on big
    files) — use it when you only need names/current contexts (e.g. a dashboard),
    not the tuning recommendation.
    """
    text = _read_config()
    if not text:
        return []
    vram = gpu_vram_mb() if recommend else None
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
        cpu_moe = ("--cpu-moe" in body) or ("--n-cpu-moe" in body)
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
