"""Offline library: text helpers and the kiwix-serve client (no network).

Every HTTP call is stubbed — the session-wide AEGIS_LIBRARY_DISABLED switch in
conftest.py is lifted per test with the ``library_on`` fixture.
"""
import httpx
import pytest

from services.library import config, kiwix
from services.library.text import (
    _stem, best_passages, coverage, html_to_text, query_terms, split_sections,
)


@pytest.fixture
def library_on(monkeypatch):
    monkeypatch.delenv("AEGIS_LIBRARY_DISABLED", raising=False)
    monkeypatch.setattr(config, "_settings", lambda: {
        "library_enabled": True,
        "kiwix_url": "http://127.0.0.1:8095",
        "kiwix_public_url": "https://box.example.ts.net:8443",
    })
    kiwix.reset_cache()
    yield
    kiwix.reset_cache()


# ── text ───────────────────────────────────────────────────────────────────

def test_query_terms_drops_filler_and_possessives():
    assert query_terms("How do I tell water hemlock from Queen Anne's lace?") == [
        "water", "hemlock", "queen", "anne", "lace",
    ]
    assert query_terms("how to build a figure four deadfall trap") == ["figure", "four", "deadfall", "trap"]
    assert query_terms("") == []


@pytest.mark.parametrize("a,b", [
    ("purifying", "purification"), ("trapping", "traps"), ("trapper", "trap"),
    ("poisonous", "poison"), ("identification", "identify"), ("toxicity", "toxic"),
    ("berries", "berry"), ("anne's", "anne"),
])
def test_stem_brings_word_forms_together(a, b):
    assert _stem(a) == _stem(b)


def test_html_to_text_keeps_headings_lists_and_infobox_rows():
    html = """<html><head><title>Death cap</title><style>p{}</style></head><body>
      <div id="mw-content-text">
        <table class="infobox"><tr><td>Edibility</td><td>deadly</td></tr></table>
        <p>The death cap is a <b>deadly</b> fungus.<sup class="reference">[1]</sup></p>
        <h2>Similar species<span class="mw-editsection">[edit]</span></h2>
        <ul><li>Paddy straw mushroom</li><li>Caesar's mushroom</li></ul>
        <h2>References</h2><ol class="references"><li>Some book</li></ol>
        <script>alert(1)</script>
      </div></body></html>"""
    title, text = html_to_text(html)
    assert title == "Death cap"
    assert "Edibility | deadly" in text
    assert "deadly fungus." in text and "[1]" not in text
    assert "## Similar species" in text and "[edit]" not in text
    assert "- Paddy straw mushroom" in text
    assert "alert(1)" not in text
    wrapped = "<p>A paragraph wrapped\n   in the source\nstays one line.</p><pre>keep\nthis</pre>"
    assert html_to_text(wrapped)[1] == "A paragraph wrapped in the source stays one line.\n\nkeep\nthis"
    headings = [heading for heading, _body in split_sections(text)]
    assert headings[:2] == ["", "Similar species"]


def test_best_passages_keeps_lead_and_the_matching_section():
    filler = "Nothing relevant here. " * 80
    text = (
        "Lead paragraph about the species.\n\n## Taxonomy\n" + filler
        + "\n\n## Toxicity\nAmatoxins destroy the liver; half a cap can kill.\n\n## References\n" + filler
    )
    out = best_passages(text, "how toxic is it", budget=900)
    assert out.startswith("Lead paragraph")
    assert "## Toxicity" in out and "half a cap can kill" in out
    assert "## Taxonomy" not in out and "## References" not in out
    assert len(out) < 1200
    assert best_passages("short text", "anything", budget=900) == "short text"


def test_best_passages_gives_question_pages_to_the_answers():
    text = (
        "- Questions\n- Tags\n\n## What to do if bitten by a copperhead when alone\n" + "Long question text. " * 60
        + "\n\n## 2 Answers 2\n" + "Keep the limb still and get to care; do not cut or suck the wound. " * 40
        + "\n\n## Linked\nOther questions"
    )
    out = best_passages(text, "copperhead bite alone", budget=2000)
    assert out.startswith("## Question: What to do if bitten by a copperhead when alone")
    question, answers = out.split("## 2 Answers 2")
    assert len(question) < 700                      # the question is quoted briefly…
    assert answers.count("do not cut or suck the wound") >= 10      # …the answers get the room
    assert "- Questions" not in out and len(out) <= 2100
    # A page with no headings at all is simply cut at the budget.
    plain = "word " * 2000
    assert len(best_passages(plain, "anything", budget=900)) <= 905


