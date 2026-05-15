#!/usr/bin/env bash
# Double-click this in Finder to launch the Portfolio Dashboard.
# It activates the QF12 conda env if present, otherwise falls back to system python3.

set -e

cd "$(dirname "$0")"

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

exec python dashboard.py
