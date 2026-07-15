"""Recipes library routes: catalog, canned-run, RBAC, and the toolbox 409 gate."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.recipes_routes as rr


@pytest.fixture
def client(monkeypatch):
    # Auth off: admin + run access pass through (single-user).
    monkeypatch.setattr(rr, "require_admin", lambda request: "")
    import src.auth_helpers as ah
    monkeypatch.setattr(ah, "require_privilege", lambda request, key: "")
    app = FastAPI()
    app.include_router(rr.setup_recipes_routes())
    return TestClient(app)


def test_catalog_lists_recipes_with_metadata(client):
    r = client.get("/api/recipes/catalog")
    assert r.status_code == 200
    d = r.json()
    ids = {e["id"] for e in d["recipes"]}
    assert {"summarize", "stock", "inbox_declutter"} <= ids
    assert "Analyze" in d["categories"]
    # can_author reflects admin (single-user → True)
    assert d["can_author"] is True
    stock = next(e for e in d["recipes"] if e["id"] == "stock")
    assert stock["name"] == "Stock analysis — Bull / Base / Bear"
    assert stock["needs"] and stock["needs"][0]["toolbox"] == "market"


def test_run_preview_recipe_409(client):
    r = client.post("/api/recipes/catalog/morning_brief/run", json={"input": ""})
    assert r.status_code == 409


def test_run_unknown_recipe_404(client):
    r = client.post("/api/recipes/catalog/nope/run", json={"input": "x"})
    assert r.status_code == 404


def test_run_gated_recipe_returns_toolbox_409(client):
    # Market toolbox not connected → structured 409 the UI turns into "Enable".
    r = client.post("/api/recipes/catalog/stock/run", json={"input": "NVDA"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "toolbox_disabled"
    assert detail["toolbox"] == "market"


def test_run_model_only_recipe_executes(client, monkeypatch):
    # Give it a model + stub the engine so we exercise the route, not a live model.
    import src.recipe_templates as rt
    monkeypatch.setattr(rt, "best_model", lambda models: "qwen3-coder-30b")

    async def _fake_run(recipe, run_input, owner=None):
        assert recipe["nodes"] and run_input == "hello world"
        return {"ok": True, "final": "5 bullets…", "steps": [
            {"id": "n2", "type": "model", "label": None, "output": "5 bullets…"}]}

    monkeypatch.setattr(rr.recipes_engine, "run_recipe", _fake_run)
    r = client.post("/api/recipes/catalog/summarize/run", json={"input": "hello world"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "bullets" in r.json()["final"]


def test_run_model_only_without_model_400(client, monkeypatch):
    import src.recipe_templates as rt
    monkeypatch.setattr(rt, "best_model", lambda models: None)
    r = client.post("/api/recipes/catalog/summarize/run", json={"input": "x"})
    assert r.status_code == 400


def test_enable_unknown_toolbox_400(client):
    r = client.post("/api/recipes/toolbox/enable", json={"toolbox": "bogus"})
    assert r.status_code == 400
