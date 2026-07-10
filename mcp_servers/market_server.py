"""
market_server.py — the "Market Analysis (Bull / Base / Bear)" toolbox.

Built-in MCP server that lets the chat research a stock, index, commodity, ETF,
or cryptocurrency: resolve a name to a ticker, pull price history, compute the
technical signals a human would read off a chart (trend, momentum, range,
volatility), gather recent headlines, and lay out concrete Bull / Base / Bear
scenarios with real price levels — so the model can make an informed call.

Data sources (free, no API key): Yahoo Finance chart/search endpoints for
prices, Google News RSS for headlines. Everything is public market data.

NOT investment advice — this is analysis tooling. The model should present the
scenarios and reasoning, not a directive to buy or sell.
"""

import asyncio
import html
import math
import re
import sys
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

server = Server("market")

_UA = "Mozilla/5.0 (compatible; AegisMarket/1.0)"
_TIMEOUT = 15.0
_YF = "https://query1.finance.yahoo.com"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True, headers={"User-Agent": _UA})


def _fmt(x, nd=2):
    try:
        return f"{x:,.{nd}f}"
    except Exception:
        return str(x)


# ── symbol search ────────────────────────────────────────────────────────────
async def _search(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "Error: provide a name or ticker to search (e.g. 'gold', 'tesla', 'bitcoin')."
    try:
        async with _client() as c:
            r = await c.get(f"{_YF}/v1/finance/search", params={"q": q, "quotesCount": 10, "newsCount": 0})
        quotes = r.json().get("quotes", [])
    except Exception as e:
        return f"Symbol search failed: {e}"
    if not quotes:
        return f"No symbols found for '{q}'."
    out = [f"**Symbol search — '{q}'**"]
    for it in quotes[:10]:
        sym = it.get("symbol", "")
        name = it.get("shortname") or it.get("longname") or ""
        typ = it.get("quoteType", "")
        exch = it.get("exchDisp") or it.get("exchange") or ""
        out.append(f"- `{sym}` — {name} ({typ}{', ' + exch if exch else ''})")
    out.append("\nCommon tickers: gold `GC=F`, oil `CL=F`, S&P 500 `^GSPC`, Bitcoin `BTC-USD`, Ether `ETH-USD`.")
    return "\n".join(out)


# ── price fetch ──────────────────────────────────────────────────────────────
async def _fetch_chart(symbol: str, rng="1y", interval="1d"):
    async with _client() as c:
        r = await c.get(f"{_YF}/v8/finance/chart/{symbol}", params={"range": rng, "interval": interval})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    res = r.json().get("chart", {}).get("result")
    if not res:
        raise RuntimeError("no data (check the ticker symbol)")
    result = res[0]
    meta = result.get("meta", {})
    ts = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    # Drop points where close is null (holidays / bad ticks), keep ts aligned.
    pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
    return meta, [p[0] for p in pairs], [p[1] for p in pairs]


def _sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _rsi(vals, n=14):
    if len(vals) < n + 1:
        return None
    deltas = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    gains = [d if d > 0 else 0.0 for d in deltas][-n:]
    losses = [-d if d < 0 else 0.0 for d in deltas][-n:]
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _pct(a, b):
    return (a / b - 1) * 100 if b else 0.0


async def _headlines(query: str, limit=5):
    try:
        async with _client() as c:
            r = await c.get("https://news.google.com/rss/search",
                            params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        items = re.findall(r"<item>(.*?)</item>", r.text, re.S)
        out = []
        for it in items[:limit]:
            tm = re.search(r"<title>(.*?)</title>", it, re.S)
            if tm:
                out.append(html.unescape(re.sub(r"<.*?>", "", tm.group(1))).strip())
        return out
    except Exception:
        return []


# ── quote ────────────────────────────────────────────────────────────────────
async def _quote(symbol: str) -> str:
    sym = (symbol or "").strip()
    if not sym:
        return "Error: provide a ticker symbol (use market_search to find one)."
    try:
        meta, ts, closes = await _fetch_chart(sym, rng="5d", interval="1d")
    except Exception as e:
        return f"Quote failed for `{sym}`: {e}"
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    prev = meta.get("chartPreviousClose") or (closes[-2] if len(closes) > 1 else None)
    cur = meta.get("currency", "")
    out = [f"**{sym} — {meta.get('shortName') or meta.get('longName') or ''}**".rstrip(" —")]
    if price is not None:
        out.append(f"- Price: {_fmt(price)} {cur}")
    if price is not None and prev:
        out.append(f"- Change vs prev close: {_pct(price, prev):+.2f}%")
    if meta.get("exchangeName"):
        out.append(f"- Exchange: {meta['exchangeName']}")
    return "\n".join(out)


# ── the big one: analyze ─────────────────────────────────────────────────────
async def _analyze(symbol: str) -> str:
    sym = (symbol or "").strip()
    if not sym:
        return "Error: provide a ticker (use market_search: 'gold'→GC=F, 'bitcoin'→BTC-USD…)."
    try:
        meta, ts, c = await _fetch_chart(sym, rng="1y", interval="1d")
    except Exception as e:
        return f"Analysis failed for `{sym}`: {e}"
    if len(c) < 30:
        return f"Not enough price history for `{sym}` to analyze."
    name = meta.get("shortName") or meta.get("longName") or sym
    cur = meta.get("currency", "")
    price = c[-1]
    hi, lo = max(c), min(c)
    sma20, sma50, sma200 = _sma(c, 20), _sma(c, 50), _sma(c, 200)
    rsi = _rsi(c)
    # returns over trading-day lookbacks
    def chg(days):
        return _pct(price, c[-days - 1]) if len(c) > days else None
    d1, w1, m1, m3, y1 = chg(1), chg(5), chg(21), chg(63), _pct(price, c[0])
    # annualized volatility from daily log returns
    rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c)) if c[i - 1] > 0]
    vol = (sum((x - sum(rets) / len(rets)) ** 2 for x in rets) / len(rets)) ** 0.5 * math.sqrt(252) * 100 if rets else 0

    # mechanical signal tally
    sig = []
    if sma50 and sma200:
        sig.append(("trend", "bull" if sma50 > sma200 else "bear",
                    f"SMA50 {'above' if sma50 > sma200 else 'below'} SMA200 ({'golden' if sma50 > sma200 else 'death'} cross regime)"))
    if sma50:
        sig.append(("price vs SMA50", "bull" if price > sma50 else "bear",
                    f"price {'above' if price > sma50 else 'below'} SMA50 ({_fmt(sma50)})"))
    if sma200:
        sig.append(("price vs SMA200", "bull" if price > sma200 else "bear",
                    f"price {'above' if price > sma200 else 'below'} SMA200 ({_fmt(sma200)})"))
    if rsi is not None:
        r_state = "bear" if rsi > 70 else ("bull" if rsi < 30 else "neutral")
        note = "overbought" if rsi > 70 else ("oversold" if rsi < 30 else "neutral")
        sig.append(("RSI(14)", r_state, f"{rsi:.0f} ({note})"))
    if m3 is not None:
        sig.append(("3-month momentum", "bull" if m3 > 0 else "bear", f"{m3:+.1f}%"))
    bulls = sum(1 for _, s, _ in sig if s == "bull")
    bears = sum(1 for _, s, _ in sig if s == "bear")
    lean = "bullish" if bulls > bears else ("bearish" if bears > bulls else "mixed/neutral")

    news = await _headlines(f"{name} {sym}", limit=5)

    out = [f"**Market analysis — {sym} ({name})**",
           f"Price: **{_fmt(price)} {cur}**  |  1d {d1:+.1f}%  1w {w1:+.1f}%  1mo {m1:+.1f}%  3mo {m3:+.1f}%  1y {y1:+.1f}%"
           if all(v is not None for v in (d1, w1, m1, m3)) else f"Price: {_fmt(price)} {cur}  |  1y {y1:+.1f}%"]
    out.append("\n**Trend & momentum**")
    for label, s, note in sig:
        mark = {"bull": "🟢", "bear": "🔴", "neutral": "⚪"}[s]
        out.append(f"- {mark} {label}: {note}")
    out.append("\n**Range & risk**")
    out.append(f"- 52-week range: {_fmt(lo)} – {_fmt(hi)}  (now {_pct(price, hi):+.1f}% from high, {_pct(price, lo):+.1f}% from low)")
    out.append(f"- Annualized volatility: {vol:.0f}%")
    out.append(f"\n**Mechanical signal tally: {bulls} bullish / {bears} bearish → leans {lean}** (not advice; confirm against the news and fundamentals below)")

    # concrete levels for the three scenarios
    levels_above = sorted(v for v in [sma20, sma50, sma200, hi] if v and v > price)
    levels_below = sorted((v for v in [sma20, sma50, sma200, lo] if v and v < price), reverse=True)
    resistance = levels_above[0] if levels_above else hi
    support = levels_below[0] if levels_below else lo
    out.append("\n**Scenarios (build the case from the data above)**")
    out.append(f"- 🐂 **Bull**: reclaim/hold {_fmt(resistance)}, momentum + news improving → path toward 52wk high {_fmt(hi)}.")
    out.append(f"- 😐 **Base**: chops between support {_fmt(support)} and resistance {_fmt(resistance)}; no regime change.")
    out.append(f"- 🐻 **Bear**: lose {_fmt(support)} on volume / negative catalysts → path toward 52wk low {_fmt(lo)}.")

    if news:
        out.append("\n**Recent headlines**")
        out += [f"- {h}" for h in news]
    out.append("\n_Public market data via Yahoo Finance + Google News. Educational analysis, not investment advice._")
    return "\n".join(out)


