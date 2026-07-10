#!/bin/bash
# Aegis — one-command quick start for Linux.
#
#   ./start-linux.sh
#
# Sets up a local Python environment, runs first-time setup (default login:
# admin / admin), starts a local ChromaDB for vector search, and launches the
# app — so you don't need to know anything about venvs, pip, or uvicorn.
# Safe to re-run; it skips work that's already done.
#
# Why native (not Docker): Cookbook serves models on whatever machine Aegis
# runs on. Running natively gives Cookbook direct access to your GPU without
# Docker's device plumbing. (Docker remains fully supported: docker compose up)
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

# Load .env so APP_PORT and APP_BIND are available without re-typing them on
# the command line every run — consistent with how app.py reads them via
# python-dotenv. Variables already set in the shell take priority over .env.
if [ -f .env ]; then
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${key// }" ]] && continue
        value="${value%%#*}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        [ -n "$key" ] && [ -z "${!key+x}" ] && export "$key=$value"
    done < .env
fi

# Shell overrides (AEGIS_PORT / AEGIS_HOST) take top priority, then .env
# values (APP_PORT / APP_BIND), then built-in defaults.
PORT="${AEGIS_PORT:-${APP_PORT:-7000}}"
HOST="${AEGIS_HOST:-${APP_BIND:-127.0.0.1}}" # Set APP_BIND=0.0.0.0 in .env for LAN/Tailscale access.
PROBE_HOST="$HOST"
if [ "$PROBE_HOST" = "0.0.0.0" ] || [ "$PROBE_HOST" = "::" ]; then
    PROBE_HOST="127.0.0.1"
fi

# Friendly message on any failure — re-running is safe (every step is idempotent).
trap 'echo; echo "✗ Setup failed above. It is safe to re-run ./start-linux.sh."; exit 1' ERR

echo "▶ Aegis quick start for Linux"

# Fail fast if the port is already taken (e.g. a previous run still running).
if (exec 3<>"/dev/tcp/$PROBE_HOST/$PORT") 2>/dev/null; then
    echo "✗ Port $PORT is already in use on $PROBE_HOST. Stop what's using it, or pick another port:"
    echo "    AEGIS_PORT=7900 ./start-linux.sh"
    exit 1
fi

# 1. Find a Python 3.11+ to build the environment with.
PY=""
for cand in python3 python3.14 python3.13 python3.12 python3.11; do
    p="$(command -v "$cand" 2>/dev/null)" || continue
    if "$p" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null; then
        PY="$p"; break
    fi
done
if [ -z "$PY" ]; then
    echo "✗ Couldn't find Python 3.11+ on PATH. Install it with your package manager, e.g.:"
    echo "    sudo apt install python3 python3-venv python3-pip     # Debian/Ubuntu"
    echo "    sudo dnf install python3                              # Fedora"
    echo "    sudo pacman -S python                                 # Arch"
    exit 1
fi
echo "  (using $("$PY" --version 2>&1) at $PY)"

# 2. Optional system dependencies — needed by Cookbook (local model serving),
#    not to boot the core app. Warn and keep going instead of aborting.
if ! command -v tmux >/dev/null 2>&1; then
    echo "  ⚠ tmux not found — Cookbook uses it for background model downloads/serves."
    echo "    Install later with: sudo apt install tmux  (or dnf / pacman)"
fi
if ! command -v git >/dev/null 2>&1 || ! command -v cmake >/dev/null 2>&1; then
    echo "  ⚠ git and/or cmake not found — Cookbook builds llama.cpp on demand and needs"
    echo "    them plus a C++ compiler. Install later with, e.g.:"
    echo "    sudo apt install git cmake build-essential"
fi

# 3. Python environment + dependencies (kept inside the repo, in venv/).
VENV_PY="./venv/bin/python3"
if [ ! -x "$VENV_PY" ] || ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    [ -d venv ] && { echo "▶ Existing venv is incomplete (no working pip) — rebuilding…"; rm -rf venv; }
    echo "▶ Creating Python environment…"
    if ! "$PY" -m venv venv; then
        echo "✗ venv creation failed. On Debian/Ubuntu the venv module is a separate package:"
        echo "    sudo apt install python3-venv"
        exit 1
    fi
fi
REQ_HASH="$(md5sum requirements.txt | cut -d' ' -f1)"
REQ_HASH_FILE="venv/.requirements_hash"
if [ ! -f "$REQ_HASH_FILE" ] || [ "$REQ_HASH" != "$(cat "$REQ_HASH_FILE" 2>/dev/null)" ]; then
  echo "▶ Installing Python packages (first run downloads a few — can take a few minutes)…"
  "$VENV_PY" -m pip install --quiet --upgrade pip
  # Not --quiet: this is the slow step, so show progress (and any real errors).
  "$VENV_PY" -m pip install -r requirements.txt
  echo "$REQ_HASH" > "$REQ_HASH_FILE"
else
  echo "▶ Python packages up to date — skipping install"
fi

# chromadb-client (HTTP-only) conflicts with the full chromadb package. Native
# runs start their own local ChromaDB server from the venv (below), which needs
# the full package — mirror start-macos.sh and swap it in.
if "$VENV_PY" -m pip show chromadb-client >/dev/null 2>&1; then
    echo "▶ Swapping chromadb-client for the full chromadb (local vector server)…"
    "$VENV_PY" -m pip uninstall -y chromadb-client
    "$VENV_PY" -m pip install --force-reinstall chromadb
fi

# 4. First-run setup: creates data dirs, the database, and the default
#    admin / admin login (idempotent — does nothing if already set up).
echo "▶ Preparing Aegis…"
AEGIS_SKIP_RUN_HINT=1 ./venv/bin/python setup.py

# 5. ChromaDB backs the tool index and vector RAG. Start a local server before
#    launching. Skip when one is already reachable, or when CHROMADB_HOST
#    points at a remote host.
CHROMA_PID=""
CHROMA_HOST="${CHROMADB_HOST:-localhost}"   # what the app connects to
CHROMA_PORT="${CHROMADB_PORT:-8100}"
CHROMA_BIN="$(dirname "$VENV_PY")/chroma"
case "$CHROMA_HOST" in
    localhost|127.0.0.1) CHROMA_BIND="127.0.0.1" ;;
    0.0.0.0)             CHROMA_BIND="0.0.0.0" ;;
    *)                   CHROMA_BIND="" ;;   # remote host - don't start locally
