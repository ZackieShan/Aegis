"""Regression tests for the <function=NAME>/<parameter=NAME> tool-call format.

Qwen2.5/Qwen3-Coder and some Hermes variants intermittently emit tool calls in
this "equals" XML form as *text* instead of a native tool_call. Before the fix
the agent parsed 0 tool blocks and the turn dead-ended (the "it just stalls"
report). These lock in that the calls are now caught and their args preserved.
"""
import src.agent_tools  # noqa: F401  (complete the import cycle first)
from src.tool_parsing import parse_tool_blocks, _normalize_function_xml


def test_normalize_function_xml_to_invoke():
    t = "<function=create_document>\n<parameter=title>X</parameter>\n</function>"
    out = _normalize_function_xml(t)
    assert '<invoke name="create_document">' in out
    assert '<parameter name="title">' in out
    assert "</invoke>" in out


def test_normalize_is_noop_on_plain_text():
    t = "Here is how you would call a function normally in Python."
    assert _normalize_function_xml(t) == t


def test_function_xml_flat_params_parsed():
    t = ("I'll create it.\n<function=create_document>\n"
         "<parameter=title>My Doc</parameter>\n"
         "<parameter=content>Hello world</parameter>\n</function>")
    blocks = parse_tool_blocks(t, skip_fenced=True)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "create_document"
    assert "My Doc" in blocks[0].content and "Hello world" in blocks[0].content


def test_function_xml_edit_document_edits_json_string():
    # edits arrive as a JSON *string* via the XML path — must still parse.
    t = ('<function=edit_document>\n'
         '<parameter=edits>[{"find": "## Value", "replace": "## Cool Factor"}]</parameter>\n'
         '</function>')
    blocks = parse_tool_blocks(t, skip_fenced=True)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "edit_document"
    assert "<<<FIND>>>" in blocks[0].content
    assert "## Value" in blocks[0].content
    assert "## Cool Factor" in blocks[0].content


def test_function_xml_does_not_break_native_invoke():
    # The existing <invoke name="..."> form keeps working unchanged.
    t = '<invoke name="update_document">\n<parameter name="content">new body</parameter>\n</invoke>'
    blocks = parse_tool_blocks(t, skip_fenced=True)
    assert len(blocks) == 1 and blocks[0].tool_type == "update_document"
    assert blocks[0].content == "new body"


def test_prose_without_markup_yields_nothing():
    assert parse_tool_blocks("Just a normal reply, no tools here.", skip_fenced=True) == []
