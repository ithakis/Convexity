"""Desktop app entry point — native window wrapping the same HTTP server.

Runs the dashboard inside a PySide6 + QtWebEngine window instead of a
browser tab. QtWebEngine bundles its own Chromium (same rendering engine as
Chrome), so this works on a machine with no browser at all. The HTTP server
itself is untouched — see start_server()/shutdown_server() in server.py.

Startup is staged so the splash appears as fast as possible:

    t=0      python + PySide6 import (~1s — the unavoidable minimum; the
             QtWebEngine import MUST happen before QApplication is created,
             Qt enforces this)
    splash   shown immediately with a live progress bar
    worker   `import convexity.server` (pandas/yfinance/numba —
             ~2.5s+) + start_server() run on a background thread so the
             splash stays responsive and animated the whole time
    load     QWebEngineView.loadProgress drives the top of the bar
    reveal   at max(7s, actually ready) — 7s is the MINIMUM visible time
             (deliberately longer than the ~3s the boot itself needs, so
             there's always a moment to actually watch it); a slow boot
             keeps the splash up longer, and a 20s safety timer reveals
             unconditionally

Run via: python -m convexity.desktop
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

# QtWebEngineWidgets must be imported before QApplication is constructed —
# keep every PySide6 import at module top even though the splash would appear
# marginally sooner without them.
from PySide6.QtCore import QElapsedTimer, QRect, QRectF, Qt, QTimer, QUrl, QStandardPaths
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineDownloadRequest
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QSplashScreen

from convexity import __version_display__

# Package data (v1.14): the icon ships inside the wheel, so an installed
# `convexity-app` finds it without a checkout. icon.icns is optional there
# (gitignored); the installer puts its .icns in the .app bundle instead.
_ASSETS = Path(__file__).resolve().parent / "assets"
_ICON_PNG = _ASSETS / "icon.png"
_ICON_ICNS = _ASSETS / "icon.icns"
_BUNDLE_ID = "com.ithakis.convexity"
_MIN_SPLASH_MS = 6000
_SPLASH_SAFETY_MS = 20000
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}

# Page-zoom steps for the desktop window. Mirrors Chrome's own zoom ladder so
# stepping through it here feels identical to Cmd +/- in browser mode (which is
# native Chrome zoom and cannot be intercepted from JS, hence no counterpart in
# app.js). Deliberately NOT persisted — every launch starts at 100%.
_ZOOM_LADDER = (0.67, 0.75, 0.80, 0.90, 1.00, 1.10, 1.25, 1.50, 1.75, 2.00)
_ZOOM_DEFAULT_IDX = _ZOOM_LADDER.index(1.00)

# Dashboard theme (src/convexity/static/style.css [data-theme="dark"]) —
# the splash mirrors the app's own look, including the accent used by the
# in-app streaming progress bar.
_BG = "#0d1117"
_TEXT = "#e6edf3"
_MUTED = "#7d8590"
_ACCENT = "#2f81f7"
_TRACK = QColor(125, 125, 125, 41)  # rgba(125,125,125,0.16) — .lc-bar track

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

log = logging.getLogger("pt.desktop")


def _setup_logging() -> Path:
    """Timestamped boot log, overwritten each launch — the launcher runs
    windowless (no terminal), so this file is the only way to see what the
    app did and how long each boot stage took."""
    # <data>/logs/ on every OS (paths.py). It honours CONVEXITY_HOME, so a
    # dev/test launch can never truncate a running app's log (filemode="w").
    from convexity import paths

    log_path = paths.logs_dir() / "desktop.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_path),
        filemode="w",
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(message)s",
        datefmt="%H:%M:%S",
    )
    return log_path


def _load_app_icon() -> QIcon:
    # Prefer a multi-resolution .icns when one exists next to icon.png, so a
    # terminal launch gets the same rendition as Finder/Dock.
    if sys.platform == "darwin" and _ICON_ICNS.exists():
        return QIcon(str(_ICON_ICNS))
    if _ICON_PNG.exists():
        return QIcon(str(_ICON_PNG))
    return QIcon()


def _build_splash_pixmap(dpr: float) -> QPixmap:
    width, height = 480, 360
    pix = QPixmap(int(width * dpr), int(height * dpr))
    pix.setDevicePixelRatio(dpr)
    pix.fill(QColor(_BG))

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

    if _ICON_PNG.exists():
        icon_pix = QPixmap(str(_ICON_PNG)).scaled(
            int(160 * dpr),
            int(160 * dpr),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        icon_pix.setDevicePixelRatio(dpr)
        painter.drawPixmap(int((width - icon_pix.width() / dpr) / 2), 36, icon_pix)

    title_font = QFont()
    title_font.setPointSize(20)
    title_font.setBold(True)
    painter.setFont(title_font)
    painter.setPen(QColor(_TEXT))
    painter.drawText(QRect(0, 208, width, 40), Qt.AlignmentFlag.AlignCenter, "Convexity")

    sub_font = QFont()
    sub_font.setPointSize(12)
    painter.setFont(sub_font)
    painter.setPen(QColor(_MUTED))
    painter.drawText(
        QRect(0, 248, width, 30),
        Qt.AlignmentFlag.AlignCenter,
        f"Made by Alexander Tsoskounoglou 2026 · v{__version_display__}",
    )

    painter.end()
    return pix


class _Splash(QSplashScreen):
    """Splash with a live progress bar in the dashboard's own visual language
    (thin accent bar + braille spinner + tabular percent, like the in-app
    streaming progress bar / loading chip)."""

    def __init__(self, dpr: float):
        super().__init__(_build_splash_pixmap(dpr))
        self.progress = 0.0  # displayed value, eased toward .target each tick
        self.target = 0.02
        self.stage = "Starting"
        self._spin_frame = 0

    def tick(self) -> None:
        # Ease toward the current stage target; never move backwards. The
        # small floor keeps the bar visibly alive even inside a long stage.
        if self.progress < self.target:
            step = max(0.0008, (self.target - self.progress) * 0.055)
            self.progress = min(self.target, self.progress + step)
        self._spin_frame += 1
        self.update()

    def set_stage(self, stage: str, target: float) -> None:
        self.stage = stage
        self.target = max(self.target, target)

    def drawContents(self, painter: QPainter) -> None:  # noqa: N802 (Qt override)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bar_x, bar_y, bar_w, bar_h = 90, 300, 300, 5
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_TRACK)
        painter.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2.5, 2.5)
        fill_w = bar_w * self.progress
        if fill_w > 1:
            painter.setBrush(QColor(_ACCENT))
            painter.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2.5, 2.5)

        mono = QFont("Menlo" if sys.platform == "darwin" else "Consolas")
        mono.setPointSize(11)
        painter.setFont(mono)
        spinner = _SPINNER[(self._spin_frame // 3) % len(_SPINNER)]
        pct = f"{int(self.progress * 100):d}%"
        painter.setPen(QColor(_ACCENT))
        painter.drawText(QRect(bar_x, 314, 16, 20), Qt.AlignmentFlag.AlignLeft, spinner)
        painter.setPen(QColor(_MUTED))
        painter.drawText(
            QRect(bar_x + 18, 314, bar_w - 60, 20), Qt.AlignmentFlag.AlignLeft, self.stage
        )
        painter.setPen(QColor(_TEXT))
        painter.drawText(QRect(bar_x, 314, bar_w, 20), Qt.AlignmentFlag.AlignRight, pct)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # QSplashScreen hides itself on click by default — an impatient click
        # would make the app look like it vanished. Swallow it instead.
        event.accept()


class _ExternalLinkPage(QWebEnginePage):
    """Routes any navigation away from the local dashboard to the OS default
    browser instead of following it in-window. The dashboard has two
    target="_blank" links (company website, news articles) — Chromium's
    normal handling for those is either acceptNavigationRequest with
    NavigationTypeLinkClicked (in-place target changes) or createWindow
    (window.open / real new-window requests); we intercept both so neither
    path can accidentally navigate the main dashboard away or open a
    dangling native popup.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._popup_pages: list[QWebEnginePage] = []

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        is_link_click = nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked
        if is_link_click and url.host() not in _LOOPBACK_HOSTS:
            QDesktopServices.openUrl(url)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    def createWindow(self, _window_type):
        # A target="_blank"/window.open() request. Hand back a throwaway
        # page; once it knows its destination URL, open that externally and
        # discard the page (no in-app popup window is ever shown).
        popup = QWebEnginePage(self.profile(), self)
        self._popup_pages.append(popup)

        def _on_url_changed(url: QUrl) -> None:
            QDesktopServices.openUrl(url)
            popup.deleteLater()
            if popup in self._popup_pages:
                self._popup_pages.remove(popup)

        popup.urlChanged.connect(_on_url_changed)
        return popup


