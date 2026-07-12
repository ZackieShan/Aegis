"""
recipes.py — orchestration engine for "Recipes" (visual workflow graphs).

A recipe is a directed graph of nodes wired by edges. Nodes:
  - input  : emits the recipe's run-time input text.
  - tool   : calls a toolbox MCP tool (osint_*, market_*, ts_*) with args that
             may reference upstream node outputs via {{nodeId}} / {{input}}.
  - model  : runs a prompt through a model; upstream outputs are available as
             {{nodeId}} refs and are also prepended as context.
  - output : collects upstream output(s) as the recipe result.

Execution is a topological walk: each node runs after its inputs, its text
output is stored by node id, and downstream refs are substituted in. Cycles and
dangling refs are rejected before anything runs.

Storage: one JSON file per recipe under RECIPES_DIR.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from src.constants import RECIPES_DIR

NODE_TYPES = {"input", "tool", "model", "output", "branch", "loop"}
_REF_RE = re.compile(r"\{\{\s*([\w-]+)\s*\}\}")
_MAX_OUTPUT_CHARS = 8000


# ── storage ──────────────────────────────────────────────────────────────────
def _ensure_dir() -> None:
    os.makedirs(RECIPES_DIR, exist_ok=True)


def _path(recipe_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", recipe_id or "")
    if not safe:
        raise ValueError("invalid recipe id")
    return os.path.join(RECIPES_DIR, f"{safe}.json")


def list_recipes(owner: Optional[str] = None) -> List[Dict[str, Any]]:
    _ensure_dir()
    out = []
    for fn in sorted(os.listdir(RECIPES_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(RECIPES_DIR, fn), encoding="utf-8") as f:
                r = json.load(f)
        except Exception:
            continue
        if owner is not None and r.get("owner") not in (None, owner):
            continue
        out.append({
            "id": r.get("id"), "name": r.get("name", "Untitled"),
            "node_count": len(r.get("nodes", [])), "updated": r.get("updated"),
        })
    out.sort(key=lambda r: r.get("updated") or 0, reverse=True)
    return out


def get_recipe(recipe_id: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_path(recipe_id), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def save_recipe(data: Dict[str, Any], owner: Optional[str] = None) -> Dict[str, Any]:
    _ensure_dir()
    rid = data.get("id") or uuid.uuid4().hex[:12]
    err = validate_recipe(data)
    if err:
        raise ValueError(err)
    existing = get_recipe(rid)
    record = {
        "id": rid,
        "name": (data.get("name") or "Untitled").strip()[:120] or "Untitled",
        "nodes": data.get("nodes", []),
        "edges": data.get("edges", []),
        "owner": (existing or {}).get("owner", owner),
        "created": (existing or {}).get("created", time.time()),
        "updated": time.time(),
    }
    tmp = _path(rid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    os.replace(tmp, _path(rid))
    return record


def delete_recipe(recipe_id: str) -> bool:
    try:
        os.remove(_path(recipe_id))
        return True
    except (FileNotFoundError, ValueError):
        return False


# ── validation ───────────────────────────────────────────────────────────────
def validate_recipe(data: Dict[str, Any]) -> Optional[str]:
    """Return an error string, or None if the graph is runnable-shaped."""
    nodes = data.get("nodes")
    edges = data.get("edges", [])
    if not isinstance(nodes, list) or not nodes:
        return "recipe needs at least one node"
    ids = set()
    for n in nodes:
        nid = n.get("id")
        if not nid or nid in ids:
            return f"node ids must be present and unique (got {nid!r})"
        ids.add(nid)
        if n.get("type") not in NODE_TYPES:
            return f"unknown node type {n.get('type')!r}"
    for e in edges:
        if e.get("from") not in ids or e.get("to") not in ids:
            return "edge references a missing node"
    if _topo_order(nodes, edges) is None:
        return "recipe has a cycle — remove the loop"
    return None


def _topo_order(nodes: List[Dict], edges: List[Dict]) -> Optional[List[str]]:
    ids = [n["id"] for n in nodes]
    indeg = {i: 0 for i in ids}
    adj: Dict[str, List[str]] = {i: [] for i in ids}
    for e in edges:
        adj[e["from"]].append(e["to"])
        indeg[e["to"]] += 1
    queue = [i for i in ids if indeg[i] == 0]
    order = []
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    return order if len(order) == len(ids) else None


# ── execution ────────────────────────────────────────────────────────────────
def _substitute(text: str, outputs: Dict[str, str], run_input: str) -> str:
    def repl(m):
        key = m.group(1)
        if key == "input":
            return run_input
        return outputs.get(key, "")
    return _REF_RE.sub(repl, text or "")


async def run_recipe(recipe: Dict[str, Any], run_input: str, owner: Optional[str] = None) -> Dict[str, Any]:
    """Execute the graph; return {ok, final, steps:[{id,type,label,output}], outputs}."""
    err = validate_recipe(recipe)
    if err:
        return {"ok": False, "error": err}
    nodes = {n["id"]: n for n in recipe["nodes"]}
    edges = recipe.get("edges", [])
    order = _topo_order(recipe["nodes"], edges)
    parents: Dict[str, List[str]] = {nid: [] for nid in nodes}
    for e in edges:
        parents[e["to"]].append(e["from"])

    outputs: Dict[str, str] = {}
    steps: List[Dict[str, Any]] = []
    skipped: set = set()   # nodes on a branch that wasn't taken

    from src.tool_utils import get_mcp_manager
    mcp = get_mcp_manager()

    for nid in order:
        node = nodes[nid]
        ntype = node.get("type")
        cfg = node.get("config", {}) or {}
        _pars = parents.get(nid, [])
        # Skip a node whose every upstream path was cut by a branch. A node with
        # at least one live parent still runs (cut parents contribute nothing).
        if ntype != "input" and _pars and all(p in skipped for p in _pars):
            skipped.add(nid)
            outputs[nid] = ""
            steps.append({"id": nid, "type": ntype, "label": cfg.get("label") or _node_label(node),
                          "output": "(skipped — upstream branch not taken)"})
            continue
        upstream = [outputs.get(p, "") for p in _pars]
        try:
            if ntype == "input":
                out = run_input if not cfg.get("value") else _substitute(str(cfg["value"]), outputs, run_input)
            elif ntype == "output":
                out = "\n\n".join(u for u in upstream if u) or (upstream[0] if upstream else "")
            elif ntype == "tool":
                out = await _run_tool_node(mcp, cfg, outputs, run_input)
            elif ntype == "model":
                out = await _run_model_node(cfg, upstream, outputs, run_input, owner)
            elif ntype == "loop":
                out = await _run_loop_node(cfg, upstream, outputs, run_input, owner)
            elif ntype == "branch":
                _inval = "\n\n".join(u for u in upstream if u) or run_input
                if _eval_condition(cfg.get("condition") or {}, _inval):
                    out = _inval  # condition met — pass the data through
                else:
                    skipped.add(nid)
                    out = "(branch: condition not met — downstream skipped)"
            else:
                out = f"[unknown node type {ntype}]"
        except Exception as e:  # one bad node shouldn't kill the whole run
            out = f"[error in node {nid}: {type(e).__name__}: {e}]"
        out = (out or "")[:_MAX_OUTPUT_CHARS]
        # A cut branch contributes nothing downstream (so its skip note doesn't
        # bleed into a live node's context), but we still show it in the steps.
        outputs[nid] = "" if nid in skipped else out
        steps.append({"id": nid, "type": ntype, "label": cfg.get("label") or _node_label(node), "output": out})

    out_nodes = [nid for nid, n in nodes.items() if n.get("type") == "output"]
    final = "\n\n".join(outputs.get(nid, "") for nid in out_nodes) if out_nodes else (
        outputs.get(order[-1], "") if order else ""
    )
    return {"ok": True, "final": final, "steps": steps, "outputs": outputs}


def _node_label(node: Dict) -> str:
    cfg = node.get("config", {}) or {}
    t = node.get("type")
    if t == "tool":
        return cfg.get("tool", "tool")
    if t in ("model", "loop"):
        return cfg.get("model", t)
    if t == "branch":
        c = cfg.get("condition") or {}
        return f"branch: {c.get('kind', '?')} {c.get('value', '')}".strip()
    return t or "node"


async def _run_tool_node(mcp, cfg: Dict, outputs: Dict[str, str], run_input: str) -> str:
    tool = (cfg.get("tool") or "").strip()
    if not tool:
        return "[tool node: no tool selected]"
    # Accept a bare tool name (osint_whois) or a qualified one (mcp__osint__osint_whois).
    qualified = tool if tool.startswith("mcp__") else _qualify_tool(mcp, tool)
    if not qualified:
        return f"[tool node: unknown tool {tool!r}]"
    args = {}
    for k, v in (cfg.get("args") or {}).items():
        args[k] = _substitute(str(v), outputs, run_input) if isinstance(v, str) else v
    result = await mcp.call_tool(qualified, args)
    if isinstance(result, dict):
        return (result.get("stdout") or result.get("stderr") or result.get("error") or "").strip()
    return str(result)


def _qualify_tool(mcp, tool_name: str) -> Optional[str]:
    try:
        for t in mcp.get_all_tools():
            if t.get("name") == tool_name:
                return t.get("qualified_name")
    except Exception:
        pass
    return None


async def _model_generate(model_spec: str, system: str, user: str, owner: Optional[str]) -> str:
    """Resolve a model spec and run one completion. Shared by model + loop nodes."""
    from src.ai_interaction import _resolve_model
    from src.llm_core import llm_call_async
    import asyncio
    if not (model_spec or "").strip():
        return "[no model selected]"
    url, model, headers = await asyncio.to_thread(_resolve_model, model_spec, owner=owner)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return await llm_call_async(url, model, messages, headers=headers, timeout=180)


async def _run_model_node(cfg: Dict, upstream: List[str], outputs: Dict[str, str],
                          run_input: str, owner: Optional[str]) -> str:
    model_spec = (cfg.get("model") or "").strip()
    prompt = _substitute(cfg.get("prompt") or "", outputs, run_input)
    if not model_spec:
        return "[model node: no model selected]"
    if not prompt:
        prompt = "\n\n".join(u for u in upstream if u)
    context = "\n\n".join(u for u in upstream if u)
    user_content = (f"Context from previous steps:\n\n{context}\n\n---\n\n{prompt}"
                    if context and "{{" not in (cfg.get("prompt") or "") else prompt)
    return await _model_generate(
        model_spec,
        "You are one step in an automated workflow. Do exactly what the instruction asks, concisely.",
        user_content, owner)


# ── control-flow nodes ───────────────────────────────────────────────────────
def _eval_condition(cond: Dict, value: str) -> bool:
    """Evaluate a branch/loop condition against a text value."""
    kind = (cond.get("kind") or "contains").lower()
    needle = str(cond.get("value") or "")
    v = value or ""
    try:
        if kind == "contains":
            return needle.lower() in v.lower()
        if kind == "not_contains":
            return needle.lower() not in v.lower()
        if kind == "regex":
            return re.search(needle, v, re.I) is not None
        if kind == "nonempty":
            return bool(v.strip())
        if kind == "empty":
            return not v.strip()
    except Exception:
        return False
    return False


async def _run_loop_node(cfg: Dict, upstream: List[str], outputs: Dict[str, str],
                         run_input: str, owner: Optional[str]) -> str:
    """Iteratively run a model to refine a result: feed each output back as
    {{prev}} up to max_iters times, stopping early if `until` matches."""
    model_spec = (cfg.get("model") or "").strip()
    if not model_spec:
        return "[loop node: no model selected]"
    try:
        max_iters = int(cfg.get("max_iters", 3))
    except Exception:
        max_iters = 3
    max_iters = max(1, min(max_iters, 8))
    until = cfg.get("until") or {}
    _stop_active = bool(until.get("kind")) and (bool(until.get("value")) or until.get("kind") in ("nonempty", "empty"))
    prompt_tmpl = (cfg.get("prompt") or
                   "Improve the result below. If it is already good, return it unchanged.\n\n{{prev}}")
    prev = "\n\n".join(u for u in upstream if u) or run_input
    out = prev
    iters = 0
    for _ in range(max_iters):
        iters += 1
        user = _substitute(prompt_tmpl, {**outputs, "prev": prev}, run_input)
        out = await _model_generate(
            model_spec,
            "You are one step in an automated workflow that iterates to refine a result. Return only the improved result.",
            user, owner)
        if _stop_active and _eval_condition(until, out):
            break
        prev = out
    if iters > 1:
        out = f"{out}\n\n_(loop: {iters} iteration{'s' if iters != 1 else ''})_"
    return out
