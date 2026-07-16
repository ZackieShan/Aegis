#!/bin/sh
# Aegis one-line installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/ZackieShan/Aegis/main/install.sh | sh
#
# Gets the code and starts the app — the start script handles everything else
# (Homebrew deps on macOS, Python venv, first-run setup) and opens your
# browser when it's ready. Safe to re-run: it updates instead of re-installing.
# Override the install location with AEGIS_HOME=/path.
#
# POSIX sh on purpose: macOS ships an ancient bash and zsh-by-default; this
# runs identically under sh, bash, and zsh.

set -e

DEST="${AEGIS_HOME:-$HOME/Aegis}"

say() { printf '\n==> %s\n' "$1"; }

# git comes with Apple's Command Line Tools; the xcode-select prompt is the
# gentlest install path (a GUI dialog, no terminal wizardry).
if ! command -v git >/dev/null 2>&1; then
    say "git is needed first."
    if [ "$(uname -s)" = "Darwin" ]; then
        echo "    macOS will now offer to install its Command Line Tools (a normal"
        echo "    Apple dialog). Click Install, wait for it to finish, then run this"
        echo "    installer again."
        xcode-select --install 2>/dev/null || true
    else
        echo "    Install it with your package manager (e.g. sudo apt install git),"
        echo "    then run this installer again."
    fi
    exit 1
fi

if [ -d "$DEST/.git" ]; then
    say "Updating the existing install at $DEST"
    git -C "$DEST" pull --ff-only
else
    say "Downloading Aegis to $DEST"
    git clone --depth 1 https://github.com/ZackieShan/Aegis.git "$DEST"
fi

cd "$DEST"
case "$(uname -s)" in
    Darwin)
        say "Starting Aegis (first run sets everything up - give it a few minutes)"
        echo "    Next time, start it with:  cd $DEST && bash start-macos.sh"
        exec bash start-macos.sh
        ;;
    *)
        say "Starting Aegis (first run sets everything up - give it a few minutes)"
        echo "    Next time, start it with:  cd $DEST && bash start-linux.sh"
        exec bash start-linux.sh
        ;;
esac
