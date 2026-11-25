"""
RAG-based tool selection for agent mode.

Instead of injecting all tool descriptions into the system prompt,
embed them in a ChromaDB collection and retrieve only the top-K
relevant ones per user message.
"""

import logging
import hashlib
import re
import time
from typing import Dict, List, Optional, Set

from src.embedding_lanes import (
    LANE_CUSTOM,
    LANE_FASTEMBED,
    build_embedding_lanes,
    dedupe_results,
    migrate_legacy_collection,
)

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore

logger = logging.getLogger(__name__)

# Tools that are ALWAYS included regardless of retrieval results.
# Keep this deliberately tiny. Domain tools (web, documents, email,
# cookbook/model serving, files, settings, etc.) are injected by retrieval or
# keyword intent so a trivial agent prompt like "test" does not carry every
# domain's schemas and rules.
ALWAYS_AVAILABLE = frozenset({
    # Memory is ambient — "remember this" can follow any message regardless
    # of topic. Without this, RAG drops it and the agent falls back to
    # app_api /api/memory/add which fails with 422 on first attempt.
    "manage_memory",
    # Ask the user a multiple-choice question for a decision/clarification.
    # Always reachable so the agent can pause and ask at any point.
    "ask_user",
    # Write back to the active plan (tick steps done / revise) during execution.
    "update_plan",
})

# Tools that the Personal Assistant always has access to during scheduled
# check-ins and proactive tasks, in addition to RAG-selected tools.
ASSISTANT_ALWAYS_AVAILABLE = frozenset({
    "list_email_accounts", "list_emails", "read_email", "send_email", "reply_to_email",
    "bulk_email", "archive_email", "delete_email", "mark_email_read",
    "manage_calendar", "manage_notes", "manage_tasks",
    "manage_memory", "web_search", "read_file",
    "create_document", "update_document",
    "resolve_contact", "search_chats",
    "api_call",  # For Miniflux/Gitea/Linkding/etc. integrations
    # Core UI control (toggles, open panels, switch model/mode, themes).
    # Always available so vague follow-ups ("now make it playful", "make it
    # darker") that don't repeat a theme/UI keyword still keep the tool in
    # reach — without it the model narrates instead of acting.
    "ui_control",
})

COLLECTION_NAME = "aegis_tool_index"

