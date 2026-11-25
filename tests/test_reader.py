"""The reader: library pages made safe to show on Aegis's own page, a
person's shelf, their highlights, and handing a highlight to memory.

The promises under test:
  * nothing from a library page can run or reach out once it is shown here —
    no script, no handlers, no styles, no frames; pictures come through
    Aegis, links to the same collection open in the reader;
  * only the library can be opened: never an arbitrary address, never a path
    that climbs out of a collection;
  * a document's pages come back as paragraphs, and as the printed page;
  * a highlight belongs to one account, and one given to memory is a note
    there that says where it came from.
"""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

fitz = pytest.importorskip("fitz")        # PyMuPDF: page text and page pictures

KIWIX = "http://kiwix.test"
PUBLIC = "https://library.example:8443"

ARTICLE = """<!doctype html><html><head><title>Water purification - Wikipedia</title>
<script>steal()</script><style>body{display:none}</style><link rel="stylesheet" href="x.css"></head>
<body><nav>Menu <a href="Main_Page">Home</a></nav>
<div id="mw-content-text">
  <h1>Water purification</h1>
  <p onclick="steal()" style="position:fixed">Boiling <b>kills</b> most pathogens.<sup class="reference"><a href="#cite_note-1">[1]</a></sup>
     See <a href="Chlorination" title="Chlorination">chlorination</a>, <a href="../other_book/Page">another book</a>,
     <a href="https://example.org/filters">a filter guide</a>, <a href="javascript:steal()">a trick</a> and <a href="#History">history</a>.</p>
  <span class="mw-editsection">[edit]</span>
  <figure><img src="I/pot.jpg.webp" alt="A pot" width="220" height="140" onerror="steal()" srcset="x 2x"><figcaption>A pot</figcaption></figure>
  <p>Formula: <img class="mwe-math-fallback-image-inline" src="./I/math.svg" style="width:10ex" width="12ex"> and a tracker
     <img src="https://evil.example/pixel.gif"> and <img src="data:image/png;base64,AAAA">.</p>
  <iframe src="https://evil.example"></iframe><form action="/api/admin/users"><input name="x"><button>Go</button></form>
  <table class="navbox"><tr><td>Navigation junk</td></tr></table>
  <table><tr><th colspan="2">Method</th></tr><tr><td>Boil</td><td>1 minute</td></tr></table>
  <h2 id="History">History</h2><p>Long ago, people let water settle before drinking it, and the practice spread widely.</p>
</div><footer>Footer</footer></body></html>"""


def _pdf(pages, title=""):
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 96), text, fontsize=11)
    if title:
        doc.set_metadata({"title": title})
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def reader(tmp_path, monkeypatch):
    """The reader over an empty database, a library folder of its own and a
    stand-in for the Kiwix server."""
    import core.database as cdb
    import services.home  # noqa: F401  its tables are part of what an account owns
    from services.library import additions, config, kiwix
    from services.reader import content, kitchen, shelf

    engine = create_engine(f"sqlite:///{tmp_path / 'reader.db'}")
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(shelf, "SessionLocal", factory)
    monkeypatch.setattr(kitchen, "SessionLocal", factory)
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "library_dir", lambda: tmp_path / "library")
    monkeypatch.setattr(config, "enabled", lambda: True)
    monkeypatch.setattr(config, "kiwix_url", lambda: KIWIX)
    monkeypatch.setattr(config, "kiwix_public_url", lambda: PUBLIC)
    monkeypatch.setattr(kiwix, "available", lambda force=False: True)
    monkeypatch.setattr(kiwix, "book_titles", lambda: {})
    monkeypatch.setattr(kiwix, "list_books", lambda force=False: [{"id": "wikipedia_en_all", "title": "Wikipedia", "summary": "", "articles": 7}])
    monkeypatch.setattr(additions, "_start_ocr", lambda relative: True)
    (tmp_path / "memory").mkdir()
    pages = {f"{KIWIX}/content/wikipedia_en_all/Water_purification": ("text/html; charset=utf-8", ARTICLE.encode(), None)}
    asked = []

    def fetch_final(url, max_bytes=0, whole=False):
        asked.append(url)
        if not url.startswith(KIWIX + "/content/"):
            return None                                   # what the real one does for anything that is not the library
        found = pages.get(url)
        if found is None:
            return None
        return found[0], found[1], found[2] or url

    monkeypatch.setattr(kiwix, "fetch_final", fetch_final)
    return type("Reader", (), {"content": content, "shelf": shelf, "kitchen": kitchen, "add": additions, "pages": pages,
                               "asked": asked, "db": factory, "root": tmp_path})


