# Install (or update) the Convexity desktop app on Windows:
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/ithakis/Convexity/main/install.ps1 | iex"
#
# Mirrors install.sh: installs uv if missing, `uv tool install`s
# convexity[desktop] from the latest GitHub release (the tag's source archive,
# so git is not required), verifies it with the tool's own interpreter, and
# creates Start Menu + Desktop shortcuts to convexity-app.exe. Safe to re-run:
# that is also how you update (see update.ps1). User data lives in
# %APPDATA%\Convexity and is never touched.
#
# NOTE: this repo is developed on macOS. CI runs this whole script on a real
# Windows runner (windows-install-smoke); see CLAUDE.md §14 for what has still
# never run on a real user's Windows machine.
#
# Undocumented overrides for development/testing (uv's UV_TOOL_DIR /
# UV_TOOL_BIN_DIR isolate the tool): -Version / $env:CONVEXITY_VERSION (a
# release tag), -Source / $env:CONVEXITY_SOURCE (a local checkout or an
# archive URL), -AppName.

param(
    [string]$Version = $env:CONVEXITY_VERSION,
    [string]$Source = $env:CONVEXITY_SOURCE,
    [string]$AppName = "Convexity"
)

$ErrorActionPreference = "Stop"

# Windows PowerShell 5.1 on older .NET may not offer TLS 1.2, which GitHub
# requires (uv's own installer does the same).
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo = "ithakis/Convexity"
$MinTag = "v1.14.0"   # first release with pyproject.toml
$PythonVersion = "3.11"

