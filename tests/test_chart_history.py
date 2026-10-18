"""Tests for the chart data behind the stock window (fetcher.py), no network.

Intraday half of the granularity ladder: yfinance is monkeypatched. What matters is the
routing and the cap guard, because both failure modes are silent: an over-cap request
comes back as an EMPTY frame from Yahoo rather than an error, and a symbol with no
intraday data looks identical to a transient outage.

Also fetcher.fetch_events: the E and D flags."""

import pandas as pd  # noqa: E402
import pytest

from convexity import (
    cache,  # noqa: E402
    fetcher,  # noqa: E402
)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache._CACHE.clear()
    yield
    cache._CACHE.clear()


class _FakeTicker:
    """Records every history() call so tests can assert what was requested."""

    calls: list[dict] = []
    frames: dict = {}

    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, **kw):
        _FakeTicker.calls.append({"symbol": self.symbol, **kw})
        return _FakeTicker.frames.get(self.symbol, _FakeTicker.frames.get("*"))


def _frame(n=100):
    idx = pd.date_range("2026-01-01", periods=n, freq="30min", tz="UTC")
    return pd.DataFrame({"Close": range(1, n + 1), "Volume": [10] * n}, index=idx)


@pytest.fixture()
def fake_yf(monkeypatch):
    _FakeTicker.calls = []
    _FakeTicker.frames = {"*": _frame()}
    monkeypatch.setattr(fetcher.yf, "Ticker", _FakeTicker)
    return _FakeTicker


# ------------------------------ _period_days --------------------------------


@pytest.mark.parametrize(
    "period,days",
    [
        ("7d", 7),
        ("60d", 60),
        ("1y", 365.25),
        ("2y", 730.5),
        ("3mo", 91.32),
        ("1wk", 7),
        ("max", 10_000),
        ("ytd", 10_000),
        ("nonsense", 10_000),  # unparseable must read as "huge", not "tiny"
    ],
)
def test_period_days(period, days):
    assert fetcher._period_days(period) == pytest.approx(days, rel=1e-3)


def test_unparseable_period_is_treated_as_over_cap(fake_yf):
    """The failure direction matters: guessing SMALL on an unknown period would
    let an over-cap request through and Yahoo would answer with an empty frame,
    i.e. a blank chart instead of a graceful fall back to daily."""
    assert fetcher.intraday_history("AAPL", "30m", "nonsense") is None
    assert fake_yf.calls == []  # never even asked


# --------------------------- cap guard / fallback ---------------------------


@pytest.mark.parametrize(
    "interval,period,ok",
    [
        ("30m", "60d", True),  # exactly at Yahoo's 60-day cap for 30m
        ("30m", "90d", False),
        ("1h", "1y", True),  # 1h reaches 730 days
        ("1h", "3y", False),
        ("1m", "7d", True),
        ("1m", "30d", False),
    ],
)
def test_interval_caps(fake_yf, interval, period, ok):
    got = fetcher.intraday_history("AAPL", interval, period)
    assert (got is not None) is ok
    assert bool(fake_yf.calls) is ok, "an over-cap pair must not hit the network"


def test_ladder_pairs_are_all_within_their_caps():
    """The shipped ladder must never be self-defeating."""
    for rng, (interval, period) in fetcher._RANGE_INTRADAY.items():
        cap = fetcher._INTERVAL_MAX_DAYS[interval]
        assert fetcher._period_days(period) <= cap, f"{rng} exceeds the {interval} cap"


def test_fetch_window_is_wider_than_the_displayed_range():
    """The whole reason the ladder over-fetches: an SMA 200 needs 200 bars of
    warm-up, so the fetch window must comfortably exceed the visible one."""
    months = {"1M": 1, "3M": 3, "6M": 6}
    for rng, (_, period) in fetcher._RANGE_INTRADAY.items():
        assert fetcher._period_days(period) > months[rng] * 30.44 * 1.5


# ------------------------------ range_history -------------------------------


def test_range_history_intraday(fake_yf):
    out = fetcher.range_history("AAPL", "1M", ["SPY"])
    assert out["fallback"] is False
    assert out["interval"] == "30m"
    assert len(out["history"]) == 100
    assert "SPY" in out["benchmarks"]
    # Benchmarks MUST be on the same bar frequency, or the overlay renders as a
    # staircase against a smooth price line.
    intervals = {c["interval"] for c in fake_yf.calls}
    assert intervals == {"30m"}


