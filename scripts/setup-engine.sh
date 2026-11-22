#!/usr/bin/env bash
# setup-engine.sh - builds Aegis's local inference engine on Linux (Arch-first,
# tuned for AMD Strix Halo / Radeon 8060S / Omarchy). The Linux counterpart of
# scripts/setup-engine.ps1: everything installs OUTSIDE the repo so it never
# enters git. Idempotent - already-present pieces are skipped unless --force.
#
# Layout produced (ENGINE_DIR defaults to <repo-parent>/engine):
#   ENGINE_DIR/llamacpp/llama-server (+ llama-cli, llama-quantize, llama-bench)
#   ENGINE_DIR/llama-swap/llama-swap
#   ENGINE_DIR/llama-swap.yaml            (config at ENGINE_DIR root)
#   ENGINE_DIR/sdcpp/sd-server (+ sd-cli)
#   ENGINE_DIR/comfyui/venv + ENGINE_DIR/comfyui/ComfyUI
#   ENGINE_DIR/chatterbox/{venv,server.py}
#   ENGINE_DIR/qwen3tts/{venv,server.py}
#   ENGINE_DIR/aider-venv
#   ENGINE_DIR/node/bin/npx
#
# Full guide: docs/engine-setup-linux.md

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths and defaults
# ---------------------------------------------------------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE_DIR="${AEGIS_ENGINE_DIR:-"$(dirname "$REPO_DIR")/engine"}"
MODELS_DIR="$REPO_DIR/models"

BACKEND="vulkan"                 # vulkan | rocm
COMFY_CPU=0
TTS_ROCM=0
SKIP_LLAMA=0
SKIP_SD=0
SKIP_COMFY=0
SKIP_TTS=0
SKIP_SPECIESID=0
SKIP_AIDER=0
SKIP_NODE=0
SKIP_BROWSER=0
NO_PACMAN=0
APPLY_GTT=0
FORCE=0

# rocm7.2, not rocm7.0: the HSA runtime bundled in the rocm7.0 wheels segfaults
# at the first GPU op on current kernels (ComfyUI then crash-loops under the
# Aegis watchdog). Override with TORCH_INDEX_URL=... if a newer index exists.
TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/rocm7.2}"
CPU_TORCH_INDEX_URL="https://download.pytorch.org/whl/cpu"
LLAMACPP_REF="${LLAMACPP_REF:-master}"
SDCPP_REF="${SDCPP_REF:-master}"

usage() {
  cat <<'EOF'
Usage: scripts/setup-engine.sh [options]

Builds the Aegis inference engine for Linux/AMD (Vulkan default, ROCm opt-in).

Options:
  --engine-dir DIR   Install location (default: <repo-parent>/engine; also
                     honors the AEGIS_ENGINE_DIR env var)
  --backend B        vulkan (default) or rocm - llama.cpp / sd.cpp GPU backend
  --comfy-cpu        ComfyUI venv gets CPU torch instead of $TORCH_INDEX_URL
  --tts-rocm         TTS venvs get torch from $TORCH_INDEX_URL (default: CPU)
  --skip-llama       Skip llama.cpp + llama-swap
  --skip-sd          Skip stable-diffusion.cpp
  --skip-comfy       Skip ComfyUI + custom nodes
  --skip-tts         Skip chatterbox + qwen3tts venvs
  --skip-speciesid   Skip the species-ID engine (BioCLIP, used by photo identification)
  --skip-aider       Skip the aider venv
  --skip-node        Skip portable Node.js
  --skip-browser     Skip the Playwright Chromium download
  --no-pacman        Do not install system packages (print them instead)
  --apply-gtt        Write /etc/modprobe.d/aegis-gtt.conf (sudo) for the
                     Strix Halo GTT memory limits; otherwise instructions
                     are printed only
  --force            Rebuild/reinstall pieces even when already present
  -h, --help         This help

Env overrides:
  AEGIS_ENGINE_DIR   Same as --engine-dir
  TORCH_INDEX_URL    pip index for GPU torch (default
                     https://download.pytorch.org/whl/rocm7.2)
  LLAMACPP_REF       git ref for ggml-org/llama.cpp (default master)
  SDCPP_REF          git ref for leejet/stable-diffusion.cpp (default master)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --engine-dir)   ENGINE_DIR="$2"; shift 2 ;;
    --backend)      BACKEND="$2"; shift 2 ;;
    --comfy-cpu)    COMFY_CPU=1; shift ;;
    --tts-rocm)     TTS_ROCM=1; shift ;;
    --skip-llama)   SKIP_LLAMA=1; shift ;;
    --skip-sd)      SKIP_SD=1; shift ;;
    --skip-comfy)   SKIP_COMFY=1; shift ;;
    --skip-tts)     SKIP_TTS=1; shift ;;
    --skip-speciesid) SKIP_SPECIESID=1; shift ;;
    --skip-aider)   SKIP_AIDER=1; shift ;;
    --skip-node)    SKIP_NODE=1; shift ;;
    --skip-browser) SKIP_BROWSER=1; shift ;;
    --no-pacman)    NO_PACMAN=1; shift ;;
    --apply-gtt)    APPLY_GTT=1; shift ;;
    --force)        FORCE=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

