"""An agent turn must not end by telling the user that something happened
when the tool that was supposed to do it failed.

Seen on 2026-11-22 with a local model: asked to add a document to the
library, it got two refusals from the tool, then wrote "…has been added to
the survival library". And after seven identical failing calls were stopped
by the loop-breaker, "write your best final answer" produced an invented
program output.

Two deterministic backstops (no second model involved):
  * when the turn is about to end and a change-making tool's last call
    failed, with no word of a problem in the reply, the model gets one round
    to retry or to say so;
  * when the loop-breaker stops a run of calls that all failed, its stop
    note says they failed and that nothing was done.
"""
import asyncio
import json

import src.agent_loop as al

ADD = '```manage_library\n{"action": "add", "urls": ["https://example.org/page"]}\n```'


def _run(monkeypatch, replies, outcomes, max_rounds=12, tools=("manage_library",)):
    """Drive the loop with scripted model replies and tool outcomes. Returns
    (streamed text, what the model was sent each round)."""
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    results = iter(outcomes)

    async def fake_exec(block, *a, **k):
        return (block.tool_type, next(results))

    seen = []
    script = iter(replies)

    async def fake_stream(_candidates, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        yield f'data: {json.dumps({"delta": next(script)})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "execute_tool_block", fake_exec, raising=False)
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)

    async def collect():
        return [c async for c in al.stream_agent_loop(
            "http://x/v1", "m", [{"role": "user", "content": "Add https://example.org/page to the survival library."}],
            max_rounds=max_rounds, relevant_tools=set(tools))]

    text = ""
    for chunk in asyncio.run(collect()):
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                event = json.loads(chunk[6:])
            except ValueError:
                continue
            if isinstance(event.get("delta"), str):
                text += event["delta"]
    return text, seen


def _system_notes(messages):
    return [m["content"] for m in messages if m.get("role") == "system"]


FAILED = {"error": "0 of 1 document(s) are in the library.\n- NOT added — that link is a web page, not a PDF file.", "exit_code": 1}
WORKED = {"output": "- Added [Field guide](/api/reference/added/general/guide.pdf) — 12 pages.", "exit_code": 0}


def test_a_reply_that_claims_success_after_a_failed_change_gets_one_round_to_put_it_right(monkeypatch):
    text, seen = _run(monkeypatch,
                      [ADD, "The PDF has been added to the survival library.",
                       "It was not added: that link is a web page, not a PDF file.", "unused"],
                      [FAILED])
    assert len(seen) == 3                                                    # one extra round, and only one
    note = [n for n in _system_notes(seen[2]) if "FAILED" in n]
    assert len(note) == 1 and "`manage_library`" in note[0] and "web page, not a PDF file" in note[0]
    assert "Do not say it succeeded" in note[0]
    assert "Let me double-check that." in text and text.rstrip().endswith("not a PDF file.")


def test_a_reply_that_already_says_it_failed_ends_the_turn(monkeypatch):
    text, seen = _run(monkeypatch, [ADD, "I couldn't add it: that link is a web page, not a PDF file."], [FAILED])
    assert len(seen) == 2 and "double-check" not in text
    assert not any("FAILED" in n for n in _system_notes(seen[-1]))


def test_a_later_successful_call_settles_an_earlier_failure(monkeypatch):
    text, seen = _run(monkeypatch, [ADD, ADD.replace("page", "guide.pdf"), "Added the field guide."], [FAILED, WORKED])
    assert len(seen) == 3 and "double-check" not in text


def test_a_failed_lookup_is_ordinary_and_does_not_hold_the_turn_open(monkeypatch):
    fetch = '```web_fetch\nhttps://example.org/missing\n```'
    text, seen = _run(monkeypatch, [fetch, "That page does not exist; here is what I know instead: nothing."],
                      [{"error": "HTTP 404", "exit_code": 1}], tools=("web_fetch",))
    assert len(seen) == 2
    assert al._unresolved_failures({"web_fetch": (False, "HTTP 404"), "library_search": (False, "x")}) == []
    assert al._unresolved_failures({"bash": (False, "exit 1"), "write_file": (True, "")}) == [("bash", "exit 1")]


def test_a_lookup_that_finds_nothing_is_an_answer_not_a_failed_change(monkeypatch):
    """"What's on my reminders list?" → no such list → the reply says so. Nothing
    was supposed to change, so nothing needs double-checking."""
    show = '```manage_lists\n{"action": "show", "list": "reminders"}\n```'
    text, seen = _run(monkeypatch, [show, "Your grocery list has eggs and bread."],
                      [{"error": "No list called reminders.", "exit_code": 1}], tools=("manage_lists",))
    assert len(seen) == 2 and "double-check" not in text
    assert al._is_lookup_call("manage_lists", '{"action": "show", "list": "x"}')
    assert not al._is_lookup_call("manage_lists", '{"action": "add", "list": "x", "items": ["a"]}')
    assert not al._is_lookup_call("bash", "ls -la")
    # The same tool failing at a change still counts.
    add = '```manage_lists\n{"action": "add", "list": "Groceries", "items": ["milk"]}\n```'
    text, seen = _run(monkeypatch, [add, "Milk is on your grocery list now.", "Sorry — the milk was not added: the list is archived."],
                      [{"error": "That list is archived.", "exit_code": 1}], tools=("manage_lists",))
    assert len(seen) == 3 and "double-check" in text


def test_what_counts_as_saying_so():
    for honest in ("I couldn't add it.", "It was not added because the site refused.", "The command failed with exit code 3.",
                   "Added the first one. However, the second link was a web page.", "Unfortunately the PC did not answer.",
                   "No reminder list was found — tell me its exact name.", "There is no list called Chores.",
                   "That file doesn't exist on the desktop."):
        assert al._admits_a_problem(honest), honest
    for rosy in ("The PDF has been added to the survival library.", "Created the folder, wrote hello.py. Output: hello from windows",
                 "Done! Your reminder is set for 3 pm."):
        assert not al._admits_a_problem(rosy), rosy


def test_the_loop_breaker_says_so_when_every_repeated_call_failed(monkeypatch):
    call = '```manage_hosts\n{"action": "run", "host": "desktop", "shell": "powershell"}\n```'
    text, seen = _run(monkeypatch, [call] * 12 + ["Nothing ran on the desktop: every attempt was refused for lacking a command."],
                      [{"error": "Nothing to run — give the command.", "exit_code": 1}] * 12, max_rounds=20, tools=("manage_hosts",))
    stop = [n for messages in seen for n in _system_notes(messages) if "STOP calling tools" in n]
    assert stop, "the loop-breaker never fired"
    assert "all FAILED" in stop[-1] and "Nothing to run" in stop[-1] and "NOT done" in stop[-1]
    assert "invent results" in stop[-1]
    assert len(seen) < 12
