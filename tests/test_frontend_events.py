"""Static guards for the rail's Recent events and the Events view (P6).

No JS runner in this repo (see test_frontend_overlays.py): these read app.js
and style.css as text. They pin what review can't see and use would pay
for: the events are never asked on the page's path, the filings only when a
marker window opens, the ⤢ buttons open the right view, Esc closes the
window before the screen, links open outside the app, the labels and cuts
match the server's, and the data colours stay green / red / muted."""

import re
from pathlib import Path

from convexity import event_study as es

_STATIC = Path(__file__).resolve().parent.parent / "src" / "convexity" / "static"
_APP_JS = (_STATIC / "app.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "style.css").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    i = _APP_JS.index(f"function {name}(")
    ends = [
        j
        for j in (
            _APP_JS.find(m, i + 10)
            for m in ("\nfunction ", "\nasync function ", "\nconst ", "\n$(")
        )
        if j > 0
    ]
    return _APP_JS[i : min(ends)]


def test_the_events_are_asked_only_when_the_view_shows():
    assert _APP_JS.count("/api/investments/events") == 1
    assert "/api/investments/events" in _fn("evxLoad")
    for fn in ("invLoad", "invRender", "invAttentionHtml", "invRecentHtml"):
        assert "evxLoad" not in _fn(fn)
    assert 'if (view === "events") { evxAfterPaint(); evxLoad(); }' in _fn("invFullPaint")
    # A failed load waits for "Try again": the repaint after it must not ask again in a loop.
    assert "EVX.failed && EVX.tried === key" in _fn("evxLoad")


def test_the_rail_reads_the_pages_own_items_and_never_ranks():
    rail = _fn("invRecentHtml")
    assert "b.attention?.recent" in rail
    assert "sort(" not in rail  # the server ranks (|z| x sqrt weight) and orders them
    assert "e.top" in rail


def test_the_full_screen_buttons_open_the_right_view():
    att = _fn("invAttentionHtml")
    # Attention's own ⤢ keeps opening Desk; Upcoming's and Recent events' open Events.
    assert att.count("data-inv-att-full") == 1
    assert 'kicker("Upcoming", tools(evFull))' in att and "data-inv-ev-full" in att
    assert "data-inv-ev-full" in _fn("invRecentHtml")
    assert '["events", "Events"]' in _APP_JS
    assert 'invFullOpen("events", "Events")' in _fn("evxOpenFromRail")
    assert 'e.target.closest("[data-inv-ev-full]")) evxOpenFromRail(null)' in _APP_JS


def test_the_filings_are_asked_when_a_window_opens_never_when_the_view_does():
    assert _APP_JS.count("/api/earnings-history") == 1  # one request site: ehEnsure
    assert "ehEnsure" not in _fn("evxMainHtml") and "ehEnsure" not in _fn("evxDraw")
    assert "ehEnsure(false, evxEhCtx(e))" in _fn("evxPlaceCard")
    # The window's card keeps the stock window's (ehWin) by default.
    assert "const c = ctx || ehWin()" in _fn("ehModel")


def test_escape_closes_the_marker_window_before_the_screen():
    start = _APP_JS.index("Esc closes the top thing only: whatever is open over this screen")
    block = _APP_JS[start : start + 1200]
    i, j = block.index("EVX.sel"), block.index("if (INV_FULL.sel)")
    assert i < j and "}, true);" in block


def test_headline_links_open_outside_the_app():
    cause = _fn("evxCause")
    assert 'target="_blank" rel="noopener"' in cause and "escapeHtml(c.url)" in cause


def test_the_labels_and_cuts_match_the_server():
    side = _fn("evxSideHtml")
    for label in es.LABELS.values():
        assert f'"{label}"' in side
    assert es.CUTS == (1.0, 2.5)
    assert "more than 2.5× a normal move up" in side and "1× to 2.5× up" in side
    # The scale runs -4x..+4x: the 2.5 cut sits at (4 - 2.5) / 8 = 18.75%.
    assert "18.75%" in _CSS and "81.25%" in _CSS
    assert "const EVX_ZMAX = 4;" in _APP_JS
    assert f"const EVX_AHEAD = {es.UPCOMING_DAYS};" in _APP_JS


def test_the_code_writes_the_size_sentence_the_page_only_shows_it():
    assert "e.say" in _fn("evxCardHtml") and "e.say" in _fn("evxTableHtml")
    assert "two-day move" not in _fn("evxTableHtml")  # no second template in the page


def test_data_colours_stay_classic():
    start = _CSS.index("/* --- Events (P6;")
    block = _CSS[start : _CSS.index("@container (max-width: 940px)", start)]
    colours = set(re.findall(r"#[0-9a-fA-F]{3,6}\b", block))
    assert colours <= {"#fff", "#0a0e14"}  # chip text on the filled Big chips only
    assert "var(--pos)" in block and "var(--neg)" in block


def test_the_marker_window_is_the_stock_windows_card():
    assert 'class="m-ev-card evx-card' in _fn("evxPlaceCard")
    assert "data-evx-close" in _fn("evxPlaceCard")


def test_the_earnings_window_names_the_index_it_was_measured_against():
    card = _fn("evxCardHtml")
    assert "e.move?.index_name || e.index_name" in card
    assert "Against the broad market" not in card and '"S&P 500" : "broad market"' not in card


def test_market_band_labels_sit_in_their_own_lane_above_the_plot():
    draw = _fn("evxDraw")
    # The lane is above the plot's top, where a marker clipped at ±4× sits.
    lane = int(re.search(r"lane = (\d+)", draw).group(1))
    top = int(re.search(r"top = (\d+)", draw).group(1))
    assert top - lane >= 20
    assert 'class="evx-mbandt" x="${lx}" y="${lane}"' in draw
    assert "<title>${escapeHtml(lab)}:" in draw  # a label left out for room is on the band's hover


def test_a_move_not_looked_up_yet_is_looking_for_the_news():
    cause = _fn("evxCause")
    assert 'c.kind === "pending"' in cause and "Looking for the news." in cause
    assert "day0" not in _APP_JS  # the server says "pending" before the lookup now