case "$BACKEND" in
  vulkan|rocm) ;;
  *) echo "--backend must be vulkan or rocm (got: $BACKEND)" >&2; exit 2 ;;
esac

BUILD_DIR="$ENGINE_DIR/.build"
NPROC="$(nproc 2>/dev/null || echo 4)"

banner() { printf '\n==> %s\n' "$1"; }
skip()   { printf '  - %s (already present, skipping - use --force to redo)\n' "$1"; }
warn()   { printf 'WARNING: %s\n' "$1" >&2; }

mkdir -p "$ENGINE_DIR" "$BUILD_DIR"
echo "Repo dir:   $REPO_DIR"
echo "Engine dir: $ENGINE_DIR"
echo "Models dir: $MODELS_DIR"
echo "Backend:    $BACKEND"

# ---------------------------------------------------------------------------
# 1. System packages (Arch / pacman)
# ---------------------------------------------------------------------------
# spirv-headers: the llama.cpp / sd.cpp Vulkan backends fail at cmake time without it.
# kiwix-tools / zim-tools / aria2: the offline reference library (docs/offline-library.md).
PACMAN_PKGS="base-devel cmake git curl python python-pip vulkan-icd-loader vulkan-headers vulkan-tools glslang shaderc spirv-headers espeak-ng ffmpeg kiwix-tools zim-tools aria2"
if [ "$BACKEND" = "rocm" ]; then
  PACMAN_PKGS="$PACMAN_PKGS rocm-hip-sdk rocminfo"
fi

banner "System packages"
if [ "$NO_PACMAN" -eq 1 ]; then
  echo "  --no-pacman: make sure these (or your distro's equivalents) exist:"
  echo "    $PACMAN_PKGS"
elif command -v pacman >/dev/null 2>&1; then
  # --needed makes this a no-op for packages already installed.
  sudo pacman -S --needed --noconfirm $PACMAN_PKGS
else
  warn "pacman not found - this script targets Arch/Omarchy. Install the"
  warn "equivalents yourself and re-run with --no-pacman. Rough mapping:"
  echo "    apt: build-essential cmake git curl python3 python3-pip python3-venv \\"
  echo "         libvulkan-dev vulkan-tools glslang-tools glslc espeak-ng ffmpeg"
  echo "    dnf: @development-tools cmake git curl python3 python3-pip \\"
  echo "         vulkan-loader-devel vulkan-tools glslang glslc espeak-ng ffmpeg"
  echo "  Continuing without system packages..."
fi

# ---------------------------------------------------------------------------
# 2. Environment checks (GPU stack + group membership)
# ---------------------------------------------------------------------------
banner "Environment checks"
if command -v vulkaninfo >/dev/null 2>&1 && vulkaninfo --summary >/dev/null 2>&1; then
  echo "  - vulkaninfo --summary: OK"
