"""Text helpers for the offline library: HTML → readable text, and picking the
passages of a long article that actually bear on a question.

Kept dependency-light on purpose (BeautifulSoup + lxml are already in the
venv): library pages come from a local kiwix-serve, are often long (a
Wikipedia species article is 30-60K characters) and the model's budget per
source is a few thousand, so *which* part is quoted matters more than how
prettily it is cleaned.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Tuple

_STOPWORDS = frozenset(
    """a about above after again against all also am an and any are aren't as at be because been
    before being below between both but by can can't cannot could couldn't did didn't do does
    doesn't doing don't down during each few for from further had hadn't has hasn't have haven't
    having he her here hers herself him himself his how i if in into is isn't it its itself
    let's me more most mustn't my myself no nor not of off on once only or other ought our ours
    ourselves out over own same shan't she should shouldn't so some such than that the their
    theirs them themselves then there these they this those through to too under until up very
    was wasn't we were weren't what when where which while who whom why with won't would
    wouldn't you your yours yourself yourselves tell show give explain describe please need
    want know find get make use using used best good way ways thing things like just really
    much many will shall may might must vs versus difference differences between apart etc
    thanks build building create creating step steps guide instructions tutorial properly
    correctly safely quickly easily easy simple per""".split()
)

_WORD = re.compile(r"[0-9A-Za-zÀ-ɏ]+(?:[-'’][0-9A-Za-zÀ-ɏ]+)*")

# Sections that are bibliography, not content.
_SKIP_SECTIONS = frozenset(
    {
        "references", "external links", "further reading", "notes", "citations",
        "bibliography", "footnotes", "sources", "see also", "notes and references",
        "works cited", "explanatory notes", "navigation menu",
    }
)

_ANSWERS_HEADING = re.compile(r"^\d+\s+answers?\b", re.I)

_DROP_SELECTORS = (
    "script", "style", "noscript", "template", "nav", "footer", "form", "iframe",
    "svg", "button", "select", "sup.reference", "span.mw-editsection", ".mw-editsection",
    ".navbox", ".vertical-navbox", ".reflist", "ol.references", ".refbegin",
    ".mw-jump-link", ".noprint", ".metadata", ".sistersitebox", ".portalbox",
    ".catlinks", "#toc", ".toc", ".mw-empty-elt", ".printfooter", ".authority-control",
)

_BLOCK_TAGS = (
    "p", "div", "section", "article", "li", "ul", "ol", "dl", "dt", "dd", "table",
    "tr", "blockquote", "pre", "figure", "figcaption", "details", "summary", "header",
    "main", "aside", "caption", "address", "hr",
)

_HEADING_TAGS = ("h1", "h2", "h3", "h4")


def query_terms(query: str) -> List[str]:
    """Meaningful lower-cased words of a question, in order, without duplicates."""
    seen = []
    for match in _WORD.finditer(query or ""):
        word = match.group(0).lower().strip("'’-")
        if word in _STOPWORDS:
            continue
        word = re.sub(r"['’]s$", "", word)          # anne's -> anne
        if len(word) < 2 or word in _STOPWORDS:
            continue
        if word not in seen:
            seen.append(word)
    return seen


_SUFFIXES = (
    "ifications", "ification", "ifying", "ified", "ifies", "ify",
    "izations", "ization", "isations", "isation", "ational", "ations", "ation",
    "ments", "ment", "ingly", "edly", "ings", "ing", "ous", "ity", "ed", "er", "ly",
)


def _stem(word: str) -> str:
    """Light stemmer so ``purifying`` meets ``purification``, ``trapping``
    meets ``traps`` and ``poisonous`` meets ``poison``. It only has to be
    good enough for scoring passages — the indexes do their own stemming."""
    word = re.sub(r"['’]s$", "", word.lower())
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith(("sses", "ches", "shes", "xes", "zes")):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: len(word) - len(suffix)]
            if len(word) >= 4 and word[-1] == word[-2] and word[-1] not in "aeioulsz":
                word = word[:-1]                    # trapp -> trap, stopp -> stop
            break
    if word.endswith("e") and len(word) > 4:
        word = word[:-1]
    return word


def html_to_text(html: str) -> Tuple[str, str]:
    """Return ``(title, text)`` for an HTML page.

    Headings become ``## Heading`` lines (so the text can be split into
    sections again), list items get a dash, and table rows are flattened to
    ``cell | cell`` — which keeps infobox facts such as a mushroom's
    "Edibility | deadly" row readable.
    """
    from bs4 import BeautifulSoup, NavigableString

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    for selector in _DROP_SELECTORS:
        for node in soup.select(selector):
            node.decompose()

    root = soup.select_one("#mw-content-text") or soup.select_one("main") or soup.body or soup

    if not title:
        heading = soup.find("h1")
        if heading:
            title = heading.get_text(" ", strip=True)

    # Line breaks in the HTML source are not line breaks in the text: fold
    # them (outside <pre>) so a paragraph wrapped at 80 columns stays one line.
    for node in root.find_all(string=True):
        if "\n" in node and node.find_parent("pre") is None:
            node.replace_with(NavigableString(re.sub(r"\s+", " ", str(node))))

    for br in root.find_all("br"):
        br.replace_with("\n")
    for cell in root.find_all(["td", "th"]):
        cell.append(NavigableString(" | "))
    for item in root.find_all("li"):
        item.insert(0, NavigableString("- "))
    for name in _HEADING_TAGS:
        for heading in root.find_all(name):
            label = heading.get_text(" ", strip=True)
            heading.replace_with(NavigableString(f"\n\n## {label}\n\n" if label else "\n"))
    for block in root.find_all(_BLOCK_TAGS):
        block.insert_before(NavigableString("\n"))
        block.append(NavigableString("\n"))

    return title, clean_text(root.get_text(""))


def clean_text(text: str) -> str:
    text = text.replace("\r", "\n").replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"(?:\s*\|\s*)+$", "", line.strip())  # trailing cell separators
        line = re.sub(r"^(?:-\s*)+$", "", line)             # empty list items
        lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_sections(text: str) -> List[Tuple[str, str]]:
    """Split ``html_to_text`` output into ``(heading, body)`` pairs. The text
    before the first heading is the lead, with an empty heading."""
    sections: List[Tuple[str, str]] = []
    heading = ""
    body: List[str] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            if heading or any(part.strip() for part in body):
                sections.append((heading, "\n".join(body).strip()))
            heading, body = line[3:].strip(), []
        else:
            body.append(line)
    if heading or any(part.strip() for part in body):
        sections.append((heading, "\n".join(body).strip()))
    return [(h, b) for h, b in sections if b or h]


def _score(text: str, stems: Sequence[str]) -> float:
    if not stems or not text:
        return 0.0
    words = [_stem(m.group(0)) for m in _WORD.finditer(text.lower())]
    if not words:
        return 0.0
    hits = 0.0
    distinct = 0
    for stem in stems:
        count = sum(1 for word in words if word == stem)
        if count:
            distinct += 1
            hits += min(count, 6)
    # Distinct terms dominate; density breaks ties without rewarding length.
    return distinct * 10.0 + hits / (1.0 + len(words) / 400.0)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut < limit * 0.6:
        cut = text.rfind(". ", 0, limit)
        cut = cut + 1 if cut > 0 else limit
    return text[:cut].rstrip() + " …"


def best_passages(text: str, query: str, budget: int = 4000, lead_chars: int = 1200) -> str:
    """Condense an article to roughly ``budget`` characters: the lead, then the
    sections that match the question best, in their original order.

    With no usable query terms this is just the beginning of the article.
    """
    text = text or ""
    if len(text) <= budget:
        return text
    sections = [
        (heading, body) for heading, body in split_sections(text)
        if heading.strip().lower() not in _SKIP_SECTIONS
    ]
    # A site's navigation menu sometimes precedes the first heading: a short
    # run of list items, unlike a (possibly just as short) lead paragraph.
    if len(sections) > 1 and not sections[0][0] and len(sections[0][1]) < 200:
        lines = [line for line in sections[0][1].splitlines() if line.strip()]
        if lines and sum(line.startswith("- ") for line in lines) >= max(1, len(lines) - 1):
            sections = sections[1:]
    if len(sections) <= 1:
        return _clip(text, budget)

    # Question-and-answer pages (Stack Exchange archives): the answers are the
    # point. Quote the question briefly and give the rest to the answers,
    # which the archive lists best-first.
    for index, (heading, body) in enumerate(sections):
        if _ANSWERS_HEADING.match(heading) and index > 0 and body:
            question_heading, question_body = sections[index - 1]
            question = _clip(question_body, max(300, int(budget * 0.25)))
            answers = _clip(body, max(300, budget - len(question) - len(question_heading) - 40))
            return f"## Question: {question_heading}\n{question}\n\n## {heading}\n{answers}"

    stems = [_stem(term) for term in query_terms(query)]
    parts: List[Tuple[int, str]] = []
    used = 0

    first_heading, first_body = sections[0]
    lead = _clip(first_body, min(lead_chars, budget))
    if first_heading:
        lead = f"## {first_heading}\n{lead}"
    parts.append((0, lead))
    used += len(lead)

    ranked = sorted(
        (
            (_score(f"{heading} {heading} {body}", stems), index)
            for index, (heading, body) in enumerate(sections) if index > 0
        ),
        key=lambda pair: (-pair[0], pair[1]),
    )
    for score, index in ranked:
        remaining = budget - used
        if remaining < 300:
            break
        if stems and score <= 0:
            break
        heading, body = sections[index]
        chunk = f"## {heading}\n{_clip(body, min(remaining - len(heading) - 4, 2400))}"
        parts.append((index, chunk))
        used += len(chunk) + 2

    parts.sort(key=lambda pair: pair[0])
    out = "\n\n".join(chunk for _, chunk in parts)
    if len(parts) < len(sections):
        out += "\n\n[other sections of this article omitted]"
    return out


def coverage(text: str, terms: Iterable[str]) -> float:
    """Fraction of the query's terms that occur in ``text`` (stem-matched)."""
    stems = {_stem(term) for term in terms}
    if not stems:
        return 0.0
    words = {_stem(m.group(0)) for m in _WORD.finditer((text or "").lower())}
    return len(stems & words) / len(stems)
