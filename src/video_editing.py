"""Stitch generated clips into one film — the Studio's movie maker.

Local models make short clips (2–10s). A "movie" here is several of them, in an
order you choose, each optionally trimmed, encoded into one file. That's the
whole feature: the clips are generated, so you re-render rather than frame-edit,
and a real NLE would be a worse version of tools that already exist.

Three things make this less trivial than "ffmpeg concat":

* **Clips don't match.** Wan and LTX render at different sizes and framerates,
  and the concat *demuxer* silently requires identical streams. So every clip is
  scaled/padded to one canvas and resampled to one fps through filter_complex,
  and re-encoded. Slower than a stream copy, correct in every case.
* **Audio is ragged.** LTX-2 emits audio; Wan doesn't. Concat needs the same
  stream layout on every segment, so silent clips get a generated silent track
  rather than having audio dropped from the whole film.
* **ffmpeg reads anything.** Paths are confined to the generated-media directory:
  otherwise "make me a movie" is an arbitrary-file-read that encodes the result
  into a video you can download.

Uses the ffmpeg bundled by imageio-ffmpeg — Playwright's stripped build (the only
other one usually on disk) has no libx264 and no filters.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.constants import GENERATED_IMAGES_DIR

logger = logging.getLogger(__name__)

# Encoding defaults. yuv420p because anything else fails to play in browsers.
_VCODEC = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20"]
_ACODEC = ["-c:a", "aac", "-b:a", "128k"]
_AUDIO_RATE = 48000

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?:\s*Video:.*", re.I)
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?:\s*Audio:", re.I)
_SIZE_RE = re.compile(r"(?<![\d])(\d{2,5})x(\d{2,5})(?![\d])")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:fps|tbr)")


class VideoEditError(RuntimeError):
    """Raised for anything the caller should show the user verbatim."""


@dataclass
class Clip:
    path: Path
    start: Optional[float] = None   # seconds into the source
    end: Optional[float] = None     # seconds into the source


def ffmpeg_exe() -> str:
    """Path to the bundled ffmpeg, or a clear error naming the fix."""
    try:
        import imageio_ffmpeg
    except ImportError as e:  # pragma: no cover - depends on install state
        raise VideoEditError(
            "ffmpeg is not available — install it with: pip install imageio-ffmpeg"
        ) from e
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # pragma: no cover
        raise VideoEditError(f"Could not locate the bundled ffmpeg: {e}") from e


def _run(args: List[str], timeout: float = 900.0) -> subprocess.CompletedProcess:
    """Run ffmpeg with an argument list — never a shell string, so a filename can
    never become a command."""
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        raise VideoEditError(f"ffmpeg timed out after {int(timeout)}s") from e


def media_root() -> Path:
    return Path(GENERATED_IMAGES_DIR).resolve()


def safe_media_path(name: str) -> Path:
    """Resolve a caller-supplied clip reference inside the generated-media dir.

    Rejects traversal and symlink escapes. ffmpeg will read whatever it's given
    and encode it into the output, so an unconfined path here would let a movie
    request exfiltrate any file the server can read.
    """
    raw = str(name or "").strip()
    if not raw:
        raise VideoEditError("Clip path is required")
    # Accept a bare filename or a /api/generated-image/<name> URL, never a path.
    raw = raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if not raw or raw in (".", ".."):
        raise VideoEditError(f"Invalid clip name: {name!r}")

    root = media_root()
    target = (root / raw).resolve()
    # resolve() follows symlinks, so this also catches a link pointing outside.
    if target != root and root not in target.parents:
        raise VideoEditError(f"Clip is outside the media directory: {raw!r}")
    if not target.is_file():
        raise VideoEditError(f"Clip not found: {raw!r}")
    return target


def probe(path: os.PathLike | str) -> Dict[str, Any]:
    """Duration/size/fps/audio for one clip.

    Parses `ffmpeg -i` stderr because imageio-ffmpeg ships ffmpeg only — there is
    no ffprobe to ask. ffmpeg exits non-zero here (no output file specified);
    that's expected, the stream info is still on stderr.
    """
    p = Path(path)
    proc = _run([ffmpeg_exe(), "-hide_banner", "-i", str(p)], timeout=60.0)
    text = (proc.stderr or "")
    if "Invalid data found" in text or "No such file" in text:
        raise VideoEditError(f"Not a readable video: {p.name}")

    duration = 0.0
    m = _DURATION_RE.search(text)
    if m:
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    width = height = 0
    fps = 0.0
    vm = _VIDEO_RE.search(text)
    if vm:
        line = vm.group(0)
        sm = _SIZE_RE.search(line)
        if sm:
            width, height = int(sm.group(1)), int(sm.group(2))
        fm = _FPS_RE.search(line)
        if fm:
            fps = float(fm.group(1))
    if not vm:
        raise VideoEditError(f"{p.name} has no video stream")

    return {
        "path": str(p),
        "name": p.name,
        "duration": round(duration, 3),
        "width": width,
        "height": height,
        "fps": fps or 24.0,
        "has_audio": bool(_AUDIO_RE.search(text)),
    }


def _even(n: int) -> int:
    """libx264 needs even dimensions."""
    return n if n % 2 == 0 else n + 1


def plan_canvas(infos: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pick one canvas + fps for the film.

    Largest width/height and the highest fps: upscaling a small clip is kinder
    than cropping a large one, and dropping frames beats inventing them.
    """
    if not infos:
        raise VideoEditError("No clips to build from")
    width = _even(max(i.get("width") or 0 for i in infos) or 512)
    height = _even(max(i.get("height") or 0 for i in infos) or 512)
    fps = max((i.get("fps") or 0) for i in infos) or 24.0
    return {"width": width, "height": height, "fps": round(float(fps), 3),
            "has_audio": any(i.get("has_audio") for i in infos)}