else
  warn "vulkaninfo --summary failed - the Vulkan ICD stack is not healthy."
  warn "On Arch install mesa + vulkan-radeon (RADV) and re-check:"
  warn "  sudo pacman -S --needed mesa vulkan-radeon vulkan-tools"
fi
for grp in video render; do
  if id -nG | tr ' ' '\n' | grep -qx "$grp"; then
    echo "  - user is in '$grp' group: OK"
  else
    warn "user is NOT in the '$grp' group - GPU access will fail. Fix with:"
    warn "  sudo usermod -aG $grp $USER   (then log out and back in)"
  fi
done

# ---------------------------------------------------------------------------
# 3. llama.cpp (built from source) + llama-swap (release binary)
# ---------------------------------------------------------------------------
if [ "$SKIP_LLAMA" -eq 1 ]; then
  banner "llama.cpp + llama-swap - skipped (--skip-llama)"
else
  banner "llama.cpp ($BACKEND, ref: $LLAMACPP_REF)"
  LLAMA_BIN_DIR="$ENGINE_DIR/llamacpp"
  if [ -x "$LLAMA_BIN_DIR/llama-server" ] && [ "$FORCE" -eq 0 ]; then
    skip "llama-server"
  else
    LLAMA_SRC="$BUILD_DIR/llama.cpp"
    if [ ! -d "$LLAMA_SRC/.git" ]; then
      git clone --depth 1 --branch "$LLAMACPP_REF" \
        https://github.com/ggml-org/llama.cpp "$LLAMA_SRC"
    else
      echo "  - reusing source checkout $LLAMA_SRC"
    fi
    if [ "$BACKEND" = "rocm" ]; then
      LLAMA_GPU_FLAGS="-DGGML_HIP=ON -DAMDGPU_TARGETS=gfx1151"
    else
      LLAMA_GPU_FLAGS="-DGGML_VULKAN=ON"
    fi
    # BUILD_SHARED_LIBS=OFF: the installed binaries must not depend on
    # libllama/libggml .so files left behind in $BUILD_DIR (a default shared
    # build installs 16KB launchers that break the moment .build is cleaned).
    cmake -S "$LLAMA_SRC" -B "$LLAMA_SRC/build" \
      $LLAMA_GPU_FLAGS -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF -DBUILD_SHARED_LIBS=OFF
    cmake --build "$LLAMA_SRC/build" --config Release -j "$NPROC" \
      --target llama-server llama-cli llama-quantize llama-bench
    mkdir -p "$LLAMA_BIN_DIR"
    for tool in llama-server llama-cli llama-quantize llama-bench; do
      install -m 755 "$LLAMA_SRC/build/bin/$tool" "$LLAMA_BIN_DIR/$tool"
    done
    echo "  - installed llama-server/cli/quantize/bench to $LLAMA_BIN_DIR"
  fi

  banner "llama-swap (linux_amd64 release)"
  SWAP_DIR="$ENGINE_DIR/llama-swap"
  if [ -x "$SWAP_DIR/llama-swap" ] && [ "$FORCE" -eq 0 ]; then
    skip "llama-swap"
  else
    # The release asset is named llama-swap_<ver>_linux_amd64.tar.gz, so the
    # stable releases/latest/download/ URL needs the real name - resolve it
    # from the GitHub API (curl + python, no jq dependency).
    SWAP_ASSET="$(curl -fsSL https://api.github.com/repos/mostlygeek/llama-swap/releases/latest \
      | python3 -c "import json,sys; r=json.load(sys.stdin); print(next(a['name'] for a in r['assets'] if a['name'].endswith('linux_amd64.tar.gz')))")"
    echo "  - downloading $SWAP_ASSET"
    mkdir -p "$SWAP_DIR"
    curl -fsSL -o "$BUILD_DIR/$SWAP_ASSET" \
      "https://github.com/mostlygeek/llama-swap/releases/latest/download/$SWAP_ASSET"
    tar -xzf "$BUILD_DIR/$SWAP_ASSET" -C "$SWAP_DIR" llama-swap
    chmod +x "$SWAP_DIR/llama-swap"
    rm -f "$BUILD_DIR/$SWAP_ASSET"
    echo "  - installed $SWAP_DIR/llama-swap"
  fi
