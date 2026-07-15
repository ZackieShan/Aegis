# Local engine setup

Aegis's differentiated capabilities run on local inference **engine binaries** that
live **outside the repo** (so they never enter git): llama.cpp + llama-swap for GGUF
serving, a portable Node + Playwright for browser automation, Aider for the coding
agent, faster-whisper for voice, and stable-diffusion.cpp for image generation.

This guide gets a fresh clone from "app only" to the full local stack.

> **Platform:** Windows + an NVIDIA GPU. The prebuilt binaries are CUDA 12; the
> setup script is PowerShell. On Linux/macOS the same pieces exist — grab the
> matching release binaries and mirror the layout below.

## Layout

The engine installs next to the repo by default (override with `-EngineDir` or the
`AEGIS_ENGINE_DIR` env var):

```
<parent>/
  aegis/            <- this repo (app + models/)
  engine/           <- created by setup-engine.ps1
    llamacpp/       llama-server.exe + CUDA DLLs
    llama-swap/     llama-swap.exe
    node/           portable Node.js + npx (Playwright MCP)
    aider-venv/     isolated venv with aider-chat
    sdcpp/          stable-diffusion.cpp (sd-server.exe)
    llama-swap.yaml your model config
```

`launch-windows.ps1` auto-starts llama-swap from this location and puts the portable
Node on `PATH`, so once the engine is set up everything comes up with one launch.

## One-shot setup

From the repo root (after the app venv exists — run `launch-windows.ps1` once first):

```powershell
.\scripts\setup-engine.ps1
```

Flags: `-SkipImageGen` (skip the ~1.5 GB stable-diffusion.cpp download),
`-SkipBrowser` (skip Node/Playwright), `-SkipAider`, `-EngineDir <path>`.

The script downloads the latest release of each component, creates the Aider venv,
installs `faster-whisper` + `gguf` into the app venv, and writes a starter
`llama-swap.yaml`. It's idempotent — re-run it any time; present pieces are skipped.

## What each piece powers

| Piece | Powers | In-app check |
|---|---|---|
| llama.cpp + llama-swap | local chat/agent models, native tool calls | `/engine`, model picker |
| portable Node + Playwright | Browser automation MCP | `/doctor` → Browser |
| Aider (isolated venv) | the `/code` coding agent | `/code status` |
| faster-whisper | Voice input (local Whisper) | `/doctor` → Voice input |
| gguf (pip) | context auto-tuner metadata | `/engine` recommendations |
| stable-diffusion.cpp | local image generation | Studio / "generate an image" |

## Adding models

1. Drop a `.gguf` (or a folder with a GGUF + `mmproj` for vision) into the repo's
   `models/` folder. It's auto-discovered by Cookbook.
2. Add a block to `engine/llama-swap.yaml` — copy an example from
   [`llama-swap.example.yaml`](llama-swap.example.yaml) and fix the paths. Because
   llama-swap runs with `-watch-config`, edits **hot-reload** (no restart).
3. In the app, run **`/engine tune`** to size each model's context window to your GPU.

Good starting models (drop into `models/`):
- **Coder / tool-caller:** a Qwen3-Coder-30B GGUF (excellent, reliable native tool calls).
- **Vision:** Qwen2.5-VL (GGUF + matching `mmproj`) for screenshots/images.
- **Image:** Qwen-Image diffusion GGUF + its Qwen2.5-VL text encoder + VAE (three files, `--diffusion-model` / `--llm` / `--vae`).

## Registering the endpoint

llama-swap exposes one OpenAI-compatible endpoint at `http://127.0.0.1:9090/v1`.
In **Admin → Models → Add endpoint**, point at that URL and mark **"supports tools"**
(so the agent uses native tool calls). All GGUFs in your `llama-swap.yaml` then show
up in the model picker.

## Enabling the features

- **Voice:** Settings → Voice → provider **local** (uses faster-whisper). Then the
  🎤 **Voice Mode** button and the composer mic work on-device.
- **Image generation:** Settings → enable image generation, and set the image model
  to your `llama-swap.yaml` image entry (e.g. `qwen-image`).
- **Browser / coding / everything else:** open the **Control Center** (grid icon in
  the rail) — every capability shows a live green/amber/grey status and a one-click
  "try it." `/doctor` offers guarded one-click fixes for anything missing.

## Troubleshooting

- **A model "spills" and generation crawls:** it doesn't fit VRAM at full offload.
  Lower `-ngl` (partial offload) or use a smaller quant. `/engine` recommends safe
  context sizes and skips partial-offload models.
- **"context exceeded" 400s:** raise `-c` in `llama-swap.yaml` (or run `/engine tune`).
- **Browser won't connect:** ensure Node is set up (`/doctor` → Browser), then restart
  Aegis so the built-in Browser MCP registers.
- **A download 404s:** the script matches the latest release asset by name; if an
  upstream renames it, grab it manually from that project's Releases page and extract
  into the matching `engine/<...>` folder.
