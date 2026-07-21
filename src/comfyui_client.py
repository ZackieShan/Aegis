"""ComfyUI engine client — a second local serve method beside llama-swap.

ComfyUI runs as its own process (engine/comfyui, default 127.0.0.1:8188) and
is driven entirely over its local HTTP API: POST /prompt queues an API-format
workflow graph, GET /history/{id} reports completion + output files, and
GET /view streams the rendered bytes. After each job POST /free releases the
models ComfyUI is holding so VRAM goes back to llama-swap — the two engines
share one 24GB GPU and must not both hold weights at idle.

Video jobs register into src.video_generation's job registry with the same
shape sd-server jobs use, so /api/video/status, cancel and every UI poller
work identically regardless of which engine rendered the clip.
"""

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from src import job_queue

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 3.0
_JOB_DEADLINE_S = 1800
# VibeVoice narration can legitimately run much longer than a clip render.
_NARRATE_DEADLINE_S = 5400


def base_url() -> str:
    return (os.getenv("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")


def input_dir() -> str:
    """ComfyUI's input directory — where LoadImage/LoadVideo/LoadAudio read
    from (same resolution rule as routes/audio_routes)."""
    env = os.getenv("COMFYUI_INPUT_DIR")
    if env:
        return env
    from src.engine_tuner import _engine_dir
    return os.path.join(_engine_dir(), "comfyui", "ComfyUI", "input")


def stage_input_bytes(data: bytes, ext: str) -> str:
    """Write bytes into ComfyUI's input dir under a fresh aegis_in_* name and
    return the bare filename (what Load* nodes take)."""
    d = input_dir()
    os.makedirs(d, exist_ok=True)
    name = f"aegis_in_{uuid.uuid4().hex[:12]}.{ext.lstrip('.')}"
    with open(os.path.join(d, name), "wb") as f:
        f.write(data)
    return name


def stage_input_file(path: str) -> str:
    """Copy an existing file into ComfyUI's input dir; returns the staged name."""
    import shutil
    d = input_dir()
    os.makedirs(d, exist_ok=True)
    ext = os.path.basename(path).rsplit(".", 1)[-1].lower()
    name = f"aegis_in_{uuid.uuid4().hex[:12]}.{ext}"
    shutil.copyfile(path, os.path.join(d, name))
    return name


def is_up(timeout: float = 2.0) -> bool:
    import httpx
    try:
        return httpx.get(f"{base_url()}/system_stats", timeout=timeout).status_code == 200
    except Exception:
        return False


async def free_vram() -> None:
    """Ask ComfyUI to drop loaded models + cached memory (best-effort)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            await client.post(f"{base_url()}/free", json={"unload_models": True, "free_memory": True})
    except Exception as e:
        logger.debug("comfyui /free failed: %s", e)


async def start_video_job(
    prompt: str,
    model: str,
    owner: Optional[str] = None,
    session_id: Optional[str] = None,
    negative_prompt: str = "",
    width: int = 640,
    height: int = 640,
    video_frames: int = 33,
    fps: int = 16,
    seed: int = -1,
    init_image_b64: Optional[str] = None,
) -> str:
    """Queue a ComfyUI video workflow and return an Aegis job id (same
    registry as sd-server jobs). Raises ValueError when the submit fails.
    `init_image_b64` (image-to-video workflows) is staged into ComfyUI's
    input dir and handed to the builder as a LoadImage filename."""
    import httpx
    from src import comfyui_workflows, video_generation

    if seed is None or seed < 0:
        seed = uuid.uuid4().int % (2**31 - 1)
    params: Dict[str, Any] = dict(
        prompt=prompt,
        negative_prompt=negative_prompt or "",
        width=int(width), height=int(height),
        video_frames=int(video_frames), fps=int(fps),
        seed=int(seed),
    )
    if init_image_b64:
        import base64
        params["init_image"] = stage_input_bytes(base64.b64decode(init_image_b64), "png")
    graph = comfyui_workflows.build_video_graph(model, **params)
    if graph is None:
        raise ValueError(f"Unknown ComfyUI workflow '{model}'")

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)) as client:
        r = await client.post(f"{base_url()}/prompt", json={"prompt": graph, "client_id": "aegis"})
        if r.status_code != 200:
            raise ValueError(f"ComfyUI submit failed ({r.status_code}): {r.text[:300]}")
        envelope = r.json()
    remote_id = str(envelope.get("prompt_id") or "")
    if not remote_id:
        raise ValueError(f"ComfyUI returned no prompt id: {str(envelope)[:200]}")

    job_id = uuid.uuid4().hex[:12]
    # ComfyUI builds its job here rather than via submit_video_job, so it must
    # register with the queue itself — a ComfyUI render that skipped this would
    # be invisible to gpu_busy(), and chat would evict it mid-render.
    queue_id = job_queue.add(
        "video",
        prompt or "video",
        owner=owner,
        session_id=session_id,
        external_id=job_id,
        meta={"model": model, "engine": "comfyui", "width": int(width), "height": int(height)},
    )
    video_generation._JOBS[job_id] = {
        "id": job_id,
        "queue_id": queue_id,
        "status": "queued",
        "prompt": prompt,
        "model": model,
        "owner": owner,
        "session_id": session_id,
        "width": int(width),
        "height": int(height),
        "video_frames": int(video_frames),
        "fps": int(fps),
        "created": time.time(),
        "remote_id": remote_id,
        "engine": "comfyui",
        "video_url": None,
        "image_id": None,
        "error": None,
        "progress": None,
    }
    video_generation._prune_jobs()
    asyncio.create_task(_poll_job(job_id, remote_id))
    logger.info("ComfyUI video job %s queued (workflow=%s, %sx%s, %s frames)",
                job_id, model, width, height, video_frames)
    return job_id


async def start_song_job(
    tags: str,
    lyrics: str = "",
    model: str = "comfy-acestep1.5-song",
    owner: Optional[str] = None,
    session_id: Optional[str] = None,
    seconds: float = 60.0,
    seed: int = -1,
    bpm: int = 120,
    language: str = "en",
    reference_audio: str = "",
) -> str:
    """Queue an ACE-Step song render and return an Aegis job id (same
    registry as video/image-edit jobs — /api/video/status polls it, the
    unified queue shows it, the GPU guard sees it). `reference_audio` names
    a file already inside ComfyUI's input dir → cover mode."""
    import httpx
    from src import comfyui_workflows, video_generation

    if seed is None or seed < 0:
        seed = uuid.uuid4().int % (2**31 - 1)
    graph = comfyui_workflows.build_audio_graph(
        model,
        tags=tags, lyrics=lyrics or "",
        seconds=float(seconds), seed=int(seed),
        bpm=int(bpm), language=language or "en",
        reference_audio=reference_audio or "",
    )
    if graph is None:
        raise ValueError(f"Unknown ComfyUI audio workflow '{model}'")

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)) as client:
        r = await client.post(f"{base_url()}/prompt", json={"prompt": graph, "client_id": "aegis"})
        if r.status_code != 200:
            raise ValueError(f"ComfyUI submit failed ({r.status_code}): {r.text[:300]}")
        envelope = r.json()
    remote_id = str(envelope.get("prompt_id") or "")
    if not remote_id:
        raise ValueError(f"ComfyUI returned no prompt id: {str(envelope)[:200]}")

    job_id = uuid.uuid4().hex[:12]
    queue_id = job_queue.add(
        "audio",
        tags or "song",
        owner=owner,
        session_id=session_id,
        external_id=job_id,
        meta={"model": model, "engine": "comfyui", "seconds": float(seconds)},
    )
    video_generation._JOBS[job_id] = {
        "id": job_id,
        "kind": "audio",
        "queue_id": queue_id,
        "status": "queued",
        "prompt": tags,
        "lyrics": bool(lyrics),
        "has_reference": bool(reference_audio),
        "model": model,
        "owner": owner,
        "session_id": session_id,
        "seconds": float(seconds),
        "created": time.time(),
        "remote_id": remote_id,
        "engine": "comfyui",
        "video_url": None,
        "image_id": None,
        "error": None,
        "progress": None,
    }
    video_generation._prune_jobs()
    asyncio.create_task(_poll_job(job_id, remote_id))
    logger.info("ComfyUI song job %s queued (%.0fs, lyrics=%s)", job_id, seconds, bool(lyrics))
    return job_id