fi

# ---------------------------------------------------------------------------
# 4. stable-diffusion.cpp (built from source - needs recent master for
#    sd-server, /v1/images/*, async vid_gen, dual-expert Wan, LTX-2.3)
# ---------------------------------------------------------------------------
if [ "$SKIP_SD" -eq 1 ]; then
  banner "stable-diffusion.cpp - skipped (--skip-sd)"
else
  banner "stable-diffusion.cpp ($BACKEND, ref: $SDCPP_REF)"
  SD_BIN_DIR="$ENGINE_DIR/sdcpp"
  if [ -x "$SD_BIN_DIR/sd-server" ] && [ "$FORCE" -eq 0 ]; then
    skip "sd-server"
  else
    SD_SRC="$BUILD_DIR/stable-diffusion.cpp"
    if [ ! -d "$SD_SRC/.git" ]; then
      git clone --depth 1 --branch "$SDCPP_REF" --recursive --shallow-submodules \
        https://github.com/leejet/stable-diffusion.cpp "$SD_SRC"
    else
      echo "  - reusing source checkout $SD_SRC"
    fi
    if [ "$BACKEND" = "rocm" ]; then
      SD_GPU_FLAGS="-DSD_HIPBLAS=ON -DAMDGPU_TARGETS=gfx1151"
    else
      SD_GPU_FLAGS="-DSD_VULKAN=ON"
    fi
    cmake -S "$SD_SRC" -B "$SD_SRC/build" \
      $SD_GPU_FLAGS -DCMAKE_BUILD_TYPE=Release
    cmake --build "$SD_SRC/build" --config Release -j "$NPROC"
    mkdir -p "$SD_BIN_DIR"
    SD_BUILT=0
    for tool in sd-server sd-cli sd; do
      src="$(find "$SD_SRC/build" -maxdepth 3 -type f -name "$tool" | head -n 1 || true)"
      if [ -n "$src" ]; then
        install -m 755 "$src" "$SD_BIN_DIR/$tool"
        echo "  - installed $SD_BIN_DIR/$tool"
        [ "$tool" = "sd-server" ] && SD_BUILT=1
      fi
    done
    if [ "$SD_BUILT" -eq 0 ]; then
      warn "sd-server was not produced - the checkout may be too old. Aegis"
      warn "needs recent master (sd-server + vid_gen API). Re-run with"
      warn "SDCPP_REF=master --force."
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 5. ComfyUI (second media engine) + custom nodes
# ---------------------------------------------------------------------------
if [ "$SKIP_COMFY" -eq 1 ]; then
  banner "ComfyUI - skipped (--skip-comfy)"
else
  banner "ComfyUI (torch: $([ "$COMFY_CPU" -eq 1 ] && echo cpu || echo "$TORCH_INDEX_URL"))"
  COMFY_DIR="$ENGINE_DIR/comfyui"
  COMFY_APP="$COMFY_DIR/ComfyUI"
  COMFY_PY="$COMFY_DIR/venv/bin/python"
  if [ ! -d "$COMFY_APP/.git" ]; then
    git clone https://github.com/comfyanonymous/ComfyUI "$COMFY_APP"
  else
    echo "  - reusing ComfyUI checkout $COMFY_APP"
  fi
  if [ -x "$COMFY_PY" ] && [ "$FORCE" -eq 0 ]; then
    skip "comfyui venv"
  else
    python3 -m venv "$COMFY_DIR/venv"
    "$COMFY_PY" -m pip install --quiet --upgrade pip
    if [ "$COMFY_CPU" -eq 1 ]; then
      "$COMFY_PY" -m pip install torch torchvision torchaudio \
        --index-url "$CPU_TORCH_INDEX_URL"
    else
      "$COMFY_PY" -m pip install torch torchvision torchaudio \
        --index-url "$TORCH_INDEX_URL"
    fi
    "$COMFY_PY" -m pip install -r "$COMFY_APP/requirements.txt"
  fi

  # Custom nodes. ComfyUI-GIMM-VFI is deliberately NOT here: it hard-depends
  # on cupy-cuda12x (no ROCm cupy) - ComfyUI-Frame-Interpolation's RIFE path
  # is the AMD replacement.
  CUSTOM_NODES="
