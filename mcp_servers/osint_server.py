"""
osint_server.py — the "Intel & Recon" toolbox.

A built-in MCP server exposing OSINT reconnaissance tools the chat can call
when investigating a domain, website, IP address, email, username, phone
number, or organization. Every tool queries **public** data sources and uses
**no API keys**, so the toolbox works out of the box.

Sources used (all free / no-auth): RDAP (rdap.org) for registration data,
Cloudflare DNS-over-HTTPS for DNS, ip-api.com for IP geolocation/ASN,
crt.sh certificate transparency for subdomain discovery, Gravatar for email
avatars, and direct HTTP for website fingerprinting. Username search checks
public profile endpoints across common sites.

Intended for authorized OSINT / security research, due diligence, and
investigation of information that is already public. It does not bypass
authentication, scrape private data, or perform intrusive scanning.
"""

import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

server = Server("osint")

_UA = "Mozilla/5.0 (compatible; AegisOSINT/1.0; +https://aegis.local)"
_TIMEOUT = 10.0

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})+$")
_IPV4_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": _UA},
    )


def _clean_host(value: str) -> str:
    """Strip scheme/path/port so 'https://example.com/x' -> 'example.com'."""
    v = (value or "").strip()
    v = re.sub(r"^[a-zA-Z]+://", "", v)
    v = v.split("/")[0].split("?")[0]
    v = v.split("@")[-1]  # drop any user@ prefix
    if v.count(":") == 1:  # host:port (leave IPv6 with multiple colons alone)
        v = v.split(":")[0]
    return v.strip().lower()


# ── whois / RDAP ─────────────────────────────────────────────────────────────
async def _whois(target: str) -> str:
    host = _clean_host(target)
    if not host:
        return "Error: provide a domain or IP address."
    kind = "ip" if _IPV4_RE.match(host) or ":" in host else "domain"
    url = f"https://rdap.org/{kind}/{host}"
    try:
        async with _client() as c:
            r = await c.get(url, headers={"Accept": "application/rdap+json"})
    except Exception as e:
        return f"RDAP lookup failed for {host}: {e}"
    if r.status_code == 404:
        return f"No RDAP record found for `{host}` (unregistered, or the TLD/registry has no RDAP)."
    if r.status_code >= 400:
        return f"RDAP returned HTTP {r.status_code} for `{host}`."
    try:
        d = r.json()
    except Exception:
        return f"RDAP returned non-JSON for `{host}`."

    out = [f"**RDAP / WHOIS — `{host}`**"]
    if kind == "domain":
        out.append(f"- Domain: {d.get('ldhName') or host}")
        status = d.get("status") or []
        if status:
            out.append(f"- Status: {', '.join(status)}")
        for ev in d.get("events", []) or []:
            action = ev.get("eventAction", "")
            date = (ev.get("eventDate", "") or "")[:10]
            if action and date:
                out.append(f"- {action.title()}: {date}")
        ns = [n.get("ldhName") for n in (d.get("nameservers") or []) if n.get("ldhName")]
        if ns:
            out.append(f"- Nameservers: {', '.join(ns)}")
        for ent in d.get("entities", []) or []:
            roles = ", ".join(ent.get("roles", []) or [])
            handle = ent.get("handle", "")
            if roles:
                out.append(f"- Entity ({roles}): {handle}")
    else:
        out.append(f"- Range: {d.get('startAddress','?')} – {d.get('endAddress','?')}")
        if d.get("name"):
            out.append(f"- Network name: {d['name']}")
        if d.get("country"):
            out.append(f"- Country: {d['country']}")
        if d.get("type"):
            out.append(f"- Type: {d['type']}")
        for ent in d.get("entities", []) or []:
            roles = ", ".join(ent.get("roles", []) or [])
            handle = ent.get("handle", "")
            if roles:
                out.append(f"- Entity ({roles}): {handle}")
    return "\n".join(out)