async def start_narrate_job(
    script: str,
    model: str = "comfy-vibevoice-narrate",
    owner: Optional[str] = None,
    session_id: Optional[str] = None,
    seed: int = -1,
    speaker_voice_paths: Optional[Dict[int, str]] = None,
) -> str:
    """Queue a VibeVoice long-form narration and return an Aegis job id (same
    registry as song/video jobs). `script` uses "[N]: line" speaker labels;
    `speaker_voice_paths` maps speaker number → an ABSOLUTE wav/mp3 path (a
    data/voices clone sample) that gets staged into ComfyUI's input dir here —
    that speaker is voice-cloned, unmapped speakers get synthetic voices.
    Narration runs on a longer leash than clips: a 4-bit VibeVoice pass emits
    minutes of audio per wall-clock minute, so a long script can legitimately
    outlive the default 30-minute poll deadline."""
    import httpx
    from src import comfyui_workflows, video_generation

    if seed is None or seed < 0:
        seed = uuid.uuid4().int % (2**31 - 1)
    staged = {int(n): stage_input_file(p) for n, p in (speaker_voice_paths or {}).items()}
    graph = comfyui_workflows.build_audio_graph(
        model, script=script, speaker_voices=staged, seed=int(seed),
    )
    if graph is None:
        raise ValueError(f"Unknown ComfyUI audio workflow '{model}'")

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)) as client:
        r = await client.post(f"{base_url()}/prompt", json={"prompt": graph, "client_id": "aegis"})
        if r.status_code != 200:
            raise ValueError(f"ComfyUI submit failed ({r.status_code}): {r.text[:300]}")
        envelope = r.json()
    remote_id = str(envelope.get("prompt_id") or "")
    if not remote_id:
        raise ValueError(f"ComfyUI returned no prompt id: {str(envelope)[:200]}")

    title = " ".join(script.split())[:120]
    job_id = uuid.uuid4().hex[:12]
    queue_id = job_queue.add(
        "audio",
        f"Narration: {title}" if title else "Narration",
        owner=owner,
        session_id=session_id,
        external_id=job_id,
        meta={"model": model, "engine": "comfyui", "speakers": sorted(staged)},
    )
    video_generation._JOBS[job_id] = {
        "id": job_id,
        "kind": "audio",
        "queue_id": queue_id,
        "status": "queued",
        "prompt": title,
        "model": model,
        "owner": owner,
        "session_id": session_id,
        "cloned_speakers": sorted(staged),
        "created": time.time(),
        "remote_id": remote_id,
        "engine": "comfyui",
        "video_url": None,
        "image_id": None,
        "error": None,
        "progress": None,
    }
    video_generation._prune_jobs()
    asyncio.create_task(_poll_job(job_id, remote_id, deadline_s=_NARRATE_DEADLINE_S))
    logger.info("ComfyUI narration job %s queued (%d chars, %d cloned voices)",
                job_id, len(script), len(staged))
    return job_id


