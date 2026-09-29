"""Static guards for the hover explanations in the frontend (#5, #7, #8).

No JS runner in this repo (see test_frontend_overlays.py), so these read
app.js / index.html as text. They catch the drift that is invisible in
review: a new column or panel row that ships without its tip, or a
[data-rich-tip] key with no provider behind it, which would silently show
nothing on hover.
"""

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "src" / "convexity" / "static"
_APP_JS = (_STATIC / "app.js").read_text(encoding="utf-8")
_INDEX = (_STATIC / "index.html").read_text(encoding="utf-8")


def _block(src: str, start: str, end: str = "\n};") -> str:
    i = src.index(start)
    return src[i : src.index(end, i)]


def _object_keys(block: str) -> set[str]:
    """Top-level keys of a JS object literal, quoted or bare, two-space indented."""
    return set(re.findall(r'^  "?([^":\s]+(?: [^":]+)*)"?\s*:', block, re.MULTILINE))


def test_every_holdings_column_has_a_header_tip():
    cols = _block(_APP_JS, "const COLS = [", "\n];")
    keys = re.findall(r'key: *"([a-z0-9_]+)"|ynum\("([a-z0-9_]+)"', cols)
    keys = [a or b for a, b in keys]
    info = _object_keys(_block(_APP_JS, "const COL_INFO = {"))
    missing = [k for k in keys if k not in info]
    assert keys and not missing, f"COL_INFO has no tip for: {missing}"


def test_every_detail_metric_has_a_tip():
    """Labels passed to renderDetailMetricGrid (snapshot, valuation,
    fundamentals, dividend) all resolve in DETAIL_METRIC_INFO."""
    info = _object_keys(_block(_APP_JS, "const DETAIL_METRIC_INFO = {"))
    body = _block(_APP_JS, "function renderSections() {", "\nfunction ")
    grids = re.findall(r"const (\w+) = \[\n(.*?)\n\s*\];", body, re.DOTALL)
    used = set(re.findall(r"renderDetailMetricGrid\((\w+)\)", body))
    labels = [
        lbl
        for name, rows in grids
        if name in used
        for lbl in re.findall(r'^\s*\["([^"]+)",', rows, re.MULTILINE)
    ]
    assert len(labels) > 20, "parsed too few labels: the detail grid source moved"
    missing = [lbl for lbl in labels if lbl not in info]
    assert not missing, f"DETAIL_METRIC_INFO has no tip for: {missing}"


def test_every_rich_tip_key_has_a_provider():
    keys = set(re.findall(r'data-rich-tip="([^"$<]+)"', _APP_JS + _INDEX))
    # Keys built in a template (${...}) are listed by hand.
    keys |= {"mpt:cvar", "mpt:cvar-legacy", "contrib:wxr", "contrib:comp", "contrib:contribution"}
    providers = set(re.findall(r'RICH_TIPS\["([^"]+)"\]', _APP_JS))
    mpt = _object_keys(_block(_APP_JS, "const MPT_METRIC_INFO = {"))
    providers |= {f"mpt:{k}" for k in mpt}
    contrib = _object_keys(_block(_APP_JS, "const CONTRIB_INFO = {"))
    providers |= {f"contrib:{k}" for k in contrib}
    missing = sorted(keys - providers)
    assert not missing, f"[data-rich-tip] keys without a RICH_TIPS provider: {missing}"


def test_budget_tip_matches_the_backend_budgets():
    """The numbers in the Compute budget tip are copied from frontier.py."""
    from convexity import frontier

    info = _block(_APP_JS, "const MPT_BUDGET_INFO = [", "\n];")
    for name, b in frontier._BUDGETS.items():
        row = re.search(
            rf'name: "{name.capitalize()}", *secs: "~(\d+) s", *nf: (\d+), *cloud: "(\d+)k"', info
        )
        assert row, f"no {name} row in MPT_BUDGET_INFO"
        assert (int(row[1]), int(row[2]), int(row[3]) * 1000) == (
            int(b["secs"]),
            b["nf"],
            b["cloud"],
        )
    tip = _APP_JS[_APP_JS.index('RICH_TIPS["mpt-budget"]') :][:2000]
    assert f"at least {frontier._BOOT_MIN}" in tip
    # n_boot grows in whole chunks, so the cap can be overshot by a chunk.
    assert f"up to about {frontier._BOOT_CAP:,}" in tip
