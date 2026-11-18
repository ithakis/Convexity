"""Static guards for the E card's filings and the Earnings history (Phase 5).

No JS runner in this repo (see test_frontend_overlays.py), so these read
app.js / index.html / style.css as text. They pin what is invisible in
review and costly in use: the filings must never be asked when the window
opens (Finnhub quota), links must be real anchors that open outside the app
(the app never fetches sec.gov), the pop-up must stack over the stock window
and Esc must close only it, the table's rows must follow the server's rule,
and the approved copy must not drift."""

import re
from pathlib import Path

from convexity import filings

_STATIC = Path(__file__).resolve().parent.parent / "src" / "convexity" / "static"
_APP_JS = (_STATIC / "app.js").read_text(encoding="utf-8")
_INDEX = (_STATIC / "index.html").read_text(encoding="utf-8")
_CSS = (_STATIC / "style.css").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    i = _APP_JS.index(f"function {name}(")
    return _APP_JS[i : _APP_JS.index("\nfunction ", i + 10)]


def test_the_filings_are_never_asked_when_the_window_opens():
    assert "ehEnsure" not in _fn("openModal")
    assert "earnings-history" not in _fn("openModal")
    # Only one place builds the request, and it is ehEnsure.
    assert _APP_JS.count("/api/earnings-history") == 1
    assert "/api/earnings-history" in _fn("ehEnsure")


def test_they_are_asked_on_an_e_hover_an_opened_card_or_the_button():
    markers = _fn("renderEventMarkers")
    assert '".m-ev.e"' in markers and '"pointerenter", () => ehEnsure()' in markers
    assert "ehEnsure()" in _fn("openEventCard")
    assert "ehEnsure()" in _fn("ehOpen")


def test_links_are_real_anchors_that_open_outside_the_app():
    a = _fn("ehA")
    assert 'target="_blank" rel="noopener"' in a and "escapeHtml(href)" in a
    # The app hands links to the browser; it never fetches EDGAR itself.
    assert not re.search(r"fetch\([^)]*sec\.gov", _APP_JS)
    # Every filing link goes through ehA (no hand-built href in the block).
    start = _APP_JS.index("Earnings filings and the Earnings history (filings.py")
    block = _APP_JS[start : _APP_JS.index("function yTagSvg")]
    # ("#" hrefs are in-app actions: Try again, Settings.)
    assert len(re.findall(r'href="(?!#")', block)) == 1


def test_the_pop_up_opens_and_closes_through_the_helpers():
    assert 'showOverlay("#eh-bg")' in _fn("ehOpen")
    assert 'hideOverlay("#eh-bg")' in _fn("ehClose")
    assert "ehClose();" in _fn("closeModal")
    assert 'id="eh-bg"' in _INDEX and 'aria-modal="true"' in _INDEX


def test_the_pop_up_stacks_over_the_window_and_under_what_opens_over_it():
    """The stock window is 50, lifted to 88 over the Attention screen; the
    plan popover is 90, Settings 92. Not a .modal-bg, whose z is rewritten
    while the Attention screen shows."""
    z = re.search(r"\.eh-bg \{[^}]*z-index:\s*(\d+)", _CSS)
    assert z and 88 < int(z.group(1)) < 90
    assert 'class="eh-bg"' in _INDEX and 'class="modal-bg eh' not in _INDEX


def test_escape_closes_only_the_top_layer():
    start = _APP_JS.index("Esc closes the top layer only: the pop-up")
    block = _APP_JS[start : start + 900]
    assert 'window.addEventListener("keydown"' in block and "}, true);" in block
    assert "stopImmediatePropagation" in block and "ehClose()" in block
    assert "closeEventCard()" in block
    assert "#settings-bg.show, #confirm-bg.show" in block


def test_the_table_rows_follow_the_servers_rule():
    assert f"const EH_YEARS_DAYS = {int(filings.YEARS * 365)};" in _APP_JS
    assert f"const EH_ROWS = {filings.N_REPORTS};" in _APP_JS
    rows = _fn("ehRows")
    assert "e.date === t && e.actual != null" in rows


def test_the_table_draws_from_the_events_and_only_filings_wait():
    """Progressive: the rows come from DETAIL.events, the payload only fills
    the Filings column (a spinner per row until then)."""
    render = _fn("ehRender")
    assert "ehRows()" in render and "evIdx(pts" in render
    assert "ef-spin" in render


def test_price_moves_are_the_held_lines():
    render = _fn("ehRender")
    assert "d.history" in render and "price moves on" in render


def test_the_approved_copy():
    for text in (
        "Finding the filing…",
        'Add a Finnhub key in <a href="#" data-eh-keys>Settings → API keys</a> to link the filings.',
        "Couldn’t load the filings (Finnhub didn’t answer).",
        "Try again",
        "Not filed with the SEC. Results are on the company’s investor pages",
        "The press release is the first link inside.",
        "not filed yet (usually by ",
        "is the latest filing.",
        "No 10-Q: foreign companies report their quarters on a ",
        "Finnhub doesn’t list this report’s filing yet.",
        "Finnhub doesn’t list this report’s filing.",
        "EDGAR’s own list shows every ",
        "files with the SEC as <b>",
        "Yahoo has no recent earnings dates for this company.",
        "No earnings history",
        "EPS from Yahoo · filings from SEC EDGAR",
        "the US line · price moves on",
        "Click a row to open its earnings release. ",
        "All filings on EDGAR",
        "The figures below come from Yahoo and are complete.",
        "Before the chart’s history.",
        "No consensus in Yahoo’s calendar.",
        "not in Yahoo",
    ):
        assert text in _APP_JS, text


def test_data_colours_stay_classic():
    assert ".eh-modal td.pos { color: var(--pos); }" in _CSS
    assert ".eh-modal td.neg { color: var(--neg); }" in _CSS


def test_the_card_shows_the_fiscal_quarter_and_never_a_guessed_year():
    assert "const q = ehLabel(e);" in _fn("openEventCard")
    label = _fn("ehLabel")
    # The payload (which knows the company's naming) over the events' row; a
    # quarter without a certain year is named by its end, only when exact.
    assert "if (r.label) return r.label;" in label
    assert (
        "r.period_exact && r.period_end ? `Q${r.quarter} · quarter to ${ehDay(r.period_end)}` : `Q${r.quarter}`"
        in label
    )
    assert "ehLabel(x.e)" in _fn("ehRender")
    assert ".ef-q" in _fn("ehRefresh")  # the subtitle follows the payload


def test_the_card_cannot_be_clipped_by_the_chart():
    """#m-chart clips (overflow: hidden): a tall card scrolls inside it."""
    fit = _fn("ehFitCard")
    assert "wrap.clientHeight" in fit and "maxHeight" in fit
    assert "ehFitCard();" in _fn("openEventCard") and "ehFitCard();" in _fn("ehRefresh")
    assert re.search(r"\.m-ev-card\.e-card \{[^}]*overflow-y: auto", _CSS)


def test_every_failure_reason_has_its_words():
    fail = _fn("ehFailText")
    for reason, words in (
        ("busy", "too many lookups this minute"),
        ("empty", "Finnhub lists none for this company"),
    ):
        assert f'reason === "{reason}"' in fail and words in fail
    assert "Finnhub didn’t answer" in fail
