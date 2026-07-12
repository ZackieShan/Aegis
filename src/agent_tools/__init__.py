"""
agent_tools.py — Facade module.

Re-exports tool parsing, schemas, execution, and implementations
for backward compatibility. All importers continue to work unchanged.

Sub-modules:
  - tool_parsing.py: regex patterns, parse/strip functions
  - tool_schemas.py: FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
  - tool_execution.py: execute_tool_block, format_tool_result, MCP helpers
  - tool_implementations.py: all do_* tool functions
"""

import logging
from collections import namedtuple

from src.tool_security import BUILTIN_EMAIL_TOOLS
from src.tool_utils import _truncate, get_mcp_manager, set_mcp_manager

logger = logging.getLogger(__name__)

from .subprocess_tools import BashTool, PythonTool
from .web_tools import WebSearchTool, WebFetchTool
from .filesystem_tools import ReadFileTool, WriteFileTool, EditFileTool, LsTool, GlobTool, GrepTool, GetWorkspaceTool
from .document_tools import CreateDocumentTool, UpdateDocumentTool, EditDocumentTool, SuggestDocumentTool, ManageDocumentTool
from .interaction_tools import AskUserTool, UpdatePlanTool
from .model_interaction_tools import ChatWithModelTool, AskTeacherTool, ListModelsTool
from .bg_job_tools import ManageBgJobsTool
from .session_tools import CreateSessionTool, ListSessionsTool, SendToSessionTool, ManageSessionTool
from .admin_tools import (
    ADMIN_TOOL_HANDLERS,
    do_manage_endpoints, do_manage_mcp, do_manage_webhooks,
    do_manage_tokens, do_manage_settings,
)

TOOL_HANDLERS = {
    "bash": BashTool().execute,
    "python": PythonTool().execute,
    "web_search": WebSearchTool().execute,
    "web_fetch": WebFetchTool().execute,
    "read_file": ReadFileTool().execute,
    "write_file": WriteFileTool().execute,
    "edit_file": EditFileTool().execute,
    "ls": LsTool().execute,
    "glob": GlobTool().execute,
    "grep": GrepTool().execute,
    "create_document": CreateDocumentTool().execute,
    "update_document": UpdateDocumentTool().execute,
    "edit_document": EditDocumentTool().execute,
    "suggest_document": SuggestDocumentTool().execute,
    "manage_documents": ManageDocumentTool().execute,
    "get_workspace": GetWorkspaceTool().execute,
    "ask_user": AskUserTool().execute,
    "update_plan": UpdatePlanTool().execute,
    "chat_with_model": ChatWithModelTool().execute,
    "ask_teacher": AskTeacherTool().execute,
    "list_models": ListModelsTool().execute,
    "manage_bg_jobs": ManageBgJobsTool().execute,
    "create_session": CreateSessionTool().execute,
    "list_sessions": ListSessionsTool().execute,
    "send_to_session": SendToSessionTool().execute,
    "manage_session": ManageSessionTool().execute,
}
# Config/integration admin tools (manage_endpoints/mcp/webhooks/tokens/settings).
TOOL_HANDLERS.update(ADMIN_TOOL_HANDLERS)

# ---------------------------------------------------------------------------
# Constants (re-exported for backward compatibility — single source of truth
# is src.constants; always prefer importing from there for new code)
# ---------------------------------------------------------------------------
MAX_AGENT_ROUNDS = 50
SHELL_TIMEOUT = 60
PYTHON_TIMEOUT = 30

# Tool types that trigger execution
TOOL_TAGS = {"bash", "python", "web_search", "web_fetch", "read_file", "write_file", "edit_file",
             "grep", "glob", "ls", "get_workspace", "manage_bg_jobs",
             "create_document", "update_document", "edit_document",
             "search_chats",
             "chat_with_model", "create_session", "list_sessions",
             "send_to_session",
             "pipeline",
             "manage_session", "manage_memory", "list_models",
             "ui_control", "generate_image", "ask_user", "update_plan",
             "manage_tasks", "api_call", "ask_teacher", "manage_skills",
             "suggest_document",
             "manage_endpoints", "manage_mcp", "manage_webhooks",
             "manage_tokens", "manage_documents", "manage_settings",
             "manage_notes", "manage_calendar",
             "resolve_contact", "manage_contact",
             # Email tool names come from BUILTIN_EMAIL_TOOLS (unioned below)
             # so the fence regex, dispatch, and non-admin blocklist all cover
             # the same set.
             # Cookbook tools (LLM serving + downloads). Without these
             # entries, native function calls to e.g. list_served_models
             # are rejected as "Unknown function call" before reaching
             # the dispatcher — silent failure for the whole cookbook
             # surface.
             "download_model", "serve_model",
             "list_served_models", "stop_served_model",
             "list_downloads", "cancel_download",
             "search_hf_models", "list_cached_models",
             "list_serve_presets", "serve_preset", "adopt_served_model",
             "list_cookbook_servers",
             # Other tools the agent reaches for that were also missing.
             "edit_image", "trigger_research", "manage_research",
             # Generic loopback to any UI-button endpoint (cookbook,
             # gallery, email folders, etc.) — agent uses this when
             # there's no named tool wrapper for the action.
             "app_api"} | BUILTIN_EMAIL_TOOLS

ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])

# ---------------------------------------------------------------------------
# Re-exports from sub-modules — LAZY (PEP 562).
#
# These used to be eager `from src.tool_parsing import ...` imports, which
# closed an import cycle: tool_parsing/tool_schemas/tool_execution each do
# `from src.agent_tools import ToolBlock, TOOL_TAGS` at module level, so any
# cold entry into the cluster that did NOT start here (e.g.
# `import src.tool_schemas` from a test or script) died with "cannot import
# name ... from partially initialized module". Resolving the re-exports on
# first attribute access instead means every entry order works: by the time
# anyone *uses* a re-exported name, this module's own definitions (ToolBlock,
# TOOL_TAGS, TOOL_HANDLERS) are complete. `from src.agent_tools import X`
# still works — the import statement calls module __getattr__.
# ---------------------------------------------------------------------------
_LAZY_REEXPORTS = {
    # Parsing
    "parse_tool_blocks": "src.tool_parsing",
    "strip_tool_blocks": "src.tool_parsing",
    "_TOOL_NAME_MAP": "src.tool_parsing",
    "_TOOL_BLOCK_RE": "src.tool_parsing",
    "_TOOL_CALL_RE": "src.tool_parsing",
    "_XML_TOOL_CALL_RE": "src.tool_parsing",
    "_XML_INVOKE_RE": "src.tool_parsing",
    "_XML_PARAM_RE": "src.tool_parsing",
    # Schemas
    "FUNCTION_TOOL_SCHEMAS": "src.tool_schemas",
    "function_call_to_tool_block": "src.tool_schemas",
    # Execution
    "execute_tool_block": "src.tool_execution",
    "format_tool_result": "src.tool_execution",
    # Document functions
    "set_active_document": "src.agent_tools.document_tools",
    "set_active_model": "src.agent_tools.document_tools",
    # Implementations
    "do_search_chats": "src.tool_implementations",
    "do_manage_skills": "src.tool_implementations",
    "do_manage_tasks": "src.tool_implementations",
    "do_api_call": "src.tool_implementations",
}


def __getattr__(name):
    target = _LAZY_REEXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'src.agent_tools' has no attribute {name!r}")
    import importlib
    value = getattr(importlib.import_module(target), name)
    globals()[name] = value  # cache so subsequent access skips __getattr__
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_REEXPORTS))
