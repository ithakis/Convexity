#!/usr/bin/env bash
# Bootstrap the Portfolio _App desktop app on macOS/Linux: install
# Miniforge if missing, create/update the `pt` conda env, generate the app
# icon, and install a native .app launcher (+ Desktop shortcut). Safe to
# re-run — every step is idempotent.
#
# ENV_NAME / APP_NAME are overridable (not documented to end users) so this
# script can be exercised against throwaway names during development without
# touching a real conda env or the real /Applications entry.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

ENV_NAME="${ENV_NAME:-pt}"
APP_NAME="${APP_NAME:-Portfolio _App}"
BUNDLE_ID="com.ithakis.portfoliotracker"

echo "==> Portfolio _App desktop app installer"
echo "    repo:    $REPO_DIR"
echo "    env:     $ENV_NAME"
echo "    app:     $APP_NAME"

# ---------------------------------------------------------------------------
# 1. Locate or install a conda/mamba base (Miniforge).
# ---------------------------------------------------------------------------
CONDA_SH=""
for candidate in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3"; do
    if [ -f "$candidate/etc/profile.d/conda.sh" ]; then
        CONDA_SH="$candidate/etc/profile.d/conda.sh"
        break
    fi
done

if [ -z "$CONDA_SH" ]; then
    echo "==> No conda/mamba installation found — installing Miniforge silently..."
    # Miniforge's release assets use "MacOSX"/"Linux", not uname -s's "Darwin"/"Linux".
    case "$(uname -s)" in
        Darwin) OS_NAME="MacOSX" ;;
        *) OS_NAME="$(uname -s)" ;;
    esac
    ARCH_NAME="$(uname -m)"
    INSTALLER="Miniforge3-${OS_NAME}-${ARCH_NAME}.sh"
    INSTALLER_URL="https://github.com/conda-forge/miniforge/releases/latest/download/${INSTALLER}"
    # The Miniforge installer script itself checks that its own $0 ends in
    # ".sh" (`echo "$0" | grep '\.sh$'`) and refuses to run otherwise — a
    # bare `mktemp -t miniforge-installer` doesn't produce a ".sh"-suffixed
    # name, so the installer would exit with "Please run using bash/dash/
    # sh/zsh, but not . or source." even though it WAS run via bash.
    # mktemp a directory (which we own and can fully clean up) and place a
    # properly-named file inside it, rather than appending a suffix to the
    # mktemp path directly (that would create a second, different path and
    # leave the original mktemp-created file behind, orphaned).
    INSTALLER_TMPDIR="$(mktemp -d -t miniforge)"
    TMP_INSTALLER="$INSTALLER_TMPDIR/installer.sh"
    curl -fsSL "$INSTALLER_URL" -o "$TMP_INSTALLER"
    bash "$TMP_INSTALLER" -b -p "$HOME/miniforge3"
    rm -rf "$INSTALLER_TMPDIR"
    CONDA_SH="$HOME/miniforge3/etc/profile.d/conda.sh"
fi

# shellcheck disable=SC1090
source "$CONDA_SH"
CONDA_BASE="$(conda info --base)"

if command -v mamba >/dev/null 2>&1; then
    SOLVER=mamba
else
    SOLVER=conda
fi
echo "==> Using $SOLVER (base: $CONDA_BASE)"

# ---------------------------------------------------------------------------
# 2. Create or update the env (idempotent).
# ---------------------------------------------------------------------------
if conda env list | awk '{print $1}' | grep -Fxq "$ENV_NAME"; then
    echo "==> Updating existing '$ENV_NAME' env..."
    "$SOLVER" env update -n "$ENV_NAME" -f environment.yml --prune
else
    echo "==> Creating '$ENV_NAME' env..."
    "$SOLVER" env create -n "$ENV_NAME" -f environment.yml
fi

