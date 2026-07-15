"""Recipe authoring helpers — turn a plain-English description into a runnable
recipe graph, and turn a graph back into a plain-English explanation.

The prompt-building and JSON parsing are pure and unit-testable; the two public
coroutines drive a local model (via canvas._run_model) and validate the result.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_SCHEMA = """A recipe is a directed graph of nodes wired by edges. Node types:
- input   → emits the run-time input text.        config: {"label": "..."}
- tool    → calls a tool with args.               config: {"tool": "<name>", "args": {"k": "v"}}
- model   → runs a prompt through the model.       config: {"model": "<MODEL>", "prompt": "...", "label": "..."}
- output  → collects the final result.             config: {"label": "..."}
- branch  → passes data through only if a condition matches, else cuts that path.
            config: {"condition": {"kind": "contains"|"equals"|"gt"|"lt", "value": "..."}}
- loop    → re-runs the model a few times to refine. config: {"model": "<MODEL>", "max_iters": 2}

Each node is {"id": "n1", "type": "...", "x": <int>, "y": <int>, "config": {...}}.
Edges are {"from": "n1", "to": "n2"}. Give every node a unique id like n1, n2, ...
and lay them left→right with x increasing along the flow (start x=60, step ~300).

A model node automatically receives every upstream node's output as context, so
you usually do NOT need placeholders. When you DO want to reference something
explicitly, use {{input}} for the run input and {{nodeId}} for another node's
output (e.g. inside a tool arg). Every {{nodeId}} must be a real node id.
The graph must be acyclic and have exactly one input and at least one output."""


def build_generate_prompt(description: str, model: str, tool_names: List[str]) -> Tuple[str, str]:
    tools = ", ".join(sorted(tool_names)) or "(none connected)"
    system = (
        "You design Aegis 'recipe' workflows — small graphs of tools and a local model. "
        f"{_SCHEMA}\n\n"
        f"Use this exact model id for every model and loop node: {model!r}.\n"
        f"Only these tools are available: {tools}. Do not invent tool names — if none fit, "
        "solve it with model nodes alone.\n\n"
        "Return EXACTLY ONE JSON object and nothing else: "
        '{"name": "...", "nodes": [...], "edges": [...]}. No prose, no markdown fences.'
    )
    user = f"Build a recipe for:\n\n{description.strip()}"
    return system, user


def parse_recipe_json(raw: str) -> Optional[Dict[str, Any]]:
    """Pull a recipe object out of a model response (tolerates ```json fences
    and surrounding prose)."""
    if not raw:
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        # first {...} balanced-ish span
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    try:
        obj = json.loads(text)
    except Exception:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("nodes"), list):
        return None
    obj.setdefault("edges", [])
    obj.setdefault("name", "Generated recipe")
    return obj


def bind_model(recipe: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Force every model/loop node onto the served model id (the LLM sometimes
    echoes a placeholder or the wrong name)."""
    for n in recipe.get("nodes", []):
        if n.get("type") in ("model", "loop"):
            n.setdefault("config", {})["model"] = model
    return recipe


async def generate_recipe(description: str, models: List[str], tools, owner: str) -> Dict[str, Any]:
    """Generate + validate a recipe graph from a description. One repair retry
    on invalid output."""
    from src.recipe_templates import best_model
    from src.recipes import validate_recipe
    from src.canvas import _run_model

    description = (description or "").strip()
    if not description:
        return {"ok": False, "error": "Describe what the recipe should do."}
    model = best_model(models or [])
    if not model:
        return {"ok": False, "error": "No capable model is served — add one in Settings first."}
    tool_names = [t["name"] if isinstance(t, dict) else t for t in (tools or [])]

    system, user = build_generate_prompt(description, model, tool_names)
    try:
        raw = await _run_model(system, user, model, owner, max_tokens=2500)
    except Exception as e:
        logger.debug("recipe generate model call failed: %s", e)
        return {"ok": False, "error": f"generation failed: {e}"}

    recipe = parse_recipe_json(raw)
    if recipe:
        bind_model(recipe, model)
        err = validate_recipe(recipe)
        if err:
            # One repair pass: hand the error back to the model.
            repair = (f"That graph was invalid: {err}. Return a corrected JSON object with the "
                      "same intent. JSON only.")
            try:
                raw2 = await _run_model(system, user + "\n\n" + repair + "\n\nYour last attempt:\n" + raw,
                                        model, owner, max_tokens=2500)
                recipe2 = parse_recipe_json(raw2)
                if recipe2:
                    bind_model(recipe2, model)
                    if validate_recipe(recipe2) is None:
                        recipe = recipe2
                        err = None
            except Exception:
                pass
        if err is None:
            return {"ok": True, "recipe": recipe}
        return {"ok": False, "error": f"the model produced an invalid graph ({err}) — try rephrasing."}
    return {"ok": False, "error": "the model didn't return a usable recipe — try rephrasing."}


def describe_graph(recipe: Dict[str, Any]) -> str:
    """A compact text rendering of the graph for the explain prompt."""
    nodes = {n["id"]: n for n in recipe.get("nodes", [])}
    lines = []
    for n in recipe.get("nodes", []):
        cfg = n.get("config", {}) or {}
        t = n.get("type")
        if t == "tool":
            desc = f"tool {cfg.get('tool', '?')}(args={cfg.get('args', {})})"
        elif t == "model":
            desc = f"model[{cfg.get('label') or 'model'}]: {(cfg.get('prompt') or '')[:160]}"
        elif t == "loop":
            desc = f"loop x{cfg.get('max_iters', 2)}"
        elif t == "branch":
            desc = f"branch if {cfg.get('condition', {})}"
        elif t == "input":
            desc = f"input ({cfg.get('label') or 'input'})"
        else:
            desc = t
        lines.append(f"  {n['id']} = {desc}")
    edges = "; ".join(f"{e.get('from')}→{e.get('to')}" for e in recipe.get("edges", []))
    return "Nodes:\n" + "\n".join(lines) + f"\nEdges: {edges}"


async def explain_recipe(recipe: Dict[str, Any], models: List[str], owner: str) -> Dict[str, Any]:
    from src.recipe_templates import best_model
    from src.canvas import _run_model
    if not recipe.get("nodes"):
        return {"ok": False, "error": "nothing to explain yet."}
    model = best_model(models or [])
    if not model:
        return {"ok": False, "error": "No model available."}
    system = ("You explain Aegis recipe workflows to a non-technical user. Given the node graph, "
              "write 2–4 short sentences describing, in plain English, what this workflow does "
              "end to end — what goes in, what each step contributes, and what comes out. No jargon, "
              "no node ids, no bullet lists.")
    user = "Explain this workflow:\n\n" + describe_graph(recipe)
    try:
        text = await _run_model(system, user, model, owner, max_tokens=500)
    except Exception as e:
        return {"ok": False, "error": f"explain failed: {e}"}
    return {"ok": True, "explanation": (text or "").strip()}
