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


def test_near_repeat_text_helper():
    a = al._round_text_words("I'll create a photorealistic image of a mountain lake at sunrise for you.")
    b = al._round_text_words("I need to generate a photorealistic image of a mountain lake at sunrise for you.")
    c = al._round_text_words("Now checking the official documentation for the API limits.")
    assert al._is_near_repeat_text(b, [a])
    assert not al._is_near_repeat_text(c, [a])
    assert not al._is_near_repeat_text(set(), [a])
