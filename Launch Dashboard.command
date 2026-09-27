#!/usr/bin/env bash
# Double-click this in Finder to run Convexity in browser mode from this
# development checkout: `uv run convexity` (uv syncs .venv from uv.lock first).
# It prints the URL to open. The installed app is Convexity.app (install.sh).

set -e

cd "$(dirname "$0")"

PID_FILE=".dashboard.pid"

cleanup() {
    rm -f "$PID_FILE"
}

stop_existing_dashboard() {
    local existing_pid="$1"

    if ! kill -0 "$existing_pid" 2>/dev/null; then
        return 0
    fi

    echo "Stopping existing Convexity (PID $existing_pid)..."
    kill "$existing_pid" 2>/dev/null || true

    for _ in {1..50}; do
        if ! kill -0 "$existing_pid" 2>/dev/null; then
            return 0
        fi
        sleep 0.1
    done

    echo "Existing dashboard did not exit cleanly; forcing stop."
    kill -9 "$existing_pid" 2>/dev/null || true
}

if [ -f "$PID_FILE" ]; then
    EXISTING_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
        stop_existing_dashboard "$EXISTING_PID"
    fi
    rm -f "$PID_FILE"
fi

trap cleanup EXIT

UV="$(command -v uv || true)"
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
if [ -z "$UV" ]; then
    echo "uv is not installed: https://docs.astral.sh/uv/ (or run ./install.sh)."
    exit 1
fi

# iCloud-synced ~/Documents flags every file under a dot-named folder
# UF_HIDDEN; Python then skips the editable .pth ("No module named
# 'convexity'") and Qt its plugins (CLAUDE.md §3). Clearing the flag does not
# last: iCloud re-flags ~20 files/s. A venv *created* under a ".nosync" name is
# left alone (renaming an existing one is not enough), so when .venv is a real
# folder that carries the flag, rebuild it once as .venv.nosync behind a .venv
# symlink. It is rebuilt from uv.lock, so nothing is lost.
if [ -d .venv ] && [ ! -L .venv ] \
        && [ -n "$(find .venv -flags +hidden -print -quit 2>/dev/null)" ]; then
    echo "Rebuilding .venv as .venv.nosync (iCloud keeps hiding its files)..."
    rm -rf .venv .venv.nosync
    ln -s .venv.nosync .venv
    UV_PROJECT_ENVIRONMENT=.venv.nosync "$UV" sync --extra dev --extra desktop
fi
# Belt and braces for any flag already set (trailing slash: follow the symlink).
[ -d .venv ] && chflags -R nohidden .venv/ 2>/dev/null || true

# uv forwards SIGTERM/SIGINT to the server, so the PID file logic above can
# stop it like before.
"$UV" run convexity &
DASHBOARD_PID=$!
echo "$DASHBOARD_PID" > "$PID_FILE"

wait "$DASHBOARD_PID"
