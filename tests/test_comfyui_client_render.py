"""ComfyUI client behaviour around the GPU guard and other people's jobs
(2026-11 Linux/Strix Halo refresh).

Two things an inline ComfyUI image render got wrong once renders stopped
being a few seconds long (FLUX.2-dev runs for minutes):

* it never registered with the unified queue, so the GPU guard could not see
  it and a chat turn would load a second big model beside it;
* giving up on a prompt (cancel / deadline / budget) sent a bare
  POST /interrupt, which stops whatever ComfyUI is executing — not
  necessarily the prompt being given up on.
"""
import asyncio
import json

import pytest

from src import comfyui_client as cc
from src import job_queue


@pytest.fixture(autouse=True)
def _clean_queue():
    job_queue._reset_for_tests()
    yield
    job_queue._reset_for_tests()


class _Resp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.content = content
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeComfy:
    """Stands in for httpx.AsyncClient against a scripted ComfyUI."""

    def __init__(self, history=None):
        self.calls = []                 # (method, path, json)
        self.history = history or (lambda prompt_id, n: {})
        self._history_polls = 0

    def client(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _path(self, url):
        return url.replace(cc.base_url(), "")

    async def post(self, url, json=None, **k):
        path = self._path(url)
        self.calls.append(("POST", path, json))
        if path == "/prompt":
            return _Resp(200, {"prompt_id": "pid-1"})
        return _Resp(200, {})

    async def get(self, url, **k):
        path = self._path(url)
        self.calls.append(("GET", path.split("?")[0], None))
        if path.startswith("/history/"):
            self._history_polls += 1
            return _Resp(200, self.history("pid-1", self._history_polls))
        if path.startswith("/view"):
            return _Resp(200, content=b"\x89PNG-bytes")
        return _Resp(404)

    def posts(self, path):
        return [body for method, p, body in self.calls if method == "POST" and p == path]


def _use(monkeypatch, fake):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", fake.client)

    async def _no_sleep(_s):
        return None
    monkeypatch.setattr(cc.asyncio, "sleep", _no_sleep)
    from src import comfyui_workflows as cw
    monkeypatch.setattr(cw, "build_image_graph",
                        lambda model, **p: {"1": {"class_type": "X", "inputs": {}}})


_DONE = {"pid-1": {"status": {"status_str": "success"},
                   "outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "aegis", "type": "output"}]}}}}


# ── GPU-guard visibility of inline image renders ─────────────────────────────

async def test_image_render_is_gpu_busy_only_while_it_runs(monkeypatch):
    seen = {}

    async def _inner(model, prompt, **kw):
        busy = job_queue.gpu_busy()
        seen["during"] = busy and (busy["kind"], busy["status"], busy["meta"]["model"], busy["owner"])
        return {"b64_json": "QQ==", "seed": 1}

    monkeypatch.setattr(cc, "_render_image_inner", _inner)
    assert job_queue.gpu_busy() is None
    out = await cc.render_image("comfy-flux2-dev", "a fox", owner="alice", session_id="s1")
    assert out["b64_json"] == "QQ=="
    assert seen["during"] == ("image", "running", "comfy-flux2-dev", "alice")
    assert job_queue.gpu_busy() is None                      # released on success
    (entry,) = job_queue.snapshot()
    assert entry["status"] == "done" and entry["session_id"] == "s1"


async def test_image_render_error_result_releases_the_gpu(monkeypatch):
    async def _inner(model, prompt, **kw):
        return {"error": "ComfyUI: out of memory"}

    monkeypatch.setattr(cc, "_render_image_inner", _inner)
    out = await cc.render_image("comfy-flux2-klein", "a fox")
    assert out == {"error": "ComfyUI: out of memory"}
    assert job_queue.gpu_busy() is None
    (entry,) = job_queue.snapshot()
    assert entry["status"] == "error" and "out of memory" in entry["error"]


@pytest.mark.parametrize("exc", [RuntimeError("boom"), asyncio.CancelledError()])
async def test_image_render_exception_never_leaves_a_phantom_busy_entry(monkeypatch, exc):
    """A phantom 'running' image job would pin every chat turn to the CPU
    fallback model until the app restarts."""
    async def _inner(model, prompt, **kw):
        raise exc

    monkeypatch.setattr(cc, "_render_image_inner", _inner)
    with pytest.raises(type(exc)):
        await cc.render_image("comfy-flux2-klein", "a fox")
    assert job_queue.gpu_busy() is None
    assert job_queue.snapshot()[0]["status"] == "error"


async def test_image_budget_comes_from_the_catalog_entry(monkeypatch):
    from src import comfyui_workflows as cw
    budgets = {}

    async def _inner(model, prompt, **kw):
        budgets[model] = kw["budget_s"]
        return {"b64_json": "QQ==", "seed": 1}

    monkeypatch.setattr(cc, "_render_image_inner", _inner)
    await cc.render_image("comfy-flux2-dev", "p")            # slow model: its own budget
    await cc.render_image("comfy-flux2-klein", "p")          # no override: the default
    await cc.render_image("comfy-flux2-dev", "p", budget_s=42)   # explicit caller value wins
    assert cw.COMFY_IMAGE_MODELS["comfy-flux2-dev"]["budget_s"] > cc._IMAGE_BUDGET_S
    assert budgets == {"comfy-flux2-dev": 42, "comfy-flux2-klein": cc._IMAGE_BUDGET_S}
    await cc.render_image("comfy-flux2-dev", "p")
    assert budgets["comfy-flux2-dev"] == cw.COMFY_IMAGE_MODELS["comfy-flux2-dev"]["budget_s"]


# ── giving up on a prompt must not stop someone else's ────────────────────────

async def test_abandon_prompt_is_targeted():
    fake = _FakeComfy()
    await cc.abandon_prompt(fake, "pid-9")
    assert fake.posts("/queue") == [{"delete": ["pid-9"]}]          # still waiting → dequeued
    assert fake.posts("/interrupt") == [{"prompt_id": "pid-9"}]     # running → only if it is ours


async def test_abandon_prompt_survives_a_dead_comfyui():
    class _Down(_FakeComfy):
        async def post(self, url, json=None, **k):
            raise ConnectionError("refused")
    await cc.abandon_prompt(_Down(), "pid-9")                        # best-effort: no raise


async def test_image_render_success_path(monkeypatch):
    fake = _FakeComfy(history=lambda pid, n: _DONE if n >= 2 else {})
    _use(monkeypatch, fake)
    out = await cc._render_image_inner("comfy-x", "a fox", seed=5, budget_s=60)
    assert out["seed"] == 5 and out["b64_json"]
    assert fake.posts("/interrupt") == [] and fake.posts("/queue") == []
    assert fake.posts("/free")                                       # VRAM handed back afterwards


async def test_image_render_out_of_budget_stops_its_own_prompt(monkeypatch):
    fake = _FakeComfy(history=lambda pid, n: {})                     # never finishes
    _use(monkeypatch, fake)
    out = await cc._render_image_inner("comfy-x", "a fox", budget_s=0)
    assert "exceeded" in out["error"]
    assert fake.posts("/queue") == [{"delete": ["pid-1"]}]
    assert fake.posts("/interrupt") == [{"prompt_id": "pid-1"}]      # never a bare, global interrupt
    assert fake.posts("/free")


def _video_job(job_id="job-1"):
    from src import video_generation
    job = {"id": job_id, "queue_id": None, "status": "queued", "created": 0,
           "remote_id": "pid-1", "error": None, "progress": None}
    video_generation._JOBS[job_id] = job
    return job


async def test_cancelled_video_job_stops_only_its_own_prompt(monkeypatch):
    from src import video_generation
    fake = _FakeComfy()
    _use(monkeypatch, fake)
    job = _video_job()
    job["cancel_requested"] = True
    try:
        await cc._poll_job("job-1", "pid-1")
    finally:
        video_generation._JOBS.pop("job-1", None)
    assert job["status"] == "canceled"
    assert fake.posts("/queue") == [{"delete": ["pid-1"]}]
    assert fake.posts("/interrupt") == [{"prompt_id": "pid-1"}]


async def test_video_job_past_its_deadline_is_stopped_not_orphaned(monkeypatch):
    from src import video_generation
    fake = _FakeComfy()
    _use(monkeypatch, fake)
    job = _video_job()
    try:
        await cc._poll_job("job-1", "pid-1", deadline_s=0)
    finally:
        video_generation._JOBS.pop("job-1", None)
    assert job["status"] == "error" and "exceeded" in job["error"]
    assert fake.posts("/queue") == [{"delete": ["pid-1"]}]
    assert fake.posts("/interrupt") == [{"prompt_id": "pid-1"}]
    assert fake.posts("/free")


# ── making room for a media job beside the chat model (unified memory) ───────

class _FakeSwap:
    """httpx.AsyncClient stand-in for llama-swap's /running + per-model unload."""

    def __init__(self, running, unload_status=200):
        self.running = running
        self.unload_status = unload_status
        self.unloaded = []
        self.calls = []

    def client(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **k):
        self.calls.append(("GET", url))
        return _Resp(200, {"running": self.running})

    async def post(self, url, **k):
        self.calls.append(("POST", url))
        self.unloaded.append(url.rsplit("/", 1)[-1])
        return _Resp(self.unload_status, {"msg": "ok"})


_GPU_CHAT = {"model": "gpt-oss-120b", "state": "ready",
             "cmd": '"llama-server" -m "x.gguf" -ngl 99 -c 65536'}
_CPU_CHAT = {"model": "chat-moe-cpu", "state": "ready",
             "cmd": '"llama-server" -m "y.gguf" --device none -ngl 0 -c 32768'}
_TTS = {"model": "chatterbox-tts", "state": "ready", "cmd": '"python" "server.py" --port 5800'}


def _room_env(monkeypatch, tmp_path, *, free_mb, running, weights_mb=40000):
    import httpx
    from src import comfyui_workflows as cw, engine_tuner
    monkeypatch.setattr(cw, "_models_dir", lambda: str(tmp_path))
    (tmp_path / "big.gguf").write_bytes(b"x")
    monkeypatch.setattr(cc.os.path, "getsize", lambda p: weights_mb * 1024 * 1024)
    monkeypatch.setattr(engine_tuner, "gpu_free_mb", lambda: free_mb)
    monkeypatch.delenv("AEGIS_COMFY_MAKE_ROOM", raising=False)
    fake = _FakeSwap(running)
    monkeypatch.setattr(httpx, "AsyncClient", fake.client)
    return fake, {"required_files": ["big.gguf"]}


def test_job_need_is_weights_plus_overhead(monkeypatch, tmp_path):
    _fake, spec = _room_env(monkeypatch, tmp_path, free_mb=0, running=[])
    assert cc.job_need_mb(spec) == int(40000 * cc._ROOM_OVERHEAD) + cc._ROOM_MARGIN_MB
    assert cc.job_need_mb(None) is None
    assert cc.job_need_mb({"required_files": []}) is None      # nothing sizeable → no opinion


async def test_make_room_unloads_gpu_chat_models_when_the_job_will_not_fit(monkeypatch, tmp_path):
    """63GB chat model resident + a ~50GB render on a unified-memory box:
    the chat model has to go first (it reloads on the next message)."""
    fake, spec = _room_env(monkeypatch, tmp_path, free_mb=30000, running=[_GPU_CHAT, _CPU_CHAT, _TTS])
    unloaded = await cc.make_room(spec, "comfy-wan2.2-t2v")
    assert unloaded == ["gpt-oss-120b", "chatterbox-tts"]
    # The CPU fallback holds no GPU memory and is what answers chat during
    # the render — it must survive.
    assert "chat-moe-cpu" not in fake.unloaded
    assert all("/api/models/unload/" in url for method, url in fake.calls if method == "POST")


async def test_make_room_leaves_everything_alone_when_the_job_fits(monkeypatch, tmp_path):
    fake, spec = _room_env(monkeypatch, tmp_path, free_mb=60000, running=[_GPU_CHAT])
    assert await cc.make_room(spec, "comfy-z-image-turbo") == []
    assert fake.calls == []                                      # not even a /running probe


@pytest.mark.parametrize("free", [None])
async def test_make_room_does_nothing_without_a_memory_reading(monkeypatch, tmp_path, free):
    fake, spec = _room_env(monkeypatch, tmp_path, free_mb=free, running=[_GPU_CHAT])
    assert await cc.make_room(spec) == []
    assert fake.unloaded == []


async def test_make_room_can_be_switched_off(monkeypatch, tmp_path):
    fake, spec = _room_env(monkeypatch, tmp_path, free_mb=1000, running=[_GPU_CHAT])
    monkeypatch.setenv("AEGIS_COMFY_MAKE_ROOM", "0")
    assert await cc.make_room(spec) == []
    assert fake.calls == []


async def test_make_room_never_blocks_the_job_on_errors(monkeypatch, tmp_path):
    """A dead llama-swap, or one that refuses the unload, must not stop the render."""
    import httpx
    fake, spec = _room_env(monkeypatch, tmp_path, free_mb=1000, running=[_GPU_CHAT])
    fake.unload_status = 500
    assert await cc.make_room(spec) == []                        # refused → reported as not unloaded

    def _boom(*a, **k):
        raise ConnectionError("llama-swap is down")
    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    assert await cc.make_room(spec) == []                        # unreachable → no raise


async def test_image_render_makes_room_before_submitting(monkeypatch):
    order = []

    async def _room(spec, label=""):
        order.append(("room", label, bool(spec)))
        return []
    monkeypatch.setattr(cc, "make_room", _room)
    fake = _FakeComfy(history=lambda pid, n: _DONE)
    _use(monkeypatch, fake)
    real_post = fake.post

    async def _post(url, json=None, **k):
        if url.endswith("/prompt"):
            order.append(("submit",))
        return await real_post(url, json=json, **k)
    fake.post = _post
    out = await cc._render_image_inner("comfy-flux2-dev", "a fox", budget_s=60)
    assert "b64_json" in out
    assert order[0] == ("room", "comfy-flux2-dev", True) and order[1] == ("submit",)
