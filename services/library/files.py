"""The owner's own files.

Anything copied into the library folder — ``<library>/<any folder>/…`` — is
part of the library: a PDF or an e-book is read page by page into the same
index the manuals use, gets a title, an author and a shelf, and from then on
is found by ``library_search``, listed by the shelves and opened in the
reader. The files stay exactly where they were put; nothing is moved,
renamed or copied.

``scan()`` is the whole job and can be run as often as wanted: a file that
has not changed costs one stat call, a new one is read, one that is gone is
forgotten, and the same file under two names is kept once. Files still being
copied (changed within the last minute) are left for the next run.

    <library>/zim, index, manuals, added   are the library's own and are skipped
    readable: .pdf .epub .mobi .fb2 .txt .html

Shelves come from the folder a file sits in and the words of its title
(:mod:`.shelves`); :mod:`.classify` asks a model for the ones that leaves
unsure.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote

from . import config

logger = logging.getLogger(__name__)

BOOK = config.FILES_BOOK
RESERVED = frozenset({"zim", "index", "manuals", "added", "shelves", "lost+found"})
READABLE: Dict[str, str] = {".pdf": "pdf", ".epub": "epub", ".mobi": "mobi", ".fb2": "fb2", ".txt": "txt",
                            ".htm": "html", ".html": "html"}
MEDIA_TYPES = {"pdf": "application/pdf", "epub": "application/epub+zip", "mobi": "application/x-mobipocket-ebook",
               "fb2": "application/x-fictionbook+xml", "txt": "text/plain; charset=utf-8", "html": "text/plain; charset=utf-8"}
MAX_BYTES = 600 * 1024 * 1024
MIN_TEXT_BYTES = 4096            # a "read me" is not a book
SETTLE_SECONDS = 60              # a file changed more recently than this may still be arriving
TEXT_PAGE_CHARS = 3000
# One fixed page size for e-books, so "page 41" means the same page to the
# index, to a link and to the reader.
EBOOK_PAGE = (0, 0, 440, 660)
EBOOK_FONT = 11

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path     TEXT PRIMARY KEY,
    size     INTEGER NOT NULL,
    mtime    REAL NOT NULL,
    sha256   TEXT NOT NULL DEFAULT '',
    state    TEXT NOT NULL DEFAULT '',
    same_as  TEXT NOT NULL DEFAULT '',
    note     TEXT NOT NULL DEFAULT '',
    seen_at  REAL
);
CREATE INDEX IF NOT EXISTS files_by_hash ON files (sha256);
"""


# ── where the files are ────────────────────────────────────────────────────

def roots() -> List[Path]:
    """The folders of the library that hold the owner's files: every
    top-level folder except the library's own."""
    base = config.library_dir()
    try:
        return sorted(entry for entry in base.iterdir()
                      if entry.is_dir() and not entry.name.startswith(".") and entry.name.lower() not in RESERVED)
    except OSError:
        return []


def kind_of(name: str) -> str:
    return READABLE.get(os.path.splitext(str(name))[1].lower(), "")


def _relative(path: Path) -> str:
    return path.relative_to(config.library_dir()).as_posix()


def file_for(relative: str, indexed_only: bool = True) -> Optional[Path]:
    """The file for a catalogue path — only a readable file that lies in one
    of the owner's folders (and, by default, has been taken into the index).
    Never anything else on this machine, whatever the path says."""
    base = config.library_dir().resolve()
    relative = unquote(str(relative or "")).lstrip("/")
    parts = relative.split("/")
    if len(parts) < 2 or ".." in parts or parts[0].startswith(".") or parts[0].lower() in RESERVED or not kind_of(relative):
        return None
    try:
        path = (base / relative).resolve()
    except (OSError, ValueError):
        return None
    if base not in path.parents or not path.is_file():
        return None
    if indexed_only:
        from . import manuals
        conn = manuals.connect(readonly=True)
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT 1 FROM docs WHERE book = ? AND path = ?", (BOOK, relative)).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            conn.close()
        if row is None:
            return None
    return path


def candidates() -> Tuple[List[Path], Dict[str, int], int]:
    """``(readable files, {other extension: how many}, files still arriving)``."""
    readable: List[Path] = []
    other: Dict[str, int] = {}
    arriving = 0
    now = time.time()
    for root in roots():
        for folder, folders, names in os.walk(root, followlinks=False):
            folders[:] = sorted(name for name in folders if not name.startswith("."))
            for name in sorted(names):
                if name.startswith("."):
                    continue
                path = Path(folder) / name
                extension = os.path.splitext(name)[1].lower()
                if extension not in READABLE:
                    other[extension or "(none)"] = other.get(extension or "(none)", 0) + 1
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if not path.is_file() or path.is_symlink():
                    continue
                if now - stat.st_mtime < SETTLE_SECONDS:
                    arriving += 1
                    continue
                readable.append(path)
    return readable, other, arriving


