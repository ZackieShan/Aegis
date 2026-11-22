"""Client for a local ``kiwix-serve`` — the offline reference library.

kiwix-serve exposes everything needed over plain HTTP:

``/catalog/v2/entries``            OPDS list of the installed collections
``/search?pattern=…&format=xml``   full-text search across collections
``/suggest?content=…&term=…``      title completion inside one collection
``/content/<book>/<path>``         the page (or PDF, image, …) itself

Two base URLs are involved. ``kiwix_url`` is how *Aegis* reaches the server
(normally loopback); ``kiwix_public_url`` is what goes into links shown to the
user, because 127.0.0.1 means nothing to a phone. Everything returned from
here carries the public form; :func:`to_internal` converts back for fetching.

The loopback requests below deliberately bypass the SSRF guard used for web
fetches (``services.search.content``): the only host ever contacted is the
configured kiwix base, never a URL taken from a search result or the model.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

import httpx

from . import config

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(25.0, connect=2.0)
_ATOM = {"a": "http://www.w3.org/2005/Atom"}
_DATE_SUFFIX = re.compile(r"_\d{4}-\d{2}(?:-\d{2})?$")
_TAG = re.compile(r"<[^>]+>")

_UP_TTL = 30.0
_DOWN_TTL = 8.0
_BOOKS_TTL = 60.0

_lock = threading.Lock()
_avail_cache: Optional[Tuple[float, str, bool]] = None
_books_cache: Optional[Tuple[float, str, List[dict]]] = None

# Collections that hold translations next to the English page (the ArchWiki
# dump does). Their titles end in the language's own name.
_TRANSLATION_SUFFIX = re.compile(
    r"\s\((?:العربية|Български|Bosanski|Català|Čeština|Dansk|Deutsch|Ελληνικά|Español|Esperanto|"
    r"فارسی|Suomi|Français|עברית|Hrvatski|Magyar|Bahasa Indonesia|Indonesia|Italiano|日本語|한국어|"
    r"Lietuvių|Nederlands|Norsk Bokmål|Polski|Português|Română|Русский|Slovenčina|Slovenský|"
    r"Српски|Svenska|ไทย|Türkçe|Українська|Tiếng Việt|简体中文|正體中文|粵語|हिन्दी|বাংলা)\)$"
)


def strip_date(book_id: str) -> str:
    """``wikipedia_en_all_maxi_2026-08`` → ``wikipedia_en_all_maxi``.

    kiwix-serve is started with ``--nodatealiases``, so the short form stays
    valid when a collection is replaced by a newer edition."""
    return _DATE_SUFFIX.sub("", book_id or "")


def _client() -> httpx.Client:
    # trust_env=False: never route loopback traffic through a system proxy.
    return httpx.Client(timeout=_TIMEOUT, trust_env=False, follow_redirects=True)


# kiwix-serve 3.8 is not safe under concurrent full-text searches: its Xapian
# stemmer is shared between request threads ("Snowball slice invalid", then a
# segfault — seen here when six relaxed queries ran at once). A search takes
# ~30 ms, so sending them one at a time costs nothing and removes the race.
_SERIAL_PATHS = ("/search", "/suggest")
_request_lock = threading.Lock()
_RETRY_DELAY_S = 2.5          # systemd restarts a crashed server within a few seconds
_GONE = (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError)


def _get(path: str, params=None, timeout: Optional[httpx.Timeout] = None, retries: int = 1) -> httpx.Response:
    """GET from kiwix-serve. Searches are serialized (see above); a request
    that finds the server gone forgets the cached "it is up" and tries again
    after a pause, which rides out a restart."""
    url = config.kiwix_url() + path
    for attempt in range(retries + 1):
        try:
            with _client() as client:
                if path in _SERIAL_PATHS:
                    with _request_lock:
                        return client.get(url, params=params, timeout=timeout or _TIMEOUT)
                return client.get(url, params=params, timeout=timeout or _TIMEOUT)
        except _GONE:
            reset_cache()
            if attempt >= retries:
                raise
            time.sleep(_RETRY_DELAY_S)
    raise RuntimeError("unreachable")


def reset_cache() -> None:
    global _avail_cache, _books_cache
    with _lock:
        _avail_cache = None
        _books_cache = None


def available(force: bool = False) -> bool:
    """True when the library is switched on and kiwix-serve answers.

    Cached briefly (longer while up than while down) because search calls
    this on every query."""
    global _avail_cache
    if not config.enabled():
        return False
    base = config.kiwix_url()
    now = time.monotonic()
    with _lock:
        cached = _avail_cache
    if not force and cached and cached[1] == base:
        age = now - cached[0]
        if age < (_UP_TTL if cached[2] else _DOWN_TTL):
            return cached[2]
    ok = False
    try:
        response = _get("/catalog/v2/root.xml", timeout=httpx.Timeout(6.0, connect=1.0), retries=0)
        ok = response.status_code == 200
    except Exception as exc:
        logger.debug("kiwix-serve not reachable at %s: %s", base, exc)
    with _lock:
        _avail_cache = (now, base, ok)
    return ok


def _text(node: Optional[ET.Element]) -> str:
    return "".join(node.itertext()).strip() if node is not None else ""


def _int(value: str) -> int:
    digits = re.sub(r"[^\d]", "", value or "")
    return int(digits) if digits else 0


def list_books(force: bool = False) -> List[dict]:
    """Installed collections: ``id`` (URL name without date), ``name``,
    ``title``, ``summary``, ``category``, ``tags``, ``articles``, ``media``,
    ``date``, ``fulltext``."""
    global _books_cache
    base = config.kiwix_url()
    now = time.monotonic()
    with _lock:
        cached = _books_cache
    if not force and cached and cached[1] == base and now - cached[0] < _BOOKS_TTL:
        return cached[2]
    books: List[dict] = []
    try:
        response = _get("/catalog/v2/entries", params={"count": "-1"})
        response.raise_for_status()
        root = ET.fromstring(response.content)
        for entry in root.findall("a:entry", _ATOM):
            book_id = ""
            for link in entry.findall("a:link", _ATOM):
                if link.get("type") == "text/html":
                    book_id = (link.get("href") or "").rstrip("/").rsplit("/", 1)[-1]
            tags = _text(entry.find("a:tags", _ATOM))
            books.append({
                "id": strip_date(unquote(book_id)),
                "dated_id": unquote(book_id),
                "name": _text(entry.find("a:name", _ATOM)),
                "flavour": _text(entry.find("a:flavour", _ATOM)),
                "title": _text(entry.find("a:title", _ATOM)),
                "summary": _text(entry.find("a:summary", _ATOM)),
                "category": _text(entry.find("a:category", _ATOM)),
                "tags": [t for t in tags.split(";") if t and not t.startswith("_")],
                "articles": _int(_text(entry.find("a:articleCount", _ATOM))),
                "media": _int(_text(entry.find("a:mediaCount", _ATOM))),
                "date": _text(entry.find("a:updated", _ATOM))[:10],
                "fulltext": "_ftindex:yes" in tags,
            })
    except Exception as exc:
        logger.warning("Could not read the Kiwix catalog: %s", exc)
        return cached[2] if cached and cached[1] == base else []
    books.sort(key=lambda b: (b["title"].lower(), b["id"]))
    with _lock:
        _books_cache = (now, base, books)
    return books


def book_titles() -> Dict[str, str]:
    return {book["id"]: book["title"] for book in list_books()}


# ── URL handling ───────────────────────────────────────────────────────────

def public_url(path: str) -> str:
    """``/content/<book>_<date>/<page>`` → link the user can open."""
    if not path.startswith("/"):
        path = "/" + path
    match = re.match(r"^/content/([^/]+)(/.*)?$", path)
    if match:
        book = strip_date(unquote(match.group(1)))
        path = "/content/" + quote(book, safe="") + (match.group(2) or "")
    # Parentheses end a markdown link early ("Field_dressing_(hunting)"), and
    # these URLs are quoted by the model in markdown.
    path = path.replace("(", "%28").replace(")", "%29")
    return config.kiwix_public_url() + path


def owns_url(url: str) -> bool:
    """True for a link produced by :func:`public_url` (or its loopback twin)."""
    if not isinstance(url, str) or not url:
        return False
    for base in {config.kiwix_public_url(), config.kiwix_url()}:
        if url.startswith(base + "/content/"):
            return True
    return False


def to_internal(url: str) -> Optional[str]:
    """Map a library link back to the loopback URL. Returns None for anything
    that is not a ``/content/`` URL under one of the two configured bases —
    callers must not fetch those through this module."""
    if not owns_url(url):
        return None
    for base in (config.kiwix_public_url(), config.kiwix_url()):
        if url.startswith(base + "/content/"):
            tail = url[len(base):]
            # No query strings, no escaping the /content/ tree.
            tail = tail.split("#", 1)[0].split("?", 1)[0]
            if "/../" in tail or tail.endswith("/.."):
                return None
            return config.kiwix_url() + tail
    return None


def split_content_path(url_or_path: str) -> Tuple[str, str]:
    """Return ``(book_id_without_date, entry_path)`` for a ``/content/`` URL."""
    path = urlsplit(url_or_path).path if "://" in url_or_path else url_or_path.split("#", 1)[0]
    match = re.match(r"^/content/([^/]+)/?(.*)$", path)
    if not match:
        return "", ""
    return strip_date(unquote(match.group(1))), unquote(match.group(2))


# ── Search ─────────────────────────────────────────────────────────────────

def _parse_search(xml_bytes: bytes) -> List[dict]:
    items: List[dict] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return _parse_search_loose(xml_bytes.decode("utf-8", "replace"))
    for item in root.iter("item"):
        link = _text(item.find("link"))
        if not link:
            continue
        book = item.find("book")
        items.append({
            "title": _text(item.find("title")),
            "path": link,
            "snippet": re.sub(r"\s+", " ", _text(item.find("description"))).strip(". "),
            "book_title": _text(book.find("title")) if book is not None else "",
            "words": _int(_text(item.find("wordCount"))),
        })
    return items


def _parse_search_loose(text: str) -> List[dict]:
    """Regex fallback for the rare result page that is not well-formed XML
    (a snippet with a stray ``<``)."""
    import html as _html

    items = []
    for block in re.findall(r"<item>(.*?)</item>", text, flags=re.S):
        link = re.search(r"<link>(.*?)</link>", block, flags=re.S)
        if not link:
            continue
        title = re.search(r"<title>(.*?)</title>", block, flags=re.S)
        desc = re.search(r"<description>(.*?)</description>", block, flags=re.S)
        book = re.search(r"<book>\s*<title>(.*?)</title>", block, flags=re.S)
        words = re.search(r"<wordCount>(.*?)</wordCount>", block, flags=re.S)
        items.append({
            "title": _html.unescape(_TAG.sub("", title.group(1))).strip() if title else "",
            "path": _html.unescape(link.group(1)).strip(),
            "snippet": re.sub(r"\s+", " ", _html.unescape(_TAG.sub("", desc.group(1)))).strip(". ") if desc else "",
            "book_title": _html.unescape(_TAG.sub("", book.group(1))).strip() if book else "",
            "words": _int(words.group(1)) if words else 0,
        })
    return items


def search(query: str, count: int = 8, books: Optional[Iterable[str]] = None) -> List[dict]:
    """Full-text search. ``books`` limits it to those collection ids; the
    default is every installed collection.

    Each hit: ``title``, ``url`` (public), ``path``, ``snippet``, ``book``,
    ``book_title``, ``words``.
    """
    query = (query or "").strip()
    if not query:
        return []
    params: List[Tuple[str, str]] = [
        ("pattern", query),
        ("format", "xml"),
        ("start", "0"),
        # Over-fetch: translated duplicates are dropped below.
        ("pageLength", str(max(1, min(count * 2, 50)))),
    ]
    for book in books or ():
        params.append(("books.name", book))
    try:
        response = _get("/search", params=params)
    except Exception as exc:
        logger.warning("Kiwix search failed for %r: %s", query, exc)
        return []
    if response.status_code != 200:
        # 400 = "no such book" or nothing searchable in the selection.
        logger.debug("Kiwix search HTTP %s for %r", response.status_code, query)
        return []
    hits = []
    for item in _parse_search(response.content):
        if _TRANSLATION_SUFFIX.search(item["title"]):
            continue
        book, _entry = split_content_path(item["path"])
        item["book"] = book
        item["url"] = public_url(item["path"])
        hits.append(item)
        if len(hits) >= count:
            break
    return hits


def suggest(book: str, term: str, count: int = 5) -> List[dict]:
    """Title completions inside one collection: ``[{title, path, url}]``."""
    term = (term or "").strip()
    if not term or not book:
        return []
    try:
        response = _get("/suggest", params={"content": book, "term": term, "count": str(count + 4)})
        if response.status_code != 200:
            return []
        data = json.loads(response.text)
    except Exception as exc:
        logger.debug("Kiwix suggest failed for %r in %s: %s", term, book, exc)
        return []
    out = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict) or row.get("kind") != "path" or not row.get("path"):
            continue
        title = str(row.get("value") or "")
        if _TRANSLATION_SUFFIX.search(title):
            continue
        path = f"/content/{quote(book, safe='')}/{quote(str(row['path']), safe='/()!*,:@')}"
        out.append({"title": title, "path": path, "url": public_url(path), "book": book})
        if len(out) >= count:
            break
    return out


# ── Fetch ──────────────────────────────────────────────────────────────────

def fetch(url: str, max_bytes: int = 8_000_000) -> Optional[Tuple[str, bytes]]:
    """Return ``(content_type, body)`` for a library link, or None."""
    internal = to_internal(url)
    if not internal:
        return None
    try:
        with _client() as client, client.stream("GET", internal) as response:
            if response.status_code != 200:
                return None
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    break
                chunks.append(chunk)
            return response.headers.get("content-type", ""), b"".join(chunks)
    except Exception as exc:
        logger.warning("Kiwix fetch failed for %s: %s", url, exc)
        return None


def fetch_article(url: str, query: str = "", budget: int = 4000) -> dict:
    """Fetch a library page and reduce it to the text that bears on ``query``.

    Returns the same core keys as ``fetch_webpage_content`` so the web-search
    formatter can consume either: ``url``, ``title``, ``content``, ``success``,
    ``error``.
    """
    from .text import best_passages, html_to_text

    result = {"url": url, "title": "", "content": "", "success": False, "error": ""}
    fetched = fetch(url)
    if not fetched:
        result["error"] = "not available in the offline library"
        return result
    content_type, body = fetched
    kind = content_type.split(";", 1)[0].strip().lower()
    if kind in ("text/html", "application/xhtml+xml") or (not kind and body.lstrip()[:1] == b"<"):
        title, text = html_to_text(body.decode("utf-8", "replace"))
        result.update(title=title, content=best_passages(text, query, budget=budget), success=bool(text))
    elif kind.startswith("text/"):
        result.update(content=body.decode("utf-8", "replace")[:budget], success=True)
    else:
        result["error"] = f"not a text page ({kind or 'unknown type'})"
    if not result["success"] and not result["error"]:
        result["error"] = "empty page"
    return result
