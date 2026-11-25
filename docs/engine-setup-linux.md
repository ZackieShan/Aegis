# Local engine setup - Linux / Omarchy / AMD Strix Halo

The Linux counterpart of [engine-setup.md](engine-setup.md), written for the
box Aegis is moving to: a Minisforum MS-S1 MAX (Ryzen AI Max+ 395 "Strix
Halo", 16 Zen 5 cores, Radeon 8060S iGPU / gfx1151, 128GB unified LPDDR5X)
running Omarchy (Arch + Hyprland). Everything also applies to plain Arch; the
pacman steps map to apt/dnf on other distros.

No CUDA anywhere: llama.cpp and stable-diffusion.cpp build against Vulkan
(default) or ROCm, ComfyUI runs on ROCm PyTorch, and TTS/STT stay on CPU.

Work top to bottom - each step assumes the previous ones.

## 1. BIOS: keep the VRAM carveout SMALL

On Linux the GPU allocates memory from GTT (shared system memory), not the
BIOS carveout - the opposite of the Windows advice. In BIOS set the UMA /
dedicated VRAM size to the minimum (512MB, or "Auto"). A 96GB carveout on
Linux just steals RAM the kernel could have handed the GPU dynamically.

## 2. Kernel: raise the GTT limits

Default GTT caps near half of RAM - not enough for the 50-76GB flagship
models. Raise it once (this is what `setup-engine.sh --apply-gtt` writes):

```
echo 'options ttm pages_limit=31457280 page_pool_size=15728640' | \
  sudo tee /etc/modprobe.d/aegis-gtt.conf
sudo mkinitcpio -P        # Omarchy (limine unified kernel image): sudo limine-mkinitcpio
sudo reboot
```

On Omarchy the boot image is a limine UKI: plain `mkinitcpio -P` rebuilds an
initramfs the bootloader never loads and the ttm options silently do not
apply. `setup-engine.sh` prints the right command for the machine it runs on.
Set the small BIOS carveout from step 1 in the same reboot - a 120GiB GTT
limit on top of a 96GB carveout leaves the OS ~30GB.

The values are 4KiB pages: 31457280 = 120GiB GPU-allocatable, 15728640 =
60GiB pooled. `amdgpu.gttsize` is deprecated and logs a kernel warning - use
the ttm options. Verify after reboot:

```
cat /sys/class/drm/card*/device/mem_info_gtt_total   # expect ~128849018880
```

Also make sure your user is in the `video` and `render` groups:

```
sudo usermod -aG video,render $USER   # then log out and back in
```

## 3. Pick a backend: Vulkan (default) or ROCm

| | Vulkan (RADV) | ROCm (HIP) |
|---|---|---|
| Setup | mesa + vulkan-radeon, works out of the box | rocm-hip-sdk, heavier install |
| Token generation | Fastest in most tests; degrades little at long context | Often slower, falls off hard at long context |
| Prompt processing | Good (~1000 t/s on 30B MoE) | Sometimes wins on dense models |
| Stability on gfx1151 | Proven, boring | Recent builds fine; some versions broke shader compile |
| Gotchas | - | >64GB mmap bug: every llama-server entry needs `--no-mmap`; `--comfy` toolboxes also disable mmap |

Measured on the MS-S1 MAX, 2026-11-22 (llama.cpp cbb1c1c, Mesa 26.2.5 RADV
vs ROCm 7.2.4, `llama-bench -fa 1 -p 512 -n 128`, generation tok/s with 0 /
8K / 30K tokens already in context):

| Model (quant) | Vulkan RADV | ROCm HIP |
|---|---|---|
| gpt-oss-120b Heretic (MXFP4, 63GB) | 51 / 47 / 40 | 47 / 42 / 34 |
| Qwen3.8-Flash-Next abliterated (IQ3_XXS, 76GB) | 28 / 27 / 26 | 25 / 24 / 22 |
| Mistral Small 4 119B (Q4_K_M, 73GB) | 34 / 30 / 24 | 35 / 30 / - |
| MiniMax-M2.1 REAP 139B (Q4_K_M, 84GB) | 29 / 22 / 14 | 26 / 17 / - |
| GLM-Steam 106B (Q4_K_M, 73GB) | 23 / 20 / 14 | 23 / 16 / - |

RADV won or tied every row, so the stock llama-swap template points every
alias at the Vulkan build. Re-run the matrix after a Mesa/ROCm/llama.cpp
upgrade - winners have moved before.

Vulkan is the default for llama.cpp and sd.cpp; pass `--backend rocm` to
build HIP variants instead (and add `--no-mmap` to the big-model entries in
`llama-swap.yaml` if you do). ComfyUI is the exception: it always uses ROCm
PyTorch (gfx1151 is officially supported in ROCm 7.x), or CPU wheels with
`--comfy-cpu`. The TheRock gfx1151 nightly wheel index is the performance
alternative if the stock ROCm wheels disappoint - point `TORCH_INDEX_URL` at
it.

## 4. Clone the repo and create the app venv

```
git clone <your-aegis-remote> ~/ai/aegis
cd ~/ai/aegis
python -m venv venv
venv/bin/pip install -r requirements.txt
```

## 5. Run the engine installer

```
cd ~/ai/aegis
bash scripts/setup-engine.sh                 # Vulkan everywhere, CPU TTS
bash scripts/setup-engine.sh --backend rocm  # HIP llama.cpp + sd.cpp
bash scripts/setup-engine.sh --apply-gtt     # also write the modprobe conf
bash scripts/setup-engine.sh -h              # all flags
```

What it does (idempotent - re-run it any time; `--force` rebuilds):

- pacman packages (toolchain, Vulkan stack, espeak-ng, ffmpeg)
- builds llama.cpp from source (GGML_VULKAN=ON, or GGML_HIP for rocm) and
  installs llama-server/cli/quantize/bench into `engine/llamacpp/`
- downloads the llama-swap linux_amd64 release into `engine/llama-swap/`
- builds stable-diffusion.cpp from recent master (SD_VULKAN=ON) - Aegis
  needs sd-server, the /v1/images endpoints and the async vid_gen API, so
  do not pin SDCPP_REF to an old tag
- clones ComfyUI + the Aegis custom nodes into `engine/comfyui/` with a
  ROCm-torch venv, and installs extra_model_paths.yaml pointing at models/
- creates the chatterbox and qwen3tts venvs with the vendored server.py
  (CPU torch by default; `--tts-rocm` to put them on the GPU)
- aider venv, portable Node LTS + Playwright Chromium
- installs faster-whisper/gguf/kokoro/soundfile into the app venv
- installs `engine/llama-swap.yaml` from the template (next section)

The engine lands in `<repo-parent>/engine` (override: `--engine-dir` or
`AEGIS_ENGINE_DIR`), mirroring the Windows layout without the `.exe`s.

## 6. rsync the models tree from the old box

All model paths live under the repo's `models/` dir (~722GB on the old box).
From the Windows machine (or anywhere that can read the drive):

```
rsync -avP --partial /mnt/e/AI/aegis/aegis/models/ user@newbox:~/ai/aegis/models/
```

GGUF and safetensors are byte-portable - no conversion needed. Trim what you
do not use first; the flagship chat models alone are 50-76GB each.

## 7. llama-swap.yaml

`setup-engine.sh` installs `engine/llama-swap.yaml` from
`docs/llama-swap.linux.yaml` (a template with `__ENGINE_DIR__` /
`__MODELS_DIR__` placeholders, ported from the Windows config). Review it
after install:

- every alias must point at a file that actually exists in `models/`
- the 4090's partial-offload tunings are gone: everything is `-ngl 99`
  (unified memory - the whole model is GPU-resident)
- the cpu-group entries (chat-lite-cpu, chat-moe-cpu) keep `-ngl 0` /
  `--device none` on purpose: they are the GPU-guard fallback so chat can
  never evict an in-flight render
- the two TTS servers sit in the cpu group as well: `setup-engine.sh` gives
  their venvs CPU torch by default, and in the exclusive gpu group every
  spoken reply unloaded the chat model. If you build them with `--tts-rocm`,
  move them back to `gpu`
- on ROCm add `--no-mmap` to entries whose GGUF exceeds 64GB

The main chat model is `gpt-oss-120b` (63GB): it is preloaded when llama-swap
starts (`hooks.on_startup.preload`) and stays warm for an hour (`ttl: 3600`).
`gpt-oss-120b-fast` is not a second copy - it is a `filters.setParamsByID`
variant of the same process that forces `reasoning_effort: low`. Point
Aegis's Utility role at it: short background calls (titles, summaries,
memory extraction) otherwise spend their whole token budget on the model's
reasoning and come back empty. Aegis resolves a variant id to its parent
model's `-c` when it sizes context.

