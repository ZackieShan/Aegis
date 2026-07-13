"""Starter-recipe generator: every emitted graph must be valid, and tool-gated
templates must appear only when their toolbox tool is present."""

from src import recipe_templates as rt
from src.recipes import validate_recipe

# A representative install: a good local model + one tool from each toolbox.
ALL_TOOLS = [
    "osint_whois", "osint_dns", "osint_website",
    "ts_http_check", "ts_dns_diagnose", "ts_tls_cert",
    "web_extract",
    "market_analyze", "market_fundamentals",
]


def _names(recipes):
    return [r["name"] for r in recipes]


def test_best_model_prefers_capable_tool_caller():
    assert rt.best_model(["llama-pro-8b", "qwen3-coder-30b", "vicuna-7b"]) == "qwen3-coder-30b"
    assert rt.best_model(["gemma4", "llama-2-7b"]) == "gemma4"
    # Ties keep the earliest (picker order).
    assert rt.best_model(["qwen-a", "qwen-b"]) == "qwen-a"
    assert rt.best_model([]) is None


def test_best_model_never_picks_non_text_models():
    # A real llama-swap install serves image/video/vision models alongside the
    # LLMs; picker order lists qwen-image before qwen3-coder-30b, and a bare
    # substring rank ("qwen") used to make the image model win every template.
    served = [
        "ltx2.3-video", "qwen-image", "qwen-image-edit",
        "qwen-image-rapid-nsfw", "qwen-vl", "qwen3-coder-30b",
        "supergemma4-26b", "wan2.2-t2v",
    ]
    assert rt.best_model(served) == "qwen3-coder-30b"
    for m in ("qwen-image", "ltx2.3-video", "qwen-vl", "wan2.2-t2v",
              "stable-diffusion-3.5-medium", "flux-schnell"):
        assert rt.rank_model(m) == 0, m
    # An install serving ONLY non-text models has no usable default.
    assert rt.best_model(["qwen-image", "wan2.2-t2v"]) is None


def test_all_generated_recipes_validate():
    recipes = rt.generate_starters(["qwen3-coder-30b"], ALL_TOOLS)
    assert recipes, "expected starters with a model + all toolboxes"
    for r in recipes:
        err = validate_recipe(r)
        assert err is None, f"{r['name']} failed validation: {err}"
        # Preview payload the frontend relies on.
        assert r["name"] and r["description"] and r["run_example"]


def test_model_only_starters_present_without_tools():
    recipes = rt.generate_starters(["gemma4"], [])
    names = _names(recipes)
    # The three universal templates always work with just a model.
    assert "Summarize & extract (starter)" in names
    assert "Draft & self-refine (starter)" in names
    assert "Urgent triage & draft (starter)" in names
    # Tool-gated ones must NOT appear.
    assert "Domain dossier (starter)" not in names
    assert "Analyst debate (starter)" not in names
    assert "Web page brief (starter)" not in names
    assert "Site health check (starter)" not in names


def test_no_model_means_no_starters():
    assert rt.generate_starters([], ALL_TOOLS) == []


def test_tool_gating_is_per_toolbox():
    osint = _names(rt.generate_starters(["qwen"], ["osint_whois"]))
    assert "Domain dossier (starter)" in osint
    assert "Site health check (starter)" not in osint
    assert "Analyst debate (starter)" not in osint

    market = _names(rt.generate_starters(["qwen"], ["market_fundamentals"]))
    assert "Analyst debate (starter)" in market
    assert "Domain dossier (starter)" not in market

    web = _names(rt.generate_starters(["qwen"], ["web_extract"]))
    assert "Web page brief (starter)" in web

    ts = _names(rt.generate_starters(["qwen"], ["ts_tls_cert"]))
    assert "Site health check (starter)" in ts


def test_tool_specs_accept_dicts_from_the_tools_endpoint():
    # /api/recipes/tools returns [{name, ...}]; the generator must accept that.
    tools = [{"name": "osint_dns", "server": "osint"}]
    names = _names(rt.generate_starters(["qwen"], tools))
    assert "Domain dossier (starter)" in names


def test_generated_recipes_bind_the_best_model():
    recipes = rt.generate_starters(["llama-pro-8b", "qwen3-coder-30b"], ALL_TOOLS)
    for r in recipes:
        for n in r["nodes"]:
            if n["type"] in ("model", "loop"):
                assert n["config"].get("model") == "qwen3-coder-30b"


def test_starter_names_are_unique():
    names = _names(rt.generate_starters(["qwen"], ALL_TOOLS))
    assert len(names) == len(set(names))


def test_only_present_tools_are_wired():
    # Domain dossier with only whois present should reference only osint_whois.
    recipes = rt.generate_starters(["qwen"], ["osint_whois"])
    dossier = next(r for r in recipes if r["name"] == "Domain dossier (starter)")
    tool_nodes = [n for n in dossier["nodes"] if n["type"] == "tool"]
    assert [n["config"]["tool"] for n in tool_nodes] == ["osint_whois"]
