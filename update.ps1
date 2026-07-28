# Update an existing Portfolio _App checkout: pull latest source, then
# sync the `pt` conda env to environment.yml. Run install.ps1 first if
# you've never set up the env/shortcuts on this machine.
#
# NOTE: mirrors update.sh; not run on a real Windows machine — see
# CLAUDE.md's Desktop app section.

param(
    [string]$EnvName = "pt"
)

$ErrorActionPreference = "Stop"

# $ErrorActionPreference doesn't catch a non-zero exit from a native
# command (unlike bash's `set -e`) — check explicitly so a failed pull or
# env update stops the script instead of silently continuing.
function Assert-Success {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: $Step failed (exit code $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoDir

Write-Host "==> Pulling latest changes..."
git pull --ff-only
Assert-Success "git pull"

$CondaBase = $null
foreach ($candidate in @("$env:USERPROFILE\miniforge3", "$env:USERPROFILE\miniconda3", "$env:USERPROFILE\anaconda3")) {
    if (Test-Path "$candidate\condabin\conda.bat") {
        $CondaBase = $candidate
        break
    }
}
if (-not $CondaBase) {
    Write-Host "ERROR: no conda/mamba installation found. Run install.ps1 first." -ForegroundColor Red
    exit 1
}

$MambaExe = "$CondaBase\condabin\mamba.bat"
$CondaExe = "$CondaBase\condabin\conda.bat"
if (Test-Path $MambaExe) {
    $Solver = $MambaExe
} else {
    $Solver = $CondaExe
}

Write-Host "==> Updating '$EnvName' env..."
& $Solver env update -n $EnvName -f environment.yml --prune
Assert-Success "env update"

# Verify rather than assume — this script is the documented fix for a stale
# env, so it must fail loudly if the sync did not actually deliver the
# packages instead of reporting success over a degraded app.
$EnvPython = "$CondaBase\envs\$EnvName\python.exe"
if (Test-Path $EnvPython) {
    Write-Host "==> Verifying runtime dependencies in '$EnvName'..."
    & $EnvPython -m portfolio_tracker.envcheck
    Assert-Success "dependency check"
} else {
    Write-Host "WARNING: interpreter not found at $EnvPython - skipping dependency check" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "==> Done. Fully quit and relaunch Portfolio _App to pick up the update."
Write-Host "    (A failed model load is cached for the process lifetime, so an"
Write-Host "     already-running app will not pick up new packages in place.)"
