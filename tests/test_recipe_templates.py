"""Canned-recipe catalog: model ranking, per-toolbox availability, valid graphs,
preview entries, and the back-compat starter shape."""

from src import recipe_templates as rt
from src.recipes import validate_recipe

ALL_TB = {"market", "osint", "troubleshoot", "web"}


def _by_id(cat):
    return {e["id"]: e for e in cat}


# ── model ranking (unchanged behavior) ────────────────────────────────────────
def test_best_model_prefers_capable_tool_caller():
    assert rt.best_model(["llama-pro-8b", "qwen3-coder-30b", "vicuna-7b"]) == "qwen3-coder-30b"
    assert rt.best_model(["gemma4", "llama-2-7b"]) == "gemma4"
    assert rt.best_model(["qwen-a", "qwen-b"]) == "qwen-a"  # ties keep earliest
    assert rt.best_model([]) is None


def test_best_model_never_picks_non_text_models():
    served = ["ltx2.3-video", "qwen-image", "qwen-image-edit", "qwen-image-rapid-nsfw",
              "qwen-vl", "qwen3-coder-30b", "supergemma4-26b", "wan2.2-t2v"]
    assert rt.best_model(served) == "qwen3-coder-30b"
    for m in ("qwen-image", "ltx2.3-video", "qwen-vl", "wan2.2-t2v", "flux-schnell"):
        assert rt.rank_model(m) == 0, m
    assert rt.best_model(["qwen-image", "wan2.2-t2v"]) is None


# ── catalog shape ─────────────────────────────────────────────────────────────
def test_catalog_lists_everything_regardless_of_toolboxes():
    """Gated recipes stay in the list even when their toolbox is off — the UI
    needs them present to offer a one-click enable."""
    cat = _by_id(rt.catalog(["qwen3-coder-30b"], set()))
    # all four toolbox recipes are present but not available
    for rid in ("stock", "domain", "site_health", "web_brief"):
        assert rid in cat, rid
        assert cat[rid]["available"] is False
        assert cat[rid]["needs"], f"{rid} should carry a needs entry"
        assert cat[rid]["needs"][0]["enabled"] is False
    # model-only recipes are available
    for rid in ("summarize", "meeting_notes", "doc_brief", "compare", "translate"):
        assert cat[rid]["available"] is True
        assert cat[rid]["needs"] == []


def test_toolbox_recipe_becomes_available_when_connected():
    cat = _by_id(rt.catalog(["qwen"], {"market"}))
    assert cat["stock"]["available"] is True
    assert cat["stock"]["needs"][0]["enabled"] is True
    assert cat["domain"]["available"] is False  # osint still off


def test_stock_is_the_renamed_bull_base_bear():
    cat = _by_id(rt.catalog(["qwen"], {"market"}))
    assert cat["stock"]["name"] == "Stock analysis — Bull / Base / Bear"
    labels = [n["config"].get("label", "") for n in cat["stock"]["recipe"]["nodes"]
              if n["type"] == "model"]
    assert any("Bull" in l for l in labels)
    assert any("Bear" in l for l in labels)


def test_preview_entries_present_but_not_runnable():
    cat = _by_id(rt.catalog(["qwen"], ALL_TB))
    for rid in ("inbox_declutter", "morning_brief"):
        assert cat[rid]["preview"] is True
        assert cat[rid]["available"] is False
        assert cat[rid]["recipe"] is None
        assert cat[rid].get("preview_note")


def test_every_runnable_graph_validates():
    cat = rt.catalog(["qwen3-coder-30b"], ALL_TB)
    runnable = [e for e in cat if e["recipe"]]
    assert len(runnable) >= 11
    for e in runnable:
        err = validate_recipe(e["recipe"])
        assert err is None, f"{e['id']} failed validation: {err}"


def test_no_model_means_nothing_available():
    cat = _by_id(rt.catalog([], ALL_TB))
    assert all(e["available"] is False for e in cat.values())
    assert all(e["has_model"] is False for e in cat.values())
    assert all(e["recipe"] is None for e in cat.values())


def test_catalog_entries_carry_human_metadata():
    for e in rt.catalog(["qwen"], set()):
        assert e["name"] and e["description"] and e["sample_input"] and e["expected_output"]
        assert e["category"] in rt.categories()


def test_build_graph_runnable_and_gated():
    assert rt.build_graph("stock", "qwen3-coder-30b")["name"].startswith("Stock analysis")
    assert rt.build_graph("inbox_declutter", "qwen") is None   # preview
    assert rt.build_graph("nope", "qwen") is None               # unknown
    assert rt.build_graph("summarize", "") is None              # no model


def test_graphs_bind_the_best_model():
    g = rt.build_graph("stock", "qwen3-coder-30b")
    for n in g["nodes"]:
        if n["type"] in ("model", "loop"):
            assert n["config"].get("model") == "qwen3-coder-30b"


# ── back-compat starter shape ─────────────────────────────────────────────────
def test_generate_starters_returns_valid_available_recipes():
    starters = rt.generate_starters(["qwen3-coder-30b"], ["market_analyze"])
    names = [s["name"] for s in starters]
    assert "Stock analysis — Bull / Base / Bear" in names
    assert "Domain dossier" not in names  # osint not in the tool list
    for s in starters:
        assert validate_recipe(s) is None


def test_generate_starters_empty_without_model():
    assert rt.generate_starters([], ["market_analyze"]) == []


def test_connected_from_tools_maps_toolboxes():
    assert rt.connected_from_tools(["market_fundamentals"]) == {"market"}
    assert rt.connected_from_tools([{"name": "osint_dns"}]) == {"osint"}
    assert rt.connected_from_tools(["web_extract", "ts_tls_cert"]) == {"web", "troubleshoot"}
