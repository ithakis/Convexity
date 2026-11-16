"""Static guards for the Phase 4 frontend: what-ifs, the weights row, the
Weight Center, the rebalance panel and the Optimize tab's saved sets.

There is no JS test runner in this repo (see test_frontend_overlays.py), so
these check the structure that keeps the owner's rules true: a what-if shows
only with its own My Investments portfolio, the jade origin marks exist in
every theme, weight differences are never green or red, every new POST is
JSON, the plug never saves half a result, and a saved run carries what the
plug's save reads back.
"""

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "src" / "convexity" / "static"
_JS = (_STATIC / "app.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "style.css").read_text(encoding="utf-8")
_HTML = (_STATIC / "index.html").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    """The source of one top-level function of app.js."""
    i = _JS.index(f"function {name}(")
    return _JS[i : _JS.index("\n}\n", i)]


def test_a_whatif_shows_only_with_its_own_book():
    assert (
        "const wiHidden = (name) => { const b = wiBlock(name); return !!b && b.book_id !== wiShowing(); };"
        in _JS
    )
    tabs = _fn("renderTabs")
    assert "filter(n => !wiHidden(n))" in tabs
    # The startup and a switch both move off a hidden what-if.
    assert "if (LAST_VIEW && wiHidden(LAST_VIEW))" in _JS
    assert "wiFollowBooks();" in _fn("booksSwitched")
    assert "await wiFollowBooks();" in _JS[_JS.index("async function booksAskDelete") :]
    follow = _JS[_JS.index("async function wiFollowBooks") :]
    assert "await loadViews();" in follow[: follow.index("\n}\n")]


def test_the_page_keeps_the_block_and_never_writes_it():
    persist = _JS[_JS.index("async function persistView") :]
    assert "whatif: d.view.whatif || VIEWS[targetName]?.whatif" in persist[: persist.index("\n}\n")]
    for m in re.finditer(r"body: JSON\.stringify\(\{([^}]*)\}", _JS):
        assert "whatif:" not in m.group(1), m.group(0)


def test_every_new_post_is_json():
    for route in ("/api/whatif", "/api/whatif/compare", "/api/whatif/rebalance",
                  "/api/weight-presets/optimized"):  # fmt: skip
        hits = [m.start() for m in re.finditer(re.escape(f'fetch("{route}"'), _JS)]
        assert hits, route
        for i in hits:
            assert '"Content-Type": "application/json"' in _JS[i : i + 300], route
    assert "sendBeacon" not in _JS


def test_the_origin_token_is_in_every_theme_and_only_a_line():
    for theme in (':root, [data-theme="light"]', '[data-theme="dark"]', '[data-theme="bloomberg"]'):
        block = _CSS[_CSS.index(theme + " {") :]
        block = block[: block.index("\n  }")]
        assert "--origin:" in block and "--origin-rgb:" in block, theme
    # Never a fill or a text colour: a background only on the 3px legend swatches (a line).
    fills = [
        m.group(1).strip()
        for m in re.finditer(r"([^{}]+)\{[^}]*background(?:-color)?:\s*var\(--origin\)", _CSS)
    ]
    assert fills and all(sel.endswith(".sw.bk") for f in fills for sel in f.split(",")), fills
    assert not re.search(r"[^-]color:\s*var\(--origin\)", _CSS)
    assert (
        "#tbl tbody tr.wi-o > td:first-child { box-shadow: inset 4px 0 0 var(--origin); }" in _CSS
    )


def test_weight_differences_are_neutral_text():
    for sel in (".wc-c .d {", ".wc-c .d.big {", ".wc-c .v {"):
        rule = _CSS[_CSS.index(sel) :]
        rule = rule[: rule.index("}")]
        assert "--pos" not in rule and "--neg" not in rule, sel
    fill = _fn("wcFillRow")
    assert "pos" not in fill and "neg" not in fill


def test_bloomberg_current_and_selected_rings_differ():
    bb = _CSS[_CSS.index('[data-theme="bloomberg"] {') :]
    bb = bb[: bb.index("\n  }")]
    assert "--accent: #fb8b1e" in bb and "--mpt-current: #4dc7f9" in bb
    assert 'ctx.strokeStyle = "#f59e0b"' not in _JS
    assert '_mptCssColor("--mpt-current", "#f59e0b")' in _JS


def test_a_saved_run_carries_what_the_plug_reads_back():
    body = _fn("mptCompactRun")
    assert "sets: d.sets, picks: d.picks, held_out: d.held_out, stand_ins: d.stand_ins" in body
    assert "const compact = mptCompactRun(d);" in _JS
    stream = _fn("mptStreamFrontier")
    assert "sets: msg.sets, picks: msg.picks" in stream
    assert "weight_sets: wcWeightSets()" in _fn("mptRun")


def test_the_plug_saves_everything_or_nothing():
    plug = _JS[_JS.index("async function wcOptimize") :]
    plug = plug[: plug.index("\n}\n")]
    # Stream, then the run, then the set: each failure ends with "Nothing was saved".
    order = [plug.index('"/api/efficient-frontier"'), plug.index('"/api/mpt-runs"'),
             plug.index('"/api/weight-presets/optimized"')]  # fmt: skip
    assert order == sorted(order)
    assert "Nothing was saved. ${m}" in plug
    assert 'budget: "light"' in plug
    assert 'msg.type === "cloud"' in plug  # `done` ships no cloud
    assert "SAME_RISK_NOTE" in _fn("wcRenderOpt") and "SAME_RISK_NOTE" in _fn("presetTip")


def test_the_rebalance_panel_is_read_only_and_debounced():
    assert 'id="rb-bg"' in _HTML and "not advice" in _HTML
    render = _fn("rbRender")
    assert "clearTimeout(RB.timer)" in render and "setTimeout(" in render
    assert "rbAsk" in _fn("wiRevCheck")  # a new rev asks again
    assert '"shares", "Whole shares"' in _JS and 'mode: "shares"' in _JS  # the default


def test_the_second_total_row_comes_from_the_book_total():
    rows = _fn("totalRows")
    assert "wiCmpFor()?.book_total" in rows and "book: true" in rows
    assert "if (TBL.rows.length > 1)" in _fn("renderTotalRows")


def test_overlays_and_pages_go_through_the_helpers():
    assert 'showOverlay("#rb-bg")' in _fn("rbOpen")
    assert 'hideOverlay("#rb-bg")' in _fn("rbClose")
    assert 'showOverlay("#pf-weights-bg")' in _fn("openWeightsPopup")
    for name in ("wiCreate", "wiStripRender", "rbRender", "renderModeBar"):
        assert '.classList.add("show")' not in _fn(name), name


def test_the_weights_row_measures_what_fits():
    bar = _fn("renderModeBar")
    assert "wrowFit(presets, active, draw)" in bar
    fit = _fn("wrowFit")
    assert "row.clientWidth" in fit and 'row.classList.add("meta-below")' in fit
    assert "if (act && !out.includes(act)) out.push(act);" in fit
    assert (
        ".pf-wrow .pf-pill-t { max-width: 180px; overflow: hidden; text-overflow: ellipsis;" in _CSS
    )
    assert "new ResizeObserver((ents) => {" in _JS[_JS.index("function wrowFit") :]


def test_a_removed_copy_name_stays_and_its_pill_follows_the_weights():
    remove = _fn("removeEntry")
    assert remove.index("return wiZeroOrigin(entry)") < remove.index("entriesArr(ENTRIES).filter")
    body = _JS[_JS.index("\nfunction render() {") :]
    assert "if (wo) renderConstituents();" in body[: body.index("\n}\n")]
    assert "if (wiActive()) renderConstituents();" in _fn("renderModeBar")
    assert 'const back = (mode === "equal" || mode === "cap")' in _fn("selectMode")


def test_the_comparison_is_keyed_on_the_weights():
    assert "weightsForMode(STATE.mode)" in _fn("wiCmpKey")