# ── Tool description registry ──
# Each tool gets a searchable description that helps retrieval.
# These are richer than the system prompt one-liners — they're for embedding.
BUILTIN_TOOL_DESCRIPTIONS: Dict[str, str] = {
    "bash": "Run shell commands on the server. Install packages, git operations, builds, system info, process management. Prefer a dedicated tool whenever one fits the job (file read/write/edit, search, listing); use bash only for what no dedicated tool covers. Do not use for web lookup/search; use web_search or web_fetch when web tools are available.",
    "python": "Execute Python code for computation, data processing, math, scripting, and parsing. Not for writing code for the user. Prefer a dedicated tool for reading, writing, or searching files; use python only for what no dedicated tool covers. Do not use for web lookup/search; use web_search or web_fetch when web tools are available.",
    "web_search": "Quick single web lookup for a fact, current event, latest/current information, or doc mid-task. Use this instead of bash/curl/python/requests for web searches. NOT for 'research X' / 'do research on X' requests — those are deep-research jobs (use trigger_research). web_search = one query; trigger_research = a full researched report in the sidebar.",
    "web_fetch": "Fetch and read the text content of a specific URL/website the user names (e.g. 'check example.com', 'open this link'). Use when you have a concrete URL; for open-ended lookups use web_search instead.",
    "library_search": "Search the OFFLINE reference library on this server (Kiwix): a full Wikipedia encyclopedia snapshot, WikiMed and other medical references, Army field manuals and survival / first aid / SERE / land navigation / marksmanship / field sanitation manuals (PDF pages), water purification, knots, food preservation and canning, hunting, trapping, edible and poisonous plants, mushrooms, insects, repair guides (iFixit), gardening, cooking, electronics and other Stack Exchange archives. Works with NO internet. Use for established knowledge, how-to and identification questions, and whenever web_search fails or the internet is down. Not for news.",
    "translate": "Translate text between languages on this server, offline: English to Spanish, French, German, Chinese, Japanese, Arabic and 150 more, or any of them back to English. Use for: translate this, how do you say something in another language, what does this foreign phrase or sign mean, put this message into another language.",
    "convert_units": "Convert a measurement into other units exactly, offline: feet and inches to centimetres, miles to kilometres, pounds to kilograms, Fahrenheit to Celsius, gallons to litres, psi to bar, mpg to L/100km, foot-pounds to newton-metres, acres to hectares, BTU to kWh, knots to mph, mils to degrees, mSv to mrem. Use for: convert, how many X in a Y, what is this in metric or imperial.",
    "map_position": "Map coordinates and navigation, offline: convert between latitude/longitude, UTM and MGRS grid references; look up a town's position, elevation and time zone; straight-line distance and compass bearing (azimuth) between two places; where a bearing and distance lead. Use for: grid reference, MGRS, UTM, lat long, coordinates of, how far is it from A to B, bearing to, back azimuth.",
    "sun_moon": "Sunrise, sunset, dawn, dusk, twilight, first light, last light, hours of daylight, moonrise, moonset and moon phase for any place and date, computed offline; next full moon and new moon; where the sun or moon is in the sky. Use for: when does the sun rise or set, when does it get dark, how long until dark, how much daylight, is there a moon tonight, moon phase, full moon.",
    "date_math": "Calendar arithmetic, exact: days, weeks, months and weekdays between two dates; the date some days, weeks, months, years or business days from a date; day of the week, day of the year and week number of a date. Use for: how many days until or since, what day of the week, 90 days from today, how old, how long ago.",
    "identify_photo": "Identify what is in a photo the user sent, with the species identifier and the offline library: a plant, mushroom, berry, insect, spider, tick, snake, bird, animal, track or scat — its likely name, how sure, the dangerous lookalikes, whether it is edible, poisonous or venomous. Also reads a label, sign or page in a photo and describes anything else. Use for: what is this, what kind of plant or bug or snake is this, is this safe to eat, is this poisonous, what does this label say, identify this photo.",
    "news_search": "The news on a subject from the press, each article with its source and the date it was published. Use for: what is the latest on, any news about, what happened with, today's headlines, breaking news, this week's news about a person, a company, a team, an election, a trial, a war, a storm or a place; what is going on with; did anything happen.",
    "find_places": "Find real places near home or a named town with address, phone number, opening hours, whether it is open now, rating and website: the nearest pharmacy, a restaurant, pizza, coffee, a hardware store, a grocery store, a vet, a dentist, a mechanic, a petrol or gas station, a locksmith, an urgent care. Use for: where is the nearest, is there a … near me, somewhere to eat, what time does it close, is it open now, phone number of, address of.",
    "medicine_info": "Look a medicine up in its official US FDA label: what a drug is for, dose and dosing schedule, side effects, warnings, who should not take it, drug interactions, pregnancy and breastfeeding, overdose, storage — prescription or over the counter, brand name or ingredient. Use for any question about a medicine, medication, drug, pill, tablet, injection, prescription: can I take X with Y, how much X, is X safe, what are the side effects of X, what is X used for, missed a dose.",
    "paper_search": "Find published scientific studies and papers on a question in any field: title, authors, journal, year, citations, abstract, link to the free full text. Use for: what does the research say, is there evidence, studies on, clinical trials of, a meta-analysis or systematic review of, peer-reviewed papers about, the scientific literature on, who published, cite sources.",
    "ask_wolfram": "Ask Wolfram|Alpha to compute an exact answer: solve an equation, integrate, differentiate, factor, statistics, probability, a loan or mortgage payment, compound interest, chemistry and physics calculations, molar mass, nutrition of a food, distance between cities, population, properties of an element.",
    "weather": "The weather forecast for home or any town, from a live weather service: temperature now, today's high and low, chance of rain or snow, wind, the week ahead, weather alerts and air quality. Use for: what is the weather, will it rain, is it going to snow, how cold or hot will it be, do I need a jacket or an umbrella, the forecast for Saturday or the weekend, is there a storm or a weather warning, air quality today.",
    "library_browse": "See what the OFFLINE library on this server holds, shelf by shelf, without the internet: list the categories and shelves (Survival, Medicine, Cooking, History by era, Literature by era, Philosophy and religion, Military …), list the books, cookbooks, manuals and documents filed on a shelf, and find a book or document by its title or author. Use for: what do you have on a subject, do we have a certain book, anything by an author, show me the cookbooks, what is in the history section, which shelf is something on.",
    "read_file": "Read a file from disk and return its contents. View source code, config files, logs. Supports an optional line range (offset/limit) for large files.",
    "grep": "Search file CONTENTS for a regex across a directory tree (ripgrep-backed, honours .gitignore). Returns file:line:match. Use to find where code/symbols/strings live — prefer over bash grep.",
    "glob": "Find FILES by glob pattern (e.g. '**/*.py'), newest first. Use to locate files by name/extension — prefer over bash find/ls.",
    "ls": "List a directory's entries (folders then files with sizes). Use to see what's in a folder — prefer over bash ls.",
    "get_workspace": "Return the absolute path of the active workspace folder the user is working in. File tools are confined to it; the shell starts there but is not sandboxed. Call this first when the user refers to 'the project'/'the code'/'this folder' without giving a path, instead of asking them.",
    "write_file": "Write/create or fully rewrite a file ON DISK (source code, configs, project files). Use for new files or full rewrites — NOT create_document (editor panel) and NOT a bash heredoc.",
    "edit_file": "Edit an existing file ON DISK by exact string replacement (fix a bug, change a function). Shows a diff. The tool for changing files on disk — NOT edit_document (editor panel) and NOT bash sed/heredoc.",
    "create_document": "Create a new document in the editor panel. For code, articles, text content longer than 15 lines, unless an already-open document/email draft is the obvious target. If an email compose draft is open, edit that draft instead of creating another document.",
    "edit_document": "Preferred tool for editing an existing document — targeted find-and-replace. Use for any small change: add a function, fix a bug, tweak a section, rename things.",
    "update_document": "Replace the entire active document content. ONLY for full rewrites (>50% changed). Do not use for small edits — use edit_document instead.",
    "suggest_document": "Suggest changes to the active document with explanations. For code review, proofreading, feedback requests.",
    "generate_image": "Generate an AI image from a text prompt. Specify model, size, and quality. Art, illustrations, photos.",
    "chat_with_model": "Send a message to a different AI model. Compare responses, get specialized help, delegate tasks.",
    "ask_teacher": "Ask a more capable model for help with a difficult problem. Escalate complex tasks.",
    "pipeline": "Run a multi-step AI pipeline with multiple models. Chain tasks together in sequence.",
    "list_models": "List all available AI models and their endpoints.",
    "manage_session": "Chat management: rename, archive, delete, or fork chats (the UI calls these 'chats'; internally 'sessions'). Use for 'rename my chats', 'rename this chat', 'archive/delete a chat'.",
    "manage_memory": "Memory management: list, add, edit, delete, or search persistent memories. For facts about the USER (their name, preferences, where they live). NOT for info about ANOTHER person — addresses, phones, emails belonging to a contact go in manage_contact, not memory.",
    "manage_skills": "Skill management: add, update, publish, or search reusable skills/presets.",
    "manage_tasks": "Scheduled task management: list, create, edit, delete, pause, resume, or run cron tasks.",
    "manage_endpoints": "Endpoint management: list, add, delete, enable, or disable model API endpoints.",
    "manage_mcp": "MCP server management: list, add, delete, reconnect servers, or list available tools.",
    "manage_webhooks": "Webhook management: list, add, delete, enable, or disable webhooks.",
    "api_call": "Call a configured API integration by name (Home Assistant, Miniflux, Gitea, Linkding, Jellyfin, RSS reader, git forge, bookmark manager, smart home, or any other registered service). Make a GET/POST/PUT/PATCH/DELETE request to the integration's endpoint path, with an optional JSON body. Use whenever the user asks to query or control one of their connected integrations/services.",
    "manage_tokens": "API token management: list, create, or delete API access tokens.",
    "manage_documents": "List, read, delete, or tidy documents in the editor panel. action='list' returns clickable rows (most-recent first) so the user can open any doc by clicking. action='read' (aka view/open/get) with document_id returns the content; supports offset=<N> + limit=<N> to page through large docs (response includes next_offset when more remains, so you can keep calling with offset=next_offset). action='delete' with document_id removes a doc (only way to delete). Use this for ANY 'show/read/list/open my documents/docs/files/notes' request — never shell or curl.",
    "manage_research": "List, read/open, or delete saved DEEP RESEARCH results from the Library. action='list' returns clickable [query](#research-<id>) rows (most-recent first). action='read' (aka open/view/get) with id returns the report + sources. action='delete' with id removes it. Use this for ANY 'open/read/find/delete my research / that report / the research on X' request. NOTE: this is for EXISTING research; to START new research use trigger_research.",
    "manage_settings": "Change ANY real app setting (the ones the Settings panel writes) so the user never has to open it: TTS voice/provider/speed, STT, search engine + result count, default/teacher/task/utility/vision/image/research models, image quality, reminder channel (browser/email/ntfy), agent timeout/tool-call budget, and more. action=set with key (friendly aliases ok: voice, 'search engine', 'default model', 'teacher model', 'image quality', 'reminder channel'...) + value; get/list/reset too. Also toggles tools on/off (disable_tool/enable_tool/list_tools). Secrets/API keys are read-only. Use for any 'change my…/set my…/use X for…/turn on…' preference request.",
    "create_session": "Create a new chat with a name and model.",
    "list_sessions": "List all chats with their metadata (the UI calls these 'chats'). Use for 'list my chats', 'rename all my chats' (list first, then manage_session to rename each).",
    "send_to_session": "Send a message to another chat. Cross-chat communication.",
    "search_chats": "Search past session transcripts across chats.",
    "ask_user": "Ask the user a multiple-choice question to get a decision or clarification. Use this when the task is genuinely ambiguous and the answer changes what you do next — pick between approaches, confirm an assumption, choose among options — instead of guessing. Provide a clear `question` and 2-6 `options` (each with a short `label`, optional `description`). Omit `multi`/keep it false unless the question explicitly permits choosing multiple options. Calling this ENDS your turn: the user sees clickable buttons and their choice arrives as your next message. Don't use it for things you can decide from context or sensible defaults, or for irreversible-action confirmation if a dedicated flow exists.",
    "update_plan": "Write back to the ACTIVE PLAN while executing an approved plan: mark steps done or revise them. After finishing a step call this with the full checklist and that step marked done; when the user asks to change the plan call it with the revised checklist. Always pass the COMPLETE markdown checklist (`- [ ]` / `- [x]`), not a diff. The user's docked plan window updates live. No effect when there is no active plan.",
    "ui_control": "Control the UI and toggle tools on/off. Use this to turn off / turn on / disable / enable individual tools and features: shell (bash), search (web), research, browser, documents, incognito. Open panels (documents library, gallery, email inbox, sessions, notes, memories/brain, skills, settings, model_shopper, automations, kitchen) via `open_panel <name>`. Use `open_email_reply <uid> <folder> reply <body text>` (or structured body) to open an email reply draft document without sending. USE THIS whenever the user says to write/draft a reply or tells you what to say — opening an empty draft or sending immediately is wrong. Body can continue on subsequent lines for multi-line replies. Also switches between chat/agent modes, changes the current model, and applies/creates themes.",
    "list_email_accounts": "List configured email accounts and default status. Use before reading or sending mail when the user mentions Gmail, work mail, custom domain mail, another mailbox, or asks to compare/check multiple inboxes.",
    "list_emails": "List emails for a folder/account, newest first, including read messages by default. Shows subject, sender, date, UID, account, and AI summary. Check inbox, find emails needing replies. Supports account from list_email_accounts for Gmail/work/custom mailboxes. For last/latest/newest email, use max_results=1 and unread_only=false.",
    "read_email": "Read the full content of a specific email by UID or Message-ID. View email body, check details. Supports account from list_email_accounts when the UID belongs to a non-default mailbox.",
    "send_email": "Send a new email via SMTP. Provide recipient, subject, body, and optional account from list_email_accounts. For replying to a thread use reply_to_email instead.",
    "reply_to_email": "SEND a reply email immediately by UID. Do not use for write/draft/open/start reply requests; use ui_control open_email_reply with body so the user can review. Only use when the user explicitly says to send now. For send requests, use the exact UID and account from latest read_email/list_emails output; never invent UID 1. Threads automatically with In-Reply-To/References, prefixes Re:, marks original as Answered.",
    "archive_email": "Move an email out of the inbox into the Archive folder. Use after handling messages you want to keep but get out of the way.",
    "delete_email": "Delete an email — moves to Trash by default, or expunges permanently with permanent=true.",
    "mark_email_read": "Mark an email as read or unread by toggling the \\Seen flag.",
    "bulk_email": "Perform one action on many emails at once. Use for delete all those, archive these, mark all read, move spam to junk. Takes explicit UIDs from list_emails or all_unread=true. Always pass account for Gmail/work/custom mailbox results.",
    "resolve_contact": "Look up a contact's email address by name. Searches CardDAV address book and sent email history. Use when the user says 'message [name]', 'email [name]', or 'send to [name]' without an email address.",
    "manage_contact": "Save / update / delete / list address-book contacts (CardDAV). Use for info about ANOTHER person — name, email, phone, postal address. Args: action=list|add|update|delete, name, email, phones, address, uid (from list). For 'save this for <person>' / address pastes / phone numbers next to a name, this is the right tool — NOT manage_memory. Do NOT use for facts about the USER ('my name is X'); those are manage_memory.",
    "manage_notes": "Create and manage notes and checklists (Google Keep-style). ALWAYS use this for note/todo/checklist/reminder creation — NEVER hit /api/notes via app_api. Accepts natural-language `due_date` like 'tomorrow at 9am' or '11pm today' (parsed in the USER'S timezone). The due_date IS the reminder — it fires a notification at that time, so do NOT also create a calendar event for the same reminder. Set colors, labels, pin, archive. Do NOT use manage_memory for note content.",
    "manage_calendar": "Calendar event management: list, create, update, delete. Each event can carry a tag/category (event_type — work/personal/health/travel/meal/social/admin/other) and importance (low/normal/high/critical). Resolve today/tomorrow using the Current date and time context, then use ISO datetimes in the user's local wall time; supports all-day events. Use rrule only for explicit recurrence; for update_event pass rrule='' to remove repeats. For event reminders/alarms, pass reminder_minutes; this creates the Notes reminder, so do not also call manage_notes for the same reminder.",
    "download_model": "Download a HuggingFace model to a local or remote server. Specify repo_id (e.g. 'Qwen/Qwen3-8B'), optional server host, and optional include filter for specific files.",
    "serve_model": "Start serving a model with vLLM, SGLang, llama.cpp, Ollama, or Diffusers. cmd MUST start with the binary directly — e.g. `vllm serve /mnt/HADES/models/Qwen3.5-397B-A17B-AWQ --port 8003 --tensor-parallel-size 8 …`. NEVER prefix with `cd …`, `source …`, or chain with `&&`/`||` — those get rejected by the validator. The venv activation (env_prefix) and CUDA env are added automatically from the target host's saved settings. For image/inpainting/diffusion use python3 scripts/diffusion_server.py --model <repo> --port 8100. After launch, call list_served_models for readiness/errors and retry suggestions. If serve_model fails with 'Invalid characters in cmd', simplify to the bare binary + args.",
    "list_served_models": "List currently running model servers in the Model Shopper — shows status (loading, ready, idle, error), model name, port, throughput, and serve failure diagnosis/retry suggestions. Use when the user asks 'what's running', 'show the model shopper', 'which models are up', 'what's serving'.",
    "stop_served_model": "Stop a running model server in the Model Shopper by session ID or model name. Use when the user says 'kill the model server', 'stop the model', 'kill the serve', 'shut down vLLM', 'cancel the running model'.",
    "tail_serve_output": "Read the actual tmux stderr/traceback of a Model Shopper serve/download task. Use to debug WHY a task is `crashed`/`error` (compute_89 nvcc mismatch, OOM, missing kernels, wrong attention backend, etc.) so you can call serve_model with adjusted flags. Pass session_id from list_served_models; tail defaults to 300, bump if the error references 'see root cause above'.",
    "list_downloads": "List in-progress HuggingFace model downloads in the Model Shopper. Shows model name, phase, percent, session ID. Use for 'what's downloading', 'show my downloads', 'check download progress'.",
    "cancel_download": "Cancel an in-progress model download by tmux session ID. Use for 'cancel the download', 'stop downloading X', 'kill the download'. Call list_downloads first to get the session_id.",
    "search_hf_models": "Search HuggingFace for models matching a query (e.g. 'qwen 8B', 'flux', 'llama-3 instruct'). Returns ranked repo IDs with sizes and download counts. Use for 'find a model', 'search huggingface for X', 'what models are there for Y'.",
    "list_cached_models": "List models already cached on disk locally or on a remote host. Accepts friendly Model Shopper server names like ajax. Use for 'what models do I have', 'show cached models', 'is X downloaded', 'list my models'. Avoids re-downloading.",
    "list_serve_presets": "List saved Model Shopper serve presets (templates with model+host+port+cmd). Always call this BEFORE serve_model when the user asks to launch a known model — they probably have a preset for it from the UI.",
    "serve_preset": "Launch a saved Model Shopper serve preset by name. Reuses the exact tmux command + host the user already saved. Use for 'run stable diffusion 3.5', 'serve vllm-qwen', 'start the inpaint model' — preset-name matches the user's UI labels.",
    "adopt_served_model": "Register an existing tmux model server (one started manually or outside the Model Shopper) into Model Shopper tracking AND add it as a chat endpoint. Use when the user (or a previous turn) launched something via ssh+tmux and now wants it visible in the UI, stoppable via stop_served_model, and usable in the model picker.",
    "list_cookbook_servers": "List the Model Shopper's configured servers (remote GPU boxes + local) and which is the current default. Use this BEFORE download_model/serve_model when the user didn't name a host — to decide where to run, or to ask the user which server when ambiguous. Downloads/serves default to the Model Shopper's selected server, NOT localhost.",
    "app_api": "Generic loopback to allowed Aegis internal endpoints. Use this when the user wants something the UI can do but there's no named tool for it. Covers calendar, gallery, library/documents, memory, notes, tasks, settings, research, compare, Model Shopper GPUs/state (under /api/cookbook) — allowed UI buttons hit /api/* endpoints and you can hit them too. Sensitive auth/user/admin/shell paths and host-control Model Shopper mutation routes are blocked; do NOT use app_api for shell commands, package installs, engine rebuilds, or PID signalling. Use named command tooling for shell commands. action='endpoints' with filter=<keyword> lists available endpoints. action='call' takes method+path+body. Hits same routes the UI uses — auth flows free. NOTE: themes are NOT an API endpoint — use the ui_control tool (create_theme / set_theme), not app_api. SESSIONS/CHATS: do NOT use app_api for these — GET /api/sessions returns EMPTY for tool calls (it's owner-filtered and tool calls authenticate as a different identity). EMAIL ACCOUNTS: do NOT use /api/email/accounts via app_api; use list_email_accounts, list_emails, and read_email instead. To list/rename/archive/delete/fork chats use the list_sessions and manage_session tools instead.",
    "edit_image": "Edit an image in the gallery: upscale (increase resolution), remove background (rembg), inpaint (fill selected area), or harmonize (blend edits). Specify image ID and action.",
    "trigger_research": "Start a deep research job on any topic — appears in the Deep Research sidebar, streams progress, produces a detailed report. Use for 'research X', 'look into Y', 'do deep research on Z', 'investigate'. NOT a scheduled task — it runs now and surfaces in the sidebar.",
    "manage_lists": "Lists and plans for the user: shopping list, grocery list, packing list, to-do list, a plan for a party or trip. Add items to a list, tick items off, remove items, read a list back, start a new list.",
    "manage_reminders": "Reminders for the user: remind me to do something at a time, tomorrow, tonight, every day or every week; show upcoming reminders; mark one done, snooze it, move it or cancel it.",
    "manage_trackers": "Trackers for habits and numbers the user follows over time: log water, steps, weight, mood, sleep, exercise, medication taken; start a new tracker; show the streak and the last days.",
    "manage_boards": "Boards for collecting ideas like pinning: save links, pictures and notes to a board for a project such as decorating, recipes, gifts, outfits, a party or a trip; show a board.",
    "manage_kitchen": "Food recipes and cooking (the Kitchen): save a recipe to the cookbook, follow along with a recipe step by step while cooking or baking, convert an old or historical recipe (a teacup, butter the size of an egg, a quick oven) to modern measures, cups to grams, Fahrenheit to Celsius, show a saved recipe.",
    "manage_library": "Add documents to the offline reference library (the survival library) so they can be searched later: PDF manuals, guides and books from a link on the internet or from PDFs the user uploaded in the chat; list what has been added; remove a document.",
    "manage_projects": "Code projects on this server: create a new app or project, clone a git or GitHub repository, open a project so this chat works in it (file tools and shell), list projects, show git status. Use when the user wants to build, start, continue, pull or open an app, project, repo or codebase.",
    "manage_apps": "Host and deploy apps on this server: turn a project's start command into a service that keeps running, restarts after crashes and reboots, and gets an HTTPS address for the phone and other PCs over Tailscale. List running apps, status, logs, restart, stop, remove. Use for deploy, host, serve, run permanently, make it available, is my app up, app logs.",
    "manage_hosts": "Remote control of the user's other computers over SSH: run PowerShell on the Windows PC, bash on Linux or macOS machines, copy files between machines, list machines on the network or tailnet. Use for anything on 'my Windows machine', 'my PC', 'the other computer', remote commands, installing or checking something on another machine, automating Windows.",
    "delegate_agent": "Hand a coding task to another agent installed on this server: Claude Code (claude), Kimi Code (kimi), Codex, OpenCode or Aider. Runs in the background in a project folder and reports back. Use when the user names one of those agents ('ask kimi to…', 'have claude code do…', 'send this to codex') or wants a second coding agent on a task.",
    "manage_bg_jobs": "Inspect and control detached background `bash` jobs (the ones started with a `#!bg` marker). action='list' shows this chat's jobs (id/status/age/command); action='output' returns a job's captured output so far (check on a long-running job, or re-read a finished one); action='kill' stops a runaway job by id. Use for 'is the background job done', 'check on that job', 'show the build output', 'kill the background job', 'stop the bg task'. output/kill need a job_id from list.",
}


