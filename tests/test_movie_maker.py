"""Movie maker request parsing + queue integration.

parse_clips is the trust boundary: everything it returns gets handed to ffmpeg,
which will happily read any path it's given.
"""

import pytest

from src import job_queue, movie_maker, video_editing
from src.video_editing import VideoEditError


@pytest.fixture
def media(tmp_path, monkeypatch):
    root = tmp_path / "generated_images"
    root.mkdir()
    for n in ("a.webm", "b.webm"):
        (root / n).write_bytes(b"x")
    monkeypatch.setattr(video_editing, "GENERATED_IMAGES_DIR", str(root))
    job_queue._reset_for_tests()
    yield root
    job_queue._reset_for_tests()


def test_parses_plain_names(media):
    clips = movie_maker.parse_clips(["a.webm", "b.webm"])
    assert [c.path.name for c in clips] == ["a.webm", "b.webm"]


def test_preserves_caller_order(media):
    """Order *is* the reorder feature — the frontend sends the arranged list."""
    clips = movie_maker.parse_clips(["b.webm", "a.webm"])
    assert [c.path.name for c in clips] == ["b.webm", "a.webm"]


def test_accepts_the_same_clip_twice(media):
    """Reusing a clip is legitimate (a repeated beat), not an error."""
    assert len(movie_maker.parse_clips(["a.webm", "a.webm"])) == 2


def test_parses_dict_form_with_trims(media):
    clips = movie_maker.parse_clips([{"name": "a.webm", "start": 0.5, "end": 1.5}])
    assert clips[0].start == 0.5 and clips[0].end == 1.5


def test_parses_url_form(media):
    clips = movie_maker.parse_clips([{"url": "/api/generated-image/a.webm"}])
    assert clips[0].path.name == "a.webm"


def test_rejects_traversal(media):
    with pytest.raises(VideoEditError):
        movie_maker.parse_clips(["../../../etc/passwd"])


def test_rejects_empty_list(media):
    with pytest.raises(VideoEditError, match="at least one"):
        movie_maker.parse_clips([])


def test_rejects_too_many(media):
    with pytest.raises(VideoEditError, match="more than"):
        movie_maker.parse_clips(["a.webm"] * (movie_maker.MAX_CLIPS + 1))


def test_rejects_end_before_start(media):
    with pytest.raises(VideoEditError, match="end must be after start"):
        movie_maker.parse_clips([{"name": "a.webm", "start": 2.0, "end": 1.0}])


def test_rejects_negative_trim(media):
    with pytest.raises(VideoEditError, match="cannot be negative"):
        movie_maker.parse_clips([{"name": "a.webm", "start": -1}])


def test_rejects_non_numeric_trim(media):
    with pytest.raises(VideoEditError, match="must be a number"):
        movie_maker.parse_clips([{"name": "a.webm", "start": "soon"}])


def test_rejects_malformed_items(media):
    with pytest.raises(VideoEditError, match="malformed"):
        movie_maker.parse_clips([123])


@pytest.mark.asyncio
async def test_build_validates_before_queueing(media):
    """A bad request must fail at the API, not become a queue entry that dies
    seconds later with the user watching."""
    with pytest.raises(VideoEditError):
        await movie_maker.build(["../../../etc/passwd"])
    assert job_queue.snapshot() == []


@pytest.mark.asyncio
async def test_build_queues_as_non_gpu_work(media, monkeypatch):
    import asyncio

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(movie_maker, "_run", _noop)

    qid = await movie_maker.build(["a.webm", "b.webm"], title="My film")
    await asyncio.sleep(0)
    entry = job_queue.get(qid)
    assert entry["kind"] == "movie"
    assert entry["title"] == "My film"
    assert entry["gpu"] is False           # ffmpeg is CPU work
    assert job_queue.gpu_busy() is None    # must not strand chat on the 3B


@pytest.mark.asyncio
async def test_build_defaults_the_title(media, monkeypatch):
    import asyncio

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(movie_maker, "_run", _noop)
    qid = await movie_maker.build(["a.webm", "b.webm"])
    await asyncio.sleep(0)
    assert "2 clips" in job_queue.get(qid)["title"]
