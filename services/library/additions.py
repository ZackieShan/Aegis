"""Documents added to the library after it was built.

The collections in ``<library>/zim`` are packed once and read by Kiwix. This
is the part that grows: a PDF handed to the assistant ("add these to the
survival library") or fetched from a link is kept under ``<library>/added``,
read page by page into the same index the manuals use, and is searchable in
the next answer — no repacking, no restart, nothing that needs the internet
once the file is here.

``<library>/added/<section>/<file>.pdf``   the documents
``<library>/added/catalogue.json``         what each one is and where it came from

Scanned documents (pages that are pictures) are read with OCR in the
background; until that finishes they are in the library but their text
cannot be searched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urljoin, urlsplit

from . import config, manuals

logger = logging.getLogger(__name__)

BOOK = config.ADDED_BOOK
URL_PREFIX = config.ADDED_URL_PREFIX
MAX_BYTES = 300 * 1024 * 1024
DEFAULT_SECTION = "general"
_SCANNED_NOTE = "Its pages are scanned images; they are being read with OCR in the background."

_lock = threading.RLock()
_ocr_running: set = set()


class AdditionError(Exception):
    """Something about the document or the request that the person can fix."""


def folder() -> Path:
    return config.library_dir() / "added"


def _catalogue_path() -> Path:
    return folder() / "catalogue.json"


def _read_catalogue() -> List[dict]:
    try:
        data = json.loads(_catalogue_path().read_text(encoding="utf-8"))
        return [entry for entry in data if isinstance(entry, dict) and entry.get("path")]
    except (OSError, ValueError):
        return []


def _write_catalogue(entries: List[dict]) -> None:
    path = _catalogue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".catalogue-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def url_for(path: str, page: int = 0) -> str:
    return config.added_url(path, page)


def sections() -> List[str]:
    """Shelves a document can go on: those of the curated manuals, any already
    used for additions, and ``general``."""
    names = {DEFAULT_SECTION}
    for base in (config.library_dir() / "manuals", folder()):
        try:
            names.update(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            pass
    return sorted(names)


def _section(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")[:40]
    return slug or DEFAULT_SECTION


def _file_name(name: str, title: str) -> str:
    stem = unquote(str(name or "")).replace("\\", "/").rsplit("/", 1)[-1]
    stem = re.sub(r"\.pdf$", "", stem, flags=re.I)
    if len(re.sub(r"[^A-Za-z0-9]", "", stem)) < 3:
        stem = title
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:100]
    return (stem or "document") + ".pdf"


def _public(entry: dict) -> dict:
    out = dict(entry)
    out["url"] = url_for(entry["path"])
    return out


def list_added() -> List[dict]:
    """Every added document, newest first."""
    with _lock:
        entries = _read_catalogue()
    return [_public(entry) for entry in sorted(entries, key=lambda e: e.get("added_at") or 0, reverse=True)]


def file_for(relative: str) -> Optional[Path]:
    """The stored file for a catalogue path, or None — never anything outside the folder."""
    root = folder().resolve()
    try:
        path = (root / unquote(str(relative or ""))).resolve()
    except (OSError, ValueError):
        return None
    if root not in path.parents or path.suffix.lower() != ".pdf" or not path.is_file():
        return None
    return path


# ── adding ─────────────────────────────────────────────────────────────────

def _index(relative: str, title: str, pages: List[str], size: int, ocr_pages: int = 0) -> None:
    conn = manuals.connect()
    try:
        manuals._store(conn, BOOK, {"entry": relative, "title": title, "pages": pages, "ocr_pages": ocr_pages,
                                    "bytes": size, "error": ""})
        conn.commit()
    finally:
        conn.close()


def _text_pages(pages: List[str]) -> int:
    return sum(1 for page in pages if len(page.strip()) >= 24)


def add_pdf(data: bytes, *, name: str = "", title: str = "", section: str = "", source: str = "",
            added_by: str = "") -> dict:
    """Put one PDF in the library and make it searchable. Returns its
    catalogue entry with ``status`` ``added`` or ``already`` (the very same
    file was added before)."""
    if not data:
        raise AdditionError("That file is empty.")
    if len(data) > MAX_BYTES:
        raise AdditionError(f"That file is over {MAX_BYTES // (1024 * 1024)} MB, which is more than the library takes in one piece.")
    if b"%PDF-" not in data[:1024]:
        raise AdditionError("That is not a PDF. The library takes PDF documents.")
    digest = hashlib.sha256(data).hexdigest()
    with _lock:
        for entry in _read_catalogue():
            if entry.get("sha256") == digest:
                return {**_public(entry), "status": "already"}
    try:
        meta_title, pages, _ = manuals.extract_pdf(data, ocr=False)
    except ValueError as exc:
        if "encrypted" in str(exc):
            raise AdditionError("That PDF is password-protected, so it cannot be read.")
        raise AdditionError(f"That PDF could not be read: {exc}")
    except Exception as exc:
        raise AdditionError(f"That PDF could not be read ({type(exc).__name__}).")
    if not pages:
        raise AdditionError("That PDF has no pages.")

    shelf = _section(section)
    title = re.sub(r"\s+", " ", str(title or "")).strip()[:200]
    file_name = _file_name(name, title or meta_title)
    if not title:
        title = manuals.doc_title(meta_title, pages[0], file_name)
        # doc_title puts the file name in brackets after a title that lacks it.
        # That helps tell a collection's files apart; for one document handed
        # over by name ("scan0001.pdf") it is noise.
        suffix = f" ({manuals._pretty_filename(file_name)})"
        if title.endswith(suffix) and len(title) > len(suffix) + 3:
            title = title[: -len(suffix)]
    with _lock:
        target_dir = folder() / shelf
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / file_name
        counter = 2
        while target.exists():
            target = target_dir / f"{file_name[:-4]}-{counter}.pdf"
            counter += 1
        fd, tmp = tempfile.mkstemp(prefix=".incoming-", suffix=".tmp", dir=str(target_dir))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        relative = f"{shelf}/{target.name}"
        _index(relative, title, pages, len(data))
        readable = _text_pages(pages)
        scanned = readable * 2 < len(pages)
        entry = {
            "path": relative, "title": title, "section": shelf, "source": str(source or "")[:500],
            "sha256": digest, "bytes": len(data), "pages": len(pages), "text_pages": readable,
            "added_at": time.time(), "added_by": str(added_by or ""),
            "ocr": "pending" if scanned else "not needed",
        }
        _write_catalogue(_read_catalogue() + [entry])
    if scanned:
        _start_ocr(relative)
    return {**_public(entry), "status": "added", "note": _SCANNED_NOTE if scanned else ""}


def add_from_file(path: str, **details: Any) -> dict:
    source = Path(str(path or "")).expanduser()
    try:
        if not source.is_file():
            raise AdditionError(f"There is no file at {path}.")
        if source.stat().st_size > MAX_BYTES:
            raise AdditionError(f"{source.name} is over {MAX_BYTES // (1024 * 1024)} MB.")
        data = source.read_bytes()
    except OSError as exc:
        raise AdditionError(f"{source.name} could not be read: {exc.strerror or exc}")
    details.setdefault("name", source.name)
    details.setdefault("source", f"file: {source.name}")
    return add_pdf(data, **details)


def _download(url: str, max_bytes: int = MAX_BYTES, timeout: float = 60.0) -> Tuple[bytes, str, str]:
    """``(bytes, final_url, suggested_file_name)`` for a public link. The same
    guards as the assistant's web fetch — public addresses only, the address
    checked again on every redirect and the connection pinned to it — with a
    budget sized for manuals rather than web pages."""
    import httpx
    from services.search import content as web

    headers = {"User-Agent": web.WEB_FETCH_USER_AGENT, "Accept": "application/pdf,*/*;q=0.8",
               "Accept-Encoding": "identity"}
    current = url
    for _ in range(6):
        addresses = web._resolve_public_ips(current)
        with httpx.Client(headers=headers, timeout=httpx.Timeout(timeout, connect=15.0), follow_redirects=False,
                          transport=web._PinnedTransport(addresses[0])) as client:
            with client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise AdditionError("That link redirects nowhere.")
                    current = urljoin(str(response.url), location)
                    continue
                if response.status_code >= 400:
                    raise AdditionError(f"The site answered {response.status_code} for that link.")
                encoding = (response.headers.get("content-encoding") or "").strip().lower()
                if encoding and encoding != "identity":
                    raise AdditionError("The site sent a compressed download that cannot be size-checked.")
                declared = response.headers.get("content-length") or ""
                if declared.isdigit() and int(declared) > max_bytes:
                    raise AdditionError(f"That file is {int(declared) // (1024 * 1024)} MB — over the {max_bytes // (1024 * 1024)} MB the library takes.")
                chunks, read = [], 0
                for chunk in response.iter_bytes():
                    read += len(chunk)
                    if read > max_bytes:
                        raise AdditionError(f"That file is over the {max_bytes // (1024 * 1024)} MB the library takes.")
                    chunks.append(chunk)
                name = ""
                disposition = response.headers.get("content-disposition") or ""
                match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, flags=re.I)
                if match:
                    name = unquote(match.group(1)).strip()
                if not name:
                    name = unquote(urlsplit(current).path.rsplit("/", 1)[-1])
                return b"".join(chunks), current, name
    raise AdditionError("That link redirects too many times.")


def pdf_links(html: bytes, base_url: str, limit: int = 8) -> List[str]:
    """Links to PDF files found in a web page, in page order."""
    found: List[str] = []
    for raw in re.findall(rb"""href\s*=\s*["']([^"'#<>\s]+?\.pdf(?:\?[^"'<>\s]*)?)["']""", html or b"", flags=re.I):
        link = urljoin(base_url, raw.decode("utf-8", errors="ignore").replace("&amp;", "&"))
        if re.match(r"^https?://", link, flags=re.I) and link not in found:
            found.append(link)
            if len(found) >= limit:
                break
    return found