# ── what a library page is cut down to ─────────────────────────────────────

def test_a_library_page_is_made_safe_and_plain(reader):
    title, html = reader.content.clean_article(ARTICLE, "wikipedia_en_all", "Water_purification")
    assert title == "Water purification"
    lowered = html.lower()
    for gone in ("<script", "steal()", "<style", "onclick", "onerror", "style=", "<iframe", "<form", "<input", "<button",
                 "javascript:", "evil.example", "data:image", "srcset", "navigation junk", "[edit]", "menu", "footer", "[1]"):
        assert gone not in lowered, gone
    assert "Boiling <b>kills</b> most pathogens." in html
    assert "<h1" not in html                                            # the reader shows the title itself
    # links: the same collection opens in the reader, the web is marked, everything else is plain text
    assert 'href="/read?b=wikipedia_en_all&amp;p=Chlorination"' in html and 'data-kind="library"' in html
    assert 'href="https://example.org/filters"' in html and 'data-kind="web"' in html and 'rel="noopener noreferrer"' in html
    assert "another book" in html and "other_book" not in html
    assert "a trick" in html and "history" in html and html.count("<a ") == 2
    # pictures come through Aegis, line art is marked so the page can invert it on dark paper
    assert 'src="/api/reference/asset?p=/content/wikipedia_en_all/I/pot.jpg.webp"' in html and 'width="220"' in html
    assert 'src="/api/reference/asset?p=/content/wikipedia_en_all/I/math.svg"' in html and 'class="drawn"' in html
    assert 'width="12ex"' not in html and html.count("<img") == 2
    assert '<th colspan="2">Method</th>' in html and "<h2>History</h2>" in html


@pytest.mark.parametrize("page", [
    '<div id="content"><p>A</p><img src="x" onerror=alert(1)><svg onload=alert(1)><script>alert(1)</script></svg>'
    '<math><mtext><script>alert(1)</script></mtext></math><a href="JaVaScRiPt:alert(1)">x</a>'
    '<a href="data:text/html,<script>alert(1)</script>">y</a><object data="x"></object><embed src="x">'
    '<p style="background:url(javascript:alert(1))">z</p><base href="https://evil.example/"></div>',
])
def test_nothing_that_runs_survives(reader, page):
    _title, html = reader.content.clean_article(page, "book", "Page")
    lowered = html.lower()
    for gone in ("<script", "alert(1)", "onerror", "onload", "javascript:", "data:text", "<object", "<embed", "<svg", "<base", "style="):
        assert gone not in lowered, gone


@pytest.mark.parametrize("asked, expected", [
    ({"url": f"{PUBLIC}/content/wikipedia_en_all/Water_purification"}, ("article", "wikipedia_en_all", "Water_purification", 0)),
    ({"url": f"{KIWIX}/content/wikipedia_en_all_2026-01/Water_purification"}, ("article", "wikipedia_en_all", "Water_purification", 0)),
    ({"url": f"{PUBLIC}/content/manuals/water/FM_21-10.pdf#page=14"}, ("manual", "manuals", "water/FM_21-10.pdf", 14)),
    ({"url": "/api/reference/added/water/epa.pdf#page=2"}, ("manual", "aegis-added", "water/epa.pdf", 2)),
    ({"url": "/read?b=manuals&p=water%2FFM_21-10.pdf&page=3"}, ("manual", "manuals", "water/FM_21-10.pdf", 3)),
    ({"url": "/read?u=%2Fapi%2Freference%2Fadded%2Fwater%2Fepa.pdf%23page%3D2"}, ("manual", "aegis-added", "water/epa.pdf", 2)),
    ({"book": "wikipedia_en_all", "path": ""}, ("article", "wikipedia_en_all", "", 0)),
])
def test_a_link_into_the_library_says_what_to_open(reader, asked, expected):
    found = reader.content.resolve(**asked)
    assert (found["kind"], found["book"], found["path"], found["page"]) == expected


