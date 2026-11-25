"""Chat routes — /api/chat, /api/chat_stream, /api/inject_context, /api/search."""

import asyncio
import json
import os
import re
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any, AsyncGenerator, List, Optional

from fastapi import APIRouter, Request, HTTPException, Form, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from core.models import ChatMessage
from src.request_models import ChatRequest
from src.llm_core import llm_call_async, stream_llm, stream_llm_with_fallback
from src.agent_loop import stream_agent_loop
from src import agent_runs
from src.model_context import estimate_tokens
from src.chat_helpers import coerce_message_and_session
from src.endpoint_resolver import normalize_base as _normalize_base, build_chat_url
from src.session_search import search_session_messages
from src.prompt_security import untrusted_context_message
from core.exceptions import SessionNotFoundError
from src.auth_helpers import effective_user, get_current_user
from routes.session_routes import _verify_session_owner
from routes.document_helpers import _owner_session_filter
from core.database import SessionLocal, get_session_mode, set_session_mode
from core.database import Session as DBSession, ChatMessage as DBChatMessage
from core.database import Document as DBDocument, ModelEndpoint
from core.log_safety import redact_url
from routes.research_routes import _resolve_research_endpoint
from routes.model_routes import _visible_models
from routes.chat_helpers import (
    resolve_session_auth,
    build_chat_context,
    save_assistant_response,
    run_post_response_tasks,
    clean_thinking_for_save,
    _enforce_chat_privileges,
)
from src.action_intents import ToolIntent, classify_tool_intent as _classify_tool_intent
from src.tool_policy import (
    WEB_TOOL_NAMES,
    build_effective_tool_policy,
    is_web_search_explicitly_denied,
    web_search_enabled_for_turn,
)

logger = logging.getLogger(__name__)

# Track active streams for partial-save safety net
_active_streams: Dict[str, dict] = {}
_IMAGE_MODEL_PREFIXES = ("gpt-image", "dall-e", "chatgpt-image")


def _note_failure(session_id, cause) -> None:
    """Keep the technical cause of a failed reply for when the person presses "Tell …" (services/notify.py)."""
    try:
        from services import notify
        notify.note_failure(session_id, cause)
    except Exception:
        pass


def _stream_set(session_id: str, **fields) -> None:
    """Update fields on the active-stream entry for `session_id`, or
    no-op if the entry has already been popped. Using .get() avoids a
    KeyError race between `if x in d` and `d[x]["k"] = v` if a sibling
    finally pops the key in between (which becomes possible the moment
    a coroutine cancellation reaches an inner cleanup before the
    outermost cleanup runs)."""
    rec = _active_streams.get(session_id)
    if rec is None:
        return
    rec.update(fields)


def _message_plain_text(content: Any) -> str:
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return str(content or "")


def _last_user_plain_text(messages: List[Dict[str, Any]]) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            return _message_plain_text(msg.get("content"))
    return ""


def _ensure_current_request_is_latest_user(messages: List[Dict[str, Any]], current_message: str) -> List[Dict[str, Any]]:
    """Defensively keep detached streams grounded on the request that created them."""
    current = str(current_message or "").strip()
    if not current:
        return messages
    latest = _last_user_plain_text(messages).strip()
    if latest == current or current in latest or latest in current:
        return messages
    logger.warning(
        "[chat_stream] latest user context mismatch; appending current request for model call. latest=%r current=%r",
        latest[:120],
        current[:120],
    )
    repaired = list(messages or [])
    repaired.append({"role": "user", "content": current})
    return repaired


_WEB_FOLLOWUP_RE = re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:check|try\s+again|look(?:\s+now|\s+it\s+up)?|search(?:\s+now|\s+online|\s+it)?|"
    r"do\s+it|again)\??\s*$",
    re.I,
)
_RECENT_WEB_CONTEXT_RE = re.compile(
    r"\b(?:weather|forecast|rain|raining|hourly|news|headlines|rate|exchange|currency|"
    r"price|current|latest|search|look\s+up|online)\b",
    re.I,
)


def _recent_session_text(sess, limit: int = 8, max_chars: int = 2000) -> str:
    history = getattr(sess, "history", None) or getattr(sess, "_history", None) or []
    chunks: List[str] = []
    for msg in history[-limit:]:
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        text = _message_plain_text(content).strip()
        if text:
            chunks.append(text)
    return " ".join(chunks)[-max_chars:]


def _is_contextual_web_followup(message: str, sess) -> bool:
    """Treat short retry/check replies as web lookups when recent context was web."""
    if not message or not _WEB_FOLLOWUP_RE.search(message):
        return False
    return bool(_RECENT_WEB_CONTEXT_RE.search(_recent_session_text(sess)))


def _resolve_request_workspace(request, raw_value) -> tuple:
    """Resolve the posted workspace for this request: (workspace, rejected).

    Privilege is checked BEFORE the path ever touches the filesystem. Only
    admin/single-user callers can use the workspace-backed file/shell tools,
    so only they get vet_workspace() and the workspace_rejected signal. For
    any other caller the submitted value is dropped uniformly, with no vetting
    and no event: otherwise the presence/absence of workspace_rejected would
    let a non-admin chat caller probe which host paths exist.

    vet_workspace rejects non-directories, sensitive roots (.ssh, .gnupg,
    ...), and filesystem roots; on rejection there is no confinement and the
    default tool-path allowlist applies. The rejected value is surfaced so the
    stream can tell an admin client (which believes a workspace is active)
    that it was dropped.
    """
    requested = (raw_value or "").strip()
    if not requested:
        return "", ""
    from src.tool_security import owner_is_admin_or_single_user
    if not owner_is_admin_or_single_user(get_current_user(request)):
        return "", ""
    from src.tool_execution import vet_workspace
    workspace = vet_workspace(requested) or ""
    return workspace, (requested if not workspace else "")


def _session_url_matches_endpoint(session_url: str, endpoint_base: str) -> bool:
    if not session_url or not endpoint_base:
        return False
    sess = session_url.rstrip("/")
    base = _normalize_base(endpoint_base).rstrip("/")
    variants = {
        base,
        base + "/chat/completions",
        build_chat_url(base).rstrip("/"),
    }
    return sess in variants or sess.startswith(base + "/")


def _clear_orphaned_session_endpoint(sess, owner: str | None = None) -> bool:
    """Clear a session model if its endpoint was deleted from ModelEndpoint."""
    if not getattr(sess, "endpoint_url", ""):
        return False
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
        if owner:
            from src.auth_helpers import owner_filter
            q = owner_filter(q, ModelEndpoint, owner)
        endpoints = q.all()
        for ep in endpoints:
            if _session_url_matches_endpoint(sess.endpoint_url or "", ep.base_url or ""):
                return False
        db_session = db.query(DBSession).filter(DBSession.id == sess.id).first()
        if db_session:
            db_session.endpoint_url = ""
            db_session.model = ""
            db_session.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
        sess.endpoint_url = ""
        sess.model = ""
        sess.headers = {}
        return True
    except Exception as e:
        logger.warning("Failed to clear orphaned session endpoint", exc_info=e)
        db.rollback()
        return False
    finally:
        db.close()


def _endpoint_cache_contains_model(endpoint, model: str) -> bool:
    """Return True when a populated endpoint model cache includes ``model``.

    Empty/malformed caches are treated as unknown rather than a negative match
    so older image endpoints without cached models still work.
    """
    raw = getattr(endpoint, "cached_models", None)
    if not raw:
        return True
    try:
        models = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        logger.warning("Failed to parse cached models list, treating as containing model", exc_info=e)
        return True
    if not isinstance(models, list) or not models:
        return True
    wanted = (model or "").strip()
    return wanted in {str(item).strip() for item in models}


def _is_image_generation_session(sess, owner: str | None = None) -> bool:
    """Whether this chat session should bypass text chat and generate images.

    Model-name prefixes are explicit image models. Endpoint type is only used
    when the current session endpoint actually matches that image endpoint, and
    when a populated endpoint model cache includes the selected model. This
    prevents an image endpoint on the same host from misrouting ordinary text
    models into the image-generation path.
    """
    model = (getattr(sess, "model", "") or "").strip()
    if any(model.lower().startswith(prefix) for prefix in _IMAGE_MODEL_PREFIXES):
        return True

    endpoint_url = (getattr(sess, "endpoint_url", "") or "").strip()
    if not endpoint_url:
        return False

    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
        if owner:
            from src.auth_helpers import owner_filter
            q = owner_filter(q, ModelEndpoint, owner)
        endpoints = q.all()
        for endpoint in endpoints:
            if (getattr(endpoint, "model_type", None) or "llm") != "image":
                continue
            if not _session_url_matches_endpoint(endpoint_url, getattr(endpoint, "base_url", "") or ""):
                continue
            if _endpoint_cache_contains_model(endpoint, model):
                return True
    except Exception:
        return False
    finally:
        db.close()
    return False


def _recover_empty_session_model(sess, session_id: str, owner: str | None = None) -> bool:
    """Re-populate sess.model from the matching endpoint's cached models.

    Covers the window between endpoint setup and the first chat send: the
    picker showed a model in the dropdown but the session record never got
    written (Issue #587 — UI uses the cached endpoint list, not s.model).
    For ChatGPT Subscription, also repairs stale OpenAI API model names such as
    ``gpt-5`` that are not accepted by the Codex-backed ChatGPT account route.
    """
    current_model = (getattr(sess, "model", "") or "").strip()
    endpoint_url = (getattr(sess, "endpoint_url", "") or "").strip()
    is_chatgpt_subscription = False
    if current_model:
        try:
            from src.chatgpt_subscription import is_chatgpt_subscription_base
            is_chatgpt_subscription = is_chatgpt_subscription_base(endpoint_url)
            if not is_chatgpt_subscription:
                return False
        except Exception:
            return False
    db = SessionLocal()
    try:
        # Prefer the endpoint whose base URL matches the session — we know the
        # user already pointed this session at that endpoint, so its first
        # cached model is the most defensible default.
        ep = None
        if getattr(sess, "endpoint_url", ""):
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
            if owner:
                from src.auth_helpers import owner_filter
                q = owner_filter(q, ModelEndpoint, owner)
            endpoints = q.all()
            for cand in endpoints:
                if _session_url_matches_endpoint(sess.endpoint_url or "", cand.base_url or ""):
                    ep = cand
                    break
        if not ep:
            return False
        if not is_chatgpt_subscription:
            try:
                from src.chatgpt_subscription import is_chatgpt_subscription_base
                is_chatgpt_subscription = is_chatgpt_subscription_base(getattr(ep, "base_url", "") or endpoint_url)
            except Exception:
                is_chatgpt_subscription = False
        try:
            cached = json.loads(ep.cached_models) if isinstance(ep.cached_models, str) else (ep.cached_models or [])
        except Exception as e:
            logger.warning("Failed to parse cached_models for endpoint %r", getattr(ep, "id", "?"), exc_info=e)
            cached = []
        if not cached:
            visible = []
        else:
            try:
                visible = _visible_models(cached, getattr(ep, "hidden_models", None))
            except Exception:
                visible = cached
        if current_model and current_model in {str(item).strip() for item in visible}:
            return False
        if is_chatgpt_subscription:
            live_models = []
            if getattr(ep, "provider_auth_id", None):
                try:
                    from src.chatgpt_subscription import fetch_available_models
                    from src.endpoint_resolver import resolve_endpoint_runtime
                    _base, api_key = resolve_endpoint_runtime(ep, owner=owner)
                    if api_key:
                        live_models = fetch_available_models(api_key)
                        if live_models:
                            ep.cached_models = json.dumps(live_models)
                            db.commit()
                except Exception:
                    live_models = []
            # ChatGPT Subscription recovery must use the live Codex catalog.
            # Cached rows are only trusted above to avoid revalidating a model
            # that is already present in the visible picker list.
            cached = live_models
            if not cached:
                return False
            try:
                visible = _visible_models(cached, getattr(ep, "hidden_models", None))
            except Exception:
                visible = cached
            if current_model and current_model in {str(item).strip() for item in visible}:
                return False
        if not visible:
            return False
        model = visible[0]
        if not isinstance(model, str) or not model.strip():
            return False
        model = model.strip()
        # Persist so the next request, websocket reconnect, or page reload
        # picks up the same model (we'd otherwise re-pick on every send
        # and silently switch on the user if the cached order shifts).
        db_session_q = db.query(DBSession).filter(DBSession.id == session_id)
        if owner:
            db_session_q = db_session_q.filter(DBSession.owner == owner)
        db_session = db_session_q.first()
        if db_session:
            db_session.model = model
            db_session.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
        sess.model = model
        logger.info(
            "Recovered session model for %s — picked %r from endpoint %s",
            session_id, model, ep.id,
        )
        return True
    except Exception as e:
        db.rollback()
        logger.warning("Failed to recover empty session model for %s: %s", session_id, e)
        return False
    finally:
        db.close()