def _handle_download(download: "QWebEngineDownloadRequest", window: "_MainWindow") -> None:
    # QWebEngineProfile.downloadRequested is auto-cancelled (silently — no
    # error, no file) unless a connected slot calls accept(). The Export
    # button (server.py's /api/export-xlsx, sent with a Content-Disposition
    # attachment header) relies on exactly this signal, so without a handler
    # here it looked like the button did nothing. downloadFileName() already
    # carries the server's suggested name; we just redirect the directory to
    # the OS Downloads folder (Chromium's own default is a fixed internal
    # path, not necessarily where a user would look).
    downloads_dir = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.DownloadLocation
    )
    if downloads_dir:
        download.setDownloadDirectory(downloads_dir)
    download.accept()

    def _on_state_changed(state) -> None:
        if state == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
            path = str(Path(download.downloadDirectory()) / download.downloadFileName())
            log.info("download completed: %s", path)
            window.statusBar().showMessage(f"Saved: {path}", 8000)
        elif state == QWebEngineDownloadRequest.DownloadState.DownloadInterrupted:
            log.warning("download interrupted: %s", download.downloadFileName())
            window.statusBar().showMessage(f"Download failed: {download.downloadFileName()}", 8000)

    download.stateChanged.connect(_on_state_changed)


class _MainWindow(QMainWindow):
    def __init__(self, url: str):
        super().__init__()
        self.setWindowTitle(f"Convexity v{__version_display__}")
        self.resize(1440, 900)
        self._url = url

        self.view = QWebEngineView(self)
        self.page = _ExternalLinkPage(self.view)
        self.view.setPage(self.page)
        self.setCentralWidget(self.view)
        self.page.profile().downloadRequested.connect(
            lambda download: _handle_download(download, self)
        )
        self._zoom_idx = _ZOOM_DEFAULT_IDX
        self._install_zoom_shortcuts()
        # NB: deliberately does NOT load here — the caller connects
        # loadProgress/loadFinished first, then calls load(), so a load
        # that finishes early can't slip past before its handler is wired.

    def _install_zoom_shortcuts(self) -> None:
        """Wire Cmd/Ctrl +/-/0 page zoom.

        QWebEngineView ships no zoom shortcuts of its own, so without this the
        desktop app has no way to zoom at all (browser mode gets Chrome's own
        for free).

        The extra "Ctrl+=" binding is the non-obvious part: QKeySequence's
        StandardKey.ZoomIn resolves to Cmd/Ctrl++, but on a US/most layouts "+"
        is Shift+"=", so the key people actually press to zoom in is Cmd+= with
        no shift. Every browser binds both; so do we. Qt maps Ctrl to Cmd on
        macOS automatically, so these read correctly on both platforms.
        """
        for key in (QKeySequence.StandardKey.ZoomIn, QKeySequence("Ctrl+=")):
            QShortcut(key, self, activated=lambda: self._step_zoom(1))
        QShortcut(QKeySequence.StandardKey.ZoomOut, self, activated=lambda: self._step_zoom(-1))
        QShortcut(QKeySequence("Ctrl+0"), self, activated=self._reset_zoom)

    def _apply_zoom(self) -> None:
        self.view.setZoomFactor(_ZOOM_LADDER[self._zoom_idx])

    def _step_zoom(self, delta: int) -> None:
        self._zoom_idx = max(0, min(len(_ZOOM_LADDER) - 1, self._zoom_idx + delta))
        self._apply_zoom()

    def _reset_zoom(self) -> None:
        self._zoom_idx = _ZOOM_DEFAULT_IDX
        self._apply_zoom()

    def load(self) -> None:
        self.view.load(QUrl(self._url))


