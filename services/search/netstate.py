"""Is the internet reachable right now? A cheap, cached answer.

Used to route searches: with the internet down, every online provider costs
two connect timeouts before the offline library gets its turn, and the agent's
web_search tool gives up after 30 s — so "works offline" needs to know it is
offline *before* trying the web.

The probe is a plain TCP connect (no DNS, no payload) to a few anycast
resolvers; the first one to answer wins. Override the targets with
``AEGIS_NET_PROBES="host:port,host:port"``; force the answer with
``AEGIS_ASSUME_OFFLINE=1`` / ``AEGIS_ASSUME_ONLINE=1`` (tests, drills).
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_PROBES: Tuple[Tuple[str, int], ...] = (("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443))
_CONNECT_TIMEOUT = 1.5
_TTL_UP = 60.0
_TTL_DOWN = 12.0

_lock = threading.Lock()
_cache: Optional[Tuple[float, bool]] = None


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _probes() -> List[Tuple[str, int]]:
    raw = os.environ.get("AEGIS_NET_PROBES", "").strip()
    if not raw:
        return list(_DEFAULT_PROBES)
    out = []
    for item in raw.split(","):
        host, _, port = item.strip().rpartition(":")
        if host and port.isdigit():
            out.append((host.strip("[]"), int(port)))
    return out or list(_DEFAULT_PROBES)


def _can_connect(target: Tuple[str, int]) -> bool:
    try:
        with socket.create_connection(target, timeout=_CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def reset() -> None:
    global _cache
    with _lock:
        _cache = None


def internet_available(force: bool = False) -> bool:
    """True when at least one probe target accepts a TCP connection.

    Cached for a minute while up and a few seconds while down, so coming back
    online is noticed quickly and a healthy connection is not re-probed on
    every search."""
    global _cache
    if _truthy("AEGIS_ASSUME_OFFLINE"):
        return False
    if _truthy("AEGIS_ASSUME_ONLINE"):
        return True
    now = time.monotonic()
    with _lock:
        cached = _cache
    if not force and cached is not None:
        if now - cached[0] < (_TTL_UP if cached[1] else _TTL_DOWN):
            return cached[1]
    targets = _probes()
    online = False
    pool = ThreadPoolExecutor(max_workers=len(targets))
    try:
        futures = [pool.submit(_can_connect, target) for target in targets]
        for future in as_completed(futures, timeout=_CONNECT_TIMEOUT + 1.0):
            if future.result():
                online = True
                break
    except Exception:
        online = False
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    with _lock:
        previous = _cache[1] if _cache else None
        _cache = (now, online)
    if previous is not None and previous != online:
        logger.info("Internet is %s", "reachable again" if online else "unreachable — searches use the offline library")
    return online