# ── DNS over HTTPS ───────────────────────────────────────────────────────────
async def _dns(domain: str, record_type: str) -> str:
    host = _clean_host(domain)
    rtype = (record_type or "A").upper().strip()
    if not host:
        return "Error: provide a domain."
    if rtype not in {"A", "AAAA", "MX", "NS", "TXT", "CNAME", "SOA", "CAA", "SRV", "PTR"}:
        return f"Error: unsupported record type '{rtype}'."
    try:
        async with _client() as c:
            r = await c.get(
                "https://cloudflare-dns.com/dns-query",
                params={"name": host, "type": rtype},
                headers={"Accept": "application/dns-json"},
            )
        d = r.json()
    except Exception as e:
        return f"DNS lookup failed for {host} {rtype}: {e}"
    answers = d.get("Answer") or []
    if not answers:
        return f"No {rtype} records for `{host}`."
    lines = [f"**DNS {rtype} — `{host}`**"]
    for a in answers:
        lines.append(f"- {a.get('data','')}  (TTL {a.get('TTL','?')})")
    return "\n".join(lines)


# ── IP geolocation / ASN ─────────────────────────────────────────────────────
async def _ip_info(ip: str) -> str:
    host = _clean_host(ip)
    if not host:
        return "Error: provide an IP address or hostname."
    fields = "status,message,query,country,regionName,city,zip,lat,lon,timezone,isp,org,as,asname,reverse,mobile,proxy,hosting"
    try:
        async with _client() as c:
            r = await c.get(f"http://ip-api.com/json/{host}", params={"fields": fields})
        d = r.json()
    except Exception as e:
        return f"IP lookup failed for {host}: {e}"
    if d.get("status") != "success":
        return f"IP lookup failed for `{host}`: {d.get('message','unknown')}"
    out = [f"**IP intel — `{d.get('query', host)}`**"]
    loc = ", ".join(x for x in [d.get("city"), d.get("regionName"), d.get("country")] if x)
    if loc:
        out.append(f"- Location: {loc} ({d.get('lat')}, {d.get('lon')})")
    if d.get("timezone"):
        out.append(f"- Timezone: {d['timezone']}")
    if d.get("isp"):
        out.append(f"- ISP: {d['isp']}")
    if d.get("org"):
        out.append(f"- Org: {d['org']}")
    if d.get("as"):
        out.append(f"- ASN: {d['as']}")
    if d.get("reverse"):
        out.append(f"- Reverse DNS: {d['reverse']}")
    flags = [k for k in ("mobile", "proxy", "hosting") if d.get(k)]
    if flags:
        out.append(f"- Flags: {', '.join(flags)}")
    return "\n".join(out)