# ── reading one file ───────────────────────────────────────────────────────

def open_document(path: str):
    """Open a PDF or e-book with PyMuPDF. E-books have no pages of their own;
    they are laid out on one fixed page size so page numbers stay put."""
    import fitz

    fitz.TOOLS.mupdf_display_errors(False)
    document = fitz.open(path)
    if document.is_reflowable:
        document.layout(rect=fitz.Rect(*EBOOK_PAGE), fontsize=EBOOK_FONT)
    return document


def _text_pages(text: str) -> List[str]:
    """Plain text cut into pages at paragraph ends."""
    pages: List[str] = []
    current: List[str] = []
    size = 0
    for paragraph in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        paragraph = paragraph.strip("\n")
        if not paragraph.strip():
            continue
        if size and size + len(paragraph) > TEXT_PAGE_CHARS:
            pages.append("\n\n".join(current))
            current, size = [], 0
        while len(paragraph) > TEXT_PAGE_CHARS * 2:               # one endless paragraph
            pages.append(paragraph[:TEXT_PAGE_CHARS])
            paragraph = paragraph[TEXT_PAGE_CHARS:]
        current.append(paragraph)
        size += len(paragraph)
    if current:
        pages.append("\n\n".join(current))
    return pages


def epub_details(path: str) -> dict:
    """What an e-book says about itself in its package file: ``title``,
    ``author``, ``subject`` (its subject headings, joined) and ``year``.
    {} when it cannot be read — the book's pages are still indexed."""
    import xml.etree.ElementTree as ET
    import zipfile

    try:
        with zipfile.ZipFile(path) as book:
            container = ET.fromstring(book.read("META-INF/container.xml"))
            package = next((node.get("full-path") for node in container.iter() if node.tag.endswith("rootfile")), "")
            if not package or len(package) > 400:
                return {}
            info = book.getinfo(package)
            if info.file_size > 4_000_000:
                return {}
            root = ET.fromstring(book.read(package))
    except Exception:
        return {}
    found: Dict[str, List[str]] = {}
    for node in root.iter():
        name = node.tag.rsplit("}", 1)[-1].lower()
        if name in ("title", "creator", "subject", "date") and (node.text or "").strip():
            found.setdefault(name, []).append(re.sub(r"\s+", " ", node.text).strip())
    year = None
    for value in found.get("date", []):
        match = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", value)
        if match:
            year = int(match.group(1))
            break
    return {"title": (found.get("title") or [""])[0][:300], "author": "; ".join(found.get("creator", [])[:3])[:200],
            "subject": " ; ".join(found.get("subject", [])[:12])[:600], "year": year}


def extract(path: str, ocr: bool = False) -> dict:
    """Everything the index needs from one file: ``pages`` (text per page),
    ``title`` / ``author`` / ``subject`` as its own metadata gives them,
    ``contents`` (its first headings), ``ocr_pages``."""
    from . import manuals

    kind = kind_of(path)
    out = {"pages": [], "title": "", "author": "", "subject": "", "contents": "", "ocr_pages": 0, "year": None}
    if kind in ("txt", "html"):
        raw = Path(path).read_bytes()
        text = raw.decode("utf-8", errors="replace")
        if kind == "html":
            from .text import html_to_text
            out["title"], text = html_to_text(text)
        out["pages"] = _text_pages(text.replace("\x00", ""))
        return out
    tessdata = next((d for d in manuals._TESSDATA_DIRS if os.path.isdir(d)), None) if ocr and kind == "pdf" else None
    with open_document(path) as document:
        if document.needs_pass:
            raise ValueError("encrypted")
        meta = document.metadata or {}
        out["title"] = str(meta.get("title") or "")
        out["author"] = str(meta.get("author") or "")
        out["subject"] = " ; ".join(part for part in (str(meta.get("subject") or ""), str(meta.get("keywords") or "")) if part)[:600]
        try:
            out["contents"] = " | ".join(str(entry[1]).strip() for entry in (document.get_toc() or [])[:40] if entry[0] <= 2)[:1500]
        except Exception:
            pass
        if kind == "epub":                      # the package file knows more than the reader passes on
            for key, value in epub_details(path).items():
                if value:
                    out[key] = value
        for page in document:
            try:
                text = page.get_text("text")
            except Exception:
                text = ""
            if tessdata and len(text.strip()) < 24 and out["ocr_pages"] < manuals._OCR_MAX_PAGES and page.get_images():
                try:
                    textpage = page.get_textpage_ocr(language="eng", dpi=200, full=True, tessdata=tessdata)
                    text = page.get_text("text", textpage=textpage)
                    out["ocr_pages"] += 1
                except Exception:
                    pass
            out["pages"].append(text.replace("\x00", ""))
    return out


