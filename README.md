<h1 align="center">Aegis</h1>

<p align="center">
  <b>Your own AI workspace — every model, agent, and tool running on your hardware.</b><br>
  Private by default. No cloud accounts, no API keys, no per-token bill.
</p>

<p align="center">
  <a href="QUICKSTART.md">Quickstart</a> ·
  <a href="docs/setup.md">Setup</a> ·
  <a href="docs/engine-setup.md">Local engine</a> ·
  <a href="ROADMAP.md">Roadmap</a>
</p>

<p align="center">
  <img src="docs/media/chat.png" alt="Aegis — chatting with a local model, with its thinking process and token speed" width="900">
</p>

---

## What Aegis is

Aegis is a **fully self-hosted AI workspace**. Chat, autonomous agents, deep research,
coding, a web browser the AI can drive, image generation, and voice — all in one place,
all running on **your machine**. Nothing you do leaves your hardware unless you choose to
send it.

The premise is simple: the most capable AI tools shouldn't require renting someone else's
computer and handing over your data to use them. If you have a GPU, you should be able to
run a private assistant that browses, writes and runs code, researches, makes images, and
talks with you — and **own the whole thing**.

**What it unlocks**

- **Sovereignty** — your models, your data, your machine. Works offline; nothing is metered,
  logged, or trained on by a third party.
- **No ceiling** — run as much as your hardware allows. No usage caps, no per-token cost, no
  rate limits.
- **One integrated loop** — models, agents, knowledge, memory, and media work *together*
  instead of scattered across a dozen apps, tabs, and subscriptions.
- **Cloud-grade capability, self-owned** — the agent can browse the web, edit real code in a
  git repo, generate images, and take voice commands — locally.

**Where it's headed:** the default self-hosted AI environment for people who want serious
capability without surrendering privacy or control — extensible (MCP tools, visual workflows,
skills), approachable (one dashboard, one-click "try it"), and honest about running on
hardware you own. See the [roadmap](ROADMAP.md).

## What you can do

**Talk to models, locally**
- Chat with any local GGUF or API model — with tools, files, shell, skills, and long-term memory.
- A **local model engine** (llama.cpp + llama-swap) hot-swaps GGUFs through one endpoint with
  reliable **native tool calls**; drop a model in `models/` and serve it; the **context
  auto-tuner** (`/engine`) sizes each model's window to your GPU automatically.

**Put agents to work**
- **Toolboxes** — summon themed tool sets: OSINT recon, market analysis, network troubleshooting, web crawl.
- **Recipes** — chain tools and models into visual workflows, with branch and loop logic.
- **Deep Research** — multi-step web research with source reading and report generation.
- **Browser automation** — the agent navigates, reads, and clicks real web pages.

**Build software**
- **Coding agent** (`/code`) — edits real files in a git-aware workspace.
- **Code Canvas** (`/canvas`) — generate code, edit it inline, tell the AI what to change, and **run it**.
- **Repo → Wiki** (`/wiki`) — turn any local repo into a structured Overview / Architecture / module guide.

**Create & converse**
- **Image generation** — fully local diffusion, OpenAI-images-compatible.
- **Voice** — on-device speech-to-text + text-to-speech, plus a hands-free **Voice Mode**:
  speak, the agent acts, it reads the reply back.
- **Vision** — a local vision model for images and screenshots.

**Stay organized**
- AI-assisted **Documents**, **Email** (IMAP/SMTP triage + drafts), **Notes / Tasks / Calendar**
  (reminders, scheduled agent tasks, CalDAV), a **gallery / image editor**, and **web search**.

**Own the operation**
- **Control Center** — one dashboard with every capability's live status and a one-click "try it."
- **Doctor** (`/doctor`) — self-check with guarded, one-click fixes for anything missing.
- **Local observability** (`/traces`) and **knowledge-graph memory** (`/graph`) — insight and
  recall that never leave the machine.
- Runs **natively on Windows** (no Docker required) or via Docker.

> The local inference binaries (llama.cpp, llama-swap, Node/Playwright, Aider,
> stable-diffusion.cpp) install with one command — see the
> **[engine setup guide](docs/engine-setup.md)**.

## Screenshots

Everything below was produced on a single machine (one RTX 4090) by models Aegis
serves locally — including the video and the photos.

