# Desktop app and installers

[← CLAUDE.md](../../CLAUDE.md) · part of the engineering notes; `§N` references name the original CLAUDE.md sections (map in CLAUDE.md).

## 14. Desktop app (PySide6 + QtWebEngine)

### Why this exists

Browser mode (`convexity`, formerly `python dashboard.py`) historically auto-launched Google Chrome
(removed in v1.12.3; it now only prints the URL), which failed on a machine
with no Chrome. The desktop app
wraps the *identical* HTTP server in a native window instead, using
QtWebEngine (which bundles its own Chromium — same rendering engine as
Chrome, so nothing about the frontend's behavior changes). Browser mode is
untouched and remains the documented fallback; this is purely additive.

**Why single-process, not Electron-style sidecar:** the HTTP server runs on
a background thread inside the same Python process as the Qt event loop
(see `start_server()` below). No subprocess, no IPC, no second language
runtime.

**Why a uv tool, not a packaged installer:** this is personal/friends-and-
family distribution. `install.sh` / `install.ps1` are the "build step"
(v1.14): install uv, `uv tool install` the package from a release, generate
the icon, and drop a launcher (`.app` on macOS, Start Menu/Desktop shortcuts
on Windows). No PyInstaller, no code signing. Updating is re-running the
installer (`update.sh` / `update.ps1` just do that). The server lives and dies
with the app process, so an update takes effect on the next launch. (Until
v1.13 this was a Miniforge `pt` conda env built from a checkout; that is
retired — see "Installers" below.)

### The `start_server()` / `shutdown_server()` seam (`src/convexity/server.py`)

`main()` (browser mode) and `desktop.py` (app mode) both need the same
port-pick + `ThreadingHTTPServer` construction but manage their own
lifecycle, so that piece is factored into two small functions:

- **`start_server() -> (server, port)`** — picks a port via the existing
  `_pick_port()`, constructs `ThreadingHTTPServer`, sets
  `server.daemon_threads = True`, and runs `serve_forever()` on a daemon
  background thread. Returns immediately (non-blocking) — this is the key
  change from the original code, where `serve_forever()` ran inline on the
  main thread. `daemon_threads = True` matters: `socketserver.ThreadingMixIn`
  defaults per-connection handler threads to **non-daemon**, which — without
  this — can block process exit on a stuck/long-lived connection (e.g. an
  open NDJSON stream) even after `shutdown()` + `server_close()`.
- **`shutdown_server(server)`** — `server.shutdown()`, `server.server_close()`,
  flush stdout, then **`os._exit(0)`**. The `os._exit()` is deliberate and
  non-obvious: `fetch_one`/analytics/`news_sentiment` all use module-level
  `concurrent.futures.ThreadPoolExecutor` pools, and `ThreadPoolExecutor`
  registers an **atexit hook that joins any in-flight work** before a normal
  interpreter shutdown can complete — verified directly (see "Gotchas
  discovered" below) that this can stall process exit for as long as the
  slowest pending network call takes. The existing browser-mode launcher
  (`Launch Dashboard.command`) never hit this because it always kills the
  process outright (SIGTERM's default disposition is an unclean, instant
  stop — no handler installed catches it, so it's no different from
  SIGKILL here); `os._exit()` gives the same guarantee from *inside* the
  process, which the desktop app needs since its quit path is triggered by
  Qt (`aboutToQuit`), not an external signal. The OS reclaims the socket and
  any WebEngine helper process either way (verified: an abruptly-killed
  parent's `QtWebEngineProcess` self-terminates within a few seconds via
  Chromium's own parent-death detection — this is normal, not a hang).
  `main()` (browser mode) calls `shutdown_server()` from its
  `finally:` block; `desktop.py` connects it to `app.aboutToQuit`.

`main()` itself now blocks on an interruptible `while True: time.sleep(1.0)`
loop instead of calling `serve_forever()` directly, since that call moved
into `start_server()` — Ctrl+C behavior is unchanged.

### `src/convexity/desktop.py`

Run via `python -m convexity.desktop` (what the installed launcher
actually invokes). Single file, ~160 lines:

- **Splash contract**: shown immediately via `QSplashScreen` (icon +
  "Convexity" + "Made by Alexander Tsoskounoglou 2026 · v{version_display}",
  colors matching the dashboard's own dark theme — `#0d1117`/`#e6edf3`/`#7d8590`
  from `style.css`). A `QElapsedTimer` starts the moment it's shown. The
  main window is revealed on `max(0, 6000ms - elapsed)` after the
  `QWebEngineView`'s `loadFinished` fires, via `reveal()` (guarded by a
  `nonlocal` flag so it only runs once). A 20s **safety timer** also calls
  `reveal()` unconditionally, so a stalled/failed page load can never trap
  the user on the splash forever. The subtitle's font is 12pt, sized down
  from an original 14pt specifically to keep the longer
  "v{version} (dd Mon yyyy)" string comfortably inside the 480px-wide
  splash pixmap — verified by rendering the real pixmap headlessly
  (`QT_QPA_PLATFORM=offscreen`) rather than guessing at text width.
- **External-link routing** (`_ExternalLinkPage`, a `QWebEnginePage`
  subclass): the dashboard has exactly two `target="_blank"` links —
  company website (`app.js` detail modal) and news article links (`app.js`
  news section) — both plain anchors, no `window.open()` calls. Chromium
  routes these through **either** `acceptNavigationRequest` (in-place
  navigation attempts) **or** `createWindow` (real new-window/tab
  requests), depending on how the click is dispatched, so both are
  overridden: `acceptNavigationRequest` blocks (`return False`) and hands
  off to `QDesktopServices.openUrl()` for any `NavigationTypeLinkClicked`
  whose host isn't our own loopback server; `createWindow` returns a
  throwaway `QWebEnginePage` whose first `urlChanged` triggers
  `QDesktopServices.openUrl()` then `deleteLater()` — no in-app popup
  window is ever shown. Verified directly against both code paths with
  `QDesktopServices.openUrl` mocked (not just by clicking through the UI).
- **Icon**: `src/convexity/assets/icon.png` — **package data** since v1.14 (it
  was at the repo root, which an installed package cannot reach, so a tool
  install would have run iconless). Resolved relative to `desktop.py`, set on
  both `QApplication` (dock/taskbar) and the window. Both call sites guard on
  `.exists()` — the app never crashes if the icon is missing, it just runs
  iconless. The installers read the same file through the tool's interpreter.
- **Shutdown**: `app.aboutToQuit.connect(lambda: shutdown_server(server))`.
  Verified via three independent paths: `app.quit()`, `window.close()`
  (which reaches `aboutToQuit` because Qt's `quitOnLastWindowClosed`
  defaults to `True`), and clicking the real close button through the
  Accessibility API — all three cleanly free the port immediately, even
  with an NDJSON stream actively in flight.

### Installers (`install.sh` / `packaging/install.ps1`, v1.14)

Since Phase 7 only `install.sh` is at the repo root — it is the curl target
(`raw.githubusercontent.com/ithakis/Convexity/main/install.sh`). The Windows
installer, both update wrappers and the dev launcher live in `packaging/`, so
the Windows one-liner is `irm …/main/packaging/install.ps1 | iex` (the old
`main/install.ps1` URL stops working once this reaches `main`).

One command on a clean account, and safe to re-run (that is the update):

1. **uv** — found on PATH or in `~/.local/bin` / `~/.cargo/bin`, else Astral's
   official installer (`curl -LsSf https://astral.sh/uv/install.sh | sh`,
   `irm https://astral.sh/uv/install.ps1 | iex`).
2. **libomp (macOS)** — see §4; checked *before* installing, never fatal.
3. **Source** — the latest GitHub release (`api.github.com/.../releases/latest`),
   installed from the tag's **source archive**
   (`github.com/ithakis/Convexity/archive/refs/tags/vX.Y.Z.tar.gz`), not
   `git+https`: a clean Mac has no git (`/usr/bin/git` is a shim that offers
   the Xcode tools), and uv would need it. Tags older than `MIN_TAG` (v1.14.0,
   the first with a pyproject) are refused with a clear message.
   `uv tool upgrade` cannot move a tag-pinned URL requirement, so updating
   re-runs `uv tool install --force` at the newest tag.
4. `uv tool install --force --python 3.11 "convexity[desktop] @ <src>"` —
   uv downloads a managed 3.11 when the machine has none.
5. **Verify** with the tool's own interpreter: `python -m convexity.envcheck`
   (fails the install on a critical miss) and `import lightgbm` (warning).
6. **Keys from an old checkout** — when the script runs from a checkout that
   has `.finnhub_key` / `.nvidia_key`, they are copied into the data folder's
   `config.json` (0600, never overwriting a key already there, values never
   printed). An installed package cannot find them by walking up from
   `site-packages`, so without this an upgrading user's News read went dark.
   Piped from curl there is no checkout and nothing to copy. `install.sh`
   looks beside itself (the repo root); `packaging/install.ps1` looks beside
   itself and in its parent, the repo root.
7. **Launcher** — macOS: `Convexity.app` (bundle ID `com.ithakis.convexity`,
   `CFBundleShortVersionString` = the installed version) whose executable is
   `exec "<uv tool dir>/convexity/bin/convexity-app"` — an absolute path,
   because LaunchServices starts bundles with a minimal PATH. The bundle and
   Desktop symlink are recreated on every run. Windows: Start Menu + Desktop
   `.lnk` straight to the tool's `Scripts\convexity-app.exe` (uv builds
   `[project.gui-scripts]` as a windowless launcher). The conda-era hidden
   `.vbs` + `conda run` wrapper is gone: it existed only because conda's
   native DLLs needed the activation DLL search path (a bare `pythonw.exe`
   hard-crashed with 0xc06d007f); PyPI wheels carry their own DLLs.

Undocumented overrides (how the scripts are tested without touching the real
install): `CONVEXITY_SOURCE` (local checkout or archive URL; `-Source`),
`CONVEXITY_VERSION` (tag; `-Version`), `APP_NAME` (`-AppName`),
`INSTALL_APPS_DIR`, `INSTALL_DESKTOP_DIR` (empty = no shortcut), plus uv's own
`UV_TOOL_DIR` / `UV_TOOL_BIN_DIR`. Prompts read `/dev/tty` so they work under
`curl | bash`; with no terminal they answer no.

### Icon generation

- **macOS** (`install.sh`): `sips -z` renders 16/32/128/256/512 (+@2x) PNGs
  from the unmodified transparent `icon.png` into a `.iconset`, then
  `iconutil -c icns` writes straight into the bundle's `Contents/Resources/`.
  Both tools are macOS built-ins. Nothing is written into a checkout.
  **Non-obvious**: `mktemp`'s printed path must be used directly — appending
  a suffix after the fact (e.g. `TMP=$(mktemp -t x).sh`) creates a *second*,
  different path and leaves the original mktemp-created file/dir behind as
  an orphaned empty temp file on every run (reproduced directly: an earlier
  version of this script did exactly that). Fix: `mktemp -d` once, place a
  properly-named file/dir *inside* it, `rm -rf` the directory afterward. (The
  conda-era Miniforge download hit the same mistake as a hard failure: its
  installer refuses to run unless its own path ends in `.sh`.)
- **Windows** (`install.ps1`): Pillow is not an app dependency, so it runs in
  a throwaway `uv run --no-project --with pillow` environment and writes a
  multi-resolution `.ico` (`sizes=[(16,16)…(256,256)]`) to
  `%LOCALAPPDATA%\Convexity\icon.ico` — app-owned, deliberately not the
  `%APPDATA%` user-data folder.

### Gotchas discovered during implementation

- **`ThreadingHTTPServer.daemon_threads` defaults to `False`.** Without
  `start_server()` setting it `True`, a per-connection handler thread stuck
  on a long-lived request can block process exit indefinitely even after
  `shutdown()` + `server_close()` — reproduced directly with an in-flight
  `/api/quotes-stream` request still open at shutdown time.
- **`concurrent.futures.ThreadPoolExecutor` registers an atexit hook that
  joins pending work.** This is why `shutdown_server()` calls `os._exit(0)`
  instead of letting the interpreter exit normally — reproduced in
  isolation (a bare `ThreadPoolExecutor` with one pending future blocks
  process exit for the full task duration; `os._exit(0)` bypasses it
  cleanly, confirmed the OS still reclaims the socket).
- **PowerShell's `$ErrorActionPreference = "Stop"` does not catch a
  non-zero exit code from a native command** (`.bat`/`.exe` invoked via
  `&`) the way bash's `set -e` does — reproduced directly (`& false`
  followed by more `Write-Host` calls executes them anyway, script exits
  0). `install.ps1` defines an `Assert-Success` helper and calls it after
  every native call (uv installer, `uv tool install`, `uv tool dir`, envcheck,
  the icon and key-copy Python calls) — otherwise a failed install would
  silently fall through to building shortcuts against a broken tool.
