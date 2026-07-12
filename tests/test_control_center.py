"""Smoke tests for the Control Center aggregator — src/control_center.py.

It probes many subsystems (doctor, engine, DB, settings, MCP), so the contract
that matters is: it always returns a well-formed, guarded snapshot even when
sources are missing, and never raises.
"""
from src import control_center as cc

_VALID_STATUS = {"ok", "warn", "off", "error"}
_VALID_ACTION = {"command", "panel", "chat", "settings", "none"}


def test_snapshot_shape_and_guards():
    snap = cc.snapshot()
    assert isinstance(snap, dict)
    assert "groups" in snap and "summary" in snap
    for g in snap["groups"]:
        assert g.get("title")
        assert isinstance(g.get("items"), list) and g["items"]
        for it in g["items"]:
            assert it["key"] and it["name"]
            assert it["status"] in _VALID_STATUS
            assert it["action"]["type"] in _VALID_ACTION


def test_summary_counts_are_consistent():
    snap = cc.snapshot()
    total = sum(len(g["items"]) for g in snap["groups"])
    ok = sum(1 for g in snap["groups"] for i in g["items"] if i["status"] == "ok")
    assert snap["summary"]["total"] == total
    assert snap["summary"]["ok"] == ok
    assert snap["summary"]["needs_attention"] == total - ok


def test_helpers_build_valid_items():
    it = cc._item("k", "Name", "ok", "detail", cc._cmd("/engine"))
    assert it["action"] == {"type": "command", "value": "/engine"}
    assert cc._panel("rail-recipes") == {"type": "panel", "value": "rail-recipes"}
    assert cc._chat("hi")["type"] == "chat"


def test_snapshot_never_raises_with_broken_source(monkeypatch):
    # Force a probe to blow up; snapshot must still return a dict.
    monkeypatch.setattr(cc, "_doctor_map", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    snap = cc.snapshot()
    assert isinstance(snap, dict) and "groups" in snap
