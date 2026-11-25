"""Deep Research has to be reachable from a chat without typing a command.

It used to be: the sidebar entry (behind the menu button on a phone), or a
typed command. The message box had only an indicator that appears once
research is already switched on, the "More tools" entry the scripts expect was
missing from the page, and the chat itself was barred from starting a job
however plainly it was asked. These keep the three ways in:

  * a button on the message box and an entry in "More tools" that open the
    Deep Research window, carrying over what is typed;
  * asking for it by name in Auto ("do deep research on …") starts a job;
  * an ordinary question still cannot start one, and the model is no longer
    told "the internet is off" just because it may not start a research job.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "static"


def _read(*parts):
    return STATIC.joinpath(*parts).read_text(encoding="utf-8")


def test_the_message_box_has_a_research_button_before_the_switches():
    page = _read("index.html")
    button = re.search(r'<button[^>]*id="composer-research-btn"[^>]*>', page)
    assert button, "no Deep Research button on the message box"
    assert 'aria-label="Deep Research"' in button.group(0) and "title=" in button.group(0)
    assert "display:none" not in button.group(0)                       # there all the time, not only once research is on
    left = page[page.index('id="composer-photo-btn"'):page.index('id="bash-toggle-btn"')]
    assert left.index('id="composer-research-btn"') < left.index('id="web-toggle-btn"')


def test_more_tools_lists_deep_research():
    page = _read("index.html")
    menu = page[page.index('id="overflow-menu"'):page.index('id="composer-photo-btn"')]
    entry = re.search(r'<button[^>]*class="overflow-menu-item"[^>]*id="overflow-deepresearch-btn"[^>]*>(.*?)</button>', menu, re.S)
    assert entry and "Deep research" in entry.group(1)


def test_both_open_the_research_window_and_carry_over_what_is_typed():
    script = _read("js", "researchButton.js")
    assert "'composer-research-btn'" in script and "'overflow-deepresearch-btn'" in script
    assert "window.researchPanelModule" in script and "openPanel()" in script       # the same window the sidebar opens
    assert "import " not in script.split("const $")[0]                               # no second copy of the window's module
    assert "$('research-query')" in script and "topic.value = typed" in script
    assert "startsWith('/')" in script                                               # a typed command is left alone
    app = _read("app.js")
    assert "import { initResearchButton } from './js/researchButton.js';" in app
    assert "initResearchButton();" in app
    assert "window.researchPanelModule = researchPanelModule;" in app


def test_the_shortcut_follows_the_permission_and_the_appearance_switch():
    init = _read("js", "init.js")
    line = next(l for l in init.splitlines() if "privs.can_use_research)" in l)
    assert "#composer-research-btn" in line and "#overflow-deepresearch-btn" in line
    assert "window._aegisResearchAllowed = false" in init
    app = _read("app.js")
    row = next(l for l in app.splitlines() if l.strip().startswith("'research-btn':"))
    assert "#composer-research-btn" in row and "#overflow-deepresearch-btn" in row
    # the default-off "deep_research" feature flag does not hide it (the sidebar entry ignores that flag too)
    flags = next(l for l in app.splitlines() if l.strip().startswith("deep_research:"))
    assert "composer-research-btn" not in flags
    assert "#composer-research-btn[hidden]" in _read("style.css")


def test_the_chat_may_start_research_only_when_asked_by_name():
    route = (REPO / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    block = route[route.index('disabled_tools.add("trigger_research")'):]
    block = block[:block.index("# Only disable bash when the caller")]
    assert "_may_start_research(message, _mode_as_sent, code_mode)" in block
    assert 'disabled_tools.discard("trigger_research")' in block
    # an account without the research permission never gets the tool
    privileges = route[route.index('if not _privs.get("can_use_research", True):'):][:200]
    assert 'disabled_tools.add("trigger_research")' in privileges
    assert "research_requested=bool(_research_asked" in route


def test_the_model_is_told_the_truth_about_research_and_the_web():
    loop = (REPO / "src" / "agent_loop.py").read_text(encoding="utf-8")
    assert "research_requested: bool = False" in loop
    # web-off is about web tools only; research has its own note
    assert "_web_off = sorted(set(_relevant_tools) & set(WEB_TOOL_NAMES) & _off)" in loop
    assert '| {"trigger_research"}' not in loop
    assert "a Deep Research job cannot be started from this message" in loop
    assert "the user has asked for a Deep Research job by name" in loop
    # the assistant does not send people to a button (the family screen has none): the screen makes the offer itself
    assert "microscope button" not in loop and "the screen offers one under your answer by itself" in loop
    # the standing rule no longer promises a tool the chat usually does not have
    assert "when `trigger_research` is available this turn" in loop
    assert "never say a research job was started" in loop