@pytest.mark.parametrize("asked", [
    {"url": "https://example.com/content/not_a_collection/Water"},
    {"url": "http://169.254.169.254/latest/meta-data"},
    {"url": "http://169.254.169.254/content/latest/meta-data"},
    {"url": f"{PUBLIC}/search?pattern=x"},
    {"url": "/read?b=manuals"},
    {"url": "/read?u=%2Fread%3Fu%3D%2Fread"},
    {"book": "manuals", "path": "../../etc/passwd"},
    {"book": "a/b", "path": "x"},
    {"book": "", "path": "x"},
])
def test_only_the_library_can_be_opened(reader, asked):
    with pytest.raises(reader.content.ReaderError):
        reader.content.resolve(**asked)
    with pytest.raises(reader.content.ReaderError):
        reader.shelf.open_document("alice", **asked)
    assert reader.shelf.shelf("alice")["recent"] == []
    assert all(url.startswith(KIWIX + "/content/") for url in reader.asked)


def test_a_link_written_with_another_address_still_reads_from_the_library_server(reader):
    """Only the collection and the path are taken from a link. Whatever host
    it names, the page comes from the library server — nothing else is ever
    fetched."""
    opened = reader.shelf.open_document("alice", url="https://old-name.example:9999/content/wikipedia_en_all/Water_purification")
    assert opened["item"]["title"] == "Water purification"
    assert reader.asked == [f"{KIWIX}/content/wikipedia_en_all/Water_purification"]


def test_a_page_that_is_only_a_script_menu_says_so(reader):
    reader.pages[f"{KIWIX}/content/knots/"] = ("text/html", b"<html><body><script src='app.js'></script><div>Loading\xe2\x80\xa6 No result for this search request. Loading\xe2\x80\xa6</div></body></html>",
                                               f"{KIWIX}/content/knots/home")
    with pytest.raises(reader.content.ReaderError, match="only works in the library's own viewer"):
        reader.content.article("knots", "")
    assert reader.content.original_for(book="knots") == f"{PUBLIC}/content/knots/"
    assert reader.content.original_for(url="https://example.com/x") == ""


# ── a document's pages ─────────────────────────────────────────────────────

def test_a_page_of_print_becomes_paragraphs(reader):
    page = ("Chapter 2 \n2-22\n\x14\x1b\x036HSW\x03\x15\x13\x14\x1b\nz\nMedicinal uses. A poultice of the leaves from common plantain, woundwort or common\n"
            "yarrow can be applied to wounds, stings, and sores in order to help the body to facil-\nitate healing of the damaged skin and tissue and, with pressure, to "
            "stop bleeding.\n"
            "Preparation \nz\nBrew tea from seeds.\n• Brew tea from leaves.\n\nThe plant is native to most of Europe and northern and central Asia, but has widely\n"
            "naturalised elsewhere in the world, and it is one of the most abundant and widely\ndistributed medicinal crops there is.")
    blocks = reader.content.reflow(page)
    assert [b["k"] for b in blocks] == ["h", "p", "li", "h", "li", "li", "p"]
    assert blocks[0]["t"] == "Chapter 2" and blocks[3]["t"] == "Preparation"
    assert blocks[2]["t"] == ("Medicinal uses. A poultice of the leaves from common plantain, woundwort or common yarrow can be applied "
                              "to wounds, stings, and sores in order to help the body to facilitate healing of the damaged skin and tissue and, "
                              "with pressure, to stop bleeding.")
    assert blocks[5]["t"] == "Brew tea from leaves."
    assert blocks[6]["t"].startswith("The plant is native") and blocks[6]["t"].endswith("medicinal crops there is.")
    assert not any("HSW" in b["t"] or "\x14" in b["t"] for b in blocks)        # a line whose letters did not survive is left out
    assert reader.content.reflow("") == [] and reader.content.reflow(None) == []