def test_range_history_daily_ranges_fall_back(fake_yf):
    for rng in ("YTD", "1Y", "5Y", "MAX"):
        out = fetcher.range_history("AAPL", rng)
        assert out["fallback"] is True
        assert "history" not in out  # client keeps drawing the daily payload
    assert fake_yf.calls == []


def test_range_history_falls_back_when_no_intraday_data(fake_yf):
    fake_yf.frames = {"*": pd.DataFrame()}
    out = fetcher.range_history("SOMEADR.XX", "1M")
    assert out["fallback"] is True and out["reason"] == "no intraday data"


def test_3m_and_6m_share_one_fetch(fake_yf):
    fetcher.range_history("AAPL", "3M")
    n_after_3m = len(fake_yf.calls)
    fetcher.range_history("AAPL", "6M")
    assert len(fake_yf.calls) == n_after_3m, "6M must reuse the 3M payload"


def test_empty_result_is_cached_but_a_raised_error_is_not(fake_yf):
    """A listing with no intraday bars shouldn't re-hit Yahoo on every range
    click; a transient exception should retry."""
    fake_yf.frames = {"*": pd.DataFrame()}
    assert fetcher.intraday_history("X", "30m", "60d") is None
    assert fetcher.intraday_history("X", "30m", "60d") is None
    assert len(fake_yf.calls) == 1, "the empty result should have been cached"

    cache._CACHE.clear()
    fake_yf.calls = []

    class _Boom(_FakeTicker):
        def history(self, **kw):
            _FakeTicker.calls.append(kw)
            raise RuntimeError("network")

    fetcher.yf.Ticker = _Boom
    assert fetcher.intraday_history("Y", "30m", "60d") is None
    assert fetcher.intraday_history("Y", "30m", "60d") is None
    assert len(fake_yf.calls) == 2, "a transient failure must not be cached"


def test_single_bar_is_rejected(fake_yf):
    """One point can't be drawn or measured; treat it as no data."""
    fake_yf.frames = {"*": _frame(1)}
    assert fetcher.intraday_history("AAPL", "30m", "60d") is None


class FakeTk:
    def __init__(self, sym):
        self.fast_info = type("FI", (), {"timezone": "Europe/Amsterdam", "last_price": 30.0})()
        idx = pd.DatetimeIndex(
            ["2026-07-29 21:00", "2026-07-29 21:00", "2026-04-30 00:00", "2026-01-28 10:00"],
            tz="America/New_York",
        )
        self._ed = pd.DataFrame(
            {
                "EPS Estimate": [0.65, 0.65, 0.50, 0.45],
                "Reported EPS": [float("nan"), 0.68, 0.54, 0.48],
                "Surprise(%)": [float("nan"), 5.19, 7.59, 6.95],
            },
            index=idx,
        )
        today = pd.Timestamp.now(tz="Europe/Amsterdam").normalize()
        self.dividends = pd.Series(
            [0.71, 0.40, 0.80],
            index=[
                today - pd.Timedelta(days=500),
                today - pd.Timedelta(days=200),
                today - pd.Timedelta(days=30),
            ],
        )

    def get_earnings_dates(self, limit=60):
        return self._ed


def test_events_read_timing_dedupe_and_yield(monkeypatch):
    monkeypatch.setattr(fetcher.yf, "Ticker", FakeTk)
    out = fetcher.fetch_events("ZZEV.AS")
    days = [e["date"] for e in out["earnings"]]
    assert days == sorted(set(days))  # one row per report
    q2 = next(e for e in out["earnings"] if e["date"] == "2026-07-30")
    # 21:00 New York is 03:00 in Amsterdam the next day: before the open.
    assert q2["actual"] == 0.68 and q2["timing"] == "before" and q2["react"] == "2026-07-30"
    jan = next(e for e in out["earnings"] if e["date"] == "2026-01-28")
    assert jan["timing"] == "after" and jan["react"] == "2026-01-29"  # 16:00 local
    assert next(e for e in out["earnings"] if e["date"] == "2026-04-30")["timing"] == ""
    assert out["div_ttm"] == pytest.approx(1.20)
    assert out["div_yield_ttm"] == pytest.approx(1.20 / 30.0)
