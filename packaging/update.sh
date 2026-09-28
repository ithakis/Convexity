#!/usr/bin/env bash
# Update Convexity to the latest release. Updating IS re-running the installer:
# it reinstalls convexity[desktop] at the newest release tag (`uv tool upgrade`
# cannot move an install pinned to a tag's archive) and rebuilds the launcher.
# Your data folder is never touched.
#
# Installed with the curl one-liner and have no checkout? Re-run the one-liner.
# Working in a development checkout? `git pull && uv sync` instead.
set -euo pipefail

# install.sh stays at the repo root (it is the curl target); this wrapper
# lives in packaging/.
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/install.sh" "$@"
