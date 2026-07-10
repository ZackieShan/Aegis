"""
troubleshoot_server.py — the "Network & Systems Troubleshooting" toolbox.

Built-in MCP server with practical diagnostics the chat can run when something
isn't working: check whether a URL/host/port is reachable, inspect HTTP
responses and redirects, diagnose DNS, read a site's TLS certificate and expiry,
find this machine's outbound IP — plus everyday dev utilities (decode
base64/URL/JWT/hex/epoch, hash text, explain a cron expression).

Network checks use httpx (HTTP) and plain TCP sockets (ports/TLS); everything
else is local computation. No API keys.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import re
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

server = Server("troubleshoot")

_UA = "Mozilla/5.0 (compatible; AegisTroubleshoot/1.0)"


def _clean_host(value: str) -> str:
    v = (value or "").strip()
    v = re.sub(r"^[a-zA-Z]+://", "", v).split("/")[0].split("?")[0].split("@")[-1]
    if v.count(":") == 1:
        v = v.split(":")[0]
    return v.strip()


# ── HTTP check ───────────────────────────────────────────────────────────────
async def _http_check(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return "Error: provide a URL."
    if not re.match(r"^[a-zA-Z]+://", u):
        u = "https://" + u
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": _UA}) as c:
            r = await c.get(u)
    except httpx.ConnectTimeout:
        return f"❌ Connection to {u} timed out (host not responding on that port)."
    except httpx.ConnectError as e:
        return f"❌ Cannot connect to {u}: {e}"
    except Exception as e:
        return f"❌ Request to {u} failed: {type(e).__name__}: {e}"
    ms = round((time.time() - t0) * 1000)
    ok = "✅" if r.status_code < 400 else "⚠️"
    out = [f"{ok} **HTTP check — {u}**",
           f"- Status: {r.status_code} {r.reason_phrase}",
           f"- Time: {ms} ms"]
    if str(r.url) != u:
        out.append(f"- Final URL: {r.url}")
        if r.history:
            out.append(f"- Redirect chain: {' → '.join(str(h.status_code) for h in r.history)} → {r.status_code}")
    for h in ("server", "content-type", "location", "cache-control", "strict-transport-security"):
        if h in r.headers:
            out.append(f"- {h}: {r.headers[h][:120]}")
    return "\n".join(out)


# ── port check ───────────────────────────────────────────────────────────────
async def _one_port(host: str, port: int, timeout=5.0):
    t0 = time.time()
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return port, "open", round((time.time() - t0) * 1000)
    except asyncio.TimeoutError:
        return port, "filtered/timeout", None
    except ConnectionRefusedError:
        return port, "closed", None
    except Exception:
        return port, "error", None


_COMMON_PORTS = {21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns", 80: "http",
                 110: "pop3", 143: "imap", 443: "https", 465: "smtps", 587: "submission",
                 3306: "mysql", 3389: "rdp", 5432: "postgres", 6379: "redis", 8080: "http-alt",
                 8000: "http-alt", 27017: "mongodb"}


async def _port_check(host: str, ports: str) -> str:
    h = _clean_host(host)
    if not h:
        return "Error: provide a host."
    raw = (ports or "").strip()
    if raw:
        try:
            plist = sorted({int(p) for p in re.split(r"[,\s]+", raw) if p})
        except ValueError:
            return "Error: ports must be numbers, e.g. '22,80,443'."
    else:
        plist = [22, 80, 443, 8080]
    plist = plist[:20]
    try:
        ip = socket.gethostbyname(h)
    except Exception as e:
        return f"❌ Cannot resolve host `{h}`: {e}"
    results = await asyncio.gather(*[_one_port(h, p) for p in plist])
    out = [f"**Port check — {h} ({ip})**"]
    for port, state, ms in results:
        svc = _COMMON_PORTS.get(port, "")
        mark = "✅" if state == "open" else "❌"
        lat = f" ({ms} ms)" if ms is not None else ""
        out.append(f"- {mark} {port}{'/' + svc if svc else ''}: {state}{lat}")
    return "\n".join(out)


# ── DNS diagnose ─────────────────────────────────────────────────────────────
async def _dns(domain: str) -> str:
    h = _clean_host(domain)
    if not h:
        return "Error: provide a domain."
    async def q(rtype):
        try:
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": _UA}) as c:
                r = await c.get("https://cloudflare-dns.com/dns-query",
                                params={"name": h, "type": rtype}, headers={"Accept": "application/dns-json"})
            return [a.get("data", "") for a in (r.json().get("Answer") or [])]
        except Exception:
            return []
    a, aaaa, mx, ns, cname = await asyncio.gather(q("A"), q("AAAA"), q("MX"), q("NS"), q("CNAME"))
    out = [f"**DNS diagnosis — `{h}`**"]
    out.append(f"- A (IPv4): {', '.join(a) if a else '❌ none'}")
    if aaaa:
        out.append(f"- AAAA (IPv6): {', '.join(aaaa)}")
    out.append(f"- MX (mail): {', '.join(m.split()[-1] for m in mx) if mx else 'none'}")
    out.append(f"- NS (nameservers): {', '.join(ns) if ns else '❌ none'}")
    if cname:
        out.append(f"- CNAME: {', '.join(cname)}")
    if not a and not cname:
        out.append("\n⚠️ No A record or CNAME — this hostname does not resolve to a server.")
    return "\n".join(out)


# ── TLS certificate ──────────────────────────────────────────────────────────
def _get_cert(host: str, port=443, timeout=8.0):
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            return ssock.getpeercert()


async def _tls(host: str) -> str:
    h = _clean_host(host)
    if not h:
        return "Error: provide a hostname."
    try:
        cert = await asyncio.to_thread(_get_cert, h)
    except ssl.SSLCertVerificationError as e:
        return f"⚠️ TLS handshake to {h} succeeded but certificate did NOT verify: {e}"
    except Exception as e:
        return f"❌ Could not read TLS certificate for {h}:443: {type(e).__name__}: {e}"
    subj = dict(x[0] for x in cert.get("subject", []))
    issuer = dict(x[0] for x in cert.get("issuer", []))
    not_after = cert.get("notAfter", "")
    days = None
    try:
        exp = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = (exp - datetime.now(timezone.utc)).days
    except Exception:
        pass
    sans = [v for k, v in cert.get("subjectAltName", []) if k == "DNS"]
    out = [f"**TLS certificate — {h}:443**",
           f"- Subject: {subj.get('commonName', '?')}",
           f"- Issuer: {issuer.get('organizationName') or issuer.get('commonName', '?')}",
           f"- Valid until: {not_after}"]
    if days is not None:
        flag = "❌ EXPIRED" if days < 0 else ("⚠️ expiring soon" if days < 21 else "✅")
        out.append(f"- Days remaining: {days} {flag}")
    if sans:
        shown = ", ".join(sans[:12])
        out.append(f"- SANs ({len(sans)}): {shown}{' …' if len(sans) > 12 else ''}")
    return "\n".join(out)


# ── public IP ────────────────────────────────────────────────────────────────
async def _public_ip() -> str:
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": _UA}) as c:
            r = await c.get("http://ip-api.com/json/",
                            params={"fields": "query,country,regionName,city,isp,org,as,reverse"})
        d = r.json()
    except Exception as e:
        return f"Could not determine public IP: {e}"
    out = [f"**This machine's outbound public IP**",
           f"- IP: {d.get('query', '?')}"]
    loc = ", ".join(x for x in [d.get("city"), d.get("regionName"), d.get("country")] if x)
    if loc:
        out.append(f"- Location: {loc}")
    if d.get("isp"):
        out.append(f"- ISP: {d['isp']}")
    if d.get("as"):
        out.append(f"- ASN: {d['as']}")
    return "\n".join(out)


# ── decode / inspect ─────────────────────────────────────────────────────────
def _decode(value: str, kind: str) -> str:
    v = (value or "").strip()
    if not v:
        return "Error: provide a value to decode."
    kind = (kind or "auto").lower()

    def try_jwt(s):
        parts = s.split(".")
        if len(parts) != 3:
            return None
        try:
            def b64(seg):
                seg += "=" * (-len(seg) % 4)
                return json.loads(base64.urlsafe_b64decode(seg))
            return f"**JWT decoded** (signature NOT verified)\n- Header: {json.dumps(b64(parts[0]))}\n- Payload: {json.dumps(b64(parts[1]), indent=2)}"
        except Exception:
            return None

    def try_epoch(s):
        if not re.fullmatch(r"\d{10}(\d{3})?", s):
            return None
        n = int(s)
        if len(s) == 13:
            n //= 1000
        try:
            return f"**Epoch timestamp** {s}\n- UTC: {datetime.fromtimestamp(n, timezone.utc):%Y-%m-%d %H:%M:%S} UTC"
        except Exception:
            return None

    def try_b64(s):
        try:
            raw = base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
            txt = raw.decode("utf-8")
            if txt.isprintable() or "\n" in txt:
                return f"**Base64 decoded**\n```\n{txt[:1000]}\n```"
        except Exception:
            return None
        return None

    def try_hex(s):
        try:
            raw = binascii.unhexlify(re.sub(r"\s+", "", s))
            return f"**Hex decoded**\n```\n{raw.decode('utf-8', 'replace')[:1000]}\n```"
        except Exception:
            return None

    def try_url(s):
        from urllib.parse import unquote
        dec = unquote(s)
        return f"**URL decoded**\n{dec}" if dec != s else None

    funcs = {"jwt": try_jwt, "epoch": try_epoch, "base64": try_b64, "hex": try_hex, "url": try_url}
    if kind in funcs:
        res = funcs[kind](v)
        return res or f"Could not decode as {kind}."
    # auto: try in a sensible order
    for name in ("jwt", "epoch", "url", "base64", "hex"):
        res = funcs[name](v)
        if res:
            return res
    return "Could not auto-decode. Specify kind = jwt | base64 | hex | url | epoch."


def _hash(text: str) -> str:
    if not text:
        return "Error: provide text to hash."
    b = text.encode("utf-8")
    return ("**Hashes**\n"
            f"- MD5: `{hashlib.md5(b).hexdigest()}`\n"
            f"- SHA1: `{hashlib.sha1(b).hexdigest()}`\n"
            f"- SHA256: `{hashlib.sha256(b).hexdigest()}`\n"
            f"- SHA512: `{hashlib.sha512(b).hexdigest()}`")


def _cron(expr: str) -> str:
    e = (expr or "").strip()
    if not e:
        return "Error: provide a cron expression, e.g. '0 9 * * 1-5'."
    try:
        from croniter import croniter
    except Exception:
        return "croniter not available."
    if not croniter.is_valid(e):
        return f"❌ '{e}' is not a valid cron expression (expected 5 fields: min hour day month weekday)."
    base = datetime.now(timezone.utc)
    it = croniter(e, base)
    runs = [it.get_next(datetime).strftime("%Y-%m-%d %H:%M UTC") for _ in range(5)]
    return "\n".join([f"**Cron `{e}`** — next 5 runs (UTC):"] + [f"- {r}" for r in runs])


# ── MCP wiring ───────────────────────────────────────────────────────────────
@server.list_tools()
async def list_tools() -> list[Tool]:
    def _obj(props, required):
        return {"type": "object", "properties": props, "required": required}
    return [
        Tool(name="ts_http_check", description=(
            "Check whether a URL responds: HTTP status, response time, redirect chain, and key "
            "headers. Use to diagnose 'is this site up / why does it error'."),
            inputSchema=_obj({"url": {"type": "string", "description": "URL or domain"}}, ["url"])),
        Tool(name="ts_port_check", description=(
            "Test whether TCP ports are open on a host (open/closed/filtered + latency). Default "
            "ports 22,80,443,8080. Use to diagnose connectivity/firewall issues."),
            inputSchema=_obj({
                "host": {"type": "string", "description": "Hostname or IP"},
                "ports": {"type": "string", "description": "Comma-separated ports, e.g. '22,443,5432'"},
            }, ["host"])),
        Tool(name="ts_dns_diagnose", description=(
            "Diagnose DNS for a domain: A/AAAA/MX/NS/CNAME records, and flag if it doesn't "
            "resolve. Use for 'domain won't load / email not delivering'."),
            inputSchema=_obj({"domain": {"type": "string", "description": "Domain name"}}, ["domain"])),
        Tool(name="ts_tls_cert", description=(
            "Read a site's TLS/SSL certificate: issuer, subject, expiry date, days remaining, and "
            "SANs. Use for 'cert expired / HTTPS broken / which domains does this cert cover'."),
            inputSchema=_obj({"host": {"type": "string", "description": "Hostname (port 443)"}}, ["host"])),
        Tool(name="ts_public_ip", description=(
            "Show this machine's outbound public IP address, location, ISP and ASN."),
            inputSchema=_obj({}, [])),
        Tool(name="ts_decode", description=(
            "Decode/inspect a string: JWT (header+payload, unverified), base64, hex, URL-encoding, "
            "or an epoch timestamp. Auto-detects if 'kind' is omitted."),
            inputSchema=_obj({
                "value": {"type": "string", "description": "The string to decode"},
                "kind": {"type": "string", "description": "jwt | base64 | hex | url | epoch (optional)"},
            }, ["value"])),
        Tool(name="ts_hash", description=(
            "Compute MD5/SHA1/SHA256/SHA512 hashes of a text string."),
            inputSchema=_obj({"text": {"type": "string", "description": "Text to hash"}}, ["text"])),
        Tool(name="ts_cron_explain", description=(
            "Validate a cron expression and show its next 5 run times (UTC). Use when building or "
            "debugging scheduled jobs."),
            inputSchema=_obj({"expression": {"type": "string", "description": "Cron expression, e.g. '0 9 * * 1-5'"}}, ["expression"])),
    ]


_DISPATCH = {
    "ts_http_check": lambda a: _http_check(a.get("url", "")),
    "ts_port_check": lambda a: _port_check(a.get("host", ""), a.get("ports", "")),
    "ts_dns_diagnose": lambda a: _dns(a.get("domain", "")),
    "ts_tls_cert": lambda a: _tls(a.get("host", "")),
    "ts_public_ip": lambda a: _public_ip(),
}
_SYNC_DISPATCH = {
    "ts_decode": lambda a: _decode(a.get("value", ""), a.get("kind", "auto")),
    "ts_hash": lambda a: _hash(a.get("text", "")),
    "ts_cron_explain": lambda a: _cron(a.get("expression", "")),
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    args = arguments or {}
    try:
        if name in _DISPATCH:
            text = await _DISPATCH[name](args)
        elif name in _SYNC_DISPATCH:
            text = _SYNC_DISPATCH[name](args)
        else:
            text = f"Unknown tool: {name}"
    except Exception as e:
        text = f"{name} failed: {type(e).__name__}: {e}"
    return [TextContent(type="text", text=text)]


async def run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
