"""generate_image (and manage_research) must have NATIVE function schemas.

Regression for the 20-round image loop (2026-07-12): tool-RAG selected
generate_image as relevant and the agent prompt described it, but
FUNCTION_TOOL_SCHEMAS had no generate_image entry — so in native-tool mode the
schema filter silently dropped it. The model (qwen3-coder-30b) read prose about
a tool it could not call, substituted the nearest schema it did have
(manage_memory), and spammed memory adds until the round cap.

Rule pinned here: every tool that _build_base_prompt can describe to a native
function-calling model must also exist in FUNCTION_TOOL_SCHEMAS (or be
deliberately schema-only/disabled), and the native call must convert into a
ToolBlock the executor understands.
"""
import ast
import json
import os
import sys
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _assigned_value(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return node.value
    raise AssertionError(f"{name} assignment not found")


def _schemas():
    src = open(os.path.join(ROOT, "src", "tool_schemas.py"), encoding="utf-8").read()
    value = _assigned_value(ast.parse(src), "FUNCTION_TOOL_SCHEMAS")
    return {item["function"]["name"]: item["function"] for item in ast.literal_eval(value)}


def test_generate_image_has_native_schema():
    schemas = _schemas()
    assert "generate_image" in schemas, (
        "generate_image lost its native schema — native-tool models will "
        "describe-but-not-offer it and substitute manage_memory (loop bug)"
    )
    fn = schemas["generate_image"]
    assert fn["parameters"]["required"] == ["prompt"]
    assert set(fn["parameters"]["properties"]) >= {"prompt", "model", "size", "quality"}


def test_manage_research_has_native_schema():
    # Same failure class: prompt-described (reading saved reports) but was
    # schema-less. trigger_research stays disabled in chat by design.
    assert "manage_research" in _schemas()


def test_prompt_sections_all_have_native_schemas():
    """Every tool the agent prompt can describe must be natively callable.

    TOOL_SECTIONS keys (including tuple keys) minus FUNCTION_TOOL_SCHEMAS
    names must be empty — a gap here recreates the substitution loop for
    whichever tool falls in it.
    """
    src = open(os.path.join(ROOT, "src", "agent_loop.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    value = _assigned_value(tree, "TOOL_SECTIONS")
    section_names = set()
    for key in value.keys:
        lit = ast.literal_eval(key)
        if isinstance(lit, tuple):
            section_names.update(lit)
        else:
            section_names.add(lit)
    missing = section_names - set(_schemas())
    assert not missing, (
        "Prompt-described tools with NO native schema (native models will "
        f"substitute the nearest schema they do have): {sorted(missing)}"
    )


def _import_converter():
    """Import function_call_to_tool_block with heavy deps stubbed (same idiom
    as test_function_call_non_object_args)."""
    stubbed = [
        "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.ext", "sqlalchemy.ext.declarative",
        "sqlalchemy.ext.hybrid", "sqlalchemy.sql", "sqlalchemy.sql.expression",
        "src.database", "core.models", "core.database", "core.auth",
    ]
    for mod in stubbed:
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()
    import src.agent_tools  # noqa: F401  (resolves the tool_schemas cycle)
    from src.tool_schemas import function_call_to_tool_block
    return function_call_to_tool_block


def test_native_generate_image_call_converts_and_parses():
    convert = _import_converter()
    block = convert("generate_image", json.dumps({"prompt": "a mountain lake at sunrise", "size": "768x768"}))
    assert block is not None
    assert block.tool_type == "generate_image"
    # The executor's MCP arg builder must round-trip the args (prompt is the
    # registered JSON primary key for generate_image).
    from src.tool_execution import _build_mcp_args
    args = _build_mcp_args("generate_image", block.content)
    assert args.get("prompt") == "a mountain lake at sunrise"
    assert args.get("size") == "768x768"


def test_native_generate_image_call_requires_prompt():
    convert = _import_converter()
    assert convert("generate_image", json.dumps({"prompt": ""})) is None
    assert convert("generate_image", "{}") is None


def test_native_manage_research_call_converts():
    convert = _import_converter()
    block = convert("manage_research", json.dumps({"action": "read", "id": "abc123"}))
    assert block is not None
    assert block.tool_type == "manage_research"
    assert json.loads(block.content) == {"action": "read", "id": "abc123"}