async def start_video_op_job(
    op: str,
    source_path: str,
    owner: Optional[str] = None,
    session_id: Optional[str] = None,
    prompt: str = "",
    seed: int = -1,
    **op_params: Any,
) -> str:
    """Queue a video post-op (foley / upscale / interpolate) over an existing
    clip. The source file is staged into ComfyUI's input dir; the finished
    clip lands in the gallery like any other video job."""
    import httpx
    from src import comfyui_workflows, video_generation

    if seed is None or seed < 0:
        seed = uuid.uuid4().int % (2**31 - 1)
    staged = stage_input_file(source_path)
    params: Dict[str, Any] = dict(video_file=staged, seed=int(seed), **op_params)
    if op in ("foley", "foley-hq"):
        params["prompt"] = prompt or ""
    graph = comfyui_workflows.build_video_op_graph(op, **params)
    if graph is None:
        raise ValueError(f"Unknown video op '{op}'")

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)) as client:
        r = await client.post(f"{base_url()}/prompt", json={"prompt": graph, "client_id": "aegis"})
        if r.status_code != 200:
            raise ValueError(f"ComfyUI submit failed ({r.status_code}): {r.text[:300]}")
        envelope = r.json()
    remote_id = str(envelope.get("prompt_id") or "")
    if not remote_id:
        raise ValueError(f"ComfyUI returned no prompt id: {str(envelope)[:200]}")

    label = comfyui_workflows.COMFY_VIDEO_OPS.get(op, {}).get("label", op)
    job_id = uuid.uuid4().hex[:12]
    queue_id = job_queue.add(
        "video",
        f"{label}: {prompt or os.path.basename(source_path)}",
        owner=owner,
        session_id=session_id,
        external_id=job_id,
        meta={"model": f"comfy-op-{op}", "engine": "comfyui", "op": op},
    )
    video_generation._JOBS[job_id] = {
        "id": job_id,
        "queue_id": queue_id,
        "status": "queued",
        "prompt": f"{label}",
        "model": f"comfy-op-{op}",
        "owner": owner,
        "session_id": session_id,
        "created": time.time(),
        "remote_id": remote_id,
        "engine": "comfyui",
        "video_url": None,
        "image_id": None,
        "error": None,
        "progress": None,
    }
    video_generation._prune_jobs()
    asyncio.create_task(_poll_job(job_id, remote_id))
    logger.info("ComfyUI video op %s queued (%s on %s)", job_id, op, os.path.basename(source_path))
    return job_id