esac
if (exec 3<>"/dev/tcp/127.0.0.1/$CHROMA_PORT") 2>/dev/null; then
    echo "▶ ChromaDB already running on 127.0.0.1:$CHROMA_PORT - using it."
elif [ -z "$CHROMA_BIND" ]; then
    echo "▶ CHROMADB_HOST=$CHROMA_HOST is remote - not starting a local ChromaDB."
elif [ -x "$CHROMA_BIN" ]; then
    CHROMA_LOG="${TMPDIR:-/tmp}/aegis-chromadb.log"
    echo "▶ Starting ChromaDB in the background on $CHROMA_BIND:$CHROMA_PORT…"
    echo "  logging to $CHROMA_LOG"
    nohup "$CHROMA_BIN" run --host "$CHROMA_BIND" --port "$CHROMA_PORT" --path "$PWD/data/chroma" >"$CHROMA_LOG" 2>&1 &
    CHROMA_PID=$!
else
    echo "▶ ChromaDB CLI not found in venv; skipping (tool index will be degraded)."
fi

# 6. Launch. Bind to loopback by default; opt into LAN/Tailscale with
#    AEGIS_HOST=0.0.0.0.
URL_HOST="$HOST"
if [ "$URL_HOST" = "0.0.0.0" ] || [ "$URL_HOST" = "::" ]; then
    URL_HOST="127.0.0.1"
fi
URL="http://$URL_HOST:$PORT"
TAILSCALE_URL=""
if [ "$HOST" = "0.0.0.0" ] && command -v tailscale >/dev/null 2>&1; then
    TS_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
    if [ -n "$TS_IP" ]; then
        TAILSCALE_URL="http://$TS_IP:$PORT"
    fi
fi

# Open the browser automatically once the server is accepting connections.
# Skips headless boxes (no DISPLAY/WAYLAND) and honors AEGIS_NO_OPEN=1.
POLLER_PID=""
if [ -z "$AEGIS_NO_OPEN" ] && command -v xdg-open >/dev/null 2>&1 \
   && { [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]; }; then
    (
        for _ in $(seq 1 90); do
            if (exec 3<>"/dev/tcp/$PROBE_HOST/$PORT") 2>/dev/null; then
                printf '\n'
                printf '  ┌────────────────────────────────────────────┐\n'
                printf '  │  ✓ Aegis is ready — opening your browser    │\n'
                printf '  │     %-40s│\n' "$URL"
                printf '  │     (Press Ctrl+C in this window to stop)   │\n'
                printf '  └────────────────────────────────────────────┘\n\n'
                xdg-open "$URL" >/dev/null 2>&1 || true
                break
            fi
            sleep 1
        done
    ) &
    POLLER_PID=$!
fi

# Setup is done — drop the setup-failure handler, and clean up the background
# helpers when the server exits or the user presses Ctrl+C.
trap - ERR
trap '[ -n "$POLLER_PID" ] && kill "$POLLER_PID" 2>/dev/null; [ -n "$CHROMA_PID" ] && kill "$CHROMA_PID" 2>/dev/null' EXIT INT TERM

echo
echo "▶ Starting Aegis at $URL  (login: admin / admin)"
if [ -n "$TAILSCALE_URL" ]; then
    echo "  Tailscale/LAN URL: $TAILSCALE_URL"
fi
echo "  (this takes a few seconds; press Ctrl+C here to stop)"
echo
"$VENV_PY" -m uvicorn app:app --host "$HOST" --port "$PORT"
