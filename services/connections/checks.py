"""Trying a connected service once, for the Try button on the Integrations screen.

``try_it(service_id)`` makes one small real request with the key that is kept and says, in a line a person
can act on, whether it worked. The result is remembered so the screen can show it next time.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Tuple

from . import catalog, store
from .http import Problem

logger = logging.getLogger(__name__)


def _brave() -> str:
    from services.search import brave
    brave.forget()                                       # a real request, not one answered from memory
    results = brave.web("weather forecast", 2)
    if not results:
        raise Problem("Brave answered, but with no results.", "nothing")
    try:
        _, texts = brave.passages("how does a heat pump work", urls=3, tokens=1024)
    except Problem:
        texts = {}
    extra = (" A search also brings back the passages that answer it." if texts
             else " (This plan gives no passages from the pages; searches still work, by reading the pages here.)")
    return "Brave Search answered. Web search, news and places are ready." + extra


def _provider(name: str, label: str) -> Callable[[], str]:
    def check() -> str:
        from services.search import core
        if not core._call_provider(name, "weather forecast", 2):
            raise Problem(f"{label} gave no results — the key is probably not right.", "key")
        return f"{label} answered."
    return check


def _medicines() -> str:
    from services.lookups import medicines
    label, _, _, _ = medicines.find("ibuprofen")
    return "The FDA's label service answered" + (" with your key." if store.value("medicines") else ". No key is needed.")


def _papers() -> str:
    from services.lookups import papers
    found = papers._openalex("vitamin D", None, "cited", 1)
    if not found:
        raise Problem("OpenAlex answered, but with nothing.", "nothing")
    return "OpenAlex answered" + (" with your key." if store.value("papers") else ". No key is needed; one gives the house its own allowance.")


def _wolfram() -> str:
    from services.lookups import wolfram
    text = wolfram.ask("2+2")
    if "4" not in text:
        raise Problem("Wolfram|Alpha answered, but not with what was expected.", "nothing")
    return "Wolfram|Alpha answered."


CHECKS: Dict[str, Callable[[], str]] = {
    "brave": _brave,
    "tavily": _provider("tavily", "Tavily"),
    "serper": _provider("serper", "Serper"),
    "google_pse": _provider("google_pse", "Google Programmable Search"),
    "medicines": _medicines,
    "papers": _papers,
    "wolfram": _wolfram,
}


def try_it(service_id: str) -> Tuple[bool, str]:
    """(worked, what to tell the administrator)."""
    service = catalog.get(service_id)
    check = CHECKS.get(service_id)
    if service is None or check is None:
        return False, "That is not a service this app knows."
    if service.needs_key and not store.has_key(service_id):
        return False, f"Type in the {service.required[0].label if service.required else 'key'} first."
    if not store.on(service_id):
        return False, f"{service.name} is switched off. Switch it on to try it."
    try:
        said = check()
        ok = True
    except Problem as problem:
        ok, said = False, str(problem)
    except Exception as problem:
        logger.exception("trying %s failed", service_id)
        ok, said = False, f"Trying {service.name} failed ({type(problem).__name__})."
    try:
        store.note_try(service_id, ok, said)
    except Exception:
        logger.debug("could not remember how the try went", exc_info=True)
    return ok, said