def _extract_job(args: Tuple[str, str, bool]) -> dict:
    """Worker: read one file. Never raises. (Same shape of job and result as
    the manuals' worker, so the crash-proof pool there runs both.)"""
    root, relative, ocr = args
    out = {"entry": relative, "pages": [], "title": "", "author": "", "subject": "", "contents": "", "bytes": 0,
           "ocr_pages": 0, "error": "", "year": None}
    try:
        path = os.path.join(root, relative)
        out["bytes"] = os.path.getsize(path)
        out.update(extract(path, ocr=ocr))
    except ValueError as exc:
        out["error"] = "password-protected" if "encrypted" in str(exc) else f"ValueError: {exc}"[:200]
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


# ── titles ─────────────────────────────────────────────────────────────────

_SITE_TAGS = re.compile(
    r"[\(\[\{][^\)\]\}]*(z-?lib|libgen|pdfdrive|b-ok|1lib|annas?[- ]archive|epub|mobi|azw3?|retail|www\.|\.com|\.org|\.net)"
    r"[^\)\]\}]*[\)\]\}]", re.I)
_COPY_COUNTER = re.compile(r"\s*[\(\[]\d{1,2}[\)\]]\s*$")
# A name that is a reference code: capitals and digits ("ADA267521", "CIA-RDP96-00788R…"),
# or a numbered scan or chapter. Anything with ordinary lower-case words in it is a title.
_CODE_CAPITALS = re.compile(r"^[A-Z]{2,8}[-_]?[A-Z0-9]*\d[A-Z0-9\-_.\[\]]*$")
_CODE_NUMBERED = re.compile(r"^(?:ch(?:apter)?|scan|doc(?:ument)?|img|image|page|file|untitled)[-_ ]?\d+(?:[-_]\d+)*$|^[a-f0-9]{16,}$|^\d+$", re.I)
_JUNK_AUTHOR = re.compile(r"^(unknown|anonymous|admin(istrator)?|user|owner|author|none|null|pc|microsoft|adobe|calibre|"
                          r"acrobat|scanner|test|default)\b|@|\.(com|org|net)\b|^\W*$", re.I)


def name_title(file_name: str) -> str:
    """A title read off a file name: site tags, copy counters and underscores gone."""
    stem = os.path.splitext(unquote(str(file_name)).rsplit("/", 1)[-1])[0]
    stem = _SITE_TAGS.sub(" ", stem)
    stem = _COPY_COUNTER.sub("", stem)
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.sub(r"(?<=[A-Za-z])\.(?=[A-Za-z])", " ", stem) if stem.count(".") >= 3 and " " not in stem else stem
    return re.sub(r"\s+", " ", stem).strip(" -–—.,;")


def is_code_name(file_name: str) -> bool:
    """A file named by a number or a reference code rather than by its title."""
    stem = _COPY_COUNTER.sub("", os.path.splitext(str(file_name).rsplit("/", 1)[-1])[0]).strip()
    squeezed = stem.replace(" ", "")
    if not squeezed:
        return False
    if _CODE_CAPITALS.match(squeezed) or _CODE_NUMBERED.match(squeezed):
        return True
    digits = sum(char.isdigit() for char in squeezed)
    return len(squeezed) >= 6 and digits * 10 >= len(squeezed) * 4        # mostly a number: "2410.18089v1", "cia-rdp96-00788r…"


def tidy_title(meta_title: str, first_text: str, file_name: str) -> str:
    """The best title for a file without asking anyone: its own metadata
    when that is a real title, else its name, else its first lines — and for
    a file named by a reference code, the title followed by that code."""
    from . import manuals

    title = re.sub(r"\s+", " ", str(meta_title or "")).strip()
    if title.lower().startswith("microsoft word - "):
        title = title[17:].strip()
    named = name_title(file_name)
    coded = is_code_name(file_name)
    junk = (len(title) < 4 or manuals._JUNK_TITLE.search(title) or manuals._BOILERPLATE_LINE.search(title)
            or is_code_name(title) or re.fullmatch(r"[\W\d_]+", title))
    if junk:
        title = "" if coded else named
    if not title:
        title = manuals._first_lines(first_text)
    if not title:
        return named or "Untitled"
    title = title[:180]
    if coded and named and named.lower() not in title.lower():
        return f"{title} ({named})"
    return title


