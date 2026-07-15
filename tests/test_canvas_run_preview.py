"""Canvas run-preview endpoints + generator steering (2026-07-14).

Run executes HTML/JS via /api/canvas/preview/{token} so the middleware can
serve it under a CSP `sandbox` policy — srcdoc/blob iframes inherit the app's
nonce CSP, which blocks generated pages' inline scripts. Python runs in a
Web Worker with a locally-vendored Pyodide (freeze-proof + offline).
"""
import inspect

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.canvas_routes as cr


@pytest.fixture
def client(monkeypatch):
    # Route auth passthrough (single-user mode returns "").
    monkeypatch.setattr(cr, "require_user", lambda request: "")
    app = FastAPI()
    app.include_router(cr.setup_canvas_routes())
    cr._previews.clear()
    return TestClient(app)


def test_preview_roundtrip(client):
    r = client.post("/api/canvas/preview", json={"code": "<h1>hi</h1><script>1</script>"})
    assert r.status_code == 200
    url = r.json()["url"]
    r2 = client.get(url)
    assert r2.status_code == 200
    assert r2.text == "<h1>hi</h1><script>1</script>"
    assert "text/html" in r2.headers["content-type"]


def test_preview_rejects_empty_and_huge(client):
    assert client.post("/api/canvas/preview", json={"code": "  "}).status_code == 400
    big = "x" * (cr._PREVIEW_MAX_BYTES + 1)
    assert client.post("/api/canvas/preview", json={"code": big}).status_code == 413


def test_preview_expiry_and_cap(client, monkeypatch):
    r = client.post("/api/canvas/preview", json={"code": "<p>old</p>"})
    token = r.json()["token"]
    # age it past the TTL → 404 on fetch
    cr._previews[token]["ts"] -= cr._PREVIEW_TTL_S + 1
    assert client.get(f"/api/canvas/preview/{token}").status_code == 404
    # the store never grows past the cap
    for i in range(cr._PREVIEW_MAX + 10):
        client.post("/api/canvas/preview", json={"code": f"<p>{i}</p>"})
    assert len(cr._previews) <= cr._PREVIEW_MAX


def test_unknown_token_404(client):
    assert client.get("/api/canvas/preview/deadbeef").status_code == 404


def test_middleware_carves_out_sandbox_csp():
    """The preview path gets the opaque-origin sandbox policy, not the nonce CSP."""
    import core.middleware as mw
    src = inspect.getsource(mw.SecurityHeadersMiddleware.dispatch)
    assert "/api/canvas/preview/" in src
    assert "sandbox allow-scripts" in src


def test_middleware_grants_wasm_to_py_worker_only():
    """The Python worker needs 'wasm-unsafe-eval' to compile Pyodide's wasm;
    the app-wide script-src must NOT carry it (scoped to the worker script)."""
    import core.middleware as mw
    src = inspect.getsource(mw.SecurityHeadersMiddleware.dispatch)
    assert "pyRunner.worker.js" in src
    assert "wasm-unsafe-eval" in src
    # the general (else-branch) app CSP stays wasm-free
    app_csp_start = src.index('"default-src \'self\'; "\n                f"script-src \'self\' \'nonce-')
    app_csp = src[app_csp_start:app_csp_start + 200]
    assert "wasm-unsafe-eval" not in app_csp


def test_generator_steers_visual_requests_to_html():
    """The canvas generate prompt must route games/UI to single-file HTML —
    the browser runtime has no display for pygame/tkinter."""
    from src import canvas
    src = inspect.getsource(canvas.generate)
    for needle in ("Pyodide", "pygame", "html", "<canvas>"):
        assert needle in src, needle
