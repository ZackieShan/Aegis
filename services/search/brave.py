"""Brave Search beyond the list of links: the passages that answer a question, the news, and places nearby.

    passages(query)   the parts of the top pages that bear on the question, with their sources — one request
                      in place of a search followed by fetching and trimming three pages ourselves
    news(query)       recent articles with when each was published
    places(query)     shops, restaurants and services near a position or town: address, phone, hours, rating
    web(query)        the plain list of results (what the provider chain and deep research use)

Every request is counted (``services/connections``) because Brave charges by the request, and none is made
once the month's limit is reached: the caller falls back to the next provider. Identical requests inside a
quarter of an hour are answered from memory and cost nothing.
"""

from __future__ import annotations

import html
import json
import logging
import math
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from services import connections
from services.connections import http
from services.connections.http import Problem

logger = logging.getLogger(__name__)

SERVICE = "brave"
API = "https://api.search.brave.com/res/v1"
NAME = "Brave Search"
FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}

_REMEMBER_SECONDS = 15 * 60
_REMEMBER_MOST = 256
_remembered: Dict[str, Tuple[float, Any]] = {}
_lock = threading.Lock()
_TAG = re.compile(r"<[^>]{1,80}>")
_no_passages_until = 0.0


def plain(text: Any) -> str:
    """Brave marks the matched words with <strong>; the assistant wants the words."""
    return html.unescape(_TAG.sub("", str(text or ""))).strip()


def _key() -> str:
    """The key, from wherever the search providers keep theirs (the Integrations screen first)."""
    try:
        from .providers import _get_provider_key
        found = _get_provider_key("brave")
    except Exception:
        found = ""
    return found or connections.value(SERVICE)


def why_not() -> Optional[Problem]:
    """Why Brave cannot be asked right now, or None when it can."""
    if not _key():
        return Problem(f"No {NAME} key is set (Settings → Integrations).", "key")
    if not connections.on(SERVICE):
        return Problem(f"{NAME} is switched off (Settings → Integrations).", "off")
    if connections.over_limit(SERVICE):
        return Problem(f"This month's limit of {connections.limit(SERVICE):,} {NAME} requests is used up "
                       "(Settings → Integrations sets it).", "limit")
    return None


def ready() -> bool:
    """A key is set, the service is switched on, and this month's limit is not used up."""
    return why_not() is None


def forget() -> None:
    """Drop what is remembered: after a key changes, and before a real try of the service."""
    global _no_passages_until
    with _lock:
        _remembered.clear()
    _no_passages_until = 0.0


def _ask(path: str, params: Dict[str, Any], kind: str, headers: Optional[Dict[str, str]] = None) -> Any:
    problem = why_not()
    if problem is not None:
        raise problem
    params = {key: value for key, value in params.items() if value not in (None, "")}
    mark = json.dumps([path, sorted(params.items()), sorted((headers or {}).items())], default=str)
    now = time.monotonic()
    with _lock:
        held = _remembered.get(mark)
        if held and now - held[0] < _REMEMBER_SECONDS:
            return held[1]
    sent = {"X-Subscription-Token": _key()}
    sent.update(headers or {})
    found = http.get(API + path, params=params, headers=sent, name=NAME)
    connections.count(SERVICE, kind)
    with _lock:
        if len(_remembered) >= _REMEMBER_MOST:
            for old in sorted(_remembered, key=lambda item: _remembered[item][0])[: _REMEMBER_MOST // 4]:
                _remembered.pop(old, None)
        _remembered[mark] = (now, found)
    return found


def _safesearch() -> Optional[str]:
    try:
        from .providers import _safesearch_for
        return _safesearch_for("brave")
    except Exception:
        return None


def _question(query: str, words: int = 50, chars: int = 400) -> str:
    """Brave refuses a query over 400 characters or 50 words; a long question is cut, not refused."""
    kept = " ".join(str(query or "").split()[:words])
    return kept[:chars].strip()


def _age(value: Any) -> str:
    """Brave gives a page's age as ["Monday, November 2, 2026", "2026-11-02", "22 days ago", iso]."""
    if isinstance(value, (list, tuple)):
        parts = [str(part) for part in value if part]
        relative = next((part for part in parts if part.endswith("ago")), "")
        day = next((part for part in parts if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part)), "")
        return f"{day} ({relative})" if day and relative else (day or relative or (parts[0] if parts else ""))
    return str(value or "")