ENV_PY="$CONDA_BASE/envs/$ENV_NAME/bin/python"
if [ ! -x "$ENV_PY" ]; then
    echo "ERROR: expected interpreter not found at $ENV_PY" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 3. Generate icon.icns from icon.png (macOS only; regenerate only if stale).
# ---------------------------------------------------------------------------
# icon.png is a transparent glyph (bars only, no background) and is packed
# into icon.icns as-is — no compositing, no added background color. macOS
# still masks every Dock/Finder icon into its own rounded-square/squircle
# shape (that's universal OS chrome applied to all apps, not something any
# app can opt out of), but the icon's own content stays exactly the source
# PNG: transparent everywhere except the candlestick bars themselves.
# (An earlier attempt flattened the glyph onto an opaque #0d1117 canvas to
# try to suppress a macOS-synthesized backing plate — that made things
# worse, adding a visible black square without actually removing the OS's
# own plate. Reverted; if a future agent revisits this, know that trick
# didn't work and don't reintroduce it without re-verifying on-device.)
ICON_PNG="$REPO_DIR/icon.png"
ICON_ICNS="$REPO_DIR/icon.icns"

if [ "$(uname -s)" = "Darwin" ] && command -v iconutil >/dev/null 2>&1; then
    if [ ! -f "$ICON_ICNS" ] || [ "$ICON_PNG" -nt "$ICON_ICNS" ]; then
        echo "==> Generating icon.icns..."
        ICONSET_TMPDIR="$(mktemp -d -t pticonset)"
        ICONSET_DIR="$ICONSET_TMPDIR/icon.iconset"
        mkdir -p "$ICONSET_DIR"
        for size in 16 32 128 256 512; do
            sips -z "$size" "$size" "$ICON_PNG" --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
            double=$((size * 2))
            sips -z "$double" "$double" "$ICON_PNG" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" >/dev/null
        done
        iconutil -c icns "$ICONSET_DIR" -o "$ICON_ICNS"
        rm -rf "$ICONSET_TMPDIR"
    fi
else
    echo "==> Skipping .icns generation (not macOS or iconutil unavailable)"
fi

# ---------------------------------------------------------------------------
# 4. Build the .app bundle (macOS only).
# ---------------------------------------------------------------------------
if [ "$(uname -s)" = "Darwin" ]; then
    if [ -w /Applications ]; then
        APPS_DIR="/Applications"
    else
        APPS_DIR="$HOME/Applications"
        mkdir -p "$APPS_DIR"
    fi
    APP_PATH="$APPS_DIR/$APP_NAME.app"

    echo "==> Installing $APP_PATH..."
    rm -rf "$APP_PATH"
    mkdir -p "$APP_PATH/Contents/MacOS" "$APP_PATH/Contents/Resources"

    cat > "$APP_PATH/Contents/MacOS/$APP_NAME" <<EOF
#!/bin/bash
cd "$REPO_DIR"
exec "$ENV_PY" -m portfolio_tracker.desktop
EOF
    chmod +x "$APP_PATH/Contents/MacOS/$APP_NAME"

    if [ -f "$ICON_ICNS" ]; then
        cp "$ICON_ICNS" "$APP_PATH/Contents/Resources/icon.icns"
    fi

    cat > "$APP_PATH/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>
    <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>
    <string>$BUNDLE_ID</string>
    <key>CFBundleExecutable</key>
    <string>$APP_NAME</string>
    <key>CFBundleIconFile</key>
    <string>icon.icns</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
</dict>
</plist>
EOF

    echo "==> Adding Desktop shortcut..."
    ln -sf "$APP_PATH" "$HOME/Desktop/$APP_NAME.app"

    echo ""
    echo "==> Done. Launch '$APP_NAME' from Launchpad, Spotlight, or your Desktop."
    echo "    (First launch may show an 'unidentified developer' prompt — this app"
    echo "    is unsigned by design; right-click > Open once to bypass it.)"
else
    echo ""
    echo "==> Done. Launch with: $ENV_PY -m portfolio_tracker.desktop"
fi
