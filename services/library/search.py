"""One search over the whole offline library: Kiwix articles + PDF manuals.

``library_search`` returns ranked hits in the shape the web-search pipeline
already understands (``title`` / ``url`` / ``snippet``), ``fetch_content``
turns a hit into quotable text, and ``format_for_model`` renders both as the
block handed to the model by the ``library_search`` agent tool.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from . import config, kiwix, manuals
from .text import coverage, query_terms

logger = logging.getLogger(__name__)

# Collections whose titles are worth an exact-title lookup (encyclopedias:
# "Amanita phalloides" should open that article, not the page that mentions
# it most often).
_TITLE_LOOKUP_PREFIXES = ("wikipedia_en_all", "wikipedia_en_medicine", "mdwiki_en_all",
                          "wikibooks_en_all", "wikivoyage_en_all")
_MAX_TITLE_WORDS = 6

# Disambiguation tags of articles that are almost never what a reference
# lookup wants. Matched on the title; ignored when the query uses the word.
_POP_CULTURE = re.compile(
    r"\((?:[^()]*\b)?(album|band|song|film|movie|novel|video game|game|TV series|"
    r"miniseries|episode|EP|soundtrack|musical|opera|play|comics|manga|anime|character|"
    r"wrestler|footballer|disambiguation)\)\s*$",
    re.I,
)

VALID_SCOPES = ("all", "articles", "manuals")


def available() -> bool:
    return kiwix.available()


def _title_hits(query: str, limit: int = 2) -> List[dict]:
    """Exact or near-exact title matches in the encyclopedic collections."""
    words = query.strip().split()
    if not words or len(words) > _MAX_TITLE_WORDS:
        return []
    hits: List[dict] = []
    wanted = re.sub(r"[^a-z0-9]+", " ", query.lower()).strip()
    for book in kiwix.list_books():
        if not book["id"].startswith(_TITLE_LOOKUP_PREFIXES):
            continue
        for row in kiwix.suggest(book["id"], query, count=4):
            tag = _POP_CULTURE.search(row["title"])
            if tag and tag.group(1).lower() not in query.lower():
                continue
            title = re.sub(r"[^a-z0-9]+", " ", row["title"].lower()).strip()
            if title == wanted or title.startswith(wanted + " ") or wanted.startswith(title + " "):
                row.update(snippet="", book_title=book["title"], kind="article", exact=title == wanted)
                hits.append(row)
        if len(hits) >= limit:
            break
    hits.sort(key=lambda h: not h["exact"])
    return hits[:limit]


# Encyclopedic / curated collections. They are searched separately from the
# Q&A archives and scraped sites so a pile of forum posts cannot push the
# encyclopedia article off the first page.
_CORE_PREFIXES = ("wikipedia_", "mdwiki_", "wikibooks_", "wikivoyage_", "medlineplus",
                  "nhs.uk", "wikem", "appropedia", "energypedia", "wikivet", "gardenology")
_SPLIT_ABOVE = 6          # with fewer collections one search over everything is fine
_WIKI_FAMILY = ("wikipedia_", "mdwiki_")


def _merge(into: List[dict], more: List[dict]) -> List[dict]:
    seen = {hit["url"] for hit in into}
    for hit in more:
        if hit["url"] not in seen:
            seen.add(hit["url"])
            into.append(hit)
    return into


def _search_relaxed(query: str, count: int, books: Optional[List[str]]) -> List[dict]:
    """Kiwix requires every word of the pattern, so a question as typed
    usually matches nothing useful ("how", "with" and "a" are required too).
    Ladder: keywords -> the raw text -> keywords minus one (any one) ->
    adjacent keyword pairs. Later rungs only run when the earlier ones came
    back nearly empty; results found by several relaxed queries rank first."""
    terms = query_terms(query)
    keywords = " ".join(terms)
    enough = min(3, count)

    hits = kiwix.search(keywords, count=count, books=books) if keywords else []
    if len(hits) >= enough:
        return hits
    if query.strip().lower() != keywords:
        _merge(hits, kiwix.search(query, count=count, books=books))
    if len(hits) >= enough or len(terms) < 3:
        return hits

    def _vote(patterns: List[str]) -> List[dict]:
        tally: Dict[str, List] = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            for found in pool.map(lambda p: kiwix.search(p, count=5, books=books), patterns):
                for rank, hit in enumerate(found):
                    entry = tally.setdefault(hit["url"], [0, rank, hit])
                    entry[0] += 1
                    entry[1] = min(entry[1], rank)
        ordered = sorted(tally.values(), key=lambda e: (-e[0], e[1]))
        return [entry[2] for entry in ordered]

    if len(terms) <= 8:
        leave_one_out = [" ".join(terms[:i] + terms[i + 1:]) for i in range(len(terms))]
        _merge(hits, _vote(leave_one_out))
    if len(hits) < enough:
        pairs = [f"{a} {b}" for a, b in zip(terms, terms[1:])][:8]
        _merge(hits, _vote(pairs))
    return hits[:count]


def _article_hits(query: str, count: int) -> List[dict]:
    books = kiwix.list_books()
    core = [b["id"] for b in books if b["id"].startswith(_CORE_PREFIXES)]
    other = [b["id"] for b in books if not b["id"].startswith(_CORE_PREFIXES)]
    if len(books) <= _SPLIT_ABOVE or not core or not other:
        hits = _search_relaxed(query, count, None)
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            core_job = pool.submit(_search_relaxed, query, count, core)
            other_job = pool.submit(_search_relaxed, query, count, other)
            core_hits, other_hits = core_job.result(), other_job.result()
        # Two encyclopedia results for each one from elsewhere.
        hits = []
        core_iter, other_iter = iter(core_hits), iter(other_hits)
        while len(hits) < count * 2:
            block = [next(core_iter, None), next(core_iter, None), next(other_iter, None)]
            block = [hit for hit in block if hit]
            if not block:
                break
            hits.extend(block)
    for hit in hits:
        hit["kind"] = "article"
    return hits[: count * 2]


def library_search(query: str, count: int = 8, scope: str = "all") -> List[dict]:
    """Search the offline library. ``scope``: ``all`` | ``articles`` | ``manuals``."""
    query = (query or "").strip()
    if not query or not config.enabled():
        return []
    scope = scope if scope in VALID_SCOPES else "all"
    count = max(1, min(int(count or 8), 25))

    articles: List[dict] = []
    manual_hits: List[dict] = []
    titles: List[dict] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {}
        if scope in ("all", "articles") and kiwix.available():
            jobs["articles"] = pool.submit(_article_hits, query, count)
            jobs["titles"] = pool.submit(_title_hits, query)
        if scope in ("all", "manuals"):
            jobs["manuals"] = pool.submit(manuals.search, query, count)
        for name, job in jobs.items():
            try:
                value = job.result(timeout=40)
            except Exception as exc:
                logger.warning("library %s search failed: %s", name, exc)
                value = []
            if name == "articles":
                articles = value
            elif name == "titles":
                titles = value
            else:
                manual_hits = value

    book_names = kiwix.book_titles() if (articles or manual_hits or titles) else {}
    for hit in manual_hits:
        hit["book_title"] = book_names.get(hit["book"], hit["book"])

    # Interleave the two sources, then reorder by how much of the question
    # each hit covers: terms in the title count most ("Hypothermia" beats a
    # page that merely mentions it), terms in the snippet next, and the
    # search engines' own order breaks ties.
    merged: List[dict] = []
    for index in range(max(len(articles), len(manual_hits))):
        if index < len(articles):
            merged.append(articles[index])
        if index < len(manual_hits):
            merged.append(manual_hits[index])
    terms = query_terms(query)
    query_lc = query.lower()
    for rank, hit in enumerate(merged):
        title = hit.get("doc_title") or hit.get("title", "")
        score = coverage(f"{title} {hit.get('snippet', '')}", terms) + 1.5 * coverage(title, terms)
        tag = _POP_CULTURE.search(title)
        if tag and tag.group(1).lower() not in query_lc:
            score -= 1.0          # "Tourniquet (band)" when nobody asked about a band
        hit["_rank"] = rank
        hit["_cov"] = round(score, 2)
    merged.sort(key=lambda h: (-h["_cov"], h["_rank"]))

    seen = set()
    wiki_titles = set()
    results: List[dict] = []
    for hit in titles + merged:
        key = hit["url"].split("#", 1)[0] + (f"#{hit.get('page')}" if hit.get("page") else "")
        if key in seen:
            continue
        seen.add(key)
        # The full Wikipedia, WikiMed and MDWiki carry the same medical
        # articles; one copy is enough.
        if hit.get("kind") == "article" and str(hit.get("book", "")).startswith(_WIKI_FAMILY):
            title_key = re.sub(r"[^a-z0-9]+", " ", hit.get("title", "").lower()).strip()
            if title_key in wiki_titles:
                continue
            wiki_titles.add(title_key)
        hit.pop("_rank", None)
        hit.pop("_cov", None)
        hit.pop("exact", None)
        results.append(hit)
        if len(results) >= count:
            break
    return results


def owns_url(url: str) -> bool:
    return kiwix.owns_url(url)


def fetch_content(url: str, query: str = "", budget: int = 4000) -> dict:
    """Quotable text for a library link (article passages or manual pages)."""
    if not kiwix.owns_url(url):
        return {"url": url, "title": "", "content": "", "success": False,
                "error": "not an offline-library link"}
    if manuals.is_manual_url(url):
        return manuals.passage(url, budget=budget)
    return kiwix.fetch_article(url, query=query, budget=budget)


def collection_summary(limit: int = 12) -> str:
    """Short human list of what is installed, biggest first."""
    books = sorted(kiwix.list_books(), key=lambda b: -(b["articles"] + b["media"]))
    names = [b["title"] for b in books[:limit]]
    extra = len(books) - len(names)
    text = ", ".join(names)
    return f"{text}, and {extra} more" if extra > 0 else text


def format_for_model(
    query: str,
    results: List[dict],
    fetch: int = 4,
    budget: int = 3500,
    offline_note: bool = False,
    max_chars: Optional[int] = None,
) -> Tuple[str, List[dict]]:
    """Render hits (+ the text of the first ``fetch`` of them) as the block the
    model reads. Returns ``(text, sources)`` with ``sources`` shaped for the
    chat UI's sources box.

    ``max_chars`` is the caller's hard cap on the whole block (agent tool
    output is truncated at ``MAX_OUTPUT_CHARS``): the excerpt budget shrinks
    to fit under it, so the closing instructions are never cut off."""
    if not results:
        return (
            f"No matches in the offline library for: {query}\n"
            "Try fewer or different keywords (the library is searched by keyword, not by meaning).",
            [],
        )
    sources = [{"url": hit["url"], "title": hit["title"]} for hit in results]

    head = ["=" * 70, "OFFLINE LIBRARY RESULTS (local reference library, no internet needed)",
            f"Query: {query}"]
    if offline_note:
        head.append("NOTE: the internet is unreachable, so this answer can only draw on the "
                    "offline library. Say so if the question needs current information.")
    head += ["=" * 70, "", "MATCHES:", "-" * 50]
    for index, hit in enumerate(results, start=1):
        where = hit.get("book_title") or hit.get("book") or ""
        kind = "manual page" if hit.get("kind") == "manual" else "article"
        head.append(f"\n[{index}] {hit['title']}")
        head.append(f"    Source: {where} ({kind})")
        head.append(f"    URL: {hit['url']}")
        if hit.get("snippet"):
            head.append(f"    Snippet: {hit['snippet'][:240]}")
    tail = ["", "=" * 70, "END OF OFFLINE LIBRARY RESULTS", "=" * 70, "",
            "Cite the sources you use by their [number] and link. Manual pages are quoted from "
            "the PDF page named in the title; articles are excerpts, not the whole page."]
    head_text = "\n".join(head)
    tail_text = "\n".join(tail)

    fetch = max(0, min(fetch, len(results)))
    if max_chars and fetch:
        # ~260 characters of labels and rules around each excerpt.
        room = max_chars - len(head_text) - len(tail_text) - 120
        while fetch and room // fetch - 260 < 700:
            fetch -= 1
        if fetch:
            budget = min(budget, room // fetch - 260)

    fetched: Dict[int, dict] = {}
    targets = list(enumerate(results[:fetch], start=1))
    if targets:
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {index: pool.submit(fetch_content, hit["url"], query, budget) for index, hit in targets}
            for index, job in jobs.items():
                try:
                    content = job.result(timeout=40)
                except Exception as exc:
                    logger.warning("library fetch failed: %s", exc)
                    continue
                if content.get("success") and content.get("content"):
                    fetched[index] = content

    body: List[str] = []
    if fetched:
        body += ["", "=" * 70, "CONTENT:", "-" * 50]
        for index in sorted(fetched):
            content = fetched[index]
            body.append(f"\n[CONTENT {index}] From: {results[index - 1]['url']}")
            body.append(f"Title: {content.get('title') or results[index - 1]['title']}")
            body.append("-" * 30)
            excerpt = content["content"]
            if len(excerpt) > budget:          # fetchers aim for the budget; this enforces it
                excerpt = excerpt[:budget].rstrip() + " …"
            body.append(excerpt)
    return "\n".join([head_text] + body + [tail_text]), sources


def status() -> dict:
    """What the library currently holds — for the CLI, the API and /lib."""
    up = kiwix.available(force=True)
    books = kiwix.list_books(force=True) if up else []
    return {
        "enabled": config.enabled(),
        "kiwix_url": config.kiwix_url(),
        "kiwix_public_url": config.kiwix_public_url(),
        "kiwix_up": up,
        "collections": len(books),
        "articles": sum(book["articles"] for book in books),
        "books": books,
        "manuals": manuals.stats(),
        "library_dir": str(config.library_dir()),
    }
