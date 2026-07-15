"""
recipe_templates.py — the canned-recipe catalog.

Every entry is a ready-to-run Recipe with human metadata (category, a plain
description, a sample input, and what comes back) plus a graph builder bound to
the best available model. The catalog is what the Recipes *library* renders —
one-click runnable cards, not a blank node canvas.

Toolbox-gated recipes (stock analysis, OSINT, site health, web) still appear in
the library when their toolbox is off; the catalog marks them `available: False`
with a `needs` entry so the UI can offer a one-click "enable these tools" rather
than hiding them. `preview: True` recipes (inbox declutter, morning brief) are
shown but not yet runnable — they arrive with Automations (schedules + email
triggers) in the next phase.

Pure and deterministic (no I/O) so it's unit-testable; the route layer feeds it
the available models + connected toolboxes and persists/runs whatever it returns.
Every graph is shaped to pass ``recipes.validate_recipe`` unchanged.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Set

# ── model ranking (mirrors static/js/recipes/index.js _rankModel) ─────────────
def rank_model(name: str) -> int:
    s = (name or "").lower()
    if re.search(r"image|video|diffusion|vision|-vl\b|embed|whisper|tts|"
                 r"\bwan[0-9.]|\bltx|flux|sdxl|stable-diffusion", s):
        return 0
    if re.search(r"qwen|firefunction|command-?r|hermes", s):
        return 5
    if re.search(r"gemma-?([3-9]|1[0-9])|gemma4", s):
        return 5
    if re.search(r"llama-?[34]|mistral|mixtral", s):
        return 4
    if re.search(r"phi-?[3-9]|granite|nemotron", s):
        return 3
    if re.search(r"llama-?pro|llama-?2|vicuna|alpaca|orca", s):
        return 1
    return 2


def best_model(models: List[str]) -> Optional[str]:
    best: Optional[str] = None
    for m in models or []:
        if rank_model(m) <= 0:
            continue
        if best is None or rank_model(m) > rank_model(best):
            best = m
    return best


# ── toolbox metadata ──────────────────────────────────────────────────────────
# Which toolbox a recipe needs → the label the UI shows in "Enable X tools".
TOOLBOX_LABELS: Dict[str, str] = {
    "market": "Market Analysis",
    "osint": "OSINT",
    "troubleshoot": "Troubleshooting",
    "web": "Web",
}


# ── graph helpers ─────────────────────────────────────────────────────────────
def _node(nid: str, ntype: str, x: int, y: int, **config: Any) -> Dict[str, Any]:
    return {"id": nid, "type": ntype, "x": x, "y": y, "config": config}


def _edge(a: str, b: str) -> Dict[str, str]:
    return {"from": a, "to": b}


def _graph(nodes: List[Dict], edges: List[Dict]) -> Dict[str, Any]:
    return {"nodes": nodes, "edges": edges}


def _linear(model: str, prompt: str, input_label: str = "Text") -> Dict[str, Any]:
    """input → model(prompt) → output — the shape most canned recipes use."""
    nodes = [
        _node("n1", "input", 60, 160, label=input_label),
        _node("n2", "model", 380, 160, model=model, prompt=prompt),
        _node("n3", "output", 720, 160),
    ]
    return _graph(nodes, [_edge("n1", "n2"), _edge("n2", "n3")])


def _fanin(model: str, tool_specs: List, model_prompt: str) -> Dict[str, Any]:
    """input → each tool → model → output. tool_specs = [(tool_name, args, y)].
    The model prompt carries no {{ref}}, so the engine auto-prepends each tool
    node's output as context."""
    nodes: List[Dict] = [_node("n1", "input", 60, 170, label="Input")]
    edges: List[Dict] = []
    seq = 2
    parents: List[str] = []
    for tname, args, y in tool_specs:
        nid = f"n{seq}"; seq += 1
        nodes.append(_node(nid, "tool", 340, y, tool=tname, args=args))
        edges.append(_edge("n1", nid))
        parents.append(nid)
    mid = f"n{seq}"; seq += 1
    nodes.append(_node(mid, "model", 660, 170, model=model, prompt=model_prompt))
    for p in parents:
        edges.append(_edge(p, mid))
    oid = f"n{seq}"; seq += 1
    nodes.append(_node(oid, "output", 980, 170))
    edges.append(_edge(mid, oid))
    return _graph(nodes, edges)