Two engines, one memory pool: before a ComfyUI job starts, Aegis compares
the job's weights (+15% and 4GB) with the memory that is free right now and,
if the job will not fit, unloads llama-swap's GPU models first
(`comfyui_client.make_room`). They reload on the next message; during the
job the GPU guard answers chat on the CPU model anyway. Without this a 63GB
chat model plus a 71GB FLUX.2 render overcommits RAM and the box swaps.
`AEGIS_COMFY_MAKE_ROOM=0` turns it off.

## 8. Start Aegis

```
./start-linux.sh
```

The in-app engine watchdog starts and supervises llama-swap (with
`-watch-config`, so yaml edits hot-reload) and ComfyUI
(`main.py --listen 127.0.0.1 --port 8188 --disable-auto-launch`) - you do
not start them by hand. Per-machine ComfyUI flags go in `.env`; the watchdog
appends them every time it launches ComfyUI. On Strix Halo:

```
AEGIS_COMFYUI_ARGS="--disable-mmap --bf16-vae --cache-none"
AEGIS_COMFYUI_ENV=LD_PRELOAD=/opt/rocm/lib/libhsa-runtime64.so.1
```

`--disable-mmap` sidesteps the ROCm mmap problems on big safetensors,
`--bf16-vae` keeps VAEs out of fp32, `--cache-none` matches how Aegis already
frees ComfyUI after every job. The `LD_PRELOAD` line is for the stock
`rocm7.2` PyTorch wheels: they bundle ROCm 7.2.1's HSA runtime, whose event
thread busy-waits on current kernels, so an *idle* ComfyUI burns one CPU
core around the clock; preloading the system runtime (ROCm 7.2.4+) brings
idle back to 0%. `AEGIS_COMFYUI_ENV` only reaches the ComfyUI process.
(ComfyUI sets `TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1` itself.)

