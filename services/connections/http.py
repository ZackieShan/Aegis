"""Asking an outside service, in one place: one way to fail, and no key ever written to a log.

Every call to a connected service goes through ``get``. It turns the many ways a request can go wrong
into a ``Problem`` whose words are fit to show a person, and whose ``reason`` says what kind it was so
the caller can fall back, wait, or tell the administrator the key is wrong.

``requests`` is used on purpose: the app's ``httpx`` logger writes the whole address of each request,
and some services take their key in the address.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

USER_AGENT = "Aegis/1.0 (self-hosted assistant)"
TIMEOUT = 12.0

# reason → what it means for the caller
#   off      the service is switched off, or the test suite is running
#   key      there is no key, or the service did not accept it
#   limit    this month's limit is reached, or the account has no credit left
#   busy     the service asked us to slow down
#   offline  the internet (or the service) could not be reached
#   refused  the service understood the request and said no
#   nothing  it answered, with nothing that fits


class Problem(Exception):
    def __init__(self, text: str, reason: str = "offline", status: int = 0):
        super().__init__(text)
        self.reason = reason
        self.status = status


def _said(response) -> str:
    """The service's own words about what went wrong, if it gave any, kept short."""
    try:
        body = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text[:160] if text and "<" not in text[:40] else ""
    if isinstance(body, dict):
        for path in (("error", "detail"), ("error", "message"), ("message",), ("detail",), ("error",)):
            found: Any = body
            for step in path:
                found = found.get(step) if isinstance(found, dict) else None
            if isinstance(found, str) and found.strip():
                return found.strip()[:160]
    return ""


def get(url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None,
        timeout: float = TIMEOUT, name: str = "The service", want: str = "json", keep_headers: Optional[dict] = None):
    """GET ``url`` and return its JSON (``want="json"``) or its text (``want="text"``).

    ``keep_headers``, when given, is filled with the response's headers (lower-case names) so a caller
    can read the service's own rate limits.
    """
    if os.environ.get("AEGIS_CONNECTIONS_DISABLED"):
        raise Problem(f"{name} is not asked while outside services are switched off.", "off")
    import requests

    sent = {"User-Agent": USER_AGENT, "Accept": "application/json" if want == "json" else "*/*"}
    sent.update(headers or {})
    try:
        response = requests.get(url, params=params, headers=sent, timeout=timeout)
    except requests.Timeout:
        raise Problem(f"{name} did not answer in time.", "offline")
    except requests.RequestException as problem:
        logger.info("%s could not be reached: %s", name, type(problem).__name__)
        raise Problem(f"{name} could not be reached.", "offline")
    if keep_headers is not None:
        keep_headers.update({key.lower(): value for key, value in response.headers.items()})
    status = response.status_code
    if status in (401, 403):
        said = _said(response)
        raise Problem(f"{name} did not accept the key." + (f" It said: {said}" if said else ""), "key", status)
    if status == 402:
        raise Problem(f"{name} says the account has no credit left.", "limit", status)
    if status == 429:
        raise Problem(f"{name} asked us to slow down.", "busy", status)
    if status >= 500:
        raise Problem(f"{name} is having trouble (it answered {status}).", "offline", status)
    if status >= 400:
        said = _said(response)
        raise Problem(f"{name} would not answer that" + (f": {said}" if said else f" (it answered {status})."), "refused", status)
    if want == "text":
        return response.text
    try:
        return response.json()
    except ValueError:
        raise Problem(f"{name} sent back something that could not be read.", "offline", status)