def _page_as_pdf(title: str, text: str, source: str) -> bytes:
    """A plain, paginated PDF of a web page's readable text."""
    import html as html_lib
    import io

    import fitz  # PyMuPDF

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n|\n", text) if part.strip()]
    body = "".join(f"<p>{html_lib.escape(part)}</p>" for part in paragraphs)
    story = fitz.Story(html=f"<h1>{html_lib.escape(title or 'Web page')}</h1>"
                            f"<p><i>Saved from {html_lib.escape(source)}</i></p>{body}",
                       user_css="body { font-family: sans-serif; font-size: 10.5pt; line-height: 1.35; } "
                                "h1 { font-size: 16pt; } p { margin: 0 0 7pt 0; }")
    buffer = io.BytesIO()
    writer = fitz.DocumentWriter(buffer)
    sheet = fitz.paper_rect("letter")
    area = sheet + (54, 54, -54, -54)
    more = True
    while more:
        device = writer.begin_page(sheet)
        more, _ = story.place(area)
        story.draw(device)
        writer.end_page()
    writer.close()
    return buffer.getvalue()


def add_from_url(url: str, as_page: bool = False, **details: Any) -> dict:
    """Add what a link points at. A PDF is kept as it is. A web page is
    refused — naming the PDFs it links to — unless ``as_page`` asks for the
    page's own text to be kept as a document."""
    url = str(url or "").strip()
    if not re.match(r"^https?://", url, flags=re.I):
        raise AdditionError("Give the full link, starting with http:// or https://.")
    try:
        data, final_url, name = _download(url)
    except AdditionError:
        raise
    except Exception as exc:
        raise AdditionError(f"That link could not be downloaded: {str(exc) or type(exc).__name__}")
    if b"%PDF-" in data[:1024]:
        details.setdefault("name", name)
        details.setdefault("source", final_url)
        return add_pdf(data, **details)
    if not as_page:
        links = pdf_links(data, final_url)
        found = (" PDF files it links to: " + " , ".join(links) + " ." if links
                 else " It links to no PDF files.")
        raise AdditionError("That link is a web page, not a PDF file." + found +
                            ' Add one of those, or keep this page itself by adding it with "as_page": true.')
    from .text import html_to_text
    try:
        title, text = html_to_text(data.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise AdditionError(f"That page could not be read ({type(exc).__name__}).")
    if len(text or "") < 400:
        raise AdditionError("That page has too little readable text to keep (it may need a browser to show its content).")
    title = re.sub(r"\s+", " ", title or "").strip() or urlsplit(final_url).netloc
    try:
        pdf = _page_as_pdf(title, text, final_url)
    except Exception as exc:
        raise AdditionError(f"That page could not be turned into a document ({type(exc).__name__}: {exc}).")
    if not str(details.get("title") or "").strip():
        details["title"] = title[:200]
    details.setdefault("name", re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:80] + ".pdf")
    details.setdefault("source", final_url)
    return add_pdf(pdf, **details)


