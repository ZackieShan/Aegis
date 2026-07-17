# Roadmap

Aegis's direction is a single idea taken seriously: **a complete, private AI environment
you fully own** — every model, agent, and tool running on your hardware, integrated into
one loop, extensible, and honest about what it does. Below is where that's headed.
Feedback and contributions are very welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Recently shipped

The local-first foundation is in place:

- **Local model engine** — llama.cpp + llama-swap hot-swapping GGUFs with reliable native
  tool calls, a zero-config `models/` drop folder, and a GPU-aware context auto-tuner.
- **Agents** — Toolboxes (OSINT / market / troubleshoot / web), Deep Research, and
  Playwright browser automation.
- **Recipes — run, schedule, build** — a library of one-click workflows (summarize,
  triage, stock bull/base/bear, inbox declutter…); turn any of them into an **automation**
  that runs on a schedule or when new email arrives and delivers the result; and build
  your own by **describing it** to a local model — no blank canvas.
- **Software** — a git-aware coding agent (Aider), a Code Canvas (generate / edit / run),
  and repo → wiki.
- **Media & voice** — local image generation (stable-diffusion.cpp), on-device Whisper
  speech-to-text + text-to-speech with a hands-free Voice Mode, and a local vision model.
- **Studio** — one home for generated media: local video up to ~10s (Wan 2.2, LTX-2 with
  audio), image-to-video from any still, a **movie maker** that stitches clips into one film
  (reorder, trim, auto-matching mismatched sizes/framerates and ragged audio), style presets
  (one locked look — model + prompt affixes + seed + LoRAs — across every generation and every
  shot in a film), an `/image` command with real seeds/steps/negative prompts, and a model
  library that auto-tags everything on disk by capability and best use.
- **Two media engines** — stable-diffusion.cpp for speed and **ComfyUI** as a sibling
  engine for what it can't run (GGUF video merges, FLUX.2-klein, Lightning LoRAs), with
  VRAM handed back after every job so both share one GPU.
- **Maker studios** — the Studio reorganized around explicit makers: a Media library
  (section filters + delete), an Image Maker with every diffusion control on screen, a
  Music Maker (composer, a persistent track player, and the Voice Lab with in-place voice
  cloning), and a Movie Maker that pairs clip generation with the film timeline. Plus the
  Aegis Amp — a Y2K classic-skinned floating player easter egg (`/winamp`).
- **One queue, and chat that survives your renders** — every long job (renders, films,
  automations, research) in one list with progress, position, and cancel. And because a
  diffusion model fills the card while llama-swap resolves contention by *evicting*, a chat
  message used to kill an in-flight render: chat now falls back to a CPU-pinned model until
  the GPU is free, says so, and switches back.
- **Knowledge & operability** — knowledge-graph memory, local call tracing, a Control
  Center dashboard, and a Doctor self-check with guarded one-click fixes.
- **One-line install** — `curl | sh` (macOS/Linux) and `irm | iex` (Windows) installers
  that go from nothing to a running app with the browser opening itself; a Start Menu
  launcher on Windows; Cookbook downloads/serves no longer require tmux on macOS.

## Where it's going

### Make it reproducible everywhere
- **Cross-platform engine setup** — the one-command engine installer and guide are
  Windows-first today; bring the same to Linux and macOS (matching release binaries + layout).
- **Fresh-install smoke tests** across Linux, macOS, and Windows — native, Docker, and WSL.
  The macOS installer especially needs a real first-run test by a Mac user.
- **Tray supervisor** — a tiny signed tray app (Tauri) that starts/stops the services,
  shows status, and opens the UI: the ".exe feel" without freezing Python into a binary
  that antivirus heuristics hate.
- **Offline mode** — vendor the remaining CDN assets so a fully air-gapped install works
  end to end.

### Local models for every machine
- **Hardware-tiered model presets** — recommended model / quant / parameter profiles for
  small, medium, and large setups, surfaced in Cookbook and Deep Research so nobody guesses.
- **Smarter model ranking** — score by architecture age, quant format, VRAM/RAM fit, backend
  support, and vision/mmproj needs instead of scoring everything the same.
- **Slimmer agent prompts** — tool schemas, skills, memory, and instructions can eat a small
  model's context before the request even starts; tighter prompts and smaller default tool
  sets for 4k/8k/16k windows.

### Richer local media
- **Character animation (video-to-video)** — Wan 2.2 Animate through the ComfyUI engine:
  drive a character image with a reference video (pose transfer), the biggest unlock left
  in the media stack.
- **Image-to-video on more pipelines** — LTX-2 image conditioning through ComfyUI so the
  fast merges can animate stills too, not just the sd.cpp path.
- **Two-stage LTX upscaling** — render small, latent-upscale 2x, refine; the upscaler
  models are already part of the standard companion set.
- **One-click serve** — the Studio's model library already shows which models on disk aren't
  served; add a button that writes the engine config entry for you.
- **More editing steps** — the movie maker does reorder/trim/concat; frame-interpolation and
  ESRGAN upscaling are the natural next ones, reusing the upscalers the library already tags.
  Transitions (crossfades) and a music/voiceover track are the obvious follow-ups — the
  bundled ffmpeg already has `xfade`, and TTS is already local.

### Deepen the loop
- **Code Canvas everywhere** — "open in canvas" on chat code blocks, and auto-open when the
  model writes a substantial file.
- **Richer voice** — continuous / push-to-talk conversation mode.
- **Computer use** — extend browser control toward safe, opt-in desktop control (screen
  capture + a vision-guided action loop) with clear guardrails.

### Trust & safety
- **Prompt-injection hardening** — treat skills, notes, documents, fetched pages, and
  memories as untrusted input; keep testing whether models obey malicious instructions from
  those surfaces.
- **Admin-tool risk docs** — clear documentation of what the powerful local tools can do and
  how to lock them down.
- **Degraded-state reporting** — honest status for ChromaDB, search, email, notifications,
  and provider probes (the Control Center is the natural home for this).

### Reliability & polish
- Bug squashing and better error surfacing — show the real command / output, copyable logs,
  and next steps instead of just "crashed".
- Backup / restore for `data/`.
- Accessibility pass (keyboard nav, focus states, contrast, reduced motion) and cleaner
  empty/error states on fresh installs.
- Refactors: CSS cleanup, a shared onboarding-tour core, modal/positioning robustness, and
  dead-code passes for stale routes and feature flags.

## Contributing

If you hit a rough edge, a broken integration, or a murky corner of the codebase, that's
exactly the feedback that helps most. Open an issue or a PR — see
[CONTRIBUTING.md](CONTRIBUTING.md).
