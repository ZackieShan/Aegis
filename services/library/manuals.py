"""Page-level full-text index over the PDF manuals in the offline library.

Why this exists: Kiwix's own search only covers HTML pages. Several of the
most useful collections — the Army Publishing Directorate dump, the military
medicine set, the survival/water/knots packs — are PDFs wrapped in a ZIM, and
their text is invisible to it. This module reads every PDF out of every ZIM
(``zimdump``, from the ``zim-tools`` package), extracts the text of each page
(PyMuPDF) and stores it in a SQLite FTS5 table, so a question can land on
"TC 4-02.1, page 37" and the answer can link straight to that page.

Layout of ``<library>/index/manuals.db``:

``sources``  one row per ZIM that was scanned (file name, size, mtime, done flag)
``docs``     one row per PDF (book, entry path, title, page counts)
``pages``    FTS5: ``text`` + ``title``; rowid = doc_id * 100000 + page

Only :func:`search`, :func:`passage` and :func:`stats` are used by the web
app — they touch nothing but SQLite. Indexing (``scripts/aegis-library
index``) is a separate, resumable batch job.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

from . import config
from .text import query_terms

logger = logging.getLogger(__name__)

_PAGE_FACTOR = 100_000          # rowid = doc_id * factor + page (1-based)
_MAX_PDF_BYTES = 600 * 1024 * 1024
_OCR_MAX_PAGES = 800            # per document, per run
_TESSDATA_DIRS = ("/usr/share/tessdata", "/usr/share/tesseract-ocr/5/tessdata")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    book        TEXT PRIMARY KEY,
    zim_file    TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    pdf_count   INTEGER NOT NULL DEFAULT 0,
    complete    INTEGER NOT NULL DEFAULT 0,
    indexed_at  REAL
);
CREATE TABLE IF NOT EXISTS docs (
    id          INTEGER PRIMARY KEY,
    book        TEXT NOT NULL,
    path        TEXT NOT NULL,
    title       TEXT NOT NULL,
    pages       INTEGER NOT NULL DEFAULT 0,
    text_pages  INTEGER NOT NULL DEFAULT 0,
    ocr_pages   INTEGER NOT NULL DEFAULT 0,
    bytes       INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    indexed_at  REAL,
    UNIQUE (book, path)
);
CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
    text, title, tokenize = 'porter unicode61 remove_diacritics 2'
);
"""


# ── Database ───────────────────────────────────────────────────────────────

def db_path() -> Path:
    return config.manuals_db_path()


def connect(readonly: bool = False) -> Optional[sqlite3.Connection]:
    """Open the index. Read-only connections return None when it does not
    exist yet (the library works without it — manuals just are not searched)."""
    path = db_path()
    if readonly:
        if not path.exists():
            return None
        conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=10)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


# ── Search (web app side) ──────────────────────────────────────────────────

def _fts_query(terms: Iterable[str], joiner: str) -> str:
    quoted = ['"' + term.replace('"', "") + '"' for term in terms if term.replace('"', "")]
    return joiner.join(quoted)


def pdf_url(book: str, path: str, page: int = 0) -> str:
    if book == config.ADDED_BOOK:                       # kept and served by Aegis, not by Kiwix
        return config.added_url(path, page)
    url = f"{config.kiwix_public_url()}/content/{quote(book, safe='')}/{quote(path, safe='/')}"
    return f"{url}#page={page}" if page else url


def _term_weights(conn: sqlite3.Connection, terms: List[str]) -> Dict[str, float]:
    """Rarity weight per query term (BM25-style idf, from the index itself).

    A term the index has never seen gets the highest weight, not zero: it is
    probably the most specific word of the question, so pages lacking it
    should not pass as matches just because they contain the common words."""
    import math

    total = conn.execute("SELECT COALESCE(SUM(text_pages), 0) FROM docs").fetchone()[0] or 1
    weights: Dict[str, float] = {}
    for term in terms:
        match = _fts_query([term], " ")
        if not match:
            continue
        try:
            seen = conn.execute(
                "SELECT COUNT(*) FROM (SELECT rowid FROM pages WHERE pages MATCH ? LIMIT 20000)", (match,)
            ).fetchone()[0]
        except sqlite3.Error:
            seen = 0
        weights[term] = math.log(1.0 + total / (seen + 0.5))
    return weights


