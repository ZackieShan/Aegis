# src/rate_limiter.py
"""Generic in-memory rate limiter — sliding window, keyed by IP."""

import threading
import time
from typing import Dict, List


class RateLimiter:
    """Sliding-window rate limiter.

    Usage:
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        if not limiter.check(ip):
            raise HTTPException(429, "Too many requests")
    """

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window = window_seconds
        self._log: Dict[str, List[float]] = {}
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()
        self._cleanup_interval = max(window_seconds * 2, 120)

    def check(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.monotonic()
        with self._lock:
            self._maybe_cleanup(now)
            timestamps = self._log.get(key, [])
            cutoff = now - self.window
            timestamps = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= self.max_requests:
                self._log[key] = timestamps
                return False
            timestamps.append(now)
            self._log[key] = timestamps
            return True

    def _maybe_cleanup(self, now: float) -> None:
        """Periodically purge stale entries."""
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        cutoff = now - self.window
        stale = [k for k, v in self._log.items() if not v or v[-1] <= cutoff]
        for k in stale:
            del self._log[k]


class FailureBackoff:
    """Slows down repeated failures against one key — e.g. sign-ins to one
    account, wherever they come from.

    The sliding-window limiter above counts requests per client address. Behind
    a reverse proxy every client can share one address, and a short password
    or PIN falls to a patient guesser at 15 tries a minute. This counts
    *failures per account* instead: the first ``free`` are free, each one
    after that locks the key for ``base_s`` seconds, doubling up to ``max_s``.
    A success clears the count, as does ``forget_after_s`` without a failure.

    The lock applies to whatever name was typed, real or not, so it reveals
    nothing about which accounts exist.
    """

    def __init__(self, free: int = 5, base_s: float = 30, max_s: float = 900,
                 forget_after_s: float = 3600, max_keys: int = 5000):
        self.free = max(int(free), 0)
        self.base_s = float(base_s)
        self.max_s = float(max_s)
        self.forget_after_s = float(forget_after_s)
        self.max_keys = max(int(max_keys), 1)
        self._state: Dict[str, List[float]] = {}     # key -> [failures, last failure, locked until]
        self._lock = threading.Lock()

    @staticmethod
    def _key(key: str) -> str:
        return str(key or "")[:128]

    def retry_after(self, key: str) -> int:
        """Seconds the caller has to wait before trying again (0 = go ahead)."""
        now = time.monotonic()
        with self._lock:
            state = self._state.get(self._key(key))
            if not state:
                return 0
            if now - state[1] >= self.forget_after_s:
                del self._state[self._key(key)]
                return 0
            remaining = state[2] - now
            return int(remaining) + 1 if remaining > 0 else 0

    def failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            key = self._key(key)
            state = self._state.get(key)
            if not state or now - state[1] >= self.forget_after_s:
                state = [0, now, 0.0]
            state[0] += 1
            state[1] = now
            over = state[0] - self.free
            if over > 0:
                state[2] = now + min(self.base_s * (2 ** min(over - 1, 30)), self.max_s)
            self._state[key] = state
            if len(self._state) > self.max_keys:
                # Typed-in names are attacker-controlled: never let them pile up.
                for stale in sorted(self._state, key=lambda k: self._state[k][1])[: len(self._state) - self.max_keys]:
                    del self._state[stale]

    def success(self, key: str) -> None:
        with self._lock:
            self._state.pop(self._key(key), None)
