"""Automations — a saved recipe that runs on a trigger and delivers its result.

A job is: a source recipe (a canned catalog template, or a saved recipe), a run
input, a trigger (manual, or a schedule — cron or fixed interval), and an output
action (in-app notification by default; email draft / document / note optional).

Storage mirrors recipes and styles: one JSON file per job under JOBS_DIR. This
module owns the data model, next-run math, and firing; the scheduler loop and
the HTTP routes drive it. Kept import-light and mostly pure so the schedule math
is unit-testable.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from src.constants import JOBS_DIR

logger = logging.getLogger(__name__)

_TRIGGERS = ("manual", "schedule")
_SCHEDULE_KINDS = ("cron", "interval")
_OUTPUTS = ("notify", "email", "document", "note")
_HISTORY_MAX = 20


# ── storage ───────────────────────────────────────────────────────────────────
def _ensure_dir() -> None:
    os.makedirs(JOBS_DIR, exist_ok=True)


def _path(job_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", job_id or "")
    if not safe:
        raise ValueError("invalid job id")
    return os.path.join(JOBS_DIR, f"{safe}.json")


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_path(job_id), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def list_jobs(owner: Optional[str] = None) -> List[Dict[str, Any]]:
    _ensure_dir()
    out: List[Dict[str, Any]] = []
    for fn in sorted(os.listdir(JOBS_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(JOBS_DIR, fn), encoding="utf-8") as f:
                job = json.load(f)
        except Exception:
            continue
        if owner is not None and job.get("owner") not in (None, owner):
            continue
        out.append(job)
    out.sort(key=lambda j: j.get("updated") or 0, reverse=True)
    return out


def all_jobs() -> List[Dict[str, Any]]:
    """Every job regardless of owner — for the scheduler loop."""
    return list_jobs(owner=None)


def _write(job: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_dir()
    tmp = _path(job["id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2)
    os.replace(tmp, _path(job["id"]))
    return job


def delete_job(job_id: str) -> bool:
    try:
        os.remove(_path(job_id))
        return True
    except (FileNotFoundError, ValueError):
        return False


def set_enabled(job_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
    job = get_job(job_id)
    if not job:
        return None
    job["enabled"] = bool(enabled)
    job["updated"] = time.time()
    job["next_run"] = compute_next_run(job) if enabled else None
    return _write(job)


# ── validation / normalization ────────────────────────────────────────────────
def _clean_trigger(raw: Any) -> Dict[str, Any]:
    raw = raw or {}
    kind = raw.get("kind") if isinstance(raw, dict) else None
    if kind not in _TRIGGERS:
        return {"kind": "manual"}
    if kind == "manual":
        return {"kind": "manual"}
    sched = raw.get("kind_of") or raw.get("schedule") or {}
    skind = raw.get("schedule_kind") or (sched.get("kind") if isinstance(sched, dict) else None) or raw.get("s_kind")
    # accept a flat shape too: {kind:"schedule", cron:"..."} / {interval_seconds:n}
    if raw.get("cron"):
        return {"kind": "schedule", "schedule": {"kind": "cron", "cron": str(raw["cron"]).strip()}}
    if raw.get("interval_seconds"):
        secs = _clamp_int(raw["interval_seconds"], 60, 30 * 24 * 3600, 3600)
        return {"kind": "schedule", "schedule": {"kind": "interval", "interval_seconds": secs}}
    if isinstance(sched, dict) and skind in _SCHEDULE_KINDS:
        if skind == "cron":
            return {"kind": "schedule", "schedule": {"kind": "cron", "cron": str(sched.get("cron", "")).strip()}}
        secs = _clamp_int(sched.get("interval_seconds"), 60, 30 * 24 * 3600, 3600)
        return {"kind": "schedule", "schedule": {"kind": "interval", "interval_seconds": secs}}
    return {"kind": "manual"}


def _clean_output(raw: Any) -> Dict[str, Any]:
    raw = raw or {}
    kind = raw.get("kind") if isinstance(raw, dict) else None
    if kind not in _OUTPUTS:
        return {"kind": "notify"}
    out: Dict[str, Any] = {"kind": kind}
    if kind == "email" and raw.get("to"):
        out["to"] = str(raw["to"])[:200]
    if kind in ("document", "note") and raw.get("title"):
        out["title"] = str(raw["title"])[:200]
    return out


def _clamp_int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def save_job(data: Dict[str, Any], owner: Optional[str] = None) -> Dict[str, Any]:
    """Create or update a job from a (possibly partial) client payload."""
    jid = data.get("id") or uuid.uuid4().hex[:12]
    existing = get_job(jid) or {}

    source = data.get("source") or existing.get("source") or {}
    if not isinstance(source, dict) or source.get("kind") not in ("canned", "saved"):
        raise ValueError("source must be {kind: canned|saved, ...}")
    if source["kind"] == "canned" and not source.get("template_id"):
        raise ValueError("canned source needs a template_id")
    if source["kind"] == "saved" and not source.get("recipe_id"):
        raise ValueError("saved source needs a recipe_id")

    trigger = _clean_trigger(data.get("trigger", existing.get("trigger")))
    now = time.time()
    job = {
        "id": jid,
        "name": (data.get("name") or existing.get("name") or "Untitled automation").strip()[:120],
        "source": {"kind": source["kind"],
                   **({"template_id": source["template_id"]} if source["kind"] == "canned"
                      else {"recipe_id": source["recipe_id"]})},
        "input": str(data.get("input", existing.get("input", "")))[:20000],
        "trigger": trigger,
        "output": _clean_output(data.get("output", existing.get("output"))),
        "enabled": bool(data.get("enabled", existing.get("enabled", True))),
        "owner": existing.get("owner", owner),
        "created": existing.get("created", now),
        "updated": now,
        "last_run": existing.get("last_run"),
        "history": existing.get("history", []),
    }
    job["next_run"] = compute_next_run(job, after=now) if job["enabled"] else None
    return _write(job)


# ── schedule math ─────────────────────────────────────────────────────────────
def compute_next_run(job: Dict[str, Any], after: Optional[float] = None) -> Optional[float]:
    """Next epoch-seconds this job should fire, or None for manual/disabled."""
    if not job.get("enabled"):
        return None
    trig = job.get("trigger") or {}
    if trig.get("kind") != "schedule":
        return None
    sched = trig.get("schedule") or {}
    base = after if after is not None else time.time()
    if sched.get("kind") == "interval":
        secs = int(sched.get("interval_seconds") or 3600)
        return base + max(60, secs)
    if sched.get("kind") == "cron":
        expr = sched.get("cron") or ""
        try:
            from croniter import croniter
            from datetime import datetime, timezone
            itr = croniter(expr, datetime.fromtimestamp(base, tz=timezone.utc))
            return itr.get_next(float)
        except Exception as e:
            logger.warning("job %s bad cron %r: %s", job.get("id"), expr, e)
            return None
    return None


def due_jobs(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Enabled scheduled jobs whose next_run has passed."""
    now = now if now is not None else time.time()
    out = []
    for job in all_jobs():
        if not job.get("enabled"):
            continue
        nr = job.get("next_run")
        if nr is not None and nr <= now:
            out.append(job)
    return out


