#!/usr/bin/env bash
# Double-click this in Finder to launch the Portfolio Dashboard.
# It activates the QF12 conda env if present, otherwise falls back to system python3.

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

    echo "Stopping existing Portfolio Dashboard (PID $existing_pid)..."
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

if [ -f "$HOME/miniforge3/etc/profile.d/conda.sh" ]; then
    # shellcheck disable=SC1091
    source "$HOME/miniforge3/etc/profile.d/conda.sh"
    conda activate QF12 2>/dev/null || true
elif [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
    # shellcheck disable=SC1091
    source "$HOME/miniconda3/etc/profile.d/conda.sh"
    conda activate QF12 2>/dev/null || true
elif [ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]; then
    # shellcheck disable=SC1091
    source "$HOME/anaconda3/etc/profile.d/conda.sh"
    conda activate QF12 2>/dev/null || true
fi

# Make sure dependencies are present; install on first run if missing.
PIP_NEEDED=0
set +e
python -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(m) for m in ('yfinance','pandas','numpy')) else 1)"
[ $? -ne 0 ] && PIP_NEEDED=1
set -e

if [ "$PIP_NEEDED" = "1" ]; then
    echo "Installing missing dependencies (yfinance, pandas, numpy)…"
    python -m pip install --quiet yfinance pandas numpy
fi

python dashboard.py &
DASHBOARD_PID=$!
echo "$DASHBOARD_PID" > "$PID_FILE"
wait "$DASHBOARD_PID"