def test_a_document_is_read_page_by_page_and_shown_as_printed(reader):
    entry = reader.add.add_pdf(_pdf(["Boil water for one minute to make it safe.", "", "Store it in clean containers with covers."],
                                    title="Emergency Water"), name="water.pdf", section="Water")
    opened = reader.shelf.open_document("alice", url=entry["url"] + "#page=2")
    item = opened["item"]
    assert item["kind"] == "manual" and item["title"] == "Emergency Water" and item["pages"] == 3 and item["page"] == 2
    assert [p["n"] for p in opened["content"]["pages"]] == [2, 3]
    assert opened["content"]["pages"][0]["blocks"] == []                                   # a page with no text
    assert opened["content"]["pages"][1]["blocks"] == [{"k": "p", "t": "Store it in clean containers with covers."}]
    assert opened["content"]["original"] == "/api/reference/added/water/water.pdf#page=2"
    assert item["link"] == "/read?b=aegis-added&p=water/water.pdf&page=2"
    earlier = reader.shelf.more_pages("alice", item["id"], 1, 1)
    assert earlier["pages"][0]["blocks"][0]["t"] == "Boil water for one minute to make it safe."

    picture = reader.content.page_image("aegis-added", "water/water.pdf", 1, width=600)
    assert picture[:3] == b"\xff\xd8\xff" and len(picture) > 2000                          # a JPEG of the page
    with pytest.raises(reader.content.ReaderError, match="no such page"):
        reader.content.page_image("aegis-added", "water/water.pdf", 9)
    with pytest.raises(reader.content.ReaderError):
        reader.content.page_image("aegis-added", "../../etc/passwd", 1)

    # reopened without a page, it is where they left it; the place can be moved
    assert reader.shelf.open_document("alice", book="aegis-added", path="water/water.pdf")["item"]["page"] == 2
    assert reader.shelf.update_item("alice", item["id"], {"page": 99})["page"] == 3
    assert reader.shelf.update_item("alice", item["id"], {"page": True, "position": "x"})["page"] == 3


def test_a_document_out_of_a_collection_is_fetched_once_for_its_pictures(reader):
    reader.pages[f"{KIWIX}/content/manuals/water/guide.pdf"] = ("application/pdf", _pdf(["Filter cloudy water through cloth."]), None)
    first = reader.content.page_image("manuals", "water/guide.pdf", 1, width=400)
    again = reader.content.page_image("manuals", "water/guide.pdf", 1, width=400)
    assert first[:3] == b"\xff\xd8\xff" and again == first
    assert reader.asked.count(f"{KIWIX}/content/manuals/water/guide.pdf") == 1             # kept on disk after the first time
    assert len(list((reader.root / "reader_cache").glob("*.pdf"))) == 1
    reader.pages[f"{KIWIX}/content/manuals/water/not.pdf"] = ("text/html", b"<html>not a pdf</html>", None)
    with pytest.raises(reader.content.ReaderError):
        reader.content.page_image("manuals", "water/not.pdf", 1)


def test_a_document_that_was_never_indexed_points_to_the_original(reader):
    with pytest.raises(reader.content.ReaderError, match="Open the original"):
        reader.shelf.open_document("alice", url=f"{PUBLIC}/content/manuals/water/unknown.pdf")
    assert reader.content.original_for(url=f"{PUBLIC}/content/manuals/water/unknown.pdf#page=4") == f"{PUBLIC}/content/manuals/water/unknown.pdf#page=4"


# ── the shelf and what is marked ───────────────────────────────────────────

def _open(reader, owner="alice"):
    return reader.shelf.open_document(owner, url=f"{PUBLIC}/content/wikipedia_en_all/Water_purification")


def test_opening_puts_it_on_the_shelf_once_and_remembers_the_place(reader):
    opened = _open(reader)
    item = opened["item"]
    assert item["kind"] == "article" and item["title"] == "Water purification" and item["link"] == "/read?b=wikipedia_en_all&p=Water_purification"
    assert "Boiling <b>kills</b>" in opened["content"]["html"] and opened["content"]["original"] == f"{PUBLIC}/content/wikipedia_en_all/Water_purification"
    assert reader.shelf.update_item("alice", item["id"], {"position": 0.4, "pinned": True})["position"] == 0.4
    assert reader.shelf.update_item("alice", item["id"], {"position": 7})["position"] == 1.0
    again = reader.shelf.open_document("alice", book="wikipedia_en_all", path="Water_purification")["item"]
    assert again["id"] == item["id"] and again["pinned"] is True
    shelf = reader.shelf.shelf("alice")
    assert [i["title"] for i in shelf["kept"]] == ["Water purification"] and shelf["recent"] == []


