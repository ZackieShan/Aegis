"""Failed sign-ins are slowed down per account.

The per-address limiter (15/min) does little for a short password or PIN,
and nothing at all behind a reverse proxy where every client shares one
address. FailureBackoff counts misses per account instead.
"""
import asyncio
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src import rate_limiter
from src.rate_limiter import FailureBackoff


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(rate_limiter.time, "monotonic", fake.monotonic)
    return fake


def test_first_misses_are_free_then_the_wait_doubles_up_to_the_cap(clock):
    guard = FailureBackoff(free=3, base_s=30, max_s=200)
    for _ in range(3):
        guard.failure("robin")
        assert guard.retry_after("robin") == 0
    waits = []
    for _ in range(5):
        guard.failure("robin")
        waits.append(guard.retry_after("robin"))
        clock.now += 1000                       # sit the lock out, then miss again
    assert waits == [31, 61, 121, 201, 201]     # 30, 60, 120, then capped (reported rounded up)


def test_the_lock_runs_out_and_a_success_clears_the_count(clock):
    guard = FailureBackoff(free=1, base_s=30, max_s=900)
    guard.failure("a")
    guard.failure("a")
    assert guard.retry_after("a") == 31
    clock.now += 29
    assert guard.retry_after("a") == 2
    clock.now += 2
    assert guard.retry_after("a") == 0
    guard.success("a")
    guard.failure("a")
    assert guard.retry_after("a") == 0          # back to the free allowance


def test_accounts_do_not_affect_each_other_and_old_misses_are_forgotten(clock):
    guard = FailureBackoff(free=0, base_s=30, max_s=900, forget_after_s=3600)
    guard.failure("a")
    assert guard.retry_after("a") == 31 and guard.retry_after("b") == 0
    clock.now += 3600
    assert guard.retry_after("a") == 0
    guard.failure("a")
    assert guard.retry_after("a") == 31         # counted from one again, not doubled


def test_typed_in_names_cannot_grow_the_table_without_bound(clock):
    guard = FailureBackoff(free=0, base_s=30, max_s=900, max_keys=50)
    for index in range(500):
        clock.now += 1
        guard.failure(f"name-{index}" + "x" * 500)
    assert len(guard._state) == 50
    assert all(len(key) <= 128 for key in guard._state)
    assert guard.retry_after("name-499" + "x" * 500) > 0     # the most recent ones are the ones kept


# ── the sign-in route ──────────────────────────────────────────────────────

def _ensure_stub(name, **attrs):
    """Same approach as test_auth_event_loop.py: stand in for the heavy
    ``core.*`` modules so the route module imports on its own."""
    parent_name, _, child = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is None:
        parent = types.ModuleType(parent_name)
        real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), *parent_name.split("."))
        parent.__path__ = [real] if os.path.isdir(real) else []
        sys.modules[parent_name] = parent
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    for key, value in attrs.items():
        if not hasattr(module, key):
            setattr(module, key, value)
    if not hasattr(parent, child):
        setattr(parent, child, module)
    return module


@pytest.fixture
def login(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.database", _ensure_stub("core.database", SessionLocal=MagicMock()))
    monkeypatch.setitem(sys.modules, "core.auth", _ensure_stub("core.auth", AuthManager=MagicMock()))
    from routes.auth_routes import LoginRequest, setup_auth_routes

    auth = MagicMock()
    auth.totp_enabled.return_value = False
    auth.create_session_trusted.return_value = "token"
    passwords = {"robin": "right", "admin": "also-right"}
    auth.verify_password.side_effect = lambda user, password: passwords.get(user) == password
    router = setup_auth_routes(auth)
    endpoint = next(r.endpoint for r in router.routes
                    if getattr(r, "path", None) == "/api/auth/login" and "POST" in getattr(r, "methods", set()))
    addresses = iter(f"10.0.{n // 250}.{n % 250}" for n in range(10_000))

    def attempt(username, password, code=None):
        # A fresh client address every time: the per-address limiter never
        # trips, so anything that stops the caller is the per-account guard.
        request = SimpleNamespace(client=SimpleNamespace(host=next(addresses)), cookies={})
        try:
            return asyncio.run(endpoint(body=LoginRequest(username=username, password=password, totp_code=code),
                                        request=request, response=MagicMock()))
        except HTTPException as exc:
            return exc

    return attempt, auth


def test_sixth_wrong_password_locks_the_account_even_from_new_addresses(login, clock):
    attempt, auth = login
    for _ in range(5):
        assert attempt("robin", "0000").status_code == 401
    assert attempt("robin", "0001").status_code == 401            # the sixth miss starts the lock
    locked = attempt("robin", "right")
    assert locked.status_code == 429 and "this account" in locked.detail
    assert locked.headers["Retry-After"] == "31"
    checked = auth.verify_password.call_count
    attempt("robin", "0002")
    assert auth.verify_password.call_count == checked                # locked attempts are not even checked
    assert attempt("admin", "also-right")["ok"] is True              # other accounts are unaffected

    clock.now += 31
    assert attempt("robin", "right")["ok"] is True                # the right password works again…
    for _ in range(5):
        assert attempt("robin", "0000").status_code == 401        # …and wipes the slate


def test_unknown_names_are_treated_exactly_like_real_ones(login, clock):
    attempt, _ = login
    for _ in range(6):
        assert attempt("nobody-here", "x").status_code == 401
    assert attempt("nobody-here", "x").status_code == 429            # no hint about which accounts exist


def test_a_wrong_second_factor_counts_as_a_miss(login, clock):
    attempt, auth = login
    auth.totp_enabled.return_value = True
    auth.totp_verify.return_value = False
    # Right password, no code yet: that is a prompt for the code, not a miss.
    for _ in range(8):
        assert attempt("robin", "right") == {"ok": False, "requires_totp": True, "username": "robin"}
    for _ in range(6):
        assert attempt("robin", "right", code="000000").status_code == 401
    assert attempt("robin", "right", code="000000").status_code == 429