# ── firing ────────────────────────────────────────────────────────────────────
async def _resolve_graph(job: Dict[str, Any]):
    """The runnable recipe graph for a job's source, or (None, error)."""
    source = job.get("source") or {}
    if source.get("kind") == "canned":
        from src import recipe_templates
        model = _best_served_model()
        graph = recipe_templates.build_graph(source.get("template_id"), model) if model else None
        if not graph:
            return None, ("No capable model is served." if not model
                          else "This recipe isn't runnable (a preview or unknown template).")
        # canned recipe may need a toolbox connected
        meta = recipe_templates.get_template(source.get("template_id"))
        if meta and meta.get("needs_toolbox"):
            from src.recipe_templates import TOOLBOX_LABELS
            if meta["needs_toolbox"] not in _connected_toolboxes():
                return None, f"Enable the {TOOLBOX_LABELS.get(meta['needs_toolbox'])} tools for this automation."
        return graph, None
    if source.get("kind") == "saved":
        from src import recipes as recipes_engine
        graph = recipes_engine.get_recipe(source.get("recipe_id"))
        if not graph:
            return None, "The saved recipe this automation runs was deleted."
        return graph, None
    return None, "Unknown automation source."


def _best_served_model() -> Optional[str]:
    """The best text model across enabled endpoints (canned recipes bind to it)."""
    from src.recipe_templates import best_model
    models: List[str] = []
    try:
        from core.database import SessionLocal, ModelEndpoint
        db = SessionLocal()
        try:
            for ep in db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all():  # noqa: E712
                for m in (json.loads(ep.cached_models) if ep.cached_models else []) or []:
                    if m not in models:
                        models.append(m)
        finally:
            db.close()
    except Exception as e:
        logger.debug("job model lookup failed: %s", e)
    return best_model(models)


def _connected_toolboxes():
    try:
        from src.tool_utils import get_mcp_manager
        from src.builtin_mcp import TOOLBOX_MCP_IDS
        mcp = get_mcp_manager()
        return {t.get("server_id") for t in mcp.get_all_tools()
                if t.get("server_id") in TOOLBOX_MCP_IDS}
    except Exception:
        return set()


async def _deliver(job: Dict[str, Any], final: str) -> None:
    """Deliver a finished run to the job's output target. Notification is the
    default and always also fires (so a result is never silently dropped);
    document/note additionally save the result somewhere durable."""
    output = job.get("output") or {"kind": "notify"}
    kind = output.get("kind", "notify")
    name = job.get("name") or "Automation"
    owner = job.get("owner")

    # In-app notification — the default channel, and a floor for every job.
    try:
        from src.event_bus import get_task_scheduler
        sched = get_task_scheduler()
        if sched is not None:
            sched.add_notification(name, "success", owner=owner, body=final)
    except Exception as e:
        logger.debug("job notify failed: %s", e)

    if kind == "document":
        await asyncio.to_thread(_save_document, job, final)
    elif kind == "note":
        await asyncio.to_thread(_save_note, job, final)
    # 'email' delivery (draft) is a fast-follow — a job set to email still gets
    # the notification above, so nothing is lost.