def test_times_are_the_moment_it_happened_whatever_the_servers_clock_zone(reader, monkeypatch):
    """Stored times are UTC without a zone. Turned into a timestamp as if they
    were local time they are hours off — on the shelf, "today" read "tomorrow"."""
    import time
    from datetime import datetime, timezone

    from services.reader.models import epoch

    assert epoch(datetime(2026, 11, 22, 23, 30, 0)) == datetime(2026, 11, 22, 23, 30, 0, tzinfo=timezone.utc).timestamp()
    assert epoch(None) is None
    item = _open(reader)["item"]
    mark = reader.shelf.add_mark("alice", item["id"], {"quote": "Boiling kills most pathogens."})
    for stamp in (item["opened"], mark["created"]):
        assert abs(stamp - time.time()) < 60


def test_a_book_is_filed_under_its_catalogue_title_not_its_whole_title_page(reader):
    book = ("<html><head><title>The Project Gutenberg eBook of Housekeeping in Old Virginia</title></head><body>"
            "<h1>Housekeeping In Old Virginia. CONTAINING CONTRIBUTIONS FROM TWO HUNDRED AND FIFTY LADIES IN VIRGINIA AND HER SISTER STATES, "
            "DISTINGUISHED FOR THEIR SKILL IN THE CULINARY ART AND OTHER BRANCHES OF DOMESTIC ECONOMY.</h1>"
            "<p>Jackson Jumbles. 3 teacups sugar. 1 teacup lard. 6 teacups flour. Roll out and bake.</p></body></html>")
    reader.pages[f"{KIWIX}/content/gutenberg_en_lcc-t/books/42450.json"] = ("application/json", b'{"title": "Housekeeping in Old Virginia"}', None)
    reader.pages[f"{KIWIX}/content/gutenberg_en_lcc-t/Housekeeping%20in%20Old%20Virginia.42450"] = ("text/html", book.encode(), None)
    # search finds the book's card; reading wants its text
    opened = reader.shelf.open_document("alice", book="gutenberg_en_lcc-t", path="index/book_42450")
    assert opened["item"]["title"] == "Housekeeping in Old Virginia" and opened["item"]["path"] == "Housekeeping in Old Virginia.42450"
    assert "CONTAINING CONTRIBUTIONS" in opened["content"]["html"] and "Jackson Jumbles" in opened["content"]["html"]     # the title page stays in the text
    again = reader.shelf.open_document("alice", book="gutenberg_en_lcc-t", path="Housekeeping in Old Virginia.42450")
    assert again["item"]["id"] == opened["item"]["id"] and again["item"]["title"] == "Housekeeping in Old Virginia"
    # elsewhere, a heading that long is cut for the title and kept in the text
    title, html = reader.content.clean_article("<html><body><h1>" + "Very long heading " * 12 + "</h1><p>Text.</p></body></html>", "book", "Page")
    assert title.endswith("…") and len(title) <= 81 and "Very long heading" in html


def test_an_article_known_by_another_name_lands_on_one_shelf_entry(reader):
    reader.pages[f"{KIWIX}/content/wikipedia_en_all/Water_treatment"] = ("text/html", ARTICLE.encode(), f"{KIWIX}/content/wikipedia_en_all_2026-01/Water_purification")
    first = _open(reader)["item"]
    second = reader.shelf.open_document("alice", book="wikipedia_en_all", path="Water_treatment")["item"]
    assert second["id"] == first["id"] and second["path"] == "Water_purification"


