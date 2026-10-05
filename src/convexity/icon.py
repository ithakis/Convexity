"""The two colourways of the one logo, derived from assets/icon.svg.

The desktop app's icon follows the dashboard theme: a white tile in Light, the
black tile of the master in Dark and Bloomberg. Rather than commit a second
logo (tests/test_icon.py allows exactly one), the light colourway is the master
with two colours swapped. Pure text substitution, no Qt, so the build script,
the desktop app and the tests share it.
"""

from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"
MASTER = ASSETS / "icon.svg"

DARK_TILE = "#0d1117"
# (master colour, light colour). The pale wick green vanishes on white, so the
# light tile uses a classic green that keeps the same contrast the pale one has
# on black.
LIGHT_SWAPS = ((DARK_TILE, "#ffffff"), ("#90e080", "#3fa34d"))


def variant_for_theme(theme: str) -> str:
    """Bloomberg is a dark theme: only Light gets the white tile."""
    return "light" if theme == "light" else "dark"


def svg_for(variant: str) -> bytes:
    svg = MASTER.read_text(encoding="utf-8")
    if variant == "light":
        for old, new in LIGHT_SWAPS:
            if old not in svg:
                raise ValueError(f"icon.svg no longer contains {old}; update LIGHT_SWAPS")
            svg = svg.replace(old, new)
    return svg.encode("utf-8")
