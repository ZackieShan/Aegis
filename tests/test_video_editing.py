"""Movie maker — path confinement, canvas planning, and the concat filtergraph.

The confinement tests are the security-critical ones: ffmpeg reads whatever path
it's handed and encodes it into the output file, so an unconfined clip reference
turns "build me a movie" into arbitrary file read with a downloadable result.
"""

from pathlib import Path

import pytest

from src import video_editing as ve
from src.video_editing import Clip, VideoEditError

REAL_CLIPS = Path("data/generated_images")


def _info(width=832, height=480, fps=16.0, duration=2.0, has_audio=False, name="c.webm"):
    return {"path": name, "name": name, "duration": duration, "width": width,
            "height": height, "fps": fps, "has_audio": has_audio}


# ── path confinement ──

@pytest.fixture
def media(tmp_path, monkeypatch):
    root = tmp_path / "generated_images"
    root.mkdir()
    (root / "clip.webm").write_bytes(b"x")
    monkeypatch.setattr(ve, "GENERATED_IMAGES_DIR", str(root))
    return root


def test_safe_path_accepts_plain_filename(media):
    assert ve.safe_media_path("clip.webm") == (media / "clip.webm").resolve()


def test_safe_path_accepts_api_url_form(media):
    """The frontend hands back /api/generated-image/<name> URLs."""
    assert ve.safe_media_path("/api/generated-image/clip.webm").name == "clip.webm"


@pytest.mark.parametrize("evil", [
    "../../../../Windows/System32/drivers/etc/hosts",
    "..\\..\\..\\secrets.env",
    "/etc/passwd",
    "C:\\Windows\\win.ini",
    "....//....//etc/passwd",
])
def test_safe_path_rejects_traversal(media, evil):
    """Every one of these must fail — either stripped to a basename that doesn't
    exist, or rejected outright. None may resolve outside the media dir."""
    with pytest.raises(VideoEditError):
        ve.safe_media_path(evil)


@pytest.mark.parametrize("bad", ["", "   ", ".", ".."])
def test_safe_path_rejects_empty_and_dots(media, bad):
    with pytest.raises(VideoEditError):
        ve.safe_media_path(bad)


def test_safe_path_rejects_missing_file(media):
    with pytest.raises(VideoEditError, match="not found"):
        ve.safe_media_path("nope.webm")


def test_safe_path_rejects_symlink_escape(media, tmp_path):
    """A symlink inside the media dir must not become a way out of it."""
    secret = tmp_path / "secret.txt"
    secret.write_text("classified")
    link = media / "innocent.webm"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this machine")
    with pytest.raises(VideoEditError, match="outside the media directory"):
        ve.safe_media_path("innocent.webm")


# ── canvas planning ──

def test_canvas_takes_largest_size_and_highest_fps():
    """Upscaling a small clip beats cropping a big one; dropping frames beats
    inventing them."""
    c = ve.plan_canvas([_info(512, 288, 16.0), _info(832, 480, 24.0)])
    assert (c["width"], c["height"], c["fps"]) == (832, 480, 24.0)


def test_canvas_forces_even_dimensions():
    """libx264 rejects odd dimensions."""
    c = ve.plan_canvas([_info(831, 479)])
    assert c["width"] % 2 == 0 and c["height"] % 2 == 0


def test_canvas_has_audio_if_any_clip_does():
    assert ve.plan_canvas([_info(has_audio=False), _info(has_audio=True)])["has_audio"] is True
    assert ve.plan_canvas([_info(has_audio=False), _info(has_audio=False)])["has_audio"] is False


def test_canvas_rejects_empty():
    with pytest.raises(VideoEditError):
        ve.plan_canvas([])


# ── filtergraph ──

def test_graph_normalises_every_clip_to_the_canvas():
    """Mismatched clips are why we re-encode instead of stream-copying."""
    clips = [Clip("a.webm"), Clip("b.webm")]
    infos = [_info(512, 288, 16.0), _info(832, 480, 24.0)]
    g = ve.build_filtergraph(clips, infos, ve.plan_canvas(infos))
    assert g.count("scale=832:480") == 2
    assert g.count("fps=24.0") == 2
    assert "concat=n=2:v=1:a=0" in g