# $ErrorActionPreference only catches PowerShell-cmdlet errors, not a
# non-zero exit code from a native command (.exe) invoked via "&" — unlike
# bash's `set -e`. Call this after every external command so a failed install
# stops the script instead of silently falling through to the shortcuts.
function Assert-Success {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: $Step failed (exit code $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

Write-Host "==> Convexity installer"

# ---------------------------------------------------------------------------
# 1. uv
# ---------------------------------------------------------------------------
$Uv = $null
$cmd = Get-Command uv -ErrorAction SilentlyContinue
if ($cmd) { $Uv = $cmd.Source }
$UvCandidates = @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe")
if (-not $Uv) {
    foreach ($c in $UvCandidates) { if (Test-Path $c) { $Uv = $c; break } }
}
if (-not $Uv) {
    Write-Host "==> Installing uv (https://docs.astral.sh/uv/)..."
    & powershell -ExecutionPolicy ByPass -NoProfile -c "irm https://astral.sh/uv/install.ps1 | iex"
    Assert-Success "uv installer"
    foreach ($c in $UvCandidates) { if (Test-Path $c) { $Uv = $c; break } }
    if (-not $Uv) {
        Write-Host "ERROR: uv was installed but cannot be found; open a new terminal and re-run." -ForegroundColor Red
        exit 1
    }
}
$UvVersion = & $Uv --version
Assert-Success "uv --version"
Write-Host "==> uv: $Uv ($UvVersion)"

# ---------------------------------------------------------------------------
# 2. What to install
# ---------------------------------------------------------------------------
if ($Source) {
    if (Test-Path $Source -PathType Container) {
        $Src = [System.Uri]::new((Resolve-Path $Source).Path, [System.UriKind]::Absolute).AbsoluteUri
    } else {
        $Src = $Source
    }
    $Label = $Src
} else {
    $Tag = $Version
    if (-not $Tag) {
        try {
            $Tag = (Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest").tag_name
        } catch {
            Write-Host "ERROR: could not look up the latest release of $Repo (offline?)." -ForegroundColor Red
            exit 1
        }
    }
    if ([version]($Tag.TrimStart("v")) -lt [version]($MinTag.TrimStart("v"))) {
        Write-Host "ERROR: release $Tag predates the uv installer (needs $MinTag or newer)." -ForegroundColor Red
        exit 1
    }
    $Src = "https://github.com/$Repo/archive/refs/tags/$Tag.tar.gz"
    $Label = $Tag
}

Write-Host "==> Installing convexity[desktop] from $Label (Python $PythonVersion)..."
& $Uv tool install --force --python $PythonVersion "convexity[desktop] @ $Src"
Assert-Success "uv tool install"

$ToolRoot = & $Uv tool dir
Assert-Success "uv tool dir"
$ToolDir = Join-Path $ToolRoot "convexity"
$ToolPy = Join-Path $ToolDir "Scripts\python.exe"
$AppExe = Join-Path $ToolDir "Scripts\convexity-app.exe"
foreach ($p in @($ToolPy, $AppExe)) {
    if (-not (Test-Path $p)) {
        Write-Host "ERROR: expected file not found: $p" -ForegroundColor Red
        exit 1
    }
}

# ---------------------------------------------------------------------------
# 3. Verify with the interpreter that will run the app (the v1.10 lesson: a
#    declared dependency is not an installed one). envcheck exits non-zero on
#    a critical miss; the lightgbm import proves its native library loads.
# ---------------------------------------------------------------------------
Write-Host "==> Verifying runtime dependencies..."
& $ToolPy -m convexity.envcheck
Assert-Success "dependency check"
# Windows PowerShell 5.1 (what the one-liner runs) turns redirected native
# stderr into a terminating error under $ErrorActionPreference = "Stop", so a
# failed import would abort the install instead of warning. Relax it here only.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $ToolPy -c "import lightgbm" 2>&1 | Out-Null
$lgbmOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevEap
if (-not $lgbmOk) {
    Write-Host "WARNING: lightgbm does not load, so the Market read will be off." -ForegroundColor Yellow
}

$AppVersion = & $ToolPy -c "import convexity; print(convexity.__version__)"
Assert-Success "read version"
$IconPng = & $ToolPy -c "import convexity, pathlib; print(pathlib.Path(convexity.__file__).parent / 'assets' / 'icon.png')"
Assert-Success "locate icon"

# ---------------------------------------------------------------------------
# 4. API keys from an old checkout (.finnhub_key / .nvidia_key next to this
#    script) into the data folder's config.json. An installed package cannot
#    find them by walking up from its own folder. Never overwrites a key that
#    is already there; values are never printed.
# ---------------------------------------------------------------------------
$ScriptDir = $PSScriptRoot
if ($ScriptDir -and ((Test-Path (Join-Path $ScriptDir ".finnhub_key")) -or (Test-Path (Join-Path $ScriptDir ".nvidia_key")))) {
    $keyScript = @'
import json, os, sys
from pathlib import Path
from convexity import paths
src = Path(sys.argv[1])
cfg = paths.config_file()
try:
    data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
except (OSError, ValueError):
    print(f"==> {cfg} is not valid JSON; leaving it alone (keys not copied).")
    sys.exit(0)
if not isinstance(data, dict):
    sys.exit(0)
added = []
for fname, key in ((".finnhub_key", "finnhub_api_key"), (".nvidia_key", "nvidia_api_key")):
    f = src / fname
    if f.is_file() and not data.get(key):
        value = f.read_text(encoding="utf-8").strip()
        if value:
            data[key] = value
            added.append(key)
if added:
    cfg.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.with_name(cfg.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, cfg)
    print(f"==> Copied {', '.join(added)} into {cfg}")
'@
    $TmpKeyPy = Join-Path $env:TEMP "convexity_copy_keys.py"
    Set-Content -Path $TmpKeyPy -Value $keyScript -Encoding UTF8
    & $ToolPy $TmpKeyPy $ScriptDir
    $rc = $LASTEXITCODE
    Remove-Item -Force $TmpKeyPy -ErrorAction SilentlyContinue
    $global:LASTEXITCODE = $rc
    Assert-Success "copy API keys"
}

# ---------------------------------------------------------------------------
# 5. icon.ico from the packaged icon.png. Pillow is not an app dependency, so
#    it comes from a throwaway `uv run --with pillow` environment. The icon
#    lives in %LOCALAPPDATA%\Convexity (app-owned, not user data).
#    icon.png is a transparent glyph packed as-is — no background compositing
#    (see install.sh for why that was tried and reverted).
# ---------------------------------------------------------------------------
$IconDir = Join-Path $env:LOCALAPPDATA "Convexity"
New-Item -ItemType Directory -Force -Path $IconDir | Out-Null
$IconIco = Join-Path $IconDir "icon.ico"
Write-Host "==> Generating icon.ico..."
$TmpIconPy = Join-Path $env:TEMP "convexity_icon_gen.py"
Set-Content -Path $TmpIconPy -Encoding UTF8 -Value @"
from PIL import Image
Image.open(r'$IconPng').save(r'$IconIco', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
"@
& $Uv run --no-project --python $PythonVersion --with pillow python $TmpIconPy
Assert-Success "icon.ico generation"
Remove-Item -Force $TmpIconPy -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
# 6. Start Menu + Desktop shortcuts, straight to convexity-app.exe. uv builds
#    it as a windowless GUI launcher ([project.gui-scripts]). The conda-era
#    `conda run` .vbs wrapper existed only for conda's DLL search path; PyPI
#    wheels carry their own DLLs.
# ---------------------------------------------------------------------------
$StartMenuDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
$DesktopDir = [Environment]::GetFolderPath("Desktop")

function New-AppShortcut {
    param([string]$Directory)
    $shortcutPath = Join-Path $Directory "$AppName.lnk"
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut($shortcutPath)
    $Shortcut.TargetPath = $AppExe
    $Shortcut.WorkingDirectory = $env:USERPROFILE
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
Write-Host "==> Done (v$AppVersion). Launch '$AppName' from the Start Menu or your Desktop."
Write-Host "    If it was already running, quit it fully and open it again."