# ── website fingerprint ──────────────────────────────────────────────────────
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_GENERATOR_RE = re.compile(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', re.I)


async def _website(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return "Error: provide a URL."
    if not re.match(r"^[a-zA-Z]+://", u):
        u = "https://" + u
    try:
        async with _client() as c:
            r = await c.get(u)
    except Exception as e:
        return f"Request to {u} failed: {e}"
    out = [f"**Website recon — {u}**"]
    if str(r.url) != u:
        chain = " → ".join(str(h.url) for h in r.history) + (" → " if r.history else "") + str(r.url)
        out.append(f"- Final URL: {r.url}")
        if r.history:
            out.append(f"- Redirects: {chain}")
    out.append(f"- Status: {r.status_code}")
    body = r.text or ""
    tm = _TITLE_RE.search(body)
    if tm:
        title = re.sub(r"\s+", " ", tm.group(1)).strip()[:150]
        out.append(f"- Title: {title}")
    interesting = ["server", "x-powered-by", "via", "cf-ray", "x-generator", "content-type", "strict-transport-security"]
    for h in interesting:
        if h in r.headers:
            out.append(f"- {h}: {r.headers[h][:120]}")
    gm = _GENERATOR_RE.search(body)
    if gm:
        out.append(f"- Generator (meta): {gm.group(1)[:80]}")
    # Light tech hints from headers/body
    hints = []
    hay = (body[:20000] + " " + " ".join(f"{k}:{v}" for k, v in r.headers.items())).lower()
    for name, needle in [
        ("Cloudflare", "cloudflare"), ("WordPress", "wp-content"), ("Shopify", "shopify"),
        ("Wix", "wix.com"), ("Squarespace", "squarespace"), ("Next.js", "__next"),
        ("React", "react"), ("nginx", "nginx"), ("Apache", "apache"),
    ]:
        if needle in hay:
            hints.append(name)
    if hints:
        out.append(f"- Tech hints: {', '.join(sorted(set(hints)))}")
    sec = [h for h in ("content-security-policy", "x-frame-options", "x-content-type-options") if h in r.headers]
    out.append(f"- Security headers present: {', '.join(sec) if sec else 'none'}")
    return "\n".join(out)


# ── crt.sh subdomain discovery ───────────────────────────────────────────────
async def _ct_crtsh(host: str) -> set:
    # crt.sh is slow for big domains; give it a long timeout, drop expired certs.
    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as c:
        r = await c.get("https://crt.sh/",
                        params={"q": f"%.{host}", "output": "json", "exclude": "expired"})
    if r.status_code != 200:
        raise RuntimeError(f"crt.sh HTTP {r.status_code}")
    names = set()
    for row in r.json():
        for n in str(row.get("name_value", "")).split("\n"):
            n = n.strip().lstrip("*.").lower()
            if n.endswith(host) and n:
                names.add(n)
    return names


async def _ct_certspotter(host: str) -> set:
    # Cert Spotter free issuances API (no key for basic use).
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as c:
        r = await c.get("https://api.certspotter.com/v1/issuances",
                        params={"domain": host, "include_subdomains": "true", "expand": "dns_names"})
    if r.status_code != 200:
        raise RuntimeError(f"certspotter HTTP {r.status_code}")
    names = set()
    for row in r.json():
        for n in row.get("dns_names", []) or []:
            n = n.strip().lstrip("*.").lower()
            if n.endswith(host) and n:
                names.add(n)
    return names


async def _subdomains(domain: str) -> str:
    host = _clean_host(domain)
    if not host or not _DOMAIN_RE.match(host):
        return "Error: provide a valid registrable domain, e.g. example.com."
    names, source, errors = set(), "", []
    for label, fn in [("crt.sh", _ct_crtsh), ("Cert Spotter", _ct_certspotter)]:
        try:
            names = await fn(host)
            source = label
            if names:
                break
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}")
    if not names:
        detail = f" ({'; '.join(errors)})" if errors else ""
        return (f"No certificate-transparency subdomains found for `{host}`, or the CT "
                f"sources were unreachable{detail}. These services are occasionally "
                f"overloaded — retry shortly.")
    ordered = sorted(names)
    shown = ordered[:80]
    out = [f"**Subdomains ({source}) — `{host}`** — {len(ordered)} unique"]
    out += [f"- {n}" for n in shown]
    if len(ordered) > len(shown):
        out.append(f"… and {len(ordered) - len(shown)} more")
    return "\n".join(out)


# ── username search across public profiles ───────────────────────────────────
# Sites whose profile URLs return a clean 200 (exists) / 404 (absent) to a plain
# GET, so a "200" is meaningful. Soft-404 sites (Telegram, Twitch, Steam,
# Replit, Pinterest — verified to return 200 for ANY handle) are deliberately
# excluded to avoid false positives. Some sites (GitLab, Reddit, Medium,
# HackerNews) sometimes answer 403/429 to bots; those are reported as
# inconclusive rather than as a hit.
_USERNAME_SITES = [
    ("GitHub", "https://api.github.com/users/{u}", "200"),
    ("GitLab", "https://gitlab.com/{u}", "200"),
    ("Reddit", "https://www.reddit.com/user/{u}/about.json", "200"),
    ("Keybase", "https://keybase.io/{u}", "200"),
    ("Medium", "https://medium.com/@{u}", "200"),
    ("Dev.to", "https://dev.to/{u}", "200"),
    ("SoundCloud", "https://soundcloud.com/{u}", "200"),
    ("GitHubGist", "https://gist.github.com/{u}", "200"),
    ("Gravatar", "https://gravatar.com/{u}", "200"),
    ("HackerNews", "https://news.ycombinator.com/user?id={u}", "200"),
    ("About.me", "https://about.me/{u}", "200"),
    ("DockerHub", "https://hub.docker.com/v2/users/{u}/", "200"),
    ("Chess.com", "https://api.chess.com/pub/player/{u}", "200"),
]


async def _check_site(c: httpx.AsyncClient, site: str, tmpl: str, username: str):
    url = tmpl.format(u=username)
    try:
        r = await c.get(url)
    except Exception:
        return (site, "?", url, "error")
    # HackerNews returns 200 with an "No such user." body for missing users.
    if site == "HackerNews" and r.status_code == 200 and "No such user" in (r.text or ""):
        return (site, "no", url, "no")
    verdict = "yes" if r.status_code == 200 else ("no" if r.status_code == 404 else "?")
    return (site, verdict, url, str(r.status_code))


async def _username(username: str) -> str:
    u = (username or "").strip().lstrip("@")
    if not u or not re.match(r"^[A-Za-z0-9._-]{1,40}$", u):
        return "Error: provide a plain username (letters, digits, . _ - )."
    async with _client() as c:
        results = await asyncio.gather(*[_check_site(c, s, t, u) for s, t, p in _USERNAME_SITES])
    found = [(s, url) for s, v, url, _ in results if v == "yes"]
    unknown = [(s, code) for s, v, url, code in results if v == "?"]
    out = [f"**Username search — `{u}`** (public profile pages; a hit is not proof of the same person)"]
    if found:
        out.append(f"\n**Likely present ({len(found)}):**")
        out += [f"- {s}: {url}" for s, url in found]
    else:
        out.append("\nNo clear matches on the checked sites.")
    if unknown:
        out.append(f"\n_Inconclusive (blocked / rate-limited / login-walled): {', '.join(s for s, _ in unknown)}_")
    return "\n".join(out)


# ── email recon ──────────────────────────────────────────────────────────────
async def _email(email: str) -> str:
    e = (email or "").strip().lower()
    if not _EMAIL_RE.match(e):
        return "Error: provide a valid email address."
    local, domain = e.rsplit("@", 1)
    out = [f"**Email recon — `{e}`**", "- Syntax: valid"]
    # MX (deliverability signal)
    try:
        async with _client() as c:
            r = await c.get(
                "https://cloudflare-dns.com/dns-query",
                params={"name": domain, "type": "MX"},
                headers={"Accept": "application/dns-json"},
            )
        mx = [a.get("data", "") for a in (r.json().get("Answer") or [])]
        if mx:
            out.append(f"- MX records ({len(mx)}): {', '.join(m.split()[-1] for m in mx[:5])}")
            out.append("- Domain can receive mail (has MX).")
        else:
            out.append("- No MX records — domain likely cannot receive mail.")
    except Exception as ex:
        out.append(f"- MX check failed: {ex}")
    # Gravatar (many accounts have one; confirms the email is used somewhere)
    h = hashlib.md5(e.encode()).hexdigest()
    try:
        async with _client() as c:
            g = await c.get(f"https://www.gravatar.com/avatar/{h}?d=404")
        if g.status_code == 200:
            out.append(f"- Gravatar: exists → https://www.gravatar.com/avatar/{h}")
            out.append(f"- Gravatar profile: https://gravatar.com/{h}")
        else:
            out.append("- Gravatar: none")
    except Exception:
        out.append("- Gravatar: check failed")
    out.append("- Breach check: use Have I Been Pwned (haveibeenpwned.com) — requires an API key, not queried here.")
    return "\n".join(out)


# ── password breach check (Pwned Passwords, free k-anonymity) ────────────────
async def _password_check(password: str) -> str:
    pw = password or ""
    if not pw:
        return "Error: provide a password to check."
    # k-anonymity: send only the first 5 chars of the SHA-1, never the password.
    sha1 = hashlib.sha1(pw.encode("utf-8")).hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]
    try:
        async with _client() as c:
            r = await c.get(f"https://api.pwnedpasswords.com/range/{prefix}",
                            headers={"Add-Padding": "true"})
        if r.status_code != 200:
            return f"Pwned Passwords returned HTTP {r.status_code} — try again shortly."
    except Exception as e:
        return f"Password breach check failed: {e}"
    count = 0
    for line in r.text.splitlines():
        parts = line.split(":")
        if len(parts) == 2 and parts[0].strip().upper() == suffix and parts[1].strip() != "0":
            count = int(parts[1])
            break
    if count:
        return (f"**Password breach check** — ⚠️ this password has appeared in **{count:,}** known "
                f"data breaches. It is unsafe; do not use it anywhere.\n"
                f"(Checked via Have I Been Pwned's k-anonymity API — the password itself was never sent.)")
    return ("**Password breach check** — ✅ this exact password was not found in Have I Been Pwned's "
            "breach corpus. (Absence isn't a guarantee of safety, but it hasn't leaked in known dumps.)")