- **The Windows one-liner runs Windows PowerShell 5.1, not pwsh 7** — three
  consequences found while verifying 1.14, all invisible to pwsh-only testing:
  (1) under `$ErrorActionPreference = "Stop"`, *redirected* native stderr
  (`2>$null`, `2>&1`) becomes a terminating error in 5.1, so a failed
  `import lightgbm` probe would abort the install — `install.ps1` relaxes the
  preference around that one call; (2) `Invoke-RestMethod` may not offer TLS 1.2
  on older .NET, so the script ORs `Tls12` into `SecurityProtocol` (as uv's own
  installer does); (3) `[Uri]"<path>"` leaves `AbsoluteUri` empty for a Unix
  path, and `[System.Uri](Resolve-Path $x).Path` casts before `.Path` is read —
  use `[System.Uri]::new(<path>, [System.UriKind]::Absolute)`.
  `windows-install-smoke` therefore runs the installer with `shell: powershell`
  (5.1). To exercise `install.ps1` on macOS: `pwsh -File packaging/install.ps1 -Source .`
  with `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`/`CONVEXITY_HOME` in scratch gets through
  `uv tool install`; to reach the `.ico`, put a `uv` shim first on `PATH` that
  runs the real uv and, after `tool install`, symlinks `Scripts\python.exe` /
  `convexity-app.exe` to the tool's `bin/` (symlinking beforehand is useless:
  `--force` recreates the tool env), and set `LOCALAPPDATA`/`TEMP`; only the
  `WScript.Shell` COM step is Windows-only.
