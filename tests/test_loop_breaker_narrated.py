"""Loop-breaker must catch NARRATED wrong-tool loops, not just silent ones.

Regression for 2026-07-12: asked for an image, the model (missing the
generate_image schema) called manage_memory with slightly different args every
round while narrating "I'll generate a photorealistic image..." each time. The
classic detectors never fired — the narration reset _stuck_rounds (it counted
as progress) and the varying args dodged both identical-signature checks — so
the loop burned all 20 rounds and wrote ~60 junk memories.

The narrated-circling detector (same tool TYPE as a recent round + narration
that is a near-repeat of a recent round's) must trip the loop-breaker within a
few rounds instead.
"""

import asyncio
import json

import src.agent_loop as al


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


# Same-plan narrations with light rephrasing — exactly what the qwen3-coder
# rounds looked like. The bash args differ every round so the identical-call
# signature detectors stay silent, as in the real incident.
_NARRATIONS = [
    "I'll create a photorealistic image of a mountain lake at sunrise for you. Let me use the image generation tool to create this.",
    "I need to generate a photorealistic image of a mountain lake at sunrise for you. Let me use the image generation tool for this task.",
    "I'll generate a photorealistic image of a mountain lake at sunrise for you right now using the image generation tool.",
]


def _patch_common(monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def _fake_exec(block, *a, **k):
        return ("bash", {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def test_narrated_circling_repeated_calls_trips_breaker(monkeypatch, caplog):
    """The model re-issues the SAME (already-seen) call behind recycled
    narration each round — genuine spinning, no new work. This must trip the
    loop-breaker even though the round writes narration text (which the
    identical-sig-no-text detector requires to be absent). The refined
    detector fires on: same tool type + near-repeat narration + NO new
    distinct call."""
    _patch_common(monkeypatch)
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        # Fixed command every round → after round 1 no call is "new".
        text = (
            f"{_NARRATIONS[calls['n'] % len(_NARRATIONS)]}\n\n"
            f"```bash\necho generating the image\n```"
        )
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "Generate an image of a mountain lake at sunrise, photorealistic."}],
        max_rounds=20,
        relevant_tools={"bash"},
    )
    with caplog.at_level("WARNING"):
        events = _events(_collect(gen))

    assert any("loop-breaker tripped" in r.message for r in caplog.records), (
        "narrated repeated-call loop was not detected — it would burn all 20 rounds"
    )
    tool_outputs = [e for e in events if e.get("type") == "tool_output"]
    assert len(tool_outputs) <= 8, f"still executed {len(tool_outputs)} spinning rounds"
    assert not any(e.get("type") == "rounds_exhausted" for e in events), events


def test_distinct_calls_with_similar_narration_not_flagged(monkeypatch, caplog):
    """The finder's concern: legit iterative work (build→test→fix) that runs a
    DISTINCT command each round with formulaic narration ("let me run that
    again") must NOT be force-answered — a new distinct call is real progress."""
    _patch_common(monkeypatch)
    calls = {"n": 0}
    _narr = [
        "Let me run the build again to check.",
        "Let me run the build once more to verify.",
        "Let me run the build again to confirm.",
    ]

    async def _fake_stream(_candidates, messages, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        if i < 3:
            # DISTINCT command each round (real iterative work) + similar narration.
            text = f"{_narr[i]}\n\n```bash\nmake build STEP={i}\n```"
        else:
            text = "Build succeeded on the third attempt — all targets are up to date."
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "build my project"}],
        max_rounds=20,
        relevant_tools={"bash"},
    )
    with caplog.at_level("WARNING"):
        events = _events(_collect(gen))

    assert not any("loop-breaker tripped" in r.message for r in caplog.records), (
        "distinct iterative work with formulaic narration was wrongly flagged as circling"
    )
    # All three distinct build steps ran, then it finished.
    assert len([e for e in events if e.get("type") == "tool_output"]) == 3


def test_distinct_work_with_fresh_narration_not_flagged(monkeypatch, caplog):
    """Legit multi-step flows (distinct calls, changing narration) must ride
    to completion untouched — same guarantee the original detector made."""
    _patch_common(monkeypatch)
    steps = [
        "Checking the repository layout first.",
        "Now inspecting the failing module for import errors.",
        "Running the test suite to confirm the fix works.",
        "All checks pass — the fix is verified and the import error is resolved.",
    ]
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        if i < 3:
            text = f"{steps[i]}\n\n```bash\nstep-command-{i}\n```"
        else:
            text = steps[3]
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "fix the import error in my project"}],
        max_rounds=20,
        relevant_tools={"bash"},
    )
    with caplog.at_level("WARNING"):
        events = _events(_collect(gen))

    assert not any("loop-breaker tripped" in r.message for r in caplog.records)
    assert len([e for e in events if e.get("type") == "tool_output"]) == 3


