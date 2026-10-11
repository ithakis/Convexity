"""Phase 3 plumbing: as-traded closes (``_bulk_close(adjusted=False)``) with
their dividends, the default path left exactly as it was, Carino linking,
and daily FX multipliers. yfinance is stubbed; public tickers only."""

import numpy as np
import pandas as pd
import pytest

from convexity import analytics as A
from convexity import cache
from convexity import fx as F


@pytest.fixture(autouse=True)
def _fresh_caches():
    for c in (cache._BULK_CLOSE_CACHE, cache._DIVIDENDS_CACHE, cache._FX_CCY_HIST_CACHE):
        c.clear()
    yield
    for c in (cache._BULK_CLOSE_CACHE, cache._DIVIDENDS_CACHE, cache._FX_CCY_HIST_CACHE):
        c.clear()


def _frame(symbols, *, raw):
    idx = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"])
    cols = {}
    for s in symbols:
        cols[(s, "Close")] = [100.0, 101.0, 102.0] if raw else [99.0, 100.0, 101.0]
        cols[(s, "Open")] = [1.0, 1.0, 1.0]
        cols[(s, "High")] = [1.0, 1.0, 1.0]
        cols[(s, "Low")] = [1.0, 1.0, 1.0]
        cols[(s, "Volume")] = [1.0, 1.0, 1.0]
        if raw:
            cols[(s, "Dividends")] = [0.0, 0.5, 0.0]
    return pd.DataFrame(cols, index=idx)


def test_default_bulk_close_call_is_unchanged(monkeypatch):
    calls = []

    def download(**kw):
        calls.append(kw)
        return _frame(kw["tickers"], raw=False)

    monkeypatch.setattr(A.yf, "download", download)
    out = A._bulk_close(["AAPL", "MSFT"], "1Y")
    assert calls == [{"tickers": ["AAPL", "MSFT"], "period": "1y", "interval": "1d",
                      "auto_adjust": True, "group_by": "ticker", "threads": True,
                      "progress": False}]  # fmt: skip
    assert list(out["AAPL"]) == [99.0, 100.0, 101.0]
    assert set(cache._BULK_CLOSE_CACHE) == {("AAPL", "1y"), ("MSFT", "1y")}
    assert cache._DIVIDENDS_CACHE == {}


def test_raw_closes_are_cached_apart_with_their_dividends(monkeypatch):
    calls = []

    def download(**kw):
        calls.append(kw)
        return _frame(kw["tickers"], raw=not kw["auto_adjust"])

    monkeypatch.setattr(A.yf, "download", download)
    adj = A._bulk_close(["AAPL"], "1Y")
    raw = A._bulk_close(["AAPL"], "1Y", adjusted=False)
    assert calls[1]["auto_adjust"] is False and calls[1]["actions"] is True
    assert list(adj["AAPL"]) == [99.0, 100.0, 101.0]
    assert list(raw["AAPL"]) == [100.0, 101.0, 102.0]
    assert ("AAPL", "1y|raw") in cache._BULK_CLOSE_CACHE
    d = A.dividends_cached("AAPL")
    assert list(d.index.strftime("%Y-%m-%d")) == ["2026-01-05"] and list(d) == [0.5]
    # A second call is served from the cache; the adjusted one is untouched.
    A._bulk_close(["AAPL"], "1Y", adjusted=False)
    assert len(calls) == 2
    assert list(A._bulk_close(["AAPL"], "1Y")["AAPL"]) == [99.0, 100.0, 101.0]


def test_raw_closes_refetch_when_only_the_dividends_expired(monkeypatch):
    calls = []
    monkeypatch.setattr(
        A.yf, "download", lambda **kw: calls.append(kw) or _frame(kw["tickers"], raw=True)
    )
    A._bulk_close(["AAPL"], "1Y", adjusted=False)
    cache._DIVIDENDS_CACHE.clear()
    A._bulk_close(["AAPL"], "1Y", adjusted=False)
    assert len(calls) == 2 and A.dividends_cached("AAPL") is not None


