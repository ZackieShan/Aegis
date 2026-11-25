"""What is on which shelf.

:mod:`.shelves` decides where something belongs; this module remembers it and
answers the questions asked of a library: what is on this shelf, which shelf is
this book on, do we have something by this author.

A whole collection needs no record — its shelf follows from its id. Records
are kept for what is filed one by one (table ``shelf_items`` in the index
database next to the page index):

* every book inside a Project Gutenberg collection, from Gutenberg's own
  catalogue (``<library>/index/pg_catalog.csv.gz``, fetched once);
* every PDF of the curated manuals and of the Army publications;
* every document added later and every file dropped into the library folder.

``build()`` is the batch job (``aegis-library shelve``): cheap, repeatable,
and it never overwrites what a person or a model decided with a rule.
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import config, manuals, shelves

logger = logging.getLogger(__name__)

RULES_VERSION = 5            # raise when the rules in shelves.py change what a book's shelf is

SCHEMA = """
CREATE TABLE IF NOT EXISTS shelf_items (
    book      TEXT NOT NULL,
    key       TEXT NOT NULL,
    title     TEXT NOT NULL,
    author    TEXT NOT NULL DEFAULT '',
    dates     TEXT NOT NULL DEFAULT '',
    year      INTEGER,
    shelf     TEXT NOT NULL,
    form      TEXT NOT NULL DEFAULT '',
    note      TEXT NOT NULL DEFAULT '',
    source    TEXT NOT NULL DEFAULT 'rule',
    filed_at  REAL,
    PRIMARY KEY (book, key)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS shelf_items_by_shelf ON shelf_items (shelf, title COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS shelf_sources (
    book      TEXT PRIMARY KEY,
    stamp     TEXT NOT NULL,
    items     INTEGER NOT NULL DEFAULT 0,
    built_at  REAL
);
"""

# Added after the first release of the shelves: how well read a book is, and
# whether it passes the owner's cut (see ``keeping``).
_LATER_COLUMNS = (("rating", "REAL"), ("kept", "INTEGER NOT NULL DEFAULT 1"), ("classic", "INTEGER NOT NULL DEFAULT 0"))


def migrate(conn: sqlite3.Connection) -> None:
    """Bring an index made by an earlier version up to date (called on every
    writing connection; a no-op once done)."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(shelf_items)")}
    for name, kind in _LATER_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE shelf_items ADD COLUMN {name} {kind}")
    conn.execute("CREATE INDEX IF NOT EXISTS shelf_items_kept ON shelf_items (kept, shelf, title COLLATE NOCASE)")


# Who decided: a later, weaker source never replaces an earlier, stronger one.
_RANK = {"person": 5, "model": 4, "catalogue": 3, "folder": 2, "rule": 1, "": 0}

PG_CATALOGUE_NAME = "pg_catalog.csv.gz"
PG_CATALOGUE_URLS = (
    "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz",
    "https://gutenberg.pglaf.org/cache/epub/feeds/pg_catalog.csv.gz",
)

_GUTENBERG_CARD = re.compile(r"^index/book_(\d+)$")
_GUTENBERG_TEXT = re.compile(r"\.(\d+)(?:\.epub|\.html?|\.pdf)?$")


# ── reading ────────────────────────────────────────────────────────────────

def item_key(book: str, path: str) -> str:
    """The catalogue key for something inside a collection: a Project
    Gutenberg book's number (from its card or its text), else the path."""
    path = str(path or "")
    if shelves.is_gutenberg(book):
        match = _GUTENBERG_CARD.match(path) or _GUTENBERG_TEXT.search(path)
        return match.group(1) if match else ""
    return path


def _row(row: sqlite3.Row) -> dict:
    keys = row.keys()
    return {"book": row["book"], "key": row["key"], "title": row["title"], "author": row["author"], "dates": row["dates"],
            "year": row["year"], "shelf": row["shelf"], "form": row["form"], "note": row["note"], "source": row["source"],
            "rating": row["rating"] if "rating" in keys else None, "kept": bool(row["kept"]) if "kept" in keys else True}


# ── the owner's cut ────────────────────────────────────────────────────────

EXEMPT_CATEGORIES = ("Survival", "Cooking", "Homestead", "Repair & making")
KEEP_PER_SHELF = 25


