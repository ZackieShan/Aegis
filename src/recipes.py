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

NODE_TYPES = {"input", "tool", "model", "output"}
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

    from src.tool_utils import get_mcp_manager
    mcp = get_mcp_manager()

    for nid in order:
        node = nodes[nid]
        ntype = node.get("type")
        cfg = node.get("config", {}) or {}
        upstream = [outputs.get(p, "") for p in parents.get(nid, [])]
        try:
            if ntype == "input":
                out = run_input if not cfg.get("value") else _substitute(str(cfg["value"]), outputs, run_input)
            elif ntype == "output":
                out = "\n\n".join(u for u in upstream if u) or (upstream[0] if upstream else "")
            elif ntype == "tool":
                out = await _run_tool_node(mcp, cfg, outputs, run_input)
            elif ntype == "model":
                out = await _run_model_node(cfg, upstream, outputs, run_input, owner)
            else:
                out = f"[unknown node type {ntype}]"
        except Exception as e:  # one bad node shouldn't kill the whole run
            out = f"[error in node {nid}: {type(e).__name__}: {e}]"
        out = (out or "")[:_MAX_OUTPUT_CHARS]
        outputs[nid] = out
        steps.append({"id": nid, "type": ntype, "label": cfg.get("label") or _node_label(node), "output": out})

    out_nodes = [nid for nid, n in nodes.items() if n.get("type") == "output"]
    final = "\n\n".join(outputs.get(nid, "") for nid in out_nodes) if out_nodes else (
        outputs.get(order[-1], "") if order else ""
    )
    return {"ok": True, "final": final, "steps": steps, "outputs": outputs}


def _node_label(node: Dict) -> str:
    cfg = node.get("config", {}) or {}
    if node.get("type") == "tool":
        return cfg.get("tool", "tool")
    if node.get("type") == "model":
        return cfg.get("model", "model")
    return node.get("type", "node")


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


async def _run_model_node(cfg: Dict, upstream: List[str], outputs: Dict[str, str],
                          run_input: str, owner: Optional[str]) -> str:
    from src.ai_interaction import _resolve_model
    from src.llm_core import llm_call_async
    import asyncio

    model_spec = (cfg.get("model") or "").strip()
    prompt = _substitute(cfg.get("prompt") or "", outputs, run_input)
    if not model_spec:
        return "[model node: no model selected]"
    if not prompt:
        prompt = "\n\n".join(u for u in upstream if u)
    url, model, headers = await asyncio.to_thread(_resolve_model, model_spec, owner=owner)
    context = "\n\n".join(u for u in upstream if u)
    user_content = (f"Context from previous steps:\n\n{context}\n\n---\n\n{prompt}"
                    if context and "{{" not in (cfg.get("prompt") or "") else prompt)
    messages = [
        {"role": "system", "content": "You are one step in an automated workflow. Do exactly what the instruction asks, concisely."},
        {"role": "user", "content": user_content},
    ]
    return await llm_call_async(url, model, messages, headers=headers, timeout=180)