class ToolIndex:
    """ChromaDB-backed tool index for RAG-based tool selection."""

    def __init__(self):
        self._lanes = build_embedding_lanes(COLLECTION_NAME)
        if not self._lanes:
            raise RuntimeError("No embedding lanes available")
        self._embedder = self._lanes[0].client
        self._collection = next(
            (lane.collection for lane in self._lanes if lane.name == LANE_FASTEMBED),
            self._lanes[0].collection,
        )
        migrate_legacy_collection(COLLECTION_NAME, self._lanes)
        self._fingerprint = ""
        self._mcp_generation = -1
        self._healthy = True
        logger.info("ToolIndex initialized (lanes=%s)", [lane.name for lane in self._lanes])

    @property
    def healthy(self):
        return self._healthy

    def _embed(self, texts: List[str]) -> List[List[float]]:
        if not self._lanes:
            return []
        vecs = self._lanes[0].encode(texts)
        if np is not None:
            return np.array(vecs, dtype=np.float32).tolist()
        return [list(v) for v in vecs]

    def index_builtin_tools(self):
        """Index all built-in tool descriptions."""
        docs = []
        ids = []
        metadatas = []
        for name, desc in BUILTIN_TOOL_DESCRIPTIONS.items():
            doc_text = f"Tool: {name}\n{desc}"
            docs.append(doc_text)
            ids.append(f"builtin_{name}")
            metadatas.append({"tool_name": name, "tool_type": "builtin"})

        if not docs:
            return

        # Drop any stale builtin_* entries that aren't in the current
        # registry (e.g. removed tools like the old vault_* set).
        # Without this, upsert leaves them in place and RAG keeps
        # surfacing tools that no longer exist.
        indexed = False
        for lane in self._lanes:
            try:
                existing = lane.collection.get(where={"tool_type": "builtin"})
                existing_ids = (existing or {}).get("ids") or []
                stale = [i for i in existing_ids if i not in set(ids)]
                if stale:
                    lane.collection.delete(ids=stale)
                    logger.info(f"Pruned {len(stale)} stale builtin tool entries from {lane.name} index")
            except Exception as e:
                logger.debug(f"Stale-pruning skipped for {lane.name}: {e}")

            try:
                lane.collection.upsert(
                    ids=ids,
                    documents=docs,
                    embeddings=lane.encode(docs),
                    metadatas=metadatas,
                )
                indexed = True
            except Exception as e:
                logger.warning("Builtin tool indexing failed in %s lane: %s", lane.name, e)
        if not indexed:
            self._healthy = False
            raise RuntimeError("Builtin tool indexing failed in all embedding lanes")
        self._fingerprint = hashlib.sha256(
            ",".join(sorted(BUILTIN_TOOL_DESCRIPTIONS.keys())).encode()
        ).hexdigest()
        logger.info(f"Indexed {len(docs)} built-in tools")

    def index_mcp_tools(self, mcp_mgr, disabled_map: Optional[Dict] = None):
        """Index MCP tool descriptions. Call after MCP servers connect/disconnect."""
        if not mcp_mgr:
            return

        # Get current MCP generation to avoid redundant reindexing
        gen = getattr(mcp_mgr, '_generation', 0)
        if gen == self._mcp_generation:
            return

        # Remove old MCP entries
        for lane in self._lanes:
            try:
                existing = lane.collection.get(where={"tool_type": "mcp"})
                if existing and existing["ids"]:
                    lane.collection.delete(ids=existing["ids"])
            except Exception:
                pass

        # Get current MCP tools
        try:
            all_tools = mcp_mgr.get_tool_descriptions_for_prompt(disabled_map or {})
        except Exception:
            all_tools = ""

        if not all_tools:
            self._mcp_generation = gen
            return

        # Parse MCP tool descriptions from the prompt text
        docs = []
        ids = []
        metadatas = []
        current_server = ""
        for line in all_tools.strip().split("\n"):
            line = line.strip()
            # Track which server section we're in (for context in descriptions)
            if line.startswith("**") and line.endswith(":**"):
                current_server = line.strip("*: ")
            elif line.startswith("- ") and ":" in line:
                # Format: "- tool_name: description"
                name_desc = line[2:].split(":", 1)
                if len(name_desc) == 2:
                    name = name_desc[0].strip()
                    desc = name_desc[1].strip()
                    # Include server identity in the indexed text so RAG can
                    # distinguish "list_emails for server-a" from "list_emails for server-b"
                    server_ctx = f" (server: {current_server})" if current_server else ""
                    doc_text = f"Tool: {name}{server_ctx}\n{desc}"
                    docs.append(doc_text)
                    ids.append(f"mcp_{name}")
                    metadatas.append({"tool_name": name, "tool_type": "mcp"})

        if not docs:
            self._mcp_generation = gen
            return

        indexed = False
        for lane in self._lanes:
            try:
                lane.collection.upsert(
                    ids=ids,
                    documents=docs,
                    embeddings=lane.encode(docs),
                    metadatas=metadatas,
                )
                indexed = True
            except Exception as e:
                logger.warning("MCP tool indexing failed in %s lane: %s", lane.name, e)
        if not indexed:
            logger.warning("MCP tool indexing failed in all embedding lanes")
            return
        self._mcp_generation = gen
        logger.info(f"Indexed {len(docs)} MCP tools")

    def retrieve(self, query: str, k: int = 8, min_score: Optional[float] = None) -> List[str]:
        """Retrieve the top-K most relevant tool names for a query.

        The nearest K are returned whatever their score, so a message that is
        about no tool at all ("thanks", a name, "ok") still brings back K of
        them at scores near noise (measured: 0.08-0.17, where a tool the message
        is really about scores 0.2-0.6). ``min_score`` leaves out what scores
        below it; the agent loop passes it for a chat that already has its
        tools, so that an unrelated message does not grow the list.
        """
        rows = []
        lane_priority = {LANE_CUSTOM: 0, LANE_FASTEMBED: 1}
        for lane in self._lanes:
            try:
                count = lane.count()
                if count == 0:
                    continue
                results = lane.collection.query(
                    query_embeddings=lane.encode([query]),
                    n_results=min(k, count),
                    include=["metadatas", "distances"],
                )
                if not results or not results.get("metadatas"):
                    continue
                distances = results.get("distances") or []
                for list_idx, meta_list in enumerate(results["metadatas"]):
                    distance_list = distances[list_idx] if list_idx < len(distances) else []
                    for idx, meta in enumerate(meta_list):
                        name = meta.get("tool_name", "")
                        if name:
                            distance = distance_list[idx] if idx < len(distance_list) else 1.0
                            rows.append({
                                "tool_name": name,
                                "score": round(1.0 - distance, 4),
                                "embedding_lane": lane.name,
                            })
            except Exception as e:
                logger.warning("Tool retrieval failed in %s lane: %s", lane.name, e)
        if min_score is not None:
            rows = [row for row in rows if row["score"] >= min_score]
        rows.sort(key=lambda row: (-row["score"], lane_priority.get(row["embedding_lane"], 99)))
        return [row["tool_name"] for row in dedupe_results(rows, id_key="tool_name", limit=k)]

    # Structural recurring-schedule intent. Typo-resilient (matches "every dya"
    # via "every <word>"), and catches bare clock times ("at 7:30 am", "7am").
    # Used in addition to the literal keyword hints below.
    _SCHEDULE_RE = re.compile(
        r"\bevery\s+\w+"                                       # every day / dya / morning / monday / 2 hours
        r"|\b(?:daily|nightly|hourly|weekly|monthly)\b"
        r"|\beach\s+(?:day|morning|night|week|hour|evening)\b"
        r"|\bat\s+\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b",  # at 7:30 am / at 7am
        re.I,
    )
    _WEB_RE = re.compile(
        r"https?://|www\.|\b(?:visit|open|fetch|check|read)\s+(?:this\s+)?(?:url|link|site|website|page)\b",
        re.I,
    )
    # A named list: "add oat milk to my test list", "what's on the packing
    # list", "cross it off our camping list". The literal hints below need
    # "my list" side by side and miss any list that has a name.
    _LIST_RE = re.compile(
        r"\b(?:to|on|off|from|in)\s+(?:my|our|the)\s+(?:[\w'-]+\s+){0,3}lists?\b|\b(?:my|our)\s+(?:[\w'-]+\s+){1,2}lists?\b",
        re.I,
    )

    # Keyword hints: if the query mentions these words, force-include the tools.
    _KEYWORD_HINTS = {
        # NOTE: "tell" was removed from this set. It fired on any "tell me ..."
        # request (e.g. "visit <url> and tell me the title"), force-including the
        # whole email toolset and crowding out the relevant tools — the model then
        # believed it had only email tools and refused web/other tasks (#1707).
        frozenset({"email", "emails", "mail", "mails", "gmail", "googlemail", "message", "messages", "send", "reply", "replies", "inbox", "unread"}):
            {"list_email_accounts", "list_emails", "read_email", "send_email", "reply_to_email", "bulk_email", "delete_email", "archive_email", "mark_email_read", "resolve_contact", "ui_control"},
        frozenset({"calendar", "event", "meeting", "schedule", "appointment"}):
            {"manage_calendar"},
        # Detached background `bash` jobs (#!bg): check on / read output / kill.
        frozenset({"background job", "background jobs", "bg job", "bg jobs",
                   "background task", "is the job done", "check the job",
                   "check on that job", "job output", "kill the job",
                   "kill the background", "stop the background", "running job"}):
            {"manage_bg_jobs"},
        frozenset({"note", "todo", "reminder", "remind", "checklist", "remember to"}):
            {"manage_notes"},
        # Chat/session management. "rename" alone maps to documents below, so a
        # request like "rename the last 12 sessions/chats" needs these session
        # keywords to surface the right tools (NOT app_api — /api/sessions is
        # owner-filtered and returns empty for tool calls).
        frozenset({"sessions", "my chats", "these chats", "those chats",
                   "chat history", "rename chat", "rename session",
                   "rename the chat", "rename my chat", "rename the session",
                   "archive chat", "archive session", "delete chat",
                   "delete session", "fork chat", "fork session",
                   "name the chats", "name my chats", "rename them"}):
            {"list_sessions", "manage_session"},
        frozenset({"recurring", "every day", "every hour", "every morning",
                   "every evening", "every night", "every week", "each morning",
                   "daily task", "background task", "scheduled task", "schedule a",
                   "automatically", "auto-summarize", "auto summarize",
                   "cron", "periodically", "on a schedule", "set up a task",
                   "create a task", "summarize my inbox every", "remind me every"}):
            {"manage_tasks"},
        frozenset({"contact", "address", "phone", "who is"}):
            {"resolve_contact", "manage_contact"},
        frozenset({"save contact", "add contact", "new contact", "update contact",
                   "edit contact", "delete contact", "remove contact",
                   "save this person", "add to contacts", "save to contacts",
                   # "add <name> to (my) contacts" — words between 'add' and
                   # 'contacts' break the literal phrase match above, so anchor
                   # on the tail.
                   "to my contacts", "to contacts", "to address book",
                   # "save this for <person>" / "save it for <person>" — the user
                   # is storing info on a known person without using the literal
                   # word 'contact'. Catches the address/phone-paste pattern.
                   "save this for", "save it for", "save for",
                   "save this one for", "save that for",
                   # Postal-address-like signals
                   "postal code", "zip code", "street address",
                   "mailing address", "their address"}):
            {"manage_contact"},
        # "Ask another model" intent → chat_with_model relays to a
        # different model and returns its answer. ask_teacher escalates
        # to the configured teacher. (second_opinion was removed.)
        frozenset({"ask gpt", "ask claude", "ask gemini", "ask deepseek",
                   "ask minimax", "ask qwen", "ask the", "ask another model",
                   "what does", "what would", "second opinion", "other model",
                   "different model", "compare answers", "compare models",
                   "delegate to", "have model"}):
            {"chat_with_model", "ask_teacher", "list_models"},
        # Deep research intent (incl. common typo "reserach")
        frozenset({"web search", "search the web", "search online", "look up",
                   "find info online", "find information online",
                   "find info", "find information", "online about",
                   "on the internet", "google", "latest", "current", "news",
                   "weather", "forecast", "stock price", "price of"}):
            {"web_search", "web_fetch"},
        # Home: lists, reminders, trackers, boards.
        # (Not the bare word "list": "list the files" is not about a shopping list.)
        frozenset({"my list", "my lists", "the list", "shopping", "grocery", "groceries", "packing list",
                   "to-do", "todo", "to do list", "checklist", "add to my", "put on my", "cross off",
                   "tick off", "on my list", "plan for", "plan my", "plan the", "plan a"}):
            {"manage_lists"},
        frozenset({"remind", "reminder", "reminders", "don't let me forget", "alert me", "ping me",
                   "nudge me", "wake me", "coming up", "upcoming", "what do i have today",
                   "what do i have tomorrow", "what do i have this week", "what's due", "due today",
                   "on my plate"}):
            {"manage_reminders"},
        frozenset({"track", "tracker", "trackers", "log my", "log that", "habit", "habits", "streak",
                   "glasses of water", "my steps", "my weight", "my mood", "took my", "i drank", "i walked"}):
            {"manage_trackers"},
        frozenset({"board", "boards", "pin this", "pin it", "pin these", "pinterest", "mood board",
                   "moodboard", "save this idea", "collect ideas", "inspiration", "ideas for"}):
            {"manage_boards"},
        frozenset({"recipe", "recipes", "receipt for", "cookbook", "cook book", "follow along", "cook this", "bake this",
                   "let's cook", "let's bake", "help me cook", "help me bake", "modern conversion", "modern measures",
                   "convert this recipe", "in grams", "in metric"}):
            {"manage_kitchen"},
        # Workbench: projects, hosted apps, other machines, other agents.
        frozenset({"project", "projects", "repo", "repository", "codebase", "clone", "git clone",
                   "new app", "build an app", "build me", "start a new", "scaffold", "open the app",
                   "work on the", "continue working on", "pull the repo", "github"}):
            {"manage_projects"},
        frozenset({"deploy", "redeploy", "host it", "host this", "hosted", "hosting", "serve it",
                   "keep it running", "make it available", "is my app", "app logs", "restart the app",
                   "my apps", "running apps", "what's running", "tailscale url", "app url"}):
            {"manage_apps", "manage_projects"},
        frozenset({"windows machine", "windows pc", "my pc", "my windows", "other computer",
                   "other machine", "other pc", "remote machine", "remotely", "over ssh", "ssh into",
                   "powershell", "my desktop", "gaming pc", "on my network", "my laptop",
                   "remote control", "another machine"}):
            {"manage_hosts"},
        frozenset({"claude code", "ask claude", "have claude", "kimi", "codex", "opencode", "aider",
                   "delegate", "hand off", "hand this to", "another agent", "other agent",
                   "second agent", "send this to"}):
            {"delegate_agent", "manage_projects"},
        # Growing the library: add PDFs from a link or a chat upload.
        frozenset({"add to the library", "add to your library", "add to my library", "add them to", "add it to the library",
                   "add this to the library", "to the survival library", "survival library", "into the library",
                   "library add", "put these in the library", "put this in the library", "save to the library",
                   "added to the library", "remove from the library"}):
            {"manage_library", "library_search"},
        # Another language.
        frozenset({"translate", "translation", "translator", "how do you say", "how do i say", "how to say",
                   "in spanish", "in french", "in german", "in italian", "in portuguese", "in chinese", "in japanese",
                   "in korean", "in russian", "in arabic", "in hindi", "into spanish", "into french", "into german",
                   "into english", "to spanish", "to french", "to english", "en español", "en français", "auf deutsch",
                   "what does this say", "what does it say", "what does this mean in english"}):
            {"translate"},
        # Exact answers worked out by the almanac rather than recalled.
        frozenset({"unit conversion", "convert units", "in metric", "to metric", "in imperial", "in celsius", "to celsius",
                   "in fahrenheit", "to fahrenheit", "in centigrade", "in kilograms", "in kg", "in pounds", "in lbs", "in grams",
                   "in ounces", "in kilometers", "in kilometres", "in km", "in miles", "in feet", "in meters", "in metres",
                   "in inches", "in centimeters", "in centimetres", "in cm", "in mm", "in liters", "in litres", "in gallons",
                   "in quarts", "in acres", "in hectares", "in psi", "in bar", "in kpa", "in knots", "in mph", "in km/h", "in kwh",
                   "in btu", "to kilograms", "to kg", "to pounds", "to lbs", "to grams", "to ounces", "to kilometers",
                   "to kilometres", "to km", "to miles", "to feet", "to meters", "to metres", "to inches", "to centimeters",
                   "to centimetres", "to cm", "to mm", "to liters", "to litres", "to gallons", "to acres", "to hectares", "to psi",
                   "to knots", "to mph", "to kwh", "to btu", "to ml", "to milliliters", "to millilitres",
                   "how many feet", "how many inches", "how many miles", "how many kilometers", "how many kilometres",
                   "how many meters", "how many metres", "how many yards", "how many ounces", "how many grams", "how many pounds",
                   "how many kilograms", "how many cups", "how many gallons", "how many liters", "how many litres", "how many quarts",
                   "how many pints", "how many tablespoons", "how many teaspoons", "how many milliliters", "how many millilitres",
                   "how many acres", "how many square feet", "mpg", "l/100km", "foot-pounds", "ft-lb", "newton meters",
                   "newton metres", "millisieverts", "millirem"}):
            {"convert_units"},
        frozenset({"mgrs", "utm", "grid reference", "grid coordinates", "grid square", "gps coordinates", "map coordinates",
                   "coordinates of", "coordinates for", "these coordinates", "latitude", "longitude", "lat long", "lat/long",
                   "lat lon", "compass bearing", "true bearing", "magnetic bearing", "bearing to", "bearing from", "back bearing",
                   "azimuth", "how far is", "how far from", "how far away", "distance from", "distance between", "distance to",
                   "as the crow flies", "straight line distance", "elevation of", "what time zone"}):
            {"map_position"},
        frozenset({"sunrise", "sunset", "sun rise", "sun set", "sunup", "sundown", "sun come up", "sun comes up", "sun go down",
                   "sun goes down", "get dark", "gets dark", "getting dark", "until dark", "before dark", "first light", "last light",
                   "civil twilight", "nautical twilight", "astronomical twilight", "hours of daylight", "how much daylight",
                   "daylight hours", "daylight left", "day length", "length of day", "solar noon", "moonrise", "moonset",
                   "moon rise", "moon set", "moon phase", "phase of the moon", "full moon", "new moon", "moon tonight",
                   "moon up", "position of the sun", "where is the sun", "where is the moon"}):
            {"sun_moon"},
        frozenset({"how many days", "how many weeks", "how many months", "days until", "days till", "days since", "days between",
                   "days from now", "days from today", "days ago", "weeks until", "weeks since", "weeks from now", "weeks from today",
                   "months from now", "months from today", "months since", "years since", "business days", "working days",
                   "what day of the week", "which day of the week", "what day is", "what day was", "what day will", "what date is",
                   "what date was", "what date will", "day of the year", "week number", "how long until", "how long since",
                   "how long ago", "how old is", "how old am", "how old was", "how old will"}):
            {"date_math"},
        # Look-ups through connected services (Settings → Integrations).
        frozenset({"news about", "news on", "any news", "in the news", "latest on", "latest news", "headlines",
                   "breaking news", "what happened with", "what happened to", "what happened in",
                   "this week's news", "today's news", "news today", "press coverage"}):
            {"news_search"},
        frozenset({"near me", "nearest", "nearby", "closest", "close to me", "around here", "open now", "open today",
                   "open late", "what time does", "when does it close", "when does it open", "opening hours",
                   "hours for", "phone number for", "phone number of", "address of", "address for", "somewhere to eat",
                   "place to eat", "places to eat", "where can i get", "where can i buy", "where can we eat",
                   "find a restaurant", "find a pharmacy", "pharmacy", "drugstore", "urgent care", "hardware store",
                   "grocery store", "gas station", "petrol station", "takeout", "take-out", "takeaway"}):
            {"find_places"},
        frozenset({"medicine", "medication", "medications", "prescription", "side effect", "side effects", "dosage", "dose of",
                   "doses of", "how much tylenol", "how much ibuprofen", "missed a dose", "missed dose", "drug interaction",
                   "interact with", "interacts with", "can i take", "can you take", "safe to take", "take it with",
                   "is it safe to mix", "contraindication", "black box warning", "boxed warning", "overdose",
                   "antibiotic", "antibiotics", "painkiller", "package insert", "prescribed for", "used to treat"}):
            {"medicine_info"},
        frozenset({"studies on", "studies about", "studies show", "study on", "study about", "the research say", "research says",
                   "research on", "is there evidence", "any evidence", "the evidence for", "evidence that", "clinical trial",
                   "clinical trials", "meta-analysis", "meta analysis", "systematic review", "peer reviewed", "peer-reviewed",
                   "scientific literature", "the literature on", "published papers", "papers on", "papers about",
                   "journal article", "journal articles", "randomized", "randomised", "cite a study", "scientific consensus"}):
            {"paper_search"},
        frozenset({"wolfram", "integrate ", "integral of", "derivative of", "differentiate", "solve for", "solve the equation",
                   "factor the", "roots of", "eigenvalue", "standard deviation of", "probability of", "monthly payment",
                   "mortgage payment", "loan payment", "compound interest", "amortization", "molar mass", "half-life of",
                   "calories in", "how many calories", "grams of protein in", "nutrition facts for"}):
            {"ask_wolfram"},
        frozenset({"weather", "weather forecast", "the forecast", "forecast for", "will it rain", "going to rain", "chance of rain",
                   "is it raining", "will it snow", "going to snow", "chance of snow", "how hot", "how cold", "how warm",
                   "temperature outside", "temperature today", "temperature tomorrow", "need a jacket", "need a coat",
                   "need an umbrella", "wind chill", "heat index", "how windy", "air quality", "weather alert",
                   "weather warning", "storm warning", "tornado warning", "tornado watch", "frost tonight", "freeze tonight"}):
            {"weather"},
        frozenset({"what is this", "what's this", "whats this", "what kind of plant", "what kind of bug", "what kind of mushroom",
                   "what kind of snake", "what kind of spider", "what kind of bird", "what kind of tree", "what plant is",
                   "what mushroom is", "what bug is", "what snake is", "what spider is", "what bird is", "what animal is",
                   "identify this", "identify the photo", "identify this photo", "in this photo", "in the photo", "in this picture",
                   "in the picture", "is this edible", "is it edible", "safe to eat", "is this poisonous", "is it poisonous",
                   "is this venomous", "is it venomous", "is this poison ivy", "what does this label", "what does the label",
                   "read this label", "read the label", "whose tracks", "what made these tracks"}):
            {"identify_photo"},
        # What the library holds: shelves, and "do we have this book".
        frozenset({"shelf", "shelves", "do we have", "do you have", "have you got", "what do you have", "what do we have",
                   "what books", "which books", "any books", "books on", "books about", "books by", "anything by",
                   "cookbooks", "cook books", "novels", "the classics", "in the library", "in your library",
                   "in my library", "library hold", "library have", "library has", "browse the library",
                   "history section", "history shelf", "what's on the", "what is on the", "my files", "my documents",
                   "my books", "audiobook"}):
            {"library_browse", "library_search"},
        # Files copied into the library folder on the server, and re-filing.
        frozenset({"scan the library", "scan my library", "scan for new", "new files", "files i copied", "files i added",
                   "i dropped", "i copied", "i brought over", "sort my books", "sort the books", "sort them",
                   "file them", "sort the library", "organize the library", "organise the library",
                   "move it to", "wrong shelf", "put it on the", "put it under", "refile", "re-file"}):
            {"manage_library", "library_browse"},
        # Offline reference library (Kiwix + PDF manuals).
        frozenset({"offline library", "reference library", "the library", "kiwix",
                   "wikipedia", "encyclopedia", "field manual", "army manual",
                   "manual say", "manuals", "survival", "first aid", "edible",
                   "poisonous", "forage", "foraging", "trapping", "snare",
                   "field dress", "purify water", "water purification",
                   "internet is down", "no internet", "offline"}):
            {"library_search"},
        frozenset({"research", "reserach", "reasearch", "look into", "investigate",
                   "deep dive", "deep research", "find out about", "study up on",
                   "report on", "do research", "look up everything"}):
            {"trigger_research"},
        # Settings-change intent — "change my…/set my…/use X for…/turn on…".
        frozenset({"change my", "set my", "use the voice", "change the voice",
                   "my voice", "tts voice", "search engine", "default model",
                   "teacher model", "task model", "background model", "image quality",
                   "reminder channel", "send reminders to", "remind me by",
                   "speak faster", "speak slower", "agent timeout", "token budget",
                   "max tool calls", "use this model for", "use that model for",
                   "my settings", "change setting", "change a setting", "set setting",
                   "preference", "preferences", "configure"}):
            {"manage_settings", "ui_control"},
        # API-integration intent → the api_call tool. Mirrors the agent-loop
        # "integrations" domain so api_call still surfaces on the retrieval and
        # keyword-fallback paths (not just the deterministic domain seed) when a
        # user names a connected service.
        frozenset({"api_call", "api call", "integration", "integrations",
                   "home assistant", "homeassistant", "miniflux", "gitea",
                   "linkding", "jellyfin"}):
            {"api_call"},
        # Managing EXISTING research in the Library — open/read/find/delete.
        frozenset({"my research", "the research", "research on", "open research",
                   "read research", "find research", "delete research",
                   "remove research", "list research", "my reports", "the report",
                   "saved research", "research library", "past research",
                   "research i did", "research about"}):
            {"manage_research", "trigger_research"},
        # Document edit/update intent
        frozenset({"edit", "change", "fix", "rewrite", "update",
                   "replace", "add a", "tweak", "modify", "rename", "paragraph",
                   "section", "line", "the doc", "the docs", "the document", "the documents", "in the doc", "in the docs", "in document"}):
            {"edit_document", "update_document", "create_document", "suggest_document"},
        # Document deletion / management — include generic open/find/read/show
        # verbs + file/doc synonyms so "open my <X>", "find the <X>", "delete
        # <X>" reach manage_documents even without the literal word "document".
        frozenset({"delete this doc", "delete the doc", "delete document",
                   "remove document", "remove the doc", "trash", "list document", "list documents",
                   "list doc", "list docs", "all my docs", "my document", "my documents", "my doc", "my docs", "my files",
                   "open the", "open my", "open document", "open doc", "find the",
                   "find my", "find document", "read the", "read my", "show me the",
                   "show my", "the file", "my file", "the report", "the write-up",
                   "the writeup", "saved document", "in my library", "in the library"}):
            {"manage_documents", "edit_document"},
        # Theme / UI control intent
        frozenset({"theme", "color scheme", "colors of the ui", "make it dark",
                   "make it light", "make the ui", "switch theme", "change theme",
                   "dark mode", "light mode", "toggle"}):
            {"ui_control"},
        # Model Shopper (model serving) intent — "stop the model", "what's running",
        # etc. The bare word "cookbook" is NOT here any more: it means the Kitchen.
        # The two old phrases stay because nobody stops a food cookbook.
        frozenset({"model shopper", "shopper", "kill cookbook", "stop cookbook",
                   "stop the model", "kill the model", "kill my model",
                   "what's running", "what is running", "whats running",
                   "running models", "running model", "running server",
                   "shut down vllm", "shutdown vllm", "stop vllm",
                   "stop serving", "kill serve", "cancel serve"}):
            {"list_served_models", "stop_served_model"},
        # Cookbook serve / launch / preset / server selection
        frozenset({"serve", "launch", "spin up", "start the model", "run the model",
                   "preset", "presets", "which server", "what servers",
                   "gpu box", "cookbook server", "model shopper server", "vllm", "on the server", "on the gpu"}):
            {"serve_preset", "serve_model", "list_serve_presets",
             "list_cookbook_servers", "list_cached_models"},
        # Cookbook downloads
        frozenset({"download", "downloading", "downloads",
                   "cancel download", "stop download", "kill download",
                   "what's downloading", "download progress", "pull model", "grab model"}):
            {"list_downloads", "cancel_download", "download_model",
             "list_cookbook_servers"},
        # HuggingFace search + cached model browse
        frozenset({"huggingface", "hugging face", "hf search",
                   "find a model", "search models", "search for a model",
                   "models for", "best model for"}):
            {"search_hf_models", "list_cached_models"},
        frozenset({"cached models", "list models", "my models",
                   "what models do i have", "is it downloaded",
                   "do i have", "already downloaded", "on disk"}):
            {"list_cached_models", "search_hf_models"},
        # Tool on/off / panel open intent — user says "turn off shell",
        # "disable search", "open library", "show gallery", etc.
        frozenset({"turn off", "turn on", "disable", "enable",
                   "shell off", "shell on", "search off", "search on",
                   "research off", "research on", "incognito",
                   "switch model", "change model", "set mode", "agent mode", "chat mode",
                   "open library", "open documents", "open gallery", "open email",
                   "open inbox", "open settings", "open memories", "open memory",
                   "open skills", "open notes", "open chats", "open sessions",
                   "show library", "show gallery", "show inbox", "show settings",
                   "show memory", "show memories", "show skills", "show notes",
                   "show chats", "show sessions", "show documents"}):
            {"ui_control"},
        # Document creation intent
        frozenset({"write a", "create a doc", "draft", "compose", "poem", "story",
                   "essay", "outline", "letter"}):
            {"create_document", "edit_document", "update_document"},
    }

    def get_tools_for_query(
        self, query: str, k: int = 8, always_include: Optional[Set[str]] = None,
        min_score: Optional[float] = None,
    ) -> Set[str]:
        """Get the set of tool names to include for a given user query.

        ``min_score`` applies to the tools found by likeness only (see
        ``retrieve``); the ones a word in the message names outright are
        included either way."""
        base = set(always_include or ALWAYS_AVAILABLE)
        retrieved = self.retrieve(query, k=k) if min_score is None else self.retrieve(query, k=k, min_score=min_score)
        base.update(retrieved)
        # Keyword-based force-include for common intents. Match on word
        # boundaries, not raw substrings, so short hints like "fix", "line",
        # "serve", "reply" or "unread" don't fire inside unrelated words
        # ("prefix", "deadline"/"online", "observe"/"reserve", "replying",
        # "unreadable"). Same word-boundary matching used in topic_analyzer.
        ql = query.lower()
        for keywords, tools in self._KEYWORD_HINTS.items():
            if any(re.search(rf"\b{re.escape(kw)}\b", ql) for kw in keywords):
                base.update(tools)
        # Structural scheduling-intent detection — typo-resilient (the literal
        # keyword "every day" misses "every dya"). Catches "every <word>",
        # daily/nightly/etc., or a clock time like "at 7:30 am" / "7am", which
        # all signal a recurring/scheduled task. Force-include manage_tasks so
        # the agent can actually create the cron job instead of fumbling.
        if self._SCHEDULE_RE.search(ql):
            base.add("manage_tasks")
        if self._LIST_RE.search(ql):
            base.add("manage_lists")
        # URL/site requests need web tools even when embedding retrieval is
        # stubbed/unavailable. Keep this structural, not always-on, so trivial
        # prompts do not drag web schemas into the agent context.
        if self._WEB_RE.search(query):
            base.update({"web_search", "web_fetch"})
        # Wherever a web lookup is on offer, offer the offline library too
        # (when one is installed): it is the better source for settled
        # knowledge and the only one when the internet is down.
        # The same goes for anything phrased as a question ("how do I treat a
        # copperhead bite"): embedding retrieval over tool descriptions is
        # poor at spotting plain knowledge questions, and those are exactly
        # what the library answers — with or without internet.
        if ("web_search" in base or _QUESTION_RE.search(query)) and _library_ready():
            base.add("library_search")
        # Hard steering: when the query is a clear "save info about a specific
        # person" pattern (address paste + name, phone next to a name, etc.),
        # the model has been observed defaulting to manage_memory even with
        # manage_contact in the toolset. Pull memory out for these queries so
        # the model literally cannot pick it. ALWAYS_AVAILABLE includes
        # manage_memory by default; we override that here.
        # The "for/to <word>" check needs to allow lowercase names (users
        # don't always capitalize) but filter out timing/pronoun stopwords
        # so "save this for later" / "save for tomorrow" don't trigger.
        _CONTACT_STOPWORDS_AFTER_FOR = {
            "later", "tomorrow", "yesterday", "now", "then", "today",
            "tonight", "me", "us", "you", "him", "her", "them", "myself",
            "yourself", "next", "this", "that", "the", "a", "an", "future",
            "real", "use", "uses", "another", "future", "reference",
        }
        # Regex catches "save (this|it|the|her|...|<noun>) for <name>" / "to my
        # contacts" patterns. More forgiving than literal-keyword matching —
        # 'save this address for Alex' uses one extra word between 'save' and
        # 'for' that breaks the contiguous 'save this for' phrase.
        save_for_match = re.search(
            r"\bsave\b(?:\s+\w+){0,3}\s+(?:for|to)\s+([A-Za-z]+)",
            ql,
        )
        # "to my contacts", "into my contacts", "in my address book", etc.
        to_contacts = re.search(r"\b(?:to|in|into)\s+(?:my\s+)?(?:contacts|address\s+book)\b", ql)
        # Possessive: "save (his|her|their) (address|phone|email|number) ..."
        # — strong contact signal even without "for <name>". Force-include
        # manage_contact here too since the keyword fallback misses this
        # construction.
        possessive_contact = re.search(
            r"\bsave\b(?:\s+\w+){0,2}\s+(?:his|her|their)\s+(?:address|phone|number|email|contact|details)",
            ql,
        )
        word_after = (
            save_for_match.group(1).lower() if save_for_match else None
        )
        contact_only_signal = (
            (save_for_match is not None
             and word_after is not None
             and word_after not in _CONTACT_STOPWORDS_AFTER_FOR)
            or to_contacts is not None
            or possessive_contact is not None
        )
        if possessive_contact is not None:
            base.add("manage_contact")
        if contact_only_signal and "manage_contact" in base:
            base.discard("manage_memory")
        return base