# ── scanned documents ──────────────────────────────────────────────────────

def _update_entry(relative: str, **changes: Any) -> None:
    with _lock:
        entries = _read_catalogue()
        for entry in entries:
            if entry.get("path") == relative:
                entry.update(changes)
        _write_catalogue(entries)


def _ocr(relative: str) -> None:
    try:
        path = file_for(relative)
        if path is None:
            return
        data = path.read_bytes()
        _update_entry(relative, ocr="running")
        meta_title, pages, ocr_pages = manuals.extract_pdf(data, ocr=True)
        with _lock:
            entry = next((e for e in _read_catalogue() if e.get("path") == relative), None)
            if entry is None:
                return                                        # removed while it was being read
            _index(relative, entry.get("title") or manuals.doc_title(meta_title, pages[0] if pages else "", relative),
                   pages, len(data), ocr_pages)
            _update_entry(relative, ocr="done" if ocr_pages else "nothing readable",
                          text_pages=_text_pages(pages), ocr_pages=ocr_pages)
        logger.info("library: OCR finished for %s (%d pages read)", relative, ocr_pages)
    except Exception:
        logger.exception("library: OCR failed for %s", relative)
        try:
            _update_entry(relative, ocr="failed")
        except Exception:
            pass
    finally:
        _ocr_running.discard(relative)