# ── graph builders (model) → {nodes, edges} ───────────────────────────────────
def _b_summarize(model: str) -> Dict[str, Any]:
    return _linear(model,
        "Summarize the text below into 5 crisp bullet points, then a one-line "
        "takeaway.\n\n{{input}}", "Text")


def _b_refine(model: str) -> Dict[str, Any]:
    # The loop node carries NO prompt so the engine's built-in refine prompt
    # (which uses the {{prev}} loop variable) drives it.
    nodes = [
        _node("n1", "input", 60, 160, label="Ask"),
        _node("n2", "model", 340, 160, model=model,
              prompt="Write a first draft that answers the request below.\n\n{{input}}"),
        _node("n3", "loop", 620, 160, model=model, max_iters=2, until={"kind": "", "value": ""}),
        _node("n4", "output", 900, 160),
    ]
    return _graph(nodes, [_edge("n1", "n2"), _edge("n2", "n3"), _edge("n3", "n4")])


def _b_triage(model: str) -> Dict[str, Any]:
    nodes = [
        _node("n1", "input", 40, 170, label="Message"),
        _node("n2", "model", 300, 170, model=model, label="Classify",
              prompt="Classify the message below. Put URGENT or ROUTINE on the first line, "
                     "then one line on why.\n\n{{input}}"),
        _node("n3", "output", 600, 70, label="Classification"),
        _node("n4", "branch", 560, 290, condition={"kind": "contains", "value": "URGENT"}),
        _node("n5", "model", 830, 290, model=model, label="Draft reply",
              prompt="This message was flagged urgent. Draft a brief, calm reply that "
                     "acknowledges it and states the immediate next step."),
        _node("n6", "output", 1110, 290, label="Urgent draft"),
    ]
    edges = [
        _edge("n1", "n2"), _edge("n2", "n3"),
        _edge("n2", "n4"), _edge("n4", "n5"), _edge("n5", "n6"),
    ]
    return _graph(nodes, edges)


def _b_meeting(model: str) -> Dict[str, Any]:
    return _linear(model,
        "These are meeting notes or a transcript. Extract three sections:\n"
        "**Decisions** — what was agreed.\n**Action items** — each as `owner — task "
        "— due (if stated)`.\n**Open questions** — anything unresolved.\n\n{{input}}",
        "Notes / transcript")


def _b_doc_brief(model: str) -> Dict[str, Any]:
    return _linear(model,
        "Turn the document below into a structured brief with these headers: "
        "**Overview** (2 sentences), **Key points** (bullets), **Risks or caveats**, "
        "**Recommended next step**.\n\n{{input}}",
        "Document text")


def _b_compare(model: str) -> Dict[str, Any]:
    return _linear(model,
        "Compare the two options described below. Give a short side-by-side on the "
        "dimensions that matter, then a clear recommendation with the single biggest "
        "reason. If a dimension is unknown, say so rather than guessing.\n\n{{input}}",
        "Two options (A vs B)")


def _b_translate(model: str) -> Dict[str, Any]:
    return _linear(model,
        "Detect the language of the text below and translate it to fluent English "
        "(if it's already English, translate to Spanish). Then add a short "
        "**Notes** section explaining any idioms, tone, or cultural context a literal "
        "translation would miss.\n\n{{input}}",
        "Text in any language")


