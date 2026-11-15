"""Engine watchdog.

Two failure directions matter and both are tested here:
  * restarting when it shouldn't (bouncing a live engine on one flaky probe,
    or hot-looping a crashing one);
  * NOT restarting when it should (a dead engine staying dead for days — the
    7/26 incident this module exists to prevent).

No real sockets or processes: probes, spawns and the clock are all faked.
"""

import asyncio

import pytest

from src import engine_watchdog as wd

DEFS = {
    "llama-swap": {
        "health_url": "http://127.0.0.1:9090/v1/models",
        "argv": ["llama-swap.exe", "-config", "llama-swap.yaml"],
        "cwd": None,
        "installed": True,
    },
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    wd._reset_for_tests()
    monkeypatch.setattr(wd, "is_enabled", lambda: True)
    yield
    wd._reset_for_tests()


@pytest.fixture
def clock(monkeypatch):
    c = {"t": 1000.0}
    monkeypatch.setattr(wd, "_now", lambda: c["t"])
    return c


@pytest.fixture
def probe(monkeypatch):
    """Controllable health probe: set probe['ok'] to steer the next cycles."""
    p = {"ok": True, "urls": []}

    async def fake_probe(url):
        p["urls"].append(url)
        return p["ok"]

    monkeypatch.setattr(wd, "_probe", fake_probe)
    return p


@pytest.fixture
def spawns(monkeypatch):
    calls = []
    monkeypatch.setattr(wd, "_spawn", lambda name, spec: calls.append((name, spec["argv"])) or True)
    return calls


@pytest.fixture
def defs(monkeypatch):
    monkeypatch.setattr(wd, "_engine_defs", lambda: {k: dict(v) for k, v in DEFS.items()})


def _cycle():
    asyncio.run(wd._run_cycle())


# ── must NOT restart ──

def test_healthy_engine_never_restarted(defs, clock, probe, spawns):
    for _ in range(3):
        _cycle()
    assert spawns == []
    st = wd.get_status()["llama-swap"]
    assert st["healthy"] is True
    assert st["installed"] is True
    assert st["consecutive_failures"] == 0
    assert st["restarts"] == 0


def test_single_failed_probe_does_not_restart(defs, clock, probe, spawns):
    probe["ok"] = False
    _cycle()
    assert spawns == []
    st = wd.get_status()["llama-swap"]
    assert st["healthy"] is False
    assert st["consecutive_failures"] == 1


def test_recovery_between_failures_resets_the_count(defs, clock, probe, spawns):
    probe["ok"] = False
    _cycle()
    probe["ok"] = True
    _cycle()
    probe["ok"] = False
    _cycle()
    assert spawns == []  # never two *consecutive* failures


def test_missing_binary_never_probed_or_restarted(monkeypatch, clock, probe, spawns):
    defs = {k: dict(v, installed=False) for k, v in DEFS.items()}
    monkeypatch.setattr(wd, "_engine_defs", lambda: defs)
    for _ in range(3):
        _cycle()
    assert probe["urls"] == []
    assert spawns == []
    st = wd.get_status()["llama-swap"]
    assert st["installed"] is False
    assert st["healthy"] is None


def test_setting_off_loop_makes_no_probes(monkeypatch, probe, spawns):
    monkeypatch.setattr(wd, "is_enabled", lambda: False)

    class _Stop(Exception):
        pass

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise _Stop

    monkeypatch.setattr(wd.asyncio, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        asyncio.run(wd._watchdog_loop())
    assert probe["urls"] == []
    assert spawns == []


def test_loop_probes_when_enabled(monkeypatch, clock, defs, probe, spawns):
    """Sanity for the loop path itself — the enabled check must not be
    inverted."""

    class _Stop(Exception):
        pass

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise _Stop

    monkeypatch.setattr(wd.asyncio, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        asyncio.run(wd._watchdog_loop())
    assert probe["urls"] != []


# ── must restart ──

def test_two_consecutive_failures_spawn_once(defs, clock, probe, spawns, caplog):
    probe["ok"] = False
    _cycle()
    with caplog.at_level("WARNING"):
        _cycle()
    assert [name for name, _ in spawns] == ["llama-swap"]
    assert any("llama-swap" in r.message and "restarting" in r.message
               for r in caplog.records if r.levelname == "WARNING")
    st = wd.get_status()["llama-swap"]
    assert st["restarts"] == 1
    assert st["backoff_seconds"] == wd.BACKOFF_BASE_S


def test_backoff_gates_respawn_and_doubles(defs, clock, probe, spawns):
    probe["ok"] = False
    _cycle()
    _cycle()                     # restart #1, backoff 30
    assert len(spawns) == 1

    _cycle()                     # still failing, 0s elapsed — gated
    clock["t"] += wd.BACKOFF_BASE_S - 1
    _cycle()                     # 29s elapsed — still gated
    assert len(spawns) == 1

    clock["t"] += 1              # 30s elapsed — allowed
    _cycle()                     # restart #2, backoff 60
    assert len(spawns) == 2
    assert wd.get_status()["llama-swap"]["backoff_seconds"] == 2 * wd.BACKOFF_BASE_S

    clock["t"] += wd.BACKOFF_BASE_S
    _cycle()                     # 30 of 60s elapsed — gated
    assert len(spawns) == 2
    clock["t"] += wd.BACKOFF_BASE_S
    _cycle()                     # restart #3, backoff 120
    assert len(spawns) == 3
    assert wd.get_status()["llama-swap"]["backoff_seconds"] == 4 * wd.BACKOFF_BASE_S


def test_backoff_caps(defs, clock, probe, spawns):
    probe["ok"] = False
    _cycle()
    for _ in range(12):
        _cycle()
        clock["t"] += wd.BACKOFF_CAP_S  # always past the gate
    assert wd.get_status()["llama-swap"]["backoff_seconds"] == wd.BACKOFF_CAP_S


def test_sustained_health_resets_backoff(defs, clock, probe, spawns):
    probe["ok"] = False
    _cycle()
    _cycle()                     # restart, backoff 30
    probe["ok"] = True
    _cycle()                     # healthy again
    clock["t"] += wd.HEALTHY_RESET_S
    _cycle()                     # healthy for 5 min — backoff resets
    assert wd.get_status()["llama-swap"]["backoff_seconds"] == 0

    probe["ok"] = False
    _cycle()
    _cycle()                     # next crash restarts immediately again
    assert len(spawns) == 2


def test_brief_health_does_not_reset_backoff(defs, clock, probe, spawns):
    """One good probe between crashes is still a crash loop."""
    probe["ok"] = False
    _cycle()
    _cycle()                     # restart, backoff 30
    probe["ok"] = True
    _cycle()                     # one healthy probe only
    probe["ok"] = False
    assert wd.get_status()["llama-swap"]["backoff_seconds"] == wd.BACKOFF_BASE_S


# ── booting children must not be doubled ──

BOOT_DEFS = {
    "comfyui": {
        "health_url": "http://127.0.0.1:8188/system_stats",
        "argv": ["python.exe", "main.py"],
        "cwd": None,
        "installed": True,
        "boot_grace_s": 100,
    },
}


@pytest.fixture
def boot_defs(monkeypatch):
    monkeypatch.setattr(wd, "_engine_defs", lambda: {k: dict(v) for k, v in BOOT_DEFS.items()})


def test_slow_booting_child_not_duplicated(boot_defs, clock, probe, spawns):
    """ComfyUI dies; its replacement spends longer than the 30s backoff in
    torch imports before binding the port. The failed probes during that boot
    must not stack a second copy — one real crash, one spawn."""
    probe["ok"] = True
    _cycle()                     # proven up once — launch grace no longer applies
    probe["ok"] = False
    _cycle()
    _cycle()                     # restart #1, backoff 30, boot grace 100
    assert len(spawns) == 1
    st = wd.get_status()["comfyui"]
    assert st["consecutive_failures"] == 0   # booting, not failing
    assert st["healthy"] is None

    for _ in range(3):
        clock["t"] += wd.PROBE_INTERVAL_S
        _cycle()                 # +30/+60/+90 — backoff long expired, still gated
    assert len(spawns) == 1

    clock["t"] += wd.PROBE_INTERVAL_S
    _cycle()                     # +120 — grace over, 2+ fresh failures
    assert len(spawns) == 2


def test_boot_grace_covers_launcher_started_engine(boot_defs, clock, probe, spawns):
    """At Aegis boot the launcher has just spawned ComfyUI itself; an engine
    that has never answered gets its boot grace before the watchdog may stack
    a duplicate on it."""
    probe["ok"] = False
    _cycle()                     # first cycle anchors the boot window
    clock["t"] += wd.PROBE_INTERVAL_S
    _cycle()                     # +30 — two failures, still booting
    clock["t"] += wd.PROBE_INTERVAL_S
    _cycle()                     # +60
    assert spawns == []

    clock["t"] += 2 * wd.PROBE_INTERVAL_S
    _cycle()                     # +120 — genuinely dead, restart
    assert [name for name, _ in spawns] == ["comfyui"]


def test_boot_grace_off_once_engine_has_answered(boot_defs, clock, probe, spawns):
    """A proven-up engine that dies gets the normal two-failure restart with
    no extra boot delay — the 7/26 case must not wait out the grace."""
    probe["ok"] = True
    _cycle()
    probe["ok"] = False
    _cycle()
    _cycle()
    assert len(spawns) == 1


# ── status snapshot ──

def test_status_exposes_ages_not_monotonic_timestamps(defs, clock, probe, spawns):
    """Internal timestamps are time.monotonic() (time-since-boot on Windows);
    the snapshot must expose ages so no consumer does epoch math on them."""
    probe["ok"] = False
    _cycle()
    _cycle()                     # restart at t
    clock["t"] += 45
    st = wd.get_status()["llama-swap"]
    assert "last_probe_ts" not in st and "last_restart_ts" not in st
    assert st["seconds_since_probe"] == 45
    assert st["seconds_since_restart"] == 45


def test_status_age_none_when_never_restarted(defs, clock, probe, spawns):
    _cycle()
    st = wd.get_status()["llama-swap"]
    assert st["seconds_since_probe"] == 0
    assert st["seconds_since_restart"] is None


# ── spawn mechanics ──

class _FakeProc:
    pid = 4242
    returncode = None

    def poll(self):
        return None


def test_chromadb_spawn_uses_run_chroma_wrapper(monkeypatch):
    """The chroma.exe console script exits silently on 1.5.9 (rust CLI argv
    bug) — a watchdog that respawned via it would 'restart' a corpse forever."""
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return _FakeProc()

    monkeypatch.setattr(wd.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(wd.time, "sleep", lambda s: None)

    spec = wd._engine_defs()["chromadb"]
    assert wd._spawn("chromadb", spec) is True

    argv, kwargs = calls[0]
    assert argv[1].endswith("run_chroma.py")
    assert argv[0].endswith("python.exe" if wd.IS_WINDOWS else "python")  # venv python, not chroma.exe
    assert "--host" in argv and "--port" in argv and "--path" in argv
    # Detached so the child survives an Aegis restart.
    if wd.IS_WINDOWS:
        flags = kwargs["creationflags"]
        for bit in (0x00000008, 0x00000200, 0x08000000):  # DETACHED | NEW_GROUP | NO_WINDOW
            assert flags & bit
    else:
        assert kwargs.get("start_new_session") is True


def test_spawn_reports_immediate_child_death(monkeypatch):
    class DeadProc:
        pid = 4242
        returncode = 1

        def poll(self):
            return 1

    monkeypatch.setattr(wd.subprocess, "Popen", lambda argv, **kw: DeadProc())
    monkeypatch.setattr(wd.time, "sleep", lambda s: None)
    assert wd._spawn("llama-swap", wd._engine_defs()["llama-swap"]) is False