async def _news(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "Error: provide a ticker or topic."
    heads = await _headlines(q, limit=10)
    if not heads:
        return f"No recent headlines found for '{q}'."
    return "\n".join([f"**Recent headlines — '{q}'**"] + [f"- {h}" for h in heads])


# ── MCP wiring ───────────────────────────────────────────────────────────────
@server.list_tools()
async def list_tools() -> list[Tool]:
    def _obj(props, required):
        return {"type": "object", "properties": props, "required": required}
    return [
        Tool(name="market_search", description=(
            "Resolve a name to a ticker symbol for stocks, ETFs, indices, commodities, or "
            "crypto (e.g. 'gold'→GC=F, 'tesla'→TSLA, 'bitcoin'→BTC-USD). Use first when the "
            "user names a market/commodity rather than a symbol."),
            inputSchema=_obj({"query": {"type": "string", "description": "Name or ticker"}}, ["query"])),
        Tool(name="market_quote", description=(
            "Current price and day change for a ticker symbol. Quick snapshot."),
            inputSchema=_obj({"symbol": {"type": "string", "description": "Ticker (e.g. AAPL, BTC-USD, GC=F)"}}, ["symbol"])),
        Tool(name="market_analyze", description=(
            "Full technical dossier for a ticker to make a Bull/Base/Bear call: price changes "
            "(1d–1y), moving averages (20/50/200), RSI, 52-week range, volatility, a mechanical "
            "signal tally, concrete support/resistance levels, and recent headlines. Use this to "
            "analyze a market or commodity and lay out the three scenarios."),
            inputSchema=_obj({"symbol": {"type": "string", "description": "Ticker symbol (use market_search if unsure)"}}, ["symbol"])),
        Tool(name="market_news", description=(
            "Recent news headlines for a ticker, company, market, or commodity (Google News)."),
            inputSchema=_obj({"query": {"type": "string", "description": "Ticker or topic"}}, ["query"])),
    ]


_DISPATCH = {
    "market_search": lambda a: _search(a.get("query", "")),
    "market_quote": lambda a: _quote(a.get("symbol", "")),
    "market_analyze": lambda a: _analyze(a.get("symbol", "")),
    "market_news": lambda a: _news(a.get("query", "")),
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