- **Blank window when launched from the .app bundle (ROOT-CAUSED & FIXED —
  `--single-process`).** The single most important desktop-app gotcha. When
  launched from the installed `.app` via LaunchServices (Finder / Dock /
  Spotlight / `open -a`), QtWebEngine's default **multi-process** Chromium
  cannot establish its Mojo IPC channel to the helper (network / renderer)
  processes. The browser process logs
  `mojo/core/channel_mac.cc ... mach_msg receive: (ipc/rcv) msg too large
  (0x10004004)`, every network request — including the initial
  `http://127.0.0.1:<port>/` page load — fails, `QWebEngineView.loadFinished`
  fires **`ok=False`**, and the window comes up **blank white**. This is what
  the earlier "intermittent invisible window / `Compositor returned null
  texture`" observation actually was: not a GPU/display quirk, but a failed
  page load rendering as blank. Root cause: a LaunchServices-launched process
  gets a **different Mach bootstrap namespace** than a shell child, so the
  Chromium helpers can't check back in. Verified exhaustively:
  - Terminal/shell launch of the *same* module → `ok=True`, renders. Bundle
    launch → `ok=False` **every time** (8/8), blank.
  - `urllib` (and `curl`) reach the server fine and the readiness probe gets
    a 200 — it is specifically Chromium's multi-process networking that
    breaks, not the server.
  - `--no-sandbox`, `--in-process-gpu`, `--disable-gpu` do **not** help
    (still `ok=False`). `--use-gl=swiftshader` crashes (SIGABRT — not
    available in this build). **Only `--single-process` fixes it** — it
    collapses renderer/network/GPU into the main process so there are no
    child processes to do Mach IPC with. Confirmed 8/8 bundle launches render
    and are fully interactive with it set.
  `desktop.py:_configure_chromium()` sets
  `QTWEBENGINE_CHROMIUM_FLAGS=--single-process` **before** `QApplication` is
  constructed (Qt reads it at web-engine init). Safe here: single-user,
  single-origin, fully-local content (external links are handed to the real
  browser), so the single-process downsides — no site isolation, a renderer
  crash takes the one window with it — don't apply. **Do not remove this
  flag.** If a future agent sees a blank window, check `loadFinished ok=` and
  the `channel_mac.cc mach_msg` line in `<data>/logs/desktop.log`
  / Chromium stderr before touching anything else.
- **Two robustness layers back up the flag** (in `desktop.py`): a **server
  readiness probe** in the boot worker (`urllib` GETs `/` until HTTP 200
  before the URL is handed to the view — kills the start_server-vs-first-
  request race that also produced `ok=False`), and a **load-retry** on
  `loadFinished(ok=False)` (up to 4 `view.reload()` attempts) with the
  min-splash / safety timer as the final backstop. Belt, braces, and a
  second belt — the app must open 100% of the time.
- **Dock icon before/after-launch mismatch (FIXED).** The old code called
  `app.setWindowIcon(QIcon("icon.png"))` unconditionally; on a bundle launch
  that *replaces* the Dock's bundle-`.icns` rendition (which gets macOS's
  standard rounded-tile treatment) with a raw PNG rendered differently — so
  the icon visibly changed the moment the app launched. Fix: `desktop.py`
  only calls `setWindowIcon` when **not** launched from the bundle (detected
  via `os.environ["__CFBundleIdentifier"] == BUNDLE_ID`); inside the bundle
  the `.icns` is left to win everywhere, so before/after are identical. The
  `.icns` is generated from `icon.png` by `install.sh` and matches it pixel-
  for-pixel (verified).
- **Startup speed / splash.** The splash (branded pixmap + a live **accent
  progress bar + braille spinner + percent**, mirroring the in-app
  `.progress-bar` / `.lc` loading-chip assets) is shown *before* the heavy
  import thread starts — starting the import first makes its GIL-heavy work
  contend with the main thread and visibly delays the splash. Two backend
  imports were made **lazy** to get `import convexity.server` off the
  startup path from ~6s down to ~1–3s (warm): `openai` (deferred into
  `news_sentiment._get_client()`, ~1.2s — the News read is on-demand) and
  `convexity.frontier`/`mpt`/`numba` (deferred to its two call sites
  in `server.py`, ~1.7s — the Optimize/MPT feature is on-demand). `pandas` +
  `yfinance` (~2.5s) stay eager since the first data render needs them. Net:
  the whole import now finishes inside the 6s minimum-splash window, so the
  perceived launch time is the intended 6s floor, not import time. The 6s is
  a **minimum** visible duration (deliberately longer than the boot itself
  needs, so there's always a moment to watch it); a slower
  boot keeps the splash up longer, and a 20s safety timer + boot-still-running
  re-arm guarantees the splash never traps the user.

### Windows verification status

This repo is developed on macOS; `install.ps1` has never run on a real
user's Windows machine. What is covered: PowerShell Core parses both scripts
in the lint job, and since v1.14 `windows-install-smoke` runs the **whole**
`install.ps1` on a real `windows-latest` runner — uv tool install, envcheck,
the Pillow `.ico` and real `WScript.Shell` shortcuts re-read and checked
(§13). The conda-era installer had a real Windows-only bug (NSIS `/D=`
quoting of the Miniforge install path) that only a real runner caught; that
code path is gone with conda.

**Still unverified** (needs a real end-user machine): that double-clicking
the shortcut gives a windowless launch (uv's GUI launcher should; never
observed by a human), first-launch SmartScreen/Defender prompts for the
unsigned app, the uv installer and `uv tool` paths on an account whose name
contains a space (the runner's account is `runneradmin`), and whether the
desktop window itself renders (CI never starts the GUI on Windows).