https://github.com/city96/ComfyUI-GGUF
https://github.com/phazei/ComfyUI-HunyuanVideo-Foley
https://github.com/kijai/ComfyUI-MMAudio
https://github.com/kijai/ComfyUI-SUPIR
https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler
https://github.com/Enemyx-net/VibeVoice-ComfyUI
https://github.com/Fannovel16/ComfyUI-Frame-Interpolation
"
  for repo in $CUSTOM_NODES; do
    name="$(basename "$repo")"
    node_dir="$COMFY_APP/custom_nodes/$name"
    if [ -d "$node_dir" ] && [ "$FORCE" -eq 0 ]; then
      skip "custom node $name"
    else
      rm -rf "$node_dir"
      git clone --depth 1 "$repo" "$node_dir"
    fi
    req="$node_dir/requirements.txt"
    # Prefer a no-cupy requirements file when the node ships one (RIFE).
    [ -f "$node_dir/requirements-no-cupy.txt" ] && req="$node_dir/requirements-no-cupy.txt"
    if [ -f "$req" ]; then
      if grep -qi "cupy" "$req"; then
        warn "skipping $(basename "$req") for $name - it would pull cupy (CUDA-only)"
      else
        "$COMFY_PY" -m pip install --quiet -r "$req"
      fi
    fi
  done

  # Point ComfyUI at the shared models/ tree.
  EMP_TEMPLATE="$REPO_DIR/scripts/engine-templates/comfyui-extra-model-paths.yaml"
  EMP_TARGET="$COMFY_APP/extra_model_paths.yaml"
  if [ -f "$EMP_TARGET" ] && [ "$FORCE" -eq 0 ]; then
    skip "extra_model_paths.yaml"
  else
    sed "s|__ENGINE_DIR__|$ENGINE_DIR|g; s|__MODELS_DIR__|$MODELS_DIR|g" \
      "$EMP_TEMPLATE" > "$EMP_TARGET"
    echo "  - installed $EMP_TARGET (base_path: $MODELS_DIR)"
  fi
fi

# ---------------------------------------------------------------------------
# 6. TTS engines: chatterbox + qwen3tts (vendored server.py + venv each).
#    CPU torch by default - both servers auto-select cuda-vs-cpu at runtime,
#    and ROCm torch masquerades as cuda, so --tts-rocm needs no code change.
# ---------------------------------------------------------------------------
if [ "$SKIP_TTS" -eq 1 ]; then
  banner "TTS (chatterbox + qwen3tts) - skipped (--skip-tts)"