def _configure_chromium() -> None:
    """Force QtWebEngine into single-process mode.

    Why this is mandatory (and not a perf micro-opt): when the app is launched
    from the macOS .app bundle via LaunchServices (Finder/Dock/Spotlight), the
    multi-process Chromium that QtWebEngine normally spins up cannot establish
    its Mojo IPC channel to the helper (network/renderer) processes — the
    browser process logs `mojo/core/channel_mac.cc ... mach_msg receive:
    (ipc/rcv) msg too large` and every network request (including the initial
    http://127.0.0.1/ page load) fails, so QWebEngineView.loadFinished fires
    ok=False and the window comes up blank. This does NOT happen when the same
    Python module is run straight from a shell, because a bundled process gets
    a different Mach bootstrap namespace than a shell child — the helper
    processes can't check back in. Verified directly: bundle launch fails
    ok=False every time; --no-sandbox / --in-process-gpu / --disable-gpu do
    NOT help (still ok=False); --single-process fixes it 100% of the time.

    Single-process collapses the renderer/network/GPU work into the main
    process, so there are no child processes to do Mach IPC with. For a
    single-user, single-origin, fully-local dashboard (the only web content is
    our own 127.0.0.1 server; external links are handed off to the real
    browser) the usual single-process downsides — no site isolation, a
    renderer crash takes the window with it — don't matter.
    """
    existing = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if "--single-process" not in existing:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (existing + " --single-process").strip()