def test_highlights_are_kept_per_person_and_come_back_with_the_page(reader):
    item = _open(reader)["item"]
    mark = reader.shelf.add_mark("alice", item["id"], {"page": 0, "start": 0, "end": 36, "quote": "Boiling kills most pathogens.", "prefix": "x" * 90,
                                                       "suffix": "y" * 90, "color": "green", "note": "  the short version  "})
    assert mark["color"] == "green" and mark["note"] == "the short version" and len(mark["prefix"]) == 60 and len(mark["suffix"]) == 60
    assert mark["title"] == "Water purification" and mark["link"] == "/read?b=wikipedia_en_all&p=Water_purification"
    assert reader.shelf.add_mark("alice", item["id"], {"quote": "x", "color": "ultraviolet"})["color"] == "yellow"
    for bad in ({"quote": "   "}, {"quote": "q" * 6001}):
        with pytest.raises(reader.content.ReaderError):
            reader.shelf.add_mark("alice", item["id"], bad)
    assert [m["id"] for m in _open(reader)["marks"]][0] == mark["id"] and len(_open(reader)["marks"]) == 2
    assert reader.shelf.update_mark("alice", mark["id"], {"note": "changed", "color": "pink"})["color"] == "pink"
    assert reader.shelf.shelf("alice")["highlight_count"] == 2 and reader.shelf.shelf("alice")["recent"][0]["marks"] == 2

    # nobody else sees, changes or removes them — and their own shelf is their own
    assert _open(reader, "bob")["marks"] == [] and reader.shelf.all_marks("bob") == []
    for attempt in (lambda: reader.shelf.update_mark("bob", mark["id"], {"note": "mine now"}),
                    lambda: reader.shelf.add_mark("bob", item["id"], {"quote": "theirs"}),
                    lambda: reader.shelf.update_item("bob", item["id"], {"pinned": True}),
                    lambda: reader.shelf.more_pages("bob", item["id"], 1)):
        with pytest.raises(reader.content.ReaderError):
            attempt()
    assert reader.shelf.remove_mark("bob", mark["id"]) is False and reader.shelf.remove_item("bob", item["id"]) is False
    assert reader.shelf.update_mark("alice", mark["id"], {})["note"] == "changed"

    text = reader.shelf.as_markdown("alice")
    assert "## Water purification" in text and "> Boiling kills most pathogens." in text and "changed" in text
    assert reader.shelf.as_markdown("bob").strip() == "# Highlights"

    assert reader.shelf.remove_item("alice", item["id"]) is True                           # off the shelf, with its highlights
    assert reader.shelf.all_marks("alice") == [] and reader.shelf.shelf("alice")["highlight_count"] == 0


def test_a_highlight_given_to_memory_is_a_note_that_says_where_it_came_from(reader, tmp_path):
    from services.memory import journal, mind
    from src.memory import MemoryManager

    memory = MemoryManager(str(tmp_path / "memory"))
    item = _open(reader)["item"]
    mark = reader.shelf.add_mark("alice", item["id"], {"page": 0, "start": 0, "end": 30, "quote": "Boiling kills most pathogens.", "note": "for the cabin"})
    result = reader.shelf.share_mark("alice", mark["id"], memory, topic="Water")
    assert result["added"] is True and result["topic"]["name"] == "Water" and result["mark"]["shared_topic"] == "Water" and result["mark"]["shared"]
    topic = mind.get_topic(memory, "alice", result["topic"]["id"])
    note = topic["notes"][0]
    assert note["text"] == "“Boiling kills most pathogens.” — Water purification · My note: for the cabin"
    assert note["by"] == "you" and note["source"] == {"kind": "reading", "title": "Water purification",
                                                      "url": f"/read?b=wikipedia_en_all&p=Water_purification#mark={mark['id']}"}
    assert len(note["text"]) <= mind.NOTE_CHARS
    written = [entry for entry in journal.read(memory, "alice", limit=20) if entry.get("op") == "note_add"]
    assert written and written[0]["cause"]["by"] == "you" and written[0]["cause"].get("detail") == "reader"   # in the history, as their own doing
    assert reader.shelf.share_mark("alice", mark["id"], memory, topic="Water")["added"] is False                  # not twice
    assert mind.list_topics(memory, "bob") == []                                                                   # theirs alone
    with pytest.raises(reader.content.ReaderError):
        reader.shelf.share_mark("bob", mark["id"], memory)

    # with no topic named it is filed under what was being read; a long passage is cut to fit a note
    long = reader.shelf.add_mark("alice", item["id"], {"quote": "Settling, filtering and boiling. " * 30})
    filed = reader.shelf.share_mark("alice", long["id"], memory)
    assert filed["topic"]["name"] == "Water purification"
    kept = mind.get_topic(memory, "alice", filed["topic"]["id"])["notes"][0]["text"]
    assert len(kept) <= mind.NOTE_CHARS and kept.startswith("“Settling, filtering") and "…” — Water purification" in kept


