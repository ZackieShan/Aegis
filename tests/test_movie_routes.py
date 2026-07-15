"""Movie + queue HTTP surface.

Covers the bits that only break at the route layer: auth wiring, error mapping
(a bad clip list should be a 400, not a 500), and cross-user cancel.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import movie_routes
from src import job_queue, video_editing


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = tmp_path / "generated_images"
    root.mkdir()
    (root / "a.webm").write_bytes(b"x")
    monkeypatch.setattr(video_editing, "GENERATED_IMAGES_DIR", str(root))
    monkeypatch.setattr(movie_routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(movie_routes, "require_privilege", lambda request, priv: "alice")
    job_queue._reset_for_tests()

    app = FastAPI()
    app.include_router(movie_routes.setup_movie_routes())
    yield TestClient(app)
    job_queue._reset_for_tests()


# ── queue ──

def test_queue_empty(client):
    d = client.get("/api/queue").json()
    assert d["jobs"] == []
    assert d["gpu_busy"] is False
    assert d["gpu_job"] is None


def test_queue_reports_gpu_busy_during_a_render(client):
    qid = job_queue.add("video", "a cat", owner="alice")
    job_queue.start(qid)
    d = client.get("/api/queue").json()
    assert d["gpu_busy"] is True
    assert d["gpu_job"]["title"] == "a cat"
    assert d["jobs"][0]["position"] == 0


def test_queue_movie_does_not_report_gpu_busy(client):
    """Stitching is CPU work — it must not read as GPU contention."""
    qid = job_queue.add("movie", "my film", owner="alice")
    job_queue.start(qid)
    d = client.get("/api/queue").json()
    assert d["gpu_busy"] is False


def test_cancel_marks_cancelled(client):
    qid = job_queue.add("movie", "film", owner="alice")
    assert client.post(f"/api/queue/{qid}/cancel").json()["ok"] is True
    assert job_queue.get(qid)["status"] == "cancelled"


def test_cancel_unknown_is_404(client):
    assert client.post("/api/queue/nope/cancel").status_code == 404


def test_cannot_cancel_another_users_job(client):
    qid = job_queue.add("video", "theirs", owner="bob")
    assert client.post(f"/api/queue/{qid}/cancel").status_code == 403
    assert job_queue.get(qid)["status"] == "queued"


def test_cancel_finished_reports_not_ok(client):
    qid = job_queue.add("movie", "film", owner="alice")
    job_queue.finish(qid, "done")
    assert client.post(f"/api/queue/{qid}/cancel").json()["ok"] is False


def test_clear_removes_only_finished(client):
    live = job_queue.add("movie", "live", owner="alice")
    done = job_queue.add("movie", "done", owner="alice")
    job_queue.finish(done, "done")
    assert client.post("/api/queue/clear").json()["cleared"] == 1
    assert job_queue.get(live) is not None


# ── movie build ──

def test_build_rejects_traversal_with_400(client):
    """A hostile clip name must be a clean 400, never a 500 or a read."""
    r = client.post("/api/movie/build", json={"clips": ["../../../etc/passwd"]})
    assert r.status_code == 400
    assert job_queue.snapshot() == []


def test_build_rejects_empty_with_400(client):
    assert client.post("/api/movie/build", json={"clips": []}).status_code == 400


def test_build_rejects_bad_trim_with_400(client):
    r = client.post("/api/movie/build",
                    json={"clips": [{"name": "a.webm", "start": 5, "end": 1}]})
    assert r.status_code == 400


def test_build_queues_and_returns_job_id(client, monkeypatch):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr("src.movie_maker._run", _noop)

    d = client.post("/api/movie/build",
                    json={"clips": ["a.webm"], "title": "Film"}).json()
    assert d["status"] == "queued"
    entry = job_queue.get(d["job_id"])
    assert entry["kind"] == "movie" and entry["owner"] == "alice"


def test_probe_rejects_traversal(client):
    assert client.post("/api/movie/probe", json={"name": "../../etc/passwd"}).status_code == 400
