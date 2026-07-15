"""Chat-during-render guard.

Two failure directions matter and both are tested here:
  * swapping when it shouldn't (chat needlessly degraded to a 3B on CPU);
  * NOT swapping when it should (llama-swap evicts sd-server, render dies).
"""

import pytest

from src import gpu_guard, job_queue

LOCAL = "http://127.0.0.1:9090/v1"
REMOTE = "https://api.openai.com/v1"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    job_queue._reset_for_tests()
    monkeypatch.setattr(gpu_guard, "is_enabled", lambda owner=None: True)
    monkeypatch.setattr(gpu_guard, "fallback_model", lambda owner=None: "chat-lite-cpu")
    # Default: the fallback is properly served locally.
    monkeypatch.setattr(gpu_guard, "_endpoint_serves", lambda url, model, owner=None: True)
    yield
    job_queue._reset_for_tests()


def _render():
    qid = job_queue.add("video", "a cat surfing")
    job_queue.start(qid)
    return qid


# ── must NOT swap ──

def test_no_swap_when_gpu_idle():
    assert gpu_guard.busy_swap("qwen3-coder-30b", LOCAL) is None


def test_no_swap_for_remote_endpoint():
    """A remote model runs on someone else's GPU — swapping would degrade chat
    for no reason."""
    _render()
    assert gpu_guard.busy_swap("gpt-4o", REMOTE) is None


def test_no_swap_when_already_on_fallback():
    _render()
    assert gpu_guard.busy_swap("chat-lite-cpu", LOCAL) is None


def test_no_swap_when_disabled(monkeypatch):
    monkeypatch.setattr(gpu_guard, "is_enabled", lambda owner=None: False)
    _render()
    assert gpu_guard.busy_swap("qwen3-coder-30b", LOCAL) is None


def test_no_swap_when_fallback_not_served(monkeypatch):
    """If the CPU entry isn't configured, leave the user on a model that works.
    A degraded reply beats a 404 and no reply."""
    monkeypatch.setattr(gpu_guard, "_endpoint_serves", lambda url, model, owner=None: False)
    _render()
    assert gpu_guard.busy_swap("qwen3-coder-30b", LOCAL) is None


def test_no_swap_for_non_gpu_work():
    """A recipe run doesn't touch VRAM."""
    qid = job_queue.add("recipe", "summarize inbox")
    job_queue.start(qid)
    assert gpu_guard.busy_swap("qwen3-coder-30b", LOCAL) is None


def test_no_swap_after_render_finishes():
    qid = _render()
    job_queue.finish(qid, "done")
    assert gpu_guard.busy_swap("qwen3-coder-30b", LOCAL) is None


# ── must swap ──

def test_swaps_during_render():
    _render()
    out = gpu_guard.busy_swap("qwen3-coder-30b", LOCAL)
    assert out is not None
    assert out["model"] == "chat-lite-cpu"
    assert out["original_model"] == "qwen3-coder-30b"


def test_swap_notice_names_the_blocking_job_and_is_readable():
    _render()
    out = gpu_guard.busy_swap("qwen3-coder-30b", LOCAL)
    notice = out["notice"]
    assert "chat-lite-cpu" in notice
    assert "CPU" in notice
    assert "cat surfing" in notice  # tells the user *what* is hogging the GPU


def test_swaps_while_render_still_queued():
    """Queued counts: the render is about to take the card and a chat load in
    that window still evicts it."""
    job_queue.add("video", "clip")
    assert gpu_guard.busy_swap("qwen3-coder-30b", LOCAL) is not None


def test_swaps_for_image_render_too():
    qid = job_queue.add("image", "a fox")
    job_queue.start(qid)
    assert gpu_guard.busy_swap("qwen3-coder-30b", LOCAL) is not None


def test_swap_reports_blocking_job():
    _render()
    out = gpu_guard.busy_swap("qwen3-coder-30b", LOCAL)
    assert out["job"]["kind"] == "video"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9090/v1",
    "http://localhost:9090/v1",
    "http://0.0.0.0:9090/v1",
])
def test_local_engine_variants_all_swap(url):
    _render()
    assert gpu_guard.busy_swap("qwen3-coder-30b", url) is not None


@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1",
    "https://api.anthropic.com/v1",
    "http://192.168.1.50:9090/v1",  # another box's GPU, not ours
])
def test_non_local_engines_never_swap(url):
    _render()
    assert gpu_guard.busy_swap("qwen3-coder-30b", url) is None


# ── context clamping (agent mode) ──

def test_swap_advertises_the_fallback_context():
    _render()
    out = gpu_guard.busy_swap("qwen3-coder-30b", LOCAL)
    assert out["context_length"] == gpu_guard.FALLBACK_CONTEXT