# ── the plain list of results ──────────────────────────────────────────────

def web(query: str, count: int = 5, time_filter: Optional[str] = None) -> List[dict]:
    """Results as the provider chain wants them: title, url, snippet, age."""
    found = _ask("/web/search", {
        "q": _question(query), "count": max(1, min(int(count or 5), 20)), "safesearch": _safesearch(),
        "freshness": FRESHNESS.get(str(time_filter or "").lower()),
    }, "web")
    results = []
    for item in ((found.get("web") or {}).get("results") or []):
        url = item.get("url") or ""
        if not url:
            continue
        results.append({
            "title": plain(item.get("title")), "url": url,
            "snippet": plain(item.get("description")),
            "age": str(item.get("age") or item.get("page_age") or ""),
        })
    return results[: max(1, int(count or 5))]


# ── the passages that answer the question ──────────────────────────────────

def passages(query: str, time_filter: Optional[str] = None, urls: int = 6, tokens: int = 3200) -> Tuple[List[dict], Dict[str, List[str]]]:
    """(results, {url: [passage, …]}) — the top pages and the parts of each that bear on the question.

    ``results`` are in Brave's order and shaped like every provider's (title, url, snippet, age), so the
    rest of the search pipeline — the source list, the numbering the model cites by — is unchanged.
    """
    global _no_passages_until
    if time.monotonic() < _no_passages_until:
        return [], {}
    try:
        found = _ask("/llm/context", {
            "q": _question(query), "count": 20, "safesearch": _safesearch(),
            "freshness": FRESHNESS.get(str(time_filter or "").lower()),
            "maximum_number_of_urls": max(1, min(int(urls), 20)),
            "maximum_number_of_tokens": max(1024, min(int(tokens), 16000)),
            "maximum_number_of_tokens_per_url": 1024,
        }, "passages")
    except Problem as problem:
        if problem.reason == "refused" and "plan" in str(problem).lower():
            # The key's plan has no passages. Stop asking for an hour; the plain search still works.
            _no_passages_until = time.monotonic() + 3600
            logger.info("Brave: this plan gives no passages; using the plain search")
            return [], {}
        raise
    sources = found.get("sources") if isinstance(found.get("sources"), dict) else {}
    results: List[dict] = []
    texts: Dict[str, List[str]] = {}
    for item in ((found.get("grounding") or {}).get("generic") or []):
        url = item.get("url") or ""
        parts = [str(part).strip() for part in (item.get("snippets") or []) if str(part or "").strip()]
        if not url or not parts or url in texts:
            continue
        about = sources.get(url) if isinstance(sources.get(url), dict) else {}
        results.append({
            "title": plain(item.get("title") or about.get("title")), "url": url,
            "snippet": plain(about.get("snippet") or parts[0])[:300], "age": _age(about.get("age")),
        })
        texts[url] = parts
    return results, texts


# ── the news ───────────────────────────────────────────────────────────────

def news(query: str, count: int = 8, time_filter: Optional[str] = None) -> List[dict]:
    """Recent articles, newest information first as Brave ranks them: title, source, when, summary, url."""
    found = _ask("/news/search", {
        "q": _question(query), "count": max(1, min(int(count or 8), 20)), "safesearch": _safesearch(),
        "freshness": FRESHNESS.get(str(time_filter or "").lower()),
    }, "news")
    stories = []
    for item in (found.get("results") or []):
        url = item.get("url") or ""
        if not url:
            continue
        host = ((item.get("meta_url") or {}).get("hostname") or "").removeprefix("www.")
        stories.append({
            "title": plain(item.get("title")), "url": url, "source": host,
            "when": str(item.get("page_age") or "")[:10], "age": str(item.get("age") or ""),
            "summary": plain(item.get("description")), "more": [],
        })
    return stories


