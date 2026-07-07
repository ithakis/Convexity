#!/usr/bin/env bash
# Update an existing Portfolio Tracker checkout: pull latest source, then
# sync the `pt` conda env to environment.yml. Run install.sh first if you've
# never set up the env/app launcher on this machine.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

ENV_NAME="${ENV_NAME:-pt}"

echo "==> Pulling latest changes..."
git pull --ff-only

CONDA_SH=""
for candidate in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3"; do
    if [ -f "$candidate/etc/profile.d/conda.sh" ]; then
        CONDA_SH="$candidate/etc/profile.d/conda.sh"
        break
    fi
done
if [ -z "$CONDA_SH" ]; then
    echo "ERROR: no conda/mamba installation found. Run install.sh first." >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$CONDA_SH"

if command -v mamba >/dev/null 2>&1; then
    SOLVER=mamba
else
    SOLVER=conda
fi

echo "==> Updating '$ENV_NAME' env..."
"$SOLVER" env update -n "$ENV_NAME" -f environment.yml --prune

echo ""
echo "==> Done. Relaunch Portfolio Tracker to pick up the update."
