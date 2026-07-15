"""Built-in recipe tools — in-process tool nodes that run with the recipe's
owner, for data the MCP toolbox subprocesses can't reach (like the user's
mailbox). Registered in BUILTIN_RECIPE_TOOLS; the recipe engine checks this
before falling back to MCP (see src/recipes._run_tool_node).

Each tool is ``async def(args: dict, owner: str|None) -> str`` and returns text
the downstream model node reads as context.
"""

import asyncio
import email
import logging
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_HDR_FIELDS = "FROM SUBJECT DATE LIST-UNSUBSCRIBE PRECEDENCE LIST-ID"
_MAX_FETCH = 250  # cap the header scan on large inboxes


def _decode(raw: Optional[str]) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _unsub_link(list_unsub: str) -> str:
    """The preferred unsubscribe target from a List-Unsubscribe header
    (an https link if present, else a mailto:)."""
    if not list_unsub:
        return ""
    links = re.findall(r"<([^>]+)>", list_unsub) or [list_unsub]
    http = next((l for l in links if l.lower().startswith("http")), "")
    mailto = next((l for l in links if l.lower().startswith("mailto:")), "")
    return http or mailto


def _sender_key(from_hdr: str) -> str:
    m = re.search(r"<([^>]+)>", from_hdr)
    addr = (m.group(1) if m else from_hdr).strip().lower()
    return addr


def _fetch_promotional(owner: Optional[str], days: int) -> Dict[str, Any]:
    """Sync IMAP scan: recent bulk/promotional mail grouped by sender.

    "Promotional" = carries a List-Unsubscribe header (the reliable bulk-mail
    signal), or Precedence: bulk/list, or a List-Id. Returns a structured dict
    or {"error": ...} when no mailbox is configured / reachable.
    """
    from routes.email_helpers import _get_email_config, _imap

    cfg = _get_email_config(owner=owner or "")
    if not cfg or not cfg.get("imap_host"):
        return {"error": "no_account"}

    since = (datetime.utcnow() - timedelta(days=max(1, days))).strftime("%d-%b-%Y")
    senders: Dict[str, Dict[str, Any]] = {}
    scanned = 0
    with _imap(owner=owner or "") as conn:
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, f"(SINCE {since})")
        if typ != "OK" or not data or not data[0]:
            return {"senders": [], "scanned": 0, "days": days}
        seqs = data[0].split()
        seqs = seqs[-_MAX_FETCH:]  # newest window
        # Batch-fetch just the headers we need.
        fetch_set = b",".join(seqs)
        typ, msgs = conn.fetch(fetch_set, f"(BODY.PEEK[HEADER.FIELDS ({_HDR_FIELDS})])")
        for part in msgs or []:
            if not isinstance(part, tuple) or len(part) < 2:
                continue
            hdr = email.message_from_bytes(part[1])
            list_unsub = hdr.get("List-Unsubscribe", "")
            precedence = (hdr.get("Precedence", "") or "").lower()
            list_id = hdr.get("List-Id", "")
            is_promo = bool(list_unsub) or precedence in ("bulk", "list") or bool(list_id)
            if not is_promo:
                continue
            scanned += 1
            from_hdr = _decode(hdr.get("From", ""))
            key = _sender_key(from_hdr)
            rec = senders.setdefault(key, {
                "from": from_hdr, "count": 0, "subjects": [],
                "unsubscribe": _unsub_link(list_unsub),
            })
            rec["count"] += 1
            subj = _decode(hdr.get("Subject", ""))
            if subj and len(rec["subjects"]) < 3:
                rec["subjects"].append(subj)
            if not rec["unsubscribe"]:
                rec["unsubscribe"] = _unsub_link(list_unsub)

    ranked = sorted(senders.values(), key=lambda s: s["count"], reverse=True)
    return {"senders": ranked, "scanned": scanned, "days": days}


def _render_promotional(data: Dict[str, Any]) -> str:
    if data.get("error") == "no_account":
        return ("[No email account is connected. Add one in Settings → Email, then this "
                "automation can scan your inbox.]")
    senders = data.get("senders") or []
    if not senders:
        return f"No promotional or newsletter mail found in the last {data.get('days', 7)} days."
    lines = [f"Promotional / newsletter mail in the last {data.get('days', 7)} days "
             f"— {data.get('scanned', 0)} messages from {len(senders)} senders:\n"]
    for s in senders:
        unsub = f"  unsubscribe: {s['unsubscribe']}" if s.get("unsubscribe") else "  (no unsubscribe link found)"
        subs = "; ".join(s.get("subjects") or [])
        lines.append(f"- {s['from']} — {s['count']} email(s)\n  recent: {subs}\n{unsub}")
    return "\n".join(lines)


async def email_recent(args: Dict[str, Any], owner: Optional[str]) -> str:
    """Recent promotional/newsletter mail grouped by sender, with unsubscribe
    links. args: {days?: int}."""
    try:
        days = int(args.get("days") or 7)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(30, days))
    try:
        data = await asyncio.to_thread(_fetch_promotional, owner, days)
    except Exception as e:
        logger.warning("email_recent failed: %s", e)
        return f"[Could not read the inbox: {e}]"
    return _render_promotional(data)


BUILTIN_RECIPE_TOOLS = {
    "email_recent": email_recent,
}


def builtin_tool_names() -> List[str]:
    return list(BUILTIN_RECIPE_TOOLS.keys())
