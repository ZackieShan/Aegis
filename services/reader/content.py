"""What the reader shows: a library article made safe and plain, or the pages
of a manual.

The library is someone else's HTML (Wikipedia, Stack Exchange, repair guides…)
and scanned or typeset PDFs. The reader shows both in one calm column on
Aegis's own page, where a person can highlight and take notes — so:

* an **article** is fetched from the Kiwix server, cut down to its main
  content, and passed through an allow-list (``nh3``): text structure, tables,
  pictures and links, nothing that can run or reach out. Pictures are served
  through Aegis (``/api/reference/asset``); links to other articles of the
  same collection open in the reader; outside links are marked as such.
* a **manual** (a PDF inside a collection, or a document added later) is read
  from the page index — the text of each page, its lines joined back into
  paragraphs — and each page can also be shown as a picture, for when the
  layout or a figure matters.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

logger = logging.getLogger(__name__)

MAX_ARTICLE_BYTES = 12_000_000
PAGES_PER_REQUEST = 6
MAX_PDF_BYTES = 400_000_000          # the largest document a page picture is made from
PDF_CACHE_BYTES = 1_500_000_000      # documents kept on disk for page pictures
PAGE_IMAGE_WIDTH = 1200

_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd", "blockquote", "pre", "code",
    "em", "strong", "b", "i", "u", "s", "sub", "sup", "small", "abbr", "cite", "q", "span", "div", "section",
    "figure", "figcaption", "img", "a", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    "kbd", "samp", "var", "time",
}
_ATTRIBUTES = {
    "a": {"href", "title", "data-kind"},
    "img": {"src", "alt", "width", "height", "loading", "class"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "ol": {"start"},
}
# Page furniture that is not the text: navigation, edit links, banners.
_DROP_TAGS = ("script", "style", "noscript", "iframe", "object", "embed", "form", "input", "button", "select",
              "textarea", "nav", "header", "footer", "link", "meta", "svg", "audio", "video", "canvas", "template")
_DROP_CLASSES = re.compile(
    r"(^|\s)(mw-editsection|navbox|vertical-navbox|noprint|metadata|sistersitebox|ambox|mw-jump-link|"
    r"mw-empty-elt|printfooter|catlinks|toc|toclimit\S*|reference-accessdate|portal\S*|navigation-not-searchable|"
    r"mw-ref|reference|post-menu|s-topbar|site-header|site-footer|sidebar|share-\S+|ad|ads|advert\S*)(\s|$)", re.I)
_MAIN_SELECTORS = ("#mw-content-text", "main", "article", "[role=main]", "#content", ".content", "#bodyContent", "body")


class ReaderError(Exception):
    """Why something cannot be opened, in words for the person."""


# ── which document a link means ───────────────────────────────────────────

def resolve(url: str = "", book: str = "", path: str = "", page: int = 0) -> Dict[str, object]:
    """``{"kind", "book", "path", "page"}`` for a library link, or for a
    collection + path given directly (a shelf entry being reopened)."""
    from services.library import config, kiwix

    if url and urlsplit(url).path == "/read":         # the reader's own address for something
        asked = parse_qs(urlsplit(url).query)
        inner = (asked.get("u") or [""])[0]
        if inner and urlsplit(inner).path != "/read":
            return resolve(url=inner)
        book, path = (asked.get("b") or [""])[0], (asked.get("p") or [""])[0]
        page = int((asked.get("page") or ["0"])[0]) if (asked.get("page") or ["0"])[0].isdigit() else 0
        url = ""
        if not path:
            raise ReaderError("That is not something from the library.")
    if url:
        parts = urlsplit(url)
        fragment = re.search(r"page=(\d+)", parts.fragment or "")
        if fragment:
            page = int(fragment.group(1))
        local = config.local_document(url)
        if local:                                     # a document kept on this server
            book, path = local
        else:
            book, path = kiwix.split_content_path(url if "://" in url else parts.path)
            # Only the collection and the path inside it are taken from a link;
            # the page itself always comes from the library server. A link
            # written with another address (an old chat, the library reached
            # by another name) is still good — if it names a collection that
            # is really here.
            if parts.netloc and not kiwix.owns_url(url) and book not in _collection_ids():
                book = ""
    book, path = str(book or "").strip(), str(path or "").strip().lstrip("/")
    if not book or "/" in book or ".." in path.split("/") or (url and not path):
        raise ReaderError("That is not something from the library.")
    is_document = book in config.LOCAL_BOOKS or path.lower().endswith(".pdf")
    return {"kind": "manual" if is_document else "article", "book": book, "path": path,
            "page": max(1, int(page or 1)) if is_document else 0}


def _collection_ids() -> set:
    from services.library import kiwix
    try:
        return {str(book.get("id") or "") for book in kiwix.list_books()}
    except Exception:
        return set()


def library_url(book: str, path: str, page: int = 0) -> str:
    """The link to the original (the Kiwix reader, or the stored file)."""
    from services.library import config, kiwix, manuals

    if book in config.LOCAL_BOOKS or path.lower().endswith(".pdf"):
        return manuals.pdf_url(book, path, page)
    return kiwix.public_url(f"/content/{book}/{quote(path, safe='/')}")


def original_for(url: str = "", book: str = "", path: str = "", page: int = 0) -> str:
    """The link to the original for whatever was asked for — also when it
    could not be opened here. "" when it is not a library address at all."""
    from services.library import kiwix

    try:
        target = resolve(url=url, book=book, path=path, page=page)
    except (ReaderError, ValueError):
        return ""
    if not target["path"]:
        return kiwix.public_url(f"/content/{target['book']}/")
    return library_url(str(target["book"]), str(target["path"]), int(target["page"] or 0))


# ── articles ──────────────────────────────────────────────────────────────

def _content_path(book: str, path: str) -> str:
    return f"/content/{quote(book, safe='')}/{quote(path, safe='/')}"


def _rewrite(root, book: str, path: str) -> None:
    """Point pictures at Aegis's own image proxy and links at the reader."""
    base = "http://library" + _content_path(book, path)
    prefix = f"/content/{quote(book, safe='')}/"
    for image in root.find_all("img"):
        source = (image.get("src") or image.get("data-src") or "").strip()
        target = urlsplit(urljoin(base, source)) if source and not source.startswith("data:") else None
        if target is None or target.netloc != "library" or not target.path.startswith("/content/"):
            image.decompose()
            continue
        drawn = "mwe-math" in " ".join(image.get("class") or []) or target.path.lower().endswith(".svg")
        for name in list(image.attrs):
            if name not in ("alt", "width", "height"):
                del image[name]
        for name in ("width", "height"):                 # numbers only: "12ex" is a style, not a size
            if name in image.attrs and not str(image[name]).isdigit():
                del image[name]
        image["src"] = "/api/reference/asset?p=" + quote(target.path, safe="/")
        image["loading"] = "lazy"
        if drawn:
            image["class"] = "drawn"                  # line art: the page inverts it on dark paper
    for link in root.find_all("a"):
        href = (link.get("href") or "").strip()
        title = link.get("title")
        link.attrs = {}
        if title:
            link["title"] = title
        if not href or href.startswith("#"):
            link.unwrap()
            continue
        target = urlsplit(urljoin(base, href))
        if target.scheme in ("http", "https") and target.netloc == "library":
            if target.path.startswith(prefix) and not re.search(r"\.(png|jpe?g|gif|webp|svg|css|js)$", target.path, re.I):
                inner = unquote(target.path[len(prefix):])
                link["href"] = f"/read?b={quote(book, safe='')}&p={quote(inner, safe='/')}"
                link["data-kind"] = "library"
            else:
                link.unwrap()
        elif target.scheme in ("http", "https"):
            link["href"] = target.geturl()
            link["data-kind"] = "web"
        else:
            link.unwrap()


