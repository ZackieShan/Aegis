"""
web_server.py — the "Web Research — Crawl & Extract" toolbox.

A firecrawl-style capability, fully local and keyless: turn a web page (or a
whole same-domain section of a site) into clean, LLM-ready markdown. Feeds
Recipes (as a tool node), opt-in chat (/web), and anything that wants readable
page text instead of raw HTML.

  web_extract(url)                      one page -> clean markdown
  web_crawl(url, max_pages, same_domain) BFS the site -> combined markdown

Data source: public web pages fetched over HTTPS. No API key.
"""

import asyncio
import sys
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# One page reader for the whole app: this toolbox reads pages through the same
# guarded fetcher search uses (public addresses only, redirects re-checked,
# bodies size-capped). It used to carry its own, with none of those checks.
from services.search.content import read_public_page  # noqa: E402

server = Server("web")

_TIMEOUT = 15.0


async def _fetch(url: str) -> tuple[str, str, list[str]]:
    """(title, markdown, same-site links) of one public page."""
    return await asyncio.to_thread(read_public_page, url, _TIMEOUT)


async def _extract_one(url: str) -> str:
    u = (url or "").strip()
    if not u.startswith("http"):
        u = "https://" + u
    try:
        title, text, _ = await _fetch(u)
    except Exception as e:
        return f"Fetch failed for {u}: {type(e).__name__}: {e}"
    if not text:
        return f"**{title}**\n{u}\n\n(No readable text extracted.)"
    return f"# {title}\n<{u}>\n\n{text[:12000]}"


async def _crawl(url: str, max_pages: int = 8, same_domain: bool = True) -> str:
    start = (url or "").strip()
    if not start.startswith("http"):
        start = "https://" + start
    try:
        max_pages = max(1, min(int(max_pages), 25))
    except Exception:
        max_pages = 8
    seen: set[str] = set()
    q: deque[str] = deque([start])
    out: list[str] = []
    try:
        while q and len(seen) < max_pages:
            u = q.popleft()
            if u in seen:
                continue
            seen.add(u)
            try:
                title, text, links = await _fetch(u)
            except Exception as e:
                out.append(f"## {u}\n(failed: {type(e).__name__})")
                continue
            if text:
                out.append(f"## {title}\n<{u}>\n\n{text[:4000]}")
            for link in links:
                if link not in seen and len(seen) + len(q) < max_pages * 2:
                    q.append(link)
    except Exception as e:
        return f"Crawl failed for {start}: {type(e).__name__}: {e}"
    if not out:
        return f"No pages could be crawled from {start}."
    header = f"**Crawled {len(out)} page(s) from `{urlparse(start).netloc}`**\n"
    return header + "\n\n---\n\n".join(out)[:16000]


# ── MCP wiring ───────────────────────────────────────────────────────────────
@server.list_tools()
async def list_tools() -> list[Tool]:
    def _obj(props, required):
        return {"type": "object", "properties": props, "required": required}
    return [
        Tool(name="web_extract", description=(
            "Fetch ONE web page and return its main content as clean, readable markdown "
            "(strips nav/ads/scripts). Use when the user names a specific URL/page and wants "
            "its content, not a search."),
            inputSchema=_obj({"url": {"type": "string", "description": "Page URL"}}, ["url"])),
        Tool(name="web_crawl", description=(
            "Crawl a site starting from a URL (breadth-first, same domain) and return the "
            "combined main content of up to `max_pages` pages as markdown. Use to ingest a docs "
            "site, blog, or section for research/summarization. Bounded and keyless."),
            inputSchema=_obj({
                "url": {"type": "string", "description": "Start URL"},
                "max_pages": {"type": "integer", "description": "Max pages to fetch (1–25, default 8)"},
            }, ["url"])),
    ]


_DISPATCH = {
    "web_extract": lambda a: _extract_one(a.get("url", "")),
    "web_crawl": lambda a: _crawl(a.get("url", ""), a.get("max_pages", 8)),
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    try:
        text = await handler(arguments or {})
    except Exception as e:
        text = f"{name} failed: {type(e).__name__}: {e}"
    return [TextContent(type="text", text=text)]


async def run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
