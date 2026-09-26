#!/usr/bin/env bash
# Update an existing Convexity checkout: pull latest source, then
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

# Verify rather than assume. This script is the documented fix for a stale env,
# so it must fail loudly if the sync did not actually deliver the packages —
# otherwise it reports success and the app keeps running degraded, which is
# exactly how the ML model stayed dead through v1.10.0.
CONDA_BASE="$(conda info --base)"
ENV_PY="$CONDA_BASE/envs/$ENV_NAME/bin/python"
if [ -x "$ENV_PY" ]; then
    echo "==> Verifying runtime dependencies in '$ENV_NAME'..."
    "$ENV_PY" -m convexity.envcheck
else
    echo "WARNING: interpreter not found at $ENV_PY — skipping dependency check" >&2
fi

echo ""
echo "==> Done. Fully quit and relaunch Convexity to pick up the update."
echo "    (A failed model load is cached for the process lifetime, so an"
echo "     already-running app will not pick up new packages in place.)"
