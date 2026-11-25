"""Published studies on a question: who wrote what, where and when, how often it has been cited, what it
found (its abstract), and a link to the free full text when there is one.

OpenAlex first: it indexes every field, with abstracts and citation counts. It works without a key on a small
allowance the house shares with everyone on the same network address; a free key (Settings → Integrations)
gives the house its own. When OpenAlex cannot answer, Crossref does — no key, every field, but fewer
abstracts. The answer says which one it came from.

A web search finds pages about studies. This finds the studies.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from services import connections
from services.connections import http
from services.connections.http import Problem

logger = logging.getLogger(__name__)

SERVICE = "papers"
OPENALEX = "https://api.openalex.org/works"
CROSSREF = "https://api.crossref.org/works"
ABSTRACT = 900       # characters of each abstract in the answer
SORTS = {"": "", "relevance": "", "best": "", "newest": "newest", "new": "newest", "recent": "newest", "latest": "newest",
         "cited": "cited", "most cited": "cited", "citations": "cited", "influential": "cited"}


def _abstract(index: Any) -> str:
    """OpenAlex keeps an abstract as {word: [positions]}; put the words back in order."""
    if not isinstance(index, dict):
        return ""
    placed: List[Tuple[int, str]] = []
    for word, positions in index.items():
        for position in positions if isinstance(positions, list) else []:
            if isinstance(position, int):
                placed.append((position, str(word)))
    return " ".join(word for _, word in sorted(placed))


def _since(value: Any) -> Optional[int]:
    """A year to look back to: 2020, "2020", "5 years" or "last 3 years"."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    found = re.search(r"\b(19|20)\d{2}\b", text)
    if found:
        return int(found.group(0))
    found = re.search(r"(\d{1,2})\s*(?:years?|yrs?)", text)
    if found:
        return date.today().year - int(found.group(1))
    return None


_SMALL_WORDS = {"the", "and", "for", "with", "from", "that", "this", "does", "what", "how", "are", "can", "effect", "effects",
                "study", "studies", "use", "using", "between", "during", "after", "into", "about", "over", "under"}


def _terms(query: str) -> List[str]:
    return [word for word in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", query.lower()) if word not in _SMALL_WORDS]


def _in_title(title: str, terms: List[str]) -> float:
    """How much of the question is in the title, 0 to 1."""
    if not terms:
        return 0.0
    low = title.lower()
    return sum(1 for term in terms if term in low) / len(terms)


def _read_openalex(work: dict) -> Optional[dict]:
    title = " ".join(str(work.get("display_name") or "").split())
    if not title:
        return None
    authors = [str((item.get("author") or {}).get("display_name") or "") for item in (work.get("authorships") or [])]
    place = work.get("primary_location") or {}
    free = (work.get("best_oa_location") or {}).get("pdf_url") or (work.get("open_access") or {}).get("oa_url") or ""
    doi = str(work.get("doi") or "")
    return {
        "id": str(work.get("id") or title), "title": title, "authors": [name for name in authors if name],
        "year": work.get("publication_year"), "date": str(work.get("publication_date") or ""),
        "journal": str((place.get("source") or {}).get("display_name") or ""), "cited": int(work.get("cited_by_count") or 0),
        "kind": str(work.get("type") or ""), "abstract": _abstract(work.get("abstract_inverted_index")),
        "link": str(doi or place.get("landing_page_url") or work.get("id") or ""), "free": str(free),
        "retracted": bool(work.get("is_retracted")),
    }


def _openalex(query: str, since: Optional[int], sort: str, count: int) -> List[dict]:
    """Papers whose title or abstract holds the question's words.

    The index's own order puts a word-for-word title match first, however obscure; ordering by citations
    alone brings up famous papers that merely mention the words. So, unless a particular order was asked
    for, both lists are fetched and each paper is weighed on three things: how much of the question is in
    its title, how often it is cited, and how high the index itself placed it.
    """
    filters = [f"title_and_abstract.search:{re.sub(r'[,:|]', ' ', query)}", "type:article|review|preprint",
               f"to_publication_date:{date.today().isoformat()}"]
    if since:
        filters.append(f"from_publication_date:{since}-01-01")
    base: Dict[str, Any] = {
        "filter": ",".join(filters),
        "select": "id,doi,display_name,publication_year,publication_date,cited_by_count,type,authorships,"
                  "primary_location,best_oa_location,open_access,abstract_inverted_index,is_retracted",
    }
    key = connections.value(SERVICE)
    if key:
        base["api_key"] = key

    def ask(order: str, number: int) -> List[dict]:
        params = dict(base)
        params["per-page"] = number
        if order:
            params["sort"] = order
        found = http.get(OPENALEX, params=params, name="OpenAlex", timeout=20)
        return [paper for paper in (_read_openalex(work) for work in (found.get("results") or [])) if paper]

    if sort == "newest":
        return ask("publication_date:desc", count)
    if sort == "cited":
        return ask("cited_by_count:desc", count)
    matched = ask("", 15)
    if not matched:
        return []
    for place, paper in enumerate(matched):
        paper["place"] = place
    try:
        cited = ask("cited_by_count:desc", 15)
    except Problem:
        cited = []                       # the first list still answers the question
    known = {paper["id"] for paper in matched}
    papers = matched + [paper for paper in cited if paper["id"] not in known]
    terms = _terms(query)

    def weight(paper: dict) -> float:
        placed = 3.0 * (15 - paper["place"]) / 15 if "place" in paper else 0.0
        about = _in_title(paper["title"], terms)
        # Citations count for less when the title is about something else: a famous paper that only
        # mentions the subject should not come before a modest one that studies it.
        return 6.0 * about + 2.5 * math.log10(1 + paper["cited"]) * (0.5 + 0.5 * about) + placed

    papers.sort(key=weight, reverse=True)
    return papers[:count]


def _crossref(query: str, since: Optional[int], sort: str, count: int) -> List[dict]:
    params: Dict[str, Any] = {
        "query.bibliographic": query, "rows": count,
        "select": "title,author,container-title,issued,is-referenced-by-count,DOI,abstract,type",
        "filter": "type:journal-article" + (f",from-pub-date:{since}-01-01" if since else ""),
    }
    if sort == "newest":
        params.update(sort="published", order="desc")
    elif sort == "cited":
        params.update(sort="is-referenced-by-count", order="desc")
    found = http.get(CROSSREF, params=params, name="Crossref", timeout=20)
    papers = []
    for item in ((found.get("message") or {}).get("items") or []):
        title = " ".join(str((item.get("title") or [""])[0]).split())
        if not title:
            continue
        parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0] or [None]
        doi = str(item.get("DOI") or "")
        papers.append({
            "id": doi or title, "title": re.sub(r"<[^>]+>", "", title),
            "authors": [" ".join(part for part in (person.get("given"), person.get("family")) if part).title()
                        if str(person.get("family") or "").isupper() else " ".join(part for part in (person.get("given"), person.get("family")) if part)
                        for person in (item.get("author") or []) if person.get("family")],
            "year": parts[0], "date": "-".join(f"{int(part):02d}" for part in parts if part),
            "journal": str((item.get("container-title") or [""])[0]), "cited": int(item.get("is-referenced-by-count") or 0),
            "kind": "", "abstract": re.sub(r"<[^>]+>", " ", str(item.get("abstract") or "")),
            "link": f"https://doi.org/{doi}" if doi else "", "free": "", "retracted": False,
        })
    return papers