def _weighted_coverage(text: str, weights: Dict[str, float]) -> float:
    from .text import _WORD, _stem

    total = sum(weights.values())
    if total <= 0:
        return 0.0
    words = {_stem(m.group(0)) for m in _WORD.finditer((text or "").lower())}
    return sum(weight for term, weight in weights.items() if _stem(term) in words) / total


_PAPERWORK = re.compile(
    r"^(general orders?\b|(da|dd|sf|opm)[ -]?form\b|AR \d|DA PAM \d|AGO \d|HQDA\b|army regulation\b|"
    r"department of the army pamphlet\b|army directive\b|ALARACT\b|.*\b(scorecard|datasheet|checklist|"
    r"inspection record|feeder card)\b)",
    re.I,
)


def _is_paperwork(title: str) -> bool:
    return bool(_PAPERWORK.search(title or ""))


# How much to trust a collection as a source of procedures and numbers.
#   0  curated packs built with scripts/library/build-manuals-zim.py (named
#      aegis-*): every file has a known publisher and date
#   1  official publishers scraped whole
#   2  everything else — anonymous compilations ("Water (2).pdf"), wiki uploads
# A lower tier is worth about six places of the search engine's own order, so
# the 2017 EPA sheet is read before an undated leaflet that says "two drops".
_OFFICIAL_PREFIXES = ("armypubs", "irp.fas.org", "medlineplus", "wwwnc.cdc.gov", "ready.gov",
                      "usda", "nhs.uk", "mdwiki", "libretexts")
_TIER_PLACES = 6


def collection_tier(book: str) -> int:
    book = (book or "").lower()
    if book.startswith("aegis-"):
        return 0
    if book.startswith(_OFFICIAL_PREFIXES):
        return 1
    return 2


def _same_document_key(title: str, page: int) -> Tuple[str, int]:
    """Collections overlap (the survival packs share files; a curated pack
    repeats manuals from the Army dump). Same title + same page = same hit."""
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())[:80], page


