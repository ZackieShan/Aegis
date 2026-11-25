"""Taking new files in, as one job.

Three steps make a file copied into the library folder part of the library:
read it into the index (:func:`files.scan`), refresh the shelves
(:func:`catalogue.build`) and, for what the folder and title did not settle,
ask the model (:func:`classify.run`). ``run`` does all three and reports as it
goes; ``start`` runs it in the background of the web app so a page can ask how
it is getting on.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_state = {"running": False, "started": 0.0, "finished": 0.0, "step": "", "summary": None, "error": ""}
_task: Optional[asyncio.Task] = None


def status() -> dict:
    from . import classify, files

    out = dict(_state)
    out["files"] = files.stats()
    out["waiting_for_model"] = len(classify.pending())
    return out


async def run(ask_model: bool = True, model_limit: int = 0, ocr: bool = False,
              progress: Optional[Callable[[str], None]] = None) -> dict:
    """Scan, shelve and (optionally) ask the model. Returns the scan's summary
    with ``model`` (what the model did) and ``busy`` when another run has the
    library folder."""
    from . import catalogue, classify, files

    def say(message: str) -> None:
        _state["step"] = message
        if progress:
            progress(message)

    say("looking for new files")
    summary = await asyncio.to_thread(lambda: files.scan(progress=say, ocr=ocr))
    if summary.get("busy"):
        return summary
    say("putting them on shelves")
    await asyncio.to_thread(lambda: catalogue.build(progress=say))
    summary["model"] = {"asked": 0, "filed": 0, "kept": 0, "failed": 0, "left": 0}
    if ask_model:
        summary["model"] = await classify.run(limit=model_limit, progress=say)
    summary["waiting_for_model"] = len(classify.pending())
    return summary


def start(ask_model: bool = True, model_limit: int = 0) -> bool:
    """Begin a run in the background (inside the web app's event loop).
    False when one is already going."""
    global _task
    if _state["running"]:
        return False
    _state.update(running=True, started=time.time(), finished=0.0, step="starting", summary=None, error="")

    async def job() -> None:
        try:
            _state["summary"] = await run(ask_model=ask_model, model_limit=model_limit)
        except Exception as exc:
            logger.exception("library: taking in new files failed")
            _state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _state.update(running=False, finished=time.time(), step="")

    _task = asyncio.get_running_loop().create_task(job())
    return True
