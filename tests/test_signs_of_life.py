"""While an answer is on its way: the screen never stands still, and the wait itself is shorter.

Two things were wrong, found from two real chats on the home server:

  * In the full app, nothing was drawn while the model was thinking: its thinking box was one of
    "the workings" (hidden unless an administrator switched them on) and the waiting line had already
    been taken down. For five to thirty seconds a reply was a name over an empty box.
  * Every message waited eight to eleven seconds before the first sign of the model, because the
    model read the whole prompt again each time: the tools offered changed with every message (they
    are picked by likeness, and the eight nearest come back whatever the message says), what was
    recalled for a message sat ahead of the conversation, and the notes the model keeps between tool
    steps were stripped, so each tool result was read twice.

What is checked here:

  the wait is shorter
    - a chat keeps the tools it was offered, so the top of its prompt stays the same
    - a likeness-only match joins a chat's tools only above a floor; a keyword match always does
    - what was looked up for one message is placed beside that message, behind the conversation
    - a local gpt-oss keeps its notes on every tool step of the turn
  the wait is shown
    - the model server's reading progress reaches the browser as "reading" events
    - how much was read and how much was still cached is kept with the answer
    - the thinking is shown again, with its own switch, and is never the workings' to hide
    - while it thinks there is always a header that moves, even with the thinking switched off
    - a tool step that ends the thinking closes it, so the next step's is not taken for the answer
    - the family screen's waiting line: a moving mark, plain words, seconds, a bar, a glimpse of the thought

Every name and sentence in here is made up.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parents[1]
STYLE = (REPO / "static" / "style.css").read_text(encoding="utf-8")
PAGE = (REPO / "static" / "index.html").read_text(encoding="utf-8")
CHAT_JS = (REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8")
SPINNER_JS = (REPO / "static" / "js" / "spinner.js").read_text(encoding="utf-8")
MARKDOWN_JS = (REPO / "static" / "js" / "markdown.js").read_text(encoding="utf-8")
HOME_JS = (REPO / "static" / "lite" / "home.js").read_text(encoding="utf-8")
HOME_CSS = (REPO / "static" / "lite" / "home.css").read_text(encoding="utf-8")
CHAT_ROUTES = (REPO / "routes" / "chat_routes.py").read_text(encoding="utf-8")
AGENT_LOOP = (REPO / "src" / "agent_loop.py").read_text(encoding="utf-8")

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


@pytest.fixture(autouse=True)
def _clean_slate():
    from src import prompt_stability
    prompt_stability.forget()
    yield
    prompt_stability.forget()


# ── a chat keeps its tools ─────────────────────────────────────────────────

def test_a_chat_keeps_the_tools_it_was_offered():
    from src import prompt_stability as stability
    assert stability.settle("chat-1", {"web_search", "ask_user"}) == {"web_search", "ask_user"}       # nothing kept yet
    stability.remember("chat-1", tools={"web_search", "ask_user"})
    # the next message is about something else: what it calls for is added, nothing is taken away
    assert stability.settle("chat-1", {"manage_lists", "ask_user"}) == {"web_search", "manage_lists", "ask_user"}
    # and a message that calls for nothing new leaves the list exactly as it was
    stability.remember("chat-1", tools={"web_search", "manage_lists", "ask_user"})
    assert stability.settle("chat-1", {"ask_user"}) == {"web_search", "manage_lists", "ask_user"}
    # another chat knows nothing of it
    assert stability.settle("chat-2", {"ask_user"}) == {"ask_user"}


def test_keeping_never_brings_back_what_is_switched_off():
    from src import prompt_stability as stability
    stability.remember("chat-1", tools={"bash", "web_search", "manage_memory"})
    assert stability.settle("chat-1", {"ask_user"}, blocked={"bash", "manage_memory"}) == {"web_search", "ask_user"}


def test_no_picking_in_force_is_left_alone_and_a_crowded_chat_starts_again():
    from src import prompt_stability as stability
    stability.remember("chat-1", tools={"a"})
    assert stability.settle("chat-1", None) is None                      # everything is on offer: nothing to settle
    assert stability.settle(None, {"a"}) == {"a"}                        # no chat: nothing to keep
    stability.remember("big", tools={f"tool_{i}" for i in range(stability.MAX_TOOLS)})
    assert stability.settle("big", {"one_more"}) == {"one_more"}         # past the limit the fresh pick stands alone


def test_flags_once_set_stay_for_the_chat_and_old_chats_are_let_go():
    from src import prompt_stability as stability
    stability.remember("chat-1", admin=True)
    stability.remember("chat-1", tools={"a"}, admin=False, email_style=False)
    kept = stability.recall("chat-1")
    assert kept["admin"] is True and kept["email_style"] is False and kept["tools"] == {"a"}
    for n in range(stability.MAX_CHATS + 5):
        stability.remember(f"s{n}", tools={"a"})
    assert stability.recall("chat-1")["tools"] == set()                  # the oldest were dropped


def test_it_can_be_switched_off(monkeypatch):
    from src import prompt_stability as stability
    stability.remember("chat-1", tools={"a"}, admin=True)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: False if key == "keep_prompt_stable" else default)
    assert stability.enabled() is False
    assert stability.settle("chat-1", {"b"}) == {"b"} and stability.recall("chat-1")["admin"] is False
    assert stability.floor_for("chat-1") is None


def test_a_first_message_picks_freely_and_later_ones_need_a_real_match():
    from src import prompt_stability as stability
    assert stability.floor_for("chat-1") is None
    stability.remember("chat-1", tools={"ask_user"})
    assert stability.floor_for("chat-1") == stability.ADD_FLOOR and 0.15 < stability.ADD_FLOOR < 0.3


def test_once_a_chat_is_under_way_auto_leaves_its_prompt_alone():
    from src import prompt_stability as stability
    assert stability.under_way("chat-1") is False and stability.under_way(None) is False
    stability.remember("chat-1", admin=True)
    assert stability.under_way("chat-1") is False                        # no turn with tools yet
    stability.remember("chat-1", tools={"ask_user"})
    assert stability.under_way("chat-1") is True
    # the route: no quick lane and no lower effort then; more thought for a hard question is still given
    at = CHAT_ROUTES.index("_chat_under_way = _stability_route.under_way(session)")
    rule = CHAT_ROUTES[at:at + 520]
    assert 'if chat_mode == "agent" and _auto_route.path == "chat" and not _chat_under_way:' in rule
    assert '(_auto_route.effort == "high" or not _chat_under_way)' in rule


class _Lane:
    """An embedding lane that answers every query with the same scored tools."""
    name = "fastembed"

    def __init__(self, scored):
        self._scored = scored
        self.collection = self

    def count(self):
        return len(self._scored)

    def encode(self, texts):
        return [[0.0]]

    def query(self, query_embeddings, n_results, include):
        rows = self._scored[:n_results]
        return {"metadatas": [[{"tool_name": name} for name, _ in rows]], "distances": [[1.0 - score for _, score in rows]]}


def _index(scored):
    from src.tool_index import ToolIndex
    index = ToolIndex.__new__(ToolIndex)
    index._lanes = [_Lane(scored)]
    return index


def test_the_nearest_tools_come_back_whatever_the_message_says_unless_a_floor_is_given():
    index = _index([("bulk_email", 0.62), ("manage_kitchen", 0.25), ("serve_preset", 0.13), ("map_position", 0.09)])
    assert index.retrieve("thanks", k=8) == ["bulk_email", "manage_kitchen", "serve_preset", "map_position"]
    assert index.retrieve("thanks", k=8, min_score=0.2) == ["bulk_email", "manage_kitchen"]
    assert index.retrieve("thanks", k=8, min_score=0.9) == []


def test_a_tool_named_by_a_word_in_the_message_is_added_whatever_its_score(monkeypatch):
    from src import tool_index
    monkeypatch.setattr(tool_index, "_library_ready", lambda: False)
    index = _index([("serve_preset", 0.11)])
    picked = index.get_tools_for_query("add oat milk to my shopping list", 8, None, 0.2)
    assert "manage_lists" in picked and "serve_preset" not in picked
    assert "serve_preset" in index.get_tools_for_query("add oat milk to my shopping list", 8)     # a first message: no floor


def test_the_agent_loop_settles_the_tools_before_the_prunes_and_remembers_them_after():
    settle = AGENT_LOOP.index("_stability.settle(session_id, _relevant_tools")
    prune = AGENT_LOOP.index("active email draft pruned fetch tools")
    remember = AGENT_LOOP.index("_stability.remember(session_id, tools=_relevant_tools, admin=_needs_admin)")
    assert settle < prune < remember
    # a caller that chose the tools itself (a scheduled task) and a guide-only turn are left alone
    assert "_settle_tools = bool(session_id) and not guide_only and not relevant_tools" in AGENT_LOOP
    # what is always on offer and missing from the pick was taken out on purpose
    assert "_taken_out = set(_always) - set(_relevant_tools)" in AGENT_LOOP


# ── what was looked up for one message sits beside it ──────────────────────

def test_context_for_one_message_is_marked():
    from src.prompt_security import for_this_turn, is_for_this_turn, untrusted_context_message
    block = untrusted_context_message("saved memory: retrieved context", "Likes rye bread.")
    assert not is_for_this_turn(block)
    assert for_this_turn(block) is block and is_for_this_turn(block)
    assert block["metadata"]["trusted"] is False                              # still untrusted data
    assert not is_for_this_turn({"role": "user", "content": "hello"}) and not is_for_this_turn(None)


def test_which_memory_blocks_are_for_the_message_and_which_stay_put():
    source = (REPO / "src" / "chat_processor.py").read_text(encoding="utf-8")
    for label in ('"saved memory: retrieved context"', '"deep memory: topic notes"', '"deep memory: earlier conversations"',
                  '"retrieved documents"', '"web search results"'):
        at = source.index(label)
        assert "for_this_turn(untrusted_context_message(" in source[at - 120:at + len(label)], label
    for label in ('"deep memory: recent conversations"', '"saved memory: pinned user facts"', '"available skills index"'):
        at = source.index(label)
        assert "for_this_turn(" not in source[at - 120:at], label


def _chat_helpers(monkeypatch):
    for name in ("starlette.middleware", "starlette.middleware.base", "core.models", "core.database", "routes.prefs_routes",
                 "routes.research_routes", "src.llm_core", "src.context_compactor", "src.model_context", "src.auth_helpers"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, MagicMock())
    return importlib.import_module("routes.chat_helpers")


def _harness(monkeypatch, helpers, recalled):
    """build_chat_context with a made-up session and a preface that holds one block that stays (the profile)
    and whatever was recalled for this message."""
    from src.prompt_security import for_this_turn, untrusted_context_message

    async def preprocess(chat_handler, message, att_ids, sess, **kwargs):
        return helpers.PreprocessedMessage(enhanced_message=message, user_content=message, text_for_context=message,
                                           youtube_transcripts=[], attachment_meta=[])

    async def no_compaction(sess, endpoint_url, model, messages, headers, owner=None):
        return messages, 8192, False

    monkeypatch.setattr(helpers, "preprocess", preprocess)
    monkeypatch.setattr(helpers, "extract_preset", lambda handler, preset_id: helpers.PresetInfo(
        temperature=0.7, max_tokens=1024, system_prompt="You are the house assistant.", character_name=None))
    monkeypatch.setattr(helpers, "add_user_message", lambda sess, handler, pre, incognito=False: sess.messages.append(
        {"role": "user", "content": pre.user_content}))
    monkeypatch.setattr(helpers, "load_prefs_for_user", lambda user: {})
    monkeypatch.setattr(helpers, "effective_user", lambda request: "tester")
    monkeypatch.setattr(helpers, "normalize_model_id", lambda endpoint_url, model, **kwargs: None)
    monkeypatch.setattr(helpers, "maybe_compact", no_compaction)
    monkeypatch.setattr(helpers, "trim_for_context", lambda messages, context_length: messages)
    sess = SimpleNamespace(endpoint_url="http://192.168.1.50:1234/v1", model="test-model", headers={}, messages=[],
                           get_context_messages=lambda: list(sess.messages))

    def preface(**kwargs):
        blocks = [{"role": "system", "content": "You are the house assistant."},
                  untrusted_context_message("saved memory: user profile", "Bakes on Sundays.")]
        blocks += [for_this_turn(untrusted_context_message("saved memory: retrieved context", text)) for text in recalled(kwargs["message"])]
        return blocks, [], []

    return sess, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(build_context_preface=preface)


def _without_clock(messages):
    return [m for m in messages if "[Context" not in str(m.get("content"))]


@pytest.mark.asyncio
async def test_recalled_context_goes_beside_its_message_so_the_chat_before_it_stays_the_same(monkeypatch):
    helpers = _chat_helpers(monkeypatch)
    recalled = {"How long does rye flour keep?": ["Keeps rye flour in the cellar."], "And spelt?": ["Bought spelt in May."]}
    sess, request, handler, processor = _harness(monkeypatch, helpers, lambda message: recalled.get(message, []))

    first = await helpers.build_chat_context(sess=sess, request=request, chat_handler=handler, chat_processor=processor,
                                             message="How long does rye flour keep?", session_id="chat-1")
    one = _without_clock(first.messages)
    # instructions, what stays, then what was recalled for this message, then the message itself
    assert [m["role"] for m in one] == ["system", "user", "user", "user"]
    assert "user profile" in one[1]["content"] and "rye flour in the cellar" in one[2]["content"] and one[3]["content"] == "How long does rye flour keep?"
    assert [m.get("content") for m in first.preface] == [one[0]["content"], one[1]["content"]]      # the preface is what leads

    sess.messages.append({"role": "assistant", "content": "About three months, cool and dry."})
    second = await helpers.build_chat_context(sess=sess, request=request, chat_handler=handler, chat_processor=processor,
                                              message="And spelt?", session_id="chat-1")
    two = _without_clock(second.messages)
    assert "Bought spelt in May" in two[-2]["content"] and two[-1]["content"] == "And spelt?"
    # everything up to the first message is what the model read last time, byte for byte: it is not read again
    assert json.dumps(two[:2]) == json.dumps(one[:2])
    assert two[2] == {"role": "user", "content": "How long does rye flour keep?"} and two[3]["role"] == "assistant"
    assert not any("rye flour in the cellar" in str(m.get("content")) for m in two)                 # last message's recall is gone


@pytest.mark.asyncio
async def test_with_the_setting_off_the_old_order_is_kept(monkeypatch):
    helpers = _chat_helpers(monkeypatch)
    monkeypatch.setattr("src.prompt_stability.enabled", lambda: False)
    sess, request, handler, processor = _harness(monkeypatch, helpers, lambda message: ["Keeps rye flour in the cellar."])
    sess.messages += [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hello."}]
    ctx = await helpers.build_chat_context(sess=sess, request=request, chat_handler=handler, chat_processor=processor,
                                           message="How long does rye flour keep?", session_id="chat-1")
    order = _without_clock(ctx.messages)
    assert "rye flour in the cellar" in order[2]["content"] and order[3]["content"] == "Hello"      # ahead of the conversation


# ── the notes between tool steps ───────────────────────────────────────────

def _two_steps(keep):
    from src.agent_loop import _append_tool_results
    messages = [{"role": "user", "content": "What does the shelf say about rye?"}]
    call = lambda n: [{"id": f"call-{n}", "name": "library_search", "arguments": "{}"}]
    _append_tool_results(messages, "", call(1), ["one"], ["first result"], True, 1, round_reasoning="Search the shelf first.", keep_reasoning=keep)
    after_one = json.dumps(messages)
    _append_tool_results(messages, "", call(2), ["two"], ["second result"], True, 2, round_reasoning="Try a wider search.", keep_reasoning=keep)
    return messages, after_one


def test_a_local_gpt_oss_keeps_its_notes_so_each_tool_result_is_read_once():
    messages, after_one = _two_steps(keep=True)
    assert [m.get("reasoning_content") for m in messages if m["role"] == "assistant"] == ["Search the shelf first.", "Try a wider search."]
    # the prompt only grew at the end: what was sent after the first step is still its opening, unchanged
    assert json.dumps(messages).startswith(after_one[:-1])


def test_elsewhere_only_the_newest_step_keeps_them():
    messages, after_one = _two_steps(keep=False)
    assert [m.get("reasoning_content") for m in messages if m["role"] == "assistant"] == [None, "Try a wider search."]
    assert not json.dumps(messages).startswith(after_one[:-1])               # which is why the result after it was read again


def test_only_a_local_gpt_oss_keeps_them():
    at = AGENT_LOOP.index("_keep_step_reasoning = (")
    rule = AGENT_LOOP[at:at + 260]
    assert '"gpt-oss" in (model or "").lower()' in rule and "_is_local_endpoint(endpoint_url)" in rule and "enabled()" in rule
    assert "keep_reasoning=_keep_step_reasoning" in AGENT_LOOP


# ── the model server says how far it has read ──────────────────────────────

def test_a_reading_event_from_the_servers_progress_block():
    from src.llm_core import _reading_event
    event = json.loads(_reading_event({"total": 8397, "cache": 5973, "processed": 7057, "time_ms": 1200})[6:])
    assert event == {"type": "reading", "done": 7057, "total": 8397, "cached": 5973}
    assert json.loads(_reading_event({"total": 100, "cache": 500, "processed": 900})[6:]) == {"type": "reading", "done": 100, "total": 100, "cached": 100}
    assert _reading_event({"total": 0, "processed": 0}) is None and _reading_event(None) is None and _reading_event({"total": "x"}) is None


def test_progress_is_asked_of_a_server_at_home_only():
    from src import llm_core
    home, away = {}, {}
    llm_core._apply_local_progress(home, "http://127.0.0.1:9090/v1/chat/completions")
    llm_core._apply_local_progress(away, "https://api.openai.com/v1/chat/completions")
    assert home == {"return_progress": True} and away == {}


class _Lines:
    def __init__(self, lines):
        self._lines, self.status_code = lines, 200

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _Client:
    def __init__(self, lines):
        self._lines, self.sent = lines, []

    def stream(self, method, url, **kwargs):
        self.sent.append(kwargs.get("json"))
        lines = self._lines

        class _Context:
            async def __aenter__(self):
                return _Lines(lines)

            async def __aexit__(self, *exc):
                return False

        return _Context()


def _stream(monkeypatch, lines, **kwargs):
    from src import llm_core
    client = _Client(lines)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)

    async def run():
        events = []
        async for chunk in llm_core._stream_llm_inner("http://127.0.0.1:9090/v1/chat/completions", "gpt-oss-120b",
                                                      [{"role": "user", "content": "hi"}], **kwargs):
            for line in chunk.split("\n"):
                if line.startswith("data: ") and line[6:] != "[DONE]":
                    events.append(json.loads(line[6:]))
        return events

    return asyncio.run(run()), client


_READING = [
    "data: " + json.dumps({"choices": [{"delta": {"role": "assistant", "content": None}}], "prompt_progress": {"total": 5000, "cache": 0, "processed": 2048, "time_ms": 1700}}),
    "data: " + json.dumps({"choices": [{"delta": {"role": "assistant", "content": None}}], "prompt_progress": {"total": 5000, "cache": 0, "processed": 5000, "time_ms": 4200}}),
    "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "A plain question."}}]}),
    "data: " + json.dumps({"choices": [{"delta": {"content": "Hello."}}]}),
    "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 5000, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 0}},
                           "timings": {"prompt_n": 5000, "cache_n": 0, "prompt_per_second": 1190.0, "predicted_per_second": 47.0}}),
    "data: [DONE]",
]


def test_reading_progress_and_what_was_read_reach_the_caller(monkeypatch):
    events, client = _stream(monkeypatch, _READING, progress=True)
    assert client.sent[0].get("return_progress") is True
    assert [e for e in events if e.get("type") == "reading"] == [
        {"type": "reading", "done": 2048, "total": 5000, "cached": 0}, {"type": "reading", "done": 5000, "total": 5000, "cached": 0}]
    assert [e["delta"] for e in events if "delta" in e] == ["A plain question.", "Hello."]
    usage = next(e["data"] for e in events if e.get("type") == "usage")
    assert usage["read_tokens"] == 5000 and usage["cached_tokens"] == 0 and usage["input_tokens"] == 5000


def test_nobody_who_did_not_ask_gets_reading_events(monkeypatch):
    events, client = _stream(monkeypatch, _READING)
    assert "return_progress" not in client.sent[0]
    assert not [e for e in events if e.get("type") == "reading"]
    assert [e["delta"] for e in events if "delta" in e] == ["A plain question.", "Hello."]


def test_reading_is_not_an_answer_so_a_fallback_can_still_take_over(monkeypatch):
    from src import llm_core
    tried = []

    async def fake_stream(url, model, messages, headers=None, **kwargs):
        tried.append(model)
        if model == "first":
            yield 'data: {"type": "reading", "done": 10, "total": 900, "cached": 0}\n\n'
            yield 'event: error\ndata: {"error": "it fell over", "status": 500}\n\n'
            return
        yield 'data: {"delta": "From the second."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(llm_core, "stream_llm", fake_stream)

    async def run():
        return [chunk async for chunk in llm_core.stream_llm_with_fallback([("http://a/v1", "first", {}), ("http://b/v1", "second", {})], [])]

    chunks = asyncio.run(run())
    assert tried == ["first", "second"] and any("From the second." in chunk for chunk in chunks)


def test_the_chat_route_sends_the_signs_of_life_on():
    at = CHAT_ROUTES.index('"tool_start", "tool_output", "agent_step",')
    forwarded = CHAT_ROUTES[at:at + 900]
    for kind in ('"reading"', '"tool_progress"', '"budget_exceeded"'):
        assert kind in forwarded, kind
    assert 'if data.get("type") == "reading":' in CHAT_ROUTES and "progress=True" in CHAT_ROUTES          # plain chat mode too
    assert "progress=True," in AGENT_LOOP and 'elif data.get("type") == "reading":' in AGENT_LOOP


def test_what_was_read_and_how_long_it_thought_are_kept_with_the_answer():
    assert 'metrics["read_tokens"] = read_tokens' in AGENT_LOOP and 'metrics["cached_tokens"] = cached_tokens' in AGENT_LOOP
    assert CHAT_ROUTES.count('_metrics_to_save["thinking_time"] = round(_think_clock["total"], 1)') == 2    # chat mode and agent mode
    assert "_note_thinking(False)        # the thinking ended in a tool call" in CHAT_ROUTES


# ── the moving mark and the words beside it ────────────────────────────────

def _node(script):
    out = subprocess.run(["node", "--input-type=module", "-e", script], cwd=REPO, check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(out.stdout)


@needs_node
def test_seconds_the_last_thought_and_reading_progress():
    got = _node("""
        const { seconds, lastThought, reading, MARK_HTML } = await import('./static/js/working.js');
        const long = 'The question is about rye flour. It keeps about three months in a cool cupboard. Whole-grain flour goes rancid sooner because of the oil in the germ. '
          + 'A sealed tin in the cellar is better than the paper bag. I should say how to tell when it has turned, and give the freezer as the long-term answer.';
        console.log(JSON.stringify({
          seconds: [seconds(400), seconds(6200), seconds(59999), seconds(65000), seconds(120000)],
          short: lastThought('User asks'), tidy: lastThought('<think>**Plan**\\n- look in `the library`\\n- then answer</think>'),
          tail: lastThought(long, 120), whole: lastThought('It keeps about three months.'),
          reading: [reading({ done: 2048, total: 8397, cached: 0 }), reading({ done: 7057, total: 7573, cached: 5973 }), reading({ done: 100, total: 300, cached: 0 }), reading(null)],
          mark: MARK_HTML,
        }));
    """)
    assert got["seconds"] == ["", "6 s", "59 s", "1 min 5 s", "2 min"]
    assert got["short"] == "" and got["tidy"] == "Plan look in the library then answer" and got["whole"] == "It keeps about three months."
    assert len(got["tail"]) <= 121 and got["tail"].endswith("the long-term answer.") and "<" not in got["tail"]
    assert got["reading"][0]["percent"] == 24 and got["reading"][1] == {"percent": 68, "toRead": 1600, "label": "68%"}
    assert got["reading"][2] is None and got["reading"][3] is None           # too little to read for a bar to mean anything
    assert got["mark"].count("<i>") == 3


def test_the_mark_is_calm_for_those_who_ask_for_less_motion():
    module = (REPO / "static" / "js" / "working.js").read_text(encoding="utf-8")
    assert "@media (prefers-reduced-motion: reduce)" in module and "working-fade" in module
    assert "@keyframes working-bar" in module and "@keyframes working-shimmer" in module


def test_the_chats_waiting_line_is_the_mark_plain_words_seconds_and_a_bar():
    assert "import { ensureStyles, mark as workingMark, seconds as workingSeconds, bar as workingBar } from './working.js';" in SPINNER_JS
    assert "if (this.animation === 'wave') {\n      return this._createWorkingElement();" in SPINNER_JS and "setProgress(percent)" in SPINNER_JS
    assert "'Reading your message'" in CHAT_JS and "'Reading what it found'" in CHAT_JS and "spinner.setProgress(_read.percent)" in CHAT_JS
    for jargon in ("Still waiting for first token", "pre-filling context", "no tokens yet",
                   "updateMessage('Processing request')", "create('Initializing'"):
        assert jargon not in CHAT_JS, jargon
    assert "json.code === 'waking'" in CHAT_JS                                # a model that had to be loaded says so in the same line
    assert "▁▂▃" not in CHAT_JS and CHAT_JS.count("working.MARK_HTML") >= 3    # running steps and the thinking header use the mark


# ── the thinking is shown, and something always moves ──────────────────────

def test_the_thinking_has_its_own_switch_and_is_not_one_of_the_workings():
    row = PAGE[PAGE.index('data-ui-key="show-thinking"') - 700:PAGE.index('data-ui-key="show-thinking"') + 40]
    assert "Show the thinking" in row and "checked" in row and "data-admin-only" not in row[row.rindex("<label"):]
    assert "body:not(.show-workings) .thinking-section" not in STYLE
    workings = PAGE[PAGE.index("Show the workings"):PAGE.index("Show the workings") + 200]
    assert "thinking" not in workings.lower()


def test_with_the_thinking_off_its_header_still_moves_while_it_thinks():
    assert "body.hide-thinking .thinking-section:not(.is-live) { display: none !important; }" in STYLE
    assert "body.hide-thinking .thinking-section.is-live .thinking-content," in STYLE
    assert "body.hide-thinking .thinking-section { display: none" not in STYLE     # the rule that left a reply blank
    assert '<div class="thinking-section is-live">' in CHAT_JS and "${working.MARK_HTML}<span class=\"live-think-header-text working-label\">Thinking</span>" in CHAT_JS
    assert "_liveThinkSection.classList.remove('is-live');" in CHAT_JS


def test_thinking_folds_to_how_long_it_took():
    assert "export function thoughtLabel(secondsTaken)" in MARKDOWN_JS and "`Thought for ${s} s`" in MARKDOWN_JS
    assert '<span data-fixed="1">${label}</span>' in MARKDOWN_JS and "!label_el.dataset.fixed" in MARKDOWN_JS
    assert CHAT_JS.count("_settleLiveThinking(") == 4                              # defined once, used at each of the three ways it can end
    assert "_liveThinkContent.classList.remove('expanded', 'is-long');" in CHAT_JS
    # seconds only, unless the workings are shown
    at = CHAT_JS.index("function _formatThinkStats(seconds, tokenCount, done)")
    assert "show-workings" in CHAT_JS[at:at + 420] and "Math.floor(seconds) + ' s'" in CHAT_JS[at:at + 520]


def test_a_tool_step_closes_the_thinking_before_it():
    at = CHAT_JS.index("} else if (json.type === 'tool_start') {")
    head = CHAT_JS[at:at + 1100]
    assert "if (_thinkOpen) {" in head and "accumulated += '</think>';" in head and "_thinkOpen = false;" in head
    assert head.index("_thinkOpen = false;") < head.index("if (_isBg) continue;")       # also for a chat that is not on screen
    step = CHAT_JS[CHAT_JS.index("} else if (json.type === 'agent_step') {"):][:400]
    assert "if (_thinkOpen) { accumulated += '</think>';" in step


# ── the family screen ──────────────────────────────────────────────────────

def test_the_family_screens_waiting_line():
    assert "import working from '../js/working.js';" in HOME_JS
    line = HOME_JS[HOME_JS.index("function workingLine(message)"):HOME_JS.index("/** What was thought")]
    for part in ("working.mark()", "working-label", "working-secs", "working.bar()", "working-thought", "working.seconds(waited)",
                 "working.lastThought(message.thinking)"):
        assert part in line, part
    assert "status: 'Reading your message…'" in HOME_JS and "'Reading what it found…'" in HOME_JS
    assert "h('span', { class: 'spinner' }), m.status" not in HOME_JS               # the ring that turned beside a word that never changed
    assert "} else if (event.type === 'reading') {" in HOME_JS and "working.reading(event)" in HOME_JS
    assert ".bubble.them:empty { display: none; }" in HOME_CSS and ".working [hidden] { display: none; }" in HOME_CSS


def test_the_family_screen_shows_a_glimpse_of_the_thought_unless_switched_off():
    assert "localStorage.getItem('aegis-home-thinking') !== '0'" in HOME_JS            # on unless this phone said otherwise
    assert "S.showThinking && message.thinkingNow ? working.lastThought(message.thinking) : ''" in HOME_JS
    assert "function thinkingRow()" in HOME_JS and "thinkingRow()," in HOME_JS         # the switch, in the account sheet
    assert "Show what ${S.me.assistant} is thinking" in HOME_JS
    # afterwards it is one quiet line under the answer, for an answer that took real thought
    assert "String(message.thinking || '').trim().length > 40" in HOME_JS and "function showThought(message)" in HOME_JS
    assert "thoughtFor: Number(meta.thinking_time) || 0" in HOME_JS                    # and it is there when the chat is reopened
