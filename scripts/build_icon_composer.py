"""Build an Icon Composer package (AppIcon.icon) from the one master logo.

    uv run --extra desktop python scripts/build_icon_composer.py OUT_DIR

Why: macOS 26 paints its Liquid Glass rim (a bright edge top-left and along the
bottom) on every legacy .icns icon, and an app that is not running cannot
change that. An icon shipped as a compiled Assets.car (Icon Composer's .icon,
built by Xcode's actool) can switch the specular highlight off. This writes
that package; .github/workflows/closed-app-icon.yml compiles it on a macOS
runner, because the Command Line Tools have no actool.

Nothing here is a second logo: the glyph layers are renders of assets/icon.svg
with its background tile removed, in the two colourways of convexity.icon. The
tile itself is the package's fill, so macOS can switch it with its own
appearance: white in Light, the master's black in Dark (the running app follows
the dashboard theme instead, see desktop.py _IconSwitcher).
"""

import json
import re
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

from convexity.icon import DARK_TILE, LIGHT_SWAPS, svg_for

SIZE = 1024
# The master's background: the one full-bleed rect, drawn before the glyph group.
_TILE_RECT = re.compile(r'\s*<rect width="1024" height="1024" fill="[^"]+"/>')


def glyph_png(variant: str, path: Path) -> None:
    svg = svg_for(variant).decode("utf-8")
    svg, n = _TILE_RECT.subn("", svg, count=1)
    if n != 1:
        raise SystemExit("icon.svg no longer has its full-bleed background rect; update _TILE_RECT")
    img = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(p, QRectF(0, 0, SIZE, SIZE))
    p.end()
    if not img.save(str(path)):
        raise SystemExit(f"could not write {path}")


def srgb(hex_colour: str) -> str:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return f"srgb:{r:.5f},{g:.5f},{b:.5f},1.00000"


def icon_json() -> dict:
    light_tile = dict(LIGHT_SWAPS)[DARK_TILE]
    return {
        # The tile. "Default" is the light appearance; dark is the master's black.
        "fill-specializations": [
            {"value": {"solid": srgb(light_tile)}},
            {"appearance": "dark", "value": {"solid": srgb(DARK_TILE)}},
        ],
        "groups": [
            {
                "layers": [
                    {
                        # No glass on the candles, and no specular highlight on the
                        # group: the whole point of this package.
                        "glass": False,
                        "image-name-specializations": [
                            {"value": "glyph-light.png"},
                            {"appearance": "dark", "value": "glyph-dark.png"},
                        ],
                        "name": "candles",
                    }
                ],
                "lighting": "individual",
                "shadow": {"kind": "none", "opacity": 0.5},
                "specular": False,
                "translucency": {"enabled": False, "value": 0.5},
            }
        ],
        "supported-platforms": {"squares": "shared"},
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    out = Path(sys.argv[1]) / "AppIcon.icon"
    assets = out / "Assets"
    assets.mkdir(parents=True, exist_ok=True)
    _app = QGuiApplication.instance() or QGuiApplication([])
    glyph_png("light", assets / "glyph-light.png")
    glyph_png("dark", assets / "glyph-dark.png")
    (out / "icon.json").write_text(json.dumps(icon_json(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
