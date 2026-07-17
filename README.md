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
computer and handing over your data to use them. Whatever machine you have — an aging
laptop or a multi-GPU workstation — you should be able to run a private assistant that
browses, writes and runs code, researches, makes images, and talks with you — and **own
the whole thing**, with models sized to your hardware.

**What it unlocks**

- **Sovereignty** — your models, your data, your machine. Works offline; nothing is metered,
  logged, or trained on by a third party.
- **No ceiling** — run as much as your hardware allows. No usage caps, no per-token cost, no
  rate limits.
- **No minimum spec** — model strength is your dial, not a requirement: pick sizes and
  quantizations that fit whatever you're running (see below).
- **One integrated loop** — models, agents, knowledge, memory, and media work *together*
  instead of scattered across a dozen apps, tabs, and subscriptions.
- **Cloud-grade capability, self-owned** — the agent can browse the web, edit real code in a
  git repo, generate images, and take voice commands — locally.

**Where it's headed:** the default self-hosted AI environment for people who want serious
capability without surrendering privacy or control — extensible (MCP tools, visual workflows,
skills), approachable (one dashboard, one-click "try it"), and honest about running on
hardware you own. See the [roadmap](ROADMAP.md).

## Runs on whatever you have

Aegis doesn't demand a spec — **you choose how strong the models are**, and the app helps
you fit them to your machine:

- **Models are just files.** Drop any GGUF into `models/` — a 1–2 GB quantized model for a
  laptop, a 20 GB coder or 15 GB video model for a big GPU, anything in between. Every
  capability works the same regardless of which you pick; only speed and quality scale.
- **Cookbook scans your hardware** and rates every model against it — perfect / good /
  marginal fit plus an expected token speed — so you never have to guess what will run.
- **The engine right-sizes itself.** `/engine` reads each model's real memory cost and your
  free VRAM, then tunes every context window automatically — no YAML, no OOM roulette.
- **CPU-only works.** llama.cpp and Ollama run smaller models with no GPU at all, and any
  OpenAI-compatible endpoint can stand in where local horsepower runs out — your choice.
- **Media scales too.** Image and video sizes, steps, and quantizations are all adjustable,
  and Lightning LoRAs cut diffusion to 4–8 steps for modest cards.

## What you can do

**Talk to models, locally**
- Chat with any local GGUF or API model — with tools, files, shell, skills, and long-term memory.
- A **local model engine** (llama.cpp + llama-swap) hot-swaps GGUFs through one endpoint with
  reliable **native tool calls**; drop a model in `models/` and serve it; the **context
  auto-tuner** (`/engine`) sizes each model's window to your GPU automatically.
- **Per-model settings** — a sliders button by the model picker remembers, for each model:
  **thinking** on / off / auto (toggle a reasoning model's visible thought process without
  touching config), **temperature**, and **max response length**. Applied per message.

**Put agents to work**
- **Toolboxes** — summon themed tool sets: OSINT recon, market analysis, network troubleshooting, web crawl.
- **Recipes** — a library of one-click workflows you can also **schedule as automations**
  (daily, or when new email arrives) and **build by describing them**; chains tools and
  models with branch and loop logic.
- **Deep Research** — multi-step web research with source reading and report generation.
- **Browser automation** — the agent navigates, reads, and clicks real web pages.

**Build software**
- **Coding agent** (`/code`) — edits real files in a git-aware workspace.
- **Code Canvas** (`/canvas`) — generate code, edit it inline, tell the AI what to change, and **run it**.
- **Repo → Wiki** (`/wiki`) — turn any local repo into a structured Overview / Architecture / module guide.

**Create & converse**
- **Studio** (`/studio`) — one home for all your media, organized as explicit makers:
  a **Media** library (photos, generated images, videos, and music, with section filters,
  favorites, and one-click delete), an **Image Maker**, a **Music Maker**, a **Movie
  Maker**, the image editor, albums, style presets, the job queue, and the tagged model
  library. Every maker keeps all its controls on screen — prompt, model, style, size,
  steps, seed, negative prompt, duration — with a ✨ button that rewrites rough intent
  into a diffusion-ready scene using a local model. Video renders are stored as MP4.
