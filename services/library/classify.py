"""Asking a model where a document belongs.

The folder a file was dropped in and the words of its title place most
things (:mod:`.shelves`). What is left — a scan named ``DOC-0041988.pdf``, a
pile of "assorted books" — is shown to the local model: the file's name, its
own metadata, its contents list and its first page or two. The model answers
with a title, an author, a year, one sentence on what it is, and a shelf
picked from the library's own list. Its choice is kept only when it names a
shelf that exists; otherwise the rule-based shelf stays.

Everything stays on this machine: the model is the one Aegis already runs.
Nothing a person filed by hand is ever asked about again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from . import catalogue, config, manuals, shelves

logger = logging.getLogger(__name__)

OPENING_CHARS = 1800
_ASKABLE = (config.FILES_BOOK, config.ADDED_BOOK)

SYSTEM_PROMPT = """You file documents in a home reference library. You are given the library's numbered list of shelves \
and what is known about one document. Reply with ONE JSON object and nothing else:

{"title": "...", "author": "...", "year": null, "shelf": 12, "shelf_name": "Category › Shelf", "form": "...", "about": "..."}

- title: the document's real title, as a person would say it. No file extensions, no site names, no "(1)". \
For a report or memo with no title, a short plain description of it, keeping any reference number.
- author: the person or body that wrote it; "" when it is not stated.
- year: the year it was written or published, ONLY when the text itself states it; otherwise null. Never work a \
year out of a file name or a reference number.
- shelf and shelf_name: the NUMBER of exactly one shelf from the list, and that same line's name copied out. \
Choose by what the document is for: a cookbook goes on a \
Cooking shelf by its style of cooking, a history on a History shelf by the era it covers, a novel or stories on a \
Literature shelf by when it was written, a how-to on the shelf of its skill. If a shelf is suggested by its folder \
and title, keep it unless it is plainly wrong. If a folder hint names a category, stay inside that category unless \
the document plainly is something else. Always the most specific shelf that fits.
- form: two or three words for the kind of thing it is: cookbook, field manual, novel, textbook, technical report, \
declassified memo, research paper, illustrated guide, magazine.
- about: one plain sentence, under 30 words, saying what is in it."""


def numbered_shelves() -> List[str]:
    """Every place a document can be filed, in tree order: the shelves, and a
    category itself when it has no shelves (the owner's own categories)."""
    places: List[str] = []
    for category in shelves.tree():
        places.extend(shelf["path"] for shelf in category["shelves"])
        if not category["shelves"]:
            places.append(category["name"])
    return places


def shelf_list() -> str:
    lines = []
    for number, place in enumerate(numbered_shelves(), start=1):
        what = shelves.about(place)
        lines.append(f"{number}. {shelves.display(place)}" + (f" — {what}" if what else ""))
    return "\n".join(lines)


def facts(conn: sqlite3.Connection, book: str, key: str, row: Optional[sqlite3.Row] = None) -> Optional[dict]:
    """What is known about one catalogued document, for the model."""
    doc = conn.execute("SELECT id, title, pages FROM docs WHERE book = ? AND path = ?", (book, key)).fetchone()
    if doc is None:
        return None
    opening: List[str] = []
    size = 0
    first = doc["id"] * manuals._PAGE_FACTOR
    for page in conn.execute("SELECT text FROM pages WHERE rowid > ? AND rowid < ? ORDER BY rowid LIMIT 12",
                             (first, first + manuals._PAGE_FACTOR)):
        text = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", page["text"] or "")).strip()
        if len(text) < 40:
            continue
        opening.append(text[: OPENING_CHARS - size])
        size += len(opening[-1])
        if size >= OPENING_CHARS:
            break
    item = row or conn.execute("SELECT * FROM shelf_items WHERE book = ? AND key = ?", (book, key)).fetchone()
    shelf_now = item["shelf"] if item is not None else ""
    return {
        "file": key.rsplit("/", 1)[-1], "folder": key.rsplit("/", 1)[0] if "/" in key else "",
        "title_now": doc["title"], "author_now": item["author"] if item is not None else "",
        "pages": int(doc["pages"] or 0), "hint": shelves.folder_hint(key) if book == config.FILES_BOOK else "",
        "shelf_now": shelf_now, "suggested": "" if unsure(shelf_now) else shelf_now,
        "opening": "\n\n".join(opening),
    }


def prompt_for(known: dict) -> str:
    parts = [f"File name: {known['file']}"]
    if known.get("folder"):
        parts.append(f"Folder: {known['folder']}")
    if known.get("hint"):
        parts.append(f"Folder hint: {shelves.display(known['hint'])}")
    if known.get("suggested"):
        places = numbered_shelves()
        if known["suggested"] in places:
            parts.append(f"Shelf suggested by its folder and title: {places.index(known['suggested']) + 1}. {shelves.display(known['suggested'])}")
    parts.append(f"Title as found: {known.get('title_now') or '(none)'}")
    if known.get("author_now"):
        parts.append(f"Author as found: {known['author_now']}")
    parts.append(f"Pages: {known.get('pages') or 'unknown'}")
    parts.append("How it begins:\n" + (known.get("opening") or "(no readable text — a scan that has not been read yet)"))
    return "THE SHELVES:\n" + shelf_list() + "\n\nTHE DOCUMENT:\n" + "\n".join(parts) + "\n\nReturn the JSON object now."


def read_answer(raw: str, fallback_shelf: str = "") -> Optional[dict]:
    """The model's answer made safe: a shelf that exists (or the fallback), a
    sane year, short fields. None when there is no usable answer at all."""
    from services.memory.profile import _parse_object

    try:
        data = _parse_object(raw or "")
    except Exception:
        data = {}
    if not isinstance(data, dict) or not data:
        return None
    places = numbered_shelves()
    chosen = data.get("shelf")
    shelf = ""
    number = None
    if isinstance(chosen, (int, float)) and not isinstance(chosen, bool):
        number = int(chosen)
    elif isinstance(chosen, str) and re.fullmatch(r"\s*\d{1,3}\s*\.?", chosen):
        number = int(re.sub(r"\D", "", chosen))
    named = shelves.resolve(re.sub(r"^\s*\d{1,3}\s*[.)]\s*", "", str(data.get("shelf_name") or "")).split(" — ")[0])
    if number is not None and 1 <= number <= len(places):
        shelf = places[number - 1]
    elif isinstance(chosen, str):                       # it wrote the name out instead
        shelf = shelves.resolve(re.sub(r"^\s*\d{1,3}\s*[.)]\s*", "", chosen).split(" — ")[0])
    if named and shelves.SEP in named and named != shelf:
        shelf = named                                   # the number and the name disagree: the name is what it meant
    if not shelf or shelf == shelves.UNSORTED or not shelves.valid(shelf):
        shelf = fallback_shelf
    elif shelves.SEP not in shelf and fallback_shelf and shelves.under(fallback_shelf, shelf) and fallback_shelf != shelf:
        shelf = fallback_shelf                          # only the category named: the rules knew the shelf inside it
    if not shelf:
        return None
    year = data.get("year")
    try:
        year = int(year) if year is not None and str(year).strip() not in ("", "null", "None") else None
    except (TypeError, ValueError):
        year = None
    if year is not None and not (-3000 <= year <= time.gmtime().tm_year + 1):
        year = None

    def clean(value: object, limit: int) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

    title = clean(data.get("title"), 240)
    return {"title": re.sub(r"\.(pdf|epub|mobi|txt)$", "", title, flags=re.I), "author": clean(data.get("author"), 160),
            "year": year, "shelf": shelf, "form": clean(data.get("form"), 60), "about": clean(data.get("about"), 400)}


async def ask_model(prompt: str) -> str:
    """The local model's reply. Raises when no model answers."""
    from src.llm_core import llm_call_async_with_fallback
    from src.task_endpoint import resolve_task_candidates

    candidates = resolve_task_candidates()
    if not candidates:
        raise RuntimeError("no model is set up")
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
    return await llm_call_async_with_fallback(candidates, messages, temperature=0.1, max_tokens=1500, timeout=180)


def unsure(shelf: str) -> bool:
    """Is this a placement the rules could not finish? Unsorted, a category
    with no shelf chosen inside it, a cookbook of unknown style, a story of
    unknown date."""
    if not shelf or shelf == shelves.UNSORTED or shelf in (shelves.COOKING_DEFAULT, "Literature/Collections & undated"):
        return True
    category, name = shelves.split(shelf)
    if name:
        return False
    return any(entry["name"] == category and entry["shelves"] for entry in shelves.tree())     # a category that has shelves


def pending(limit: int = 0, books: Tuple[str, ...] = _ASKABLE, redo: bool = False,
            everything: bool = False) -> List[Tuple[str, str]]:
    """Documents for the model to look at: those the rules left unsure, never
    ones filed by hand or already looked at. ``everything`` also takes the
    ones a folder or a title placed firmly (for their titles and authors);
    ``redo`` asks again about ones the model has seen."""
    conn = manuals.connect(readonly=True)
    if conn is None:
        return []
    try:
        marks = ",".join("?" for _ in books)
        sources = "('rule', 'folder', '', 'model')" if redo else "('rule', 'folder', '')"
        rows = conn.execute(
            f"SELECT book, key, shelf FROM shelf_items WHERE book IN ({marks}) AND source IN {sources} ORDER BY key",
            list(books)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    chosen = [(row["book"], row["key"]) for row in rows if everything or unsure(row["shelf"])]
    return chosen[:limit] if limit else chosen


async def run(limit: int = 0, redo: bool = False, everything: bool = False,
              ask: Optional[Callable[[str], Awaitable[str]]] = None,
              progress: Optional[Callable[[str], None]] = None, stop: Optional[Callable[[], bool]] = None) -> dict:
    """Ask the model about the documents the rules left unsure (all of them
    with ``everything``). One at a time (it is the same model people are
    chatting with); each answer is saved as it comes, so stopping loses
    nothing."""
    say = progress or (lambda message: None)
    todo = pending(limit=limit, redo=redo, everything=everything)
    summary = {"asked": 0, "filed": 0, "kept": 0, "failed": 0, "left": 0}
    if not todo:
        return summary
    say(f"asking the model about {len(todo)} document(s)")
    failures_in_a_row = 0
    for number, (book, key) in enumerate(todo, start=1):
        if stop and stop():
            summary["left"] = len(todo) - number + 1
            break
        conn = manuals.connect()
        try:
            item = conn.execute("SELECT * FROM shelf_items WHERE book = ? AND key = ?", (book, key)).fetchone()
            known = facts(conn, book, key, item)
        finally:
            conn.close()
        if known is None or item is None:
            continue
        if not known["opening"] and known["pages"]:
            from . import files
            if files.is_code_name(known["file"]):
                # A scan that has not been read yet, named by a number: there is
                # nothing to judge it by. It waits for OCR (`files --ocr`).
                summary["unread"] = summary.get("unread", 0) + 1
                continue
        summary["asked"] += 1
        try:
            raw = await (ask or ask_model)(prompt_for(known))
            failures_in_a_row = 0
        except Exception as exc:
            summary["failed"] += 1
            failures_in_a_row += 1
            logger.warning("library: the model could not be asked about %s: %s", key, exc)
            if failures_in_a_row >= 3:                 # no model is answering: stop rather than fail a thousand times
                summary["left"] = len(todo) - number
                say("the model is not answering; stopped")
                break
            continue
        fallback = item["shelf"] if item["shelf"] != shelves.UNSORTED else ""
        answer = read_answer(raw, fallback_shelf=fallback)
        if answer is None:
            summary["kept"] += 1
            continue
        conn = manuals.connect()
        try:
            title = answer["title"] or item["title"]
            catalogue.put(conn, book, key, title, answer["shelf"], author=answer["author"] or item["author"],
                          dates=str(answer["year"]) if answer["year"] else "", year=answer["year"],
                          form=answer["form"] or item["form"], note=answer["about"], source="model")
            if title != item["title"]:
                manuals.retitle(conn, book, key, title)
            conn.commit()
        finally:
            conn.close()
        summary["filed"] += 1
        if number % 10 == 0 or number == len(todo):
            say(f"model: {number}/{len(todo)} looked at, {summary['filed']} filed")
    return summary


def run_sync(**options) -> dict:
    return asyncio.run(run(**options))