# ── the API ────────────────────────────────────────────────────────────────

@pytest.fixture
def client(reader, monkeypatch, tmp_path):
    import routes.reader_routes as routes
    from src.memory import MemoryManager

    who = {"name": "alice"}
    monkeypatch.setattr(routes, "require_user", lambda request: who["name"])
    app = FastAPI()
    app.include_router(routes.setup_reader_routes(MemoryManager(str(tmp_path / "memory"))))
    browser = TestClient(app)
    browser.who = who
    return browser


def test_the_api_needs_a_signed_in_person(reader, monkeypatch):
    import routes.reader_routes as routes

    def deny(request):
        raise HTTPException(401, "Not authenticated")

    monkeypatch.setattr(routes, "require_user", deny)
    app = FastAPI()
    app.include_router(routes.setup_reader_routes(None))
    anonymous = TestClient(app)
    for method, path in (("get", "/api/reader/where"), ("get", "/api/reader/shelf"), ("get", "/api/reader/open?b=x&p=y"), ("get", "/api/reader/marks"),
                         ("get", "/api/reader/marks/export"), ("get", "/api/reader/collections"), ("get", "/api/reader/collections/x/documents"),
                         ("get", "/api/reader/items/i/pages"), ("get", "/api/reader/items/i/pages/1/image"), ("delete", "/api/reader/items/i"),
                         ("delete", "/api/reader/marks/m"), ("get", "/api/kitchen/recipes"), ("get", "/api/kitchen/recipes/r"),
                         ("delete", "/api/kitchen/recipes/r")):
        assert getattr(anonymous, method)(path).status_code == 401, path
    for path, body in (("/api/reader/items/i/marks", {"quote": "x"}), ("/api/reader/marks/m/remember", {}), ("/api/kitchen/recipes", {"text": "x" * 40}),
                       ("/api/kitchen/recipes/r/redo", {})):
        assert anonymous.post(path, json=body).status_code == 401, path
    assert reader.asked == []


def test_reading_and_marking_through_the_api(client, reader):
    where = client.get("/api/reader/where").json()
    assert where == {"library": True, "base": PUBLIC, "added": "/api/reference/added/", "files": "/api/reference/files/"}
    opened = client.get("/api/reader/open", params={"u": f"{PUBLIC}/content/wikipedia_en_all/Water_purification"}).json()
    item = opened["item"]
    mark = client.post(f"/api/reader/items/{item['id']}/marks", json={"page": 0, "start": 0, "end": 30, "quote": "Boiling kills most pathogens."}).json()
    assert client.patch(f"/api/reader/marks/{mark['id']}", json={"note": "noted"}).json()["note"] == "noted"
    kept = client.post(f"/api/reader/marks/{mark['id']}/remember", json={"topic": "Water"}).json()
    assert kept["added"] is True and kept["topic"]["name"] == "Water"
    export = client.get("/api/reader/marks/export")
    assert export.headers["content-type"].startswith("text/markdown") and "attachment" in export.headers["content-disposition"]
    assert "> Boiling kills most pathogens." in export.text
    assert client.patch(f"/api/reader/items/{item['id']}", json={"pinned": True, "position": 0.5}).json()["pinned"] is True
    assert client.get("/api/reader/shelf").json()["kept"][0]["title"] == "Water purification"

    # what cannot be shown comes back as a reason and the way to the original — not an error page
    refused = client.get("/api/reader/open", params={"u": "https://example.com/content/x/y"}).json()
    assert refused == {"problem": "That is not something from the library.", "original": ""}
    missing = client.get("/api/reader/open", params={"b": "wikipedia_en_all", "p": "Nope"}).json()
    assert missing["problem"] == "That page is not in the library." and missing["original"] == f"{PUBLIC}/content/wikipedia_en_all/Nope"
    assert client.get("/api/reader/open", params={"u": "x" * 2100}).status_code == 400

    client.who["name"] = "bob"                                           # someone else
    assert client.get("/api/reader/shelf").json()["kept"] == [] and client.get("/api/reader/marks").json()["marks"] == []
    assert client.patch(f"/api/reader/marks/{mark['id']}", json={"note": "x"}).status_code == 400
    assert client.delete(f"/api/reader/marks/{mark['id']}").status_code == 404
    assert client.delete(f"/api/reader/items/{item['id']}").status_code == 404
    assert client.get(f"/api/reader/items/{item['id']}/pages/1/image").status_code == 400
    assert client.post(f"/api/reader/marks/{mark['id']}/remember", json={}).status_code == 400