Under systemd, restarting `aegis-ui` also stops llama-swap and ComfyUI (they
live in the service's control group); the watchdog brings llama-swap back
within about a minute and ComfyUI within about four. For an always-on box, run the app under systemd - see
`aegis-ui.service` in the repo root for the unit to adapt (point it at
`start-linux.sh`, `User=` yourself, `WantedBy=default.target`).

In the app run `/engine` (context auto-sizing) and `/doctor` (browser,
voice, coding-agent health) to confirm everything is green.

## 9. What to expect (Ryzen AI Max+ 395, late-2026 numbers)

| Workload | Expectation |
|---|---|
| 30B-class A3B MoE Q4 chat | ~60 tok/s generation, ~1000 tok/s prompt |
| Dense 27-31B Q4 | ~11-12 tok/s (roughly 2x with MTP speculative decoding) |
| Image (sd.cpp Vulkan, 1024px) | ~10s per image (Flux-class) |
| Video (Wan2.2 / LTX via sd.cpp + ComfyUI) | Functional, markedly slower than the 4090 |
| TTS (Kokoro CPU) | 2-5x realtime |
| TTS (Chatterbox Turbo CPU) | ~2x realtime; needs `--tts-turbo` (a 4GB fetch) |
| TTS (standard Chatterbox / Qwen3 CPU) | Below realtime (0.57x / 0.2x) |
| STT (faster-whisper CPU int8) | Fine on 16 Zen 5 cores |

Measured 2026-11-22 on the MS-S1 MAX (cold start each time - Aegis frees the
media engine after every job, so model load is part of every render):

| Workload | Measured |
|---|---|
| Huihui 35B-A3B Q5_K (default chat) | 66 tok/s (61 with 8K in context) |
| Qwen3-Coder-Next 80B Q4_K_XL | 46 tok/s (44 at 8K) |
| Qwopus 122B-A10B Q4_K_M | 28 tok/s (27 at 8K) |
| The five 60-85GB models from the 2026-11 refresh | 23-51 tok/s, see the table in step 3 |
| Z-Image-Turbo, 1024px, 8 steps (ComfyUI) | 27 s |
| FLUX.2-dev Q8_0, 1024px, 20 steps (ComfyUI) | 5.7 min (3.5 min as `comfy-flux2-dev-turbo`, 8 steps) |
| Qwen-Image-2512 Q8_0, 1024px (sd.cpp Vulkan) | 4.0 min |
| Wan2.2 T2V Q8_0 + Lightning, 832x480, 49 frames (ComfyUI) | 2.9 min |
| Wan2.2 T2V Q8_0 + Lightning, 832x480, 33 frames (sd.cpp Vulkan) | 11.9 min |
| HunyuanVideo 1.5 T2V, 832x480, 49 frames, 20 steps cfg 6 (ComfyUI) | 19 min |

For video, prefer the ComfyUI workflows (`comfy-wan2.2-t2v`): ROCm PyTorch is
about twice as fast per denoising pass as sd.cpp on Vulkan here, and the
ComfyUI Lightning graph needs a quarter of the passes.

## 10. Feature notes (what changed vs. Windows/4090)

- GIMM-VFI frame interpolation is gone: it hard-depends on cupy-cuda12x,
  which has no ROCm build. ComfyUI-Frame-Interpolation (RIFE) replaces it
  and is installed by setup-engine.sh.
- fp8 "scaled" checkpoints (hunyuanvideo1.5, seedvr2, wan2.2 fp8, qwen VL
  fp8) still load: RDNA 3.5 has no native fp8 compute, so ComfyUI dequants
  to bf16 - works, slower, more memory (fine with 128GB). Where a
  full-precision or Q8_0 file is on disk the workflow builders pick it
  instead (Wan T2V: Q8_0 GGUF > fp8_scaled > Q3_K_M; HunyuanVideo: the
  full-precision Qwen2.5-VL text encoder and no load-time fp8 cast on a
  >=32GB GPU pool - `AEGIS_COMFY_BIG_GPU=0/1` overrides the detection).
- STT and Kokoro stay CPU: CTranslate2 has no ROCm backend. Not worth
  chasing - the 16-core Zen 5 handles both comfortably.
- Models that are fetched from Hugging Face on FIRST USE are not part of
  the models/ tree and do not migrate with it. Pre-fetch them while online
  or the feature fails the first time the network is down:
  `ResembleAI/chatterbox` (cloned-voice TTS, 3.2GB),
  `ResembleAI/chatterbox-turbo` (the faster cloned-voice model, 4GB; only
  ever fetched by `--tts-turbo` or by hand, never on first use),
  `Systran/faster-whisper-base` (local STT), and for the HQ foley op
  `google/siglip2-base-patch16-512` + `laion/larger_clap_general`. They land
  in `~/.cache/huggingface/hub` of the user the service runs as.
- CPU TTS with the standard models is slower than realtime here (Chatterbox
  ~0.45-0.57x, Qwen3-TTS ~0.2x, measured 2026-11-22). Chatterbox Turbo is
  about 2x realtime on the same processor (measured 2026-11-23) and is the
  fix if voice replies matter: `--tts-turbo` fetches it, and the chatterbox
  server uses it from then on (see docs/studio.md, "The faster model").
  `--tts-rocm` is the other way.
- Smart App Control / Defender workarounds, the Start Menu launcher, and
  the other Windows-only shims simply do not exist here.

### Offline reference library and photo identification

Kiwix collections (Wikipedia, medical references, Army manuals, Q&A archives)
plus a page-level index of the PDF manuals inside them, wired into web search
(the automatic fallback, and the first choice when the internet is down), an
agent tool (`library_search`), `/lib`, and photo identification (`/identify`,
BioCLIP species classifier + the vision model + library notes). Everything in
it works with no internet. Setup, collection choices and the measured accuracy
are in [offline-library.md](offline-library.md).

On this class of machine, measured: a search over 127 GB of Wikipedia takes
~30 ms; indexing 5,768 Army PDFs (155,100 pages) takes 30 s; a photo
identification takes ~18 s when the classifier is confident and ~35 s
otherwise, plus up to a minute the first time while the vision model loads.
`identify_model` is worth pointing at the fastest vision-capable model served
(here `qwen3.8-flash-next`, 28 tok/s, against 7 tok/s for the 40B vision model).

### Workbench: build, host, other machines

Agent mode can create or clone a project, deploy it as a systemd user service
with a tailnet HTTPS address, run commands on other machines over SSH and hand
work to Claude Code / Kimi / Codex / OpenCode / Aider (`manage_projects`,
`manage_apps`, `manage_hosts`, `delegate_agent`; `/project`, `/apps`, `/hosts`,
`/remote`). One-time host setup: `sudo loginctl enable-linger $USER` so the
services start at boot. Details, the access model and the security notes are
in [workbench.md](workbench.md).

## 11. Omarchy notes

- Omarchy ships Docker + Compose by default (but since 4.0.1 your user is
  NOT auto-added to the docker group - opt in yourself if you want it).
- Its rolling kernel (6.16+) covers the MS-S1 MAX's Realtek RTL8127 10GbE
  NICs out of the box; older-kernel distros need a DKMS driver.
- It is a desktop-first distro: fine as an Aegis host that doubles as a
  workstation, but there is no headless profile - for a pure always-on
  server, plain Arch or a server distro is the conventional pick.
- BIOS updates for the MS-S1 MAX are Windows-centric; a USB + EFI shell
  path exists and Linux boot entries survive it.

## 12. Troubleshooting

- `vulkaninfo --summary` fails: install `mesa vulkan-radeon vulkan-tools`
  and re-check. llama.cpp Vulkan will not start without a working ICD.
- "Permission denied" on /dev/kfd or /dev/dri: you are not in the
  `render`/`video` groups - `sudo usermod -aG video,render $USER`, re-login.
- Big model fails to load / OOM at ~64GB: the GTT params from step 2 are
  not active. Check
  `cat /sys/class/drm/card*/device/mem_info_gtt_total` - if it prints
  ~half your RAM, the modprobe conf is missing or initramfs was not
  rebuilt (`sudo mkinitcpio -P`, or `sudo limine-mkinitcpio` on Omarchy,
  then reboot).
- ROCm llama-server crashes or garbage on >64GB GGUFs: add `--no-mmap` to
  that entry (known ROCm mmap bug), or switch the build back to Vulkan.
- Every ComfyUI workflow is rejected with "value_not_in_list" on a model
  filename: the graph named a subfolder file with the wrong path separator.
  ComfyUI lists `video/x.safetensors` on Linux and `video\x.safetensors` on
  Windows and matches the string exactly - builders in
  `src/comfyui_workflows.py` must go through `_mp("video/x.safetensors")`.
- ComfyUI (or any ROCm PyTorch process) sits at 100% of one CPU core while
  idle: the wheel-bundled HSA runtime is busy-waiting. See the `LD_PRELOAD`
  note in step 8; `~/migrate/bench/hsa_idle_probe.py`-style checks (init the
  GPU, sleep, read per-thread CPU from /proc) confirm it in seconds.
- ComfyUI crash-loops at its first GPU operation: the venv has torch from the
  `rocm7.0` index. Reinstall from `rocm7.2`
  (`TORCH_INDEX_URL=https://download.pytorch.org/whl/rocm7.2`).
- ComfyUI NaN/black outputs: try `--force-fp32`; silent hangs on video
  workflows are a known occasional issue on gfx1151 - re-queue the job.
- Playwright Chromium will not launch: `sudo pacman -S --needed chromium`
  to pull the shared libs (Arch is not a Playwright `install-deps` target).
- Kokoro errors about espeak: `sudo pacman -S --needed espeak-ng`.
