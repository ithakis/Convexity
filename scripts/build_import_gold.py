"""Rebuild the import eval's synthetic screenshots and PDFs from their HTML.

The gold set (tests/data/import/) is made-up data with public tickers; its
images and PDFs are rendered from tests/data/import/src/*.html by headless
Chrome, so they can be regenerated and reviewed as text. Needs Google Chrome
(dev machines only; the app never runs this):

    uv run python scripts/build_import_gold.py
"""

import base64
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

GOLD = Path(__file__).resolve().parent.parent / "tests" / "data" / "import"
SRC = GOLD / "src"
CHROMES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
)


def chrome() -> str:
    for c in CHROMES:
        if Path(c).exists() or shutil.which(c):
            return c
    sys.exit("Google Chrome not found")


def run(out: Path, *args: str) -> None:
    """Chrome writes the file but its headless mode doesn't always exit, so
    wait for the output to appear and settle, then stop it."""
    prof = tempfile.mkdtemp(prefix="gold-")
    out.unlink(missing_ok=True)
    proc = subprocess.Popen([chrome(), "--headless=new", f"--user-data-dir={prof}", "--hide-scrollbars",
                             "--no-first-run", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    size, deadline = -1, time.time() + 60
    try:
        while time.time() < deadline:
            time.sleep(0.5)
            now = out.stat().st_size if out.exists() else -1
            if now > 0 and now == size:
                return
            size = now
        sys.exit(f"Chrome didn't write {out.name}")
    finally:
        proc.terminate()


def shot(html: Path, out: Path, w: int, h: int, scale: int) -> None:
    run(
        out,
        f"--screenshot={out}",
        f"--window-size={w},{h}",
        f"--force-device-scale-factor={scale}",
        html.as_uri(),
    )


def pdf(html: Path, out: Path) -> None:
    run(out, f"--print-to-pdf={out}", "--no-pdf-header-footer", html.as_uri())


def main() -> None:
    # Phone screenshots at an iPhone's real resolution: shrunk, the vision
    # model misreads digits (405.00 as 405.05), and real ones arrive full size.
    # The pages are a fixed 393 px wide: headless Chrome's window has a
    # minimum width, and a fluid page laid out wider loses its right column.
    shot(SRC / "activity.html", GOLD / "activity.png", 393, 640, 3)
    shot(SRC / "holdings.html", GOLD / "holdings.png", 393, 560, 3)
    pdf(SRC / "statement.html", GOLD / "statement.pdf")
    # A scanned statement: the page as a picture inside a PDF, no text layer.
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "scan.png"
        shot(SRC / "scan_source.html", png, 830, 330, 2)
        wrap = Path(tmp) / "scanned.html"
        b64 = base64.b64encode(png.read_bytes()).decode()
        wrap.write_text(f'<!doctype html><body style="margin:0"><img src="data:image/png;base64,{b64}" '
                        'style="width:100%"></body>')  # fmt: skip
        pdf(wrap, GOLD / "scanned.pdf")
    for p in sorted(GOLD.glob("*.p*")):
        print(f"{p.name}: {p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
