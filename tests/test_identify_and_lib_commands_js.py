"""/identify and /lib in the chat UI.

slashCommands.js is a browser ES module with a heavy import chain (it cannot
be node-imported in isolation), so — per the repo's DOM-coupled-guard
convention — these assert the wiring that matters is present in the source.
"""
import re
from pathlib import Path

SRC = Path("static/js/slashCommands.js").read_text(encoding="utf-8")
HTML = Path("static/index.html").read_text(encoding="utf-8")


def _registry():
    start = SRC.index("const COMMANDS = {")
    end = SRC.index("const LEGACY_ALIASES", start)
    return SRC[start:end]


def _function(name):
    match = re.search(rf"\n(?:async\s+)?function {re.escape(name)}\(", SRC)
    assert match, f"{name} not found"
    rest = SRC[match.end():]
    nxt = re.search(r"\n(?:export\s+)?(?:async\s+)?function |\n// -{20,}|\n/\*\*", rest)
    return rest[: nxt.start()] if nxt else rest


def test_commands_are_registered_with_their_handlers():
    registry = _registry()
    identify = registry[registry.index("\n  identify: {"):]
    identify = identify[: identify.index("\n  },") + 5]
    assert "_cmdIdentify(args)" in identify and "noUserBubble: true" in identify
    lib = registry[registry.index("\n  lib: {"):]
    lib = lib[: lib.index("\n  },") + 5]
    assert "_cmdLib(args)" in lib


def test_names_and_aliases_do_not_collide_with_existing_commands():
    registry = _registry()
    # Top-level command keys sit at two spaces of indent.
    names = re.findall(r"^  ['\"]?([a-z0-9][a-z0-9_-]*)['\"]?: \{", registry, flags=re.M)
    mine = {"identify": ["id", "whatis", "ocr"], "lib": ["kiwix", "lookup", "ref", "offline"]}
    for name in mine:
        assert names.count(name) == 1, f"/{name} registered {names.count(name)} times"
    used = {}
    for match in re.finditer(r"alias: \[([^\]]*)\]", registry):
        for alias in re.findall(r"'([^']+)'", match.group(1)):
            used[alias] = used.get(alias, 0) + 1
    for name, aliases in mine.items():
        for alias in aliases:
            assert used.get(alias) == 1, f"alias {alias!r} of /{name} is used {used.get(alias)} times"
            assert alias not in names, f"alias {alias!r} shadows a command"
    # /library stays the Documents panel.
    assert "library" in names and "lib" not in mine["identify"]


def test_identification_is_saved_as_conversation_not_as_slash_chatter():
    body = _function("identifyPhoto")
    # source:'slash' messages are dropped from the model's context
    # (core/models.py get_context_messages); the question and the answer must
    # stay in it so follow-up questions work.
    assert "source: 'slash'" not in body
    assert "_persistMsg('user', userText, { attachments, source: 'identify' })" in body
    assert "_persistMsg('assistant', d.markdown, metadata)" in body
    assert "metadata.web_sources = sources" in body            # sources box survives a reload
    assert "/api/upload" in body and "/api/reference/identify" in body
    assert "_identifyBusy" in body


def test_lookup_list_is_slash_chatter():
    body = _function("_cmdLib")
    assert "/api/reference/search" in body and "/api/reference/status" in body
    assert "_persistMsg('assistant', md, { source: 'slash' })" in body


def test_identify_command_does_not_block_the_composer_on_the_photo_chooser():
    body = _function("_cmdIdentify")
    code = "\n".join(line.split("//", 1)[0] for line in body.splitlines())      # comments aside
    assert not re.search(r"\bawait\b", code), "the handler must return before the user has picked a photo"
    assert "pickAndIdentify(hint)" in code and "identifyPhoto(attached, hint)" in code
    assert "return true;" in code


def test_photo_chooser_accepts_images_and_handles_dismissal():
    body = _function("_pickPhoto")
    assert "input.accept = 'image/*'" in body
    assert "addEventListener('cancel'" in body and "addEventListener('change'" in body
    assert "capture" not in body, "no capture attribute: the phone should offer camera AND gallery"


def test_composer_menu_item_exists_and_is_wired_inside_the_click():
    assert 'id="overflow-identify-btn"' in HTML and "<span>Identify photo</span>" in HTML
    init = SRC[SRC.index("export function initSlashCommands(deps)"):]
    init = init[: init.index("\n}\n")]
    assert "getElementById('overflow-identify-btn')" in init
    assert "pickAndIdentify(hint)" in init
    assert "'identify-btn':        '#overflow-identify-btn'" in Path("static/app.js").read_text(encoding="utf-8")


def test_library_tool_has_a_label_and_a_settings_entry():
    chat = Path("static/js/chat.js").read_text(encoding="utf-8")
    assert "'library_search': 'Searching the library'" in chat
    assert '<option value="kiwix"' in HTML
    settings = Path("static/js/settings.js").read_text(encoding="utf-8")
    assert "kiwix: 'Offline library'" in settings
