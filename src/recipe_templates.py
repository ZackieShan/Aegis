"""
recipe_templates.py — starter Recipe generator.

Produces ready-to-run Recipe graphs tailored to what an install actually has:
it picks a capable local model and only emits templates whose required toolbox
tools are connected. Backs the one-click "install starter recipes" and the
preview menu so a first-time user isn't staring at a blank canvas.

Pure and deterministic (no I/O) so it is unit-testable; the route layer feeds it
the available models + tool names and persists whatever it returns. Every graph
this module emits is shaped to pass ``recipes.validate_recipe`` unchanged.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Set


# Rough ranking of how reliably a local model calls tools / follows instructions.
# Mirrors static/js/recipes/index.js `_rankModel` so a backend-picked default
# matches what the editor would choose. Higher = better default.
def rank_model(name: str) -> int:
    s = (name or "").lower()
    # Non-text models (diffusion/video/vision/audio) can't drive a model node.
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
    """Pick the highest-ranked text model; ties keep the earliest (picker
    order). Rank-0 entries (image/video/vision models) are never picked."""
    best: Optional[str] = None
    for m in models or []:
        if rank_model(m) <= 0:
            continue
        if best is None or rank_model(m) > rank_model(best):
            best = m
    return best


# ── graph builders ────────────────────────────────────────────────────────────
def _node(nid: str, ntype: str, x: int, y: int, **config: Any) -> Dict[str, Any]:
    return {"id": nid, "type": ntype, "x": x, "y": y, "config": config}


def _edge(a: str, b: str) -> Dict[str, str]:
    return {"from": a, "to": b}


def _recipe(name: str, description: str, nodes: List[Dict], edges: List[Dict],
            run_example: str) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "nodes": nodes,
        "edges": edges,
        "run_example": run_example,
    }


def _fanin(name: str, description: str, input_label: str, run_example: str,
           model: str, tool_specs: List, model_prompt: str) -> Dict[str, Any]:
    """input → each present tool → model → output.

    `tool_specs` is a list of (tool_name, args_dict, y). The model prompt carries
    no {{ref}}, so the engine auto-prepends each tool node's output as context.
    """
    nodes: List[Dict] = [_node("n1", "input", 60, 170, label=input_label)]
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
    return _recipe(name, description, nodes, edges, run_example)


# ── model-only templates (always available when any model exists) ─────────────
def _t_summarize(model: Optional[str], tools: Set[str]) -> Optional[Dict]:
    if not model:
        return None
    nodes = [
        _node("n1", "input", 60, 160, label="Text"),
        _node("n2", "model", 380, 160, model=model,
              prompt="Summarize the text below into 5 crisp bullet points, then a one-line takeaway.\n\n{{input}}"),
        _node("n3", "output", 720, 160),
    ]
    edges = [_edge("n1", "n2"), _edge("n2", "n3")]
    return _recipe(
        "Summarize & extract (starter)",
        "Any text in → 5 bullets + a takeaway. The simplest recipe — a good first run.",
        nodes, edges,
        "Paste an article, email, or meeting notes here and hit Run.")


def _t_refine(model: Optional[str], tools: Set[str]) -> Optional[Dict]:
    if not model:
        return None
    # The loop node deliberately carries NO prompt so the engine's built-in
    # refine prompt (which uses the {{prev}} loop variable) drives it — putting
    # {{prev}} in node config would fail validate_recipe (prev is not a node id).
    nodes = [
        _node("n1", "input", 60, 160, label="Ask"),
        _node("n2", "model", 340, 160, model=model,
              prompt="Write a first draft that answers the request below.\n\n{{input}}"),
        _node("n3", "loop", 620, 160, model=model, max_iters=2, until={"kind": "", "value": ""}),
        _node("n4", "output", 900, 160),
    ]
    edges = [_edge("n1", "n2"), _edge("n2", "n3"), _edge("n3", "n4")]
    return _recipe(
        "Draft & self-refine (starter)",
        "The model drafts, then a Loop node refines it a couple of times before output.",
        nodes, edges,
        "Write a short, friendly email introducing a new self-hosted AI workspace.")


def _t_triage(model: Optional[str], tools: Set[str]) -> Optional[Dict]:
    if not model:
        return None
    # Dual output: the classification always shows; the draft only when the
    # Branch condition (contains "URGENT") is met — a clear Branch demo that
    # never produces an empty screen.
    nodes = [
        _node("n1", "input", 40, 170, label="Message"),
        _node("n2", "model", 300, 170, model=model, label="Classify",
              prompt="Classify the message below. Put URGENT or ROUTINE on the first line, then one line on why.\n\n{{input}}"),
        _node("n3", "output", 600, 70, label="Classification"),
        _node("n4", "branch", 560, 290, condition={"kind": "contains", "value": "URGENT"}),
        _node("n5", "model", 830, 290, model=model, label="Draft reply",
              prompt="This message was flagged urgent. Draft a brief, calm reply that acknowledges it and states the immediate next step."),
        _node("n6", "output", 1110, 290, label="Urgent draft"),
    ]
    edges = [
        _edge("n1", "n2"), _edge("n2", "n3"),
        _edge("n2", "n4"), _edge("n4", "n5"), _edge("n5", "n6"),
    ]
    return _recipe(
        "Urgent triage & draft (starter)",
        "Classify a message; only if it's URGENT does the Branch flow on to draft a reply.",
        nodes, edges,
        "Hi — the front door lock jammed and I'm locked out, can someone help right now?")


# ── tool-gated templates (emitted only when their toolbox is connected) ───────
def _t_domain(model: Optional[str], tools: Set[str]) -> Optional[Dict]:
    if not model:
        return None
    specs = []
    if "osint_whois" in tools:
        specs.append(("osint_whois", {"target": "{{input}}"}, 60))
    if "osint_dns" in tools:
        specs.append(("osint_dns", {"domain": "{{input}}", "record_type": "A"}, 180))
    if "osint_website" in tools:
        specs.append(("osint_website", {"url": "{{input}}"}, 300))
    if not specs:
        return None
    return _fanin(
        "Domain dossier (starter)",
        "OSINT recon on a domain → a short risk brief. Needs the OSINT toolbox.",
        "Domain", "example.com", model, specs,
        "Using the recon above, write a short risk brief on this domain: who owns it, its "
        "infrastructure and mail setup, and anything notable or suspicious.")


def _t_site_health(model: Optional[str], tools: Set[str]) -> Optional[Dict]:
    if not model:
        return None
    specs = []
    if "ts_http_check" in tools:
        specs.append(("ts_http_check", {"url": "{{input}}"}, 60))
    if "ts_dns_diagnose" in tools:
        specs.append(("ts_dns_diagnose", {"domain": "{{input}}"}, 180))
    if "ts_tls_cert" in tools:
        specs.append(("ts_tls_cert", {"host": "{{input}}"}, 300))
    if not specs:
        return None
    return _fanin(
        "Site health check (starter)",
        "Is this site up? HTTP + DNS + TLS checks → a plain-English diagnosis. Needs the Troubleshooting toolbox.",
        "Site or domain", "example.com", model, specs,
        "Based on the checks above, say whether the site looks healthy. Call out any errors, DNS "
        "problems, or a soon-to-expire TLS certificate, and suggest the likely fix.")


def _t_web_brief(model: Optional[str], tools: Set[str]) -> Optional[Dict]:
    if not model or "web_extract" not in tools:
        return None
    return _fanin(
        "Web page brief (starter)",
        "Fetch a URL and summarize the page into a brief. Needs the Web toolbox.",
        "Page URL", "https://example.com", model,
        [("web_extract", {"url": "{{input}}"}, 170)],
        "Summarize the page content above into a short brief: what it is, the key points, and any "
        "action items or links worth following.")


def _t_analyst(model: Optional[str], tools: Set[str]) -> Optional[Dict]:
    if not model:
        return None
    data = []
    if "market_analyze" in tools:
        data.append(("market_analyze", {"symbol": "{{input}}"}, 110))
    if "market_fundamentals" in tools:
        data.append(("market_fundamentals", {"symbol": "{{input}}"}, 350))
    if not data:
        return None
    nodes: List[Dict] = [_node("n1", "input", 40, 230, label="Ticker")]
    edges: List[Dict] = []
    seq = 2
    data_ids: List[str] = []
    for tname, args, y in data:
        nid = f"n{seq}"; seq += 1
        nodes.append(_node(nid, "tool", 280, y, tool=tname, args=args))
        edges.append(_edge("n1", nid))
        data_ids.append(nid)
    personas = [
        ("Value investor", 110,
         "You are a disciplined value investor (Buffett/Graham). Using the market data above, judge "
         "whether this is a quality business at a fair price — cite valuation (P/E, P/B, FCF), margins, "
         "and balance-sheet health. End with your call: BULLISH, BEARISH, or NEUTRAL, and a one-line reason."),
        ("Growth investor", 230,
         "You are a growth investor (Wood/Lynch). Using the market data above, judge the growth story: "
         "revenue/earnings growth, momentum, and narrative, weighed against the price paid. End with your "
         "call: BULLISH, BEARISH, or NEUTRAL, and a one-line reason."),
        ("Contrarian / risk", 350,
         "You are a contrarian risk manager (Burry/Taleb). Using the market data above, stress-test the "
         "bull case: overvaluation, debt, volatility, crowding, and tail risks. End with your call: "
         "BULLISH, BEARISH, or NEUTRAL, and a one-line reason."),
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
                       prompt="You are the portfolio manager. Three analysts gave their views above. Weigh "
                              "them, note where they agree and disagree, and give a final call — BUY, HOLD, or "
                              "SELL — with a confidence (low/medium/high) and a two-sentence rationale. This is "
                              "educational analysis, not investment advice."))
    for pid in persona_ids:
        edges.append(_edge(pid, pm))
    oid = f"n{seq}"; seq += 1
    nodes.append(_node(oid, "output", 1140, 230))
    edges.append(_edge(pm, oid))
    return _recipe(
        "Analyst debate (starter)",
        "Market data → value/growth/contrarian analysts → a portfolio-manager verdict. Needs the Market toolbox.",
        nodes, edges, "NVDA")


# Order matters: universal templates first so the list reads simple → advanced.
_TEMPLATES: List[Callable[[Optional[str], Set[str]], Optional[Dict]]] = [
    _t_summarize,
    _t_refine,
    _t_triage,
    _t_web_brief,
    _t_domain,
    _t_site_health,
    _t_analyst,
]


def _tool_names(tools) -> Set[str]:
    names: Set[str] = set()
    for t in tools or []:
        if isinstance(t, str):
            names.add(t)
        elif isinstance(t, dict) and t.get("name"):
            names.add(t["name"])
    return names


def generate_starters(models: List[str], tools) -> List[Dict[str, Any]]:
    """Return the starter recipes runnable on this install.

    `models` is a list of model-id strings; `tools` is a list of tool-name
    strings or {name: ...} dicts. Emits only templates whose requirements are
    met, with node configs bound to the best available model and present tools.
    """
    model = best_model(models or [])
    names = _tool_names(tools)
    out: List[Dict[str, Any]] = []
    for build in _TEMPLATES:
        try:
            recipe = build(model, names)
        except Exception:
            recipe = None
        if recipe:
            out.append(recipe)
    return out