def test_clamp_is_noop_without_swap():
    """The overwhelmingly common path: no render, don't touch the budget."""
    assert gpu_guard.clamp_context(45056, None) == 45056
    assert gpu_guard.clamp_context(None, None) is None


def test_clamp_shrinks_gpu_window_to_fallback():
    """45K of messages sent to the 32K CPU model is a 400, not a reply."""
    _render()
    swap = gpu_guard.busy_swap("qwen3-coder-30b", LOCAL)
    assert gpu_guard.clamp_context(45056, swap) == gpu_guard.FALLBACK_CONTEXT


def test_clamp_keeps_smaller_caller_budget():
    """Never *raise* a caller's budget — an 8K session stays 8K."""
    _render()
    swap = gpu_guard.busy_swap("qwen3-coder-30b", LOCAL)
    assert gpu_guard.clamp_context(8192, swap) == 8192


def test_clamp_handles_missing_and_bad_values():
    _render()
    swap = gpu_guard.busy_swap("qwen3-coder-30b", LOCAL)
    assert gpu_guard.clamp_context(None, swap) == gpu_guard.FALLBACK_CONTEXT
    assert gpu_guard.clamp_context("nonsense", swap) == gpu_guard.FALLBACK_CONTEXT


def _engine_config():
    """The parsed llama-swap config, or None when it isn't on this machine."""
    from pathlib import Path

    cfg = Path(__file__).resolve().parents[2] / "engine" / "llama-swap.yaml"
    if not cfg.exists():
        return None
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(cfg.read_text(encoding="utf-8"))


def test_fallback_context_matches_the_engine_config():
    """gpu_guard.FALLBACK_CONTEXT and chat-lite-cpu's -c must not drift: if the
    constant claims more than the model was started with, every swapped agent
    turn 400s with 'request exceeds available context size'."""
    import re

    cfg = _engine_config()
    if not cfg or gpu_guard.DEFAULT_FALLBACK_MODEL not in (cfg.get("models") or {}):
        pytest.skip("chat-lite-cpu not configured on this machine")
    cmd = cfg["models"][gpu_guard.DEFAULT_FALLBACK_MODEL]["cmd"]
    ctx = re.search(r"(?:-c|--ctx-size)\s+(\d+)", cmd)
    assert ctx, "chat-lite-cpu has no -c flag"
    assert int(ctx.group(1)) == gpu_guard.FALLBACK_CONTEXT


def test_fallback_model_is_cpu_only():
    """--device none is what makes coexistence possible: a render leaves under
    1GB free, so a fallback that grabbed a CUDA context could OOM or, worse,
    push llama-swap into evicting the render anyway."""
    cfg = _engine_config()
    if not cfg or gpu_guard.DEFAULT_FALLBACK_MODEL not in (cfg.get("models") or {}):
        pytest.skip("chat-lite-cpu not configured on this machine")
    cmd = cfg["models"][gpu_guard.DEFAULT_FALLBACK_MODEL]["cmd"]
    assert "--device none" in cmd
    assert "-ngl 0" in cmd


def test_fallback_is_in_a_coexisting_group():
    """The routing half is useless without the engine half: if chat-lite-cpu
    isn't in a non-exclusive persistent group, llama-swap evicts the render the
    moment we route chat to it — which is the whole bug."""
    cfg = _engine_config()
    groups = (cfg or {}).get("groups") or {}
    if not cfg or gpu_guard.DEFAULT_FALLBACK_MODEL not in (cfg.get("models") or {}):
        pytest.skip("chat-lite-cpu not configured on this machine")
    assert groups, "no groups: section — every model evicts every other"
    owning = [g for g, spec in groups.items()
              if gpu_guard.DEFAULT_FALLBACK_MODEL in (spec.get("members") or [])]
    assert owning, "chat-lite-cpu belongs to no group"
    spec = groups[owning[0]]
    assert spec.get("exclusive") is False, "loading the fallback would evict the render"
    assert spec.get("persistent") is True, "a render would evict the fallback"


def test_no_comments_leak_into_engine_commands():
    """`cmd: |` is a literal block — a '#' line there is NOT a YAML comment, it
    is handed to llama-server as arguments and the model fails to load."""
    cfg = _engine_config()
    if not cfg:
        pytest.skip("engine/llama-swap.yaml not present")
    offenders = {
        name: [ln for ln in str(spec.get("cmd") or "").splitlines() if ln.strip().startswith("#")]
        for name, spec in (cfg.get("models") or {}).items()
    }
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, f"comment lines inside cmd blocks: {offenders}"
