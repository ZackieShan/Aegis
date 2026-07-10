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