def search(query: str, count: int = 8, per_doc: int = 2) -> List[dict]:
    """Best-matching manual pages for a question.

    Pages that contain every term rank first. If there are too few, pages
    matching some of the terms fill the rest — but only pages that carry at
    least half of the question's *weight*, where rare terms weigh more than
    common ones. At most ``per_doc`` pages per manual, so one long document
    cannot crowd out the others.
    """
    terms = query_terms(query)
    if not terms:
        return []
    conn = connect(readonly=True)
    if conn is None:
        return []
    sql = (
        "SELECT rowid, snippet(pages, 0, '', '', ' … ', 48) AS snip, text, title, "
        "bm25(pages, 1.0, 6.0) AS score FROM pages WHERE pages MATCH ? "
        "ORDER BY score LIMIT ?"
    )
    try:
        rows: List[sqlite3.Row] = []
        seen = set()
        attempts = [(_fts_query(terms, " "), 0.0)]
        if len(terms) > 1:
            # Some-terms fill with a weighted floor — otherwise "pacman keyring
            # update" drags in a malaria guide for the word "update".
            attempts.append((_fts_query(terms, " OR "), 0.5))
        weights: Optional[Dict[str, float]] = None
        for match, floor in attempts:
            if not match:
                continue
            if floor and weights is None:
                weights = _term_weights(conn, terms)
            for row in conn.execute(sql, (match, count * 8)):
                if row["rowid"] in seen:
                    continue
                if floor and _weighted_coverage(f"{row['title']} {row['text']}", weights or {}) < floor:
                    continue
                seen.add(row["rowid"])
                rows.append(row)
            if len(rows) >= count * 3:
                break

        docs: Dict[int, sqlite3.Row] = {}
        for row in rows:
            doc_id = row["rowid"] // _PAGE_FACTOR
            if doc_id not in docs:
                found = conn.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
                if found is not None:
                    docs[doc_id] = found
        # Administrative paperwork (orders, regulations, forms) shares its
        # vocabulary with the manuals but is never what a how-to question
        # wants: keep the engine's order, but manuals first.
        # …and sourced collections before anonymous ones (see collection_tier).
        rows = [row for row in rows if row["rowid"] // _PAGE_FACTOR in docs]
        order = {row["rowid"]: position for position, row in enumerate(rows)}

        def rank(row) -> Tuple[bool, int, int]:
            doc = docs[row["rowid"] // _PAGE_FACTOR]
            position = order[row["rowid"]]
            return (_is_paperwork(doc["title"]), position + collection_tier(doc["book"]) * _TIER_PLACES, position)

        rows.sort(key=rank)

        hits: List[dict] = []
        used: Dict[int, int] = {}
        seen_documents = set()
        for row in rows:
            doc_id, page = divmod(row["rowid"], _PAGE_FACTOR)
            if used.get(doc_id, 0) >= per_doc:
                continue
            doc = docs[doc_id]
            same = _same_document_key(doc["title"], page)
            if same in seen_documents:
                continue
            seen_documents.add(same)
            used[doc_id] = used.get(doc_id, 0) + 1
            hits.append({
                "kind": "manual",
                "title": f"{doc['title']} — page {page}",
                "doc_title": doc["title"],
                "url": pdf_url(doc["book"], doc["path"], page),
                "snippet": re.sub(r"\s+", " ", row["snip"] or "").strip(),
                "book": doc["book"],
                "page": page,
                "pages": doc["pages"],
                "doc_id": doc_id,
            })
            if len(hits) >= count:
                break
        return hits
    except sqlite3.Error as exc:
        logger.warning("Manual index search failed for %r: %s", query, exc)
        return []
    finally:
        conn.close()


def is_manual_url(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(".pdf")


def passage(url: str, budget: int = 4000) -> dict:
    """Text of the page a manual link points at, plus the pages after and
    before it while the budget lasts. Same keys as ``kiwix.fetch_article``."""
    result = {"url": url, "title": "", "content": "", "success": False, "error": ""}
    parts = urlsplit(url)
    if parts.path.startswith(config.ADDED_URL_PREFIX):
        book, path = config.ADDED_BOOK, unquote(parts.path[len(config.ADDED_URL_PREFIX):])
    else:
        match = re.match(r"^/content/([^/]+)/(.+)$", parts.path)
        if not match:
            result["error"] = "not a library document link"
            return result
        book = re.sub(r"_\d{4}-\d{2}(?:-\d{2})?$", "", unquote(match.group(1)))
        path = unquote(match.group(2))
    page_match = re.search(r"page=(\d+)", parts.fragment or "")
    page = int(page_match.group(1)) if page_match else 1
    conn = connect(readonly=True)
    if conn is None:
        result["error"] = "the manual index has not been built"
        return result
    try:
        doc = conn.execute("SELECT * FROM docs WHERE book = ? AND path = ?", (book, path)).fetchone()
        if doc is None:
            result["error"] = "this document is not in the manual index"
            return result
        total = doc["pages"] or 0
        page = max(1, min(page, max(total, 1)))
        order = [page, page + 1, page - 1, page + 2]
        chunks: List[Tuple[int, str]] = []
        used = 0
        for number in order:
            if number < 1 or (total and number > total) or used >= budget:
                continue
            row = conn.execute(
                "SELECT text FROM pages WHERE rowid = ?", (doc["id"] * _PAGE_FACTOR + number,)
            ).fetchone()
            text = (row["text"] if row else "").strip()
            if not text:
                continue
            room = budget - used
            if len(text) > room:
                if number != page and room < 400:
                    continue
                text = text[:room].rstrip() + " …"
            chunks.append((number, text))
            used += len(text)
        chunks.sort(key=lambda pair: pair[0])
        body = "\n\n".join(
            f"[page {number} of {total}{' — the matching page' if number == page and len(chunks) > 1 else ''}]\n{text}"
            for number, text in chunks
        )
        result.update(title=f"{doc['title']} — page {page}", content=body, success=bool(body))
        if not body:
            result["error"] = "no extractable text on that page (scanned image?)"
        return result
    except sqlite3.Error as exc:
        result["error"] = f"manual index error: {exc}"
        return result
    finally:
        conn.close()


def stats() -> dict:
    conn = connect(readonly=True)
    if conn is None:
        return {"built": False, "documents": 0, "pages": 0, "collections": []}
    try:
        docs = conn.execute(
            "SELECT book, COUNT(*) AS n, SUM(pages) AS pages, SUM(text_pages) AS text_pages, "
            "SUM(CASE WHEN pages > 0 AND text_pages * 2 < pages THEN 1 ELSE 0 END) AS scanned "
            "FROM docs GROUP BY book ORDER BY book"
        ).fetchall()
        sources = {row["book"]: row for row in conn.execute("SELECT * FROM sources")}
        collections = [{
            "book": row["book"],
            "documents": row["n"],
            "pages": row["pages"] or 0,
            "text_pages": row["text_pages"] or 0,
            "scanned_documents": row["scanned"] or 0,
            "complete": (bool(sources[row["book"]]["complete"]) if row["book"] in sources
                         else row["book"] == config.ADDED_BOOK),      # added one by one: never half-scanned
        } for row in docs]
        return {
            "built": True,
            "documents": sum(c["documents"] for c in collections),
            "pages": sum(c["text_pages"] for c in collections),
            "collections": collections,
            "size_mb": round(db_path().stat().st_size / 1e6, 1),
        }
    except sqlite3.Error as exc:
        return {"built": False, "documents": 0, "pages": 0, "collections": [], "error": str(exc)}
    finally:
        conn.close()


# ── Indexing (batch side) ──────────────────────────────────────────────────

def _run(args: List[str], timeout: int = 600) -> bytes:
    return subprocess.run(args, check=True, capture_output=True, timeout=timeout).stdout


def zim_pdf_count(zim: Path) -> int:
    """Number of PDFs in a ZIM according to its Counter metadata (cheap: no
    listing of a multi-million-entry archive just to find it has none)."""
    try:
        counter = _run(["zimdump", "show", "--url=Counter", "--ns=M", "--", str(zim)], timeout=120)
    except Exception:
        return -1          # unknown: caller falls back to listing
    match = re.search(rb"application/pdf=(\d+)", counter)
    return int(match.group(1)) if match else 0


def zim_pdf_entries(zim: Path) -> List[str]:
    listing = _run(["zimdump", "list", "--", str(zim)], timeout=3600).decode("utf-8", "replace")
    return sorted({line for line in listing.splitlines() if line.lower().endswith(".pdf")})


def read_zim_entry(zim: Path, entry: str) -> bytes:
    return _run(["zimdump", "show", f"--url={entry}", "--", str(zim)], timeout=600)


_JUNK_TITLE = re.compile(
    r"^(untitled|title|document\d*|microsoft word|powerpoint presentation|slide \d+|"
    r"layout \d+|print|scanned document|null|none|unknown|name|pdf|https?://|www\.)\b|"
    r"\.(docx?|pdf|indd|qxd|pmd?|fm|p65|wpd|pptx?|xlsx?|rtf|txt|pub|ai|eps|tmp|dvi|ps)$|"
    # Section names that authoring tools leave in the title field.
    r"^(chapter|appendix|section|part|preface|contents|table of contents|foreword|"
    r"introduction|cover|front ?matter|figure|change|summary of change)\b.{0,14}$",
    re.I,
)

# Boilerplate at the top of government publications — never the title.
_BOILERPLATE_LINE = re.compile(
    r"^(this publication (is available|supersedes)|\*?this (publication|is for)|distribution( restriction)?\b|"
    r"approved for public release|headquarters\b|department of the (army|navy|air force)\s*$|"
    r"unclassified\b|pin:|by order of|official:|washington,? d\.?c|www\.|https?://|"
    r"united states (army|marine corps|navy|air force)\s*$|page \d+|\d+\s*$)",
    re.I,
)

# Publication designations as they appear in file names, where "." is often
# written "x" and the publisher appends change/format suffixes:
# "ARN12086 ATP 3-50x21 FINAL WEB 2" -> "ATP 3-50.21".
_PUB_NUMBER = re.compile(
    r"(?<![A-Za-z])(ATTP|ATP|ADRP|ADP|FMI|FM|STP|GTA|TC|TM|TB ?MED|DA ?PAM|AR|JP|AFH|AFTTP|"
    r"MCRP|MCWP|MCTP|NTTP|NAVMC|CTA|SH|ST)[ _-]?"
    r"(\d{1,4}(?:[-.]\d+[A-Z]?)*(?:-[A-Z]{3,5})?)(?![A-Za-z0-9])",
    re.I,
)
_NO_SEPARATOR_OK = {"TB MED"}


def pub_designation(text: str) -> str:
    """``ATP 3-50.21`` from a file name such as ``ARN12086 ATP 3-50x21 FINAL WEB 2``."""
    name = re.sub(r"(?<=\d)x(?=\d)", ".", text or "", flags=re.I)
    name = re.sub(r"[-_ ]\d{3}[-_ ]WEB\b.*$", "", name, flags=re.I)        # "-000-WEB-1"
    name = re.sub(r"[-_ ](FINAL|WEB|C\d+|CHANGE)\b.*$", "", name, flags=re.I)
    name = re.sub(r"^(tc|fm|atp|tm|stp)(\d{1,2})[ _](\d)", r"\1 \2-\3", name, flags=re.I)  # "tc3 97.61"
    match = _PUB_NUMBER.search(name)
    if not match:
        return ""
    series = re.sub(r"[\s_]+", "", match.group(1).upper())
    series = {"TBMED": "TB MED", "DAPAM": "DA PAM"}.get(series, series)
    number = match.group(2).upper()
    if not re.search(r"[-.]", number) and series not in _NO_SEPARATOR_OK:
        return ""
    return f"{series} {number}"


def _pretty_filename(path: str) -> str:
    stem = unquote(path.rsplit("/", 1)[-1])
    stem = re.sub(r"\.pdf$", "", stem, flags=re.I)
    stem = re.sub(r"[_]+", " ", stem)
    return re.sub(r"\s+", " ", stem).strip() or "document"


def _first_lines(text: str, limit: int = 110) -> str:
    """A title from the top of page one: the first line if it already reads
    like one, otherwise up to three short lines joined."""
    lines: List[str] = []
    for line in (text or "").splitlines():
        line = re.sub(r"\.{4,}.*$", "", line)                 # dot leaders of a contents page
        line = re.sub(r"\s+", " ", line).strip(" .-–—_*\"'“”")
        if len(line) < 3 or not re.search(r"[A-Za-z]{3}", line):
            continue
        if _JUNK_TITLE.search(line) or _BOILERPLATE_LINE.search(line):
            continue
        if line.lower() in (seen.lower() for seen in lines):
            continue
        lines.append(line)
        if len(lines) == 1 and len(line) >= 28:
            break
        if len(" — ".join(lines)) >= 60 or len(lines) >= 3:
            break
    return " — ".join(lines)[:limit].strip(" —")


def doc_title(meta_title: str, first_text: str, path: str) -> str:
    """Best human title for a PDF.

    The metadata title unless it is junk ("Microsoft Word - cover.doc",
    "Chapter 1"), else the first real lines of page one. A publication number
    found in the file name is put in front when the title lacks it
    ("ATP 3-50.21 — Survival"); otherwise the file name follows in brackets
    when it adds anything."""
    filename = _pretty_filename(path)
    title = re.sub(r"\s+", " ", (meta_title or "")).strip()
    if title.lower().startswith("microsoft word - "):
        title = title[17:].strip()
    if len(title) < 4 or _JUNK_TITLE.search(title) or _BOILERPLATE_LINE.search(title):
        title = _first_lines(first_text)
    number = pub_designation(filename)
    if not title:
        return f"{number} ({filename})" if number else filename
    title = title[:150]
    squashed = re.sub(r"[^a-z0-9]", "", title.lower())
    if number:
        if re.sub(r"[^a-z0-9]", "", number.lower()) in squashed:
            return title
        return f"{number} — {title}"
    if re.sub(r"[^a-z0-9]", "", filename.lower()) in squashed or len(filename) > 60:
        return title
    return f"{title} ({filename})"


def extract_pdf(data: bytes, ocr: bool = False) -> Tuple[str, List[str], int]:
    """Return ``(metadata_title, page_texts, ocr_page_count)``. Imports
    PyMuPDF lazily: only the indexer needs it."""
    import fitz  # PyMuPDF

    fitz.TOOLS.mupdf_display_errors(False)
    tessdata = next((d for d in _TESSDATA_DIRS if os.path.isdir(d)), None) if ocr else None
    pages: List[str] = []
    ocr_pages = 0
    with fitz.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ValueError("encrypted")
        meta_title = (doc.metadata or {}).get("title") or ""
        for page in doc:
            try:
                text = page.get_text("text")
            except Exception:
                text = ""
            if tessdata and len(text.strip()) < 24 and ocr_pages < _OCR_MAX_PAGES and page.get_images():
                try:
                    textpage = page.get_textpage_ocr(language="eng", dpi=200, full=True, tessdata=tessdata)
                    text = page.get_text("text", textpage=textpage)
                    ocr_pages += 1
                except Exception:
                    pass
            pages.append(text.replace("\x00", ""))
    return meta_title, pages, ocr_pages


def _extract_job(args: Tuple[str, str, bool]) -> dict:
    """Worker: pull one PDF out of a ZIM and extract it. Never raises."""
    zim, entry, ocr = args
    out = {"entry": entry, "pages": [], "title": "", "bytes": 0, "ocr_pages": 0, "error": ""}
    try:
        data = read_zim_entry(Path(zim), entry)
        out["bytes"] = len(data)
        if not data.startswith(b"%PDF") and b"%PDF" not in data[:1024]:
            out["error"] = "not a PDF"
            return out
        if len(data) > _MAX_PDF_BYTES:
            out["error"] = "too large"
            return out
        meta_title, pages, ocr_pages = extract_pdf(data, ocr=ocr)
        first = next((p for p in pages if p.strip()), "")
        out.update(pages=pages, title=doc_title(meta_title, first, entry), ocr_pages=ocr_pages)
    except subprocess.TimeoutExpired:
        out["error"] = "timeout"
    except subprocess.CalledProcessError:
        out["error"] = "unreadable entry (a redirect, or not stored in this ZIM)"
    except Exception as exc:  # corrupt PDFs are routine in web scrapes
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    if not out["title"]:
        out["title"] = _pretty_filename(entry)
    return out


def _quiet_worker() -> None:
    """Worker start-up: a crash here must not leave a core file (or, on desktops
    that watch for them, a crash dialog) for every malformed PDF in a scrape."""
    try:
        import ctypes
        ctypes.CDLL(None).prctl(4, 0)          # PR_SET_DUMPABLE = 0
    except Exception:
        pass


def _crashed(job: Tuple[str, str, bool]) -> dict:
    return {"entry": job[1], "pages": [], "title": _pretty_filename(job[1]), "bytes": 0, "ocr_pages": 0,
            "error": "crashed the PDF reader (malformed file)"}


def _extract_all(jobs: List[Tuple[str, str, bool]], workers: int, pool_factory=None):
    """Run ``_extract_job`` over ``jobs`` in worker processes, yielding one
    result per job, in order.

    A malformed PDF can segfault the PDF library. That kills the worker, and a
    process pool that loses a worker fails *every* job still in flight — so
    jobs go out in small windows, and when a window breaks, its unfinished
    jobs are retried one at a time, each in a process of its own. The one that
    dies again is recorded as unreadable; the rest proceed normally.
    """
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool

    factory = pool_factory or (lambda count: ProcessPoolExecutor(max_workers=count, initializer=_quiet_worker))
    window = max(8, workers * 4)
    pool = factory(workers)
    try:
        for offset in range(0, len(jobs), window):
            batch = jobs[offset:offset + window]
            futures = [pool.submit(_extract_job, job) for job in batch]
            unfinished: List[Tuple[int, Tuple[str, str, bool]]] = []
            results: Dict[int, dict] = {}
            for index, (job, future) in enumerate(zip(batch, futures)):
                try:
                    results[index] = future.result()
                except BrokenProcessPool:
                    unfinished.append((index, job))
                except Exception as exc:
                    results[index] = dict(_crashed(job), error=f"{type(exc).__name__}: {exc}"[:200])
            if unfinished:
                pool.shutdown(wait=False, cancel_futures=True)
                for index, job in unfinished:
                    single = factory(1)
                    try:
                        results[index] = single.submit(_extract_job, job).result()
                    except BrokenProcessPool:
                        results[index] = _crashed(job)
                    except Exception as exc:
                        results[index] = dict(_crashed(job), error=f"{type(exc).__name__}: {exc}"[:200])
                    finally:
                        single.shutdown(wait=False, cancel_futures=True)
                pool = factory(workers)
            for index in range(len(batch)):
                yield results[index]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _store(conn: sqlite3.Connection, book: str, result: dict) -> None:
    pages = result["pages"]
    row = conn.execute("SELECT id FROM docs WHERE book = ? AND path = ?", (book, result["entry"])).fetchone()
    if row:
        doc_id = row["id"]
        conn.execute("DELETE FROM pages WHERE rowid >= ? AND rowid < ?",
                     (doc_id * _PAGE_FACTOR, (doc_id + 1) * _PAGE_FACTOR))
        conn.execute(
            "UPDATE docs SET title=?, pages=?, text_pages=?, ocr_pages=?, bytes=?, error=?, indexed_at=? WHERE id=?",
            (result["title"], len(pages), sum(1 for p in pages if len(p.strip()) >= 24),
             result["ocr_pages"], result["bytes"], result["error"], time.time(), doc_id),
        )
    else:
        cursor = conn.execute(
            "INSERT INTO docs (book, path, title, pages, text_pages, ocr_pages, bytes, error, indexed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (book, result["entry"], result["title"], len(pages),
             sum(1 for p in pages if len(p.strip()) >= 24), result["ocr_pages"], result["bytes"],
             result["error"], time.time()),
        )
        doc_id = cursor.lastrowid
    conn.executemany(
        "INSERT INTO pages (rowid, text, title) VALUES (?,?,?)",
        [
            (doc_id * _PAGE_FACTOR + number, text, result["title"])
            for number, text in enumerate(pages[: _PAGE_FACTOR - 1], start=1)
            if text.strip()
        ],
    )


TITLES_ENTRY = "titles.json"


def apply_titles(book: str, titles: Dict[str, str]) -> int:
    """Give documents of a collection their catalogued titles.

    A PDF's own metadata is often junk ("/tardir/mig/a325861.tiff") and a
    scanned cover OCRs badly; a curated collection knows better. ``titles``
    maps entry paths to titles. Only the title changes — page text (and any
    OCR already done) stays.
    """
    if not titles:
        return 0
    conn = connect()
    changed = 0
    try:
        for row in conn.execute("SELECT id, path, title FROM docs WHERE book = ?", (book,)).fetchall():
            title = re.sub(r"\s+", " ", str(titles.get(row["path"]) or "")).strip()[:200]
            if not title or title == row["title"]:
                continue
            conn.execute("UPDATE docs SET title = ? WHERE id = ?", (title, row["id"]))
            conn.execute("UPDATE pages SET title = ? WHERE rowid >= ? AND rowid < ?",
                         (title, row["id"] * _PAGE_FACTOR, (row["id"] + 1) * _PAGE_FACTOR))
            changed += 1
        conn.commit()
    finally:
        conn.close()
    return changed


def _catalogued_titles(zim: Path) -> Dict[str, str]:
    """The ``titles.json`` a curated collection carries, or {}."""
    try:
        import json
        data = json.loads(read_zim_entry(zim, TITLES_ENTRY).decode("utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def book_id_for(zim: Path) -> str:
    return re.sub(r"_\d{4}-\d{2}(?:-\d{2})?$", "", zim.name[:-4] if zim.name.endswith(".zim") else zim.name)


def _drop_book(conn: sqlite3.Connection, book: str) -> None:
    for row in conn.execute("SELECT id FROM docs WHERE book = ?", (book,)).fetchall():
        conn.execute("DELETE FROM pages WHERE rowid >= ? AND rowid < ?",
                     (row["id"] * _PAGE_FACTOR, (row["id"] + 1) * _PAGE_FACTOR))
    conn.execute("DELETE FROM docs WHERE book = ?", (book,))
    conn.execute("DELETE FROM sources WHERE book = ?", (book,))


def index_zim(
    zim: Path,
    workers: int = 0,
    ocr: bool = False,
    force: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Index (or finish indexing) the PDFs of one ZIM. Resumable: documents
    already stored are skipped, and a ZIM whose file is unchanged and marked
    complete costs one stat call. With ``ocr`` it revisits documents whose
    pages are mostly images."""
    say = progress or (lambda message: None)
    zim = Path(zim)
    book = book_id_for(zim)
    stat = zim.stat()
    summary = {"book": book, "pdfs": 0, "indexed": 0, "skipped": 0, "failed": 0, "pages": 0}

    conn = connect()
    try:
        source = conn.execute("SELECT * FROM sources WHERE book = ?", (book,)).fetchone()
        unchanged = bool(
            source and source["zim_file"] == zim.name and source["size"] == stat.st_size
            and abs(source["mtime"] - stat.st_mtime) < 1
        )
        if source and (force or not unchanged):
            say(f"{book}: collection changed, re-indexing")
            _drop_book(conn, book)
            conn.commit()
            source, unchanged = None, False
        if unchanged and source["complete"] and not ocr:
            summary["pdfs"] = source["pdf_count"]
            summary["skipped"] = source["pdf_count"]
            return summary

        if not source:
            count = zim_pdf_count(zim)
            if count == 0:
                conn.execute(
                    "INSERT OR REPLACE INTO sources (book, zim_file, size, mtime, pdf_count, complete, indexed_at) "
                    "VALUES (?,?,?,?,0,1,?)", (book, zim.name, stat.st_size, stat.st_mtime, time.time()))
                conn.commit()
                return summary

        entries = zim_pdf_entries(zim)
        summary["pdfs"] = len(entries)
        conn.execute(
            "INSERT INTO sources (book, zim_file, size, mtime, pdf_count, complete, indexed_at) "
            "VALUES (?,?,?,?,?,0,?) ON CONFLICT(book) DO UPDATE SET pdf_count = excluded.pdf_count",
            (book, zim.name, stat.st_size, stat.st_mtime, len(entries), time.time()))
        conn.commit()

        known = {row["path"]: row for row in conn.execute(
            "SELECT path, pages, text_pages, ocr_pages, error FROM docs WHERE book = ?", (book,))}
        todo = []
        for entry in entries:
            row = known.get(entry)
            if row is None:
                todo.append(entry)
            elif ocr and not row["error"] and row["pages"] and row["text_pages"] * 2 < row["pages"] \
                    and not row["ocr_pages"]:
                todo.append(entry)          # mostly images, never OCRed
            else:
                summary["skipped"] += 1
        if not todo:
            conn.execute("UPDATE sources SET complete = 1, indexed_at = ? WHERE book = ?", (time.time(), book))
            conn.commit()
            return summary

        workers = workers or max(1, min(12, (os.cpu_count() or 4) - 2))
        say(f"{book}: {len(todo)} PDFs to read ({summary['skipped']} already done), {workers} workers"
            + (", OCR on" if ocr else ""))
        started = time.time()
        done = 0
        for result in _extract_all([(str(zim), entry, ocr) for entry in todo], workers):
            _store(conn, book, result)
            done += 1
            if result["error"]:
                summary["failed"] += 1
            else:
                summary["indexed"] += 1
                summary["pages"] += len(result["pages"])
            if done % 50 == 0 or done == len(todo):
                conn.commit()
                rate = done / max(time.time() - started, 0.001)
                say(f"{book}: {done}/{len(todo)} PDFs, {summary['pages']} pages, {rate:.1f} PDFs/s")
        conn.execute("UPDATE sources SET complete = 1, indexed_at = ? WHERE book = ?", (time.time(), book))
        conn.commit()
        conn.close()
        renamed = apply_titles(book, _catalogued_titles(zim))
        if renamed:
            say(f"{book}: {renamed} titles taken from the collection's catalogue")
        return summary
    finally:
        conn.close()


def index_all(
    workers: int = 0,
    ocr: bool = False,
    force: bool = False,
    only: Optional[Iterable[str]] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> List[dict]:
    """Index every finished ZIM in the library folder, newest edition of each
    collection only, and forget collections that are no longer installed."""
    say = progress or (lambda message: None)
    zim_dir = config.zim_dir()
    newest: Dict[str, Path] = {}
    for zim in sorted(zim_dir.glob("*.zim")):
        if Path(str(zim) + ".aria2").exists():
            continue                        # still downloading
        book = book_id_for(zim)
        if book not in newest or zim.name > newest[book].name:
            newest[book] = zim
    wanted = set(only or ())
    results = []
    for book, zim in sorted(newest.items()):
        if wanted and book not in wanted:
            continue
        try:
            results.append(index_zim(zim, workers=workers, ocr=ocr, force=force, progress=progress))
        except Exception as exc:
            say(f"{book}: FAILED — {type(exc).__name__}: {exc}")
            results.append({"book": book, "error": str(exc)})
    if not wanted:
        conn = connect()
        try:
            gone = [row["book"] for row in conn.execute("SELECT book FROM sources")
                    if row["book"] not in newest]
            for book in gone:
                say(f"{book}: no longer installed, removing from the index")
                _drop_book(conn, book)
            conn.commit()
        finally:
            conn.close()
    return results
