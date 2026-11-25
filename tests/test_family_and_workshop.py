"""Two screens and the line between them.

What must hold:
  * everyone but an administrator gets the family screen from "/"; the full app (the workshop) is the
    administrator's, and one person can be let in by an administrator;
  * an administrator switches between the two, and the choice is the browser's (a phone in family
    mode, a desk in the workshop);
  * the page says, as it is served, whether the viewer is an administrator — the server's controls
    are never drawn for anyone else, and cannot be reached by a side door;
  * the sidebar is a handful of everyday tools and one folded Workshop;
  * nobody is asked whether to use the web, or whether tools are allowed: the switch on the message
    box is Auto | Code, "no tools" is a setting, and a browser that never chose starts on Auto;
  * the workings under a reply (model, speed, context, internal tool names) are off until
    an administrator switches them on, and a step then still says in plain words what was used.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from core import landing

REPO = Path(__file__).resolve().parent.parent
PAGE = (REPO / "static" / "index.html").read_text(encoding="utf-8")
STYLE = (REPO / "static" / "style.css").read_text(encoding="utf-8")
APP_JS = (REPO / "static" / "app.js").read_text(encoding="utf-8")
SERVED = (REPO / "app.py").read_text(encoding="utf-8")

FAMILY = {"mode": "simple", "allow_full_app": False}
LET_IN = {"mode": "simple", "allow_full_app": True}
FULL = {"mode": "full"}


# ── who gets which screen ──────────────────────────────────────────────────

def test_the_built_in_roles_put_everyone_but_the_administrator_on_the_family_screen():
    from core import access
    roles = access._builtin_roles()
    assert {name: role["experience"]["mode"] for name, role in roles.items()} == {
        "admin": "full", "member": "simple", "household": "simple", "guest": "simple"}
    assert "family screen" in roles["member"]["description"].lower() and "full app" not in roles["member"]["description"].lower()


@pytest.mark.parametrize("experience, is_admin, wants_full, view, home", [
    (FAMILY, False, False, "", True), (FAMILY, False, True, "", True),          # the family screen is all they have
    (FAMILY, False, True, "workshop", True),                                    # …whatever a cookie says
    (LET_IN, False, False, "", True), (LET_IN, False, True, "", False),         # let in: only when they ask
    (FULL, True, False, "", False), (FULL, True, False, "family", True),        # an administrator: by their switch
    (FULL, True, True, "family", False),                                        # …and "?full" wins for that visit
    (FAMILY, True, False, "", True), (FAMILY, True, True, "", False),           # an administrator set to the family screen
    (FULL, False, False, "", False), (FULL, False, False, "family", True),      # a custom role with the full app
    (None, True, False, "", False), ({}, False, False, "", False),
])
def test_what_the_front_door_shows(experience, is_admin, wants_full, view, home):
    assert landing.lands_on_home(experience, is_admin, wants_full, view) is home


def test_only_those_who_may_open_the_full_app_can_switch_to_it():
    assert landing.may_open_full_app(FAMILY, False) is False and landing.may_open_full_app(LET_IN, False) is True
    assert landing.may_open_full_app(FAMILY, True) is True and landing.may_open_full_app(FULL, False) is True
    assert landing.switch_target("family", True) == "/home" and landing.switch_target("family", False) == "/home"
    assert landing.switch_target("workshop", True) == "/?full=1" and landing.switch_target("workshop", False) == "/home"
    assert landing.switch_target("admin", True) is None and landing.switch_target("", True) is None


def test_the_switch_is_remembered_by_the_browser_not_the_account():
    assert '@app.get("/switch/{view}")' in SERVED and "_landing.switch_target(view" in SERVED
    assert "response.set_cookie(_landing.VIEW_COOKIE, _landing.FAMILY" in SERVED and "httponly=True" in SERVED
    assert "response.delete_cookie(_landing.VIEW_COOKIE" in SERVED
    assert "request.cookies.get(_landing.VIEW_COOKIE)" in SERVED
    assert 'id="user-bar-family" href="/switch/family"' in PAGE              # in the full app: to family mode
    home = (REPO / "static" / "lite" / "home.js").read_text(encoding="utf-8")
    assert "'/switch/workshop'" in home                                       # on the family screen: back to the workshop


# ── the page says who is looking ───────────────────────────────────────────

def test_the_shell_is_stamped_as_it_is_served():
    from fastapi import Request
    from src.app_helpers import serve_html_with_nonce

    assert '<html lang="en"{{ROOT_ATTRS}}>' in PAGE
    assert "root_attrs=marks" in SERVED and '\' data-admin="1"\' if _is_admin_viewer(request) else \' data-admin="0"\'' in SERVED
    request = Request({"type": "http", "headers": [], "state": {}})
    for marks, expected in ((' data-admin="1"', '<html lang="en" data-admin="1">'), ("", '<html lang="en">')):
        body = serve_html_with_nonce(request, str(REPO / "static" / "index.html"), root_attrs=marks).body.decode("utf-8")
        assert expected in body and "{{ROOT_ATTRS}}" not in body


def test_what_is_the_administrators_is_never_drawn_for_anyone_else():
    assert 'html:not([data-admin="1"]) [data-admin-only],' in STYLE and 'html:not([data-admin="1"]) .admin-only { display: none !important; }' in STYLE
    marked = set(re.findall(r'id="([\w-]+)"[^>]*\bdata-admin-only\b', PAGE)) | set(re.findall(r'\bdata-admin-only\b[^>]*id="([\w-]+)"', PAGE))
    for must in ("workshop-group", "model-picker-wrap", "composer-research-btn", "overflow-deepresearch-btn", "overflow-rag-btn",
                 "overflow-workspace-btn", "overflow-preset-btn", "bash-toggle-btn", "email-section", "models-section",
                 "rail-control", "rail-cookbook", "rail-compare", "rail-recipes", "rail-research", "rail-tasks", "rail-memory"):
        assert must in marked, must
    assert '<div class="mode-toggle" data-admin-only>' in PAGE
    # …and a shortcut, a typed command or a bookmark, which all end in a click on one of those rows, is stopped too
    assert "closest('[data-admin-only], .admin-only')" in APP_JS and "event.stopImmediatePropagation();" in APP_JS
    # the account keeps the mark true if the page was cached from someone else's visit
    assert "document.documentElement.dataset.admin = (d.is_admin || d.configured === false) ? '1' : '0';" in APP_JS


def test_settings_for_everyone_else_is_looks_notifications_and_account():
    tabs = re.findall(r'<button class="settings-nav-item([^"]*)" data-settings-tab="([\w-]+)"', PAGE)
    everyone = [tab for classes, tab in tabs if "admin-only" not in classes]
    assert everyone == ["reminders", "appearance", "account"]
    assert {tab for classes, tab in tabs if "admin-only" in classes} >= {"services", "added-models", "ai", "search", "integrations", "email", "tools", "users", "system"}
    settings = (REPO / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    assert "const EVERYONE_TABS = new Set(['appearance', 'reminders', 'account']);" in settings
    assert "if (!viewerIsAdmin() && !EVERYONE_TABS.has(tab || '')) tab = 'appearance';" in settings


# ── the sidebar ────────────────────────────────────────────────────────────

def _tools_section():
    start = PAGE.index('<div class="section" id="tools-section">')
    return PAGE[start:PAGE.index('<div class="sidebar-user-bar" id="sidebar-user-bar">', start)]


def test_a_handful_of_everyday_tools_and_one_folded_workshop():
    section = _tools_section()
    workshop_at = section.index('id="workshop-group"')
    everyday = re.findall(r'class="list-item" id="(tool-[\w-]+)"', section[:workshop_at])
    assert everyday == ["tool-gallery-btn", "tool-notes-btn", "tool-calendar-btn", "tool-kitchen-link", "tool-reader-link",
                        "tool-translate-link", "tool-almanac-link", "tool-mind-link"]
    inside = re.findall(r'class="list-item" id="(tool-[\w-]+)"', section[workshop_at:])
    assert inside == ["tool-cookbook-btn", "tool-engine-btn", "tool-control-btn", "tool-research-btn", "tool-compare-btn", "tool-recipes-btn",
                      "tool-tasks-btn", "tool-library-btn", "tool-memory-btn", "tool-theme-btn", "tool-people-link"]
    assert 'id="workshop-items" hidden>' in section and 'aria-controls="workshop-items"' in section       # folded until opened
    assert "localStorage.setItem('aegis-workshop-open'" in APP_JS


def test_names_say_what_things_are():
    section = _tools_section()
    for row, label in (("tool-tasks-btn", "Scheduled"), ("tool-library-btn", "Documents"), ("tool-memory-btn", "Skills and memory tools"),
                       ("tool-mind-link", "Memory")):
        assert re.search(rf'id="{row}"[^>]*>(?:(?!</div>|</a>).)*<span class="grow">{label}</span>', section, re.S), row
    assert 'id="tool-mind-link" href="/mind"' in section                      # one memory screen: the memory page
    assert ">Brain<" not in PAGE and '<span class="grow">Tasks</span>' not in PAGE and '<span class="grow">Library</span>' not in PAGE


# ── nobody is asked to decide what the machine can ─────────────────────────

def test_the_web_is_not_a_button_and_tools_are_not_a_position():
    assert re.search(r'id="web-toggle-btn"[^>]*\bhidden\b', PAGE) and "#web-toggle-btn { display: none !important; }" in STYLE
    assert 'data-ui-key="web-toggle-btn"' not in PAGE                          # nor a switch for a button that is gone
    switch = PAGE[PAGE.index('<div class="mode-toggle" data-admin-only>'):]
    switch = switch[:switch.index("</div>")]
    assert re.findall(r'id="(mode-[a-z]+-btn)"', switch) == ["mode-agent-btn", "mode-code-btn", "mode-chat-btn"]
    assert ">Auto</button>" in switch and ">Code</button>" in switch and ">No tools</button>" in switch and ">Chat</button>" not in switch
    assert "#mode-chat-btn:not(.active) { display: none !important; }" in STYLE          # shown only while it is on
    assert 'id="set-no-tools"' in PAGE and "__aegisSetChatMode(box.checked ? 'chat' : 'agent')" in APP_JS


def test_a_browser_that_never_chose_starts_on_auto():
    for script in ("app.js", "js/chat.js"):
        source = (REPO / "static" / script).read_text(encoding="utf-8")
        assert not re.search(r"mode\)?\s*\|\|\s*'chat'", source), script
    assert "let currentMode = state.mode || 'agent';" in APP_JS


def test_the_web_being_allowed_is_not_a_reason_to_keep_tools():
    routes = (REPO / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    assert 'search_on=str(use_web).lower() == "true",' in routes
    assert "if _search_enabled and _web_wanted:\n                        _forced_tools = set(WEB_TOOL_NAMES)" in routes
    from services import router
    assert router.decide(router.Turn(message="thanks!", mode="agent")).path == "chat"
    assert router.decide(router.Turn(message="thanks!", mode="agent", search_on=True)).path == "agent"


# ── the workings ───────────────────────────────────────────────────────────

def test_the_workings_are_off_until_an_administrator_switches_them_on():
    assert "'show-workings']);" in APP_JS and "state['show-workings'] === true && document.documentElement.dataset.admin === '1'" in APP_JS
    row = PAGE[PAGE.index('data-ui-key="show-workings"') - 900:PAGE.index('data-ui-key="show-workings"') + 60]
    assert 'class="vis-row" data-admin-only' in row and "Show the workings" in row and "checked" not in row.split("data-ui-key")[-2][-40:]
    for hidden in (".response-metrics", ".ctx-ring", ".role-provider-logo"):
        assert f"body:not(.show-workings) {hidden}" in STYLE, hidden
    assert "body:not(.show-workings) .agent-thread-tool[data-plain]::after" in STYLE
    # The thinking is not one of the workings: hidden with them, a reply showed nothing at all while
    # the model thought. It has its own switch, on for everyone (tests/test_signs_of_life.py).
    assert "body:not(.show-workings) .thinking-section" not in STYLE


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_a_step_says_in_plain_words_what_was_used():
    out = subprocess.run(["node", "--input-type=module", "-e", """
        const { plainName } = await import('./static/js/workings.js');
        console.log(JSON.stringify(['LIBRARY_SEARCH', 'sun_moon', 'weather', 'manage_lists', 'web_search', 'identify_photo', 'mcp__email__list', 'frob_widget', ''].map(plainName)));
    """], cwd=REPO, check=True, capture_output=True, text=True, encoding="utf-8")
    assert json.loads(out.stdout) == ["The home library", "Almanac: sun and moon", "Weather forecast", "Lists", "The web", "Photo identification",
                                      "A connected tool", "Frob widget", "A step"]