def keeping() -> dict:
    """The owner's cut for books that come by the tens of thousands (the
    Project Gutenberg collections), from ``shelves.json``:

    ``min_rating``      keep books at least this well read (Project Gutenberg's
                        popularity figure); 0 or absent keeps everything
    ``keep_per_shelf``  …and always the best-read N of every shelf (default 25)
    ``keep_whole``      categories the cut never touches (default: the practical
                        ones — Survival, Cooking, Homestead, Repair & making)

    A book tagged a classic by Project Gutenberg, anything a person or a model
    filed, and everything that is not a Gutenberg book is always kept. Nothing
    is deleted: a book below the cut is only left off the shelves and out of
    searches (its collection is one archive; a book cannot be taken out of it)."""
    own = shelves.custom()
    try:
        cut = float(own.get("min_rating") or 0)
    except (TypeError, ValueError):
        cut = 0.0
    try:
        per_shelf = max(0, int(own.get("keep_per_shelf", KEEP_PER_SHELF)))
    except (TypeError, ValueError):
        per_shelf = KEEP_PER_SHELF
    whole = own.get("keep_whole")
    whole = [str(name) for name in whole if str(name) in shelves.categories()] if isinstance(whole, list) else list(EXEMPT_CATEGORIES)
    return {"min_rating": max(0.0, cut), "keep_per_shelf": per_shelf, "keep_whole": whole}


def apply_cut(conn: sqlite3.Connection) -> dict:
    """Mark every catalogued item kept or not by the owner's cut. Cheap (one
    pass over the table), so it runs at the end of every build."""
    rule = keeping()
    if rule["min_rating"] <= 0:
        conn.execute("UPDATE shelf_items SET kept = 1 WHERE kept != 1")
        conn.commit()
        return {"min_rating": 0, "kept": conn.execute("SELECT count(*) FROM shelf_items").fetchone()[0], "hidden": 0}
    whole_sql, whole_values = "0", []
    if rule["keep_whole"]:
        whole_sql = " OR ".join("shelf = ? OR shelf LIKE ?" for _ in rule["keep_whole"])
        for name in rule["keep_whole"]:
            whole_values += [name, _like(name) + shelves.SEP + "%"]
    conn.execute(
        "UPDATE shelf_items SET kept = CASE "
        "WHEN book NOT LIKE 'gutenberg%' THEN 1 "          # only the mass of Gutenberg books is cut
        "WHEN source IN ('person', 'model') THEN 1 "
        "WHEN classic = 1 THEN 1 "
        "WHEN rating IS NULL THEN 1 "                      # no figure for it: not a reason to hide it
        "WHEN rating >= ? THEN 1 "
        f"WHEN ({whole_sql}) THEN 1 ELSE 0 END",
        [rule["min_rating"]] + whole_values)
    if rule["keep_per_shelf"]:
        conn.execute(
            "UPDATE shelf_items SET kept = 1 WHERE kept = 0 AND (book, key) IN ("
            "SELECT book, key FROM (SELECT book, key, ROW_NUMBER() OVER (PARTITION BY shelf ORDER BY rating DESC, key) AS place "
            "FROM shelf_items WHERE book LIKE 'gutenberg%') WHERE place <= ?)", (rule["keep_per_shelf"],))
    conn.commit()
    kept = conn.execute("SELECT count(*) FROM shelf_items WHERE kept = 1").fetchone()[0]
    hidden = conn.execute("SELECT count(*) FROM shelf_items WHERE kept = 0").fetchone()[0]
    return {"min_rating": rule["min_rating"], "kept": kept, "hidden": hidden}


def places(pairs: Iterable[Tuple[str, str]]) -> Dict[Tuple[str, str], dict]:
    """Catalogue records for ``(collection, key)`` pairs — those that have one."""
    wanted = [(str(book), str(key)) for book, key in pairs if book and key]
    if not wanted:
        return {}
    conn = manuals.connect(readonly=True)
    if conn is None:
        return {}
    found: Dict[Tuple[str, str], dict] = {}
    try:
        for book, key in dict.fromkeys(wanted):
            row = conn.execute("SELECT * FROM shelf_items WHERE book = ? AND key = ?", (book, key)).fetchone()
            if row is not None:
                found[(book, key)] = _row(row)
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return found


