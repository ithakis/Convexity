# Bootstrap the Portfolio _App desktop app on Windows: install Miniforge
# if missing, create/update the `pt` conda env, generate the app icon, and
# create Start Menu + Desktop shortcuts. Safe to re-run — every step is
# idempotent.
#
# NOTE: written to mirror install.sh 1:1 where the platforms allow, but has
# not been run on a real Windows machine (this repo was built on macOS) —
# see CLAUDE.md's Desktop app section for exactly what is untested.
#
# EnvName / AppName are overridable (not documented to end users) so this
# script can be exercised against throwaway names during development.

param(
    [string]$EnvName = "pt",
    [string]$AppName = "Portfolio _App"
)

$ErrorActionPreference = "Stop"

# $ErrorActionPreference only catches PowerShell-cmdlet errors, not a
# non-zero exit code from a native command (.bat/.exe) invoked via "&" —
# unlike bash's `set -e`. Call this after every external command so a
# failed env create/update or installer run actually stops the script
# instead of silently falling through to the icon/shortcut steps.
function Assert-Success {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: $Step failed (exit code $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoDir

Write-Host "==> Portfolio _App desktop app installer"
Write-Host "    repo:    $RepoDir"
Write-Host "    env:     $EnvName"
Write-Host "    app:     $AppName"

# ---------------------------------------------------------------------------
# 1. Locate or install a conda/mamba base (Miniforge).
# ---------------------------------------------------------------------------
$CondaBase = $null
foreach ($candidate in @("$env:USERPROFILE\miniforge3", "$env:USERPROFILE\miniconda3", "$env:USERPROFILE\anaconda3")) {
    if (Test-Path "$candidate\condabin\conda.bat") {
        $CondaBase = $candidate
        break
    }
}

if (-not $CondaBase) {
    Write-Host "==> No conda/mamba installation found — installing Miniforge silently..."
    $installerUrl = "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Windows-x86_64.exe"
    $installerPath = Join-Path $env:TEMP "Miniforge3-Windows-x86_64.exe"
    Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath
    $targetDir = "$env:USERPROFILE\miniforge3"
    # NSIS's /D= must be the last argument and must NOT be quoted, even when
    # the path contains spaces (a real risk here: $env:USERPROFILE contains
    # spaces for any "First Last"-style Windows account name). Passing a
    # single pre-joined string to -ArgumentList (not a string[]) avoids
    # PowerShell's per-element quoting, which would otherwise wrap the path
    # in quotes and break NSIS's parsing of /D=.
    $proc = Start-Process -FilePath $installerPath -ArgumentList "/S /D=$targetDir" -Wait -PassThru
    Remove-Item $installerPath -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) {
        Write-Host "ERROR: Miniforge installer failed (exit code $($proc.ExitCode))" -ForegroundColor Red
        exit $proc.ExitCode
    }
    $CondaBase = $targetDir
}

$CondaExe = "$CondaBase\condabin\conda.bat"
$MambaExe = "$CondaBase\condabin\mamba.bat"
if (Test-Path $MambaExe) {
    $Solver = $MambaExe
} else {
    $Solver = $CondaExe
}
Write-Host "==> Using $Solver (base: $CondaBase)"

# ---------------------------------------------------------------------------
# 2. Create or update the env (idempotent).
# ---------------------------------------------------------------------------
$EnvDir = "$CondaBase\envs\$EnvName"
if (Test-Path $EnvDir) {
    Write-Host "==> Updating existing '$EnvName' env..."
    & $Solver env update -n $EnvName -f environment.yml --prune
    Assert-Success "env update"
} else {
    Write-Host "==> Creating '$EnvName' env..."
    & $Solver env create -n $EnvName -f environment.yml
    Assert-Success "env create"
}

# Prove the env can actually import what the app needs, rather than trusting
# that the solver did what environment.yml asked. A half-solved env used to
# pass silently here and only surfaced weeks later as a dead ML model inside
# the running app. envcheck exits non-zero on a critical miss, and
# Assert-Success is required because PowerShell does not fail on a native
# command's exit code even under $ErrorActionPreference = "Stop".
$EnvPythonw = "$EnvDir\pythonw.exe"
$EnvPython = "$EnvDir\python.exe"
if (Test-Path $EnvPython) {
    Write-Host "==> Verifying runtime dependencies in '$EnvName'..."
    & $EnvPython -m portfolio_tracker.envcheck
    Assert-Success "dependency check"
}
if (-not (Test-Path $EnvPythonw)) {
    Write-Host "ERROR: expected interpreter not found at $EnvPythonw" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# 3. Generate icon.ico from icon.png (regenerate only if stale).
# ---------------------------------------------------------------------------
# icon.png is a transparent glyph (bars only, no background) and is packed
# into icon.ico as-is — no compositing. (An earlier version flattened it onto
# an opaque #0d1117 canvas to try to suppress a macOS Dock plate; that turned
# out not to fix the macOS issue and just added an unwanted background, so
# it was reverted on both platforms — see install.sh for the full story.)
$IconPng = Join-Path $RepoDir "icon.png"
$IconIco = Join-Path $RepoDir "icon.ico"

if ((-not (Test-Path $IconIco)) -or ((Get-Item $IconPng).LastWriteTime -gt (Get-Item $IconIco).LastWriteTime)) {
    Write-Host "==> Generating icon.ico..."
    $pyScript = @"
from PIL import Image
Image.open(r'$IconPng').save(r'$IconIco', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
"@
    $TmpPy = Join-Path $env:TEMP "pt_icon_gen.py"
    Set-Content -Path $TmpPy -Value $pyScript -Encoding UTF8
    & $EnvPython $TmpPy
    Assert-Success "icon.ico generation"
    Remove-Item -Force $TmpPy -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# 4. Create Start Menu + Desktop shortcuts.
# ---------------------------------------------------------------------------
# Shortcuts do NOT launch $EnvPythonw directly. A conda env's native deps
# (numpy/scipy/numba's MKL + llvmlite DLLs under envs\<name>\Library\bin)
# rely on the DLL search path that `conda activate` / `conda run` sets up.
# Without it, pythonw.exe starts fine but hard-crashes with no Python
# traceback (Windows Application-Error 0xc06d007f in KERNELBASE.dll) as
# soon as a background task exercises numba/scipy — reproduced directly on
# this machine, both via a bare shortcut-style launch and via
# `python.exe dashboard.py`; `conda run -n <env> ...` did not crash under
# the same load. Route the shortcut through `conda run` inside a hidden
# .vbs wrapper (WScript.Shell.Run with windowStyle 0) so there's no console
# flash for what's meant to be a GUI app.
$LauncherVbs = Join-Path $RepoDir "launch_desktop.vbs"
$CondaBatEscaped = $CondaExe -replace '"', '""'
$VbsContent = @"
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "$RepoDir"
shell.Run "cmd /c ""$CondaBatEscaped"" run -n $EnvName --no-capture-output pythonw -m portfolio_tracker.desktop", 0, False
"@
Set-Content -Path $LauncherVbs -Value $VbsContent -Encoding ASCII

$StartMenuDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
$DesktopDir = [Environment]::GetFolderPath("Desktop")

function New-AppShortcut {
    param([string]$Directory)
    $shortcutPath = Join-Path $Directory "$AppName.lnk"
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut($shortcutPath)
    $Shortcut.TargetPath = "$env:WINDIR\System32\wscript.exe"
    $Shortcut.Arguments = "`"$LauncherVbs`""
    $Shortcut.WorkingDirectory = $RepoDir
    if (Test-Path $IconIco) {
        $Shortcut.IconLocation = $IconIco
    }
    $Shortcut.Save()
}

Write-Host "==> Creating Start Menu shortcut..."
New-AppShortcut -Directory $StartMenuDir

Write-Host "==> Creating Desktop shortcut..."
New-AppShortcut -Directory $DesktopDir

Write-Host ""
Write-Host "==> Done. Launch '$AppName' from the Start Menu or your Desktop."
