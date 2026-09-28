"""analyze_portfolios_multi: SMA warm-up and the benchmark table (offline)."""

import numpy as np
import pandas as pd

from convexity import analytics as A


def _closes(symbols, periods=520, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-09-25", periods=periods)
    return pd.DataFrame(
        {s: 100 * np.cumprod(1 + rng.normal(0.0005, 0.01, periods)) for s in symbols}, index=idx
    )


def _run(monkeypatch, period="1Y"):
    tickers = ["AAA", "BBB"] + [t for t, _, _ in A._BENCHMARKS.values()] + ["XLK"]
    wide = _closes(tickers)
    monkeypatch.setattr(A, "_bulk_close", lambda syms, period: wide[[s for s in syms if s in wide]])
    monkeypatch.setattr(A, "_apply_fx_to_closes", lambda df, *a, **k: df)
    monkeypatch.setattr(A, "_analyst_for", lambda s, row=None: {})
    rows = [{"symbol": "AAA", "sector": "Technology"}, {"symbol": "BBB", "sector": "Technology"}]
    out = A.analyze_portfolios_multi(rows, {"eq": {"AAA": 1, "BBB": 1}}, period)
    return wide, out["eq"]


def test_sma_spans_the_whole_period(monkeypatch):
    wide, a = _run(monkeypatch)
    port = a["series"]["portfolio"]
    sma200 = a["series"]["sma"]["200"]
    # Starts at the period's first bar, not ~200 bars in.
    assert sma200[0][0] == port[0][0] and len(sma200) == len(port)

    # And equals a plain 200-bar rolling mean of the equal-weight index,
    # rescaled onto the period's rebased-100 axis.
    idx = wide[["AAA", "BBB"]].pct_change().fillna(0).mean(axis=1).add(1).cumprod()
    start = pd.Timestamp(port[0][0], unit="ms")
    expected = 100 * idx.rolling(200).mean().loc[start] / idx.loc[start]
    assert abs(sma200[0][1] - expected) < 1e-9


def test_benchmarks_shape(monkeypatch):
    _, a = _run(monkeypatch)
    b = a["benchmarks"]
    assert set(b) == set(A._BENCHMARKS) | {"SECTOR"}
    for blk in b.values():
        assert abs(blk["series"][0][1] - 100.0) < 1e-9
        assert {"total_return", "beta"} <= set(blk["stats"])
        assert {"beta", "r2", "te"} <= set(blk["rel"])
    assert abs(b["SPY"]["stats"]["beta"] - 1.0) < 1e-12  # SPY vs itself
    # Stats come from the period slice, not the warm-up window.
    first = pd.Timestamp(a["series"]["portfolio"][0][0], unit="ms")
    assert first >= pd.Timestamp("2026-09-25") - pd.DateOffset(years=1)
