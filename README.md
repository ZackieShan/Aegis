<h1 align="center">Aegis</h1>

<p align="center">
  A self-hosted AI workspace for chat, agents, research, documents, email, notes, calendar, and local model workflows.
</p>

<p align="center">
  <a href="QUICKSTART.md">Quickstart</a> ·
  <a href="docs/setup.md">Setup Guide</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="ROADMAP.md">Roadmap</a>
</p>

---

## What Aegis adds over Odysseus

Aegis is a heavily extended fork of [Odysseus](https://github.com/pewdiepie-archdaemon/odysseus),
rebuilt around a **local-first, closed-loop** philosophy — everything runs on your own
hardware, no cloud API keys required. The differentiated engine binaries (llama.cpp,
llama-swap, Node/Playwright, Aider, stable-diffusion.cpp) live *outside* the repo and are
set up with one command — `scripts\setup-engine.ps1` (see the
**[engine setup guide](docs/engine-setup.md)**); this repo is the application. What's new:

### Local model engine
- **llama.cpp + llama-swap** — hot-swap GGUF models through one OpenAI-compatible endpoint,
  with grammar-locked **native tool calls** (far more reliable than text-parsed tools).
- **Zero-config model drop folder** — drop a `.gguf` (or a folder with a GGUF + `mmproj`) into
  `models/` and it's auto-discovered; serve it with one click.
- **Context auto-tuner** (`/engine`) — reads each model's GGUF metadata + your GPU VRAM and
  **right-sizes the context window automatically** — no hand-editing configs, no "context
  exceeded" errors.

### Agentic capabilities
- **Toolboxes** — themed MCP tool collections the agent can summon (opt-in per message):
  **OSINT** (recon), **Market** (analysis), **Troubleshoot** (network/systems), **Web**
  (crawl & extract).
- **Recipes** — a visual node editor to chain tools + models into workflows, with **branch**
  (conditional) and **loop** (refine) nodes.
- **Coding agent** (`/code`) — Aider wrapped as a workspace-scoped, git-aware coding agent.
- **Code Canvas** (`/canvas`) — an artifact-style editor: generate code, edit it inline, tell
  the AI what to change, and **run it** (Python in-browser, HTML live preview, or server-side).
- **Repo → Wiki** (`/wiki`) — point at any local repo and get a structured Overview /
  Architecture / Module-guide wiki, generated locally.
- **Browser automation** — a built-in Playwright MCP lets the agent navigate, read, and click
  real web pages via accessibility snapshots.

### Local media
- **Image generation** — Qwen-Image (or any diffusion GGUF) via stable-diffusion.cpp, fully
  local and OpenAI-images-compatible.
- **Voice** — local Whisper speech-to-text + text-to-speech, plus a hands-free **Voice Mode**:
  speak → the agent acts → it reads the reply back.
- **Vision** — a vision model (Qwen2.5-VL) for screenshot / image understanding.

### Knowledge, memory & observability
- **Knowledge-graph memory** (`/graph`) — a local *(subject, relation, object)* graph extracted
  from your notes and chats.
- **Local tracing** (`/traces`) — every model/agent call recorded to a local SQLite store
  (with optional opik export). No data leaves the machine.

### Operability
- **Control Center** — one dashboard showing every capability's **live status** with a one-click
  "try it," so nothing is hidden behind commands.
- **Doctor** (`/doctor`) — capability self-check with guarded, one-click fixes for missing
  dependencies.
- **Windows-native** — runs natively on Windows with no Docker, plus many Windows-specific fixes
  (paths, shell, GPU serving).
- **Aurora theme** — a purple/black aurora-borealis theme, JetBrains Mono, and a trident mark.

Everything above is opt-in and local. See [ROADMAP.md](ROADMAP.md) for what's next.

## Quick Start

New here? The **[Quickstart guide](QUICKSTART.md)** covers everything from
first launch to connecting your first model.

### Windows (native, no Docker)

Double-click `launch-windows.bat`, or from a terminal:

```powershell
.\launch-windows.bat
```

This creates a virtualenv, installs dependencies, runs first-time setup, and
starts the server at `http://127.0.0.1:7000`. Requires Python 3.11+.

### Linux / macOS (native, no Docker)

```bash
./start-linux.sh    # Linux — requires Python 3.11+
./start-macos.sh    # macOS
```

### Docker

```bash
cp .env.example .env
docker compose up -d --build
```

Open `http://localhost:7000` when the containers are healthy.

Log in with **admin / admin** and change the password after first login
(Settings → Account). Native installs, GPU notes, Windows/macOS instructions,
HTTPS, and configuration live in the [setup guide](docs/setup.md).

## Features

- **Chat + Agents** — local/API models, tools, MCP, files, shell, skills, and memory.
- **Cookbook** — hardware-aware model recommendations, downloads, and serving.
- **Deep Research** — multi-step web research with source reading and report generation.
- **Compare** — blind side-by-side model testing and synthesis.
- **Documents** — writing-first editor with AI edits, suggestions, Markdown, HTML, CSV, and syntax highlighting.
- **Email** — IMAP/SMTP inbox with triage, tags, summaries, reminders, and reply drafts.
- **Notes, Tasks + Calendar** — reminders, todos, scheduled agent tasks, and CalDAV sync.
- **Extras** — gallery/image editor, themes, uploads, web search, presets, sessions, and 2FA.

## Security

Aegis is a self-hosted workspace with powerful local tools. Keep auth enabled, keep private data out of Git, and do not expose raw model/service ports publicly. Deployment details are in the [setup guide](docs/setup.md#security-notes).

## License

AGPL-3.0-or-later -- see [LICENSE](LICENSE) and [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).

Aegis is based on [Odysseus](https://github.com/pewdiepie-archdaemon/odysseus)
(AGPL-3.0-or-later). See [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) for full attribution.