else
  if [ "$TTS_ROCM" -eq 1 ]; then
    TTS_TORCH_INDEX="$TORCH_INDEX_URL"
  else
    TTS_TORCH_INDEX="$CPU_TORCH_INDEX_URL"
  fi

  for tts in chatterbox qwen3tts; do
    banner "TTS: $tts (torch index: $TTS_TORCH_INDEX)"
    TTS_DIR="$ENGINE_DIR/$tts"
    TTS_PY="$TTS_DIR/venv/bin/python"
    mkdir -p "$TTS_DIR"
    if [ ! -f "$TTS_DIR/server.py" ] || [ "$FORCE" -eq 1 ]; then
      cp "$REPO_DIR/scripts/engine-templates/$tts-server.py" "$TTS_DIR/server.py"
      echo "  - installed $TTS_DIR/server.py (vendored template)"
    else
      skip "$tts/server.py"
    fi
    if [ -x "$TTS_PY" ] && [ "$FORCE" -eq 0 ]; then
      skip "$tts venv"
    else
      python3 -m venv "$TTS_DIR/venv"
      "$TTS_PY" -m pip install --quiet --upgrade pip
      "$TTS_PY" -m pip install torch torchaudio --index-url "$TTS_TORCH_INDEX"
      # Package lists match what each vendored server.py imports.
      if [ "$tts" = "chatterbox" ]; then
        # imports: torch, torchaudio, chatterbox.tts, fastapi, pydantic,
        # anyio, uvicorn
        "$TTS_PY" -m pip install chatterbox-tts fastapi uvicorn
      else
        # imports: torch, qwen_tts, numpy, soundfile, fastapi, pydantic,
        # anyio, uvicorn (qwen-tts pulls transformers/accelerate/onnxruntime)
        "$TTS_PY" -m pip install qwen-tts transformers accelerate soundfile \
          onnxruntime numpy fastapi uvicorn
      fi
    fi
  done
fi

# ---------------------------------------------------------------------------
# 6b. Species-ID engine (BioCLIP via pybioclip; photo identification).
#     CPU torch: one photo takes ~0.4 s, and it must never compete with the
#     chat model for GPU memory. The model + text embeddings (~5 GB) go to the
#     Hugging Face cache on --prefetch; after that the server loads offline.
# ---------------------------------------------------------------------------
if [ "$SKIP_SPECIESID" -eq 1 ]; then
  banner "Species-ID (BioCLIP) - skipped (--skip-speciesid)"
else
  banner "Species-ID: BioCLIP (torch index: $CPU_TORCH_INDEX_URL)"
  SID_DIR="$ENGINE_DIR/speciesid"
  SID_PY="$SID_DIR/venv/bin/python"
  mkdir -p "$SID_DIR"
  if [ ! -f "$SID_DIR/server.py" ] || [ "$FORCE" -eq 1 ]; then
    cp "$REPO_DIR/scripts/engine-templates/speciesid-server.py" "$SID_DIR/server.py"
    echo "  - installed $SID_DIR/server.py (vendored template)"
  else
    skip "speciesid/server.py"
  fi
  if [ -x "$SID_PY" ] && [ "$FORCE" -eq 0 ]; then
    skip "speciesid venv"
  else
    python3 -m venv "$SID_DIR/venv"
    "$SID_PY" -m pip install --quiet --upgrade pip
    "$SID_PY" -m pip install torch torchvision --index-url "$CPU_TORCH_INDEX_URL"
    # imports: bioclip (pulls open_clip_torch, pandas), fastapi, uvicorn,
    # python-multipart (file upload), PIL
    "$SID_PY" -m pip install pybioclip fastapi uvicorn python-multipart pillow
  fi
  # Non-fatal: with no internet right now the engine is installed but the
  # weights are not; run the same command again later.
  if "$SID_PY" "$SID_DIR/server.py" --prefetch; then
    echo "  - BioCLIP model + embeddings cached"
  else
    warn "Species-ID weights were NOT downloaded. Run once while online:"
    warn "  $SID_PY $SID_DIR/server.py --prefetch"
  fi
fi

# ---------------------------------------------------------------------------
# 7. Aider (coding agent, isolated venv)
# ---------------------------------------------------------------------------
if [ "$SKIP_AIDER" -eq 1 ]; then
  banner "Aider - skipped (--skip-aider)"