def _set_user_time_from_request(request: Request) -> None:
    """Copy browser timezone headers into the per-request context.

    This is intentionally ephemeral: it is used only while building prompts
    and running tools for this request. It is not persisted or logged.
    """
    try:
        tz_offset = request.headers.get("x-tz-offset")
        tz_name = request.headers.get("x-tz-name")
        from src.user_time import clear_user_time_context, set_user_tz_name, set_user_tz_offset

        clear_user_time_context()
        if tz_offset is not None:
            set_user_tz_offset(tz_offset)
        if tz_name:
            set_user_tz_name(tz_name)
    except Exception:
        pass


def setup_chat_routes(
    session_manager,
    chat_handler,
    chat_processor,
    memory_manager,
    research_handler,
    upload_handler,
    memory_vector=None,
    webhook_manager=None,
    skills_manager=None,
) -> APIRouter:
    router = APIRouter(tags=["chat"])

    # ------------------------------------------------------------------ #
    # POST /api/chat (non-streaming)
    # ------------------------------------------------------------------ #
    @router.post("/api/chat", response_model=Dict[str, str])
    async def chat_endpoint(request: Request, chat_request: ChatRequest) -> Dict[str, str]:
        _set_user_time_from_request(request)

        message = chat_request.message
        session = chat_request.session
        att_ids = chat_request.attachments or []
        use_web = chat_request.use_web
        use_research = chat_request.use_research
        time_filter = chat_request.time_filter
        preset_id = chat_request.preset_id

        # Verify the caller owns this session before loading it.
        # Without this, any authenticated user can post into another user's chat.
        _verify_session_owner(request, session)

        try:
            sess = session_manager.get_session(session)
        except KeyError:
            raise HTTPException(404, f"Session '{session}' not found")
        owner = effective_user(request)
        if _clear_orphaned_session_endpoint(sess, owner=owner):
            raise HTTPException(400, "Selected model endpoint was removed. Pick another model in Settings.")

        # Empty model + live endpoint = setup race (Issue #587). Repair from
        # the endpoint's cached model list before privilege checks, which
        # otherwise see "" and behave inconsistently with the allowlist.
        _recover_empty_session_model(sess, session, owner=owner)
        if not getattr(sess, "model", "").strip():
            raise HTTPException(
                400,
                "No model selected for this chat. Open the model picker and choose one before sending.",
            )

        # Same allowed_models + daily-cap gate as chat_stream (mirror so the
        # non-streaming path can't be used to bypass).
        _enforce_chat_privileges(request, sess)

        tool_policy = build_effective_tool_policy(last_user_message=message)
        allow_tool_preprocessing = not tool_policy.block_all_tool_calls

        # Inline memory command
        memory_response = None
        if not tool_policy.blocks("manage_memory"):
            memory_response = await chat_handler.handle_memory_command(sess, message)
        if memory_response:
            return {"response": memory_response}

        # Build shared context (preset, preprocess, preface, compact)
        ctx = await build_chat_context(
            sess, request, chat_handler, chat_processor,
            message=message,
            session_id=session,
            preset_id=preset_id,
            att_ids=att_ids,
            use_web=use_web,
            time_filter=time_filter,
            webhook_manager=webhook_manager,
            allow_tool_preprocessing=allow_tool_preprocessing,
        )

        # Research injection
        research_blocked_by_policy = (
            tool_policy.blocks("trigger_research")
            or tool_policy.blocks("manage_research")
        )
        if use_research and not research_blocked_by_policy:
            try:
                _r_ep, _r_model, _r_headers = _resolve_research_endpoint(sess)
                research_ctx = await research_handler.call_research_service(
                    message, _r_ep, _r_model, llm_headers=_r_headers
                )
                ctx.messages.insert(
                    len(ctx.preface),
                    untrusted_context_message("research context", research_ctx),
                )
            except Exception as e:
                logger.error(f"Research failed: {e}")

        reply = await llm_call_async(
            sess.endpoint_url,
            sess.model,
            ctx.messages,
            headers=sess.headers,
            temperature=ctx.preset.temperature,
            max_tokens=ctx.preset.max_tokens,
            prompt_type=preset_id,
            session_id=session,
        )
        _clean_reply, _clean_md = clean_thinking_for_save(reply, {"model": sess.model})
        sess.add_message(ChatMessage("assistant", _clean_reply, metadata=_clean_md))

        from core.database import update_session_last_accessed
        update_session_last_accessed(session)
        session_manager.save_sessions()

        # Background tasks (memory, webhook, auto-name)
        run_post_response_tasks(
            sess, session_manager, session, message, reply, None,
            ctx.uprefs, memory_manager, memory_vector, webhook_manager,
            character_name=ctx.preset.character_name,
            owner=ctx.user,
            allow_background_extraction=not tool_policy.block_all_tool_calls,
        )

        return {"response": reply}

    # ------------------------------------------------------------------ #
    # POST /api/chat_stream
    # ------------------------------------------------------------------ #
    @router.post("/api/chat_stream")
    async def chat_stream(request: Request) -> StreamingResponse:
        body = None
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                try:
                    body = await request.json()
                except json.JSONDecodeError as e:
                    raise HTTPException(400, f"Invalid JSON: {e}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"Request parsing error: {e}")

        _set_user_time_from_request(request)

        form_data = await request.form()
        message = form_data.get("message")
        session = form_data.get("session")
        attachments = form_data.get("attachments")
        # Which screen is asking. The family screen ("home") draws an answer's card; the full app does
        # not, so a job that answers with a card has the card's content written out for it (services/cards.py).
        _draws_cards = str(form_data.get("screen") or "").lower() == "home"
        use_web = form_data.get("use_web")
        use_research = form_data.get("use_research")
        time_filter = form_data.get("time_filter")
        preset_id = form_data.get("preset_id")
        # Per-model chat settings from the UI's model-settings popover. `think`
        # is on|off|"" (auto — leave the engine default); gen_temperature and
        # gen_max_tokens override the preset for this turn only. Applied to
        # ctx.preset (a mutable dataclass) right after build_chat_context, and
        # the think contextvar is read inside the llm_core payload builders.
        _think_raw = str(form_data.get("think", "") or "").lower()
        _think_override = True if _think_raw == "on" else (False if _think_raw == "off" else None)
        try:
            from src import llm_core as _llm_core
            _llm_core.set_think_override(_think_override)
        except Exception:
            pass

        def _num_or_none(key, cast, lo, hi):
            raw = form_data.get(key)
            if raw in (None, ""):
                return None
            try:
                v = cast(raw)
            except (TypeError, ValueError):
                return None
            return max(lo, min(hi, v))

        _temp_override = _num_or_none("gen_temperature", float, 0.0, 2.0)
        _maxtok_override = _num_or_none("gen_max_tokens", int, 64, 32768)

        def _apply_gen_overrides(_ctx):
            """Override the resolved preset's temperature/max_tokens with the
            per-model UI choices, when supplied."""
            if _ctx is None or getattr(_ctx, "preset", None) is None:
                return
            if _temp_override is not None:
                _ctx.preset.temperature = _temp_override
            if _maxtok_override is not None:
                _ctx.preset.max_tokens = _maxtok_override
        # Issue #3229: API callers send JSON, not FormData.  Read from the
        # JSON body as fallback so callers who send {"allow_bash": true}
        # actually get bash enabled.
        allow_bash = form_data.get("allow_bash") or (body or {}).get("allow_bash")
        allow_web_search = form_data.get("allow_web_search") or (body or {}).get("allow_web_search")
        use_rag = form_data.get("use_rag")
        search_context = form_data.get("search_context")  # pre-fetched web search results (compare mode)
        compare_mode = str(form_data.get("compare_mode", "")).lower() == "true"
        incognito = str(form_data.get("incognito", "")).lower() == "true"
        # Plan mode is not part of the merge-ready UI. Ignore stale clients or
        # manual form posts that still send plan_mode=true.
        plan_mode = False
        chat_mode = str(form_data.get("mode", "")).lower()  # 'chat' or 'agent'
        # Code mode: agent mode pointed at software work, on a chosen machine
        # (this server unless another is picked in the composer). It implies
        # the shell. Whether this account may use it is decided further down,
        # with the other privileges.
        code_mode = str(form_data.get("code_mode") or (body or {}).get("code_mode") or "").lower() == "true"
        code_host = str(form_data.get("code_host") or (body or {}).get("code_host") or "").strip().lower() or "local"
        code_target = None
        if code_mode:
            chat_mode = "agent"
            allow_bash = "true"
        # Workspace: confine the agent's file/shell tools to this folder.
        workspace, workspace_rejected = _resolve_request_workspace(
            request, form_data.get("workspace")
        )
        # A project opened in this chat (manage_projects / /project) wins over
        # the browser-wide workspace setting.
        try:
            from services.workbench import projects as _wb_projects
            _bound = _wb_projects.bound_workspace(str(session or ""))
            if _bound:
                workspace, workspace_rejected = _bound, ""
        except Exception:
            pass
        # Plan mode is a modifier on agent mode — it only makes sense with tools.
        if plan_mode:
            chat_mode = "agent"
        # An approved plan being EXECUTED: the frontend sends the checklist back
        # on each turn so we can pin it in context. This way a long plan on a
        # weak model survives history truncation — the agent can always re-read
        # the plan. Ignored while still proposing (plan_mode on). Capped so a
        # huge plan can't blow the prompt.
        approved_plan = ""
        if not plan_mode:
            approved_plan = (form_data.get("approved_plan") or "").strip()[:8192]
        # Did the USER explicitly pick agent mode? (vs. us auto-escalating
        # below). Skill extraction should only learn from real agent sessions,
        # not chats we quietly promoted for a notes/calendar intent.
        user_requested_agent = (chat_mode == "agent")
        _search_enabled = web_search_enabled_for_turn(allow_web_search, use_web)
        # Intent auto-escalation: if the user is clearly asking the assistant
        # to create a todo, reminder, or calendar event, promote chat → agent
        # for this turn so the LLM has access to manage_notes / manage_calendar.
        # This is a LIGHT promotion — see the disabled_tools block below, which
        # withholds shell/code/file tools so the model doesn't try to `bash`
        # its way through a plain chat request (and fail, especially with the
        # shell disabled).
        auto_escalated = False
        _tool_intent = _classify_tool_intent(message) if isinstance(message, str) else None
        if chat_mode == "chat" and _tool_intent and _tool_intent.needs_tools:
            chat_mode = "agent"
            auto_escalated = True
            logger.info(
                "chat→agent auto-escalation: category=%s reason=%s",
                _tool_intent.category,
                _tool_intent.reason,
            )
        elif chat_mode == "chat" and _search_enabled:
            chat_mode = "agent"
            auto_escalated = True
            logger.info("chat→agent auto-escalation: search enabled")
        active_doc_id = form_data.get("active_doc_id", "").strip()
        logger.info(f"[doc-inject] chat_mode={chat_mode}, active_doc_id={active_doc_id!r}")
        # Toolboxes are OFF in ordinary chat. They only turn on when the user
        # opts in per-message with a leading /osint | /market | /troubleshoot
        # (the frontend sets active_toolboxes); otherwise this is empty and the
        # agent loop offers no toolbox tools. Toolboxes are also Recipes blocks.
        _active_toolboxes = {
            t.strip().lower() for t in (form_data.get("active_toolboxes", "") or "").split(",") if t.strip()
        }

        # Active email reader — when the user has an email open in the UI, the
        # frontend passes its uid/folder/account so "reply", "summarize this",
        # etc. resolve to the real email instead of the agent inventing a
        # fake markdown draft.
        active_email_uid = form_data.get("active_email_uid", "").strip()
        active_email_folder = form_data.get("active_email_folder", "INBOX").strip() or "INBOX"
        active_email_account = form_data.get("active_email_account", "").strip()
        active_email_ctx: Optional[Dict[str, str]] = None
        # Always reset between requests so a stale active-email pointer from
        # a previous turn (different reader closed, different account, etc.)
        # can't leak in when the user has no email open this turn.
        try:
            from src.tool_implementations import clear_active_email
            clear_active_email()
        except Exception:
            pass
        if active_email_uid:
            active_email_ctx = {
                "uid": active_email_uid,
                "folder": active_email_folder,
                "account": active_email_account,
            }
            # Try to enrich with subject + from so the agent's system prompt
            # block can quote them. Best-effort: a stale cache is fine, a
            # missing email just means we pass uid/folder/account only.
            try:
                from routes.email_routes import _read_cache_get, _read_cache_key
                _ck = _read_cache_key(active_email_account or None, active_email_folder, active_email_uid, owner=get_current_user(request))
                _cached_email = _read_cache_get(_ck)
                if _cached_email and isinstance(_cached_email, dict):
                    active_email_ctx["subject"] = str(_cached_email.get("subject") or "")
                    active_email_ctx["from"] = str(
                        _cached_email.get("from_address")
                        or _cached_email.get("from")
                        or _cached_email.get("from_name")
                        or ""
                    )
                    _body_preview = (_cached_email.get("body") or "")[:2000]
                    if _body_preview:
                        active_email_ctx["body_preview"] = _body_preview
            except Exception as _e:
                logger.debug(f"[email-inject] cache enrich skipped: {_e}")
            # Stash so email tools can resolve "this email" without UID guessing.
            try:
                from src.tool_implementations import set_active_email
                set_active_email(
                    uid=active_email_uid,
                    folder=active_email_folder,
                    account=active_email_account or None,
                    subject=active_email_ctx.get("subject"),
                    sender=active_email_ctx.get("from"),
                )
            except Exception as _e:
                logger.debug(f"[email-inject] set_active_email failed: {_e}")
            logger.info(
                "[email-inject] active_email uid=%s folder=%s account=%s subject=%r",
                active_email_uid, active_email_folder, active_email_account or "(default)",
                active_email_ctx.get("subject", ""),
            )

        try:
            # Attachment-only sends: skip the message-required check when the
            # user has attached one or more files (the attachment IS the action).
            _has_atts = (
                bool(body and isinstance(body.get("attachments"), list) and body["attachments"])
                or bool(form_data.get("attachments"))
            )
            message, session = coerce_message_and_session(
                body, message, session, session_manager, allow_empty=_has_atts,
            )
            # Verify ownership AFTER coerce (which may resolve a default session)
            # but BEFORE loading. Prevents cross-user session hijack.
            _verify_session_owner(request, session)
            sess = session_manager.get_session(session)
            owner = effective_user(request)
            if _clear_orphaned_session_endpoint(sess, owner=owner):
                raise HTTPException(400, "Selected model endpoint was removed. Pick another model in Settings.")
            # Issue #587: picker shows a model from the endpoint cache but
            # s.model never made it onto the DB row (first-send race after
            # endpoint setup, or a previous endpoint delete/recreate). Pull
            # the first cached model off the matching endpoint so the
            # upstream isn't called with model="" (which surfaces as a
            # generic 401/503).
            _recover_empty_session_model(sess, session, owner=owner)
            if not getattr(sess, "model", "").strip():
                raise HTTPException(
                    400,
                    "No model selected for this chat. Open the model picker and choose one before sending.",
                )
            if (
                chat_mode == "chat"
                and isinstance(message, str)
                and (not _tool_intent or not _tool_intent.needs_tools)
                and _is_contextual_web_followup(message, sess)
            ):
                _tool_intent = ToolIntent(True, "web", "contextual web lookup follow-up")
                chat_mode = "agent"
                auto_escalated = True
                logger.info(
                    "chat→agent auto-escalation: category=%s reason=%s",
                    _tool_intent.category,
                    _tool_intent.reason,
                )
        except SessionNotFoundError as e:
            raise HTTPException(404, str(e))
        except (ValueError, ValidationError):
            raise HTTPException(400, "Invalid request parameters")

        # ------------------------------------------------------------------ #
        # Auto: how should this turn be answered? (services/router.py)
        # A turn left in the tool-using mode takes the quick lane — a plain
        # answer, no tool prompt — when the message is plainly conversation.
        # The effort follows the message when Thinking was not set by hand. A
        # model is swapped only where the administrator named one. The mode
        # the person chose is what gets saved with the chat, not the lane.
        _mode_as_sent = chat_mode
        _auto_route = None
        _auto_model = None
        _auto_quick_lane = False
        try:
            from services import router as _router
            if isinstance(message, str) and message.strip() and _router.enabled(owner):
                _last_reply, _last_tools = "", False
                for _past in reversed(list(getattr(sess, "history", None) or [])):
                    if getattr(_past, "role", "") == "assistant":
                        _said = getattr(_past, "content", "")
                        _last_reply = _said if isinstance(_said, str) else ""
                        _last_tools = bool((getattr(_past, "metadata", None) or {}).get("tool_events"))
                        break
                _has_files = bool(body and isinstance(body.get("attachments"), list) and body["attachments"]) or (
                    isinstance(attachments, str) and attachments.strip() not in ("", "[]", "null"))
                _named = _router.named_models()
                try:
                    from src.tool_index import _library_ready as _lib_ready
                    from src.tool_security import owner_uses_home_screen as _home_user
                    _library_ok, _is_home = bool(_lib_ready()), bool(_home_user(owner))
                except Exception:
                    _library_ok, _is_home = False, False
                _auto_route = _router.decide(_router.Turn(
                    message=message, mode=chat_mode, code_mode=code_mode, has_attachments=_has_files,
                    # The web being allowed (it is, by default) is not a reason to
                    # keep tools: only plain chat's own "search first" is.
                    search_on=str(use_web).lower() == "true",
                    special=bool(active_doc_id or active_email_uid or _active_toolboxes or compare_mode or approved_plan
                                 or str(use_rag).lower() == "true" or str(use_research).lower() == "true"),
                    home_account=_is_home, library_ready=_library_ok, think_set=_think_override,
                    last_reply=_last_reply, last_used_tools=_last_tools, auto_models=_named,
                    available_models=_router.served_models(sess.endpoint_url) if _named else None,
                    current_model=sess.model or ""))
                # In a chat that is under way, the top of the prompt is what a local model still
                # has from the turn before (src/prompt_stability.py). The quick lane is a different
                # prompt altogether and a lower effort changes its first lines: either makes the
                # model read the whole chat again, which costs more than "thanks" answered the
                # quick way saves. So both are for a chat's opening; a harder question still gets
                # more thought whenever it comes.
                from src import prompt_stability as _stability_route
                _chat_under_way = _stability_route.under_way(session)
                if chat_mode == "agent" and _auto_route.path == "chat" and not _chat_under_way:
                    chat_mode = "chat"
                    _auto_quick_lane = True
                if _auto_route.effort and _think_override is None and (_auto_route.effort == "high" or not _chat_under_way):
                    from src import llm_core as _auto_llm
                    _auto_llm.set_think_override(_auto_route.effort == "high")
                _auto_model = _auto_route.model
                logger.info("auto route: mode=%s→%s effort=%s model=%s (%s)", _mode_as_sent, chat_mode,
                            _auto_route.effort or "default", _auto_model or "-", "; ".join(_auto_route.reasons))
        except Exception:
            logger.debug("auto routing skipped", exc_info=True)
            _auto_route, _auto_model, _auto_quick_lane = None, None, False
            chat_mode = _mode_as_sent

        # ------------------------------------------------------------------ #
        # Privilege gates that must fire BEFORE any LLM work / token spend.
        #   1. allowed_models — reject if session.model isn't in the user's
        #      configured allowlist (empty list = "no restriction").
        #   2. max_messages_per_day — count user-role ChatMessage rows owned
        #      by this user in the last UTC day; 429 if at/over the cap.
        # Admins always have full privileges via get_privileges (returns
        # ADMIN_PRIVILEGES wholesale) so this is a no-op for them.
        _enforce_chat_privileges(request, sess)

        # Ensure session has auth headers
        resolve_session_auth(sess, session, owner=effective_user(request))

        # Check for research_pending BEFORE mode persist overwrites it
        do_research = str(use_research).lower() == "true"
        if not do_research:
            if get_session_mode(session) == 'research_pending':
                do_research = True
                logger.info(f"Session {session} in research_pending — auto-triggering research")

        # ------------------------------------------------------------------ #
        # The straight-to-tool lane (services/direct.py). A sentence one exact
        # tool can answer alone — sunset, a unit conversion, days until a date,
        # the forecast, a short translation, something for a list — is answered
        # by that tool with no model round first: about a second instead of
        # four to eleven, and the same answer every time. Only a turn left on
        # Auto, with nothing attached or open beside it, is looked at; anything
        # the lane does not read goes on exactly as before.
        _direct = None
        _plain_turn = False
        _lane_off = None                      # tools that are not this person's to use; None until worked out
        try:
            _plain_turn = (
                isinstance(message, str) and _mode_as_sent == "agent" and not code_mode and not compare_mode and not incognito
                and not do_research and not active_doc_id and not active_email_uid and not _active_toolboxes and not approved_plan
                and not (body and isinstance(body.get("attachments"), list) and body["attachments"])
                and not (isinstance(attachments, str) and attachments.strip() not in ("", "[]", "null"))
            )
            if _plain_turn:
                from services import direct as _direct_lane
                from services import router as _lane_router
                if _lane_router.enabled(owner):
                    _may_act = True
                    if owner and hasattr(request.app.state, "auth_manager") and request.app.state.auth_manager:
                        _may_act = bool(request.app.state.auth_manager.get_privileges(owner).get("can_use_agent", True))
                    if _may_act:
                        from src.settings import get_setting as _lane_setting
                        from src.tool_security import blocked_tools_for_owner as _lane_blocked
                        _off = set(_lane_blocked(owner)) | set(_lane_setting("disabled_tools", []) or [])
                        _lane_policy = build_effective_tool_policy(last_user_message=message)
                        _off |= _lane_policy.all_disabled_names()
                        if not _lane_policy.block_all_tool_calls:       # "no tools, just tell me": then no tool answers either
                            _lane_off = _off
                            if _direct_lane.worth_trying(message):
                                _direct = await _direct_lane.answer(message, owner, allowed=lambda tool: tool not in _off)
        except Exception:
            logger.debug("straight-to-tool lane skipped", exc_info=True)
            _direct = None
        if _direct is not None:
            logger.info("direct answer: %s answered %r without a model round", _direct.tool, message[:80])
            _direct_started = time.time()
            _direct_event = {"round": 1, "tool": _direct.tool, "command": _direct.command, "output": _direct.output, "exit_code": 0}
            if _direct.card:
                _direct_event["card"] = _direct.card
            sess.add_message(ChatMessage("user", message))
            try:
                chat_handler.update_session_name_if_needed(sess, message)
            except Exception:
                pass
            set_session_mode(session, "agent")
            _direct_metrics = {"model": sess.model, "requested_model": sess.model, "direct": _direct.tool, "response_time": 0.0,
                               "tool_events": [_direct_event]}

            async def _direct_stream() -> AsyncGenerator[str, None]:
                _info = {"type": "model_info", "model": sess.model, "direct": _direct.tool,
                         "auto": {"path": "direct", "effort": "none", "model": "", "label": "instant",
                                  "why": "one exact tool answers this: no model round was needed"}}
                yield f'data: {json.dumps(_info)}\n\n'
                yield f'data: {json.dumps({"type": "tool_start", "tool": _direct.tool, "command": _direct.command, "round": 1})}\n\n'
                yield f'data: {json.dumps({"type": "tool_output", **{k: v for k, v in _direct_event.items() if k != "round"}})}\n\n'
                yield f'data: {json.dumps({"delta": _direct.text})}\n\n'
                _direct_metrics["response_time"] = round(time.time() - _direct_started, 2)
                _saved = save_assistant_response(sess, session_manager, session, _direct.text, _direct_metrics, tool_events=[_direct_event])
                yield f'data: {json.dumps({"type": "metrics", "data": _direct_metrics})}\n\n'
                if _saved:
                    yield f'data: {json.dumps({"type": "message_saved", "id": _saved})}\n\n'
                yield "data: [DONE]\n\n"

            return StreamingResponse(_direct_stream(), media_type="text/event-stream")

        # ------------------------------------------------------------------ #
        # Dinner (services/dinner.py): "what can I make with chicken and rice?"
        # done as one job with fixed steps — the cookbooks, one recipe chosen,
        # set out in the Kitchen, what is missing, when to start — and answered
        # with a recipe card. And its follow-up, "add the ingredients for … to
        # my shopping list", which needs no model at all.
        _dinner, _shop = None, None
        try:
            if _plain_turn and _lane_off is not None and "manage_lists" not in _lane_off:
                from services import dinner as _dinner_job
                _shop = await asyncio.to_thread(_dinner_job.shopping, message, owner, getattr(sess, "history", None))
            if (_shop is None and _plain_turn and _lane_off is not None and "library_search" not in _lane_off
                    and "manage_kitchen" not in _lane_off):
                from services import dinner as _dinner_job
                from services.library import config as _library_config
                from src.user_time import now_user_local as _local_now
                if _library_config.enabled():
                    _dinner = _dinner_job.read(message, _local_now())
        except Exception:
            logger.debug("dinner check skipped", exc_info=True)
            _dinner, _shop = None, None
        if _shop is not None:
            sess.add_message(ChatMessage("user", message))
            set_session_mode(session, "agent")
            _shop_events = [dict(event, round=1, exit_code=0) for event in _shop["events"]]

            async def _shop_stream() -> AsyncGenerator[str, None]:
                yield f'data: {json.dumps({"type": "model_info", "model": sess.model, "direct": "manage_lists", "auto": {"label": "instant"}})}\n\n'
                yield f'data: {json.dumps({"type": "tool_start", "tool": "manage_lists", "command": "", "round": 1})}\n\n'
                yield f'data: {json.dumps({"type": "tool_output", "tool": "manage_lists", "command": "add", "output": _shop["text"], "exit_code": 0, "card": _shop["card"]})}\n\n'
                yield f'data: {json.dumps({"delta": _shop["text"]})}\n\n'
                _metrics = {"model": sess.model, "requested_model": sess.model, "direct": "manage_lists", "response_time": 0.0, "tool_events": _shop_events}
                _saved = save_assistant_response(sess, session_manager, session, _shop["text"], _metrics, tool_events=_shop_events)
                yield f'data: {json.dumps({"type": "metrics", "data": _metrics})}\n\n'
                if _saved:
                    yield f'data: {json.dumps({"type": "message_saved", "id": _saved})}\n\n'
                yield "data: [DONE]\n\n"

            return StreamingResponse(_shop_stream(), media_type="text/event-stream")
        if _dinner is not None:
            logger.info("dinner: looking for %s", _dinner["dish"] or ", ".join(_dinner["have"]))
            sess.add_message(ChatMessage("user", message))
            try:
                chat_handler.update_session_name_if_needed(sess, message)
            except Exception:
                pass
            set_session_mode(session, "agent")
            _dinner_started = time.time()

            async def _ask_about_dinner(prompt: str) -> str:
                from src.llm_core import llm_call_async, set_think_override
                set_think_override(False)
                return await llm_call_async(sess.endpoint_url, sess.model, [{"role": "user", "content": prompt}],
                                            temperature=0.2, max_tokens=1200, headers=sess.headers, timeout=180, session_id=session)

            async def _dinner_stream() -> AsyncGenerator[str, None]:
                from src.llm_core import set_think_override as _think
                _think(False)                       # the Kitchen's own setting-out is asked the same way a home turn asks
                yield f'data: {json.dumps({"type": "model_info", "model": sess.model})}\n\n'
                yield f'data: {json.dumps({"type": "smart", "key": "dinner", "title": "Finding something to cook"})}\n\n'
                _text, _events, _card, _last, _told, _said = "", [], None, "", False, ""
                try:
                    async for _step in _dinner_job.plan(_dinner, owner, _ask_about_dinner, may=lambda tool: tool not in _lane_off):
                        if _step.get("say"):                 # what was chosen, said while it is being set out
                            _said = _step["say"]
                            yield f'data: {json.dumps({"delta": _said})}\n\n'
                        elif _step.get("step"):
                            if _step["step"] in ("thinking", "manage_kitchen") and not _told:
                                _told = True
                                try:                      # the first model round of this job: say so if the model has to be woken
                                    from services import waiting as _wait
                                    _asleep = await _wait.waking(sess.endpoint_url, sess.model)
                                    if _asleep:
                                        yield f'data: {json.dumps(_asleep)}\n\n'
                                except Exception:
                                    pass
                            if _step["step"] != "thinking":
                                if _last:
                                    yield f'data: {json.dumps({"type": "tool_output", "tool": _last, "command": "", "output": "", "exit_code": 0})}\n\n'
                                _last = _step["step"]
                                yield f'data: {json.dumps({"type": "tool_start", "tool": _last, "command": "", "round": 1})}\n\n'
                        elif _step.get("sources"):
                            try:
                                from services.search.origin import LIBRARY as _LIB, marked as _marked
                                _found = _marked(_step["sources"], _LIB)
                            except Exception:
                                _found = _step["sources"]
                            yield f'data: {json.dumps({"type": "web_sources", "data": _found})}\n\n'
                        elif _step.get("done"):
                            _text, _card = _step["text"], _step.get("card")
                            _events = [dict(event, round=1, exit_code=0) for event in _step["events"]]
                            if _card:
                                yield f'data: {json.dumps({"type": "tool_output", "tool": "plan_dinner", "command": _card["title"], "output": _text, "exit_code": 0, "card": _card})}\n\n'
                                if not _draws_cards:      # the full app: what the card holds, in words
                                    from services.cards import as_markdown as _written
                                    _text = (_text + "\n\n" + _written(_card)).strip()
                            elif _last:
                                yield f'data: {json.dumps({"type": "tool_output", "tool": _last, "command": "", "output": "", "exit_code": 0})}\n\n'
                except Exception as _dinner_error:
                    logger.exception("dinner: the job failed part-way")
                    _note_failure(session, f"dinner: {type(_dinner_error).__name__}: {_dinner_error}")
                    yield f'data: {json.dumps({"type": "error", "error": "I could not finish finding a recipe."})}\n\n'
                    yield "data: [DONE]\n\n"
                    return
                yield f'data: {json.dumps({"delta": _text[len(_said):] if _said and _text.startswith(_said.strip()) else _text})}\n\n'
                _metrics = {"model": sess.model, "requested_model": sess.model, "direct": "plan_dinner",
                            "response_time": round(time.time() - _dinner_started, 2), "tool_events": _events}
                _saved = save_assistant_response(sess, session_manager, session, _text, _metrics, tool_events=_events)
                yield f'data: {json.dumps({"type": "metrics", "data": _metrics})}\n\n'
                if _saved:
                    yield f'data: {json.dumps({"type": "message_saved", "id": _saved})}\n\n'
                yield "data: [DONE]\n\n"

            return StreamingResponse(_dinner_stream(), media_type="text/event-stream")

        # ------------------------------------------------------------------ #
        # Going out (services/outing.py): "plan a day hike for Saturday" done as
        # one job with fixed steps — the sun, the forecast, the library, a saved
        # packing list, a reminder — and answered with a trip card. The model
        # is asked one thing (what to pack, what to watch for); everything else
        # is the same every time. Taken only when there is a place to plan for;
        # with no home saved the assistant asks, as before.
        _outing = None
        try:
            if _plain_turn and _lane_off is not None and "sun_moon" not in _lane_off and "manage_lists" not in _lane_off:
                from services import outing as _outing_job
                from src.user_time import now_user_local as _local_now
                _outing = (_outing_job.read(message, _local_now().date())
                           or _outing_job.again(message, getattr(sess, "history", None), _local_now().date()))
                if _outing is not None:
                    from services.almanac import places as _places
                    try:
                        _places.resolve(_outing.get("place") or "", owner)
                    except _places.PlaceError:
                        # A lake or a trailhead it cannot place: plan for home and say so, when there is
                        # a home; with none, the assistant asks where.
                        if _outing.get("place") and _places.home(owner) is not None:
                            _outing["place_unknown"], _outing["place"] = _outing["place"], ""
                        else:
                            _outing = None
        except Exception:
            logger.debug("outing check skipped", exc_info=True)
            _outing = None
        if _outing is not None:
            logger.info("outing: planning a %s for %s", _outing["kind"], _outing["day"])
            sess.add_message(ChatMessage("user", message))
            try:
                chat_handler.update_session_name_if_needed(sess, message)
            except Exception:
                pass
            set_session_mode(session, "agent")
            _outing_started = time.time()

            async def _ask_for_the_lists(prompt: str) -> str:
                from src.llm_core import llm_call_async, set_think_override
                set_think_override(False)           # two short lists: a short plan is enough, and a long one eats the answer
                return await llm_call_async(sess.endpoint_url, sess.model, [{"role": "user", "content": prompt}],
                                            temperature=0.2, max_tokens=1500, headers=sess.headers, timeout=180, session_id=session)

            async def _outing_stream() -> AsyncGenerator[str, None]:
                yield f'data: {json.dumps({"type": "model_info", "model": sess.model})}\n\n'
                yield f'data: {json.dumps({"type": "smart", "key": "going_out", "title": "Planning your outing"})}\n\n'
                _text, _events, _last = "", [], ""
                try:
                    async for _step in _outing_job.plan(_outing, owner, _ask_for_the_lists, may=lambda tool: tool not in _lane_off):
                        if _step.get("unreadable"):
                            _text = "Where are you going? A town is enough."
                        elif _step.get("step") == "thinking":
                            if _last:
                                yield f'data: {json.dumps({"type": "tool_output", "tool": _last, "command": "", "output": "", "exit_code": 0})}\n\n'
                            try:                          # the one model round of this job: say so if the model has to be woken first
                                from services import waiting as _wait
                                _asleep = await _wait.waking(sess.endpoint_url, sess.model)
                                if _asleep:
                                    yield f'data: {json.dumps(_asleep)}\n\n'
                            except Exception:
                                pass
                        elif _step.get("step"):
                            _last = _step["step"]
                            yield f'data: {json.dumps({"type": "tool_start", "tool": _last, "command": "", "round": 1})}\n\n'
                        elif _step.get("sources"):
                            try:
                                from services.search.origin import LIBRARY as _LIB, marked as _marked
                                _found = _marked(_step["sources"], _LIB)
                            except Exception:
                                _found = _step["sources"]
                            yield f'data: {json.dumps({"type": "web_sources", "data": _found})}\n\n'
                        elif _step.get("done"):
                            _text, _events = _step["text"], [dict(event, round=1, exit_code=0) for event in _step["events"]]
                            yield f'data: {json.dumps({"type": "tool_output", "tool": "plan_outing", "command": _step["card"]["title"], "output": _text, "exit_code": 0, "card": _step["card"]})}\n\n'
                            if not _draws_cards:          # the full app: what the card holds, in words
                                from services.cards import as_markdown as _written
                                _text = (_text + "\n\n" + _written(_step["card"])).strip()
                except Exception as _outing_error:
                    logger.exception("outing: the plan failed part-way")
                    _note_failure(session, f"outing: {type(_outing_error).__name__}: {_outing_error}")
                    yield f'data: {json.dumps({"type": "error", "error": "The plan could not be finished."})}\n\n'
                    yield "data: [DONE]\n\n"
                    return
                yield f'data: {json.dumps({"delta": _text})}\n\n'
                _metrics = {"model": sess.model, "requested_model": sess.model, "direct": "plan_outing",
                            "response_time": round(time.time() - _outing_started, 2), "tool_events": _events}
                _saved = save_assistant_response(sess, session_manager, session, _text, _metrics, tool_events=_events)
                yield f'data: {json.dumps({"type": "metrics", "data": _metrics})}\n\n'
                if _saved:
                    yield f'data: {json.dumps({"type": "message_saved", "id": _saved})}\n\n'
                yield "data: [DONE]\n\n"

            return StreamingResponse(_outing_stream(), media_type="text/event-stream")

        att_ids = []
        if body and isinstance(body.get("attachments"), list):
            att_ids = [str(x) for x in body["attachments"]]
        elif attachments:
            try:
                att_ids = [str(x) for x in json.loads(attachments)]
            except Exception as e:
                logger.warning("Failed to parse attachments JSON, ignoring attachments", exc_info=e)

        no_memory = str(form_data.get("no_memory", "")).lower() == "true"
        pre_context_tool_policy = build_effective_tool_policy(
            last_user_message=message,
        )
        allow_tool_preprocessing = not pre_context_tool_policy.block_all_tool_calls

        # Build shared context (stream path uses enhanced_message for context preface)
        ctx = await build_chat_context(
            sess, request, chat_handler, chat_processor,
            message=message,
            session_id=session,
            preset_id=preset_id,
            att_ids=att_ids,
            use_web=use_web,
            use_rag=use_rag,
            time_filter=time_filter,
            incognito=incognito,
            no_memory=no_memory,
            search_context=search_context,
            compare_mode=compare_mode,
            webhook_manager=webhook_manager,
            use_enhanced_message=True,
            # Skills index only ships when the model can actually call
            # manage_skills (agent mode). In plain chat or incognito the
            # index would be useless / unwanted noise.
            agent_mode=(chat_mode == "agent"),
            allow_tool_preprocessing=allow_tool_preprocessing,
        )
        # Per-model UI overrides for temperature / max response length.
        _apply_gen_overrides(ctx)

        _research_flags = {"do": do_research}  # Mutable container for generator scope

        # Query active document — prefer explicit ID from frontend, fall back to session lookup
        active_doc = None
        _doc_db = SessionLocal()
        try:
            if active_doc_id:
                logger.info(f"[doc-inject] active_doc_id from frontend: {active_doc_id}")
                # Scope to the caller's documents. The session and in-memory
                # fallbacks below are already owner/session-bound; this
                # explicit-id path looked up by id alone, so a user could
                # inject another user's document by passing its id.
                _doc_q = _doc_db.query(DBDocument).filter(DBDocument.id == active_doc_id)
                active_doc = _owner_session_filter(_doc_q, ctx.user).first()
                if active_doc:
                    doc_session = active_doc.session_id
                    doc_owner = getattr(active_doc, "owner", None)
                    if doc_owner and ctx.user and doc_owner != ctx.user:
                        logger.warning(
                            "[doc-inject] ignoring active_doc_id %s owned by another user",
                            active_doc_id,
                        )
                        active_doc = None
                    else:
                        # NOTE: previously dropped the doc when doc.session_id
                        # != current chat session — but that broke the common
                        # case of "open an email draft from one chat, ask a
                        # different chat to write into it". The frontend only
                        # sends active_doc_id for docs currently visible in
                        # the UI, and we already owner-checked above, so trust
                        # the explicit signal. We just log the mismatch and
                        # re-bind the doc to the current session so future
                        # turns find it via the session-fallback path too.
                        if doc_session and doc_session != session:
                            logger.info(
                                "[doc-inject] cross-session active_doc_id %s (was session %s, now %s) — accepting and rebinding",
                                active_doc_id, doc_session, session,
                            )
                            try:
                                active_doc.session_id = session
                                _doc_db.commit()
                            except Exception as _e:
                                _doc_db.rollback()
                                logger.warning(f"[doc-inject] session rebind failed: {_e}")
                        logger.info(f"[doc-inject] found by ID: title={active_doc.title!r}, lang={active_doc.language!r}, is_active={active_doc.is_active}, content_len={len(active_doc.current_content or '')}")
                else:
                    logger.warning(f"[doc-inject] NOT FOUND by ID {active_doc_id}")
            if not active_doc:
                _email_doc_q = _doc_db.query(DBDocument).filter(
                    DBDocument.session_id == session,
                    DBDocument.is_active == True,
                    DBDocument.language == "email",
                )
                active_doc = _owner_session_filter(_email_doc_q, ctx.user).order_by(DBDocument.updated_at.desc()).first()
                if active_doc:
                    logger.info(f"[doc-inject] found email draft by session fallback: title={active_doc.title!r}")
            if not active_doc:
                _session_doc_q = _doc_db.query(DBDocument).filter(
                    DBDocument.session_id == session,
                    DBDocument.is_active == True
                )
                active_doc = _owner_session_filter(_session_doc_q, ctx.user).order_by(DBDocument.updated_at.desc()).first()
                if active_doc:
                    logger.info(f"[doc-inject] found by session fallback: title={active_doc.title!r}")
            # Last resort: the document the agent itself just created/edited
            # (tracked in-memory by the tool layer). This rescues docs that
            # got orphaned from their session (session_id NULL) — otherwise
            # neither lookup above can associate them with this conversation,
            # so the agent never sees what it just wrote. Guarded so we never
            # leak a doc that belongs to a DIFFERENT session.
            if not active_doc:
                try:
                    from src.agent_tools.document_tools import get_active_document
                    _mem_id = get_active_document()
                    if _mem_id:
                        _mem_q = _doc_db.query(DBDocument).filter(DBDocument.id == _mem_id)
                        cand = _owner_session_filter(_mem_q, ctx.user).first()
                        if cand and (not cand.session_id or cand.session_id == session):
                            active_doc = cand
                            logger.info(f"[doc-inject] found by in-memory active id: title={active_doc.title!r} (session_id={cand.session_id!r})")
                except Exception as _e:
                    logger.debug(f"[doc-inject] in-memory fallback failed: {_e}")
            if not active_doc:
                logger.info(f"[doc-inject] no active doc for session {session}")
            if active_doc:
                _doc_db.expunge(active_doc)
        except Exception as e:
            logger.warning(f"Failed to query active document: {e}")
        finally:
            _doc_db.close()

        # Build disabled-tools set from frontend toggles + user privileges
        disabled_tools = set()
        # Deep Research is its own dedicated function (the Research sidebar/button,
        # which hits /api/research/start directly). The chat agent must NOT be able
        # to auto-start research jobs — that let a single ordinary question spawn
        # multi-minute background jobs the user never asked for. Block only the
        # START tool; manage_research (read/list/delete existing reports) stays so
        # the agent can still open a report the user points at.
        disabled_tools.add("trigger_research")
        # ...unless the person asked for exactly that, in so many words ("do deep
        # research on …", "research the best …"), in a turn that has tools. Their
        # words decide (services/router.asks_for_research), not the model's
        # judgment, so an ordinary question still cannot start a job. Without
        # this the only way to start one from a chat was a typed command.
        _research_asked = False
        try:
            from services.router import may_start_research as _may_start_research
            if isinstance(message, str) and _may_start_research(message, _mode_as_sent, code_mode):
                disabled_tools.discard("trigger_research")
                _research_asked = True
                logger.info("research asked for by name: trigger_research allowed this turn")
        except Exception:
            logger.debug("research-request check skipped", exc_info=True)
        # Only disable bash when the caller *explicitly* set it to a falsy
        # value. When unset (None), defer to per-user privilege checks below.
        # Web search is per-turn opt-in: either the chat pre-search setting
        # (`use_web=true`) or agent web toggle (`allow_web_search=true`) must
        # explicitly enable it.
        if allow_bash is not None and str(allow_bash).lower() != "true":
            disabled_tools.add("bash")
        _explicit_web_intent = bool(_tool_intent and _tool_intent.category == "web")
        if is_web_search_explicitly_denied(allow_web_search) or not _search_enabled:
            disabled_tools.update(WEB_TOOL_NAMES)
        if _explicit_web_intent:
            # A direct lookup/search request should not drift into personal
            # tools or shell fallbacks. It can only use web_search/web_fetch
            # when the request's explicit web setting enabled them.
            disabled_tools.update({
                "bash", "python",
                "search_chats", "manage_skills", "manage_memory",
                "read_file", "write_file", "edit_file",
                "create_document", "edit_document", "update_document",
                "send_email", "reply_to_email",
                "manage_notes", "manage_calendar", "manage_tasks",
                "api_call", "builtin_browser",
            })
            if _search_enabled:
                disabled_tools.difference_update(WEB_TOOL_NAMES)
            else:
                disabled_tools.update(WEB_TOOL_NAMES)
        elif _search_enabled:
            disabled_tools.difference_update(WEB_TOOL_NAMES)

        # Nobody/incognito mode: deny tools that would expose the user's
        # persistent memory, past chats, or other identity-linked data.
        if incognito:
            disabled_tools.update({
                "manage_memory",      # persistent memory store
                "search_chats",       # past chat history
                "manage_skills",      # skill presets tied to user
            })

        # Active email reader open → strip the tools that let the agent drift
        # away from the visible email or skip review. The only allowed compose
        # path is ui_control open_email_reply, which opens the same draft editor
        # as the Reply button with the generated body pre-filled. This prevents
        # the model from falling back to direct SMTP when it botches a draft
        # call, and prevents fake email-shaped documents.
        if active_email_ctx and active_email_ctx.get("uid"):
            disabled_tools.update({
                "create_document",
                "send_email",
                "reply_to_email",
                "mcp__email__send_email",
                "mcp__email__reply_to_email",
            })

        # Enforce per-user privileges
        _privs = {}
        _user = ctx.user
        if _user and hasattr(request.app.state, 'auth_manager') and request.app.state.auth_manager:
            _privs = request.app.state.auth_manager.get_privileges(_user)
        # Which machine this chat's shell and file tools act on. Only an
        # administrator can point a chat at one; any other request (and any
        # turn that is not in Code mode) puts the chat back on this server.
        try:
            from services import workbench as _workbench
            from services.workbench import targets as _code_targets
            from src.tool_security import owner_is_admin_or_single_user as _is_admin_user
            _may_code = bool(code_mode and session and _workbench.enabled() and _is_admin_user(_user))
            if _may_code:
                code_target = await asyncio.to_thread(_code_targets.for_turn, str(session), code_host)
                logger.info("code mode: session=%s machine=%s folder=%s", session, code_target.get("host"),
                            code_target.get("folder") or "-")
            elif session:
                _code_targets.clear(str(session))
        except Exception as _code_err:
            logger.warning("code mode: could not set the machine for this turn: %s", _code_err)
            code_target = None
        if _privs:
            if not _privs.get("can_use_bash", True):
                disabled_tools.update({"bash", "python", "read_file", "write_file"})
            if not _privs.get("can_use_browser", True):
                disabled_tools.add("builtin_browser")
            if not _privs.get("can_use_documents", True):
                disabled_tools.update({"create_document", "edit_document", "update_document", "suggest_document"})
            if not _privs.get("can_generate_images", True):
                disabled_tools.add("generate_image")
            if not _privs.get("can_manage_memory", True):
                disabled_tools.update({"manage_memory", "manage_skills"})
            if not _privs.get("can_use_research", True):
                _research_flags["do"] = False
                disabled_tools.add("trigger_research")
            if not _privs.get("can_use_agent", True):
                _effective_mode = 'chat'
                chat_mode = 'chat'
                _auto_quick_lane = False
        # Global admin disabled tools
        from src.settings import get_setting
        _global_disabled = get_setting("disabled_tools", [])
        if _global_disabled and isinstance(_global_disabled, list):
            disabled_tools.update(_global_disabled)

        # Light auto-escalation: the user is in chat mode and just expressed a
        # notes/calendar/email intent. Grant the relevant managers but withhold
        # the heavy "do things on the computer" tools — otherwise the model
        # tries to shell out for a request that never needed it, then fails
        # (and looks broken when the shell is disabled).
        if auto_escalated:
            disabled_tools.update({
                "bash", "python", "read_file", "write_file", "builtin_browser",
            })

        # Disable document tools in compare sessions — they break the pane UI
        if sess.name and sess.name.startswith("[CMP]"):
            disabled_tools.update({"create_document", "edit_document", "update_document"})

        # Compare mode: disable tools based on compare type
        if compare_mode:
            _compare_strip = {
                "create_document", "edit_document", "update_document",
                "chat_with_model", "create_session", "list_sessions",
                "send_to_session",
                "pipeline", "manage_session", "manage_memory", "list_models",
                "generate_image", "ui_control",
            }
            disabled_tools.update(_compare_strip)
            # In chat mode compare, disable ALL agent tools (no bash, python, file ops)
            if chat_mode == 'chat':
                disabled_tools.update({"bash", "python", "read_file", "write_file", "web_search", "web_fetch", "search_chats", "manage_tasks"})

        # Plan mode: investigate read-only, propose a plan, don't mutate. Block
        # every tool not on the read-only allowlist. (stream_agent_loop enforces
        # this again + drops MCP, so this is belt-and-suspenders.)
        if plan_mode:
            from src.tool_security import plan_mode_disabled_tools
            disabled_tools.update(plan_mode_disabled_tools())

        tool_policy = build_effective_tool_policy(
            disabled_tools=disabled_tools,
            last_user_message=message,
        )
        disabled_tools = tool_policy.all_disabled_names()
        research_blocked_by_policy = bool(
            tool_policy.blocks("trigger_research")
            or tool_policy.blocks("manage_research")
        )
        effective_do_research = bool(
            do_research and _research_flags["do"] and not research_blocked_by_policy
        )

        # Persist session mode after policy/privilege gates so blocked research
        # turns remain ordinary chat/agent streams and saved messages.
        # (_mode_as_sent: a turn Auto answered in the quick lane still belongs to the mode the person chose.)
        _effective_mode = 'research' if effective_do_research else (('agent' if _auto_quick_lane else chat_mode) or 'chat')
        if _effective_mode in ('agent', 'research', 'chat'):
            set_session_mode(session, _effective_mode)

        async def stream_with_save() -> AsyncGenerator[str, None]:
            # _effective_mode is read-only here; closure captures it from
            # the outer scope. (Was `nonlocal` but never reassigned.)
            research_sources = None
            web_sources = ctx.web_sources

            # Register active stream for partial-save safety net
            _active_streams[session] = {"status": "streaming", "partial": "", "query": message, "is_research": effective_do_research, "mode": _effective_mode}

            # The client sent a workspace the server refused to bind (deleted
            # folder, file path, sensitive dir, filesystem root). Tell it up
            # front so the UI can clear the pill instead of displaying a
            # confinement that is not actually in effect.
            if workspace_rejected:
                yield f"data: {json.dumps({'type': 'workspace_rejected', 'data': {'path': workspace_rejected}})}\n\n"

            if ctx.preprocessed.attachment_meta:
                yield f"data: {json.dumps({'type': 'attachments', 'data': ctx.preprocessed.attachment_meta})}\n\n"

            # Announce any docs auto-created during preprocess (e.g. fillable
            # PDF → editable markdown) so the editor pane switches to them
            # before the model starts streaming.
            for _opened in ctx.auto_opened_docs:
                yield (
                    f'data: {json.dumps({"type": "doc_update", **_opened})}\n\n'
                )

            if ctx.rag_sources:
                yield f"data: {json.dumps({'type': 'rag_sources', 'data': ctx.rag_sources})}\n\n"

            if web_sources:
                yield f"data: {json.dumps({'type': 'web_sources', 'data': web_sources})}\n\n"

            # Emit which memories were injected into context (captured before stream)
            if ctx.used_memories:
                yield f"data: {json.dumps({'type': 'memories_used', 'data': ctx.used_memories})}\n\n"

            # Run research as a background task (survives page refresh)
            if effective_do_research:
                _r_ep, _r_model, _r_headers = _resolve_research_endpoint(sess)
                _auth_keys = list(_r_headers.keys()) if _r_headers else []
                logger.info(f"Research endpoint resolved: model={_r_model}, endpoint={redact_url(_r_ep)}, auth_keys={_auth_keys}, sess_headers_keys={list(sess.headers.keys()) if isinstance(sess.headers, dict) else type(sess.headers)}")

                # Clarification round: only for very short/vague queries on first research message.
                # Skip in compare mode — each pane is a fresh session, so every one would
                # ask clarifying questions and the user would have to answer each pane
                # separately, breaking the parallel comparison.
                _prior_json = research_handler._get_session_json(session)
                _history_len = len(sess.history) if hasattr(sess, 'history') else 0
                _is_first_research = not _prior_json and _history_len <= 2 and not compare_mode

                if _is_first_research:
                    logger.info(f"First research message — asking clarifying questions for: {message[:60]}")
                    yield f'data: {json.dumps({"type": "model_info", "model": sess.model, "suffix": "Research"})}\n\n'
                    # Set DB mode to research_pending so the NEXT message auto-triggers research
                    set_session_mode(session, "research_pending")
                    ctx.messages.insert(0, {"role": "system", "content":
                        "The user wants to start deep web research. Before searching, ask 2-3 brief "
                        "clarifying questions to understand exactly what they want to know. For example: "
                        "what aspects matter most, are they comparing to something, what's their context "
                        "(moving, traveling, curiosity). Be conversational. Keep it short."
                    })
                    _skip_research = True
                else:
                    _skip_research = False

                if not _skip_research:
                    # Phase 2: Start actual research
                    def _on_research_done(_sid, _result, _sources, _findings):
                        """Persist research to DB when background task finishes."""
                        if incognito:
                            return
                        try:
                            _s = session_manager.get_session(_sid)
                            if not _s:
                                logger.warning(f"Session {_sid} expired before research completed")
                                return
                            _md = {"research": True, "model": _s.model}
                            if _sources:
                                _md["research_sources"] = _sources
                            if _findings:
                                _md["research_findings"] = _findings
                            _clean_res, _md = clean_thinking_for_save(_result, _md)
                            _s.add_message(ChatMessage("assistant", _clean_res, metadata=_md))
                            session_manager.save_sessions()
                            logger.info(f"Research result persisted to DB for session {_sid}")
                        except Exception as _e:
                            logger.error(f"Failed to persist research to DB: {_e}")

                    # Check for prior research to continue from
                    _prior_report = ""
                    _prior_findings = None
                    _prior_urls = None
                    _prior_json = research_handler._get_session_json(session)
                    if _prior_json:
                        _prior_report = _prior_json.get("raw_report", "")
                        _prior_findings = _prior_json.get("raw_findings")
                        _src_urls = {s.get("url", "") for s in (_prior_json.get("sources") or []) if s.get("url")}
                        _prior_urls = _src_urls if _src_urls else None
                        if _prior_report:
                            logger.info(f"Continuing research for session {session} with {len(_src_urls)} prior URLs")

                    # Synthesize conversation into a focused research query
                    _research_query = await research_handler.synthesize_query(
                        sess, message, _r_ep, _r_model, _r_headers,
                    )
                    logger.info(f"Research query: {_research_query[:120]}")

                    research_handler.start_research(
                        session, _research_query, _r_ep, _r_model,
                        llm_headers=_r_headers,
                        prior_report=_prior_report,
                        prior_findings=_prior_findings,
                        prior_urls=_prior_urls,
                        on_complete=_on_research_done,
                        owner=_user,
                    )

                    _heartbeat_counter = 0
                    _last_progress = {}
                    _sent_avg = False
                    while True:
                        status = research_handler.get_status(session)
                        if not status or status["status"] != "running":
                            break
                        progress = status.get("progress", {})
                        if progress and progress != _last_progress:
                            _last_progress = progress
                            if not _sent_avg:
                                _sent_avg = True
                                progress = dict(progress)
                                progress["started_at"] = status.get("started_at")
                                avg = status.get("avg_duration")
                                if avg:
                                    progress["avg_duration"] = avg
                            yield f"data: {json.dumps({'type': 'research_progress', 'data': progress})}\n\n"
                            _heartbeat_counter = 0
                        else:
                            _heartbeat_counter += 1
                            yield f": heartbeat {_heartbeat_counter}\n\n"
                        await asyncio.sleep(1.0)

                    research_sources = research_handler.get_sources(session)
                    if research_sources:
                        yield f"data: {json.dumps({'type': 'research_sources', 'data': research_sources})}\n\n"

                    research_findings = research_handler.get_raw_findings(session)
                    if research_findings:
                        yield f"data: {json.dumps({'type': 'research_findings', 'data': research_findings})}\n\n"

                    # Signal frontend to fetch and render the research result
                    yield f"data: {json.dumps({'type': 'research_done', 'data': {'session_id': session}})}\n\n"
                    yield "data: [DONE]\n\n"
                    research_handler.clear_result(session)
                    _stream_set(session, status="done")
                    _active_streams.pop(session, None)
                    return

            messages = _ensure_current_request_is_latest_user(ctx.messages, message)

            # Auto-compact notification
            if ctx.was_compacted:
                yield f"data: {json.dumps({'type': 'compacted', 'context_length': ctx.context_length})}\n\n"
            if ctx.context_trimmed and not ctx.was_compacted:
                yield f"data: {json.dumps({'type': 'context_trimmed', 'data': {'context_length': ctx.context_length, 'messages_before': ctx.context_messages_before_trim, 'messages_after': ctx.context_messages_after_trim, 'tokens_before': ctx.context_tokens_before_trim, 'tokens_after': ctx.context_tokens_after_trim}})}\n\n"

            full_response = ""
            thinking_response = ""
            # How long the model spent thinking, summed over the rounds of the answer: kept with the
            # message so a reopened chat can still say "Thought for 6 s".
            _think_clock = {"since": None, "total": 0.0}

            def _note_thinking(is_thinking: bool) -> None:
                now = time.time()
                if is_thinking:
                    if _think_clock["since"] is None:
                        _think_clock["since"] = now
                elif _think_clock["since"] is not None:
                    _think_clock["total"] += now - _think_clock["since"]
                    _think_clock["since"] = None
            last_metrics = None

            # Configured fallback chain for the default chat model. Tried in
            # order if the session's primary model fails before producing
            # output. Resolved once per request.
            try:
                from src.endpoint_resolver import resolve_chat_fallback_candidates
                _fallback_candidates = resolve_chat_fallback_candidates(owner=_user)
            except Exception:
                _fallback_candidates = []

            # A render fills the GPU, and llama-swap frees VRAM by evicting —
            # so answering this turn on a GPU model would stop the render dead.
            # Route to the CPU model instead while the card is busy. Returns None
            # (the common case) whenever there's nothing to protect.
            try:
                from src import gpu_guard as _gpu_guard_mod
                _busy_swap = _gpu_guard_mod.busy_swap(sess.model, sess.endpoint_url, owner=_user)
            except Exception:
                logger.debug("GPU busy-swap check failed", exc_info=True)
                _gpu_guard_mod, _busy_swap = None, None
            _turn_model = _busy_swap["model"] if _busy_swap else (_auto_model or sess.model)

            # Send model name early so the frontend can show it during streaming
            _model_suffix = "Research" if effective_do_research else None
            _model_info = {"type": "model_info", "model": _turn_model}
            if _busy_swap:
                # Be visible about the downgrade rather than silently answering
                # on a 3B — and tell them how to switch it off.
                _model_info["notice"] = _busy_swap["notice"]
                _model_info["busy_fallback"] = True
                _model_info["original_model"] = _busy_swap["original_model"]
            if _model_suffix:
                _model_info["suffix"] = _model_suffix
            if _auto_route is not None and _auto_route.moved and not _busy_swap:
                _model_info["auto"] = _auto_route.as_event()          # "quick" / "careful", and why
                if _auto_model:
                    _model_info["requested_model"] = sess.model
            if ctx.preset.character_name:
                _model_info["character_name"] = ctx.preset.character_name
            yield f'data: {json.dumps(_model_info)}\n\n'

            # Honest waiting (services/waiting.py): say so when the model has to
            # be woken first, or when a smaller helper answers because the
            # machine is busy — in the same plain words on every screen.
            _woke_from = None
            try:
                from services import waiting as _waiting
                if _busy_swap:
                    yield f'data: {json.dumps(_waiting.busy())}\n\n'
                else:
                    _asleep = await _waiting.waking(sess.endpoint_url, _turn_model)
                    if _asleep:
                        _woke_from = time.time()
                        yield f'data: {json.dumps(_asleep)}\n\n'
            except Exception:
                logger.debug("waiting notice skipped", exc_info=True)
            _offline_said = False

            if _is_image_generation_session(sess, owner=_user):
                from src.settings import get_setting
                if tool_policy.blocks("generate_image"):
                    _blocked_msg = tool_policy.reason_for("generate_image")
                    yield f'data: {json.dumps({"delta": _blocked_msg})}\n\n'
                    yield "data: [DONE]\n\n"
                    _active_streams.pop(session, None)
                    return
                if not get_setting("image_gen_enabled", True):
                    yield f'data: {json.dumps({"delta": "Image generation is disabled by the administrator."})}\n\n'
                    yield "data: [DONE]\n\n"
                    _active_streams.pop(session, None)
                    return
                from src.ai_interaction import do_generate_image
                _user_msg = message or ""
                yield f'data: {json.dumps({"type": "tool_start", "tool": "generate_image", "command": _user_msg[:100]})}\n\n'
                yield ": heartbeat\n\n"
                _img_result = await do_generate_image(f"{_user_msg}\n{sess.model}", session, owner=_user)
                _img_output = _img_result.get("results", _img_result.get("error", ""))
                _img_tool_data = {"type": "tool_output", "tool": "generate_image", "command": _user_msg[:100], "output": _img_output, "exit_code": 0 if "error" not in _img_result else 1}
                for _k in ("image_url", "image_id", "image_prompt", "image_model", "image_size", "image_quality"):
                    if _k in _img_result:
                        _img_tool_data[_k] = _img_result[_k]
                yield f'data: {json.dumps(_img_tool_data)}\n\n'
                _desc = _img_result.get("results", _img_result.get("error", "Image generation complete"))
                full_response = _desc
                yield f'data: {json.dumps({"delta": _desc})}\n\n'
                # Save to session history
                if not incognito:
                    _ev = {"round": 1, "tool": "generate_image", "command": _user_msg[:100], "output": _img_output, "exit_code": 0 if "error" not in _img_result else 1}
                    for _ek in ("image_url", "image_id", "image_prompt", "image_model", "image_size", "image_quality"):
                        if _img_result.get(_ek):
                            _ev[_ek] = _img_result[_ek]
                    sess.add_message(ChatMessage("assistant", full_response, metadata={"tool_events": [_ev], "model": sess.model}))
                    session_manager.save_sessions()
                yield f'data: {json.dumps({"type": "metrics", "data": {"total_time": 0}})}\n\n'
                yield "data: [DONE]\n\n"
                _active_streams.pop(session, None)
                return
            elif chat_mode == "chat":
                _chat_start = time.time()
                _answered_by = None  # set if the selected model failed and a fallback answered
                _requested_model = sess.model
                _actual_model = None
                # ── Chat mode: call stream_llm directly, NO tools, NO document access ──
                try:
                    # _turn_model is sess.model unless a render holds the GPU, in
                    # which case it's the CPU model so this turn can't evict it.
                    _chat_candidates = [(sess.endpoint_url, _turn_model, sess.headers)] + _fallback_candidates
                    async for chunk in stream_llm_with_fallback(
                        _chat_candidates,
                        messages,
                        temperature=ctx.preset.temperature,
                        # Respect the preset; 0/unset = let the server decide (no
                        # cap), matching agent mode. The old hard 4096 fallback
                        # truncated reasoning models mid-<think> — they'd burn the
                        # whole budget thinking and never emit the answer (seen in
                        # Compare on heavy generation prompts).
                        max_tokens=ctx.preset.max_tokens,
                        prompt_type=preset_id,
                        tools=None,
                        session_id=session,
                        progress=True,
                    ):
                        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                            try:
                                data = json.loads(chunk[6:])
                                if data.get("type") == "reading":
                                    yield chunk                      # how far the model has read: the wait before its first word
                                elif "delta" in data:
                                    # Reasoning tokens arrive flagged thinking:true.
                                    # Forward them so the client can show a thinking
                                    # indicator, but don't fold them into the saved
                                    # reply (mirrors the rewrite path below).
                                    _note_thinking(bool(data.get("thinking")))
                                    if data.get("thinking"):
                                        thinking_response += data["delta"]
                                    else:
                                        full_response += data["delta"]
                                        _stream_set(session, partial=full_response)
                                    if _woke_from is not None:       # its first word after being woken: next time's estimate
                                        try:
                                            _waiting.note_woke(_turn_model, time.time() - _woke_from)
                                        except Exception:
                                            pass
                                        _woke_from = None
                                    yield chunk
                                elif data.get("type") == "fallback":
                                    # Selected model failed; a fallback answered.
                                    # Forward the notice and remember the real model.
                                    _answered_by = data.get("answered_by") or _answered_by
                                    _actual_model = _actual_model or _answered_by
                                    data["selected_model"] = data.get("selected_model") or _requested_model
                                    yield chunk
                                elif data.get("type") == "model_actual":
                                    _actual_model = data.get("model") or _actual_model
                                    data["requested_model"] = _requested_model
                                    yield f'data: {json.dumps(data)}\n\n'
                                elif data.get("type") == "usage":
                                    last_metrics = data.get("data", {})
                                    _reported_model = last_metrics.get("model")
                                    last_metrics["requested_model"] = _requested_model
                                    last_metrics["model"] = _reported_model or _actual_model or _answered_by or _requested_model
                                    if ctx.context_trimmed:
                                        last_metrics["context_trimmed"] = True
                                        last_metrics["context_messages_before_trim"] = ctx.context_messages_before_trim
                                        last_metrics["context_messages_after_trim"] = ctx.context_messages_after_trim
                                        last_metrics["context_tokens_before_trim"] = ctx.context_tokens_before_trim
                                        last_metrics["context_tokens_after_trim"] = ctx.context_tokens_after_trim
                                    if ctx.context_length and last_metrics.get("input_tokens"):
                                        pct = min(round((last_metrics["input_tokens"] / ctx.context_length) * 100, 1), 100.0)
                                        last_metrics["context_percent"] = pct
                                        last_metrics["context_length"] = ctx.context_length
                                    # The frontend reads `tokens_per_second`; the raw usage event
                                    # carries the backend's true gen speed as `gen_tps` (llama.cpp
                                    # timings). Map it through so this direct-chat path shows real
                                    # t/s instead of "n/a" → falling back to a bare token count.
                                    if last_metrics.get("gen_tps") and not last_metrics.get("tokens_per_second"):
                                        last_metrics["tokens_per_second"] = last_metrics["gen_tps"]
                                        last_metrics["tps_source"] = "backend"
                                    # Wall-clock response time for the stats popup ("Time").
                                    last_metrics.setdefault("response_time", round(time.time() - _chat_start, 2))
                                    yield f'data: {json.dumps({"type": "metrics", "data": last_metrics})}\n\n'
                            except json.JSONDecodeError:
                                yield chunk
                        elif chunk.startswith("event: error"):
                            logger.warning(f"Stream error for {sess.model} on {sess.endpoint_url}: {chunk!r}")
                            _note_failure(session, f"{sess.model}: {chunk[:500]}")
                            yield chunk
                        elif chunk.startswith("event: "):
                            yield chunk
                        elif chunk == "data: [DONE]\n\n":
                            # Generate fallback metrics if LLM didn't send usage
                            if not last_metrics and full_response:
                                _elapsed = time.time() - _chat_start
                                _est_in = estimate_tokens(messages)
                                _est_out = len(full_response) // 4
                                _tps = round(_est_out / _elapsed, 2) if _elapsed > 0 else 0
                                _ctx_pct = min(round((_est_in / ctx.context_length) * 100, 1), 100.0) if ctx.context_length else 0
                                last_metrics = {
                                    "response_time": round(_elapsed, 2),
                                    "input_tokens": _est_in,
                                    "output_tokens": _est_out,
                                    "tokens_per_second": _tps,
                                    "context_percent": _ctx_pct,
                                    "context_length": ctx.context_length,
                                    "model": _actual_model or _answered_by or _requested_model,
                                    "requested_model": _requested_model,
                                    "usage_source": "estimated",
                                }
                                yield f'data: {json.dumps({"type": "metrics", "data": last_metrics})}\n\n'
                            if full_response:
                                _metrics_to_save = dict(last_metrics or {})
                                if thinking_response.strip() and not _metrics_to_save.get("thinking"):
                                    _metrics_to_save["thinking"] = thinking_response.strip()
                                    _note_thinking(False)
                                    if _think_clock["total"] >= 0.5:
                                        _metrics_to_save["thinking_time"] = round(_think_clock["total"], 1)
                                _saved_id = save_assistant_response(
                                    sess, session_manager, session, full_response, _metrics_to_save,
                                    character_name=ctx.preset.character_name,
                                    web_sources=web_sources,
                                    rag_sources=ctx.rag_sources,
                                    research_sources=research_sources,
                                    used_memories=ctx.used_memories,
                                    do_research=effective_do_research,
                                    incognito=incognito,
                                )
                                if _saved_id:
                                    yield f'data: {json.dumps({"type": "message_saved", "id": _saved_id})}\n\n'
                                run_post_response_tasks(
                                    sess, session_manager, session, message, full_response,
                                    _metrics_to_save, ctx.uprefs, memory_manager, memory_vector, webhook_manager,
                                    incognito=incognito, compare_mode=compare_mode,
                                    character_name=ctx.preset.character_name,
                                    owner=_user,
                                    allow_background_extraction=not tool_policy.block_all_tool_calls,
                                )
                            _stream_set(session, status="done")
                            yield chunk
                except (asyncio.CancelledError, GeneratorExit):
                    if full_response:
                        logger.info("Client disconnected mid-stream (chat mode) for session %s, saving partial (%d chars)", session, len(full_response))
                        _stopped_content, _stopped_md = clean_thinking_for_save(
                            full_response,
                            {
                                "stopped": True,
                                "model": _actual_model or _answered_by or _requested_model,
                                "requested_model": _requested_model,
                            },
                        )
                        sess.add_message(ChatMessage("assistant", _stopped_content, metadata=_stopped_md), persist=not incognito)
                        if not incognito:
                            session_manager.save_sessions()
                    raise
                finally:
                    _active_streams.pop(session, None)
            else:
                # ── Agent mode: full agent loop with tools ──
                _agent_rounds = 0
                _agent_tool_calls = 0
                _tools_used: set = set()
                _answered_by = None  # set if the selected model failed and a fallback answered
                _requested_model = sess.model
                _actual_model = None
                try:
                    from src.settings import get_setting
                    from src.agent_tools import MAX_AGENT_ROUNDS as _DEFAULT_ROUNDS
                    # Per-message tool budget from settings; guard defensively in
                    # case settings.json was hand-edited to a non-numeric value
                    # (the HTTP admin endpoint validates, but direct edits bypass
                    # it). 0 = unlimited, matching auth_routes set_settings().
                    try:
                        _tool_budget = int(get_setting("agent_max_tool_calls", 0))
                    except (TypeError, ValueError):
                        _tool_budget = 0
                    # Per-message round cap from settings; clamp defensively in
                    # case settings.json was hand-edited to a bad value.
                    try:
                        _max_rounds = int(get_setting("agent_max_rounds", _DEFAULT_ROUNDS) or _DEFAULT_ROUNDS)
                    except (TypeError, ValueError):
                        _max_rounds = _DEFAULT_ROUNDS
                    _max_rounds = max(1, min(_max_rounds, 200))

                    # The web tools are allowed whenever the web is (nobody
                    # presses a button for it now); they are pressed on the
                    # assistant only when the message is about something dated
                    # or asks for a lookup (services/router.wants_web). For
                    # the rest, tool retrieval offers them when they fit and
                    # the offline library comes first.
                    _forced_tools = None
                    _web_wanted = str(use_web).lower() == "true" or bool(_tool_intent and _tool_intent.category == "web")
                    if not _web_wanted:
                        try:
                            from services.router import wants_web as _wants_web
                            _web_wanted = bool(isinstance(message, str) and _wants_web(message))
                        except Exception:
                            _web_wanted = True
                    if _search_enabled and _web_wanted:
                        _forced_tools = set(WEB_TOOL_NAMES)
                    # A smart tool (services/smart_tools.py): its blades are
                    # put in front of the assistant and its steps go with them.
                    _smart = None
                    try:
                        if isinstance(message, str) and not code_mode and not compare_mode and not active_doc_id and not active_email_uid:
                            from services import smart_tools as _smart_tools
                            _smart = _smart_tools.pick(message)
                            if _smart is not None:
                                _forced_tools = set(_forced_tools or ()) | {t for t in _smart.tools if t not in disabled_tools}
                                yield f'data: {json.dumps({"type": "smart", "key": _smart.key, "title": _smart.title})}\n\n'
                    except Exception:
                        logger.debug("smart tool check skipped", exc_info=True)
                        _smart = None
                    # Asked for by name: the research tool is offered whatever
                    # tool retrieval picked, and the loop is told to use it.
                    if _research_asked and "trigger_research" not in disabled_tools:
                        _forced_tools = set(_forced_tools or ()) | {"trigger_research"}

                    async for chunk in stream_agent_loop(
                        sess.endpoint_url,
                        # _turn_model is sess.model unless a render holds the
                        # GPU, in which case it's the CPU model — otherwise this
                        # turn would evict sd-server and kill the render.
                        _turn_model,
                        messages,
                        headers=sess.headers,
                        temperature=ctx.preset.temperature,
                        max_tokens=ctx.preset.max_tokens,
                        prompt_type=preset_id,
                        max_tool_calls=_tool_budget,
                        max_rounds=_max_rounds,
                        # ctx.context_length is sized for sess.model; when we've
                        # swapped, clamp to the CPU model's smaller window.
                        context_length=(
                            _gpu_guard_mod.clamp_context(ctx.context_length, _busy_swap)
                            if (_gpu_guard_mod and _busy_swap) else ctx.context_length
                        ),
                        active_document=active_doc,
                        active_email=active_email_ctx,
                        session_id=session,
                        disabled_tools=disabled_tools if disabled_tools else None,
                        tool_policy=tool_policy,
                        owner=_user,
                        fallbacks=_fallback_candidates,
                        plan_mode=plan_mode,
                        approved_plan=approved_plan or None,
                        workspace=workspace or None,
                        forced_tools=_forced_tools,
                        code_target=code_target,
                        uploaded_files=ctx.uploaded_files,
                        active_toolboxes=_active_toolboxes,
                        research_requested=bool(_research_asked and "trigger_research" not in disabled_tools),
                        smart_tool=_smart.key if _smart is not None else None,
                    ):
                        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                            try:
                                data = json.loads(chunk[6:])
                                if "delta" in data:
                                    # Reasoning tokens arrive flagged thinking:true.
                                    # Forward them for the live indicator, but keep
                                    # them out of the saved reply (same as chat mode).
                                    _note_thinking(bool(data.get("thinking")))
                                    if data.get("thinking"):
                                        thinking_response += data["delta"]
                                    else:
                                        full_response += data["delta"]
                                        _stream_set(session, partial=full_response)
                                    if _woke_from is not None:       # its first word after being woken: next time's estimate
                                        try:
                                            _waiting.note_woke(_turn_model, time.time() - _woke_from)
                                        except Exception:
                                            pass
                                        _woke_from = None
                                    yield chunk
                                elif data.get("type") == "web_sources":
                                    web_sources = data.get("data", [])
                                    yield chunk
                                elif data.get("type") in (
                                    "tool_start", "tool_output", "agent_step",
                                    "doc_stream_open", "doc_stream_delta",
                                    "doc_update", "doc_suggestions", "ui_control",
                                    "rounds_exhausted",
                                    "ask_user",
                                    "plan_update",
                                    # Signs of life while nothing is being written yet: how far the
                                    # model has read, what a long-running command has printed so far,
                                    # and why a run was cut short. The screen draws all three; they
                                    # were made here and never sent on.
                                    "reading", "tool_progress", "budget_exceeded",
                                ):
                                    if data.get("type") == "agent_step":
                                        _agent_rounds = max(_agent_rounds, data.get("round", 1))
                                    elif data.get("type") == "tool_start":
                                        _note_thinking(False)        # the thinking ended in a tool call
                                        _agent_tool_calls += 1
                                        _tools_used.add(str(data.get("tool") or ""))
                                    yield chunk
                                    if data.get("type") == "tool_start" and data.get("tool") in WEB_TOOL_NAMES and not _offline_said:
                                        # The search answers from the offline library by itself when the internet
                                        # is down; say so, so nobody takes a library page for today's web.
                                        try:
                                            from services.search import netstate as _net
                                            if not _net.internet_available():
                                                _offline_said = True
                                                yield f'data: {json.dumps(_waiting.OFFLINE)}\n\n'
                                        except Exception:
                                            pass
                                elif data.get("type") == "fallback":
                                    # Selected model failed; a fallback answered.
                                    # Forward the notice and remember the real
                                    # model so metrics reflect it, not the masked
                                    # selected model.
                                    _answered_by = data.get("answered_by") or _answered_by
                                    _actual_model = _actual_model or _answered_by
                                    data["selected_model"] = data.get("selected_model") or _requested_model
                                    yield chunk
                                elif data.get("type") == "model_actual":
                                    _actual_model = data.get("model") or _actual_model
                                    data["requested_model"] = _requested_model
                                    yield f'data: {json.dumps(data)}\n\n'
                                elif data.get("type") == "metrics":
                                    last_metrics = data.get("data", {})
                                    _reported_model = last_metrics.get("model")
                                    last_metrics["requested_model"] = last_metrics.get("requested_model") or _requested_model
                                    last_metrics["model"] = _reported_model or _actual_model or _answered_by or _requested_model
                                    if ctx.context_trimmed:
                                        last_metrics["context_trimmed"] = True
                                        last_metrics["context_messages_before_trim"] = ctx.context_messages_before_trim
                                        last_metrics["context_messages_after_trim"] = ctx.context_messages_after_trim
                                        last_metrics["context_tokens_before_trim"] = ctx.context_tokens_before_trim
                                        last_metrics["context_tokens_after_trim"] = ctx.context_tokens_after_trim
                                    yield f'data: {json.dumps({"type": "metrics", "data": last_metrics})}\n\n'
                            except json.JSONDecodeError:
                                yield chunk
                        elif chunk.startswith("event: "):
                            if chunk.startswith("event: error"):
                                _note_failure(session, f"{sess.model}: {chunk[:500]}")
                            yield chunk
                        elif chunk == "data: [DONE]\n\n":
                            _has_tool_events = bool((last_metrics or {}).get("tool_events"))
                            if full_response or _has_tool_events:
                                _response_to_save = full_response or "Done."
                                _metrics_to_save = dict(last_metrics or {})
                                if thinking_response.strip() and not _metrics_to_save.get("thinking"):
                                    _metrics_to_save["thinking"] = thinking_response.strip()
                                    _note_thinking(False)
                                    if _think_clock["total"] >= 0.5:
                                        _metrics_to_save["thinking_time"] = round(_think_clock["total"], 1)
                                _saved_id = save_assistant_response(
                                    sess, session_manager, session, _response_to_save, _metrics_to_save,
                                    character_name=ctx.preset.character_name,
                                    web_sources=web_sources,
                                    rag_sources=ctx.rag_sources,
                                    used_memories=ctx.used_memories,
                                    incognito=incognito,
                                )
                                if _saved_id:
                                    yield f'data: {json.dumps({"type": "message_saved", "id": _saved_id})}\n\n'
                                run_post_response_tasks(
                                    sess, session_manager, session, message, _response_to_save,
                                    _metrics_to_save, ctx.uprefs, memory_manager, memory_vector, webhook_manager,
                                    incognito=incognito, compare_mode=compare_mode,
                                    character_name=ctx.preset.character_name,
                                                            agent_rounds=_agent_rounds,
                                    agent_tool_calls=_agent_tool_calls,
                                    skills_manager=skills_manager,
                                    owner=_user,
                                    extract_skills=user_requested_agent,
                                    allow_background_extraction=not tool_policy.block_all_tool_calls,
                                )
                            # A comparison, a purchase, a plan with many parts: the short answer
                            # stands, and one button under it offers the full report. Nobody has
                            # to know the feature exists or the words that start it.
                            try:
                                if (full_response and not _research_asked and "trigger_research" not in _tools_used
                                        and _privs.get("can_use_research", True) and not incognito and not compare_mode and not code_mode):
                                    from services.router import report_offer as _report_offer
                                    _offer = _report_offer(message if isinstance(message, str) else "")
                                    if _offer:
                                        yield f'data: {json.dumps(_offer)}\n\n'
                            except Exception:
                                logger.debug("report offer skipped", exc_info=True)
                            _stream_set(session, status="done")
                            yield chunk
                except (asyncio.CancelledError, GeneratorExit):
                    # Client disconnected — save partial response. Wrap
                    # the save in its own try so an exception inside
                    # add_message / save_sessions doesn't mask the
                    # original CancelledError (which prevented the
                    # outer finally from running and left _active_streams
                    # with a stale entry).
                    try:
                        if full_response:
                            logger.info("Client disconnected mid-stream for session %s, saving partial response (%d chars)", session, len(full_response))
                            _stopped_content2, _stopped_md2 = clean_thinking_for_save(
                                full_response,
                                {
                                    "stopped": True,
                                    "model": _actual_model or _answered_by or _requested_model,
                                    "requested_model": _requested_model,
                                },
                            )
                            sess.add_message(ChatMessage("assistant", _stopped_content2, metadata=_stopped_md2), persist=not incognito)
                            if not incognito:
                                session_manager.save_sessions()
                    except Exception:
                        logger.exception("Failed to save partial response on disconnect (session %s)", session)
                    raise
                except Exception as _loop_exc:
                    # The agent loop raised mid-stream (e.g. a tool result that
                    # won't JSON-serialize, or an uncaught tool error) BEFORE
                    # it emitted its own [DONE]. Without this branch the save
                    # (gated on [DONE]) never ran → the turn reloaded blank, and
                    # in compare mode the pane hung with no terminal event.
                    # Save whatever we have and terminate the stream cleanly.
                    logger.exception("Agent stream failed mid-loop (session %s)", session)
                    _note_failure(session, f"{type(_loop_exc).__name__}: {_loop_exc}")
                    try:
                        if full_response:
                            _err_content, _err_md = clean_thinking_for_save(
                                full_response,
                                {
                                    "stopped": True,
                                    "error": str(_loop_exc)[:200],
                                    "model": _actual_model or _answered_by or _requested_model,
                                    "requested_model": _requested_model,
                                },
                            )
                            sess.add_message(ChatMessage("assistant", _err_content, metadata=_err_md), persist=not incognito)
                            if not incognito:
                                session_manager.save_sessions()
                    except Exception:
                        logger.exception("Failed to save partial response after loop error (session %s)", session)
                    _stream_set(session, status="error")
                    try:
                        yield f'data: {json.dumps({"type": "error", "error": "The response was interrupted by an internal error."})}\n\n'
                    except Exception:
                        pass
                    yield "data: [DONE]\n\n"
                finally:
                    _active_streams.pop(session, None)

        async def _safe_stream() -> AsyncGenerator[str, None]:
            """Wrapper that guarantees _active_streams cleanup even if stream_with_save
            raises before reaching a mode-specific finally block."""
            try:
                async for chunk in stream_with_save():
                    yield chunk
            finally:
                _active_streams.pop(session, None)

        # Compare panes are short-lived, single-shot generations whose sessions
        # exist only to drive that one pane — there's nothing to "resume" and
        # the user expects the pane's Stop button (which aborts the fetch,
        # closing this SSE) to promptly cancel the upstream LLM call. Detaching
        # them would keep burning upstream tokens/compute after the pane is
        # stopped or the comparison is abandoned, and would surface a stale
        # "still streaming" /resume target for a session nobody will revisit.
        #
        # So: stream them directly (no agent_runs wrapping). Starlette cancels
        # the underlying async generator (raising CancelledError/GeneratorExit
        # inside it) as soon as it notices the client disconnected — which the
        # mode-specific except blocks above already handle by saving the
        # partial response exactly once. This stops the upstream call promptly
        # without waiting on the next streamed chunk.
        #
        # Normal chat/agent streams keep the DETACHED behavior below: they
        # survive the client closing the tab / navigating away. The SSE response just subscribes (replay
        # buffered output + live); dropping the SSE only removes a subscriber —
        # the run keeps going and saves the assistant message on completion
        # regardless. Reconnect via /api/chat/resume.
        if compare_mode:
            return StreamingResponse(_safe_stream(), media_type="text/event-stream")

        agent_runs.start(session, _safe_stream())
        return StreamingResponse(agent_runs.subscribe(session), media_type="text/event-stream")

    # ------------------------------------------------------------------ #
    # GET /api/chat/resume — reconnect to a detached run that's still going
    # (e.g. after reopening a session whose agent kept running in the background)
    # ------------------------------------------------------------------ #
    @router.get("/api/chat/resume/{session_id}")
    async def chat_resume(request: Request, session_id: str) -> StreamingResponse:
        _verify_session_owner(request, session_id)
        if not agent_runs.is_active(session_id):
            raise HTTPException(404, "No active run for this session")
        return StreamingResponse(agent_runs.subscribe(session_id), media_type="text/event-stream")

    # ------------------------------------------------------------------ #
    # POST /api/chat/stop — cancel a detached run (Stop button). Closing the SSE
    # no longer stops it (it's detached), so the Stop button must call this.
    # ------------------------------------------------------------------ #
    @router.post("/api/chat/stop/{session_id}")
    async def chat_stop(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        stopped = agent_runs.stop(session_id)
        return {"stopped": stopped}

    # ------------------------------------------------------------------ #
    # GET /api/chat/stream_status — check if a stream is active for a session
    # ------------------------------------------------------------------ #
    @router.get("/api/chat/stream_status/{session_id}")
    async def chat_stream_status(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        # A detached run can still be going even if _active_streams was popped;
        # report it as active so the client knows to reconnect via /resume.
        # Read once via .get() to avoid a KeyError race between the membership
        # check and the indexed read if a sibling stream's finally pops the
        # entry in between (same pattern _stream_set already uses).
        rec = _active_streams.get(session_id)
        if rec is None:
            if agent_runs.is_active(session_id):
                return {"status": "streaming", "detached": True}
            raise HTTPException(404, "No active stream for this session")
        return rec

    # ------------------------------------------------------------------ #
    # POST /api/inject_context
    # ------------------------------------------------------------------ #
    @router.post("/api/inject_context/{session_id}")
    async def inject_context(request: Request, session_id: str, context: str = Form(...)) -> Dict[str, str]:
        _verify_session_owner(request, session_id)
        try:
            sess = session_manager.get_session(session_id)
            msg = untrusted_context_message("injected research context", f"Research Context: {context}")
            sess.add_message(ChatMessage(msg["role"], msg["content"], metadata=msg.get("metadata")))
            session_manager.save_sessions()
            return {"status": "context_injected"}
        except KeyError:
            raise HTTPException(404, "Session not found")

    # ------------------------------------------------------------------ #
    # GET /api/search — search across chat messages
    # ------------------------------------------------------------------ #
    @router.get("/api/search")
    async def search_messages(
        request: Request,
        q: str = Query("", min_length=0),
        limit: int = Query(20, ge=1, le=100),
    ) -> List[Dict[str, Any]]:
        if not q or not q.strip():
            return []

        _user = effective_user(request)
        return [
            result.to_dict()
            for result in search_session_messages(
                q,
                limit=limit,
                owner=_user,
                restrict_owner=_user is not None,
                include_legacy_owner=False,
            )
        ]

    # ------------------------------------------------------------------ #
    # POST /api/rewrite — lightweight rewrite of last AI message (no tools)
    # ------------------------------------------------------------------ #
    @router.post("/api/rewrite")
    async def rewrite_message(request: Request) -> StreamingResponse:
        """Rewrite the last AI message with an instruction (shorter/simpler/etc).

        Unlike the full chat pipeline, this does NOT run the agent loop or tools.
        It just asks the LLM to rewrite the given text.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        session_id = body.get("session_id")
        original_text = body.get("original_text", "")
        instruction = body.get("instruction", "")

        if not session_id or not original_text or not instruction:
            raise HTTPException(400, "session_id, original_text, and instruction are required")

        _verify_session_owner(request, session_id)

        try:
            sess = session_manager.get_session(session_id)
        except (KeyError, SessionNotFoundError):
            raise HTTPException(404, "Session not found")

        messages = [
            {"role": "system", "content": (
                "You are rewriting a previous response. Follow the instruction exactly. "
                "Output ONLY the rewritten text — no preamble, no explanation, no meta-commentary. "
                "Preserve any formatting (markdown, code blocks, lists) from the original."
            )},
            {"role": "user", "content": (
                f"Here is the original response:\n\n{original_text}\n\n"
                f"Instruction: {instruction}"
            )},
        ]

        async def stream_rewrite() -> AsyncGenerator[str, None]:
            full_response = ""
            try:
                async for chunk in stream_llm(
                    sess.endpoint_url,
                    sess.model,
                    messages,
                    headers=sess.headers,
                    temperature=0.7,
                    # 0 = let the server decide (no cap). A hardcoded 4096 made
                    # local reasoning models (Qwen3 / R1) burn the whole budget
                    # inside <think> and emit no rewrite — the bubble just hung
                    # on "Rewriting...". Same fix as the chat max_tokens cap.
                    max_tokens=0,
                    tools=None,
                ):
                    if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                        try:
                            data = json.loads(chunk[6:])
                            if "delta" in data:
                                # Forward the chunk (so the client can show a
                                # thinking indicator) but DON'T fold reasoning
                                # tokens into the saved rewrite — only real
                                # content. reasoning_content arrives flagged
                                # with thinking:true.
                                if not data.get("thinking"):
                                    full_response += data["delta"]
                                yield chunk
                        except json.JSONDecodeError:
                            yield chunk
                    elif chunk.startswith("event: "):
                        yield chunk
                    elif chunk == "data: [DONE]\n\n":
                        # Update the last assistant message in session history.
                        # Strip reasoning-model <think> blocks so the persisted
                        # rewrite is just the rewritten text, not its scratchpad.
                        from src.research_utils import strip_thinking
                        full_response = strip_thinking(full_response).strip() or full_response
                        if full_response:
                            for msg in reversed(sess.history):
                                if (isinstance(msg, ChatMessage) and msg.role == 'assistant') or \
                                   (isinstance(msg, dict) and msg.get('role') == 'assistant'):
                                    if isinstance(msg, ChatMessage):
                                        msg.content = full_response
                                    else:
                                        msg['content'] = full_response
                                    break
                            # Update in DB too
                            db = SessionLocal()
                            try:
                                db_msg = (
                                    db.query(DBChatMessage)
                                    .filter(DBChatMessage.session_id == session_id, DBChatMessage.role == 'assistant')
                                    .order_by(DBChatMessage.timestamp.desc())
                                    .first()
                                )
                                if db_msg:
                                    db_msg.content = full_response
                                    db.commit()
                            except Exception as e:
                                logger.warning("Failed to update rewritten message in DB: %s", e)
                                db.rollback()
                            finally:
                                db.close()
                            session_manager.save_sessions()
                        yield chunk
            except Exception as e:
                logger.error("Rewrite stream error: %s", e)
                yield f'event: error\ndata: {json.dumps({"error": str(e), "status": 500})}\n\n'

        return StreamingResponse(stream_rewrite(), media_type="text/event-stream")

    return router