def _b_stock(model: str) -> Dict[str, Any]:
    """The bull / base / bear debate: market data → three investor personas →
    a portfolio-manager verdict. Needs the Market toolbox."""
    nodes: List[Dict] = [_node("n1", "input", 40, 230, label="Ticker")]
    edges: List[Dict] = []
    data = [("market_analyze", {"symbol": "{{input}}"}, 110),
            ("market_fundamentals", {"symbol": "{{input}}"}, 350)]
    seq = 2
    data_ids: List[str] = []
    for tname, args, y in data:
        nid = f"n{seq}"; seq += 1
        nodes.append(_node(nid, "tool", 280, y, tool=tname, args=args))
        edges.append(_edge("n1", nid))
        data_ids.append(nid)
    personas = [
        ("Bull — value", 110,
         "You are a disciplined value investor (Buffett/Graham). Using the market data above, "
         "judge whether this is a quality business at a fair price — cite valuation (P/E, P/B, FCF), "
         "margins, and balance-sheet health. End with your call: BULLISH, BEARISH, or NEUTRAL, and "
         "a one-line reason."),
        ("Base — growth", 230,
         "You are a growth investor (Wood/Lynch). Using the market data above, judge the growth "
         "story: revenue/earnings growth, momentum, and narrative, weighed against the price paid. "
         "End with your call: BULLISH, BEARISH, or NEUTRAL, and a one-line reason."),
        ("Bear — contrarian", 350,
         "You are a contrarian risk manager (Burry/Taleb). Using the market data above, stress-test "
         "the bull case: overvaluation, debt, volatility, crowding, and tail risks. End with your "
         "call: BULLISH, BEARISH, or NEUTRAL, and a one-line reason."),
    ]
    persona_ids: List[str] = []
    for label, y, prompt in personas:
        nid = f"n{seq}"; seq += 1
        nodes.append(_node(nid, "model", 560, y, model=model, label=label, prompt=prompt))
        for d in data_ids:
            edges.append(_edge(d, nid))
        persona_ids.append(nid)
    pm = f"n{seq}"; seq += 1
    nodes.append(_node(pm, "model", 860, 230, model=model, label="Portfolio manager",
                       prompt="You are the portfolio manager. Three analysts gave their views above "
                              "(bull, base, bear). Weigh them, note where they agree and disagree, and "
                              "give a final call — BUY, HOLD, or SELL — with a confidence "
                              "(low/medium/high) and a two-sentence rationale. This is educational "
                              "analysis, not investment advice."))
    for pid in persona_ids:
        edges.append(_edge(pid, pm))
    oid = f"n{seq}"; seq += 1
    nodes.append(_node(oid, "output", 1140, 230))
    edges.append(_edge(pm, oid))
    return _graph(nodes, edges)


def _b_domain(model: str) -> Dict[str, Any]:
    return _fanin(model, [
        ("osint_whois", {"target": "{{input}}"}, 60),
        ("osint_dns", {"domain": "{{input}}", "record_type": "A"}, 180),
        ("osint_website", {"url": "{{input}}"}, 300),
    ], "Using the recon above, write a short risk brief on this domain: who owns it, its "
       "infrastructure and mail setup, and anything notable or suspicious.")


def _b_site_health(model: str) -> Dict[str, Any]:
    return _fanin(model, [
        ("ts_http_check", {"url": "{{input}}"}, 60),
        ("ts_dns_diagnose", {"domain": "{{input}}"}, 180),
        ("ts_tls_cert", {"host": "{{input}}"}, 300),
    ], "Based on the checks above, say whether the site looks healthy. Call out any errors, DNS "
       "problems, or a soon-to-expire TLS certificate, and suggest the likely fix.")


def _b_web_brief(model: str) -> Dict[str, Any]:
    return _fanin(model, [("web_extract", {"url": "{{input}}"}, 170)],
        "Summarize the page content above into a short brief: what it is, the key points, and any "
        "action items or links worth following.")


