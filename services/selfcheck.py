"""A look, each night, at whether the house's server is well — and a word to whoever runs it when it is not.

The Doctor (``src/doctor.py``) checks what is installed and is run by hand. This checks what is *running*,
by itself, and says nothing when all is well:

    the model server answers            (the local one only: no outside service is called)
    the library answers                 (when the library is switched on)
    the drives have room                (the data folder and the library's)
    nothing scheduled failed overnight  (scheduled tasks and automations, in the last day)

``run()`` returns every check as ``{"name", "ok", "detail"}``; ``nightly()`` runs them, keeps the result in
``DATA_DIR/selfcheck.json``, and tells the administrators (their phones, and any open screen) when something
needs a look. A check that cannot be made is not a failure: only what was seen to be wrong is reported.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

LOW_SHARE = 0.05          # a drive with less than this free…
LOW_BYTES = 5 * 1024 ** 3  # …or less than this many bytes, needs a look


def _result(name: str, ok: bool, detail: str = "") -> Dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": str(detail or "")[:300]}


def _get(url: str, timeout: float = 6.0) -> int:
    """The status of a GET to a local address. Raises when it cannot be reached."""
    import httpx
    return httpx.get(url, timeout=timeout).status_code


def _model_server() -> Optional[Dict[str, Any]]:
    """The local model server answers its list of models. This is the plain list llama-swap serves from
    memory: it loads nothing and wakes nothing. An outside provider is never called from here."""
    try:
        from src.endpoint_resolver import resolve_endpoint
        from src.model_context import is_local_endpoint
        found = resolve_endpoint("default")
        url = str(found[0] or "") if found else ""
        if not url or not is_local_endpoint(url):
            return None
    except Exception:
        return None
    try:
        # the endpoint is kept as its chat address (…/v1/chat/completions) or as its base (…/v1): the list is beside either
        base = url.rstrip("/")
        for tail in ("/chat/completions", "/completions"):
            if base.endswith(tail):
                base = base[: -len(tail)]
        status = _get(base + "/models")
        return _result("The model server", status == 200, "" if status == 200 else f"It answered {status}.")
    except Exception as problem:
        return _result("The model server", False, f"It does not answer ({type(problem).__name__}).")


def _library() -> Optional[Dict[str, Any]]:
    try:
        from services.library import config
        if not config.enabled():
            return None
        url = config.kiwix_url()
    except Exception:
        return None
    try:
        status = _get(url.rstrip("/") + "/")
        return _result("The library", status == 200, "" if status == 200 else f"Its server answered {status}.")
    except Exception as problem:
        return _result("The library", False, f"Its server does not answer ({type(problem).__name__}).")


def room(path: str, name: str) -> Optional[Dict[str, Any]]:
    """Whether the drive holding ``path`` has room: at least 5% and at least 5 GB free."""
    try:
        usage = shutil.disk_usage(path)
    except Exception:
        return None
    free_gb = usage.free / 1024 ** 3
    low = usage.free < LOW_BYTES or usage.free / max(usage.total, 1) < LOW_SHARE
    return _result(name, not low, f"Only {free_gb:.0f} GB free of {usage.total / 1024 ** 3:.0f} GB." if low else f"{free_gb:.0f} GB free.")


def _drives() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    places = []
    try:
        from src.constants import DATA_DIR
        places.append((str(DATA_DIR), "Room on the data drive"))
    except Exception:
        pass
    try:
        from services.library import config
        if config.enabled():
            places.append((str(config.library_dir()), "Room on the library's drive"))
    except Exception:
        pass
    for path, name in places:
        try:
            device = os.stat(path).st_dev
        except OSError:
            continue
        if device in seen:
            continue
        seen.add(device)
        found = room(path, name)
        if found:
            out.append(found)
    return out


def _scheduled(since: datetime) -> Optional[Dict[str, Any]]:
    """Scheduled tasks and automations whose latest run since ``since`` failed."""
    failed: List[str] = []
    try:
        from core.database import ScheduledTask, SessionLocal, TaskRun
        db = SessionLocal()
        try:
            latest: Dict[str, Any] = {}
            rows = (db.query(TaskRun, ScheduledTask.name).join(ScheduledTask, ScheduledTask.id == TaskRun.task_id)
                    .filter(TaskRun.started_at >= since.replace(tzinfo=None)).order_by(TaskRun.started_at.asc()).all())
            for run, name in rows:
                latest[name] = run.status
            failed += [name for name, status in latest.items() if status == "error"]
        finally:
            db.close()
    except Exception:
        logger.debug("selfcheck: scheduled tasks not read", exc_info=True)
    try:
        from src import jobs
        for job in jobs.all_jobs():
            last = job.get("last_run") or {}
            if last and last.get("ok") is False and float(last.get("at") or 0) >= since.timestamp():
                failed.append(str(job.get("name") or "An automation"))
    except Exception:
        logger.debug("selfcheck: automations not read", exc_info=True)
    if not failed:
        return _result("Scheduled work", True, "Nothing failed in the last day.")
    listed = ", ".join(failed[:4]) + (f" and {len(failed) - 4} more" if len(failed) > 4 else "")
    return _result("Scheduled work", False, f"Failed in the last day: {listed}.")


def run() -> List[Dict[str, Any]]:
    """Every check, as ``{"name", "ok", "detail"}``. Slow by a few seconds at most; makes no change anywhere."""
    from core.database import utcnow_naive
    found = [_model_server(), _library(), *_drives(), _scheduled(utcnow_naive() - timedelta(hours=24))]
    return [entry for entry in found if entry]


def problems(results: List[Dict[str, Any]]) -> List[str]:
    """What needs a look, in words: "The library: its server does not answer (ConnectError)." """
    return [f"{entry['name']}: {entry['detail'] or 'not well.'}" for entry in results if not entry.get("ok")]


def _path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(str(DATA_DIR), "selfcheck.json")


def last() -> Dict[str, Any]:
    """The last night's look, or {} when there has been none."""
    try:
        with open(_path(), encoding="utf-8") as handle:
            found = json.load(handle)
        return found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        return {}


async def nightly(tell: bool = True) -> Dict[str, Any]:
    """Run the checks, keep the result, and tell the administrators when something is wrong."""
    results = await asyncio.to_thread(run)
    wrong = problems(results)
    record = {"at": time.time(), "results": results, "problems": wrong}
    try:
        with open(_path(), "w", encoding="utf-8") as handle:
            json.dump(record, handle)
    except OSError:
        logger.debug("selfcheck: the result was not kept", exc_info=True)
    if wrong and tell:
        try:
            from services import notify
            await notify.tell_admins("The server needs a look" if len(wrong) > 1 else wrong[0].split(":")[0] + " needs a look",
                                     " ".join(wrong)[:300], url="/admin#activity", tag="selfcheck")
        except Exception:
            logger.debug("selfcheck: nobody was told", exc_info=True)
    logger.info("selfcheck: %s", "all well" if not wrong else "; ".join(wrong))
    return record
