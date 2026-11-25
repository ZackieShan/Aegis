"""Per-request thinking override (2026-07-18).

The chat UI's model-settings toggle sets a contextvar; the llm_core payload
builders encode it for LOCAL, thinking-capable models. Because a localhost /v1
URL can't distinguish llama.cpp (reads chat_template_kwargs.enable_thinking)
from a custom-port Ollama (reads a top-level `think`), the applier sets BOTH —
each engine ignores the knob it doesn't use. Cloud endpoints and non-thinking
models are never touched.
"""
from src import llm_core


def _apply(url, model, want):
    payload = {"model": model}
    llm_core.set_think_override(want)
    try:
        llm_core._apply_think_override(payload, url, model)
    finally:
        llm_core.set_think_override(None)
    return payload


def test_local_thinking_model_sets_both_knobs():
    p = _apply("http://127.0.0.1:9090/v1", "qwen3.6-35b-a3b", True)
    assert p.get("chat_template_kwargs") == {"enable_thinking": True}
    assert p.get("think") is True
    p = _apply("http://127.0.0.1:9090/v1", "qwen3.6-35b-a3b", False)
    assert p["chat_template_kwargs"] == {"enable_thinking": False}
    assert p["think"] is False


def test_ollama_native_url_gets_knobs():
    # Native Ollama (http://localhost:11434) must still receive `think`.
    p = _apply("http://127.0.0.1:11434", "qwen3:8b", True)
    assert p.get("think") is True


def test_auto_none_is_noop():
    p = _apply("http://127.0.0.1:9090/v1", "qwen3.6-35b-a3b", None)
    assert "chat_template_kwargs" not in p and "think" not in p


def test_non_thinking_model_ignored():
    # A diffusion / non-reasoning model must never get a thinking knob.
    p = _apply("http://127.0.0.1:9090/v1", "qwen-image", True)
    assert "chat_template_kwargs" not in p and "think" not in p


def test_cloud_endpoint_ignored():
    p = _apply("https://api.openai.com/v1", "qwen3.6-35b-a3b", True)
    assert "chat_template_kwargs" not in p and "think" not in p


def test_preserves_existing_chat_template_kwargs():
    payload = {"model": "qwen3.6-35b-a3b", "chat_template_kwargs": {"foo": "bar"}}
    llm_core.set_think_override(True)
    try:
        llm_core._apply_think_override(payload, "http://127.0.0.1:9090/v1", "qwen3.6-35b-a3b")
    finally:
        llm_core.set_think_override(None)
    assert payload["chat_template_kwargs"] == {"foo": "bar", "enable_thinking": True}


def test_override_is_request_local():
    # A stale override must not linger: setting None restores default behavior.
    llm_core.set_think_override(True)
    llm_core.set_think_override(None)
    p = {"model": "qwen3.6-35b-a3b"}
    llm_core._apply_think_override(p, "http://127.0.0.1:9090/v1", "qwen3.6-35b-a3b")
    assert "chat_template_kwargs" not in p and "think" not in p


# ── effort-steered models (gpt-oss): the toggle maps to reasoning_effort ─────

def test_gpt_oss_toggle_maps_to_reasoning_effort():
    """gpt-oss has no enable_thinking switch — only low/medium/high. Sending it
    the boolean knobs did nothing, so the UI toggle was dead for this family."""
    on = _apply("http://127.0.0.1:9090/v1", "gpt-oss-120b", True)
    assert on == {"model": "gpt-oss-120b", "reasoning_effort": "high"}
    off = _apply("http://127.0.0.1:9090/v1", "gpt-oss-120b-fast", False)
    assert off == {"model": "gpt-oss-120b-fast", "reasoning_effort": "low"}
    # no boolean thinking knobs for this family
    assert "think" not in on and "chat_template_kwargs" not in on


def test_gpt_oss_auto_and_cloud_are_untouched():
    assert _apply("http://127.0.0.1:9090/v1", "gpt-oss-120b", None) == {"model": "gpt-oss-120b"}
    assert _apply("https://openrouter.ai/api/v1", "openai/gpt-oss-120b", True) == {"model": "openai/gpt-oss-120b"}


def test_gpt_oss_explicit_effort_from_the_caller_wins():
    payload = {"model": "gpt-oss-120b", "reasoning_effort": "medium"}
    llm_core.set_think_override(True)
    try:
        llm_core._apply_think_override(payload, "http://127.0.0.1:9090/v1", "gpt-oss-120b")
    finally:
        llm_core.set_think_override(None)
    assert payload["reasoning_effort"] == "medium"


def test_reasoning_effort_helper_only_matches_effort_models():
    assert llm_core._reasoning_effort_for("gpt-oss-120b", True) == "high"
    assert llm_core._reasoning_effort_for("GPT-OSS-20B", False) == "low"
    for other in ("qwen3.6-35b-a3b", "mistral-small-4", "gpt-4o", "", None):
        assert llm_core._reasoning_effort_for(other, True) is None
