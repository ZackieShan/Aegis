"""Look a taxon up in the offline library and boil its article down to the
facts an identification needs: what it looks like, what it is confused with,
and whether it will hurt you.

Works on Wikipedia-style articles served by Kiwix (``services.library``).
Returns nothing — quietly — when the library is not installed.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

# Section headings worth quoting, most useful first: (pattern, characters).
_SECTION_PRIORITY = (
    (re.compile(r"similar|look.?alike|confus|distinguish|identif", re.I), 750),
    (re.compile(r"toxic|poison|venom|bite|sting|edib|culinary|food|uses|medic|safety|"
                r"symptom|treatment|first aid|human", re.I), 850),
    (re.compile(r"descript|characteristic|appearance|morpholog|anatomy", re.I), 550),
    (re.compile(r"distribution|habitat|range|ecology|season", re.I), 320),
)
_ICON = re.compile(r"icon|\.svg|OOjs|Red_Pencil|Status_|Question_book|Wiki|Commons-logo|"
                   r"Symbol_|Padlock|Ambox|Edit-|Speaker|Loudspeaker|Gnome|Crystal|map|range|distribution",
                   re.I)
_RASTER = re.compile(r"\.(jpe?g|png|webp)$", re.I)


def _encyclopedia() -> Optional[str]:
    """Id of the main encyclopedia collection, biggest edition first."""
    from services.library import kiwix

    books = [b for b in kiwix.list_books() if b["id"].startswith("wikipedia_en")]
    if not books:
        return None
    full = [b for b in books if b["id"].startswith("wikipedia_en_all")]
    pool = full or books
    return max(pool, key=lambda b: b["articles"])["id"]


def _article_url(book: str, name: str) -> str:
    from services.library import kiwix

    slug = quote(name.strip().replace(" ", "_"), safe="()_,'-.")
    return kiwix.public_url(f"/content/{book}/{slug}")


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s*\n\s*", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text.rfind(". ", 0, limit)
    return (text[: cut + 1] if cut > limit * 0.5 else text[:limit].rstrip()) + " …"


def notes_from_article(html: str, budget: int = 1900) -> dict:
    """``{"title", "notes", "image"}`` from an article's HTML.

    ``notes`` leads with the infobox field marks (a mushroom's "Edibility is
    deadly", "Spore print is white"), then the opening paragraph, then the
    sections that matter for identification, clipped to ``budget``."""
    from services.library.text import html_to_text, split_sections

    title, text = html_to_text(html)
    sections = split_sections(text)
    parts: List[str] = []
    used = 0

    lead = sections[0][1] if sections and not sections[0][0] else ""
    marks = [line[2:].strip() for line in lead.splitlines() if line.startswith("| ") and len(line) < 90]
    if marks:
        chunk = "Field marks: " + "; ".join(dict.fromkeys(marks))[:420]
        parts.append(chunk)
        used += len(chunk)
    paragraphs = [line for line in lead.split("\n") if len(line) >= 140]
    if paragraphs:
        chunk = _clip(paragraphs[0], 520)
        parts.append(chunk)
        used += len(chunk)

    taken = set()
    for pattern, limit in _SECTION_PRIORITY:
        for index, (heading, body) in enumerate(sections):
            if index in taken or not heading or not body or not pattern.search(heading):
                continue
            room = budget - used
            if room < 180:
                break
            chunk = f"{heading}: {_clip(body, min(limit, room - len(heading) - 2))}"
            parts.append(chunk)
            used += len(chunk)
            taken.add(index)
            break            # one section per priority class

    image = ""
    for src in re.findall(r'<img[^>]*\ssrc="([^"]+)"', html)[:12]:
        if _ICON.search(src) or not _RASTER.search(src.split("?", 1)[0]):
            continue
        image = src
        break
    return {"title": title, "notes": "\n".join(parts), "image": image}


def lookup(name: str, budget: int = 1900) -> Optional[dict]:
    """Reference notes for a scientific or common name, or None.

    Tries the article of that exact name (the encyclopedia redirects
    scientific names to common-name titles and vice versa), then the best
    title completion, then — for a binomial — the genus."""
    from services.library import kiwix

    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name or not kiwix.available():
        return None
    book = _encyclopedia()
    if not book:
        return None

    tried = []
    candidates = [name]
    words = name.split()
    if len(words) >= 2 and words[0][:1].isupper() and words[1].islower():
        candidates.append(words[0])                     # genus fallback
    for index, candidate in enumerate(candidates):
        urls = [_article_url(book, candidate)]
        if index == 0:
            urls += [hit["url"] for hit in kiwix.suggest(book, candidate, count=2)]
        for url in urls:
            if url in tried:
                continue
            tried.append(url)
            fetched = kiwix.fetch(url)
            if not fetched or "html" not in fetched[0]:
                continue
            html = fetched[1].decode("utf-8", "replace")
            info = notes_from_article(html, budget=budget)
            if not info["notes"]:
                continue
            image_path = ""
            if info["image"]:
                src = info["image"].lstrip("./")
                image_path = f"/content/{book}/{src}"
            return {
                "query": name,
                "matched": candidate,
                "is_genus_fallback": index == 1,
                "title": info["title"] or candidate,
                "url": url,
                "notes": info["notes"],
                "image_path": image_path,
            }
    return None
