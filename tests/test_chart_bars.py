"""TradingView-style chart types (v1.19): the bars behind HLC area / candles /
volume, for single stocks (real OHLC) and portfolios (approximate).

The portfolio bars are the risky part: a portfolio has no traded high or low,
so analytics._portfolio_bars builds them from the holdings. The invariants
that make the chart honest are checked here without the network:
  * the close line always sits inside its own bar (low <= close <= high),
    because a band that the close escapes would visibly contradict itself;
  * currency cancels (ratios are taken against each listing's OWN close);
  * a holding with no bars means NO bars at all, never a partial band.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from convexity import analytics
from convexity.helpers import _ohlc_to_points

_STATIC = Path(__file__).resolve().parent.parent / "src" / "convexity" / "static"


def _days(n):
    return pd.date_range("2026-01-05", periods=n, freq="B")


def _bars(close, spread=0.02, open_shift=-0.005, volume=1e6):
    close = pd.Series(close, dtype=float)
    return pd.DataFrame(
        {
            "Open": close * (1 + open_shift),
            "High": close * (1 + spread),
            "Low": close * (1 - spread),
            "Close": close,
            "Volume": volume,
        }
    )


@pytest.fixture
def bar_cache(monkeypatch):
    store = {}
    monkeypatch.setattr(analytics, "_bulk_bars_get", lambda s, p: store.get((s, p)))
    return store


def test_ohlc_points_drop_incomplete_bars_and_round():
    idx = _days(3)
    df = pd.DataFrame(
        {"Open": [1.123456789, np.nan, 3.0], "High": [2.0, 2.0, 4.0], "Low": [0.5, 0.5, 2.5]},
        index=idx,
    )
    pts = _ohlc_to_points(df)
    assert [p[0] for p in pts] == [int(idx[0].timestamp() * 1000), int(idx[2].timestamp() * 1000)]
    assert pts[0][1] == 1.12346  # 6 significant figures
    assert _ohlc_to_points(pd.DataFrame()) == []
    assert _ohlc_to_points(df[["Open", "High"]]) == []


def _port(conv, w):
    return 100 * (1 + (conv.pct_change().fillna(0) * w).sum(axis=1)).cumprod()


REL = 1e-5  # prices are sent at 6 significant figures


def test_portfolio_bars_contain_the_close_and_weight_the_volume(bar_cache):
    idx = _days(60)
    rng = np.random.default_rng(7)
    a = 100 * np.cumprod(1 + rng.normal(0, 0.02, len(idx)))
    b = 50 * np.cumprod(1 + rng.normal(0, 0.03, len(idx)))
    bar_cache[("AAA", "2y")] = _bars(a, spread=0.03).set_axis(idx)
    bar_cache[("BBB", "2y")] = _bars(b, spread=0.01, volume=2e6).set_axis(idx)
    conv = pd.DataFrame({"AAA": a, "BBB": b}, index=idx)
    w = pd.Series({"AAA": 0.25, "BBB": 0.75})
    port = _port(conv, w)

    out = analytics._portfolio_bars(port, w, "2y", conv)
    assert len(out["ohlc"]) == len(idx)
    for (_, o, h, lo), c in zip(out["ohlc"], port.values, strict=True):
        assert lo <= c * (1 + REL) and c <= h * (1 + REL)
        assert lo <= o <= h
    # First day (no move yet): the high is exactly the weighted spread.
    _, _, h0, l0 = out["ohlc"][0]
    assert h0 == pytest.approx(port.iloc[0] * (0.25 * 1.03 + 0.75 * 1.01), rel=REL)
    assert l0 == pytest.approx(port.iloc[0] * (0.25 * 0.97 + 0.75 * 0.99), rel=REL)
    # Volume = sum of weight x shares x display-currency close (4 sig. figures).
    assert out["volume"][0][1] == pytest.approx(0.25 * 1e6 * a[0] + 0.75 * 2e6 * b[0], rel=1e-3)


def test_portfolio_bars_are_the_rebalanced_bound(bar_cache):
    """The adversarial review's counter-example: 50/50, A doubles and closes
    at half its high, B halves and closes at its high. The daily-rebalanced
    basket at A's and B's highs is P(t-1) * (0.5*2*2 + 0.5*0.5*1) = 2.25."""
    idx = _days(2)
    bar_cache[("A", "2y")] = pd.DataFrame(
        {"Open": [1, 2], "High": [1, 4], "Low": [1, 2], "Close": [1, 2], "Volume": [0, 0]},
        index=idx,
        dtype=float,
    )
    bar_cache[("B", "2y")] = pd.DataFrame(
        {"Open": [1, 0.5], "High": [1, 0.5], "Low": [1, 0.5], "Close": [1, 0.5], "Volume": [0, 0]},
        index=idx,
        dtype=float,
    )
    conv = pd.DataFrame({"A": [1.0, 2.0], "B": [1.0, 0.5]}, index=idx)
    w = pd.Series({"A": 0.5, "B": 0.5})
    port = _port(conv, w)  # 1.0, 1.25
    out = analytics._portfolio_bars(port, w, "2y", conv)
    assert out["ohlc"][1][2] == pytest.approx(2.25 * port.iloc[0], rel=REL)


def test_portfolio_bars_ignore_currency(bar_cache):
    """Bars are in the listing's currency, the index in the display currency:
    a constant FX rate must not move the band."""
    idx = _days(10)
    local = np.linspace(100, 110, len(idx))
    bar_cache[("EUR1", "2y")] = _bars(local).set_axis(idx)
    w = pd.Series({"EUR1": 1.0})
    port = pd.Series(100 * local / local[0], index=idx)
    usd = analytics._portfolio_bars(port, w, "2y", pd.DataFrame({"EUR1": local * 1.1}, index=idx))
    jpy = analytics._portfolio_bars(port, w, "2y", pd.DataFrame({"EUR1": local * 160}, index=idx))
    assert [p[1:] for p in usd["ohlc"]] == [p[1:] for p in jpy["ohlc"]]


def test_portfolio_bars_clip_bad_prints(bar_cache):
    """VOD.L-style bad print: one day's high at 2x the close must not set the
    width of the band (wicks capped at 5x the median range, at least 3%)."""
    idx = _days(40)
    px = np.full(len(idx), 100.0)
    frame = _bars(px, spread=0.01).set_axis(idx)
    frame.iloc[20, frame.columns.get_loc("High")] = 200.0
    bar_cache[("X", "2y")] = frame
    w = pd.Series({"X": 1.0})
    port = pd.Series(100.0, index=idx)
    out = analytics._portfolio_bars(port, w, "2y", pd.DataFrame({"X": px}, index=idx))
    assert out["ohlc"][20][2] == pytest.approx(110.0, rel=REL)  # 5 x 2% median range


def test_portfolio_bars_need_every_weighted_holding(bar_cache):
    idx = _days(10)
    px = np.linspace(10, 12, len(idx))
    bar_cache[("AAA", "2y")] = _bars(px).set_axis(idx)
    conv = pd.DataFrame({"AAA": px, "BBB": px}, index=idx)
    port = pd.Series(100 * px / px[0], index=idx)
    # BBB has no cached bars → no band at all (the chart falls back to a line).
    # The keys are still sent, so the client knows the payload is current.
    none = analytics._portfolio_bars(port, pd.Series({"AAA": 0.5, "BBB": 0.5}), "2y", conv)
    assert none == {"ohlc": [], "volume": []}
    # ...unless BBB carries no weight.
    assert analytics._portfolio_bars(port, pd.Series({"AAA": 1.0, "BBB": 0.0}), "2y", conv)["ohlc"]


def test_put_bars_drops_duplicate_dates(monkeypatch):
    """yf.download can repeat a date; reindexing on it would raise and 500
    every portfolio's analytics."""
    store = {}
    monkeypatch.setattr(analytics, "_bulk_bars_put", lambda s, p, f: store.__setitem__(s, f))
    idx = pd.DatetimeIndex(["2026-01-05", "2026-01-05", "2026-01-06"])
    analytics._put_bars("D", "2y", _bars([1.0, 2.0, 3.0]).set_axis(idx))
    assert not store["D"].index.duplicated().any() and store["D"]["Close"].iloc[0] == 2.0


def test_chart_type_frontend_contract():
    js = (_STATIC / "app.js").read_text(encoding="utf-8")
    css = (_STATIC / "style.css").read_text(encoding="utf-8")
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    # HLC area is the default the user chose.
    assert 'return CHART_TYPES.some(t => t[0] === v) ? v : "hlc";' in js
    # The high line's colour exists in every theme (light/:root, dark, bloomberg).
    assert css.count("--hlc-hi:") == 3
    assert 'id="pf-show-vol"' in html and 'wireOverlayPill("#pf-show-vol", "showVol")' in js
    # The approximation is explained in Settings → About, not on the chart.
    assert 'id: "chart-bars"' in js