def main() -> None:
    t0 = time.monotonic()
    log_path = _setup_logging()
    _configure_chromium()

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication([])
    app.setApplicationName("Convexity")
    log.info("qt ready (+%.2fs) log=%s", time.monotonic() - t0, log_path)

    boot: dict = {"server": None, "port": None, "shutdown": None, "error": None, "done": False}

    def _boot_worker() -> None:
        # `import convexity.server` drags in pandas/yfinance — the
        # single slowest boot step. Run it off the GUI thread so the splash
        # (already on screen by the time this starts) stays responsive; the
        # anim timer polls for completion (no cross-thread Qt calls — the
        # worker only writes plain fields).
        try:
            from convexity.server import shutdown_server, start_server

            log.info("server module imported (+%.2fs)", time.monotonic() - t0)
            server, port = start_server()
            boot["shutdown"] = shutdown_server
            boot["server"] = server
            # Probe until the server actually serves a 200 before we hand the
            # URL to QtWebEngine. start_server() returns as soon as the socket
            # is bound, but the very first request can race the serve_forever
            # accept loop — a QWebEngineView.load() that loses that race fires
            # loadFinished(ok=False) and shows a blank page. A cheap readiness
            # probe here makes the first in-view load deterministic.
            url = f"http://127.0.0.1:{port}/"
            deadline = time.monotonic() + 8.0
            ready = False
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(url, timeout=1.0) as resp:
                        if resp.status == 200:
                            ready = True
                            break
                except Exception:
                    time.sleep(0.05)
            boot["port"] = port
            if ready:
                log.info("server ready (200) on %d (+%.2fs)", port, time.monotonic() - t0)
            else:
                # Deadline hit without a confirmed 200 — proceed anyway (the
                # load-retry + safety timer are the backstop) but don't claim
                # readiness we never observed.
                log.warning(
                    "server readiness probe timed out on %d (+%.2fs); "
                    "proceeding, relying on load-retry",
                    port,
                    time.monotonic() - t0,
                )
        except Exception:
            boot["error"] = traceback.format_exc()
            log.error("boot failed:\n%s", boot["error"])
        finally:
            boot["done"] = True

    icon = _load_app_icon()
    log.info("icon loaded (+%.2fs)", time.monotonic() - t0)
    # When launched from the installed .app, macOS already shows the bundle's
    # icon.icns in the Dock; overriding it at runtime replaces that with a
    # raw image that the Dock renders differently (no bundle icon treatment),
    # which is exactly the before/after-launch mismatch we're avoiding. Only
    # set the application icon when there is no bundle to provide one
    # (terminal launches, Windows, Linux).
    launched_from_bundle = (
        sys.platform == "darwin" and os.environ.get("__CFBundleIdentifier") == _BUNDLE_ID
    )
    if not launched_from_bundle:
        app.setWindowIcon(icon)
    log.info("launched_from_bundle=%s", launched_from_bundle)

    dpr = app.primaryScreen().devicePixelRatio() if app.primaryScreen() else 1.0
    splash = _Splash(dpr)
    log.info("splash constructed (+%.2fs)", time.monotonic() - t0)
    splash.show()
    app.processEvents()
    log.info("splash visible (+%.2fs)", time.monotonic() - t0)

    # Start the heavy import ONLY now that the splash is painted — kicking it
    # off earlier makes its GIL-heavy import contend with the main thread and
    # visibly delays the splash from appearing. The import (~2.5s) still
    # finishes well inside the 7s minimum-splash window, so nothing is lost.
    elapsed = QElapsedTimer()
    elapsed.start()
    splash.set_stage("Loading market data engine", 0.55)
    threading.Thread(target=_boot_worker, name="pt-boot", daemon=True).start()

    state = {
        "window": None,
        "revealed": False,
        "reveal_scheduled": False,
        "load_tries": 0,
        "load_ok": False,
    }
    _MAX_LOAD_TRIES = 4

    def reveal() -> None:
        if state["revealed"] or state["window"] is None:
            return
        state["revealed"] = True
        # The splash's only job is done — stop the 33ms animation timer so it
        # doesn't keep waking the CPU (and ticking a hidden splash) for the
        # entire lifetime of the app.
        anim.stop()
        window = state["window"]
        # Full screen only when the page actually rendered. The failure
        # reveals (retry-exhausted, 20s safety timer) keep a normal window so
        # a blank page still has visible chrome — macOS native fullscreen
        # hides the title bar and close button, which would trap the user in
        # an empty fullscreen Space with no obvious way out.
        if state["load_ok"]:
            window.showFullScreen()
        else:
            window.show()
        window.raise_()
        window.activateWindow()
        splash.finish(window)
        log.info("window revealed (+%.2fs)", time.monotonic() - t0)

        def _expose_check() -> None:
            handle = window.windowHandle()
            exposed = bool(handle and handle.isExposed())
            log.info("expose check: visible=%s exposed=%s", window.isVisible(), exposed)

        QTimer.singleShot(1500, _expose_check)

    def schedule_reveal() -> None:
        if state["reveal_scheduled"]:
            return
        state["reveal_scheduled"] = True
        # 7s is the MINIMUM the splash stays up (so there's always a moment
        # to actually watch it); if the app is ready sooner we still wait it
        # out, if it takes longer the splash stays until it's ready.
        remaining = max(0, _MIN_SPLASH_MS - elapsed.elapsed())
        log.info("ready — revealing in %dms (min-splash rule)", remaining)
        QTimer.singleShot(remaining, reveal)

    def on_load_progress(p: int) -> None:
        splash.set_stage("Rendering dashboard", 0.62 + 0.38 * (p / 100.0))

    def on_load_finished(ok: bool) -> None:
        log.info(
            "page loadFinished ok=%s try=%d (+%.2fs)",
            ok,
            state["load_tries"],
            time.monotonic() - t0,
        )
        if ok:
            state["load_ok"] = True
            splash.set_stage("Ready", 1.0)
            schedule_reveal()
            return
        # A failed load must never surface as a blank window. Retry the
        # navigation a few times (the server is confirmed-ready by the boot
        # probe, so this covers transient QtWebEngine first-navigation
        # hiccups), then fall back to revealing so the safety net still holds.
        if state["load_tries"] < _MAX_LOAD_TRIES and state["window"] is not None:
            state["load_tries"] += 1
            delay = 250 * state["load_tries"]
            log.warning(
                "load failed — retry %d/%d in %dms", state["load_tries"], _MAX_LOAD_TRIES, delay
            )
            splash.set_stage("Retrying", splash.target)
            # Re-issue the explicit URL load rather than view.reload(): after a
            # failed first navigation the view may have no committed URL, so
            # reload() could no-op.
            QTimer.singleShot(delay, lambda: state["window"].load())
        else:
            log.error("load still failing after %d tries — revealing anyway", state["load_tries"])
            schedule_reveal()

    def poll() -> None:
        splash.tick()
        if state["window"] is None and boot["done"]:
            if boot["error"] is not None:
                anim.stop()
                splash.hide()
                QMessageBox.critical(
                    None,
                    "Convexity",
                    "The app failed to start.\n\nDetails were written to:\n" + str(log_path),
                )
                os._exit(1)
            splash.set_stage("Starting dashboard", 0.62)
            window = _MainWindow(f"http://127.0.0.1:{boot['port']}/")
            window.setWindowIcon(icon)
            window.view.loadProgress.connect(on_load_progress)
            window.view.loadFinished.connect(on_load_finished)
            state["window"] = window
            window.load()  # start loading only after the signals are wired
            log.info("window created, page loading (+%.2fs)", time.monotonic() - t0)

    anim = QTimer()
    anim.setInterval(33)
    anim.timeout.connect(poll)
    anim.start()

    def safety_reveal() -> None:
        # Never trap the user on the splash: if the page load stalls (or
        # loadFinished never fires), reveal whatever we have. If the boot
        # thread itself is still running there is no window to show yet —
        # keep the splash and check again shortly rather than giving up.
        if state["revealed"]:
            return
        if state["window"] is None:
            log.warning("safety timer: boot still in progress — re-arming")
            QTimer.singleShot(5000, safety_reveal)
            return
        log.warning("safety timer fired — revealing without loadFinished")
        reveal()

    QTimer.singleShot(_SPLASH_SAFETY_MS, safety_reveal)

    def on_quit() -> None:
        log.info("aboutToQuit (+%.2fs)", time.monotonic() - t0)
        logging.shutdown()
        if boot["shutdown"] is not None and boot["server"] is not None:
            boot["shutdown"](boot["server"])  # calls os._exit(0)
        else:
            os._exit(0)  # quit during splash, before the server existed

    app.aboutToQuit.connect(on_quit)

    app.exec()


if __name__ == "__main__":
    main()