# ── catalog ───────────────────────────────────────────────────────────────────
# Order: model-only (simple → rich), then toolbox-gated, then previews.
_CATALOG: List[Dict[str, Any]] = [
    {"id": "summarize", "name": "Summarize & extract", "category": "Analyze",
     "description": "Any text in → 5 crisp bullets and a one-line takeaway. The simplest recipe.",
     "sample_input": "Paste an article, email thread, or meeting notes here.",
     "expected_output": "Five bullet points and a takeaway line.",
     "needs_toolbox": None, "builder": _b_summarize},
    {"id": "meeting_notes", "name": "Meeting notes → actions", "category": "Communicate",
     "description": "Turn raw notes or a transcript into decisions, owned action items, and open questions.",
     "sample_input": "Standup: Ana ships the API by Fri, Ben blocked on creds, we agreed to cut the CSV export…",
     "expected_output": "Decisions, action items (owner — task — due), and open questions.",
     "needs_toolbox": None, "builder": _b_meeting},
    {"id": "doc_brief", "name": "Document → brief", "category": "Analyze",
     "description": "A long document (paste the text) → a structured brief: overview, key points, risks, next step.",
     "sample_input": "Paste the full text of a report, spec, or contract.",
     "expected_output": "Overview, key points, risks/caveats, recommended next step.",
     "needs_toolbox": None, "builder": _b_doc_brief},
    {"id": "compare", "name": "Compare two options", "category": "Analyze",
     "description": "Describe two choices → a side-by-side on what matters plus a clear recommendation.",
     "sample_input": "Option A: hosted Postgres at $50/mo, managed backups. Option B: self-host on the existing box, free but I maintain it.",
     "expected_output": "A dimension-by-dimension comparison and a recommendation with the biggest reason.",
     "needs_toolbox": None, "builder": _b_compare},
    {"id": "translate", "name": "Translate & explain", "category": "Communicate",
     "description": "Translate text in any language, then explain idioms and context a literal version would miss.",
     "sample_input": "Paste text in any language.",
     "expected_output": "A fluent translation plus notes on idioms, tone, and context.",
     "needs_toolbox": None, "builder": _b_translate},
    {"id": "draft_refine", "name": "Draft & self-refine", "category": "Create",
     "description": "The model drafts an answer, then a Loop node improves it a couple of passes before output.",
     "sample_input": "Write a short, friendly email introducing a new self-hosted AI workspace.",
     "expected_output": "A polished draft that's been refined twice.",
     "needs_toolbox": None, "builder": _b_refine},
    {"id": "triage", "name": "Urgent triage & draft", "category": "Communicate",
     "description": "Classify a message URGENT/ROUTINE; only if it's urgent does a Branch flow on to draft a reply.",
     "sample_input": "Hi — the front door lock jammed and I'm locked out, can someone help right now?",
     "expected_output": "A classification, plus a drafted reply when the message is urgent.",
     "needs_toolbox": None, "builder": _b_triage},

    {"id": "stock", "name": "Stock analysis — Bull / Base / Bear", "category": "Analyze",
     "description": "Market data → value, growth, and contrarian analysts debate a ticker → a portfolio-manager verdict.",
     "sample_input": "NVDA",
     "expected_output": "Three analyst takes (bull/base/bear) and a BUY/HOLD/SELL call with confidence.",
     "needs_toolbox": "market", "builder": _b_stock},
    {"id": "domain", "name": "Domain dossier", "category": "Research",
     "description": "OSINT recon on a domain — ownership, DNS, infrastructure — rolled into a short risk brief.",
     "sample_input": "example.com",
     "expected_output": "A risk brief covering ownership, infrastructure, and mail setup.",
     "needs_toolbox": "osint", "builder": _b_domain},
    {"id": "site_health", "name": "Site health check", "category": "Monitor",
     "description": "Is this site up? HTTP + DNS + TLS checks → a plain-English diagnosis and likely fix.",
     "sample_input": "example.com",
     "expected_output": "A health verdict with any errors, DNS issues, or expiring certs called out.",
     "needs_toolbox": "troubleshoot", "builder": _b_site_health},
    {"id": "web_brief", "name": "Web page brief", "category": "Research",
     "description": "Fetch a URL and summarize the page into a short brief with any action items.",
     "sample_input": "https://example.com",
     "expected_output": "A brief: what the page is, key points, and links worth following.",
     "needs_toolbox": "web", "builder": _b_web_brief},

    # Previews — visible in the library, runnable once Automations lands (Phase 2).
    {"id": "inbox_declutter", "name": "Inbox declutter", "category": "Monitor",
     "description": "Daily: digest your promotional and newsletter mail, and queue up who to unsubscribe from — you approve each one.",
     "sample_input": "Runs on a schedule against your inbox — no input needed.",
     "expected_output": "A daily digest of bulk senders with one-click unsubscribes to approve.",
     "needs_toolbox": None, "builder": None, "preview": True,
     "preview_note": "Arrives with Automations — needs email access and a daily schedule."},
    {"id": "morning_brief", "name": "Morning brief", "category": "Monitor",
     "description": "Daily: your watched tickers and saved topics pulled into one short digest, waiting when you wake up.",
     "sample_input": "Runs on a schedule against your saved tickers and topics.",
     "expected_output": "A single morning digest across your markets and topics.",
     "needs_toolbox": None, "builder": None, "preview": True,
     "preview_note": "Arrives with Automations — needs a daily schedule (and the Market/Web tools)."},
]