async def start_image_op_job(
    op: str,
    source_path: str,
    owner: Optional[str] = None,
    session_id: Optional[str] = None,
    seed: int = -1,
    **op_params: Any,
) -> str:
    """Queue an image post-op (upscale / restore) over an existing still.
    Same job registry as everything else; the result lands in the gallery."""
    import httpx
    from src import comfyui_workflows, video_generation

    if seed is None or seed < 0:
        seed = uuid.uuid4().int % (2**31 - 1)
    staged = stage_input_file(source_path)
    graph = comfyui_workflows.build_image_op_graph(op, image_file=staged, seed=int(seed), **op_params)
    if graph is None:
        raise ValueError(f"Unknown image op '{op}'")

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)) as client:
        r = await client.post(f"{base_url()}/prompt", json={"prompt": graph, "client_id": "aegis"})
        if r.status_code != 200:
            raise ValueError(f"ComfyUI submit failed ({r.status_code}): {r.text[:300]}")
        envelope = r.json()
    remote_id = str(envelope.get("prompt_id") or "")
    if not remote_id:
        raise ValueError(f"ComfyUI returned no prompt id: {str(envelope)[:200]}")

    label = comfyui_workflows.COMFY_IMAGE_OPS.get(op, {}).get("label", op)
    job_id = uuid.uuid4().hex[:12]
    queue_id = job_queue.add(
        "image",
        f"{label}: {os.path.basename(source_path)}",
        owner=owner,
        session_id=session_id,
        external_id=job_id,
        meta={"model": f"comfy-op-{op}", "engine": "comfyui", "op": op},
    )
    video_generation._JOBS[job_id] = {
        "id": job_id,
        "kind": "image",
        "queue_id": queue_id,
        "status": "queued",
        "prompt": label,
        "model": f"comfy-op-{op}",
        "owner": owner,
        "session_id": session_id,
        "created": time.time(),
        "remote_id": remote_id,
        "engine": "comfyui",
        "video_url": None,
        "image_id": None,
        "error": None,
        "progress": None,
    }
    video_generation._prune_jobs()
    asyncio.create_task(_poll_job(job_id, remote_id))
    logger.info("ComfyUI image op %s queued (%s on %s)", job_id, op, os.path.basename(source_path))
    return job_id