def _clip_duration(info: Dict[str, Any], clip: Clip) -> float:
    start = max(0.0, float(clip.start or 0.0))
    end = float(clip.end) if clip.end is not None else float(info["duration"])
    end = min(end, float(info["duration"])) if info["duration"] else end
    return max(0.0, end - start)


def build_filtergraph(
    clips: List[Clip],
    infos: List[Dict[str, Any]],
    canvas: Dict[str, Any],
) -> str:
    """The filter_complex that trims, normalises and concatenates every clip.

    Split out from build_movie so it can be asserted on without spending a
    minute of encoding per test.
    """
    w, h, fps = canvas["width"], canvas["height"], canvas["fps"]
    want_audio = bool(canvas.get("has_audio"))
    parts: List[str] = []
    labels: List[str] = []

    # Silent clips borrow audio from extra anullsrc inputs appended after the
    # real ones; this tracks which input index each will get.
    silent_input = len(clips)

    for i, (clip, info) in enumerate(zip(clips, infos)):
        trim = ""
        if clip.start is not None or clip.end is not None:
            bits = []
            if clip.start is not None:
                bits.append(f"start={float(clip.start):.3f}")
            if clip.end is not None:
                bits.append(f"end={float(clip.end):.3f}")
            trim = "trim=" + ":".join(bits) + ","
        parts.append(
            f"[{i}:v]{trim}setpts=PTS-STARTPTS,"
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={fps},setsar=1[v{i}]"
        )
        labels.append(f"[v{i}]")

        if want_audio:
            if info.get("has_audio"):
                atrim = ""
                if clip.start is not None or clip.end is not None:
                    bits = []
                    if clip.start is not None:
                        bits.append(f"start={float(clip.start):.3f}")
                    if clip.end is not None:
                        bits.append(f"end={float(clip.end):.3f}")
                    atrim = "atrim=" + ":".join(bits) + ","
                parts.append(
                    f"[{i}:a]{atrim}asetpts=PTS-STARTPTS,"
                    f"aresample={_AUDIO_RATE}[a{i}]"
                )
            else:
                # A silent clip still needs an audio segment: concat demands the
                # same stream layout from every input, so dropping the track
                # here would drop audio from the entire film.
                dur = _clip_duration(info, clip)
                parts.append(
                    f"[{silent_input}:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[a{i}]"
                )
                silent_input += 1
            labels.append(f"[a{i}]")

    n = len(clips)
    if want_audio:
        parts.append("".join(labels) + f"concat=n={n}:v=1:a=1[outv][outa]")
    else:
        parts.append("".join(labels) + f"concat=n={n}:v=1:a=0[outv]")
    return ";".join(parts)


def build_movie(
    clips: List[Clip],
    out_path: os.PathLike | str,
    canvas: Optional[Dict[str, Any]] = None,
    timeout: float = 1800.0,
) -> Dict[str, Any]:
    """Render `clips` into one file at `out_path`. Returns a probe of the result."""
    if not clips:
        raise VideoEditError("Pick at least one clip")
    if len(clips) > 50:
        raise VideoEditError("That's more than 50 clips — trim the list down")

    infos = [probe(c.path) for c in clips]
    for info, clip in zip(infos, clips):
        if _clip_duration(info, clip) <= 0:
            raise VideoEditError(
                f"{info['name']}: the trim leaves nothing (start must be before end)"
            )
    canvas = canvas or plan_canvas(infos)

    args: List[str] = [ffmpeg_exe(), "-hide_banner", "-y"]
    for c in clips:
        args += ["-i", str(c.path)]
    if canvas.get("has_audio"):
        for info, clip in zip(infos, clips):
            if not info.get("has_audio"):
                args += ["-f", "lavfi", "-t", f"{_clip_duration(info, clip):.3f}",
                         "-i", f"anullsrc=channel_layout=stereo:sample_rate={_AUDIO_RATE}"]

    args += ["-filter_complex", build_filtergraph(clips, infos, canvas)]
    args += ["-map", "[outv]"]
    if canvas.get("has_audio"):
        args += ["-map", "[outa]"] + _ACODEC
    args += _VCODEC
    args += ["-movflags", "+faststart", str(out_path)]

    proc = _run(args, timeout=timeout)
    if proc.returncode != 0 or not Path(out_path).exists():
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-6:])
        raise VideoEditError(f"ffmpeg failed to build the movie:\n{tail}")

    out = probe(out_path)
    out["clips"] = len(clips)
    return out