- **Image generation** (`/image`) — fully local diffusion with seeds, steps, and negative prompts.
- **Video generation** (`/video`) — local clips up to ~10s, with audio on LTX-2 models; **animate
  any Studio still** into a clip (`/video image=last` or the Studio's Animate button). Chat uses
  your default model; the Movie Maker lets you pick per render — Wan 2.2 (with 4-step Lightning
  LoRAs for clean hands), LTX-2, or HunyuanVideo 1.5.
- **Stylize any photo** — the Stylize button on a Studio photo opens Create with the photo
  attached: describe the change ("make it a watercolor"), pick a style and an edit model
  (qwen-image-edit), and the result lands in Media next to the original. Animate works the
  same way — prompt, style, model, and clip length before anything renders.
- **Music generation** (`/song`) — full songs with vocals, locally, on ACE-Step 1.5: style
  tags + optional lyrics (Studio → **Music** is the Music Maker: composer with lyrics editor,
  a real track player with prev/next/seek that keeps playing while you browse, and the Voice
  Lab) → an MP3 in the Studio. A 45-second track renders in ~20 seconds on a 4090.
- **Cover any track** — upload an MP3 (or pick a Studio song) as a reference and ACE-Step
  re-imagines it in a new style, keeping the melody and structure: `/song from=last
  synthwave, retro 80s` or the Song tab's reference picker. New lyrics welcome.
- **Read replies aloud** — local Kokoro TTS with ~30 named voices (American/British,
  male/female) and a preview button in Settings → Text to Speech; runs faster than realtime
  on CPU so it never competes with renders for the GPU. Voice Mode closes the loop:
  speak → agent acts → the reply is spoken back.
- **Clone your own voice** — the **Voice Lab** (Studio → Music) records ~10 seconds, names
  it, and Aegis speaks as you (or grandma, with permission) via MIT-licensed Chatterbox on
  the local engine — with per-voice Test and "Use this voice" buttons, and automatic
  routing so a cloned voice works no matter which TTS provider is active.
- **Movie maker** — stitch your clips into one film: drag to reorder, trim heads and tails,
  render. Clips of different sizes/framerates are matched automatically, and clips without
  audio are padded with silence so a mixed set still concatenates cleanly.
- **Style presets** (`/style`) — lock a model + prompt affixes + seed + LoRAs into a named
  look that applies to every image and video generation — and holds across every shot in a film.
- **Model library** — every model on disk auto-tagged by capability and best use, with rescan.
- **Two media engines** — stable-diffusion.cpp for speed, **ComfyUI** for workflows it can't
  run (GGUF video merges, FLUX.2-klein, Lightning LoRAs); VRAM is handed back after every job.
- **A queue that tells the truth** — Studio → Queue lists every long job (renders, films,
  automations, research): what's running, how far along, what's next, and a cancel button.
- **Chat survives your renders** — a diffusion model fills the card, and the model server frees
  VRAM by *evicting*, so a chat message would otherwise kill an in-flight render. Aegis routes
  chat to a small CPU model until the GPU is free, tells you it did, and switches back after.
- **Voice** — on-device speech-to-text + text-to-speech, plus a hands-free **Voice Mode**:
  speak, the agent acts, it reads the reply back.
- **Vision** — a local vision model for images and screenshots.
- **Y2K easter eggs** — Bonzi Buddy (`/bonzi`) supervises your chats, and the **Aegis Amp**
  (`/winamp`) is a classic-late-90s-skinned floating player for your Studio songs — green
  LCD, spectrum bars, beveled buttons. It really whips the llama's ass.

**Stay organized**
- AI-assisted **Documents**, **Email** (IMAP/SMTP triage + drafts), **Notes / Tasks / Calendar**
  (reminders, scheduled agent tasks, CalDAV), the **Studio / image editor**, and **web search**.

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

**The Studio's Media library.** Everything you make or upload in one place — photos,
generated images, videos, and songs — with section filters, favorites, search, and
one-click delete (that's the Aegis Amp easter egg in the corner):

![The Studio Media library with photos, songs and videos](docs/media/studio-media.png)

![Photo detail view](docs/media/image-detail.png)

**The Music Maker.** Compose (style tags + lyrics, or cover a reference track), then
play everything you've made in a real track player — playback keeps going while you
browse other tabs:

![The Music Maker with song composer and track player](docs/media/music-maker.png)

**The Voice Lab.** Preview any of the ~30 local voices, or clone your own from a
10-second recording — Test and "Use this voice" buttons right on each clone:

![The Voice Lab with voice preview and cloning](docs/media/voice-lab.png)

**The Image and Movie Makers.** Every control on screen — model, style, size, steps,
seed, negative prompt — and the Movie Maker pairs clip generation with the film
timeline, so you make clips and stitch them in the same tab:

![The Image Maker with all diffusion controls](docs/media/image-maker.png)

![The Movie Maker with clip generation and film assembly](docs/media/movie-maker.png)

**The Aegis Amp.** A love letter to late-90s media players, one `/winamp` away —
it plays your Studio songs with a green LCD, spectrum bars, and beveled everything:

![The Aegis Amp Y2K player skin over the Studio](docs/media/aegis-amp.png)

**AI image editor.** Masked inpaint, background removal, upscaling, and full-image
instruction edits, driven by a served edit model (Qwen-Image-Edit):

![Image editor with AI inpaint panel](docs/media/editor.png)

**Style presets — one look across every prompt.** A preset locks a model, prompt
affixes, a seed, and LoRAs into a named style; activate it once and every image and
video generation matches. Different prompts, same preset, below — nothing else shared:

![Two different prompts rendered with one locked style preset](docs/media/style-presets.png)

**Image-to-video.** Any still in the Studio grows an **Animate** button (or
`/video image=last <motion prompt>` from chat) — the clip starts on your exact
image, first frame pixel-for-pixel:

<p align="center">
  <img src="docs/media/image-to-video.gif" alt="A generated still animated into a video clip — she lowers the umbrella as rain falls" width="400">
</p>

**Studio → Settings: the model library.** Every served model plus every file in the drop folder,
auto-tagged by capability and what it's best used for — with a rescan button and a
filter that searches the tags (try "extraction" or "translation"). Style presets live
next door in Studio → Styles:

![Studio model library with capability tags](docs/media/media-studio.png)

![Studio style preset editor](docs/media/media-studio-styles.png)

**Create, watch, stitch.** The Studio's Create tab generates images and clips in
place (with a ✨ button that rewrites rough intent into a diffusion-ready scene);
the Queue shows every long job — and while a render holds the GPU, chat answers
on a small CPU model *so the render survives*; the Movie tab stitches clips into
one film with reorder and trim:

![Create — prompt, model, style, duration in one panel](docs/media/studio-create.png)

![The queue during a live render — chat protected on a CPU model](docs/media/studio-queue.png)

![The movie maker with two clips staged](docs/media/studio-movie.png)

> Full walkthrough: the **[Studio guide](docs/studio.md)** — movie maker, styles,
> the queue, and why chat drops to a CPU model while the GPU is rendering.

**A second engine: ComfyUI.** Aegis drives ComfyUI workflows over its local API as
a sibling to llama-swap — GGUF video models via custom nodes, joint audio+video
LTX-2 merges, FLUX.2-klein, and 4-step Lightning LoRAs — and frees its VRAM after
every job so both engines share one GPU. This klein render took 40 seconds locally:

<p align="center">
  <img src="docs/media/comfy-klein.png" alt="A photoreal cabin interior generated locally by FLUX.2-klein through the ComfyUI engine" width="560">
</p>

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

**Recipes — a workflow library you run, schedule, and build.** Recipes opens to a
browsable library of one-click workflows — summarize, triage a message, stock
bull/base/bear, inbox declutter, and more. Pick one, type an input, hit Run — no node
wiring:

![Recipes library of one-click workflows](docs/media/recipes-library.png)

**Turn any recipe into an automation.** A recipe + a trigger — daily, every N hours, a
cron, or *when new email arrives* — + a delivery (an in-app notification, or saved to a
document) becomes a job that runs itself unattended and hands you the result:

![Recipes automations — recipes on a schedule](docs/media/recipes-automations.png)

**Build your own — by describing it.** The editor never starts blank: pick a template,
or describe what you want and a local model drafts the node graph. An **Explain** button
summarizes any workflow in plain English. Every node — tools and models — runs locally:

![Recipes editor — start from a template or describe what you want](docs/media/recipes-editor.png)

The whole loop — run a recipe, schedule it, then build one from a sentence:

![Recipes walkthrough — run, schedule, build](docs/media/recipes-howto.gif)

> Full walkthrough: the **[Recipes guide](docs/recipes.md)**.

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

### macOS — one pasted line

```bash
curl -fsSL https://raw.githubusercontent.com/ZackieShan/Aegis/main/install.sh | sh
```

Downloads Aegis, sets everything up (it asks before installing anything), and
opens your browser when it's ready — at `http://localhost:7860` on a Mac
(AirPlay holds the usual port).

### Windows — one pasted line

```powershell
irm https://raw.githubusercontent.com/ZackieShan/Aegis/main/install.ps1 | iex
```

Installs Git/Python if missing, adds **Aegis to your Start Menu**, and opens
the browser at `http://127.0.0.1:7000`. After that, launch it like any app.

### Docker

```bash
git clone https://github.com/ZackieShan/Aegis.git && cd Aegis
cp .env.example .env
docker compose up -d --build
```

Open `http://localhost:7000` when the containers are healthy.

### Already cloned? (any OS)

`launch-windows.bat` on Windows; `bash start-macos.sh` / `bash start-linux.sh`
elsewhere.

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
