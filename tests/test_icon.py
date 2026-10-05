"""There is one logo: src/convexity/assets/icon.svg. Everything else derives from it.

Guards the v1.14.x icon unification: the Dock icon, splash, window icon,
Windows .ico and README used to come from a transparent glyph that macOS 26
wrapped in a grey plate. Now icon.svg is the master, scripts/build_icon.py
renders icon.png (full-bleed) and icon-rounded.png, and these tests fail if
either PNG drifts from a fresh render, or a second icon appears.

The drift tests need PySide6 (QtSvg), so they skip in CI's plain test job and
run in the desktop job, which installs the desktop extra and Qt's libraries.
"""

import struct
import subprocess
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "convexity" / "assets"
IMAGE_SUFFIXES = {".png", ".svg", ".icns", ".ico", ".jpg", ".jpeg"}
ALLOWED = {
    "src/convexity/assets/icon.svg",
    "src/convexity/assets/icon.png",
    "src/convexity/assets/icon-rounded.png",
}


def test_only_one_logo_is_tracked():
    files = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    extra = [
        f
        for f in files
        if Path(f).suffix.lower() in IMAGE_SUFFIXES
        and not f.startswith(("docs/screenshots/", "docs/"))
        and f not in ALLOWED
    ]
    assert extra == [], f"second image/icon files tracked (one logo rule): {extra}"


def test_runtime_and_installers_read_the_shipped_icons_only():
    desktop = (ROOT / "src/convexity/desktop.py").read_text()
    assert '_ASSETS / "icon.png"' in desktop and '_ASSETS / "icon-rounded.png"' in desktop
    assert "assets/icon-rounded.png" in (ROOT / "README.md").read_text()
    assert "'icon-rounded.png'" in (ROOT / "packaging/install.ps1").read_text()
    assert 'ICON_PNG="$("$TOOL_PY"' in (ROOT / "install.sh").read_text()


def _renders():
    pytest.importorskip("PySide6.QtSvg")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_icon", ROOT / "scripts/build_icon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from PySide6.QtGui import QGuiApplication

    _app = QGuiApplication.instance() or QGuiApplication([])
    return mod


def _mean_abs_diff(a, b):
    assert a.size() == b.size()
    total = n = 0
    for y in range(0, a.height(), 8):
        for x in range(0, a.width(), 8):
            pa, pb = a.pixelColor(x, y), b.pixelColor(x, y)
            total += (
                abs(pa.red() - pb.red())
                + abs(pa.green() - pb.green())
                + abs(pa.blue() - pb.blue())
                + abs(pa.alpha() - pb.alpha())
            )
            n += 4
    return total / n


@pytest.mark.parametrize("name,rounded", [("icon.png", False), ("icon-rounded.png", True)])
def test_pngs_match_the_master(name, rounded):
    mod = _renders()
    from PySide6.QtGui import QImage

    fresh = mod.render(rounded)
    shipped = QImage(str(ASSETS / name)).convertToFormat(QImage.Format.Format_ARGB32)
    assert _mean_abs_diff(fresh, shipped) < 1.0, (
        f"{name} drifted from icon.svg; run `uv run python scripts/build_icon.py`"
    )


def _png_rgba(path: Path) -> tuple[int, int, list[bytes]]:
    """Decode an 8-bit RGBA PNG with the standard library only, so this check
    runs in CI's plain test job, which has no PySide6. Returns (w, h, rows)."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(data):
        n = struct.unpack(">I", data[pos : pos + 4])[0]
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            assert (depth, ctype) == (8, 6), "expected 8-bit RGBA"
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    raw, stride, rows, prev = zlib.decompress(idat), w * 4, [], bytearray(w * 4)
    for y in range(h):
        f, line = (
            raw[y * (stride + 1)],
            bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)]),
        )
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0
            b, c = prev[i], prev[i - 4] if i >= 4 else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line))
        prev = line
    return w, h, rows


def _pixel(rows, x, y):
    return tuple(rows[y][x * 4 : x * 4 + 4])


def test_full_bleed_icon_is_opaque_and_rounded_has_transparent_corners():
    w, h, square = _png_rgba(ASSETS / "icon.png")
    _, _, rounded = _png_rgba(ASSETS / "icon-rounded.png")
    assert (w, h) == (1024, 1024)
    # Transparent corners on the macOS icon are what makes macOS 26 add a grey plate.
    for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        assert _pixel(square, x, y) == (0x0D, 0x11, 0x17, 255)  # == the splash background
        assert _pixel(rounded, x, y)[3] == 0
    assert _pixel(rounded, 512, 512)[3] == 255