def test_graph_emits_trim_only_when_asked():
    infos = [_info(), _info()]
    g = ve.build_filtergraph([Clip("a"), Clip("b")], infos, ve.plan_canvas(infos))
    assert "trim=" not in g

    g2 = ve.build_filtergraph(
        [Clip("a", start=0.5, end=1.5), Clip("b")], infos, ve.plan_canvas(infos)
    )
    assert "trim=start=0.500:end=1.500" in g2


def test_graph_gives_silent_clips_a_generated_track():
    """Concat needs identical stream layouts. Without the silence, mixing an LTX
    clip (audio) with a Wan clip (none) would drop audio from the whole film."""
    clips = [Clip("loud.webm"), Clip("silent.webm")]
    infos = [_info(has_audio=True, name="loud.webm"), _info(has_audio=False, name="silent.webm")]
    g = ve.build_filtergraph(clips, infos, ve.plan_canvas(infos))
    assert "[0:a]" in g                 # real audio from clip 0
    assert "[2:a]atrim=0:2.000" in g    # silence input appended after both clips
    assert "concat=n=2:v=1:a=1[outv][outa]" in g


def test_graph_skips_audio_entirely_when_no_clip_has_it():
    infos = [_info(has_audio=False), _info(has_audio=False)]
    g = ve.build_filtergraph([Clip("a"), Clip("b")], infos, ve.plan_canvas(infos))
    assert ":a]" not in g
    assert "concat=n=2:v=1:a=0[outv]" in g


def test_graph_trims_the_generated_silence_to_the_trimmed_length():
    """Silence must match the *trimmed* clip, not the source, or audio and video
    drift apart from that segment on."""
    clips = [Clip("loud.webm"), Clip("silent.webm", start=0.5, end=1.0)]
    infos = [_info(has_audio=True), _info(has_audio=False, duration=2.0)]
    g = ve.build_filtergraph(clips, infos, ve.plan_canvas(infos))
    assert "atrim=0:0.500" in g  # 1.0 - 0.5, not 2.0


def test_clip_duration_clamps_end_to_source_length():
    assert ve._clip_duration(_info(duration=2.0), Clip("a", end=99.0)) == 2.0
    assert ve._clip_duration(_info(duration=2.0), Clip("a", start=0.5)) == 1.5


# ── guards ──

def test_build_movie_rejects_empty_and_oversized_lists(tmp_path):
    with pytest.raises(VideoEditError, match="at least one"):
        ve.build_movie([], tmp_path / "out.mp4")
    with pytest.raises(VideoEditError, match="50 clips"):
        ve.build_movie([Clip("x")] * 51, tmp_path / "out.mp4")


# ── against real generated clips ──

def _real():
    if not REAL_CLIPS.is_dir():
        return []
    return sorted(REAL_CLIPS.glob("*.webm"))[:2]


@pytest.mark.skipif(not _real(), reason="no generated clips on this machine")
def test_probe_reads_a_real_clip():
    info = ve.probe(_real()[0])
    assert info["duration"] > 0
    assert info["width"] > 0 and info["height"] > 0
    assert info["fps"] > 0


@pytest.mark.skipif(len(_real()) < 2, reason="need two generated clips")
def test_build_a_real_movie_end_to_end(tmp_path):
    a, b = _real()[0], _real()[1]
    want = ve.probe(a)["duration"] + ve.probe(b)["duration"]
    out = tmp_path / "movie.mp4"
    res = ve.build_movie([Clip(a), Clip(b)], out)
    assert out.exists() and out.stat().st_size > 0
    assert res["clips"] == 2
    assert abs(res["duration"] - want) < 0.5  # concatenated, not overlaid


@pytest.mark.skipif(not _real(), reason="no generated clips on this machine")
def test_trim_actually_shortens_the_result(tmp_path):
    a = _real()[0]
    out = tmp_path / "trimmed.mp4"
    res = ve.build_movie([Clip(a, start=0.0, end=1.0)], out)
    assert res["duration"] < ve.probe(a)["duration"]
