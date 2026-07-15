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
- **Media studio** — local video generation up to ~10s (Wan 2.2, LTX-2 with audio),
  image-to-video from any gallery still, style presets (one locked look — model + prompt
  affixes + seed + LoRAs — across every generation), an `/image` command with real
  seeds/steps/negative prompts, and a model library that auto-tags everything on disk by
  capability and best use.
- **Two media engines** — stable-diffusion.cpp for speed and **ComfyUI** as a sibling
  engine for what it can't run (GGUF video merges, FLUX.2-klein, Lightning LoRAs), with
  VRAM handed back after every job so both share one GPU.
- **Knowledge & operability** — knowledge-graph memory, local call tracing, a Control
  Center dashboard, and a Doctor self-check with guarded one-click fixes.

## Where it's going

### Make it reproducible everywhere
- **Cross-platform engine setup** — the one-command engine installer and guide are
  Windows-first today; bring the same to Linux and macOS (matching release binaries + layout).
- **Fresh-install smoke tests** across Linux, macOS, and Windows — native, Docker, and WSL.
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
- **One-click serve** — the Media Studio library already shows which models on disk aren't
  served; add a button that writes the engine config entry for you.
- **Video editing steps** — trim, frame-interpolate, and ESRGAN-upscale clips from the
  Gallery, reusing the upscalers the library already tags.

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