# ── email breach check (HIBP, requires an API key) ───────────────────────────
async def _breach(email: str) -> str:
    e = (email or "").strip().lower()
    if not _EMAIL_RE.match(e):
        return "Error: provide a valid email address."
    key = os.environ.get("HIBP_API_KEY", "").strip()
    if not key:
        return ("Email→breach lookup needs a Have I Been Pwned API key (paid, ~$4/mo from "
                "haveibeenpwned.com/API/Key). Set it as the `HIBP_API_KEY` environment variable, "
                "then restart Aegis.\n\n"
                "Note: no legitimate service returns the *password* for an email — that only exists in "
                "paid/gray-market combo-list services. To test whether a specific password has leaked, "
                "use `osint_password_check` (free) instead.")
    try:
        async with _client() as c:
            r = await c.get(
                f"https://haveibeenpwned.com/api/v3/breachedaccount/{e}",
                params={"truncateResponse": "false"},
                headers={"hibp-api-key": key, "User-Agent": "Aegis-OSINT"},
            )
    except Exception as ex:
        return f"HIBP lookup failed: {ex}"
    if r.status_code == 404:
        return f"**Breach check — {e}** — ✅ not found in any known breach (per HIBP)."
    if r.status_code == 401:
        return "HIBP rejected the API key (401). Check HIBP_API_KEY."
    if r.status_code == 429:
        return "HIBP rate limit hit (429) — wait a moment and retry."
    if r.status_code != 200:
        return f"HIBP returned HTTP {r.status_code}."
    try:
        breaches = r.json()
    except Exception:
        return "HIBP returned an unexpected response."
    out = [f"**Breach check — {e}** — ⚠️ found in **{len(breaches)}** breach(es):"]
    for b in breaches[:25]:
        name = b.get("Name") or b.get("Title", "?")
        date = (b.get("BreachDate") or "")[:10]
        data = ", ".join(b.get("DataClasses", [])[:6])
        out.append(f"- **{name}** ({date}) — exposed: {data}")
    if len(breaches) > 25:
        out.append(f"… and {len(breaches) - 25} more")
    return "\n".join(out)


