<h1 align="center">Aegis</h1>

<p align="center">
  <b>A private AI workspace you host yourself — models, agents, memory, a reference library and your own apps, all on your hardware.</b><br>
  Reach it from your phone. Keep using it when the internet is down. No cloud account, no per-token bill.
</p>

<p align="center">
  <a href="QUICKSTART.md">Quickstart</a> ·
  <a href="docs/setup.md">Setup</a> ·
  <a href="docs/engine-setup.md">Engine (Windows)</a> ·
  <a href="docs/engine-setup-linux.md">Engine (Linux)</a> ·
  <a href="docs/memory-and-users.md">Memory &amp; people</a> ·
  <a href="docs/home.md">Home screen</a> ·
  <a href="docs/offline-library.md">Offline library</a> ·
  <a href="docs/workbench.md">Workbench</a> ·
  <a href="ROADMAP.md">Roadmap</a>
</p>

<p align="center">
  <img src="docs/media/chat.png" alt="Aegis — chatting with a local model, with its thinking process and token speed" width="900">
</p>

---

## What Aegis is

Aegis is one self-hosted application that puts local models to work: chat, autonomous
agents, research, coding, image / video / music generation and voice. It runs on a laptop
for one person, or on an always-on box at home that the whole household opens from their
phones over a private network.

Five things set it apart:

- **It is yours.** Models, chats, memories and files stay on your machine. Nothing is
  metered, logged or trained on by a third party, and any OpenAI-compatible endpoint can be
  added when *you* decide local horsepower is not enough.
- **It knows who it is talking to — and shows you everything it knows.** Each account has a
  deep, living memory: facts with the words you said as evidence, a dossier per subject in
  your life, a digest of every conversation, and a profile written from all of it. One
  page lists every item, where it came from and every change ever made to it, with the
  means to correct or delete any of it. One tap makes you nobody.
- **It fits everyone in the house.** The same server gives one person a full AI workspace
  and another a plain home screen — chat, lists, reminders, trackers, boards — with roles
  that decide what each account may do.
- **It still works when the internet does not.** Put an offline Wikipedia, medical
  references and thousands of field manuals on the same disk as the models: they are
  searched page by page, and a photo of a plant, mushroom, insect or label is identified
  locally.
- **It does things.** Agents browse, research, run code, build and host apps, and run
  commands on your other computers — from a chat on your phone.

## What is in it

| Area | What you get | More |
|---|---|---|
| **Models** | llama.cpp + llama-swap serving any GGUF through one endpoint with native tool calls, a hardware-aware context tuner, per-model thinking / temperature settings, optional cloud endpoints | [Windows](docs/engine-setup.md) · [Linux](docs/engine-setup-linux.md) |
| **Memory** | Facts with evidence, topic dossiers, conversation digests and a profile, kept current in the background; a page to inspect, trace, correct and delete all of it; Nobody mode | [Guide](docs/memory-and-users.md) |
| **People & access** | Separate accounts, roles, per-person app and assistant manner, PINs, an administrator's console with an activity log | [Guide](docs/memory-and-users.md#people-roles-and-each-persons-app) |
| **Home screen** | A simple phone screen for non-technical people: chat, lists, reminders (with phone notifications), trackers, boards | [Guide](docs/home.md) |
| **Offline library** | Kiwix collections (Wikipedia, medicine, how-to, Stack Exchange) plus a page-level index of PDF manuals; used automatically when the internet is down | [Guide](docs/offline-library.md) |
| **Photo identification** | `/identify`: species classifier + local vision model + the library's own notes on lookalikes | [Guide](docs/offline-library.md#photo-identification) |
| **Workbench** | Projects in git, apps kept running as services with an address, commands on other machines over SSH, hand-offs to other coding agents | [Guide](docs/workbench.md) |
| **Agents** | Tool-using agent mode, toolboxes, Deep Research, browser automation, skills, MCP servers | |
| **Recipes** | One-click workflows you can schedule or build by describing them | [Guide](docs/recipes.md) |
| **Studio** | Local image, video and music generation, an editor, a movie maker, voice cloning | [Guide](docs/studio.md) |
| **Everyday** | Documents, email triage, notes, tasks, calendar, web search | |
| **Operations** | Control Center, Doctor self-check, local traces, backup and restore | [Backup](docs/backup-restore.md) |

