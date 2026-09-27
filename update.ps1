# Update Convexity to the latest release. Updating IS re-running the
# installer: it reinstalls convexity[desktop] at the newest release tag
# (`uv tool upgrade` cannot move an install pinned to a tag's archive) and
# recreates the shortcuts. Your data folder is never touched.
#
# Installed with the one-liner and have no checkout? Re-run the one-liner.
# Working in a development checkout? `git pull; uv sync` instead.

$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "install.ps1") @args
exit $LASTEXITCODE
