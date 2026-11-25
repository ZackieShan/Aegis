"""/api/reference/* — offline library status/search, the same-origin image
proxy and the identify endpoint's request handling.

The image proxy is the security-relevant part: it fetches from a loopback
server on the user's behalf and serves the bytes under Aegis's own origin, so
it must take only ``/content/`` paths and return only raster images.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import reference_routes
from services import identify, library
from services.library import config, kiwix


class _Uploads:
    def __init__(self, files):
        self.files = files
        self.asked = []

    def resolve_upload(self, upload_id, owner=None, auth_manager=None, allow_admin=True):
        self.asked.append((upload_id, owner))
        return self.files.get(upload_id)


@pytest.fixture
def uploads():
    return _Uploads({
        "a" * 32 + ".jpg": {"path": "/data/uploads/photo.jpg", "mime": "image/jpeg"},
        "b" * 32 + ".pdf": {"path": "/data/uploads/doc.pdf", "mime": "application/pdf"},
    })


@pytest.fixture
def client(monkeypatch, uploads):
    monkeypatch.setattr(reference_routes, "require_user", lambda request: "alice")
    monkeypatch.setattr(config, "_settings", lambda: {"kiwix_url": "http://127.0.0.1:8095"})
    app = FastAPI()
    app.include_router(reference_routes.setup_reference_routes(uploads))
    return TestClient(app)


def test_routes_require_a_user(monkeypatch, uploads):
    from fastapi import HTTPException

    def deny(request):
        raise HTTPException(401, "Not authenticated")

    monkeypatch.setattr(reference_routes, "require_user", deny)
    app = FastAPI()
    app.include_router(reference_routes.setup_reference_routes(uploads))
    anonymous = TestClient(app)
    assert anonymous.get("/api/reference/status").status_code == 401
    assert anonymous.get("/api/reference/search?q=x").status_code == 401
    assert anonymous.get("/api/reference/asset?p=/content/a/b.jpg").status_code == 401
    assert anonymous.post("/api/reference/identify", json={"file_id": "x"}).status_code == 401


def test_status_trims_the_catalog_and_hides_server_paths(client, monkeypatch):
    monkeypatch.setattr(library, "status", lambda: {
        "enabled": True, "kiwix_up": True, "collections": 1, "articles": 7, "library_dir": "/home/someone/library",
        "kiwix_url": "http://127.0.0.1:8095", "kiwix_public_url": "https://lib",
        "books": [{"id": "w", "title": "Wikipedia", "summary": "s", "articles": 7, "date": "2026-08-25",
                   "fulltext": True, "tags": ["x"], "dated_id": "w_2026-08"}],
        "manuals": {"built": True, "documents": 3, "pages": 9},
    })
    data = client.get("/api/reference/status").json()
    assert "library_dir" not in data
    assert data["books"] == [{"id": "w", "title": "Wikipedia", "summary": "s", "articles": 7,
                              "date": "2026-08-25", "fulltext": True}]


def test_search_validates_and_shapes_results(client, monkeypatch):
    seen = {}

    def fake_search(query, count, scope, shelf=""):
        seen.update(query=query, count=count, scope=scope)
        return [{"title": "First Aid — page 46", "url": "https://lib/x.pdf#page=46", "snippet": "tourniquet",
                 "book": "armypubs_en_all", "book_title": "Army Publishing Directorate", "kind": "manual",
                 "page": 46, "doc_id": 3, "shelf": "Survival/Medical & first aid"}]

    monkeypatch.setattr(library, "library_search", fake_search)
    monkeypatch.setattr(library, "available", lambda: True)
    assert client.get("/api/reference/search").status_code == 400
    data = client.get("/api/reference/search", params={"q": " tourniquet ", "scope": "bogus", "count": 999}).json()
    assert seen == {"query": "tourniquet", "count": 25, "scope": "all"}
    assert data["results"] == [{"title": "First Aid — page 46", "url": "https://lib/x.pdf#page=46",
                                "snippet": "tourniquet", "source": "Army Publishing Directorate",
                                "kind": "manual", "page": 46, "shelf": "Survival/Medical & first aid",
                                "shelf_name": "Survival › Medical & first aid", "by": "", "dates": "", "form": ""}]
    assert data["shelf"] == ""


@pytest.mark.parametrize("path", [
    "", "/search?pattern=x", "/catalog/v2/entries", "http://169.254.169.254/latest/meta-data",
    "/content/../catalog/v2/root.xml", "/content/a/../../skin/x.js", "content/a/b.jpg",
    "/content/" + "a" * 1100,
])
def test_asset_proxy_takes_only_content_paths(client, monkeypatch, path):
    monkeypatch.setattr(kiwix, "fetch", lambda *a, **k: pytest.fail("must not fetch"))
    assert client.get("/api/reference/asset", params={"p": path}).status_code == 400


def test_asset_proxy_serves_only_images_and_nothing_that_can_run(client, monkeypatch):
    fetched = []

    def fake_fetch(url, max_bytes=0):
        fetched.append(url)
        if url.endswith(".jpg"):
            return ("image/jpeg", b"\xff\xd8jpeg")
        if url.endswith(".svg"):
            return ("image/svg+xml", b"<svg onload=alert(1)>")
        if url.endswith("/Article"):
            return ("text/html; charset=utf-8", b"<script>alert(1)</script>")
        return None

    monkeypatch.setattr(kiwix, "fetch", fake_fetch)
    ok = client.get("/api/reference/asset", params={"p": "/content/w/_assets_/a/b.jpg?x=1#frag"})
    assert ok.status_code == 200 and ok.content == b"\xff\xd8jpeg"
    assert ok.headers["content-type"] == "image/jpeg" and ok.headers["x-content-type-options"] == "nosniff"
    assert fetched == ["http://127.0.0.1:8095/content/w/_assets_/a/b.jpg"]      # internal base, query dropped
    # Every answer is inert if it is ever opened as a page.
    assert ok.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    # An SVG (a formula, a diagram) is a picture that can carry script: it is
    # served to an <img>, never as a page of its own.
    as_picture = client.get("/api/reference/asset", params={"p": "/content/w/logo.svg"}, headers={"Sec-Fetch-Dest": "image"})
    assert as_picture.status_code == 200 and as_picture.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in as_picture.headers["content-security-policy"] and "default-src 'none'" in as_picture.headers["content-security-policy"]
    assert as_picture.headers["x-content-type-options"] == "nosniff" and "Sec-Fetch-Dest" in as_picture.headers["vary"]
    for destination in ("document", "iframe", "object", "embed", "empty"):
        refused = client.get("/api/reference/asset", params={"p": "/content/w/logo.svg"}, headers={"Sec-Fetch-Dest": destination})
        assert refused.status_code == 415, destination
    old_browser = client.get("/api/reference/asset", params={"p": "/content/w/logo.svg"})        # says nothing about what it is loading
    assert old_browser.status_code == 200 and "sandbox" in old_browser.headers["content-security-policy"]
    assert client.get("/api/reference/asset", params={"p": "/content/w/Article"}).status_code == 415
    assert client.get("/api/reference/asset", params={"p": "/content/w/missing.png"}).status_code == 404


def test_identify_checks_the_upload_and_passes_the_owner(client, monkeypatch, uploads):
    seen = {}

    async def fake_identify(path, hint="", owner=None, want=""):
        seen.update(path=path, hint=hint, owner=owner, want=want)
        return {"markdown": "**Most likely:** Death cap", "sources": []}

    monkeypatch.setattr(identify, "identify_image", fake_identify)
    photo = "a" * 32 + ".jpg"
    data = client.post("/api/reference/identify", json={"file_id": photo, "hint": " under oaks " + "x" * 600}).json()
    assert data["markdown"].startswith("**Most likely:**")
    assert seen["path"] == "/data/uploads/photo.jpg" and seen["owner"] == "alice"
    assert seen["hint"].startswith("under oaks") and len(seen["hint"]) <= 500
    assert uploads.asked[-1] == (photo, "alice")                     # owner-checked lookup
    assert seen["want"] == ""                                        # nobody has to say what kind of photo it is
    client.post("/api/reference/identify", json={"file_id": photo, "want": "text"})
    assert seen["want"] == "text"                                    # …but they can correct the sorting
    assert client.post("/api/reference/identify", json={"file_id": photo, "want": "x-ray"}).status_code == 400

    assert client.post("/api/reference/identify", json={}).status_code == 400
    assert client.post("/api/reference/identify", json=["x"]).status_code == 400
    assert client.post("/api/reference/identify", content=b"not json").status_code == 400
    assert client.post("/api/reference/identify", json={"file_id": "c" * 32 + ".jpg"}).status_code == 404
    assert client.post("/api/reference/identify", json={"file_id": "b" * 32 + ".pdf"}).status_code == 400


def test_identify_errors_keep_their_status(client, monkeypatch):
    async def unavailable(path, hint="", owner=None, want=""):
        raise identify.IdentifyError("No vision model is configured (Settings → Vision).", 503)

    monkeypatch.setattr(identify, "identify_image", unavailable)
    response = client.post("/api/reference/identify", json={"file_id": "a" * 32 + ".jpg"})
    assert response.status_code == 503 and "vision model" in response.json()["detail"].lower()