def test_the_library_switched_off_opens_nothing(client, reader, monkeypatch):
    from services.library import config

    monkeypatch.setattr(config, "enabled", lambda: False)
    assert client.get("/api/reader/open", params={"b": "wikipedia_en_all", "p": "Water_purification"}).status_code == 404
    assert client.get("/api/reader/collections").status_code == 404
    assert client.get("/api/reader/where").json() == {"library": False, "base": "", "added": "/api/reference/added/",
                                                      "files": "/api/reference/files/"}
    assert client.get("/api/reader/shelf").json()["library"] is False
    assert reader.asked == []


def test_collections_list_their_documents(client, reader, monkeypatch):
    from services.library import kiwix

    monkeypatch.setattr(kiwix, "list_books", lambda force=False: [
        {"id": "wikipedia_en_all", "title": "Wikipedia", "summary": "The free encyclopedia", "articles": 7000000}])
    reader.add.add_pdf(_pdf(["Boil water."], title="Emergency Water"), name="water.pdf", section="Water")
    reader.add.add_pdf(_pdf(["Smoke calms bees."], title="Apiary Smoker Notes"), name="bees.pdf", section="Homestead")
    listed = client.get("/api/reader/collections").json()["collections"]
    assert [(c["id"], c["documents"]) for c in listed] == [("aegis-added", 2), ("wikipedia_en_all", 0)]
    documents = client.get("/api/reader/collections/aegis-added/documents").json()
    assert documents["total"] == 2 and [d["title"] for d in documents["documents"]] == ["Apiary Smoker Notes", "Emergency Water"]
    assert [d["title"] for d in client.get("/api/reader/collections/aegis-added/documents", params={"q": "water emerg"}).json()["documents"]] == ["Emergency Water"]
    assert client.get("/api/reader/collections/aegis-added/documents", params={"q": "%' OR 1=1 --"}).json()["total"] == 0


def test_the_image_proxy_path_is_locked_down_by_the_middleware_too():
    """core/middleware sets the same nothing-allowed policy for the image
    proxy as the route does, so a missing header on either side still leaves
    a picture that cannot run."""
    from core.middleware import SecurityHeadersMiddleware
    from fastapi.responses import Response

    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/api/reference/asset")
    def asset():
        return Response(content=b"<svg/>", media_type="image/svg+xml")

    @app.get("/read")
    def page():
        return Response(content=b"<html></html>", media_type="text/html")

    browser = TestClient(app)
    policy = browser.get("/api/reference/asset").headers["content-security-policy"]
    assert policy == "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    assert "sandbox" not in browser.get("/read").headers["content-security-policy"]


def test_deleting_an_account_takes_its_shelf_highlights_and_recipes(reader, monkeypatch):
    import core.database as cdb
    from services import account_data
    from services.reader.models import KitchenRecipe, ReaderItem, ReaderMark

    monkeypatch.setattr(cdb, "SessionLocal", reader.db)
    for owner in ("alice", "bob"):
        item = _open(reader, owner)["item"]
        reader.shelf.add_mark(owner, item["id"], {"quote": "Boiling kills most pathogens."})
        db = reader.db()
        db.add(KitchenRecipe(id=f"r-{owner}", owner=owner, title="Jumbles", original="Roll out and bake.", data="{}"))
        db.commit()
        db.close()
    for table in (ReaderItem.__table__, ReaderMark.__table__, KitchenRecipe.__table__):
        assert account_data._their_rows(table, "alice") is not None
    account_data._erase_rows("alice")
    db = reader.db()
    try:
        for model in (ReaderItem, ReaderMark, KitchenRecipe):
            assert [row.owner for row in db.query(model).all()] == ["bob"], model.__tablename__
    finally:
        db.close()
