#!/bin/bash
# Install Aegis as a systemd service so it starts on boot and restarts on crash.
#
#   ./install-service.sh          # system-wide unit in /etc/systemd/system (sudo)
#   ./install-service.sh --user   # per-user unit in ~/.config/systemd/user (no sudo)
#
# Renders aegis-ui.service's __PLACEHOLDER__ tokens from THIS repo checkout, so
# run it from the clone you want to serve. Run ./start-linux.sh once first: it
# creates the venv, installs dependencies, and sets up the database the unit
# expects. Safe to re-run; it just reinstalls the unit.
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$REPO_DIR/aegis-ui.service"
UNIT_NAME="aegis-ui.service"

MODE="system"
case "${1:-}" in
    "")     ;;
    --user) MODE="user" ;;
    *)      echo "Usage: $0 [--user]"; exit 1 ;;
esac

if [ ! -f "$TEMPLATE" ]; then
    echo "Error: $TEMPLATE not found."
    exit 1
fi
if [ ! -x "$REPO_DIR/venv/bin/python" ]; then
    echo "Error: no venv at $REPO_DIR/venv."
    echo "Run ./start-linux.sh once first - it creates the venv, installs the"
    echo "Python packages, and sets up the database this service expects."
    exit 1
fi

# Engine passthrough: if the engine dir exists now (default: sibling of the
# repo, overridable with AEGIS_ENGINE_DIR), bake its path into the unit so the
# in-app engine watchdog finds llama-swap/ComfyUI when running headless.
ENGINE_DIR="${AEGIS_ENGINE_DIR:-$(dirname "$REPO_DIR")/engine}"
if [ -d "$ENGINE_DIR" ]; then
    ENGINE_SED="s|__ENGINE_LINE__|Environment=AEGIS_ENGINE_DIR=$ENGINE_DIR|"
else
    ENGINE_SED="/__ENGINE_LINE__/d"
fi

if [ "$MODE" = "system" ]; then
    # System units run as root unless told otherwise - run as the installing user.
    USER_SED="s|__USER_LINE__|User=$(id -un)|"
    WANTED_BY="multi-user.target"
else
    # User units always run as the owning user; a User= line is not allowed.
    USER_SED="/__USER_LINE__/d"
    WANTED_BY="default.target"
fi

RENDERED="$(mktemp)"
sed -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "$USER_SED" \
    -e "$ENGINE_SED" \
    -e "s|__WANTED_BY__|$WANTED_BY|" \
    "$TEMPLATE" > "$RENDERED"

echo "Installing $UNIT_NAME ($MODE mode) for $REPO_DIR ..."
if [ "$MODE" = "system" ]; then
    sudo cp "$RENDERED" "/etc/systemd/system/$UNIT_NAME"
    rm -f "$RENDERED"
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$UNIT_NAME"
    echo ""
    echo "Done. Aegis is now managed by systemd (default: http://127.0.0.1:7000)."
    echo "  status:  systemctl status $UNIT_NAME"
    echo "  logs:    journalctl -u $UNIT_NAME -f"
    echo "  stop:    sudo systemctl disable --now $UNIT_NAME"
else
    mkdir -p "$HOME/.config/systemd/user"
    cp "$RENDERED" "$HOME/.config/systemd/user/$UNIT_NAME"
    rm -f "$RENDERED"
    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT_NAME"
    echo ""
    echo "Done. Aegis is now managed by systemd (default: http://127.0.0.1:7000)."
    echo "  status:  systemctl --user status $UNIT_NAME"
    echo "  logs:    journalctl --user -u $UNIT_NAME -f"
    echo "  stop:    systemctl --user disable --now $UNIT_NAME"
    echo ""
    echo "Note: user units stop at logout unless lingering is enabled:"
    echo "  loginctl enable-linger $(id -un)"
fi