def test_coverage_is_stem_matched_fraction():
    assert coverage("Purifying water with bleach", ["purification", "water", "iodine"]) == pytest.approx(2 / 3)
    assert coverage("anything", []) == 0.0


# ── kiwix client: URLs ─────────────────────────────────────────────────────

def test_public_url_drops_the_date_and_escapes_parentheses(library_on):
    url = kiwix.public_url("/content/wikipedia_en_all_maxi_2026-08/Field_dressing_(hunting)")
    assert url == "https://box.example.ts.net:8443/content/wikipedia_en_all_maxi/Field_dressing_%28hunting%29"
    assert kiwix.strip_date("zimgit-water_en_2024-08") == "zimgit-water_en"
    assert kiwix.strip_date("archlinux_en_all_maxi") == "archlinux_en_all_maxi"


def test_only_content_urls_under_the_configured_bases_are_fetchable(library_on):
    public = "https://box.example.ts.net:8443/content/wikipedia_en_all_maxi/Tourniquet"
    assert kiwix.owns_url(public)
    assert kiwix.to_internal(public) == "http://127.0.0.1:8095/content/wikipedia_en_all_maxi/Tourniquet"
    assert kiwix.to_internal(public + "?x=1#frag") == "http://127.0.0.1:8095/content/wikipedia_en_all_maxi/Tourniquet"
    # Other hosts, other trees and path tricks are refused — this client is
    # the one place allowed to fetch loopback URLs.
    for bad in (
        "https://evil.example/content/wikipedia_en_all_maxi/Tourniquet",
        "https://box.example.ts.net:8443/search?pattern=x",
        "https://box.example.ts.net:8443/content/../catalog",
        "http://127.0.0.1:9090/content/x/y",
        "", None,
    ):
        assert not kiwix.owns_url(bad) or kiwix.to_internal(bad) is None
    assert kiwix.fetch("https://evil.example/content/a/b") is None


def test_split_content_path(library_on):
    assert kiwix.split_content_path("/content/wikipedia_en_all_maxi_2026-08/Bow_drill") == (
        "wikipedia_en_all_maxi", "Bow_drill")
    assert kiwix.split_content_path("https://h/content/zimgit-water_en/files/Water%20(2).pdf#page=3") == (
        "zimgit-water_en", "files/Water (2).pdf")
    assert kiwix.split_content_path("/search") == ("", "")


# ── kiwix client: search ───────────────────────────────────────────────────

_SEARCH_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"><channel>
<title>Search: pacman</title><link>/search?pattern=pacman</link>
<opensearch:totalResults>1,234</opensearch:totalResults>
<item><title>Pacman (Suomi)</title><link>/content/archlinux_en_all_maxi_2026-10/Pacman_(Suomi)</link>
  <description>...<b>pacman</b> on...</description><book><title>ArchWiki</title></book><wordCount>900</wordCount></item>
<item><title>Pacman/Package signing</title><link>/content/archlinux_en_all_maxi_2026-10/Pacman/Package_signing</link>
  <description><b>pacman</b> uses OpenPGP keys &lt; in a web of trust...</description>
  <book><title>ArchWiki</title></book><wordCount>1,845</wordCount></item>
