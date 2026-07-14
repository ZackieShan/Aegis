from bs4 import BeautifulSoup

import src.visual_report as visual_report
from src.visual_report import generate_visual_report


def test_visual_report_toc_links_match_rendered_heading_ids():
    report = """
# Automated Crypto Trading Bot Strategies

### **1.0 Introduction & Research Scope**

Intro body.

### **2.0 Determining the "Best" Configuration**

Configuration body.
"""

    html = generate_visual_report(
        "crypto bot strategies",
        report,
        sources=[],
        stats={},
        session_id="rp-test",
    )
    soup = BeautifulSoup(html, "html.parser")

    links = soup.select(".toc-sidebar nav a")
    assert [link.get_text(strip=True) for link in links] == [
        "1.0 Introduction & Research Scope",
        '2.0 Determining the "Best" Configuration',
    ]

    for link in links:
        target_id = link["href"].removeprefix("#")
        target = soup.find(id=target_id)
        assert target is not None
        assert target.name in {"h2", "h3"}


def test_fallback_clean_strips_active_content():
    dirty = (
        '<h2 id="intro" onclick="evil()">Title</h2>'
        "<script>alert(1)</script>"
        '<p><code class="code">ok</code> <a href="javascript:alert(2)">bad link</a>'
        ' <a target="_blank" rel="noopener noreferrer" href="https://example.com">good link</a></p>'
        '<img src="data:text/html,x" alt="pic">'
        '<img src="https://example.com/a.png" alt="ok-img">'
        "<!-- secret comment -->"
        "<unknowntag>kept text</unknowntag>"
    )
    out = visual_report._fallback_clean(dirty)

    assert "alert(1)" not in out and "<script" not in out
    assert "onclick" not in out
    assert "javascript:" not in out
    assert "secret comment" not in out
    assert "<unknowntag" not in out and "kept text" in out
    # Allowed formatting survives
    assert 'id="intro"' in out
    assert 'class="code"' in out
    assert 'href="https://example.com"' in out
    assert 'target="_blank"' in out and 'rel="noopener noreferrer"' in out
    # javascript:/data: URLs are dropped but the elements remain
    assert "bad link" in out
    assert 'src="https://example.com/a.png"' in out
    soup = BeautifulSoup(out, "html.parser")
    assert soup.find("img", alt="pic") is not None
    assert not soup.find("img", alt="pic").has_attr("src")


def test_fallback_url_allowed_edge_cases():
    allowed = visual_report._fallback_url_allowed
    assert allowed("https://example.com/x")
    assert allowed("#section-1")
    assert allowed("relative/path.html")
    assert allowed("/path?q=a:b")
    assert allowed("mailto:a@b.c")
    assert not allowed("javascript:alert(1)")
    assert not allowed("JaVaScRiPt:alert(1)")
    assert not allowed("java\tscript:alert(1)")
    assert not allowed(" \x00javascript:alert(1)")
    assert not allowed("data:text/html,x")
    assert not allowed("vbscript:x")


def test_visual_report_renders_without_nh3(monkeypatch):
    # Simulate nh3's extension DLL being blocked (Smart App Control / WDAC):
    # the module must fall back to the pure-Python sanitizer, not crash.
    monkeypatch.setattr(visual_report, "nh3", None)
    html = generate_visual_report(
        "fallback sanitizer",
        '## Section\n\nBody <script>alert("xss-probe")</script> text.\n',
        sources=[],
        stats={},
        session_id="rp-test-fallback",
    )
    # The raw report text legitimately appears HTML-escaped in the meta
    # description; what must not survive is an executable script payload.
    assert 'alert("xss-probe")' not in html
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        assert "xss-probe" not in (script.string or "")
    assert "Body" in html
