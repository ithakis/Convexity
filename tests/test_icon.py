"""There is one logo: src/convexity/assets/icon.svg. Everything else derives from it.

Guards the v1.14.x icon unification: the Dock icon, splash, window icon,
Windows .ico and README used to come from a transparent glyph that macOS 26
wrapped in a grey plate. Now icon.svg is the master, scripts/build_icon.py
renders icon.png (full-bleed) and icon-rounded.png, and these tests fail if
either PNG drifts from a fresh render, or a second icon appears.
"""

import subprocess
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


def test_full_bleed_icon_is_opaque_and_rounded_has_transparent_corners():
    from PySide6.QtGui import QImage

    square = QImage(str(ASSETS / "icon.png"))
    rounded = QImage(str(ASSETS / "icon-rounded.png"))
    # Transparent corners on the macOS icon are what makes macOS 26 add a grey plate.
    assert square.pixelColor(0, 0).alpha() == 255
    assert square.pixelColor(0, 0).name() == "#0d1117"  # == the splash background
    assert rounded.pixelColor(0, 0).alpha() == 0
    assert rounded.pixelColor(512, 512).alpha() == 255