</channel></rss>"""


class _Response:
    def __init__(self, status=200, content=b"", text=None):
        self.status_code = status
        self.content = content
        self.text = text if text is not None else content.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)


def test_search_parses_results_and_skips_translations(library_on, monkeypatch):
    seen = {}

    def fake_get(path, params=None, timeout=None, retries=1):
        seen["path"], seen["params"] = path, params
        return _Response(content=_SEARCH_XML)

    monkeypatch.setattr(kiwix, "_get", fake_get)
    hits = kiwix.search("pacman", count=5, books=["archlinux_en_all_maxi"])
    assert seen["path"] == "/search"
    assert ("books.name", "archlinux_en_all_maxi") in seen["params"]
    assert [hit["title"] for hit in hits] == ["Pacman/Package signing"]
    hit = hits[0]
    assert hit["book"] == "archlinux_en_all_maxi" and hit["book_title"] == "ArchWiki"
    assert hit["words"] == 1845
    assert hit["snippet"].startswith("pacman uses OpenPGP keys < in a web of trust")
    assert hit["url"].endswith("/content/archlinux_en_all_maxi/Pacman/Package_signing")


def test_search_survives_malformed_xml_errors_and_http_failures(library_on, monkeypatch):
    loose = (b"<rss><channel><item><title>A & B</title><link>/content/x_en_2026-01/A</link>"
             b"<description>one <b>two</b> < three</description></item></channel></rss>")
    monkeypatch.setattr(kiwix, "_get", lambda *a, **k: _Response(content=loose))
    hits = kiwix.search("a", count=3)
    assert len(hits) == 1 and hits[0]["title"] == "A & B" and "two" in hits[0]["snippet"]

    monkeypatch.setattr(kiwix, "_get", lambda *a, **k: _Response(status=400, content=b"<error/>"))
    assert kiwix.search("a") == []

    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(kiwix, "_get", boom)
    assert kiwix.search("a") == []
    assert kiwix.search("   ") == []


def test_suggest_returns_only_real_titles(library_on, monkeypatch):
    body = ('[{"value": "Amanita phalloides", "label": "x", "kind": "path", "path": "Amanita_phalloides"},'
            ' {"value": "Pacman (Español)", "label": "x", "kind": "path", "path": "Pacman_(Español)"},'
            ' {"value": "amanita ", "label": "containing", "kind": "pattern"}]')
    monkeypatch.setattr(kiwix, "_get", lambda *a, **k: _Response(content=body.encode()))
    rows = kiwix.suggest("wikipedia_en_all_maxi", "amanita")
    assert [row["title"] for row in rows] == ["Amanita phalloides"]
    assert rows[0]["url"].endswith("/content/wikipedia_en_all_maxi/Amanita_phalloides")


def test_available_is_cached_and_respects_the_switches(library_on, monkeypatch):
    calls = []

    def fake_get(path, params=None, timeout=None, retries=1):
        calls.append(path)
        return _Response(content=b"<feed/>")

    monkeypatch.setattr(kiwix, "_get", fake_get)
    assert kiwix.available() is True
    assert kiwix.available() is True
    assert len(calls) == 1                      # second answer came from the cache
    assert kiwix.available(force=True) is True and len(calls) == 2

    monkeypatch.setenv("AEGIS_LIBRARY_DISABLED", "1")
    assert kiwix.available() is False           # kill switch wins without a request
    assert len(calls) == 2


def test_fetch_article_reduces_html_and_rejects_binary(library_on, monkeypatch):
    page = b"<html><head><title>Bow drill</title></head><body><p>" + b"A bow drill makes fire. " * 30 + b"</p></body></html>"
    monkeypatch.setattr(kiwix, "fetch", lambda url, max_bytes=0: ("text/html; charset=utf-8", page))
    url = "https://box.example.ts.net:8443/content/wikipedia_en_all_maxi/Bow_drill"
    out = kiwix.fetch_article(url, query="fire", budget=300)
    assert out["success"] and out["title"] == "Bow drill" and "bow drill makes fire" in out["content"].lower()
    assert len(out["content"]) <= 320

    monkeypatch.setattr(kiwix, "fetch", lambda url, max_bytes=0: ("application/pdf", b"%PDF-1.4"))
    out = kiwix.fetch_article(url)
    assert not out["success"] and "not a text page" in out["error"]

    monkeypatch.setattr(kiwix, "fetch", lambda url, max_bytes=0: None)
    assert kiwix.fetch_article(url)["success"] is False


# ── kiwix client: one search at a time, and riding out a restart ───────────

class _FakeClient:
    def __init__(self, script, seen):
        self.script, self.seen = script, seen

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None, timeout=None):
        self.seen.append((url, kiwix._request_lock.locked()))
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_search_requests_are_serialized_and_retried_after_a_restart(library_on, monkeypatch):
    seen = []
    script = [httpx.ConnectError("refused"), _Response(content=b"<rss/>")]
    monkeypatch.setattr(kiwix, "_client", lambda: _FakeClient(script, seen))
    monkeypatch.setattr(kiwix, "_RETRY_DELAY_S", 0)
    kiwix._avail_cache = (0.0, "http://127.0.0.1:8095", True)       # "it was up a moment ago"

    response = kiwix._get("/search", params=[("pattern", "x")])
    assert response.status_code == 200
    assert [held for _url, held in seen] == [True, True]             # each attempt under the lock
    assert kiwix._avail_cache is None                                # the stale "up" was dropped

    # Non-search paths are not serialized, and the last failure propagates.
    seen.clear()
    script[:] = [_Response(content=b"<feed/>"), httpx.ConnectError("refused"), httpx.ConnectError("refused")]
    kiwix._get("/catalog/v2/root.xml")
    assert seen[0][1] is False
    with pytest.raises(httpx.ConnectError):
        kiwix._get("/suggest", params={"term": "x"})
    assert set(kiwix._SERIAL_PATHS) == {"/search", "/suggest"}