def find(query: str, since: Any = None, sort: str = "", count: int = 6) -> Tuple[List[dict], str]:
    """(papers, who answered). Raises ``Problem`` when nobody could."""
    query = " ".join(str(query or "").split())[:300]
    if len(query) < 3:
        raise Problem("Say what the studies should be about.", "refused")
    if not connections.on(SERVICE):
        raise Problem("Studies and papers are switched off (Settings → Integrations).", "off")
    count = max(1, min(int(count or 6), 10))
    year = _since(since)
    order = SORTS.get(str(sort or "").strip().lower(), "")
    first: Optional[Problem] = None
    try:
        papers = _openalex(query, year, order, count)
        if papers:
            return papers, "OpenAlex"
    except Problem as problem:
        first = problem
        logger.info("OpenAlex not used: %s", problem)
    try:
        papers = _crossref(query, year, order, count)
    except Problem as problem:
        raise first or problem
    if not papers:
        raise Problem(f"No published studies were found for “{query}”" + (f" since {year}." if year else ".") +
                      " Try fewer, more general words.", "nothing")
    return papers, "Crossref (OpenAlex could not be asked, so some abstracts are missing)" if first else "Crossref"


def answer(args: Dict[str, Any]) -> Tuple[str, List[dict]]:
    query = str(args.get("query") or args.get("about") or args.get("topic") or args.get("q") or args.get("_text") or "")
    papers, who = find(query, args.get("since") or args.get("from") or args.get("year"), str(args.get("sort") or ""), args.get("count") or 6)
    lines = [f"STUDIES about: {' '.join(query.split())}", f"From {who}. {len(papers)} papers:", ""]
    sources = []
    for number, paper in enumerate(papers, 1):
        names = paper["authors"]
        by = ", ".join(names[:3]) + (f" and {len(names) - 3} others" if len(names) > 3 else "")
        where = " · ".join(str(part) for part in (paper["journal"], paper["year"], paper["kind"].replace("-", " ") if paper["kind"] not in ("", "article") else "",
                                                  f"cited {paper['cited']:,} times" if paper["cited"] else "not yet cited") if part)
        lines.append(f"[{number}] {paper['title']}" + ("  — RETRACTED" if paper["retracted"] else ""))
        if by:
            lines.append(f"    {by}")
        lines.append(f"    {where}")
        text = " ".join(paper["abstract"].split())
        if text:
            lines.append("    Abstract: " + (text[:ABSTRACT].rsplit(" ", 1)[0] + " …" if len(text) > ABSTRACT else text))
        else:
            lines.append("    (No abstract in the index.)")
        lines.append(f"    {paper['link']}")
        if paper["free"] and paper["free"] != paper["link"]:
            lines.append(f"    Free full text: {paper['free']}")
        lines.append("")
        sources.append({"url": paper["link"], "title": paper["title"]})
    lines.append("These are the papers' own abstracts. Say what each study was (a trial, a review, how many people) and its year "
                 "when you report it; one paper is not a consensus. To read one, open its free full text with web_fetch.")
    return "\n".join(lines), sources
