"""fetcher.fetch_events: the stock window's E and D flags (no network)."""

import pandas as pd
import pytest

from convexity import fetcher


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
