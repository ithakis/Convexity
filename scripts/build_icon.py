"""Regenerate the raster icons from the one master, src/convexity/assets/icon.svg.

    uv run python scripts/build_icon.py

Outputs (both 1024x1024, both committed, both read at runtime or by the installers):
  icon.png          full-bleed square. macOS .icns (install.sh), the splash, and
                    the window icon. macOS 26 masks it into its own squircle, so
                    it must NOT carry transparent corners (that is what makes the
                    OS add a grey plate behind the glyph).
  icon-rounded.png  the same picture clipped to a rounded square with transparent
                    corners, for places that do not mask: the Windows .ico and
                    the README.

The white light-theme tile is not a file: the desktop app renders it at
runtime from the same master via convexity.icon.svg_for("light").

Never edit the PNGs by hand: change icon.svg and re-run this.
"""

from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPainterPath
from PySide6.QtSvg import QSvgRenderer

from convexity.icon import svg_for

ASSETS = Path(__file__).resolve().parent.parent / "src" / "convexity" / "assets"
SIZE = 1024
# Apple's squircle is a continuous curve; a plain rounded rect at 22.4% is the
# usual close stand-in for non-masked contexts.
CORNER = SIZE * 0.224


def render(rounded: bool, variant: str = "dark") -> QImage:
    img = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if rounded:
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, SIZE, SIZE), CORNER, CORNER)
        p.setClipPath(path)
    QSvgRenderer(QByteArray(svg_for(variant))).render(p, QRectF(0, 0, SIZE, SIZE))
    p.end()
    return img


def main() -> None:
    _app = QGuiApplication.instance() or QGuiApplication([])
    render(False).save(str(ASSETS / "icon.png"))
    render(True).save(str(ASSETS / "icon-rounded.png"))
    print("wrote icon.png, icon-rounded.png")


if __name__ == "__main__":
    main()