def tidy_author(author: str) -> str:
    author = re.sub(r"\s+", " ", str(author or "")).strip(" ;,")
    if not author or len(author) > 120 or _JUNK_AUTHOR.search(author):
        return ""
    return author


# ── the scan ───────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _drop(conn: sqlite3.Connection, relative: str) -> None:
    from . import catalogue, manuals

    row = conn.execute("SELECT id FROM docs WHERE book = ? AND path = ?", (BOOK, relative)).fetchone()
    if row:
        conn.execute("DELETE FROM pages WHERE rowid >= ? AND rowid < ?",
                     (row["id"] * manuals._PAGE_FACTOR, (row["id"] + 1) * manuals._PAGE_FACTOR))
        conn.execute("DELETE FROM docs WHERE id = ?", (row["id"],))
    catalogue.forget(conn, BOOK, relative)
    conn.execute("DELETE FROM files WHERE path = ?", (relative,))


def _lock():
    """One scan at a time, across the web app and the command line."""
    folder = config.library_dir() / "index"
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(folder / ".files-scan.lock", "w")
    try:
        try:
            import fcntl
        except ImportError:  # Windows: no fcntl, lock the first byte instead
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def scan(progress: Optional[Callable[[str], None]] = None, ocr: bool = False, workers: int = 0,
         limit: int = 0) -> dict:
    """Take the owner's files into the library. Returns what happened:
    ``found`` readable files, ``new`` read this time, ``same`` (a second copy
    of a file already here), ``failed``, ``removed`` (gone from disk),
    ``arriving`` (still being copied — run again), ``other`` (kinds of file
    the library cannot read, with counts)."""
    from . import catalogue, manuals, shelves

    say = progress or (lambda message: None)
    summary = {"found": 0, "new": 0, "same": 0, "failed": 0, "removed": 0, "unchanged": 0, "arriving": 0, "other": {},
               "busy": False}
    lock = _lock()
    if lock is None:
        summary["busy"] = True
        return summary
    try:
        base = config.library_dir()
        readable, summary["other"], summary["arriving"] = candidates()
        summary["found"] = len(readable)
        conn = manuals.connect()
        try:
            known = {row["path"]: row for row in conn.execute("SELECT * FROM files")}
            on_disk = set()
            todo: List[Tuple[str, Path, os.stat_result]] = []
            for path in readable:
                relative = _relative(path)
                on_disk.add(relative)
                stat = path.stat()
                row = known.get(relative)
                if row is not None and row["size"] == stat.st_size and abs(row["mtime"] - stat.st_mtime) < 1:
                    wants_ocr = False
                    if ocr and row["state"] == "indexed" and kind_of(relative) == "pdf":
                        doc = conn.execute("SELECT pages, text_pages, ocr_pages FROM docs WHERE book = ? AND path = ?",
                                           (BOOK, relative)).fetchone()
                        wants_ocr = bool(doc and doc["pages"] and doc["text_pages"] * 2 < doc["pages"] and not doc["ocr_pages"])
                    if not wants_ocr:
                        summary["unchanged"] += 1
                        continue
                todo.append((relative, path, stat))
            # Files that are gone — but never because the whole folder is away.
            if base.is_dir():
                for relative in sorted(set(known) - on_disk):
                    top = base / relative.split("/", 1)[0]
                    if top.is_dir() and not (base / relative).exists():
                        _drop(conn, relative)
                        summary["removed"] += 1
                conn.commit()
            # Of two copies of one file, keep the one without "(1)" in its name.
            todo.sort(key=lambda entry: (bool(_COPY_COUNTER.search(os.path.splitext(entry[0])[0])), entry[0]))
            if limit:
                todo = todo[:limit]
            if not todo:
                return summary

            say(f"your files: {len(todo)} to read ({summary['unchanged']} already in the library)")
            hashes = {row["sha256"]: row["path"] for row in conn.execute(
                "SELECT sha256, path FROM files WHERE state = 'indexed' AND sha256 != ''")}
            jobs: List[Tuple[str, str, bool]] = []
            stats: Dict[str, Tuple[os.stat_result, str]] = {}
            for relative, path, stat in todo:
                kind = kind_of(relative)
                note = ""
                if stat.st_size > MAX_BYTES:
                    note = "too large to read"
                elif kind in ("txt", "html") and stat.st_size < MIN_TEXT_BYTES:
                    note = "too short to be a document"
                if note:
                    conn.execute("INSERT OR REPLACE INTO files (path, size, mtime, sha256, state, same_as, note, seen_at) "
                                 "VALUES (?,?,?,?,?,?,?,?)", (relative, stat.st_size, stat.st_mtime, "", "skipped", "", note, time.time()))
                    continue
                try:
                    digest = _sha256(path)
                except OSError as exc:
                    conn.execute("INSERT OR REPLACE INTO files (path, size, mtime, sha256, state, same_as, note, seen_at) "
                                 "VALUES (?,?,?,?,?,?,?,?)", (relative, stat.st_size, stat.st_mtime, "", "failed", "",
                                                              f"unreadable: {exc.strerror or exc}", time.time()))
                    summary["failed"] += 1
                    continue
                first = hashes.get(digest)
                if first and first != relative and (base / first).is_file():
                    _drop(conn, relative)
                    conn.execute("INSERT OR REPLACE INTO files (path, size, mtime, sha256, state, same_as, note, seen_at) "
                                 "VALUES (?,?,?,?,?,?,?,?)", (relative, stat.st_size, stat.st_mtime, digest, "same", first,
                                                              "the same file as " + first, time.time()))
                    summary["same"] += 1
                    continue
                hashes[digest] = relative
                stats[relative] = (stat, digest)
                jobs.append((str(base), relative, ocr))
            conn.commit()

            workers = workers or max(1, min(8, (os.cpu_count() or 4) - 2))
            started = time.time()
            for number, result in enumerate(manuals._extract_all(jobs, workers, work=_extract_job), start=1):
                relative = result["entry"]
                stat, digest = stats[relative]
                pages = result["pages"]
                first_text = next((page for page in pages if page.strip()), "")
                title = tidy_title(result.get("title", ""), first_text, relative)
                result["title"] = title
                manuals._store(conn, BOOK, result)
                failed = bool(result["error"]) or not pages
                state = "failed" if failed else "indexed"
                conn.execute("INSERT OR REPLACE INTO files (path, size, mtime, sha256, state, same_as, note, seen_at) "
                             "VALUES (?,?,?,?,?,?,?,?)", (relative, stat.st_size, stat.st_mtime, digest, state, "",
                                                          result["error"] or ("no pages" if not pages else ""), time.time()))
                if failed:
                    summary["failed"] += 1
                    # Nothing to read, nothing to find: keep it out of the index.
                    row = conn.execute("SELECT id FROM docs WHERE book = ? AND path = ?", (BOOK, relative)).fetchone()
                    if row:
                        conn.execute("DELETE FROM docs WHERE id = ?", (row["id"],))
                    catalogue.forget(conn, BOOK, relative)
                else:
                    summary["new"] += 1
                    shelf, why = shelves.place_document(title, folder=relative, subjects=result.get("subject", ""))
                    kind = kind_of(relative)
                    year = result.get("year") if isinstance(result.get("year"), int) else None
                    catalogue.put(conn, BOOK, relative, title, shelf, author=tidy_author(result.get("author", "")),
                                  dates=str(year) if year else "", year=year,
                                  form="book" if kind in ("epub", "mobi", "fb2") else "document",
                                  note="", source="folder" if why.startswith("folder") else "rule")
                if ocr or number % 20 == 0 or number == len(jobs):
                    conn.commit()                    # OCR is slow: never hold the index for more than one document
                    rate = number / max(time.time() - started, 0.001)
                    say(f"your files: {number}/{len(jobs)} read, {rate:.1f} a second")
            conn.commit()
        finally:
            conn.close()
        return summary
    finally:
        lock.close()


def stats() -> dict:
    """How many of the owner's files are in the library, and what was left out."""
    from . import manuals

    conn = manuals.connect(readonly=True)
    if conn is None:
        return {"indexed": 0, "same": 0, "failed": 0, "skipped": 0}
    try:
        out = {"indexed": 0, "same": 0, "failed": 0, "skipped": 0}
        for row in conn.execute("SELECT state, count(*) AS n FROM files GROUP BY state"):
            out[row["state"] or "other"] = int(row["n"])
        return out
    except sqlite3.Error:
        return {"indexed": 0, "same": 0, "failed": 0, "skipped": 0}
    finally:
        conn.close()


def problems(limit: int = 50) -> List[dict]:
    """Files that could not be taken in, and why."""
    from . import manuals

    conn = manuals.connect(readonly=True)
    if conn is None:
        return []
    try:
        return [{"path": row["path"], "state": row["state"], "note": row["note"]} for row in conn.execute(
            "SELECT path, state, note FROM files WHERE state IN ('failed', 'skipped') ORDER BY path LIMIT ?", (limit,))]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