def _save_document(job: Dict[str, Any], final: str) -> None:
    import datetime
    from core.database import SessionLocal, Document, DocumentVersion
    title = (job.get("output") or {}).get("title") or f"{job.get('name', 'Automation')} — {datetime.date.today().isoformat()}"
    db = SessionLocal()
    try:
        doc_id = uuid.uuid4().hex
        db.add(Document(id=doc_id, title=title[:200], language="markdown",
                        current_content=final, version_count=1, owner=job.get("owner")))
        db.add(DocumentVersion(id=uuid.uuid4().hex, document_id=doc_id, version_number=1,
                               content=final, summary="Automation result", source="ai"))
        db.commit()
    finally:
        db.close()


def _save_note(job: Dict[str, Any], final: str) -> None:
    from core.database import SessionLocal, Note
    title = (job.get("output") or {}).get("title") or job.get("name", "Automation")
    db = SessionLocal()
    try:
        db.add(Note(id=uuid.uuid4().hex, content=f"**{title}**\n\n{final}",
                    owner=job.get("owner"), source="agent"))
        db.commit()
    finally:
        db.close()


async def fire_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Run a job's recipe and deliver the result. Records the run + reschedules.
    Returns {ok, final|error}."""
    from src import recipes as recipes_engine
    graph, err = await _resolve_graph(job)
    if err:
        record_run(job["id"], ok=False, error=err)
        return {"ok": False, "error": err}
    try:
        result = await recipes_engine.run_recipe(graph, job.get("input", ""), owner=job.get("owner"))
    except Exception as e:
        logger.exception("automation %s run crashed", job.get("id"))
        record_run(job["id"], ok=False, error=str(e))
        return {"ok": False, "error": str(e)}
    if not result.get("ok"):
        record_run(job["id"], ok=False, error=result.get("error", "run failed"))
        return {"ok": False, "error": result.get("error", "run failed")}

    final = result.get("final") or ""
    try:
        await _deliver(job, final)
    except Exception as e:
        logger.warning("automation %s delivery failed: %s", job.get("id"), e)
    record_run(job["id"], ok=True, summary=final[:600])
    return {"ok": True, "final": final}


def record_run(job_id: str, ok: bool, summary: str = "", error: str = "",
               reschedule: bool = True) -> Optional[Dict[str, Any]]:
    """Stamp a run onto the job: last_run, history (capped), and next_run."""
    job = get_job(job_id)
    if not job:
        return None
    now = time.time()
    entry = {"at": now, "ok": bool(ok), "summary": (summary or "")[:600],
             "error": (error or "")[:600]}
    job["last_run"] = entry
    hist = (job.get("history") or [])
    hist.insert(0, entry)
    job["history"] = hist[:_HISTORY_MAX]
    if reschedule:
        job["next_run"] = compute_next_run(job, after=now)
    job["updated"] = now
    return _write(job)


# ── scheduler loop ─────────────────────────────────────────────────────────────
# A small dedicated poll loop (the app's TaskScheduler drives ScheduledTask rows;
# automations are file-backed and firing is idempotent, so a separate ~30s tick
# is simpler than retrofitting them onto that model). next_run is persisted, so a
# restart resumes cleanly; record_run advances next_run, so a due job never
# re-fires in a tight loop.
_scheduler_task: Optional["asyncio.Task"] = None
_POLL_SECONDS = 30


async def _scheduler_loop() -> None:
    logger.info("Automations scheduler started (%ss tick)", _POLL_SECONDS)
    while True:
        try:
            await asyncio.sleep(_POLL_SECONDS)
            for job in due_jobs():
                # Advance next_run first so a slow run can't be double-fired by
                # the next tick; fire_job then records the single real outcome.
                _claim(job)
                asyncio.create_task(_fire_and_forget(job))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("automations scheduler tick failed")


async def _fire_and_forget(job: Dict[str, Any]) -> None:
    try:
        await fire_job(job)  # fire_job records the real outcome (overwrites "(started)")
    except Exception:
        logger.exception("automation %s crashed", job.get("id"))


def start_scheduler() -> None:
    """Start the poll loop (idempotent). Call from the app lifespan."""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        return
    # On boot, advance any next_run that elapsed while the app was down to the
    # next future slot instead of firing a backlog all at once.
    _catch_up()
    _scheduler_task = asyncio.create_task(_scheduler_loop())


def _claim(job: Dict[str, Any]) -> None:
    """Advance next_run past now without touching history (fire_job records the
    outcome). Re-reads from disk so a concurrent toggle/edit isn't clobbered."""
    fresh = get_job(job["id"])
    if not fresh:
        return
    fresh["next_run"] = compute_next_run(fresh, after=time.time())
    _write(fresh)


def _catch_up() -> None:
    now = time.time()
    for job in all_jobs():
        if job.get("enabled") and job.get("next_run") and job["next_run"] <= now:
            job["next_run"] = compute_next_run(job, after=now)
            _write(job)