def clean_article(html: str, book: str, path: str) -> Tuple[str, str]:
    """``(title, safe html)`` for a library page."""
    import nh3
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    root = None
    for selector in _MAIN_SELECTORS:
        root = soup.select_one(selector)
        if root is not None:
            break
    if root is None:
        root = soup
    heading = root.find("h1") or soup.find("h1")
    said = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)) if heading else ""
    if said and len(said) <= 120:
        title = said
        if heading in root.descendants:
            heading.decompose()                       # the reader shows the title itself
    elif said and not title:
        title = said[:80].rsplit(" ", 1)[0] + "…"     # a whole title page set as one heading: it stays in the text
    for tag in root.find_all(_DROP_TAGS):
        tag.decompose()
    for tag in root.find_all(True):
        if tag.attrs is None:
            continue
        classes = " ".join(tag.get("class") or [])
        if classes and _DROP_CLASSES.search(classes):
            tag.decompose()
    _rewrite(root, book, path)
    safe = nh3.clean(root.decode_contents(), tags=_TAGS, attributes=_ATTRIBUTES, url_schemes={"http", "https"},
                     link_rel="noopener noreferrer", strip_comments=True)
    safe = re.sub(r"(<br\s*/?>\s*){3,}", "<br><br>", safe)
    return re.sub(r"\s+", " ", title)[:300], safe


