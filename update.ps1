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

Write-Host ""
Write-Host "==> Done. Relaunch Portfolio _App to pick up the update."