_QUESTION_RE = re.compile(
    r"^\s*(how|what|why|when|where|which|who|whom|whose|is|are|was|were|can|could|does|do|did|"
    r"should|will|would)\b|\?\s*$|\bhow (to|do|does|can|should|long|much|many)\b",
    re.I,
)


def _library_ready() -> bool:
    """True when the offline library is switched on and its server answers
    (cached inside services.library, so this is cheap per query)."""
    try:
        from services import library
        return bool(library.available())
    except Exception:
        return False


# ── Singleton ──

_tool_index: Optional[ToolIndex] = None
_last_attempt = 0.0
_RETRY_INTERVAL = 30.0


def get_tool_index() -> Optional[ToolIndex]:
    """Get or create the singleton ToolIndex. Returns None if unavailable."""
    global _tool_index, _last_attempt

    if _tool_index is not None and _tool_index.healthy:
        return _tool_index

    now = time.monotonic()
    if now - _last_attempt < _RETRY_INTERVAL:
        return None
    _last_attempt = now

    try:
        _tool_index = ToolIndex()
        _tool_index.index_builtin_tools()
        return _tool_index
    except Exception as e:
        logger.warning(f"ToolIndex init failed (will retry in {_RETRY_INTERVAL}s): {e}")
        _tool_index = None
        return None


def reset_tool_index() -> None:
    """Clear the singleton so embedding endpoint changes rebuild tool lanes."""
    global _tool_index, _last_attempt
    _tool_index = None
    _last_attempt = 0.0