## It knows who it is talking to

Aegis keeps a memory of each person in four layers, and keeps it current in the background
while you chat:

- **Facts** — single lasting statements, each with the words you said that showed it and
  the chat it came from. When something changes ("I'm up to four hives now") the fact is
  updated in place, not duplicated.
- **Topics** — a dossier per subject in your life (a hobby, a project, a person, your
  home): dated notes from every chat that touched it, and a digest — where things stand,
  a timeline, open questions — rewritten from those notes.
- **Conversations** — a short digest of each chat, so the next one can pick up the thread.
- **A profile** — the standing picture written from all of it, given to the model on every
  turn.

So "hi" is answered by name; a question about boiling eggs comes back short and in metric,
if that is what you like, without your hobbies shoehorned in; a question about a new
prescription starts with the allergy you mentioned three weeks ago; and "when should I
treat the hives next?" is answered from what you told it about *your* hives.

**You can see all of it.** `/mind` (or *Brain → See everything & history*) shows every
fact, topic, digest and profile line; where each came from; a log of every change — what
it was before and after, when, and whether you, the assistant or the background learner
made it — with undo; the exact text the model would be given for any message; and the
controls: kinds of information never to keep, export everything, forget everything.

```
/me                  what Aegis knows about you, with a link to the full view
/me add <fact>       tell it something
/me off | /me on     pause | resume using and learning (deletes nothing)
/me forget           delete everything it knows about you (asks first)
```

- **Yours to correct.** A profile line you write is kept word for word; one you remove
  never comes back. A fact you wrote or pinned is never rewritten by the learner.
- **It forgets when you say so.** Delete a fact and it is out of the profile at once — the
  profile never outlives what it was written from.
- **Nobody mode.** Tap the eye on the welcome screen and that chat uses no memory, learns
  nothing and is not saved.
- **Per person.** Nothing crosses between accounts, and an administrator cannot read
  anyone else's memory.

<p align="center">
  <img src="docs/media/mind-overview.png" alt="Your memory: counts, the summary, and what the model is told about you" width="230">
  <img src="docs/media/mind-topic.png" alt="A topic dossier: summary, where things stand, open questions" width="230">
  <img src="docs/media/mind-history.png" alt="History: every change, who made it, and undo" width="230">
</p>

→ [Memory, and more than one person](docs/memory-and-users.md)

## It fits everyone in the house

**A home screen for people who do not want an AI workspace.** Give someone the *Household*
role and signing in lands them on one plain screen: a chat box and four things.

> *Add bread and butter to my groceries, and remind me to call the vet tomorrow at 3.*

It does it, says so in a sentence, and offers a button to open the list. Lists and plans,
reminders (once or repeating, with notifications on the phone), trackers (a habit, an
amount toward a daily goal, a 1–5 rating) and boards (photos, links and notes around an
idea). No models, modes or settings anywhere.

**Roles and each person's app.** `/admin` is the administrator's console: add people,
give each a role (Administrator, Member, Household, Guest, or your own), choose which app
they get, what the assistant is called for them and how it should talk to them, allow a
PIN instead of a password, switch an account off, and see who signed in and what was
changed. No role can hand out access to the server itself.

<p align="center">
  <img src="docs/media/home-screen.png" alt="The home screen: what is coming up, and things to say" width="200">
  <img src="docs/media/home-chat.png" alt="Asking about a list and upcoming reminders in one sentence" width="200">
  <img src="docs/media/home-list.png" alt="A grocery list" width="200">
  <img src="docs/media/home-trackers.png" alt="Trackers: a daily habit and an amount toward a goal" width="200">
</p>
<p align="center">
  <img src="docs/media/admin-people.png" alt="The administrator's console: people, their roles and how much each has stored" width="200">
  <img src="docs/media/admin-roles.png" alt="Roles and what each lets the assistant do" width="620">
</p>