def place_for(book: str, path: str, known: Optional[Dict[Tuple[str, str], dict]] = None) -> dict:
    """Where one thing is filed: ``{"shelf", "kind", "title", "author", "dates",
    "form"}`` — its own record when it has one, else its collection's shelf."""
    key = item_key(book, path)
    record = None
    if key:
        record = (known or {}).get((book, key)) if known is not None else places([(book, key)]).get((book, key))
    shelf, kind = shelves.collection_place(book)
    if record:
        return {"shelf": record["shelf"], "kind": "book" if shelves.is_gutenberg(book) else "document",
                "title": record["title"], "author": record["author"], "dates": record["dates"], "form": record["form"],
                "note": record["note"], "kept": record.get("kept", True)}
    return {"shelf": shelf, "kind": kind, "title": "", "author": "", "dates": "", "form": "", "note": "", "kept": True}


def link(book: str, key: str, page: int = 0) -> str:
    """The library link that opens a catalogued item."""
    from . import kiwix

    if shelves.is_gutenberg(book):
        return kiwix.public_url(f"/content/{book}/index/book_{key}")
    return manuals.pdf_url(book, key, page)


def _has_cut(conn: sqlite3.Connection) -> bool:
    """Does this index have the rating and kept columns? One made by the first
    release of the shelves does not until something writes to it."""
    try:
        conn.execute("SELECT kept, rating FROM shelf_items LIMIT 1")
    except sqlite3.Error:
        return False
    return True


def _kept_sql(conn: sqlite3.Connection, everything: bool) -> str:
    """`` AND kept = 1`` unless everything is wanted (or the index predates the cut)."""
    return "" if everything or not _has_cut(conn) else " AND kept = 1"