_BY_ID: Dict[str, Dict[str, Any]] = {t["id"]: t for t in _CATALOG}


def _needs_info(needs: Optional[str], connected: Set[str]) -> List[Dict[str, Any]]:
    if not needs:
        return []
    return [{"toolbox": needs, "label": TOOLBOX_LABELS.get(needs, needs),
             "enabled": needs in connected}]


def catalog(models: List[str], connected_toolboxes) -> List[Dict[str, Any]]:
    """The full library: every canned recipe with metadata + availability.

    `connected_toolboxes` is the set of toolbox ids currently connected. A
    recipe stays in the list even when its toolbox is off (available=False,
    with a `needs` entry) so the UI can offer a one-click enable.
    """
    model = best_model(models or [])
    connected: Set[str] = set(connected_toolboxes or [])
    out: List[Dict[str, Any]] = []
    for meta in _CATALOG:
        needs = meta.get("needs_toolbox")
        preview = bool(meta.get("preview"))
        tb_ok = (needs is None) or (needs in connected)
        available = bool(model) and tb_ok and not preview
        entry: Dict[str, Any] = {
            "id": meta["id"], "name": meta["name"], "category": meta["category"],
            "description": meta["description"], "sample_input": meta["sample_input"],
            "expected_output": meta["expected_output"],
            "preview": preview, "available": available, "has_model": bool(model),
            "needs": _needs_info(needs, connected),
        }
        if preview and meta.get("preview_note"):
            entry["preview_note"] = meta["preview_note"]
        if model and not preview and meta.get("builder"):
            try:
                entry["recipe"] = {"name": meta["name"], **meta["builder"](model)}
            except Exception:
                entry["recipe"] = None
        else:
            entry["recipe"] = None
        out.append(entry)
    return out


def build_graph(template_id: str, model: str) -> Optional[Dict[str, Any]]:
    """The runnable graph for one canned recipe (used by the run endpoint)."""
    meta = _BY_ID.get(template_id)
    if not meta or meta.get("preview") or not meta.get("builder") or not model:
        return None
    graph = meta["builder"](model)
    return {"name": meta["name"], **graph}


def get_template(template_id: str) -> Optional[Dict[str, Any]]:
    return _BY_ID.get(template_id)


def categories() -> List[str]:
    seen: List[str] = []
    for t in _CATALOG:
        if t["category"] not in seen:
            seen.append(t["category"])
    return seen


# ── back-compat: the old install-starters flow ────────────────────────────────
def _tool_names(tools) -> Set[str]:
    names: Set[str] = set()
    for t in tools or []:
        if isinstance(t, str):
            names.add(t)
        elif isinstance(t, dict) and t.get("name"):
            names.add(t["name"])
    return names


# Which toolbox each tool name belongs to (for deriving connected toolboxes
# from a flat tool list).
_TOOL_TOOLBOX = {
    "market_analyze": "market", "market_fundamentals": "market",
    "osint_whois": "osint", "osint_dns": "osint", "osint_website": "osint",
    "ts_http_check": "troubleshoot", "ts_dns_diagnose": "troubleshoot", "ts_tls_cert": "troubleshoot",
    "web_extract": "web",
}


def connected_from_tools(tools) -> Set[str]:
    return {_TOOL_TOOLBOX[n] for n in _tool_names(tools) if n in _TOOL_TOOLBOX}


def generate_starters(models: List[str], tools) -> List[Dict[str, Any]]:
    """Legacy shape: the runnable canned recipes as {name, nodes, edges}. Kept
    for the editor's existing "install starters" menu."""
    connected = connected_from_tools(tools)
    out: List[Dict[str, Any]] = []
    for entry in catalog(models, connected):
        if entry.get("available") and entry.get("recipe"):
            r = entry["recipe"]
            out.append({"name": entry["name"], "nodes": r["nodes"], "edges": r["edges"]})
    return out