def test_carino_link_adds_up_to_the_compounded_total():
    rng = np.random.default_rng(3)
    daily = pd.DataFrame(rng.normal(0.0004, 0.01, (250, 4)), columns=list("ABCD"))
    linked = A._carino_link(daily)
    total = float(np.prod(1 + daily.sum(axis=1)) - 1)
    assert sum(linked.values()) == pytest.approx(total, abs=1e-12)
    # The wrapper the research tabs use is unchanged: percentage points.
    w = pd.Series({"A": 0.4, "B": 0.3, "C": 0.2, "D": 0.1})
    pp = A._linked_contribution(daily, w)
    assert sum(pp.values()) / 100 == pytest.approx(float(np.prod(1 + daily.mul(w).sum(axis=1)) - 1))


def test_fx_multipliers_cross_through_usd_day_by_day(monkeypatch):
    usd_per = {
        "GBP": pd.Series([1.25, 1.30], index=pd.to_datetime(["2026-01-02", "2026-01-06"])),
        "EUR": pd.Series([1.10, 1.20], index=pd.to_datetime(["2026-01-02", "2026-01-06"])),
    }
    monkeypatch.setattr(F, "_fx_usd_series", lambda c, p: usd_per.get(c))
    dates = ["2025-12-31", "2026-01-02", "2026-01-05", "2026-01-06"]
    m = F.fx_multipliers(["EUR", "GBp", "USD", "XYZ"], "GBP", dates)
    # Before the history starts: its first rate. Between fixings: the last one.
    assert m["EUR"] == pytest.approx([1.10 / 1.25, 1.10 / 1.25, 1.10 / 1.25, 1.20 / 1.30])
    assert m["GBp"] == pytest.approx([1.0] * 4)  # pence map to pounds
    assert m["USD"] == pytest.approx([1 / 1.25, 1 / 1.25, 1 / 1.25, 1 / 1.30])
    assert m["XYZ"] is None  # no history: the caller falls back and says so


def test_a_missed_symbol_is_retried_alone_on_local_dates(monkeypatch):
    """The per-ticker retry (``Ticker.history``) stamps bars at local midnight;
    they land on the exchange's own date, with the dividends, in both modes."""
    monkeypatch.setattr(A.yf, "download", lambda **kw: pd.DataFrame())
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    idx = pd.DatetimeIndex(["2026-01-05", "2026-01-06"]).tz_localize("Europe/London")
    hist = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": [2500.0, 2510.0],
                         "Volume": 1.0, "Dividends": [0.0, 27.0]}, index=idx)  # fmt: skip
    calls = []

    class T:
        def __init__(self, s):
            pass

        def history(self, **kw):
            calls.append(kw)
            return hist

    monkeypatch.setattr(A.yf, "Ticker", T)
    raw = A._bulk_close(["SHEL.L"], "1Y", adjusted=False)
    adj = A._bulk_close(["SHEL.L"], "1Y")
    assert [c["auto_adjust"] for c in calls] == [False, True] and calls[0]["actions"] is True
    for df in (raw, adj):
        assert list(df.index.strftime("%Y-%m-%d")) == ["2026-01-05", "2026-01-06"]
    assert list(A.dividends_cached("SHEL.L")) == [27.0]
    assert cache._bulk_bars_get("SHEL.L", "1y") is not None  # bars for the adjusted path only


def test_a_short_fetch_never_shrinks_the_cached_dividends():
    import pandas as pd

    from convexity import cache

    cache._DIVIDENDS_CACHE.pop("DIVX", None)
    years = pd.Series([0.5] * 8, index=pd.date_range("2022-01-15", periods=8, freq="QS"))
    cache._dividends_put("DIVX", years)
    cache._dividends_put("DIVX", years.iloc[-1:])  # a one-month window
    assert len(cache._dividends_get("DIVX")) == 8
    cache._DIVIDENDS_CACHE.pop("DIVX", None)
