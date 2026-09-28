#!/usr/bin/env bash
# Install (or update) the Convexity desktop app on macOS/Linux:
#
#   curl -LsSf https://raw.githubusercontent.com/ithakis/Convexity/main/install.sh | bash
#
# 1. installs uv if missing (Astral's official installer);
# 2. `uv tool install`s convexity[desktop] from the LATEST GitHub release — the
#    tag's source archive, not `git+https`: a clean Mac has no git (the
#    /usr/bin/git shim only offers to install the Xcode tools);
# 3. verifies the install with the tool's own interpreter (envcheck + lightgbm);
# 4. macOS: builds Convexity.app whose launcher execs `convexity-app`.
# Safe to re-run: that is also how you update (see update.sh).
#
# Works piped from curl, so nothing here may depend on a checkout. User data
# lives in the per-user data folder (src/convexity/paths.py) and is never touched.
#
# Undocumented overrides, for development and for testing this script against
# throwaway locations (uv's own UV_TOOL_DIR / UV_TOOL_BIN_DIR isolate the tool):
#   CONVEXITY_VERSION   a release tag (vX.Y.Z) instead of the latest release
#   CONVEXITY_SOURCE    a local checkout or an archive URL instead of a release
#   APP_NAME            bundle name (default Convexity)
#   INSTALL_APPS_DIR    where the .app goes (default /Applications, else ~/Applications)
#   INSTALL_DESKTOP_DIR Desktop shortcut dir (default ~/Desktop; empty = none)
set -euo pipefail

REPO="ithakis/Convexity"
MIN_TAG="v1.14.0"   # first release with pyproject.toml; older tags cannot be installed
PYTHON_VERSION="3.11"
APP_NAME="${APP_NAME:-Convexity}"
BUNDLE_ID="com.ithakis.convexity"
OS="$(uname -s)"

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Answer a y/N question from the terminal even when stdin is the curl pipe.
# No terminal (CI, cron) => "no".
ask_yes() {
    local ans=""
    { printf '%s [y/N] ' "$1" > /dev/tty && read -r ans < /dev/tty; } 2>/dev/null || ans=""
    [[ "$ans" =~ ^[Yy] ]]
}

# vX.Y.Z >= vA.B.C ?
tag_at_least() {
    awk -v a="${1#v}" -v b="${2#v}" 'BEGIN {
        n = split(a, x, "."); split(b, y, ".")
        for (i = 1; i <= 3; i++) { if (x[i] + 0 > y[i] + 0) exit 0; if (x[i] + 0 < y[i] + 0) exit 1 }
        exit 0 }'
}

say "Convexity installer"

# ---------------------------------------------------------------------------
# 1. uv
# ---------------------------------------------------------------------------
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
    for c in "${XDG_BIN_HOME:-}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$c" ]; then UV="$c"; break; fi
    done
fi
if [ -z "$UV" ]; then
    say "Installing uv (https://docs.astral.sh/uv/)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    for c in "${XDG_BIN_HOME:-}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$c" ]; then UV="$c"; break; fi
    done
    [ -n "$UV" ] || die "uv was installed but cannot be found; open a new terminal and re-run."
fi
say "uv: $UV ($("$UV" --version))"

# ---------------------------------------------------------------------------
# 2. macOS: libomp. The PyPI lightgbm wheel loads @rpath/libomp.dylib from the
#    Homebrew / MacPorts locations only (CLAUDE.md §4); without it the Market
#    read is off. Everything else works, so this never aborts the install.
# ---------------------------------------------------------------------------
LIBOMP_OK=1
if [ "$OS" = "Darwin" ]; then
    LIBOMP_OK=0
    for d in /opt/homebrew/opt/libomp/lib /usr/local/opt/libomp/lib /opt/local/lib/libomp; do
        if [ -e "$d/libomp.dylib" ]; then LIBOMP_OK=1; break; fi
    done
    if [ "$LIBOMP_OK" = 0 ]; then
        warn "libomp is not installed. The Market read (LightGBM model) needs it."
        if command -v brew >/dev/null 2>&1; then
            if ask_yes "Run 'brew install libomp' now?"; then
                brew install libomp && LIBOMP_OK=1
            else
                warn "Skipped. Run 'brew install libomp' later, then restart Convexity."
            fi
        else
            warn "Install Homebrew (https://brew.sh), then run 'brew install libomp'."
            warn "The rest of the app works without it."
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 3. What to install
# ---------------------------------------------------------------------------
if [ -n "${CONVEXITY_SOURCE:-}" ]; then
    if [ -d "$CONVEXITY_SOURCE" ]; then
        SRC="file://$(cd "$CONVEXITY_SOURCE" && pwd)"
    else
        SRC="$CONVEXITY_SOURCE"
    fi
    LABEL="$SRC"
else
    TAG="${CONVEXITY_VERSION:-}"
    if [ -z "$TAG" ]; then
        TAG="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
            | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)" \
            || true
        [ -n "$TAG" ] || die "could not look up the latest release of $REPO (offline?)."
    fi
    tag_at_least "$TAG" "$MIN_TAG" \
        || die "release $TAG predates the uv installer (needs $MIN_TAG or newer)."
    SRC="https://github.com/$REPO/archive/refs/tags/$TAG.tar.gz"
    LABEL="$TAG"
fi

say "Installing convexity[desktop] from $LABEL (Python $PYTHON_VERSION)..."
# --force: replace an existing install (this is the update path) and take over
# the convexity / convexity-app commands.
"$UV" tool install --force --python "$PYTHON_VERSION" "convexity[desktop] @ $SRC"

