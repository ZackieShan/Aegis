# Aegis Quickstart

Aegis is a self-hosted AI workspace — chat, agents, deep research, documents,
email, notes, calendar, and local model workflows — that runs entirely on your
own machine.

## 1. Start it

The fastest path is one pasted line — it downloads Aegis, sets everything up,
and opens the app in your browser when it's ready:

### macOS — paste into Terminal (⌘-Space, type "Terminal")

```bash
curl -fsSL https://raw.githubusercontent.com/ZackieShan/Aegis/main/install.sh | sh
```

First run takes a few minutes: it installs what it needs (it will ask before
installing Homebrew if you don't have it), asks you to create your admin
account, and **opens your browser by itself** — on a Mac the app lives at
**http://localhost:7860** (macOS AirPlay occupies the usual 7000).

### Windows — paste into PowerShell

```powershell
irm https://raw.githubusercontent.com/ZackieShan/Aegis/main/install.ps1 | iex
```

Installs Git/Python if needed, puts **Aegis in your Start Menu**, and opens
the browser at **http://localhost:7000** when the server is up. From then on,
launch it like any app: Start → Aegis.

### Docker (Windows / macOS / Linux)

Requires [Docker](https://docs.docker.com/get-docker/) with Compose:

```bash
git clone https://github.com/ZackieShan/Aegis.git && cd Aegis
cp .env.example .env
docker compose up -d --build
```

This also starts the bundled companion services (SearXNG web search, ChromaDB
vector store, ntfy notifications) — nothing else to install. App at
**http://localhost:7000**.

### Already have the code? (git clone or ZIP)

From the project folder: `launch-windows.bat` on Windows,
`bash start-macos.sh` on macOS, `bash start-linux.sh` on Linux. (Use
`bash <script>` rather than `./<script>` — it works even when the ZIP
download loses the executable bit.)

## 2. Log in

The first launch asks you to create your admin account in the terminal (if
you skip it, the default is **admin / admin** — change it right after logging
in under Settings → Account). The server binds to `127.0.0.1` by default, so
it is only reachable from your own machine until you deliberately expose it.

## 3. Connect a model

Aegis is bring-your-own-model. **Easiest first model,** especially on a Mac:
install [Ollama](https://ollama.com/download) (a normal drag-to-Applications
app), then in Aegis open **Settings (gear icon) → Add Models → Scan for
Servers** — Aegis finds it and connects. Pull a small model like `llama3.2`
in Ollama and it appears in the chat picker.

The other paths, whichever fits:

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