else
  banner "Aider (isolated venv)"
  AIDER_VENV="$ENGINE_DIR/aider-venv"
  if [ -x "$AIDER_VENV/bin/python" ] && [ "$FORCE" -eq 0 ]; then
    skip "aider-venv"
  else
    # aider-chat pins wheels that do not exist for the newest CPython (3.14
    # on current Arch). Prefer a uv-managed 3.12 venv when uv is available,
    # and never let this optional step abort the rest of the install.
    if command -v uv >/dev/null 2>&1; then
      uv venv --python 3.12 --seed "$AIDER_VENV" || python3 -m venv "$AIDER_VENV"
    else
      python3 -m venv "$AIDER_VENV"
    fi
    "$AIDER_VENV/bin/python" -m pip install --quiet --upgrade pip || true
    if ! "$AIDER_VENV/bin/python" -m pip install --quiet aider-chat; then
      warn "aider-chat did not install on $("$AIDER_VENV/bin/python" -V 2>&1)."
      warn "Install uv (sudo pacman -S uv) and re-run with --force, or skip"
      warn "it with --skip-aider. Continuing with the remaining steps."
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 8. Portable Node.js + Playwright MCP Chromium (browser automation)
# ---------------------------------------------------------------------------
if [ "$SKIP_NODE" -eq 1 ]; then
  banner "Node.js - skipped (--skip-node)"
else
  banner "portable Node.js (latest LTS, linux-x64)"
  NODE_DIR="$ENGINE_DIR/node"
  if [ -x "$NODE_DIR/bin/npx" ] && [ "$FORCE" -eq 0 ]; then
    skip "node/bin/npx"
  else
    NODE_VER="$(curl -fsSL https://nodejs.org/dist/index.json \
      | python3 -c "import json,sys; print(next(e['version'] for e in json.load(sys.stdin) if e.get('lts')))")"
    echo "  - downloading Node $NODE_VER"
    NODE_TAR="$BUILD_DIR/node-$NODE_VER-linux-x64.tar.xz"
    curl -fsSL -o "$NODE_TAR" \
      "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-x64.tar.xz"
    rm -rf "$NODE_DIR"
    mkdir -p "$NODE_DIR"
    tar -xJf "$NODE_TAR" -C "$NODE_DIR" --strip-components=1
    rm -f "$NODE_TAR"
    echo "  - installed $NODE_DIR/bin/node ($NODE_VER)"
  fi

  if [ "$SKIP_BROWSER" -eq 1 ]; then
    banner "Playwright Chromium - skipped (--skip-browser)"
  else
    banner "Playwright MCP + Chromium (browser)"
    PATH="$NODE_DIR/bin:$PATH" "$NODE_DIR/bin/npx" -y "@playwright/mcp@latest" \
      install-browser chromium
    echo "  Note: Playwright does not manage shared libs on Arch. If Chromium"
    echo "  fails to launch, install them via the system package:"
    echo "    sudo pacman -S --needed chromium"
  fi
fi

# ---------------------------------------------------------------------------
# 9. App-venv extras (voice STT, context tuner, Kokoro TTS)
# ---------------------------------------------------------------------------
banner "app-venv extras (faster-whisper gguf kokoro soundfile)"
APP_PY="$REPO_DIR/venv/bin/python"
if [ -x "$APP_PY" ]; then
  "$APP_PY" -m pip install --quiet faster-whisper gguf kokoro soundfile
  echo "  - installed into $REPO_DIR/venv"
else
  warn "app venv not found at $REPO_DIR/venv - create it first (see"
  warn "docs/engine-setup-linux.md), then run:"
  warn "  $REPO_DIR/venv/bin/pip install faster-whisper gguf kokoro soundfile"
fi

# ---------------------------------------------------------------------------
# 10. llama-swap.yaml (model serving config at ENGINE_DIR root)
# ---------------------------------------------------------------------------
banner "llama-swap config"
SWAP_CFG="$ENGINE_DIR/llama-swap.yaml"
SWAP_TEMPLATE="$REPO_DIR/docs/llama-swap.linux.yaml"
if [ -f "$SWAP_CFG" ] && [ "$FORCE" -eq 0 ]; then
  skip "llama-swap.yaml"
elif [ -f "$SWAP_TEMPLATE" ]; then
  sed "s|__ENGINE_DIR__|$ENGINE_DIR|g; s|__MODELS_DIR__|$MODELS_DIR|g" \
    "$SWAP_TEMPLATE" > "$SWAP_CFG"
  echo "  - installed $SWAP_CFG from docs/llama-swap.linux.yaml"