def test_arg_jitter_with_promissory_narration_trips_breaker(monkeypatch, caplog):
    """Regression for 2026-07-16: with no web tool available, the model called
    manage_notes with a slightly different query every round while writing a
    fresh 'Let me search...' one-liner each time. The value jitter dodged the
    exact-signature detectors (each round was a "new" call) and the varied
    phrasing dodged the 0.6 Jaccard near-repeat bar, so the loop ran 12+
    rounds until the user hit stop. Coarse work signatures (tool + action +
    arg KEYS) plus the promissory-text marker must trip the breaker within a
    few rounds."""
    _patch_common(monkeypatch)
    calls = {"n": 0}
    # Real narrations from the incident's app.log — all distinct phrasings.
    _narr = [
        "Let me search for recent news on these conflicts for you.",
        "Let me search for the latest on these conflicts.",
        "I don't have direct web search capabilities, but let me use the browser to gather recent news.",
        "Let me try to gather this information via the browser.",
        "Let me try to navigate to a news search to get the latest information.",
        "Let me use the browser to search for recent news on these conflicts.",
        "Let me use the browser to search for the latest news on these conflicts.",
    ]

    async def _fake_stream(_candidates, messages, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        # Different query value every round → new EXACT signature each time,
        # same coarse signature (manage_notes:search:action,query) throughout.
        args = json.dumps({"action": "search", "query": f"war news variant {i}"})
        text = f"{_narr[i % len(_narr)]}\n\n```manage_notes\n{args}\n```"
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "What's going on with the war in Ukraine this week?"}],
        max_rounds=20,
        relevant_tools={"manage_notes"},
    )
    with caplog.at_level("WARNING"):
        events = _events(_collect(gen))

    assert any("loop-breaker tripped" in r.message for r in caplog.records), (
        "arg-jitter + promissory-narration loop was not detected — it would run "
        "to max_rounds or a user interrupt, as in the 2026-07-16 incident"
    )
    tool_outputs = [e for e in events if e.get("type") == "tool_output"]
    assert len(tool_outputs) <= 8, f"still executed {len(tool_outputs)} spinning rounds"
    assert not any(e.get("type") == "rounds_exhausted" for e in events), events


def test_file_hunt_with_terse_narration_not_flagged(monkeypatch, caplog):
    """A multi-file hunt (read_file over DISTINCT paths) with terse promissory
    narration each round is legitimate exploration — distinct identifiers are
    new work, unlike reworded search queries, and must not trip the breaker."""
    _patch_common(monkeypatch)
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        if i < 5:
            args = json.dumps({"path": f"src/module_{i}.py"})
            text = f"Let me check the next file.\n\n```read_file\n{args}\n```"
        else:
            text = "Found it — the import error is in src/module_4.py, line 12."
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "find the import error in my project"}],
        max_rounds=20,
        relevant_tools={"read_file"},
    )
    with caplog.at_level("WARNING"):
        events = _events(_collect(gen))

    assert not any("loop-breaker tripped" in r.message for r in caplog.records), (
        "a legitimate multi-file hunt with terse narration was flagged as circling"
    )
    assert len([e for e in events if e.get("type") == "tool_output"]) == 5


def test_serial_write_batch_with_promissory_narration_not_flagged(monkeypatch, caplog):
    """'Add these 5 appointments' executed one create_event per round (the
    serial native-caller pattern of qwen-class models) with short bridging
    narration is a legitimate batch: a WRITE with different values is new
    work per call and must never read as circling."""
    _patch_common(monkeypatch)
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        if i < 5:
            args = json.dumps({
                "action": "create_event", "summary": f"Appointment {i}",
                "dtstart": f"2026-07-2{i} 10:00", "dtend": f"2026-07-2{i} 11:00",
            })
            text = f"Added appointment {i}. Now let me add the next one.\n\n```manage_calendar\n{args}\n```"
        else:
            text = "All five appointments are on your calendar."
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "Add these five appointments to my calendar"}],
        max_rounds=20,
        relevant_tools={"manage_calendar"},
    )
    with caplog.at_level("WARNING"):
        events = _events(_collect(gen))

    assert not any("loop-breaker tripped" in r.message for r in caplog.records), (
        "a legitimate serial write batch was flagged as circling"
    )
    assert len([e for e in events if e.get("type") == "tool_output"]) == 5


def test_long_prefix_bash_commands_not_flagged(monkeypatch, caplog):
    """Distinct bash commands sharing a long common prefix (a cd into the
    project, a venv pytest invocation) are distinct work — the coarse
    signature must not truncate them into one bucket."""
    _patch_common(monkeypatch)
    calls = {"n": 0}
    prefix = "cd <repo-root> && venv/Scripts/python.exe -m pytest"

    async def _fake_stream(_candidates, messages, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        if i < 4:
            text = f"Let me run the next test file.\n\n```bash\n{prefix} tests/test_{i}.py\n```"
        else:
            text = "All four test files pass — the fix is verified."
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "run the test files one by one"}],
        max_rounds=20,
        relevant_tools={"bash"},
    )
    with caplog.at_level("WARNING"):
        events = _events(_collect(gen))

    assert not any("loop-breaker tripped" in r.message for r in caplog.records), (
        "distinct long-prefix bash commands were wrongly collapsed into one signature"
    )
    assert len([e for e in events if e.get("type") == "tool_output"]) == 4


def test_near_repeat_text_helper():
    a = al._round_text_words("I'll create a photorealistic image of a mountain lake at sunrise for you.")
    b = al._round_text_words("I need to generate a photorealistic image of a mountain lake at sunrise for you.")
    c = al._round_text_words("Now checking the official documentation for the API limits.")
    assert al._is_near_repeat_text(b, [a])
    assert not al._is_near_repeat_text(c, [a])
    assert not al._is_near_repeat_text(set(), [a])
