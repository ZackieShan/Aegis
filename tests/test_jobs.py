"""Automations (jobs): storage, normalization, schedule math, and due-detection."""
import time

import pytest


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    import src.jobs as j
    monkeypatch.setattr(j, "JOBS_DIR", str(tmp_path))
    return j


def _canned(**over):
    base = {"name": "Daily NVDA", "source": {"kind": "canned", "template_id": "stock"},
            "input": "NVDA", "trigger": {"kind": "schedule", "interval_seconds": 3600},
            "output": {"kind": "notify"}}
    base.update(over)
    return base


def test_save_normalizes_and_computes_next_run(jobs):
    rec = jobs.save_job(_canned(), owner="me")
    assert rec["id"] and rec["owner"] == "me" and rec["enabled"] is True
    assert rec["source"] == {"kind": "canned", "template_id": "stock"}
    assert rec["trigger"] == {"kind": "schedule", "schedule": {"kind": "interval", "interval_seconds": 3600}}
    assert rec["output"] == {"kind": "notify"}
    assert rec["next_run"] and rec["next_run"] > time.time()


def test_save_rejects_bad_source(jobs):
    with pytest.raises(ValueError):
        jobs.save_job({"name": "x", "source": {"kind": "bogus"}})
    with pytest.raises(ValueError):
        jobs.save_job({"name": "x", "source": {"kind": "canned"}})  # no template_id


def test_manual_and_cron_triggers(jobs):
    m = jobs.save_job(_canned(trigger={"kind": "manual"}))
    assert m["trigger"] == {"kind": "manual"} and m["next_run"] is None
    c = jobs.save_job(_canned(trigger={"kind": "schedule", "cron": "0 7 * * *"}))
    assert c["trigger"]["schedule"] == {"kind": "cron", "cron": "0 7 * * *"}
    assert c["next_run"] is not None


def test_interval_next_run_is_clamped(jobs):
    rec = jobs.save_job(_canned(trigger={"kind": "schedule", "interval_seconds": 5}))
    # floor is 60s
    assert rec["trigger"]["schedule"]["interval_seconds"] == 60


def test_disabled_job_has_no_next_run(jobs):
    rec = jobs.save_job(_canned(enabled=False))
    assert rec["enabled"] is False and rec["next_run"] is None


def test_set_enabled_toggles_next_run(jobs):
    rec = jobs.save_job(_canned())
    off = jobs.set_enabled(rec["id"], False)
    assert off["enabled"] is False and off["next_run"] is None
    on = jobs.set_enabled(rec["id"], True)
    assert on["enabled"] is True and on["next_run"] is not None


def test_due_jobs_detects_overdue(jobs, monkeypatch):
    rec = jobs.save_job(_canned())
    # force next_run into the past
    rec["next_run"] = time.time() - 10
    jobs._write(rec)
    due = jobs.due_jobs()
    assert [d["id"] for d in due] == [rec["id"]]
    # a future job is not due
    future = jobs.save_job(_canned(name="future"))
    assert future["id"] not in [d["id"] for d in jobs.due_jobs(now=time.time())]


def test_record_run_stamps_history_and_reschedules(jobs):
    rec = jobs.save_job(_canned())
    before = rec["next_run"]
    time.sleep(0.01)
    updated = jobs.record_run(rec["id"], ok=True, summary="did the thing")
    assert updated["last_run"]["ok"] is True
    assert updated["last_run"]["summary"] == "did the thing"
    assert len(updated["history"]) == 1
    assert updated["next_run"] != before  # rescheduled


def test_history_is_capped(jobs):
    rec = jobs.save_job(_canned())
    for i in range(25):
        jobs.record_run(rec["id"], ok=True, summary=f"run {i}")
    assert len(jobs.get_job(rec["id"])["history"]) == jobs._HISTORY_MAX


def test_list_jobs_owner_scoped(jobs):
    jobs.save_job(_canned(name="mine"), owner="me")
    jobs.save_job(_canned(name="yours"), owner="you")
    jobs.save_job(_canned(name="shared"), owner=None)
    mine = {j["name"] for j in jobs.list_jobs(owner="me")}
    assert "mine" in mine and "shared" in mine and "yours" not in mine


# ── firing (recipe run + delivery) ────────────────────────────────────────────
def test_fire_job_runs_recipe_and_delivers(jobs, monkeypatch):
    import asyncio

    # stub recipe resolution + run + delivery so we exercise fire_job's wiring
    async def _fake_resolve(job):
        return {"nodes": [{"id": "n1", "type": "input"}], "edges": []}, None

    async def _fake_run(recipe, run_input, owner=None):
        assert run_input == "NVDA"
        return {"ok": True, "final": "BUY — high confidence."}

    delivered = {}

    async def _fake_deliver(job, final):
        delivered["final"] = final
        delivered["output"] = job["output"]["kind"]

    monkeypatch.setattr(jobs, "_resolve_graph", _fake_resolve)
    import src.recipes as recipes_engine
    monkeypatch.setattr(recipes_engine, "run_recipe", _fake_run)
    monkeypatch.setattr(jobs, "_deliver", _fake_deliver)

    rec = jobs.save_job(_canned())
    result = asyncio.run(jobs.fire_job(rec))
    assert result == {"ok": True, "final": "BUY — high confidence."}
    assert delivered["final"] == "BUY — high confidence." and delivered["output"] == "notify"
    # the run was recorded on the job
    saved = jobs.get_job(rec["id"])
    assert saved["last_run"]["ok"] is True and saved["last_run"]["summary"] == "BUY — high confidence."


def test_fire_job_records_failure(jobs, monkeypatch):
    import asyncio

    async def _resolve_err(job):
        return None, "Enable the Market Analysis tools for this automation."

    monkeypatch.setattr(jobs, "_resolve_graph", _resolve_err)
    rec = jobs.save_job(_canned())
    result = asyncio.run(jobs.fire_job(rec))
    assert result["ok"] is False and "Market Analysis" in result["error"]
    assert jobs.get_job(rec["id"])["last_run"]["ok"] is False


def test_deliver_notify_uses_scheduler(jobs, monkeypatch):
    import asyncio

    class _FakeSched:
        def __init__(self): self.calls = []
        def add_notification(self, name, status, owner=None, body=None):
            self.calls.append((name, status, owner, body))

    fake = _FakeSched()
    import src.event_bus as eb
    monkeypatch.setattr(eb, "get_task_scheduler", lambda: fake)
    rec = jobs.save_job(_canned(output={"kind": "notify"}))
    asyncio.run(jobs._deliver(rec, "the result"))
    assert fake.calls and fake.calls[0][0] == "Daily NVDA" and fake.calls[0][3] == "the result"
