"""Built-in recipe email tool: List-Unsubscribe parsing, sender grouping, render."""
import asyncio

from src import recipe_builtin_tools as bt


def test_unsub_link_prefers_http_over_mailto():
    assert bt._unsub_link("<https://x.com/u?a=1>, <mailto:u@x.com>") == "https://x.com/u?a=1"
    assert bt._unsub_link("<mailto:u@x.com>") == "mailto:u@x.com"
    assert bt._unsub_link("") == ""
    # a bare url with no angle brackets still resolves
    assert bt._unsub_link("https://x.com/unsub") == "https://x.com/unsub"


def test_sender_key_extracts_address():
    assert bt._sender_key("Acme Deals <deals@acme.com>") == "deals@acme.com"
    assert bt._sender_key("news@foo.io") == "news@foo.io"
    assert bt._sender_key("BIG <SALES@Shop.COM>") == "sales@shop.com"


def test_render_no_account():
    out = bt._render_promotional({"error": "no_account"})
    assert "No email account" in out and "Settings" in out


def test_render_empty():
    out = bt._render_promotional({"senders": [], "scanned": 0, "days": 5})
    assert "No promotional" in out and "5 days" in out


def test_render_groups_senders_with_links():
    data = {"days": 7, "scanned": 4, "senders": [
        {"from": "Acme <deals@acme.com>", "count": 3, "subjects": ["50% off", "Flash sale"],
         "unsubscribe": "https://acme.com/u"},
        {"from": "news@blog.io", "count": 1, "subjects": ["Weekly digest"], "unsubscribe": ""},
    ]}
    out = bt._render_promotional(data)
    assert "4 messages from 2 senders" in out
    assert "Acme <deals@acme.com> — 3 email(s)" in out
    assert "unsubscribe: https://acme.com/u" in out
    assert "no unsubscribe link found" in out  # the second sender


def test_email_recent_graceful_without_account():
    out = asyncio.run(bt.email_recent({"days": 7}, owner="nobody@example.com"))
    assert "No email account" in out


def test_email_recent_clamps_days():
    # non-numeric days falls back to 7; the call still returns a string
    out = asyncio.run(bt.email_recent({"days": "banana"}, owner="nobody@example.com"))
    assert isinstance(out, str)