def _book_text_path(book: str, number: str) -> str:
    """Where the text of a Project Gutenberg book is (search finds its card)."""
    from services.library import kiwix
    return kiwix.book_text_path(book, number)


def article(book: str, path: str = "") -> Dict[str, object]:
    """A library page ready to show: ``{"path", "title", "html"}``. ``path`` is
    where the page really lives — the front page of a collection (no path
    asked for) and an article known by another name answer from elsewhere."""
    from services.library import config, kiwix

    if not kiwix.available():
        raise ReaderError("The library server is not answering right now. Try again in a moment.")
    card = re.match(r"^index/book_(\d+)$", path or "")
    if card:
        path = _book_text_path(book, card.group(1)) or path
    asked = config.kiwix_url() + (f"/content/{quote(book, safe='')}/" if not path else _content_path(book, path))
    fetched = kiwix.fetch_final(asked, MAX_ARTICLE_BYTES)
    if not fetched:
        raise ReaderError("That page is not in the library.")
    kind, body, final = fetched
    landed_book, landed_path = kiwix.split_content_path(final)
    if landed_book == kiwix.strip_date(book) or landed_book == book:
        path = landed_path or path
    if "html" not in kind.lower():
        raise ReaderError("That is not a page that can be read here.")
    raw = body.decode("utf-8", errors="replace")
    title, html = clean_article(raw, book, path)
    shelved = re.match(r"^(.{2,200})\.\d+$", path) if book.startswith("gutenberg") else None
    if shelved:                                       # a Project Gutenberg book is filed under its catalogue title
        title = shelved.group(1)
    words = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
    # Nothing left, or only the placeholder of a page that is drawn by script.
    if len(words) < 40 or (len(words) < 300 and "<script" in raw.lower() and re.search(r"\bLoading\b", words)):
        raise ReaderError("That page has nothing to read here — it is a menu that only works in the library's own viewer.")
    return {"path": path, "title": title or unquote(path.rsplit("/", 1)[-1]).replace("_", " "), "html": html}


def article_text(book: str, path: str) -> Dict[str, str]:
    """The same page as plain text (what a recipe is made from)."""
    from bs4 import BeautifulSoup

    page = article(book, path)
    soup = BeautifulSoup(str(page["html"]), "lxml")
    for block in soup.find_all(["p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br", "div", "dt", "dd"]):
        block.insert_after("\n")
    text = re.sub(r"[ \t\xa0]+", " ", soup.get_text())
    return {"title": str(page["title"]), "text": re.sub(r"\n\s*\n\s*", "\n", text).strip()}


# ── manuals and added documents ───────────────────────────────────────────

def document(book: str, path: str) -> Dict[str, object]:
    """Title and page count of an indexed document."""
    from services.library import manuals

    found = manuals.document(book, path)
    if found is None:
        raise ReaderError("That document has not been read into the library's index, so it cannot be shown here. "
                          "Open the original instead.")
    return found


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ufffd]")
_SYMBOL = "•●▪■◦‣·∙○◆►▶➢➤✓✔❖\uf0b7\uf0a7\uf0d8\uf076"
_LONE_BULLET = re.compile(rf"^(?:[{_SYMBOL}\ue000-\uf8ff]|[lnoquvz])$")      # a symbol-font bullet alone on its line
_BULLET_START = re.compile(rf"^[{_SYMBOL}\ue000-\uf8ff]\s*")
_LIST_START = re.compile(r"^(?:\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|[-–—*])\s+\S")
_ENDS_SENTENCE = (".", "?", "!", ":", ";", ",", "”", '"', ")")


