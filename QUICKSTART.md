# Aegis Quickstart

Aegis is a self-hosted AI workspace — chat, agents, deep research, documents,
email, notes, calendar, and local model workflows — that runs entirely on your
own machine.

## 1. Start it

Pick the path that matches your machine. Every path ends with the app at
**http://localhost:7000**.

### Windows (native, no Docker)

Requires [Python 3.11+](https://www.python.org/downloads/). **Double-click
`launch-windows.bat`** in the project folder — or from a terminal:

```powershell
.\launch-windows.bat
```

First run creates a virtualenv, installs dependencies, runs setup, and starts
the server. Re-running it skips whatever already exists. (The `.bat` wrapper
exists because Windows blocks `.ps1` scripts by default; it runs
`launch-windows.ps1` with the policy bypassed.)

### Docker (Windows / macOS / Linux)

Requires [Docker](https://docs.docker.com/get-docker/) with Compose:

```bash
cp .env.example .env
docker compose up -d --build
```

This also starts the bundled companion services (SearXNG web search, ChromaDB
vector store, ntfy notifications) — nothing else to install.

### Linux (native)

Requires Python 3.11+ (`python3-venv` on Debian/Ubuntu):

```bash
./start-linux.sh
```

Sets up the environment, starts a local ChromaDB for vector search, and
launches the app. `tmux`, `git`, and `cmake` are optional (Cookbook model
serving); the script tells you if they're missing.

### macOS (native)

```bash
./start-macos.sh
```

## 2. Log in

- **Username:** `admin`
- **Password:** `admin`

**Change the password right after your first login** (Settings → Account).
The server binds to `127.0.0.1` by default, so it is only reachable from your
own machine until you deliberately expose it.

## 3. Connect a model

Aegis is bring-your-own-model. Open **Settings (gear icon) → Add Models** and
pick whichever fits:

- **Hosted API** — choose a provider preset (Anthropic, OpenAI, OpenRouter,
  Z.AI, Groq, Gemini, Mistral, …), paste your API key, hit **Test**, then
  **Add**. Models appear in the chat picker immediately.
- **Local server you already run** — pick **Ollama (local)**, **LM Studio
  (local)**, or **llama.cpp / vLLM (local)**; no API key needed. Or hit
  **Scan for Servers** and Aegis finds anything listening on the usual ports.
- **Nothing installed yet?** Open **Cookbook** in the sidebar — it reads your
  actual hardware (GPU/VRAM/RAM), recommends models that fit, and handles the
  download and serving for you.
- **Have GGUF files already?** Drop them into the app's **`models/` folder**
  (one subfolder per model) and they appear in Cookbook → Serve automatically —
  see [models/README.md](models/README.md).

Then pick your model in the chat's **Select model** menu and start talking.

## 4. Optional extras

Everything below is optional — the core app works without it and degrades
gracefully:

| Extra | What it adds | How |
|---|---|---|
| **ChromaDB** | Vector search for document RAG and semantic memory (otherwise keyword fallback) | Started automatically by the native launchers and Docker — nothing to do |
| **SearXNG** | Self-hosted web search for Deep Research | `docker compose up -d searxng`, or point Settings → Search at any instance |
| **Git Bash** (Windows native) | Cookbook background downloads + agent shell tool | [git-scm.com](https://git-scm.com/download/win) |
| **Node.js** | Browser-automation MCP tools | [nodejs.org](https://nodejs.org), then restart Aegis |
| **ntfy** | Push notifications for reminders | `docker compose up -d ntfy` |
| **.env** | Email (IMAP/SMTP), CalDAV sync, ports, pre-seeded admin credentials | copy `.env.example` → `.env` and edit |

## 5. Good to know

- **Your data** lives in the `data/` folder next to the app (SQLite + JSON) —
  back that folder up and you've backed up everything.
- **Themes and fonts** are under **Theme** in the sidebar; the default is the
  dark-violet *aurora* theme with JetBrains Mono.
- **More depth**: [docs/setup.md](docs/setup.md) covers GPU setups, HTTPS,
  reverse proxies, and every configuration flag.

## Security in one paragraph

Keep auth enabled, change the default password, and don't port-forward the
raw app or model-server ports to the internet. If you want remote access, use
a VPN (Tailscale works well) or a reverse proxy with HTTPS — see the
[setup guide](docs/setup.md#security-notes).