else
  echo "  docs/llama-swap.linux.yaml not found in this checkout - skipping."
  echo "  Pull the latest repo (the Linux template ships with the port) or"
  echo "  copy docs/llama-swap.example.yaml to $SWAP_CFG and edit paths."
fi

# ---------------------------------------------------------------------------
# 11. Strix Halo GTT / unified-memory kernel tuning
# ---------------------------------------------------------------------------
banner "GTT kernel tuning (Strix Halo unified memory)"
GTT_CONF="/etc/modprobe.d/aegis-gtt.conf"
GTT_LINE="options ttm pages_limit=31457280 page_pool_size=15728640"
# Omarchy boots a limine unified kernel image: plain `mkinitcpio -P` rebuilds
# an initramfs the bootloader never loads, so the ttm options would not apply.
if command -v limine-mkinitcpio >/dev/null 2>&1; then
  INITRAMFS_CMD="sudo limine-mkinitcpio"
else
  INITRAMFS_CMD="sudo mkinitcpio -P"
fi
if [ "$APPLY_GTT" -eq 1 ]; then
  echo "$GTT_LINE" | sudo tee "$GTT_CONF" >/dev/null
  echo "  - wrote $GTT_CONF"
  echo "  Now rebuild the initramfs and reboot:"
  echo "    $INITRAMFS_CMD && sudo reboot"
else
  echo "  Not applied (pass --apply-gtt to write it). To serve the 50-76GB"
  echo "  models fully GPU-resident, do this once:"
  echo "    echo '$GTT_LINE' | sudo tee $GTT_CONF"
  echo "    $INITRAMFS_CMD && sudo reboot"
fi
echo "  Set the BIOS UMA/VRAM carveout SMALL (512MB) in the SAME reboot - on Linux the GPU"
echo "  allocates from GTT, not the carveout. (pages_limit=31457280 is 120GiB"
echo "  and page_pool_size=15728640 is 60GiB, in 4KiB pages; amdgpu.gttsize"
echo "  is deprecated.)"
echo "  Verify after reboot:"
echo "    cat /sys/class/drm/card*/device/mem_info_gtt_total"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
banner "Engine setup complete"
cat <<EOF

Installed under $ENGINE_DIR:
  llamacpp/llama-server (+ cli/quantize/bench)   [$([ "$SKIP_LLAMA" -eq 1 ] && echo skipped || echo done)]
  llama-swap/llama-swap                          [$([ "$SKIP_LLAMA" -eq 1 ] && echo skipped || echo done)]
  sdcpp/sd-server                                [$([ "$SKIP_SD" -eq 1 ] && echo skipped || echo done)]
  comfyui/venv + comfyui/ComfyUI                 [$([ "$SKIP_COMFY" -eq 1 ] && echo skipped || echo done)]
  chatterbox/ + qwen3tts/ (server.py + venv)     [$([ "$SKIP_TTS" -eq 1 ] && echo skipped || echo done)]
  speciesid/ (server.py + venv)                  [$([ "$SKIP_SPECIESID" -eq 1 ] && echo skipped || echo done)]
  aider-venv/                                    [$([ "$SKIP_AIDER" -eq 1 ] && echo skipped || echo done)]
  node/bin/npx                                   [$([ "$SKIP_NODE" -eq 1 ] && echo skipped || echo done)]
  llama-swap.yaml                                [$([ -f "$SWAP_CFG" ] && echo present || echo MISSING)]

Next steps:
  1. Apply the GTT kernel params above (if not done) and reboot.
  2. rsync the models/ tree from the old box into $MODELS_DIR
     (see docs/engine-setup-linux.md for the exact rsync line).
  3. Review $SWAP_CFG - model aliases must match files in $MODELS_DIR.
  4. Start Aegis (./start-linux.sh) - the in-app watchdog starts llama-swap
     and ComfyUI automatically and restarts them if they die.
  5. In the app run /engine and /doctor to confirm everything is green.

Full guide: docs/engine-setup-linux.md
EOF
