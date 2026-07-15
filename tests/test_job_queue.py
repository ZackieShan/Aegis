"""Unified job queue — registry behaviour and the GPU-busy signal.

The gpu_busy() tests matter most: chat routing depends on that signal, and if it
returns None while a render is live, chat loads a GPU model and llama-swap evicts
the render mid-job.
"""

import pytest

from src import job_queue


@pytest.fixture(autouse=True)
def _clean():
    job_queue._reset_for_tests()
    yield
    job_queue._reset_for_tests()


def test_add_returns_queued_entry():
    qid = job_queue.add("video", "a cat surfing", owner="zack")
    e = job_queue.get(qid)
    assert e["status"] == "queued"
    assert e["title"] == "a cat surfing"
    assert e["owner"] == "zack"
    assert e["gpu"] is True  # video is a GPU kind


def test_non_gpu_kind_is_not_gpu():
    qid = job_queue.add("recipe", "inbox declutter")
    assert job_queue.get(qid)["gpu"] is False


def test_movie_building_is_not_gpu_work():
    """Stitching clips is ffmpeg/libx264 on the CPU — it must not push chat onto
    the CPU fallback, because it isn't competing for the card."""
    qid = job_queue.add("movie", "my film")
    job_queue.start(qid)
    assert job_queue.get(qid)["gpu"] is False
    assert job_queue.gpu_busy() is None


def test_gpu_flag_can_be_overridden():
    qid = job_queue.add("recipe", "recipe that renders", gpu=True)
    assert job_queue.get(qid)["gpu"] is True


def test_lifecycle_start_update_finish():
    qid = job_queue.add("video", "clip")
    job_queue.start(qid)
    assert job_queue.get(qid)["status"] == "running"
    assert job_queue.get(qid)["started"] is not None

    job_queue.update(qid, progress=0.5, detail="step 5/10")
    e = job_queue.get(qid)
    assert e["progress"] == 0.5
    assert e["detail"] == "step 5/10"

    job_queue.finish(qid, "done", result_url="/media/x.mp4")
    e = job_queue.get(qid)
    assert e["status"] == "done"
    assert e["result_url"] == "/media/x.mp4"
    assert e["progress"] == 1.0  # completion implies full progress
    assert e["finished"] is not None


def test_progress_is_clamped():
    qid = job_queue.add("video", "clip")
    job_queue.update(qid, progress=5.0)
    assert job_queue.get(qid)["progress"] == 1.0
    job_queue.update(qid, progress=-2.0)
    assert job_queue.get(qid)["progress"] == 0.0


def test_update_after_finish_is_ignored():
    """A late poll must not resurrect a finished job — otherwise a stale
    in-flight poller could pin gpu_busy() on forever and strand chat on CPU."""
    qid = job_queue.add("video", "clip")
    job_queue.finish(qid, "cancelled")
    job_queue.update(qid, status="running", progress=0.2)
    e = job_queue.get(qid)
    assert e["status"] == "cancelled"
    assert e["progress"] is None


def test_start_after_finish_is_ignored():
    qid = job_queue.add("video", "clip")
    job_queue.finish(qid, "error", error="boom")
    job_queue.start(qid)
    assert job_queue.get(qid)["status"] == "error"


# ── the signal chat routing depends on ──

def test_gpu_busy_none_when_idle():
    assert job_queue.gpu_busy() is None


def test_gpu_busy_detects_queued_render():
    """Queued counts as busy: the render is about to take the card, and a chat
    request in that window would still evict it."""
    job_queue.add("video", "clip")
    assert job_queue.gpu_busy() is not None


def test_gpu_busy_detects_running_render():
    qid = job_queue.add("image", "a fox")
    job_queue.start(qid)
    busy = job_queue.gpu_busy()
    assert busy is not None
    assert busy["kind"] == "image"


def test_gpu_busy_clears_when_render_finishes():
    qid = job_queue.add("video", "clip")
    job_queue.start(qid)
    job_queue.finish(qid, "done")
    assert job_queue.gpu_busy() is None


def test_gpu_busy_clears_on_error_and_cancel():
    a = job_queue.add("video", "a")
    job_queue.finish(a, "error", error="x")
    assert job_queue.gpu_busy() is None
    b = job_queue.add("video", "b")
    job_queue.cancel(b)
    assert job_queue.gpu_busy() is None


def test_gpu_busy_ignores_non_gpu_work():
    """A recipe run must not strand chat on the CPU model."""
    qid = job_queue.add("recipe", "summarize")
    job_queue.start(qid)
    assert job_queue.gpu_busy() is None


def test_gpu_busy_is_not_owner_scoped_by_default():
    """One physical GPU: another user's render blocks this user's chat too."""
    qid = job_queue.add("video", "clip", owner="someone-else")
    job_queue.start(qid)
    assert job_queue.gpu_busy() is not None


def test_gpu_busy_owner_filter_when_asked():
    qid = job_queue.add("video", "clip", owner="someone-else")
    job_queue.start(qid)
    assert job_queue.gpu_busy(owner="zack") is None
    assert job_queue.gpu_busy(owner="someone-else") is not None


# ── cancel ──

def test_cancel_marks_cancelled_once():
    qid = job_queue.add("video", "clip")
    assert job_queue.cancel(qid) is True
    assert job_queue.get(qid)["status"] == "cancelled"
    assert job_queue.cancel(qid) is False  # already terminal


def test_cancel_unknown_id_is_false():
    assert job_queue.cancel("nope") is False


# ── snapshot ──

def test_snapshot_lists_live_before_done_and_positions_live():
    a = job_queue.add("video", "first")
    b = job_queue.add("video", "second")
    c = job_queue.add("video", "already done")
    job_queue.finish(c, "done")

    rows = job_queue.snapshot()
    live = [r for r in rows if r["status"] not in job_queue.DONE_STATES]
    assert [r["id"] for r in live] == [a, b]      # oldest first == "what's next"
    assert [r["position"] for r in live] == [0, 1]
    assert rows[-1]["id"] == c                     # finished work sinks


def test_snapshot_can_exclude_done():
    qid = job_queue.add("video", "x")
    job_queue.finish(qid, "done")
    assert job_queue.snapshot(include_done=False) == []


def test_snapshot_owner_scoping_hides_other_users():
    job_queue.add("video", "mine", owner="zack")
    job_queue.add("video", "theirs", owner="other")
    titles = {r["title"] for r in job_queue.snapshot(owner="zack")}
    assert titles == {"mine"}


def test_snapshot_includes_unowned_system_work():
    """Scheduler-fired jobs have no owner; they should still be visible."""
    job_queue.add("recipe", "nightly", owner=None)
    titles = {r["title"] for r in job_queue.snapshot(owner="zack")}
    assert "nightly" in titles


def test_clear_finished_leaves_live_work():
    live = job_queue.add("video", "live")
    done = job_queue.add("video", "done")
    job_queue.finish(done, "done")
    assert job_queue.clear_finished() == 1
    assert job_queue.get(live) is not None
    assert job_queue.get(done) is None


def test_prune_keeps_live_work_and_drops_oldest_finished():
    """The cap must never evict live work — a pruned running render would make
    gpu_busy() lie."""
    live = job_queue.add("video", "live one")
    job_queue.start(live)
    for i in range(job_queue._MAX_KEPT + 20):
        q = job_queue.add("image", f"old {i}")
        job_queue.finish(q, "done")
    assert job_queue.get(live) is not None
    assert job_queue.gpu_busy() is not None
    assert len(job_queue._ENTRIES) <= job_queue._MAX_KEPT + 1