async def _poll_job(job_id: str, remote_id: str, deadline_s: float = _JOB_DEADLINE_S) -> None:
    import httpx
    from src import video_generation

    job = video_generation._JOBS.get(job_id)
    if not job:
        return
    deadline = time.time() + deadline_s
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)) as client:
            while time.time() < deadline:
                if job.get("cancel_requested") or job.get("status") == "canceled":
                    job["status"] = "canceled"
                    try:  # interrupt whatever is executing (best-effort)
                        await client.post(f"{base_url()}/interrupt")
                    except Exception:
                        pass
                    await free_vram()
                    return
                try:
                    r = await client.get(f"{base_url()}/history/{remote_id}")
                except Exception as e:
                    job["error"] = f"Lost contact with ComfyUI: {e}"
                    job["status"] = "error"
                    return
                if r.status_code != 200:
                    job["error"] = f"ComfyUI history poll failed ({r.status_code})"
                    job["status"] = "error"
                    return
                hist = r.json()
                entry = hist.get(remote_id)
                if not entry:
                    job["status"] = "running" if job["status"] != "running" else job["status"]
                    await asyncio.sleep(_POLL_INTERVAL_S)
                    continue
                status = (entry.get("status") or {})
                if status.get("status_str") == "error":
                    msgs = [m[1].get("exception_message", "") for m in status.get("messages", [])
                            if isinstance(m, list) and len(m) > 1 and m[0] == "execution_error" and isinstance(m[1], dict)]
                    job["error"] = ("ComfyUI: " + (msgs[0][:300] if msgs else "workflow execution failed"))
                    job["status"] = "error"
                    await free_vram()
                    return
                if job.get("kind") == "audio":
                    fileinfo = _first_audio_output(entry.get("outputs") or {})
                elif job.get("kind") == "image":
                    fileinfo = _first_image_output(entry.get("outputs") or {})
                else:
                    fileinfo = _first_video_output(entry.get("outputs") or {})
                if not fileinfo:
                    job["error"] = "ComfyUI finished without an output file"
                    job["status"] = "error"
                    await free_vram()
                    return
                q = urlencode({
                    "filename": fileinfo["filename"],
                    "subfolder": fileinfo.get("subfolder", ""),
                    "type": fileinfo.get("type", "output"),
                })
                vr = await client.get(f"{base_url()}/view?{q}")
                if vr.status_code != 200:
                    job["error"] = f"ComfyUI output fetch failed ({vr.status_code})"
                    job["status"] = "error"
                    return
                ext = fileinfo["filename"].rsplit(".", 1)[-1].lower()
                await asyncio.to_thread(video_generation.finish_job_bytes, job, vr.content, ext)
                await free_vram()
                return
        job["error"] = f"Video generation exceeded the {int(deadline_s) // 60}-minute cap"
        job["status"] = "error"
    except Exception as e:
        logger.exception("ComfyUI job %s poller crashed", job_id)
        job["error"] = f"Video job failed: {e}"
        job["status"] = "error"
    finally:
        # Same reasoning as the sd-server poller: mirror on every exit path so a
        # finished ComfyUI render can never leave the queue thinking the GPU is
        # still occupied.
        video_generation._mirror_queue(job)