TOOL_DIR="$("$UV" tool dir)/convexity"
TOOL_PY="$TOOL_DIR/bin/python"
APP_CMD="$TOOL_DIR/bin/convexity-app"
[ -x "$TOOL_PY" ] || die "expected interpreter not found at $TOOL_PY"
[ -x "$APP_CMD" ] || die "expected launcher not found at $APP_CMD"

# ---------------------------------------------------------------------------
# 4. Verify with the interpreter that will run the app, not the resolver's word
#    for it (the v1.10 lesson: a declared dependency is not an installed one).
#    envcheck exits non-zero on a critical miss. It uses find_spec, so it cannot
#    see a missing libomp — the lightgbm import below does.
# ---------------------------------------------------------------------------
say "Verifying runtime dependencies..."
"$TOOL_PY" -m convexity.envcheck
if ! "$TOOL_PY" -c "import lightgbm" >/dev/null 2>&1; then
    warn "lightgbm does not load, so the Market read will be off."
    [ "$OS" = "Darwin" ] && warn "Fix: brew install libomp, then restart Convexity."
fi

VERSION="$("$TOOL_PY" -c 'import convexity; print(convexity.__version__)')"
ICON_PNG="$("$TOOL_PY" -c 'import convexity, pathlib; print(pathlib.Path(convexity.__file__).parent / "assets" / "icon.png")')"

# ---------------------------------------------------------------------------
# 5. API keys from an old checkout. Before 1.14 the keys lived in .finnhub_key /
#    .nvidia_key next to the code, found by walking up from the package — which
#    an installed package can never reach. When this script runs from such a
#    checkout, copy them into the data folder's config.json (mode 0600). Never
#    overwrites a key already there; values are never printed.
# ---------------------------------------------------------------------------
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$SCRIPT_DIR" ] && { [ -f "$SCRIPT_DIR/.finnhub_key" ] || [ -f "$SCRIPT_DIR/.nvidia_key" ]; }; then
    "$TOOL_PY" - "$SCRIPT_DIR" <<'PY'
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
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, cfg)
    os.chmod(cfg, 0o600)
    print(f"==> Copied {', '.join(added)} into {cfg}")
PY
fi

# ---------------------------------------------------------------------------
# 6. macOS: Convexity.app
# ---------------------------------------------------------------------------
if [ "$OS" != "Darwin" ]; then
    echo ""
    say "Done (v$VERSION). Launch with: convexity-app   (browser mode: convexity)"
    say "If the command is not found, open a new terminal or run: $APP_CMD"
    exit 0
fi

if [ -n "${INSTALL_APPS_DIR:-}" ]; then
    APPS_DIR="$INSTALL_APPS_DIR"
elif [ -w /Applications ]; then
    APPS_DIR="/Applications"
else
    APPS_DIR="$HOME/Applications"
fi
mkdir -p "$APPS_DIR"
APP_PATH="$APPS_DIR/$APP_NAME.app"

say "Building $APP_PATH..."
rm -rf "$APP_PATH"
mkdir -p "$APP_PATH/Contents/MacOS" "$APP_PATH/Contents/Resources"

# icon.png is a transparent glyph (bars only) packed into the .icns as-is.
# macOS masks every Dock/Finder icon into its own squircle; that is OS chrome.
# (Flattening the glyph onto an opaque #0d1117 canvas was tried once to hide a
# synthesized backing plate — it only added a black square. Don't reintroduce
# it without re-verifying on-device.)
# mktemp -d, then real names inside it: appending a suffix to a mktemp path
# creates a second path and orphans the first (CLAUDE.md §14).
if [ -f "$ICON_PNG" ] && command -v iconutil >/dev/null 2>&1; then
    ICON_TMPDIR="$(mktemp -d -t convexity-icon)"
    ICONSET_DIR="$ICON_TMPDIR/icon.iconset"
    mkdir -p "$ICONSET_DIR"
    for size in 16 32 128 256 512; do
        sips -z "$size" "$size" "$ICON_PNG" --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
        double=$((size * 2))
        sips -z "$double" "$double" "$ICON_PNG" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICONSET_DIR" -o "$APP_PATH/Contents/Resources/icon.icns"
    rm -rf "$ICON_TMPDIR"
else
    warn "icon not generated (icon.png or iconutil missing); the app will use a generic icon."
fi

# Absolute path to the tool's own entry point: LaunchServices starts bundles
# with a minimal PATH, and this does not depend on ~/.local/bin being linked.
cat > "$APP_PATH/Contents/MacOS/$APP_NAME" <<EOF
#!/bin/bash
exec "$APP_CMD" "\$@"
EOF
chmod +x "$APP_PATH/Contents/MacOS/$APP_NAME"

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
    <string>$VERSION</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
</dict>
</plist>
EOF

DESKTOP_DIR="${INSTALL_DESKTOP_DIR-$HOME/Desktop}"
if [ -n "$DESKTOP_DIR" ] && [ -d "$DESKTOP_DIR" ]; then
    say "Adding Desktop shortcut..."
    ln -sfn "$APP_PATH" "$DESKTOP_DIR/$APP_NAME.app"
fi

echo ""
say "Done (v$VERSION). Launch '$APP_NAME' from Launchpad, Spotlight or your Desktop."
echo "    If it was already running, quit it fully and open it again."
echo "    The first launch may show an 'unidentified developer' prompt — the app"
echo "    is unsigned by design; right-click > Open once to allow it."