→ [Home screen](docs/home.md) · [People, roles and each person's app](docs/memory-and-users.md#people-roles-and-each-persons-app)

## It works with the internet down

Put a reference library on the server and Aegis answers from it, with links to the exact
article or manual page:

- **Kiwix collections of your choosing** — for instance the full English Wikipedia with
  images, WikiMed and other medical references, repair guides, Stack Exchange archives,
  Project Gutenberg, maps. A complete set is around 200 GB.
- **Manuals, page by page** — Aegis indexes the text of every PDF inside those collections
  (Army field manuals, first-aid, water, shelter, navigation, food preservation, plant and
  mushroom guides…), with optional OCR for scans, so a question lands on *"page 37 of the
  manual"* instead of a 300-page file.
- **Automatic fallback** — web search uses the library when the internet is unreachable, and
  the agent has a `library_search` tool for settled knowledge either way.
- **`/lib <words>`** for a quick lookup; the whole library is also browsable from a phone.
- **It grows by asking** — *"take these PDFs and add them to the survival library"*, *"add
  this link"*, or *"go out and find the official guide on … and add it"*. Each document is
  read page by page (OCR for scans) and is searchable in the next answer.

**Identify from a photo.** `/identify` (or *+ → Identify photo*) takes a picture of a
plant, mushroom, insect, spider, snake, track or object. A species classifier and a local
vision model name the likely match; the answer lists what to check in hand, the dangerous
lookalikes, and what the library says. It also reads labels and pages. Identification from
a photo is a lead, not a verdict — the answer says so where it matters.

→ [Offline library and photo identification](docs/offline-library.md)

## It builds, hosts and operates

Agent mode is a development and operations seat you can use from anywhere:

> *"Build me a workout tracker and host it."* · *"Restart the tracker and show me its log."*
> · *"How much disk is free on my Windows PC?"* · *"Have Claude Code add login to it."*

- **Code mode** — the third position of the mode switch (Agent · Chat · Code): a coding
  agent that reads, edits, runs and reports. It works on the server unless you pick another
  of your machines in the composer; then its shell and file tools act on that computer
  (PowerShell on Windows).
- **Projects** (`/project`) — create or clone a git repository on the server; the project
  you open stays open in that chat, and the agent's file and shell tools work inside it.
- **Hosted apps** (`/apps`) — deploy a project as a service that survives restarts, with
  logs, status and an address on your private network.
- **Other machines** (`/hosts`, `/remote <host> <command>`) — run commands and copy files on
  your other computers over SSH, PowerShell on Windows included.
- **Hand-offs** — give a task to another coding agent installed on the server (Claude Code,
  Codex, Kimi Code, OpenCode, Aider) and get the result back in the chat.
- Plus the built-in **coding agent** (`/code`), **Code Canvas** (`/canvas`: generate, edit,
  run) and **Repo → Wiki** (`/wiki`).

All of it is limited to administrators and written to an audit log.

<p align="center">
  <img src="docs/media/code-mode.png" alt="Code mode: the assistant creates a project, writes a script and runs it; the machine picker is open" width="820">
</p>

→ [Workbench](docs/workbench.md)

## Use it from your phone

Aegis is a web app that installs to the home screen. It listens on the server itself; to
reach it from elsewhere, publish it on a network you control rather than the open
internet. With Tailscale, for example:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:7000
```

Every device signed in to your tailnet can then open the server's `https://…ts.net`
address, at home or away, and nobody else can. Apps hosted by the workbench get their own
port the same way. HTTPS, reverse proxies and other options are in the
[setup guide](docs/setup.md).

## Runs on whatever you have

You choose how strong the models are, and the app helps you fit them to the machine:

- **Models are just files.** Drop any GGUF into `models/` — a 2 GB quantized model for a
  laptop, a 60 GB one for a large-memory box. Every feature works the same; speed and
  quality scale.
- **Cookbook scans your hardware** and rates every model against it — fit, memory, context
  and expected speed — then downloads and serves the one you pick.
- **The engine sizes itself — or you do.** `/engine` reads each model's real memory cost
  and what is free, sets every context window, and lets you override any of them. No YAML.
- **CPU-only works**, and so do very different machines: the screenshots below were made on
  a desktop with one RTX 4090; the same code runs a 120B open-weight model at 40–50
  tokens per second on a 128 GB unified-memory mini PC.
- **Media scales too.** Image and video sizes, steps and quantizations are adjustable, and
  Lightning LoRAs cut diffusion to 4–8 steps for modest cards.

## Everything else it does

**Talk to models.** Any local GGUF or API model, with tools, files, skills and memory;
model comparison side by side; per-model thinking on / off / auto, temperature and
response length.

**Put agents to work.** Toolboxes (OSINT recon, market analysis, network troubleshooting,
web crawl) · Deep Research with source reading and a cited report · browser automation on
real pages · recipes on a schedule or when new email arrives · MCP servers and teachable
skills.

**Create.**
- **Studio** (`/studio`) — a media library plus explicit makers for images, music and
  movies, an image editor, albums, style presets, the job queue and a tagged model library.
- **Images** (`/image`) with seeds, steps and negative prompts; **stylize** or **animate**
  any photo.
- **Video** (`/video`) — clips up to ~10 s, with audio on LTX-2 models; Wan 2.2,
  LTX-2 or HunyuanVideo 1.5; image-to-video from any still.
- **Music** (`/song`) — full songs with vocals on ACE-Step 1.5, or a cover of a track you
  upload.
- **Movie maker** — reorder, trim and stitch clips; mismatched sizes, framerates and audio
  are matched for you.
- **Style presets** (`/style`) — one locked look across every image and every shot.
- **Two media engines** — stable-diffusion.cpp for speed, ComfyUI for what it cannot run;
  memory is handed back after every job.
- **Chat survives your renders** — while a render holds the GPU, chat is routed to a small
  CPU model, says so, and switches back afterwards.

**Talk and listen.** On-device speech-to-text, Kokoro text-to-speech with ~30 voices, a
hands-free Voice Mode, voice cloning in the Voice Lab, and a local vision model for images
and screenshots.

**Stay organized.** AI-assisted documents, email (IMAP / SMTP triage and drafts), notes,
tasks, calendar (reminders, scheduled agent tasks, CalDAV) and web search.

**Run the place.** Control Center with live status for everything · Doctor (`/doctor`)
with guarded one-click fixes · local traces (`/traces`) · knowledge-graph memory
(`/graph`) · backup and restore · eighteen themes and a customizer · a few Y2K easter eggs
(`/bonzi`, `/winamp`, `/win98`).

Type `/` in the chat box to see every command.

## Quick start

New here? The **[Quickstart guide](QUICKSTART.md)** goes from first launch to your first
model.

### macOS — one pasted line

```bash
curl -fsSL https://raw.githubusercontent.com/ZackieShan/Aegis/main/install.sh | sh
```

Downloads Aegis, sets everything up (it asks before installing anything) and opens your
browser — at `http://localhost:7860` on a Mac, where AirPlay holds the usual port.

### Windows — one pasted line

```powershell
irm https://raw.githubusercontent.com/ZackieShan/Aegis/main/install.ps1 | iex
```

Installs Git and Python if missing, adds **Aegis to your Start Menu** and opens the browser
at `http://127.0.0.1:7000`.

### Linux

```bash
git clone https://github.com/ZackieShan/Aegis.git && cd Aegis
bash start-linux.sh                 # the app, at http://127.0.0.1:7000
scripts/setup-engine.sh             # the local model engine — see the Linux engine guide
```

The [Linux engine guide](docs/engine-setup-linux.md) covers llama.cpp, llama-swap, media
engines and running everything as services on an always-on machine.

### Docker

```bash
git clone https://github.com/ZackieShan/Aegis.git && cd Aegis
cp .env.example .env
docker compose up -d --build
```

Open `http://localhost:7000` when the containers are healthy.

### Already cloned?

`launch-windows.bat` on Windows; `bash start-macos.sh` / `bash start-linux.sh` elsewhere.

Sign in with **admin / admin** and change the password straight away (*Settings →
Account*). Native installs, GPU notes, HTTPS and configuration are in the
[setup guide](docs/setup.md).

### After the first chat

| To add | Do |
|---|---|
| Local models | The engine guide for your OS, then *Cookbook* to pick models that fit |
| Another person | `/admin` → *Add person*: pick a role (Household for the simple home screen) — [guide](docs/memory-and-users.md#people-roles-and-each-persons-app) |
| The offline library | Install Kiwix, choose collections, index — [guide](docs/offline-library.md#setting-it-up) |
| Building and hosting apps | [Workbench guide](docs/workbench.md) |

## A tour in screenshots

The media below — the photos and the video included — was produced by models Aegis served
locally on one machine with a single RTX 4090.

### Models and the engine

**One engine, many models.** The picker lists every GGUF served through llama-swap — chat,
coding, vision, image and video models — with a live "loaded" indicator:

![Model picker with locally served models](docs/media/models.png)

**The engine tunes itself.** `/engine` reads each GGUF's real KV-cache cost and your free
memory, then right-sizes every context window — no YAML editing, no restart:

![The /engine command showing per-model context recommendations](docs/media/engine.png)

**Cookbook.** Scans your hardware and rates every model against it — fit, memory, context
and expected speed — then downloads and serves the one you pick:

![Cookbook hardware scan and model fit table](docs/media/cookbook.png)

**Control Center.** One dashboard for the engine, memory, models, agents and every
capability's health:

![Control Center dashboard](docs/media/control-center.png)

### Memory, research and writing

**Brain.** The long-term memory Aegis carries across chats — recall, edit, pin and delete
what it knows about you, alongside teachable skills. The profile is written from these:

![Brain panel with long-term memories](docs/media/brain.png)

**Deep Research.** An agent runs rounds of web search, reads the sources and writes a cited
report — here 16 rounds over 117 URLs on a local model, ending in a visual report:

![Deep Research reading sources with a live progress graph](docs/media/research-progress.png)

![The finished visual research report](docs/media/research-report.png)

**Document editor.** Notes and documents on one side, the chat on the other, with
versioning and markdown preview:

![Document editor beside the chat](docs/media/doc-editor.png)

**Code Canvas.** Describe what to build; the local coding model writes it into a runnable
buffer you can edit with follow-up instructions:

![Code Canvas with generated Python](docs/media/code-canvas.png)

### Recipes

**A workflow library you run, schedule and build.** Pick a recipe, type an input, hit Run —
no node wiring:

![Recipes library of one-click workflows](docs/media/recipes-library.png)

**Turn any recipe into an automation.** A recipe plus a trigger — daily, every N hours, a
cron, or *when new email arrives* — plus a delivery becomes a job that runs itself:

![Recipes automations — recipes on a schedule](docs/media/recipes-automations.png)

**Build your own by describing it.** Pick a template or describe what you want and a local
model drafts the node graph; **Explain** summarizes any workflow in plain English:

![Recipes editor — start from a template or describe what you want](docs/media/recipes-editor.png)

![Recipes walkthrough — run, schedule, build](docs/media/recipes-howto.gif)

> Full walkthrough: the **[Recipes guide](docs/recipes.md)**.

### Studio

**Video from chat.** `/video a small red fox trotting through fresh snow at golden hour`
submits a job to the engine and streams progress into the chat until the clip lands:

![Generating a video from chat with /video](docs/media/video-flow.gif)

The finished clip — LTX-2.3, rendered locally in about three minutes, audio included:

<p align="center">
  <img src="docs/media/sample-video.gif" alt="A locally generated video clip of a fox in snow at golden hour" width="560">
</p>

**The Media library.** Everything you make or upload in one place — photos, generated
images, videos and songs — with filters, favorites, search and one-click delete:

![The Studio Media library with photos, songs and videos](docs/media/studio-media.png)

![The gallery opened over a chat, with an imported photo and a generated clip](docs/media/gallery.png)

![Photo detail view](docs/media/image-detail.png)

**The Image and Movie Makers.** Every control on screen — model, style, size, steps, seed,
negative prompt — and clip generation next to the film timeline:

![The Image Maker with all diffusion controls](docs/media/image-maker.png)

![The Movie Maker with clip generation and film assembly](docs/media/movie-maker.png)

**The Music Maker.** Compose from style tags and lyrics, or cover a reference track, then
play everything in a real track player that keeps going while you browse:

![The Music Maker with song composer and track player](docs/media/music-maker.png)

**The Voice Lab.** Preview any of the ~30 local voices, or clone one from a ten-second
recording:

![The Voice Lab with voice preview and cloning](docs/media/voice-lab.png)

**AI image editor.** Masked inpaint, background removal, upscaling and whole-image
instruction edits, driven by a served edit model:

![Image editor with AI inpaint panel](docs/media/editor.png)

**Style presets — one look across every prompt.** A preset locks a model, prompt affixes,
a seed and LoRAs into a named style. Different prompts, same preset:

![Two different prompts rendered with one locked style preset](docs/media/style-presets.png)

**Image-to-video.** Any still grows an **Animate** button (or `/video image=last <motion>`);
the clip starts on your exact image:

<p align="center">
  <img src="docs/media/image-to-video.gif" alt="A generated still animated into a video clip — she lowers the umbrella as rain falls" width="400">
</p>

**The model library and styles.** Every served model and every file in the drop folder,
tagged by capability and best use:

![Studio model library with capability tags](docs/media/media-studio.png)

![Studio style preset editor](docs/media/media-studio-styles.png)

**Create, watch, stitch.** Generate in place, follow every long job in the queue — chat is
answered by a CPU model while a render holds the GPU — and stitch clips into a film:

![Create — prompt, model, style, duration in one panel](docs/media/studio-create.png)

![The queue during a live render — chat protected on a CPU model](docs/media/studio-queue.png)

![The movie maker with two clips staged](docs/media/studio-movie.png)

**A second engine: ComfyUI.** Aegis drives ComfyUI workflows as a sibling to llama-swap —
GGUF video models, joint audio + video LTX-2 merges, FLUX.2-klein, Lightning LoRAs — and
frees its memory after every job. This klein render took 40 seconds:

<p align="center">
  <img src="docs/media/comfy-klein.png" alt="A photoreal cabin interior generated locally by FLUX.2-klein through the ComfyUI engine" width="560">
</p>

> Full walkthrough: the **[Studio guide](docs/studio.md)**.

### Looks

**Eighteen built-in themes**, plus a customizer for colors, fonts and background effects:

![Theme picker with the built-in themes](docs/media/themes.png)

**The Aegis Amp** (`/winamp`) — a late-90s player skin for your Studio songs:

![The Aegis Amp Y2K player skin over the Studio](docs/media/aegis-amp.png)

**And Bonzi** (`/bonzi`, or *Settings → Appearance*) — a certain purple gorilla from 1999,
resurrected locally from the original MS-Agent sprite data:

<p align="center">
  <img src="docs/media/bonzi.gif" alt="Bonzi Buddy easter egg — the purple gorilla waves hello next to the chat box" width="500">
</p>

![The Bonzi easter egg on the welcome screen](docs/media/bonzi-buddy.png)

## Security

Aegis gives a language model real tools on a real machine. Treat it accordingly:

- **Keep sign-in on, and change the default password.** Failed sign-ins are slowed down per
  account as well as per address. Two-factor sign-in is available.
- **Do not put it on the open internet.** Publish it on a private network (a VPN or a
  tailnet) and never expose the raw model or service ports.
- **Give other people regular accounts.** Shell, code execution, server files, email tools,
  the workbench and all settings are administrator-only, and no role can grant them. A
  short PIN is an administrator's per-account choice, meant for a phone on a private
  network.
- **Retrieved text is data, not instructions.** Web pages, documents, library articles and
  saved memories reach the model in guarded blocks marked untrusted. That reduces prompt
  injection; it does not make it impossible — review what an agent is about to do with
  anything that matters.
- **Keep private data out of Git.** Everything personal lives in `data/`, which is ignored.

More in [SECURITY.md](SECURITY.md), the [threat model](THREAT_MODEL.md) and the
[setup guide](docs/setup.md#security-notes).

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE) and [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).

Aegis began as a fork of [Odysseus](https://github.com/pewdiepie-archdaemon/odysseus)
(AGPL-3.0-or-later) and has grown well beyond it; with thanks for the foundation. Full
attribution is in [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).