def reflow(text: str) -> List[Dict[str, str]]:
    """A PDF page's text as paragraphs: ``[{"k": "p" | "h" | "li", "t": …}]``.

    A page comes out of a PDF as short lines, broken where the print was.
    Lines are joined back into paragraphs (a line clearly shorter than the
    page's full lines ends one), words split by a hyphen at a line's end are
    mended, and lines set in a font whose letters did not survive are left
    out. ``h`` marks a short line standing by itself — a heading or a page
    header; ``li`` a list item."""
    lines: List[str] = []
    for raw in str(text or "").replace("\r", "\n").split("\n"):
        junk = len(_CONTROL.findall(raw))
        if junk and junk * 3 >= len(raw.strip()):
            continue
        lines.append(_CONTROL.sub("", raw).replace("\u00a0", " ").rstrip())
    widths = sorted(len(line.strip()) for line in lines if len(line.strip()) > 20)
    full = widths[int(len(widths) * 0.85)] if widths else 0
    blocks: List[Dict[str, str]] = []
    current, item, bullet_next = "", False, False

    def flush() -> None:
        nonlocal current, item
        text_ = re.sub(r"\s+", " ", current).strip()
        if text_:
            words = text_.split()
            heading = (not item and len(text_) <= 70 and len(words) <= 9 and not text_.endswith(_ENDS_SENTENCE)
                       and any(ch.isalpha() for ch in text_))
            blocks.append({"k": "li" if item else ("h" if heading else "p"), "t": text_})
        current, item = "", False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if _LONE_BULLET.match(stripped):
            flush()
            bullet_next = True
            continue
        bullet = _BULLET_START.match(stripped)
        if bullet_next or bullet or _LIST_START.match(stripped):
            flush()
            item = True
            if bullet:
                stripped = stripped[bullet.end():]
            bullet_next = False
        # A short line beginning with a capital, after a finished sentence, is
        # a heading (or a paragraph's own last words) — not more of that sentence.
        if (current and full and len(stripped) < full * 0.6 and stripped[:1].isupper()
                and current.rstrip().endswith((".", "?", "!", ":"))):
            flush()
        if not current:
            current = stripped
        elif current.endswith("-") and len(current) > 1 and current[-2].isalpha() and stripped[:1].islower():
            current = current[:-1] + stripped                # a word split across two lines
        else:
            current += " " + stripped
        if full and len(stripped) < full * 0.6:               # a short line: the paragraph (or heading) ends here
            flush()
    flush()
    return blocks


def pages(book: str, path: str, first: int, count: int = PAGES_PER_REQUEST) -> List[Dict[str, object]]:
    from services.library import manuals

    count = max(1, min(int(count or PAGES_PER_REQUEST), 12))
    return [{"n": number, "blocks": reflow(text)} for number, text in manuals.page_texts(book, path, max(1, int(first)), count)]


def page_text(book: str, path: str, page: int) -> str:
    """One page as plain text, a paragraph to a line."""
    from services.library import manuals

    found = manuals.page_texts(book, path, max(1, int(page)), 1)
    return "\n".join(block["t"] for block in reflow(found[0][1])) if found else ""


# ── a page as a picture ───────────────────────────────────────────────────

_pdf_lock = threading.Lock()
_render_slots = threading.BoundedSemaphore(2)


def _cache_dir() -> Path:
    from src.constants import DATA_DIR
    folder = Path(DATA_DIR) / "reader_cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _trim_cache(folder: Path, keep: Path) -> None:
    files = sorted((f for f in folder.glob("*.pdf") if f != keep), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files) + (keep.stat().st_size if keep.exists() else 0)
    for stale in files:
        if total <= PDF_CACHE_BYTES:
            break
        total -= stale.stat().st_size
        stale.unlink(missing_ok=True)


def pdf_file(book: str, path: str) -> Path:
    """The document as a file on disk: an added one where it already is, one
    out of a collection copied next to Aegis's data the first time a page
    picture is asked for (and dropped again when room is needed)."""
    from services.library import additions, config, files, kiwix

    if book == config.ADDED_BOOK:
        found = additions.file_for(path)
        if found is None:
            raise ReaderError("That document's file is gone.")
        return found
    if book == config.FILES_BOOK:
        found = files.file_for(path)
        if found is None:
            raise ReaderError("That document's file is gone.")
        if files.kind_of(found.name) in ("txt", "html"):
            raise ReaderError("That document is plain text: there is no printed page to show.")
        return found
    target = _cache_dir() / (hashlib.sha1(f"{book}\n{path}".encode("utf-8")).hexdigest() + ".pdf")
    with _pdf_lock:
        if target.exists() and target.stat().st_size > 0:
            os.utime(target, None)
            return target
        fetched = kiwix.fetch_final(config.kiwix_url() + _content_path(book, path), MAX_PDF_BYTES, whole=True)
        if not fetched or not fetched[1].startswith(b"%PDF"):
            raise ReaderError("That document could not be read from the library.")
        partial = target.with_suffix(".part")
        partial.write_bytes(fetched[1])
        os.replace(partial, target)
        _trim_cache(target.parent, target)
        return target


def page_image(book: str, path: str, page: int, width: int = PAGE_IMAGE_WIDTH) -> bytes:
    """One page of a document as a JPEG, as printed."""
    import fitz

    from services.library import files

    source = pdf_file(book, path)
    with _render_slots:
        with files.open_document(str(source)) as pdf:      # an e-book gets the page size the index used
            if page < 1 or page > pdf.page_count:
                raise ReaderError("That document has no such page.")
            sheet = pdf[page - 1]
            zoom = max(0.5, min(3.0, float(width) / max(sheet.rect.width, 1.0)))
            picture = sheet.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            return picture.tobytes("jpeg", jpg_quality=82)