async def render_image(
    model: str,
    prompt: str,
    negative_prompt: str = "",
    width: int = 1024,
    height: int = 1024,
    seed: Optional[int] = None,
    steps: Optional[int] = None,
    budget_s: float = 600.0,
) -> Dict[str, Any]:
    """Render one image on a ComfyUI workflow and wait for it inline.

    Returns {"b64_json": ..., "seed": n} or {"error": ...} — the same contract
    the sd-server native path uses, so /api/image/generate can treat both
    engines identically. Frees ComfyUI's VRAM afterwards.
    """
    import base64
    import httpx
    from src import comfyui_workflows

    if seed is None or seed < 0:
        seed = uuid.uuid4().int % (2**31 - 1)
    params: Dict[str, Any] = dict(
        prompt=prompt, negative_prompt=negative_prompt or "",
        width=int(width), height=int(height), seed=int(seed),
    )
    if steps:
        params["steps"] = int(steps)
    graph = comfyui_workflows.build_image_graph(model, **params)
    if graph is None:
        return {"error": f"Unknown ComfyUI workflow '{model}'"}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)) as client:
            r = await client.post(f"{base_url()}/prompt", json={"prompt": graph, "client_id": "aegis"})
            if r.status_code != 200:
                return {"error": f"ComfyUI submit failed ({r.status_code}): {r.text[:300]}"}
            remote_id = str(r.json().get("prompt_id") or "")
            if not remote_id:
                return {"error": "ComfyUI returned no prompt id"}
            deadline = time.time() + budget_s
            while time.time() < deadline:
                hr = await client.get(f"{base_url()}/history/{remote_id}")
                if hr.status_code != 200:
                    return {"error": f"ComfyUI history poll failed ({hr.status_code})"}
                entry = hr.json().get(remote_id)
                if entry:
                    status = entry.get("status") or {}
                    if status.get("status_str") == "error":
                        msgs = [m[1].get("exception_message", "") for m in status.get("messages", [])
                                if isinstance(m, list) and len(m) > 1 and m[0] == "execution_error" and isinstance(m[1], dict)]
                        await free_vram()
                        return {"error": "ComfyUI: " + (msgs[0][:300] if msgs else "workflow execution failed")}
                    fileinfo = _first_image_output(entry.get("outputs") or {})
                    if not fileinfo:
                        await free_vram()
                        return {"error": "ComfyUI finished without an image output"}
                    q = urlencode({
                        "filename": fileinfo["filename"],
                        "subfolder": fileinfo.get("subfolder", ""),
                        "type": fileinfo.get("type", "output"),
                    })
                    vr = await client.get(f"{base_url()}/view?{q}")
                    await free_vram()
                    if vr.status_code != 200:
                        return {"error": f"ComfyUI output fetch failed ({vr.status_code})"}
                    return {"b64_json": base64.b64encode(vr.content).decode(), "seed": seed}
                await asyncio.sleep(2.0)
            return {"error": f"Image generation exceeded {int(budget_s)}s"}
    except httpx.TimeoutException:
        return {"error": "ComfyUI did not answer — is the engine running?"}


def _first_image_output(outputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for out in outputs.values():
        for f in out.get("images", []) or []:
            fn = str(f.get("filename") or "")
            if fn.rsplit(".", 1)[-1].lower() in ("png", "jpg", "jpeg", "webp"):
                return f
    return None


def _first_video_output(outputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for out in outputs.values():
        for kind in ("videos", "gifs", "images"):
            for f in out.get(kind, []) or []:
                fn = str(f.get("filename") or "")
                if fn.rsplit(".", 1)[-1].lower() in ("mp4", "webm", "mov", "m4v", "avi"):
                    return f
    return None


def _first_audio_output(outputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """SaveAudioMP3/SaveAudio report under an "audio" key (not "images")."""
    for out in outputs.values():
        for kind in ("audio", "audios"):
            for f in out.get(kind, []) or []:
                fn = str(f.get("filename") or "")
                if fn.rsplit(".", 1)[-1].lower() in ("mp3", "flac", "wav", "ogg", "opus"):
                    return f
    return None