def counts(everything: bool = False) -> Dict[str, int]:
    """How many catalogued items each shelf holds — those that pass the
    owner's cut, or with ``everything`` all of them."""
    conn = manuals.connect(readonly=True)
    if conn is None:
        return {}
    try:
        kept = _kept_sql(conn, everything)
        return {row["shelf"]: int(row["n"]) for row in conn.execute(
            f"SELECT shelf, count(*) AS n FROM shelf_items WHERE 1 = 1{kept} GROUP BY shelf")}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def hidden_under(scope: str = "") -> int:
    """How many items under ``scope`` are below the owner's cut."""
    conn = manuals.connect(readonly=True)
    if conn is None:
        return 0
    try:
        where, values = "kept = 0", []
        if scope:
            where += " AND (shelf = ? OR shelf LIKE ? ESCAPE '\\')"
            values = [scope, _like(scope) + shelves.SEP + "%"]
        return int(conn.execute(f"SELECT count(*) FROM shelf_items WHERE {where}", values).fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def books_with(scope: str) -> List[str]:
    """Collections that hold at least one catalogued item under ``scope``."""
    conn = manuals.connect(readonly=True)
    if conn is None:
        return []
    try:
        kept = _kept_sql(conn, False)
        rows = conn.execute(f"SELECT DISTINCT book FROM shelf_items WHERE (shelf = ? OR shelf LIKE ? ESCAPE '\\'){kept}",
                            (scope, _like(scope) + shelves.SEP + "%")).fetchall()
        return [row["book"] for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _like(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _words(query: str, limit: int = 6) -> List[str]:
    return [word for word in re.findall(r"[\w'’-]+", query or "") if len(word) > 1][:limit]


def items(scope: str = "", query: str = "", offset: int = 0, limit: int = 60, everything: bool = False) -> dict:
    """Catalogued items on a shelf (or anywhere under a category), by title:
    ``{"total", "items": [...]}``. ``query`` keeps those whose title, author
    or form has every word. Books below the owner's cut are left out unless
    ``everything`` is asked for."""
    conn = manuals.connect(readonly=True)
    if conn is None:
        return {"total": 0, "items": []}
    try:
        where, values = "1 = 1" + _kept_sql(conn, everything), []
        if scope:
            where += " AND (shelf = ? OR shelf LIKE ? ESCAPE '\\')"
            values = [scope, _like(scope) + shelves.SEP + "%"]
        for word in _words(query):
            where += " AND (title LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\' OR form LIKE ? ESCAPE '\\')"
            values += [f"%{_like(word)}%"] * 3
        total = conn.execute(f"SELECT count(*) FROM shelf_items WHERE {where}", values).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM shelf_items WHERE {where} ORDER BY title COLLATE NOCASE, key LIMIT ? OFFSET ?",
            values + [max(1, min(int(limit), 200)), max(0, int(offset))]).fetchall()
        return {"total": int(total), "items": [_row(row) for row in rows]}
    except sqlite3.Error:
        return {"total": 0, "items": []}
    finally:
        conn.close()


def find(query: str, limit: int = 20, scope: str = "", everything: bool = False) -> List[dict]:
    """Books and documents by title or author ("moby dick", "dickens"), best
    first: a title that starts with the words, then the better read, then
    shorter titles. Books below the owner's cut are left out unless
    ``everything`` is asked for."""
    words = _words(query)
    if not words:
        return []
    conn = manuals.connect(readonly=True)
    if conn is None:
        return []
    try:
        where, values = "1 = 1" + _kept_sql(conn, everything), []
        if scope:
            where += " AND (shelf = ? OR shelf LIKE ? ESCAPE '\\')"
            values = [scope, _like(scope) + shelves.SEP + "%"]
        for word in words:
            where += " AND (title LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\')"
            values += [f"%{_like(word)}%"] * 2
        phrase = _like(" ".join(words))
        by_rating = "COALESCE(rating, 0) DESC, " if _has_cut(conn) else ""
        rows = conn.execute(
            f"SELECT * FROM shelf_items WHERE {where} ORDER BY "
            f"(title LIKE ? ESCAPE '\\') DESC, (title LIKE ? ESCAPE '\\') DESC, {by_rating}length(title), "
            "title COLLATE NOCASE LIMIT ?",
            values + [phrase + "%", "%" + phrase + "%", max(1, min(int(limit), 100)) * 3]).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out, seen = [], set()
    for row in rows:                                  # the same book in two collections, or two editions, once each
        same = (re.sub(r"[^a-z0-9]+", "", row["title"].lower())[:60], row["author"].lower())
        if same in seen:
            continue
        seen.add(same)
        out.append(_row(row))
        if len(out) >= limit:
            break
    return out


def itemised() -> set:
    """Collections whose contents are filed book by book (the Project
    Gutenberg ones, once catalogued): on a shelf their books are shown, not
    the collection as one more row."""
    conn = manuals.connect(readonly=True)
    if conn is None:
        return set()
    try:
        return {row["book"] for row in conn.execute("SELECT book FROM shelf_sources WHERE items > 0")}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def whole_collections(books: Sequence[dict], place: str) -> List[dict]:
    """The collections filed as a whole exactly at ``place``."""
    apart = itemised()
    return [book for book in books if book["id"] not in apart and shelves.collection_shelf(book["id"]) == place]


def overview(books: Sequence[dict]) -> List[dict]:
    """The tree with what is on it: for every shelf the collections filed
    there and the number of catalogued items; for every category also what is
    filed in the category itself. ``books`` is ``kiwix.list_books()``."""
    held = counts()
    everything = counts(everything=True)
    apart = itemised()
    by_shelf: Dict[str, List[dict]] = {}
    for book in books:
        if book["id"] in apart:
            continue
        shelf = shelves.collection_shelf(book["id"])
        by_shelf.setdefault(shelf, []).append(book)
    out = []
    for category in shelves.tree():
        entry = {"name": category["name"], "about": category["about"], "custom": category.get("custom", False), "shelves": [],
                 "collections": [book["id"] for book in by_shelf.get(category["name"], [])],
                 "own_items": held.get(category["name"], 0)}
        total = entry["own_items"]
        hidden = everything.get(category["name"], 0) - entry["own_items"]
        for shelf in category["shelves"]:
            here = by_shelf.get(shelf["path"], [])
            count = held.get(shelf["path"], 0)
            below = everything.get(shelf["path"], 0) - count
            total += count
            hidden += below
            entry["shelves"].append({"name": shelf["name"], "path": shelf["path"], "about": shelf["about"],
                                     "collections": [book["id"] for book in here], "items": count, "hidden": below})
        entry["items"] = total
        entry["hidden"] = hidden
        entry["collection_count"] = len(entry["collections"]) + sum(len(shelf["collections"]) for shelf in entry["shelves"])
        out.append(entry)
    loose = by_shelf.get(shelves.UNSORTED, [])
    if loose or held.get(shelves.UNSORTED):
        out.append({"name": shelves.UNSORTED, "about": "Not filed yet", "custom": False, "shelves": [],
                    "collections": [book["id"] for book in loose], "own_items": held.get(shelves.UNSORTED, 0),
                    "items": held.get(shelves.UNSORTED, 0), "hidden": 0, "collection_count": len(loose)})
    return out


# ── writing ────────────────────────────────────────────────────────────────

def put(conn: sqlite3.Connection, book: str, key: str, title: str, shelf: str, *, author: str = "", dates: str = "",
        year: Optional[int] = None, form: str = "", note: str = "", source: str = "rule",
        rating: Optional[float] = None, classic: bool = False) -> bool:
    """File one item. A record made by a stronger hand (a person, a model)
    keeps its shelf and its details; only weaker or equal ones are replaced —
    though how well read a book is (``rating``) is a fact and is always kept
    up to date."""
    existing = conn.execute("SELECT source FROM shelf_items WHERE book = ? AND key = ?", (book, key)).fetchone()
    if existing is not None and _RANK.get(existing["source"], 0) > _RANK.get(source, 0):
        if rating is not None:
            conn.execute("UPDATE shelf_items SET rating = ?, classic = ? WHERE book = ? AND key = ?",
                         (rating, 1 if classic else 0, book, key))
        return False
    conn.execute(
        "INSERT INTO shelf_items (book, key, title, author, dates, year, shelf, form, note, source, filed_at, rating, classic) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(book, key) DO UPDATE SET title=excluded.title, author=excluded.author, "
        "dates=excluded.dates, year=excluded.year, shelf=excluded.shelf, form=excluded.form, note=excluded.note, "
        "source=excluded.source, filed_at=excluded.filed_at, rating=excluded.rating, classic=excluded.classic",
        (book, key, re.sub(r"\s+", " ", title or "").strip()[:300] or key, (author or "")[:160], (dates or "")[:40], year,
         shelf, (form or "")[:120], (note or "")[:600], source, time.time(), rating, 1 if classic else 0))
    return True


def forget(conn: sqlite3.Connection, book: str, key: Optional[str] = None) -> None:
    if key is None:
        conn.execute("DELETE FROM shelf_items WHERE book = ?", (book,))
        conn.execute("DELETE FROM shelf_sources WHERE book = ?", (book,))
    else:
        conn.execute("DELETE FROM shelf_items WHERE book = ? AND key = ?", (book, key))


def refile(book: str, key: str, shelf: str) -> dict:
    """Move one item to another shelf by hand. Remembered in
    ``shelves.json`` as well, so a rebuild of the catalogue keeps it."""
    target = shelves.resolve(shelf)
    if not target or not shelves.valid(target):
        raise ValueError(f"There is no shelf called “{shelf}”.")
    conn = manuals.connect()
    try:
        row = conn.execute("SELECT * FROM shelf_items WHERE book = ? AND key = ?", (book, key)).fetchone()
        if row is None:
            raise ValueError("That is not something the catalogue files one by one.")
        conn.execute("UPDATE shelf_items SET shelf = ?, source = 'person', kept = 1, filed_at = ? WHERE book = ? AND key = ?",
                     (target, time.time(), book, key))
        conn.commit()
        title = row["title"]
    finally:
        conn.close()
    data = dict(shelves.custom())
    filed = dict(data.get("items") or {})
    filed[f"{book}|{key}"] = target
    data["items"] = filed
    shelves.save_custom(data)
    return {"title": title, "shelf": target}


def lookup_title(reference: str, limit: int = 5) -> List[dict]:
    """Items a person might mean by a title, a path or a link."""
    reference = str(reference or "").strip()
    if not reference:
        return []
    conn = manuals.connect(readonly=True)
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT * FROM shelf_items WHERE key = ? OR title = ? COLLATE NOCASE LIMIT ?",
                            (reference, reference, limit)).fetchall()
        if rows:
            return [_row(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return find(reference, limit=limit)


# ── Project Gutenberg's catalogue ──────────────────────────────────────────

def pg_catalogue_path() -> Path:
    return config.library_dir() / "index" / PG_CATALOGUE_NAME


def fetch_pg_catalogue(progress: Optional[Callable[[str], None]] = None) -> Path:
    """Download Project Gutenberg's catalogue (about 6 MB; the only step here
    that needs the internet). Tries the main site, then its official mirror."""
    import httpx

    say = progress or (lambda message: None)
    target = pg_catalogue_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    last = ""
    for url in PG_CATALOGUE_URLS:
        try:
            with httpx.Client(timeout=httpx.Timeout(180.0, connect=15.0), follow_redirects=True,
                              headers={"User-Agent": "aegis-library"}) as client:
                response = client.get(url)
            if response.status_code != 200 or response.content[:2] != b"\x1f\x8b":
                last = f"{url}: HTTP {response.status_code}"
                continue
            gzip.decompress(response.content)[:64]            # a real archive, not an error page
            partial = target.with_suffix(".part")
            partial.write_bytes(response.content)
            partial.replace(target)
            say(f"Gutenberg catalogue: {len(response.content) // 1024} KB from {url.split('/')[2]}")
            return target
        except Exception as exc:
            last = f"{url}: {type(exc).__name__}: {exc}"
    raise RuntimeError(f"could not download Project Gutenberg's catalogue ({last})")


def read_pg_catalogue(path: Optional[Path] = None) -> Dict[str, dict]:
    """``{book number: record}`` from the catalogue file; {} when it is not there."""
    path = path or pg_catalogue_path()
    if not path.exists():
        return {}
    csv.field_size_limit(10 ** 8)
    records: Dict[str, dict] = {}
    try:
        with gzip.open(path, "rb") as raw:
            for row in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline="")):
                number = str(row.get("Text#") or "").strip()
                if number.isdigit():
                    records[number] = {"title": row.get("Title") or "", "authors": row.get("Authors") or "",
                                       "subjects": row.get("Subjects") or "", "locc": row.get("LoCC") or "",
                                       "bookshelves": row.get("Bookshelves") or ""}
    except (OSError, EOFError, csv.Error, UnicodeDecodeError) as exc:
        logger.warning("Gutenberg catalogue unreadable: %s", exc)
        return {}
    return records


def gutenberg_books(zim: Path) -> Dict[str, str]:
    """``{book number: title}`` for the books a Project Gutenberg collection
    holds, read from its list of entries (one ``zimdump`` call)."""
    listing = manuals._run(["zimdump", "list", "--", str(zim)], timeout=3600).decode("utf-8", "replace")
    numbers, titles = set(), {}
    for line in listing.splitlines():
        card = re.match(r"^books/(\d+)\.json$", line)
        if card:
            numbers.add(card.group(1))
            continue
        text = re.match(r"^(?!\d+/)(.+)\.(\d+)$", line)
        if text and "/" not in line:
            titles[text.group(2)] = text.group(1)
    return {number: titles.get(number, "") for number in numbers}


def gutenberg_ratings(zim: Path) -> Dict[str, float]:
    """``{book number: popularity}`` from a Project Gutenberg collection's own
    list of its books by popularity (a few pages of 500). {} for a collection
    that has no such list — its books then simply have no rating."""
    import json

    def show(entry: str) -> bytes:
        try:
            return manuals._run(["zimdump", "show", f"--url={entry}", "--", str(zim)], timeout=180)
        except Exception:
            return b""

    try:
        pages = int(json.loads(show("books.json") or b"{}").get("totalPages") or 0)
    except (ValueError, TypeError, AttributeError):
        pages = 0
    ratings: Dict[str, float] = {}
    for page in range(0, min(pages, 400) + 2):                # the pages are numbered from 0 in some editions, from 1 in others
        raw = show(f"books-pages/popularity/{page}.json")
        if not raw:
            continue
        try:
            listed = json.loads(raw).get("books") or []
        except (ValueError, AttributeError):
            continue
        for entry in listed:
            number = re.search(r"covers/(\d+)_", str(entry.get("coverPath") or ""))
            try:
                value = float(entry.get("popularity"))
            except (TypeError, ValueError):
                continue
            if number:
                ratings[number.group(1)] = max(value, ratings.get(number.group(1), 0.0))
    return ratings


def build_gutenberg(conn: sqlite3.Connection, zim: Path, records: Dict[str, dict], stamp_extra: str = "",
                    force: bool = False, progress: Optional[Callable[[str], None]] = None) -> dict:
    say = progress or (lambda message: None)
    book = manuals.book_id_for(zim)
    stat = zim.stat()
    stamp = f"{zim.name}:{stat.st_size}:{int(stat.st_mtime)}:r{RULES_VERSION}:{stamp_extra}"
    known = conn.execute("SELECT stamp, items FROM shelf_sources WHERE book = ?", (book,)).fetchone()
    if known is not None and known["stamp"] == stamp and not force:
        return {"book": book, "items": known["items"], "skipped": True}
    held = gutenberg_books(zim)
    ratings = gutenberg_ratings(zim)
    default_shelf = shelves.collection_shelf(book)
    conn.execute("DELETE FROM shelf_items WHERE book = ? AND source IN ('rule', 'catalogue', 'folder', '')", (book,))
    filed = 0
    for number, zim_title in held.items():
        record = records.get(number)
        classic = False
        if record:
            placed = shelves.gutenberg_place(record)
            title = (record["title"].splitlines() or [""])[0].strip() or zim_title
            source = "catalogue"
            classic = "category: classics of literature" in record["bookshelves"].lower()
        else:
            placed = {"shelf": default_shelf, "form": "", "author": "", "dates": "", "year": None}
            title, source = zim_title or f"Book {number}", "rule"
        shelf = placed["shelf"]
        if shelf == shelves.UNSORTED and default_shelf != shelves.UNSORTED:
            shelf = default_shelf
        filed += 1 if put(conn, book, number, title, shelf, author=placed["author"], dates=placed["dates"],
                          year=placed["year"], form=placed["form"], source=source, rating=ratings.get(number),
                          classic=classic) else 0
    conn.execute("INSERT INTO shelf_sources (book, stamp, items, built_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(book) DO UPDATE SET stamp=excluded.stamp, items=excluded.items, built_at=excluded.built_at",
                 (book, stamp, len(held), time.time()))
    conn.commit()
    say(f"{book}: {len(held)} books filed" + ("" if records else " (no Gutenberg catalogue: by collection only)")
        + ("" if ratings else " (no popularity list in this collection: unrated)"))
    return {"book": book, "items": len(held), "skipped": False}


def build_documents(conn: sqlite3.Connection, progress: Optional[Callable[[str], None]] = None) -> dict:
    """File the PDFs that are filed one by one: the curated manuals (by their
    section), the Army publications (by what they are), documents added later
    (by the section they were given)."""
    say = progress or (lambda message: None)
    done = {"manuals": 0, "army": 0, "added": 0}
    for row in conn.execute("SELECT book, path, title FROM docs WHERE book LIKE 'aegis-manuals%'").fetchall():
        section = row["path"].split("/", 1)[0] if "/" in row["path"] else ""
        shelf = shelves.section_shelf(section) or shelves.place_by_words(row["title"]) or "Survival/Shelter, fire & fieldcraft"
        if section in ("food-water-homestead", "navigation-fieldcraft", "survival"):
            # Sections that hold two things: let the title pick between them.
            better = shelves.place_by_words(row["title"])
            if better and (better.startswith("Survival/") or better.startswith("Cooking/")):
                shelf = better
        done["manuals"] += 1 if put(conn, row["book"], row["path"], row["title"], shelf, form="manual", source="folder") else 0
    for row in conn.execute("SELECT book, path, title FROM docs WHERE book LIKE 'armypubs%'").fetchall():
        done["army"] += 1 if put(conn, row["book"], row["path"], row["title"], shelves.army_place(row["title"]),
                                 form="Army publication", source="rule") else 0
    sections = {}
    try:
        from . import additions
        sections = {entry["path"]: entry.get("section") or "" for entry in additions.list_added()}
    except Exception:
        pass
    for row in conn.execute("SELECT book, path, title FROM docs WHERE book = ?", (config.ADDED_BOOK,)).fetchall():
        section = sections.get(row["path"]) or row["path"].split("/", 1)[0]
        shelf = shelves.section_shelf(section) or shelves.place_by_words(row["title"]) or shelves.UNSORTED
        done["added"] += 1 if put(conn, row["book"], row["path"], row["title"], shelf, form="document", source="folder") else 0
    # Whatever is filed for a document that no longer exists goes with it.
    conn.execute("DELETE FROM shelf_items WHERE (book LIKE 'aegis-manuals%' OR book LIKE 'armypubs%' OR book = ?) "
                 "AND NOT EXISTS (SELECT 1 FROM docs WHERE docs.book = shelf_items.book AND docs.path = shelf_items.key)",
                 (config.ADDED_BOOK,))
    conn.commit()
    say(f"documents filed: {done['manuals']} manuals, {done['army']} Army publications, {done['added']} added")
    return done


def apply_filed_by_hand(conn: sqlite3.Connection) -> int:
    """Re-apply what the owner filed by hand (``shelves.json`` → ``items``)."""
    moved = 0
    filed = shelves.custom().get("items") or {}
    for reference, shelf in (filed.items() if isinstance(filed, dict) else []):
        book, _, key = str(reference).partition("|")
        if not key or not shelves.valid(str(shelf)):
            continue
        cursor = conn.execute("UPDATE shelf_items SET shelf = ?, source = 'person' WHERE book = ? AND key = ?", (str(shelf), book, key))
        moved += cursor.rowcount
    conn.commit()
    return moved


def build(force: bool = False, only: Optional[Iterable[str]] = None,
          progress: Optional[Callable[[str], None]] = None) -> dict:
    """File everything that is filed one by one. Safe to run again at any
    time: a collection whose file and rules are unchanged costs one stat call."""
    say = progress or (lambda message: None)
    zim_dir = config.zim_dir()
    newest: Dict[str, Path] = {}
    for zim in sorted(zim_dir.glob("*.zim")) if zim_dir.is_dir() else []:
        if Path(str(zim) + ".aria2").exists():
            continue
        book = manuals.book_id_for(zim)
        if book not in newest or zim.name > newest[book].name:
            newest[book] = zim
    wanted = set(only or ())
    gutenberg = {book: zim for book, zim in newest.items() if shelves.is_gutenberg(book) and (not wanted or book in wanted)}
    summary = {"gutenberg_collections": 0, "gutenberg_books": 0, "rebuilt": 0}
    conn = manuals.connect()
    try:
        records: Dict[str, dict] = {}
        extra = "nocat"
        if gutenberg:
            path = pg_catalogue_path()
            if path.exists():
                extra = str(int(path.stat().st_mtime))
            needed = force or any(
                (conn.execute("SELECT stamp FROM shelf_sources WHERE book = ?", (book,)).fetchone() or {"stamp": ""})["stamp"]
                != f"{zim.name}:{zim.stat().st_size}:{int(zim.stat().st_mtime)}:r{RULES_VERSION}:{extra}"
                for book, zim in gutenberg.items())
            if needed:
                records = read_pg_catalogue(path)
                if not records:
                    say("No Gutenberg catalogue on disk (aegis-library shelve --fetch-catalogue gets it): "
                        "books are filed by their collection only.")
        for book, zim in sorted(gutenberg.items()):
            try:
                result = build_gutenberg(conn, zim, records, stamp_extra=extra, force=force, progress=progress)
            except Exception as exc:
                say(f"{book}: FAILED — {type(exc).__name__}: {exc}")
                continue
            summary["gutenberg_collections"] += 1
            summary["gutenberg_books"] += result["items"]
            summary["rebuilt"] += 0 if result.get("skipped") else 1
        if not wanted:
            gone = [row["book"] for row in conn.execute("SELECT book FROM shelf_sources") if row["book"] not in newest]
            for book in gone:
                say(f"{book}: no longer installed, taking its books off the shelves")
                forget(conn, book)
            summary["documents"] = build_documents(conn, progress=progress)
        summary["filed_by_hand"] = apply_filed_by_hand(conn)
        conn.commit()
        summary["cut"] = apply_cut(conn)
        if summary["cut"]["hidden"]:
            say(f"the cut (popularity {summary['cut']['min_rating']:g} or more): {summary['cut']['kept']:,} kept, "
                f"{summary['cut']['hidden']:,} left off the shelves")
    finally:
        conn.close()
    summary["shelves"] = len([1 for count in counts().values() if count])
    summary["items"] = sum(counts().values())
    return summary