**Local video generation.** `/video a small red fox trotting through fresh snow at
golden hour` submits an async job to the engine (Wan 2.2 or LTX-2.3 under
stable-diffusion.cpp) and streams progress right into the chat until the clip lands:

![Generating a video from chat with /video](docs/media/video-flow.gif)

The finished clip — LTX-2.3, rendered locally in about three minutes, audio included:

<p align="center">
  <img src="docs/media/sample-video.gif" alt="A locally generated video clip of a fox in snow at golden hour" width="560">
</p>

**One engine, many models.** The picker lists every GGUF served through llama-swap —
chat, coding, vision, image, and video models — with a live "loaded in VRAM" indicator:

![Model picker with locally served models](docs/media/models.png)

**The engine tunes itself.** `/engine` reads each GGUF's real KV-cache cost and your
free VRAM, then right-sizes every model's context window — no YAML editing, no
restart; llama-swap hot-reloads the change:

![The /engine command showing per-model context recommendations](docs/media/engine.png)

**Image generation and gallery.** Generated media lands in the Gallery next to your
own photos (this lighthouse came out of a served Qwen-Image model in 8 steps):

![Gallery with generated image and video](docs/media/gallery.png)

![Photo detail view](docs/media/image-detail.png)

**AI image editor.** Masked inpaint, background removal, upscaling, and full-image
instruction edits, driven by a served edit model (Qwen-Image-Edit):

![Image editor with AI inpaint panel](docs/media/editor.png)

**Control Center.** One dashboard for the engine, VRAM, models, agents, and every
capability's health:

![Control Center dashboard](docs/media/control-center.png)

**Deep Research.** Ask a question, and an LLM-in-the-loop agent runs rounds of web
search, reads the sources, and writes a cited report — here it's 16 rounds over 117
URLs on a local model, ending in a magazine-style visual report:

![Deep Research reading sources with a live progress graph](docs/media/research-progress.png)

![The finished visual research report](docs/media/research-report.png)

**Cookbook.** Scans your hardware and rates every model against it — fit, VRAM,
context, and expected speed — then downloads and serves the one you pick:

![Cookbook hardware scan and model fit table](docs/media/cookbook.png)

**Brain.** Long-term memory the AI carries across chats — recall, edit, and curate
what it knows about you, alongside teachable skills:

![Brain panel with long-term memories](docs/media/brain.png)

**Document editor.** A split-pane workspace: notes and docs on one side, the chat on
the other, with versioning and markdown preview:

![Document editor beside the chat](docs/media/doc-editor.png)

**Code Canvas.** Describe what to build; the local coding model writes it into a
runnable buffer you can edit with follow-up AI instructions:

![Code Canvas with generated Python](docs/media/code-canvas.png)

**Recipes — visual multi-agent workflows.** Wire tools and models together on a node
canvas, with branch and loop logic. This built-in starter feeds two market tools into
three investor personas — value, growth, and contrarian — then a portfolio manager
weighs their takes; every node runs on your local model:

![Recipes node editor with the analyst-debate starter](docs/media/recipes.png)

**Eighteen built-in themes** (plus a full customizer — colors, fonts, background
effects). Aurora is the default; there's a Y2K "millennium core", a terminal green,
paper, cyberpunk, and more:

![Theme picker with the built-in themes](docs/media/themes.png)

**And… Bonzi.** Flip one switch in Settings → Appearance (or type `/bonzi`) and a
certain purple gorilla from 1999 moves back in — animations, sounds, terrible jokes
and all, resurrected locally from the original MS-Agent sprite data:

<p align="center">
  <img src="docs/media/bonzi.gif" alt="Bonzi Buddy easter egg — the purple gorilla waves hello next to the chat box" width="500">
</p>

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
./start-linux.sh    # Linux -- requires Python 3.11+
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

## Security

Aegis is a self-hosted workspace with powerful local tools. Keep auth enabled, keep private data out of Git, and do not expose raw model/service ports publicly. Deployment details are in the [setup guide](docs/setup.md#security-notes).

## License

AGPL-3.0-or-later -- see [LICENSE](LICENSE) and [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).

Aegis began as a fork of [Odysseus](https://github.com/pewdiepie-archdaemon/odysseus)
(AGPL-3.0-or-later) and has grown well beyond it; with thanks for the foundation. Full
attribution is in [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).