def ocr_available() -> bool:
    return any(os.path.isdir(d) for d in manuals._TESSDATA_DIRS)


def _start_ocr(relative: str) -> bool:
    if not ocr_available():
        _update_entry(relative, ocr="unavailable")
        return False
    with _lock:
        if relative in _ocr_running:
            return False
        _ocr_running.add(relative)
    threading.Thread(target=_ocr, args=(relative,), name="library-ocr", daemon=True).start()
    return True


def resume_ocr() -> int:
    """Pick up scanned documents whose reading was cut short (a restart)."""
    started = 0
    for entry in list_added():
        if entry.get("ocr") in ("pending", "running") and entry["path"] not in _ocr_running:
            started += 1 if _start_ocr(entry["path"]) else 0
    return started


# ── removing ───────────────────────────────────────────────────────────────

def find(reference: str) -> Optional[dict]:
    """An added document by its path, link, file name or (part of its) title."""
    wanted = unquote(str(reference or "")).strip()
    if not wanted:
        return None
    if URL_PREFIX in wanted:
        wanted = wanted.split(URL_PREFIX, 1)[1].split("#", 1)[0]
    lowered = wanted.lower()
    entries = list_added()
    for test in (lambda e: e["path"] == wanted,
                 lambda e: e["path"].lower().rsplit("/", 1)[-1] == lowered,
                 lambda e: str(e.get("title") or "").lower() == lowered):
        for entry in entries:
            if test(entry):
                return entry
    partial = [e for e in entries if lowered in str(e.get("title") or "").lower() or lowered in e["path"].lower()]
    return partial[0] if len(partial) == 1 else None


def remove(reference: str) -> dict:
    entry = find(reference)
    if entry is None:
        raise AdditionError(f"No added document matches “{reference}”. List them to see their exact titles.")
    relative = entry["path"]
    with _lock:
        path = file_for(relative)
        if path is not None:
            path.unlink()
        conn = manuals.connect()
        try:
            row = conn.execute("SELECT id FROM docs WHERE book = ? AND path = ?", (BOOK, relative)).fetchone()
            if row:
                conn.execute("DELETE FROM pages WHERE rowid >= ? AND rowid < ?",
                             (row["id"] * manuals._PAGE_FACTOR, (row["id"] + 1) * manuals._PAGE_FACTOR))
                conn.execute("DELETE FROM docs WHERE id = ?", (row["id"],))
            conn.commit()
        finally:
            conn.close()
        _write_catalogue([e for e in _read_catalogue() if e.get("path") != relative])
    return {"removed": entry.get("title") or relative, "path": relative}