# ── phone number ─────────────────────────────────────────────────────────────
async def _phone(number: str, region: str = "") -> str:
    try:
        import phonenumbers
        from phonenumbers import carrier, geocoder, timezone as pn_tz
    except Exception:
        return ("Phone parsing needs the `phonenumbers` package. Install it with "
                "`pip install phonenumbers` (Apache-2.0), then restart Aegis.")
    num = (number or "").strip()
    if not num:
        return "Error: provide a phone number (ideally in +E.164 form, e.g. +14155552671)."
    try:
        parsed = phonenumbers.parse(num, (region or None) if not num.startswith("+") else None)
    except Exception as e:
        return f"Could not parse '{num}': {e}. Include the country code (e.g. +1…) or pass a region."
    valid = phonenumbers.is_valid_number(parsed)
    ntype = phonenumbers.number_type(parsed)
    type_names = {
        0: "fixed line", 1: "mobile", 2: "fixed line or mobile", 3: "toll free",
        4: "premium rate", 5: "shared cost", 6: "VoIP", 7: "personal number",
        8: "pager", 9: "UAN", 10: "voicemail", 27: "unknown",
    }
    out = [f"**Phone intel — {phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)}**"]
    out.append(f"- Valid: {'yes' if valid else 'no'}")
    out.append(f"- Country code: +{parsed.country_code}")
    loc = geocoder.description_for_number(parsed, "en")
    if loc:
        out.append(f"- Region: {loc}")
    car = carrier.name_for_number(parsed, "en")
    if car:
        out.append(f"- Carrier: {car}")
    out.append(f"- Line type: {type_names.get(ntype, 'unknown')}")
    tzs = pn_tz.time_zones_for_number(parsed)
    if tzs:
        out.append(f"- Timezone(s): {', '.join(tzs)}")
    out.append(f"- International format: {phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)}")
    return "\n".join(out)


