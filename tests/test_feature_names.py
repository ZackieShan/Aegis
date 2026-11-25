"""Two features were renamed so that "cookbook" and "recipes" only ever mean
cooking (the Kitchen):

    Cookbook  ->  Model Shopper   (find, download and serve models)
    Recipes   ->  Automations     (workflows you run, schedule and build)

The code keeps the old names (modules, ids, ``/api/cookbook``, ``/api/recipes``,
tool names). These tests keep the *shown* names from drifting back, keep the old
commands and addresses working, and keep the assistant from mixing the model
catalogue up with the user's food cookbook.
"""
import asyncio
import re
from pathlib import Path

import src.agent_tools  # noqa: F401  (break the agent_tools <-> tool_parsing import cycle)

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "static"


def _read(*parts):
    return STATIC.joinpath(*parts).read_text(encoding="utf-8")


def _code_lines(source):
    """Source without whole-line comments (they may still say what things were called)."""
    return "\n".join(line for line in source.splitlines() if not re.match(r"\s*(//|\*|/\*|<!--)", line))


# ── what people see ──────────────────────────────────────────────────────────

def test_the_sidebar_and_windows_show_the_new_names():
    page = _read("index.html")
    assert re.search(r'id="tool-cookbook-btn"[^>]*>(?:(?!</button>|</div>).)*Model Shopper', page, re.S)
    assert re.search(r'id="tool-recipes-btn"[^>]*>(?:(?!</button>|</div>).)*Automations', page, re.S)
    assert 'id="rail-cookbook" data-admin-only title="Model Shopper"' in page
    assert 'id="rail-recipes" data-admin-only title="Automations"' in page
    assert 'role="dialog" aria-label="Model Shopper"' in page
    # nothing a person reads on the main page still uses the old names
    assert not re.search(r">\s*(?:Cookbook|Recipes)\s*<", page)
    assert not re.search(r'(?:title|aria-label|placeholder)="[^"]*\b(?:Cookbook|Recipes)\b', page)


def test_the_kitchen_keeps_the_cooking_words():
    page = _read("index.html")
    assert 'id="tool-kitchen-link" href="/kitchen"' in page
    assert "recipes set out for cooking" in page


def test_the_automations_window_never_says_recipe():
    source = _code_lines(_read("js", "recipes", "index.js"))
    assert 'aria-label="Automations"' in source
    for wording in ("Start a new automation", "Untitled automation", "Search automations…", "⏰ Scheduled", "⏰ Schedule"):
        assert wording in source, wording
    # Capitalised, it is always shown text; so is lower-case text between tags.
    assert not re.search(r"\bRecipes?\b(?!\w)", re.sub(r"[A-Za-z_$][\w$]*Recipe\w*|RECIPE\w*", "", source))
    for shown in re.findall(r">([^<>{}$]*\brecipes?\b[^<>{}$]*)<", source):
        raise AssertionError(f"shown text still says recipe: {shown.strip()!r}")
    # ...and the messages that used to say it
    for gone in ("Untitled recipe", "Delete this recipe?", "No saved recipes yet.", "Could not open recipe.",
                 "Start a new recipe", "starter recipe", "All recipes", "Search recipes", "Create automation"):
        assert gone not in source, gone


def test_no_page_script_shows_the_word_cookbook():
    offenders = []
    for path in sorted(STATIC.rglob("*.js")):
        rel = path.relative_to(STATIC).as_posix()
        if rel.startswith("lite/") or "/vendor/" in rel or rel.endswith(".min.js"):
            continue                                         # the Kitchen's own pages: its cookbook is a real one
        for number, line in enumerate(_code_lines(path.read_text(encoding="utf-8")).splitlines(), 1):
            code = re.sub(r"//.*$", "", line)
            if re.search(r"(?<![\w$./-])Cookbook(?![\w$(])", re.sub(r"[A-Za-z_$][\w$]*Cookbook\w*", "", code)) and "_renamed('Cookbook'" not in code:
                offenders.append(f"{rel}: {line.strip()[:110]}")
    assert offenders == []


def test_the_commands_have_the_new_names_and_the_old_ones_still_work_out_of_sight():
    source = _read("js", "slashCommands.js")

    def entry(name):
        match = re.search(rf"\n  {name}: \{{\n(.*?)\n  \}},", source, re.S)
        assert match, f"/{name} is not a command"
        return match.group(1)

    assert "_cmdToolPanel('cookbook'" in entry("shopper") and "hidden: true" not in entry("shopper")
    assert "_cmdToolPanel('recipes'" in entry("automations") and "hidden: true" not in entry("automations")
    assert "'/kitchen'" in entry("kitchen") and "'cook'" in entry("kitchen")
    for old, shown in (("cookbook", "Model Shopper"), ("recipes", "Automations")):
        body = entry(old)
        assert "hidden: true" in body, f"/{old} should not be offered in the menu"
        assert f"_renamed(" in body and shown in body, f"/{old} should say what it is called now"
    # /open understands both the new and the old words
    assert "'shopper', 'model', 'models', 'modelshopper', 'model-shopper', 'cookbook', 'cook'" in source
    assert "automations: ['tool-recipes-btn', 'rail-recipes']" in source


def test_old_and_new_addresses_open_the_same_windows():
    app_js = _read("app.js")
    for path, button in (("/cookbook", "tool-cookbook-btn"), ("/model-shopper", "tool-cookbook-btn"), ("/automations", "tool-recipes-btn")):
        assert re.search(rf"'{re.escape(path)}':\s*\(\) => document\.getElementById\('{button}'\)", app_js), path
    served = (REPO / "app.py").read_text(encoding="utf-8")
    for path in ("/cookbook", "/model-shopper", "/automations"):
        assert f'@app.get("{path}")' in served, path
    page = _read("index.html")
    assert "'/model-shopper': 'Model Shopper — Aegis'" in page and "'/automations': 'Automations — Aegis'" in page