# ── places nearby ──────────────────────────────────────────────────────────

def miles_between(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.7613 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _hours(opening: Any) -> Tuple[str, Dict[str, str]]:
    """("11:00–22:00" for today, {"Monday": "11:00–22:00", …}) from Brave's opening_hours."""
    def spans(day: Any) -> Tuple[str, str]:
        slots = day if isinstance(day, list) else [day]
        name = ""
        parts = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            name = name or str(slot.get("full_name") or slot.get("abbr_name") or "")
            if slot.get("opens") and slot.get("closes"):
                parts.append(f"{slot['opens']}–{slot['closes']}")
        return name, ", ".join(parts)

    if not isinstance(opening, dict):
        return "", {}
    today_name, today = spans(opening.get("current_day") or [])
    week: Dict[str, str] = {}
    for day in opening.get("days") or []:
        name, text = spans(day)
        if name and text:
            week[name] = text
    if today_name and today:             # Brave lists today apart from the other six days
        week[today_name] = today
    return today, week


_TRACKING = re.compile(r"(?:^|&)(?:utm_[a-z]+|cm_mmc|y_source|gclid|fbclid|msclkid|ref|referrer)=", re.I)


def _tidy(url: str) -> str:
    """A website's address without the tracking tail some listings carry."""
    url = str(url or "")
    base, mark, tail = url.partition("?")
    return base if mark and _TRACKING.search(tail) else url


def places(query: str, near: str = "", position: Optional[Tuple[float, float]] = None, count: int = 6) -> List[dict]:
    """Places matching ``query`` near a town (``near``) or a position, nearest first when the position is known.

    Each: name, kind, address, phone, today's hours, the week's hours, rating, price, website, and the
    distance in miles when a position was given.
    """
    asked = _question(query, words=40, chars=300)
    if near and near.lower() not in asked.lower():
        asked = f"{asked} near {near}".strip()
    headers = {}
    if position:
        headers = {"X-Loc-Lat": f"{position[0]:.4f}", "X-Loc-Long": f"{position[1]:.4f}"}
    found = _ask("/web/search", {"q": asked, "count": 5, "safesearch": _safesearch(), "result_filter": "locations,web"},
                 "places", headers=headers)
    spots = []
    for item in ((found.get("locations") or {}).get("results") or []):
        name = plain(item.get("title"))
        if not name:
            continue
        today, week = _hours(item.get("opening_hours"))
        rating = item.get("rating") if isinstance(item.get("rating"), dict) else {}
        coordinates = item.get("coordinates") if isinstance(item.get("coordinates"), (list, tuple)) else None
        where = None
        if coordinates and len(coordinates) == 2:
            try:
                where = (float(coordinates[0]), float(coordinates[1]))
            except (TypeError, ValueError):
                where = None
        spot = {
            "name": name, "kind": plain(item.get("description")),
            "cuisine": [str(kind) for kind in (item.get("serves_cuisine") or []) if kind][:4],
            "address": plain((item.get("postal_address") or {}).get("displayAddress")),
            "phone": str((item.get("contact") or {}).get("telephone") or ""),
            "today": today, "week": week, "zone": str(item.get("timezone") or ""),
            "rating": rating.get("ratingValue"), "reviews": rating.get("reviewCount"),
            "price": str(item.get("price_range") or ""), "website": _tidy(item.get("url")),
            "position": where, "miles": round(miles_between(position, where), 1) if position and where else None,
        }
        spots.append(spot)
    if position:
        spots.sort(key=lambda spot: (spot["miles"] is None, spot["miles"] or 0.0))
    return spots[: max(1, min(int(count or 6), 20))]
