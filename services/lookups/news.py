"""The news on a subject: recent articles, each with where it ran and when.

Brave Search's news index when the house has a key for it (and is inside the month's limit); otherwise the
public Google News feed, which needs no key. Either way the answer says which one it came from, so nobody
takes a headline feed for a search of the whole press.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

from services.connections import http
from services.connections.http import Problem

logger = logging.getLogger(__name__)

WHEN = {"today": "day", "day": "day", "24h": "day", "week": "week", "this week": "week", "month": "month",
        "this month": "month", "year": "year", "this year": "year"}
_FEED_AGE = {"day": "1d", "week": "7d", "month": "30d", "year": "1y"}
_TAG = re.compile(r"<[^>]+>")


def _when(value: Any) -> Optional[str]:
    return WHEN.get(str(value or "").strip().lower())


def _feed(query: str, when: Optional[str], count: int) -> List[dict]:
    """The public Google News feed for a search: no key, headlines and dates only."""
    asked = f"{query} when:{_FEED_AGE[when]}" if when in _FEED_AGE else query
    text = http.get("https://news.google.com/rss/search", params={"q": asked, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                    name="Google News", want="text")
    try:
        from defusedxml import ElementTree
        root = ElementTree.fromstring(text.encode("utf-8"))
    except Exception:
        raise Problem("Google News sent back something that could not be read.", "offline")
    stories = []
    for item in root.iter("item"):
        title = html.unescape(item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        source = (item.findtext("source") or "").strip()
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        day = age = ""
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            day = published.astimezone().strftime("%Y-%m-%d")
            age = _ago(published)
        except (TypeError, ValueError):
            pass
        stories.append({"title": title, "url": link, "source": source, "when": day, "age": age, "summary": "", "more": []})
    stories.sort(key=lambda story: story["when"], reverse=True)
    return stories[:count]


def _ago(moment: datetime) -> str:
    seconds = (datetime.now(timezone.utc) - moment.astimezone(timezone.utc)).total_seconds()
    if seconds < 0:
        return ""
    if seconds < 3600:
        number, unit = max(1, int(seconds // 60)), "minute"
    elif seconds < 86400:
        number, unit = int(seconds // 3600), "hour"
    else:
        number, unit = int(seconds // 86400), "day"
    return f"{number} {unit}{'' if number == 1 else 's'} ago"


def find(query: str, when: Any = None, count: int = 8) -> Tuple[List[dict], str]:
    """(stories, who answered). Raises ``Problem`` when nobody could."""
    query = " ".join(str(query or "").split())
    if not query:
        raise Problem("Say what the news should be about.", "refused")
    count = max(1, min(int(count or 8), 15))
    span = _when(when)
    from services.search import brave

    first: Optional[Problem] = None
    if brave.why_not() is None:
        try:
            stories = brave.news(query, count, span)
            if stories:
                return stories, brave.NAME
        except Problem as problem:
            first = problem
            logger.info("Brave news not used: %s", problem)
    try:
        stories = _feed(query, span, count)
    except Problem as problem:
        raise first or problem
    if not stories:
        raise Problem(f"No news articles were found about “{query}”" + (f" in the last {span}." if span else "."), "nothing")
    return stories, "Google News (headlines only)"


def answer(args: Dict[str, Any]) -> Tuple[str, List[dict]]:
    """The text for the assistant and the sources for the screen."""
    query = str(args.get("query") or args.get("about") or args.get("topic") or args.get("q") or args.get("_text") or "")
    stories, who = find(query, args.get("when") or args.get("time_filter") or args.get("freshness"), args.get("count") or 8)
    today = datetime.now().astimezone()
    lines = [f"NEWS about: {' '.join(query.split())}",
             f"From {who}. Today is {today:%A}, {today:%B} {today.day}, {today.year}. {len(stories)} articles, each with its date:", ""]
    for number, story in enumerate(stories, 1):
        dated = " · ".join(part for part in (story["source"], story["when"], story["age"]) if part)
        lines.append(f"[{number}] {story['title']}")
        if dated:
            lines.append(f"    {dated}")
        if story["summary"]:
            lines.append(f"    {story['summary'][:400]}")
        for extra in story["more"][:1]:
            lines.append(f"    {extra[:300]}")
        lines.append(f"    {story['url']}")
        lines.append("")
    lines.append("Give the date with anything you report from these, and say when the articles disagree. "
                 "A headline is not the whole story: to say more than these lines hold, read the article with web_fetch.")
    sources = [{"url": story["url"], "title": story["title"]} for story in stories]
    return "\n".join(lines), sources
