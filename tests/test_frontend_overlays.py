"""Guards for the overlay / body-scroll-lock contract in the frontend.

There is no JS test runner in this repo, so these are static checks over
app.js / index.html / style.css. That is enough, because the contract they
protect is structural: every full-screen overlay must go through
showOverlay()/hideOverlay() (which own the single nesting counter), and every
overlay scroll container must contain its overscroll.

Why this exists at all: before v1.11 three overlays each stashed their own
"previous body overflow" in a dataset key and the other nine locked nothing.
The detail modal was one of the nine, so the page scroll-chained behind it —
and because the global Escape handler closes seven overlays in one
unconditional pass, a nested pair could restore the wrong value. Both bugs are
invisible in review and obvious in use, which is exactly the kind a cheap
static guard should catch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parent.parent / "src" / "convexity" / "static"
_APP_JS = (_STATIC / "app.js").read_text(encoding="utf-8")
_INDEX = (_STATIC / "index.html").read_text(encoding="utf-8")
_CSS = (_STATIC / "style.css").read_text(encoding="utf-8")

# Every full-screen backdrop in index.html. Popovers that are merely positioned
# (the FX hover card, the inline "Save as..." prompt, dropdowns, the toast) are
# deliberately absent — they don't cover the page, so they must NOT lock scroll.
_OVERLAY_IDS = [
    "confirm-bg",
    "pf-weights-bg",
    "pf-mpt-bg",
    "pf-mpt-info-bg",
    "cv-modal-bg",
    "info-bg",
    "modal-bg",
    "methodology-bg",
    "ns-prog-bg",
    "settings-bg",
    "ns-tape-fs-bg",
    "export-bg",
]


def test_overlay_inventory_matches_markup():
    """A new backdrop in index.html must be added to this file's list — which
    forces whoever adds it to also satisfy the two tests below."""
    found = set(re.findall(r'<div[^>]*\bid="([a-z0-9-]*bg)"', _INDEX))
    assert found == set(_OVERLAY_IDS), (
        f"overlay inventory drifted from index.html: "
        f"only in markup={sorted(found - set(_OVERLAY_IDS))}, "
        f"only in test={sorted(set(_OVERLAY_IDS) - found)}"
    )


def test_body_overflow_is_only_touched_by_the_lock_helpers():
    """document.body.style.overflow must have exactly two writers.

    Anything else is a second, competing lock — which is what the three
    dataset.*PrevOverflow implementations were.
    """
    writes = [m.start() for m in re.finditer(r"(?:document\.)?body\.style\.overflow\s*=", _APP_JS)]
    # The two legitimate writers live inside lockBodyScroll / unlockBodyScroll.
    lock_start = _APP_JS.index("function lockBodyScroll")
    lock_end = _APP_JS.index("function showOverlay")
    outside = [p for p in writes if not (lock_start <= p < lock_end)]
    assert not outside, (
        "body.style.overflow written outside lockBodyScroll/unlockBodyScroll at "
        f"offsets {outside} — use showOverlay()/hideOverlay() instead"
    )
    assert not re.search(r"PrevOverflow", _APP_JS), (
        "a per-overlay 'previous overflow' stash is back; the nesting counter "
        "in SCROLL_LOCK replaced those because nested overlays restored each "
        "other's value"
    )


@pytest.mark.parametrize("overlay_id", _OVERLAY_IDS)
def test_overlay_show_class_is_not_toggled_by_hand(overlay_id):
    """No overlay may add/remove the "show" class directly.

    Bypassing the helpers means the counter never learns the overlay opened,
    so the next close decrements someone else's lock.
    """
    # Match e.g. $("#modal-bg").classList.add("show") and the
    # getElementById equivalent, on one line.
    pattern = re.compile(
        r'["#\']' + re.escape(overlay_id) + r'["\')\s\]]*\)?\s*\.classList\.(?:add|remove)\('
    )
    hits = [ln for ln in _APP_JS.splitlines() if pattern.search(ln)]
    assert not hits, f"{overlay_id} toggles .show directly: {hits}"


def test_overlay_scroll_containers_contain_their_overscroll():
    """Each overlay's inner scroller must set overscroll-behavior: contain.

    The body lock stops the page moving; containment stops the trackpad's
    elastic bounce handing leftover delta to whatever is behind the overlay.
    Both are needed — see the grouped rule in style.css.
    """
    block = re.search(
        r"/\* ===== Overlay scroll containment.*?\*/\s*(.*?)\{\s*overscroll-behavior:\s*contain;",
        _CSS,
        re.S,
    )
    assert block, "the grouped overscroll-containment rule is gone from style.css"
    listed = {s.strip() for s in block.group(1).split(",") if s.strip()}
    for required in (
        ".modal-bg",
        ".info-modal",
        ".mth-body",
        ".ns-prog-jobs",
        ".ns-tape-fs-body",
        ".pf-weights-body",
        ".cv-list",
        ".settings-pane-body.scroll",
    ):
        assert required in listed, f"{required} missing from the containment rule"


def test_contribution_header_is_sticky_and_opaque():
    """The sticky topbar (z 30) used to scroll straight through these headers,
    which had no background at all."""
    m = re.search(r"\.pf-contrib-table th \{(.*?)\}", _CSS, re.S)
    assert m, ".pf-contrib-table th rule not found"
    body = m.group(1)
    assert "position: sticky" in body
    assert "top: 50px" in body, "must clear .progress-wrap (sticky at top: 50px)"
    assert "background:" in body, "a transparent sticky header shows rows through it"
    z = re.search(r"z-index:\s*(\d+)", body)
    assert z and 0 < int(z.group(1)) < 30, "must sit under the topbar (z 30)"