def test_settings_and_roles_use_the_new_names():
    from core import access
    from routes.account_routes import HIDEABLE_UI

    labels = dict(HIDEABLE_UI)
    # The Model Shopper and Automations are the administrator's and are never drawn for anyone else,
    # so they are no longer things to hide for one person; nothing on that list uses the old names.
    assert "tool-cookbook" not in labels and "tool-recipes" not in labels
    assert not any(word in label for label in labels.values() for word in ("Cookbook", "Recipes"))
    assert access._PRIVILEGE_LABELS["can_use_recipes"][0] == "Automations"


# ── what the assistant understands ───────────────────────────────────────────

def _ui(command):
    from src.ai_interaction import do_ui_control
    return asyncio.run(do_ui_control(command))


def test_the_assistant_opens_the_model_shopper_by_its_name():
    for said in ("open_panel model_shopper", "open_panel model shopper", "open_panel the model-shopper", "open_panel shopper", "open_panel models"):
        result = _ui(said)
        assert result.get("ui_event") == "open_panel" and result.get("panel") == "cookbook", (said, result)
        assert "Model Shopper" in result["results"] and "cookbook" not in result["results"].lower()


def test_the_assistant_opens_automations_by_name():
    for said in ("open_panel automations", "open_panel automation", "open_panel the automations panel"):
        result = _ui(said)
        assert result.get("ui_event") == "open_panel" and result.get("panel") == "automations", (said, result)
    assert "panel === 'automations'" in _read("js", "chatStream.js")


def test_cookbook_and_recipes_now_lead_to_the_kitchen():
    """The Kitchen is a page of its own: the app goes there when the reply ends,
    and the link is in the result for anywhere that cannot."""
    for said in ("open_panel cookbook", "open_panel recipes", "open_panel my cookbook", "open_panel kitchen", "open_panel cook book"):
        result = _ui(said)
        assert result.get("ui_event") == "open_panel" and result.get("panel") == "kitchen", (said, result)
        assert "[Kitchen](/kitchen)" in result["results"], (said, result)
    reader = _ui("open_panel reader")
    assert reader.get("panel") == "reader" and "[Reader](/read)" in reader["results"]
    page = _read("js", "chatStream.js")
    assert "panel === 'kitchen' || panel === 'reader'" in page
    assert "'/kitchen'" in page and "'/read'" in page and "aegis:chat-busy-change" in page


def test_an_unknown_panel_lists_the_names_that_exist_now():
    result = _ui("open_panel nonsense")
    assert "model_shopper" in result["error"] and "automations" in result["error"] and "kitchen" in result["error"]
    assert "cookbook" not in result["error"]


def test_a_printed_open_panel_line_is_still_rescued_for_the_new_names():
    from src.tool_parsing import parse_tool_blocks

    for name in ("model_shopper", "automations", "kitchen", "shopper"):
        blocks = parse_tool_blocks(f"ui_control open_panel {name}", skip_fenced=True)
        assert len(blocks) == 1 and blocks[0].content == f"open_panel {name}", name


def _hinted(query):
    from src.tool_index import ToolIndex

    index = ToolIndex.__new__(ToolIndex)
    index.retrieve = lambda query, k=8: []          # no vector store: only the keyword hints
    return index.get_tools_for_query(query, always_include={"web_fetch"})


def test_my_cookbook_means_the_kitchen_not_the_model_servers():
    for said in ("what's in my cookbook", "add this to the cookbook", "show me the recipes I saved"):
        tools = _hinted(said)
        assert "manage_kitchen" in tools, said
        assert "list_served_models" not in tools and "stop_served_model" not in tools, said


def test_the_model_shopper_brings_in_the_model_tools():
    for said in ("what is in the model shopper", "is the shopper showing anything", "what's running right now", "stop cookbook"):
        assert "list_served_models" in _hinted(said), said
    assert "manage_kitchen" not in _hinted("what's running in the model shopper")


def test_only_talk_about_models_loads_the_model_serving_rules():
    import src.agent_loop as agent_loop

    def domains(said):
        return agent_loop._classify_agent_request([{"role": "user", "content": said}], said)["domains"]

    assert "cookbook" in domains("what is serving in the model shopper")
    assert "cookbook" in domains("serve qwen on the gpu box")
    assert "cookbook" not in domains("add grandma's biscuits to my cookbook")
    assert "cookbook" not in domains("what recipes are in my cookbook")


def test_the_assistant_is_told_the_new_names():
    import src.agent_loop as agent_loop
    from src.tool_index import ToolIndex

    source = (REPO / "src" / "agent_loop.py").read_text(encoding="utf-8")
    assert '"Model Shopper" = the part of Aegis that finds, downloads and serves models' in source
    assert "'show my cookbook'" not in source and '"show my cookbook"' not in source
    described = (REPO / "src" / "tool_index.py").read_text(encoding="utf-8") + (REPO / "src" / "tool_schemas.py").read_text(encoding="utf-8")
    assert "'show my cookbook'" not in described and "'kill my cookbook'" not in described
    assert "model_shopper, automations, kitchen" in described
    assert agent_loop is not None and ToolIndex is not None


def test_a_scheduled_model_job_finds_its_calendar_under_either_name():
    schedule = _read("js", "cookbookSchedule.js")
    assert '["model shopper", "cookbook"]' in schedule and "name=Model%20Shopper" in schedule
    routes = (REPO / "routes" / "task_routes.py").read_text(encoding="utf-8")
    assert 'in ("model shopper", "cookbook")' in routes
