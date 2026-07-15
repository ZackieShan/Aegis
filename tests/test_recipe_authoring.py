"""Recipe authoring: prompt building, JSON parsing, model binding, graph render."""
from src import recipe_authoring as ra
from src.recipes import validate_recipe

_GOOD = {
    "name": "URL brief", "nodes": [
        {"id": "n1", "type": "input", "x": 60, "y": 80, "config": {"label": "URL"}},
        {"id": "n2", "type": "model", "x": 360, "y": 80, "config": {"model": "X", "prompt": "brief {{input}}"}},
        {"id": "n3", "type": "output", "x": 660, "y": 80, "config": {}},
    ], "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}],
}


def test_parse_from_fenced_json_with_prose():
    import json
    raw = "Sure!\n```json\n" + json.dumps(_GOOD) + "\n```\nHope this helps."
    r = ra.parse_recipe_json(raw)
    assert r and r["name"] == "URL brief" and len(r["nodes"]) == 3


def test_parse_bare_object():
    import json
    r = ra.parse_recipe_json(json.dumps(_GOOD))
    assert r and validate_recipe(r) is None


def test_parse_rejects_non_recipe():
    assert ra.parse_recipe_json("no json here") is None
    assert ra.parse_recipe_json('{"foo": 1}') is None       # no nodes list
    assert ra.parse_recipe_json("") is None


def test_parse_defaults_edges_and_name():
    r = ra.parse_recipe_json('{"nodes": [{"id":"n1","type":"input","config":{}}]}')
    assert r["edges"] == [] and r["name"]


def test_bind_model_forces_id_on_model_and_loop():
    recipe = {"nodes": [
        {"id": "n1", "type": "model", "config": {"model": "PLACEHOLDER", "prompt": "x"}},
        {"id": "n2", "type": "loop", "config": {"max_iters": 2}},
        {"id": "n3", "type": "tool", "config": {"tool": "web_extract"}},
    ]}
    ra.bind_model(recipe, "qwen3-coder-30b")
    assert recipe["nodes"][0]["config"]["model"] == "qwen3-coder-30b"
    assert recipe["nodes"][1]["config"]["model"] == "qwen3-coder-30b"
    assert "model" not in recipe["nodes"][2]["config"]  # tool untouched


def test_generate_prompt_lists_tools_and_pins_model():
    system, user = ra.build_generate_prompt("do a thing", "qwen3-coder-30b", ["web_extract", "market_analyze"])
    assert "web_extract" in system and "market_analyze" in system
    assert "qwen3-coder-30b" in system
    assert "do a thing" in user


def test_generate_prompt_handles_no_tools():
    system, _ = ra.build_generate_prompt("x", "m", [])
    assert "none connected" in system


def test_describe_graph_renders_nodes_and_edges():
    out = ra.describe_graph(_GOOD)
    assert "n1 = input" in out and "n2 = model" in out
    assert "n1→n2" in out and "n2→n3" in out