# ── MCP wiring ───────────────────────────────────────────────────────────────
@server.list_tools()
async def list_tools() -> list[Tool]:
    def _obj(props, required):
        return {"type": "object", "properties": props, "required": required}
    return [
        Tool(name="osint_whois", description=(
            "WHOIS/RDAP registration data for a DOMAIN or IP: registrar, creation/expiry "
            "dates, status, nameservers, and organization. Use when investigating who owns "
            "a domain or IP."),
            inputSchema=_obj({"target": {"type": "string", "description": "Domain (example.com) or IP address"}}, ["target"])),
        Tool(name="osint_dns", description=(
            "DNS records for a domain (A, AAAA, MX, NS, TXT, CNAME, SOA, CAA, SRV) via "
            "DNS-over-HTTPS. Use to map a domain's infrastructure and mail/verification records."),
            inputSchema=_obj({
                "domain": {"type": "string", "description": "Domain name"},
                "record_type": {"type": "string", "description": "Record type (default A)"},
            }, ["domain"])),
        Tool(name="osint_ip_info", description=(
            "Geolocation, ISP, organization, ASN, reverse DNS and hosting/proxy flags for an "
            "IP address (or hostname). Use to locate and attribute an IP."),
            inputSchema=_obj({"ip": {"type": "string", "description": "IP address or hostname"}}, ["ip"])),
        Tool(name="osint_website", description=(
            "Fingerprint a WEBSITE: follow redirects, report status, server/security headers, "
            "page title, and technology hints (CMS/framework/CDN). Use to profile a site."),
            inputSchema=_obj({"url": {"type": "string", "description": "Website URL or domain"}}, ["url"])),
        Tool(name="osint_subdomains", description=(
            "Discover subdomains of a domain from certificate-transparency logs (crt.sh). "
            "Use to map an organization's external footprint."),
            inputSchema=_obj({"domain": {"type": "string", "description": "Registrable domain, e.g. example.com"}}, ["domain"])),
        Tool(name="osint_username", description=(
            "Search a USERNAME across common public profile sites (GitHub, Reddit, Keybase, "
            "Telegram, Steam, etc.) and report where a matching profile likely exists. Use "
            "when investigating a person by handle. A hit is not proof of identity."),
            inputSchema=_obj({"username": {"type": "string", "description": "Username / handle (no @)"}}, ["username"])),
        Tool(name="osint_email", description=(
            "Recon an EMAIL address: syntax validity, domain MX (can it receive mail), and "
            "Gravatar presence. Use when investigating an email. For breach history use osint_breach."),
            inputSchema=_obj({"email": {"type": "string", "description": "Email address"}}, ["email"])),
        Tool(name="osint_breach", description=(
            "Check whether an EMAIL appears in known data breaches, and what data was exposed, via "
            "Have I Been Pwned. Requires a HIBP API key (env HIBP_API_KEY); says so if absent."),
            inputSchema=_obj({"email": {"type": "string", "description": "Email address"}}, ["email"])),
        Tool(name="osint_password_check", description=(
            "Check whether a PASSWORD has appeared in known breaches (Have I Been Pwned Pwned "
            "Passwords, free). Uses k-anonymity — only a partial hash is sent, never the password. "
            "Use to test password safety."),
            inputSchema=_obj({"password": {"type": "string", "description": "The password to check"}}, ["password"])),
        Tool(name="osint_phone", description=(
            "Parse a PHONE NUMBER: validity, country, region/area, carrier, line type "
            "(mobile/landline/VoIP) and timezone. Provide the number in +E.164 form, or pass "
            "a 2-letter region for national-format numbers."),
            inputSchema=_obj({
                "number": {"type": "string", "description": "Phone number, ideally +countrycode…"},
                "region": {"type": "string", "description": "2-letter region (e.g. US) if no + prefix"},
            }, ["number"])),
    ]


_DISPATCH = {
    "osint_whois": lambda a: _whois(a.get("target", "")),
    "osint_dns": lambda a: _dns(a.get("domain", ""), a.get("record_type", "A")),
    "osint_ip_info": lambda a: _ip_info(a.get("ip", "")),
    "osint_website": lambda a: _website(a.get("url", "")),
    "osint_subdomains": lambda a: _subdomains(a.get("domain", "")),
    "osint_username": lambda a: _username(a.get("username", "")),
    "osint_email": lambda a: _email(a.get("email", "")),
    "osint_breach": lambda a: _breach(a.get("email", "")),
    "osint_password_check": lambda a: _password_check(a.get("password", "")),
    "osint_phone": lambda a: _phone(a.get("number", ""), a.get("region", "")),
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
