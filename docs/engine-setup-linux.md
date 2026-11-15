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
sudo mkinitcpio -P
sudo reboot
```

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
- on ROCm add `--no-mmap` to entries whose GGUF exceeds 64GB

## 8. Start Aegis

```
./start-linux.sh
```

The in-app engine watchdog starts and supervises llama-swap (with
`-watch-config`, so yaml edits hot-reload) and ComfyUI
(`main.py --listen 127.0.0.1 --port 8188 --disable-auto-launch`) - you do
not start them by hand. On Strix Halo add
`--use-pytorch-cross-attention --disable-mmap` to the ComfyUI launch flags
(xformers/flash-attn are CUDA-only, and the ROCm mmap bug bites ComfyUI
too). For an always-on box, run the app under systemd - see
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
| TTS (Chatterbox/Qwen3 CPU) | Below realtime - use `--tts-rocm` if that hurts |
| STT (faster-whisper CPU int8) | Fine on 16 Zen 5 cores |

## 10. Feature notes (what changed vs. Windows/4090)

- GIMM-VFI frame interpolation is gone: it hard-depends on cupy-cuda12x,
  which has no ROCm build. ComfyUI-Frame-Interpolation (RIFE) replaces it
  and is installed by setup-engine.sh.
- fp8 "scaled" checkpoints (hunyuanvideo1.5, seedvr2, wan2.2 fp8, qwen VL
  fp8) still load: RDNA 3.5 has no native fp8 compute, so ComfyUI dequants
  to bf16 - works, slower, more memory (fine with 128GB).
- STT and Kokoro stay CPU: CTranslate2 has no ROCm backend. Not worth
  chasing - the 16-core Zen 5 handles both comfortably.
- Smart App Control / Defender workarounds, the Start Menu launcher, and
  the other Windows-only shims simply do not exist here.

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
  rebuilt (`sudo mkinitcpio -P`, reboot).
- ROCm llama-server crashes or garbage on >64GB GGUFs: add `--no-mmap` to
  that entry (known ROCm mmap bug), or switch the build back to Vulkan.
- ComfyUI NaN/black outputs: try `--force-fp32`; silent hangs on video
  workflows are a known occasional issue on gfx1151 - re-queue the job.
- Playwright Chromium will not launch: `sudo pacman -S --needed chromium`
  to pull the shared libs (Arch is not a Playwright `install-deps` target).
- Kokoro errors about espeak: `sudo pacman -S --needed espeak-ng`.
