"""Point-in-time (leakage) unit test for the label pipeline's stage A.

Constructs a synthetic price series with a huge one-day jump and asserts the
trailing sigma/beta stored AT the jump date do not include the jump — i.e.
every stat dated D was computed from data <= D-1 only (the shift(1) contract
the SAR label depends on).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")


def _load_labels_module():
    spec = importlib.util.spec_from_file_location(
        "labels05", ROOT / "ml" / "scripts" / "05_build_labels.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stage_a_stats_are_point_in_time(tmp_path, monkeypatch):
    mod = _load_labels_module()
    from ml import config

    n = 400
    jump_i = 350
    dates = pd.bdate_range("2020-01-01", periods=n).date
    rng = np.random.default_rng(1)
    r = rng.normal(0, 0.01, n)
    r[jump_i] = 0.60  # the jump that must NOT leak into stats dated jump day
    close = 100 * np.cumprod(1 + r)
    prices = pd.DataFrame(
        {
            "symbol": "TEST",
            "date": dates,
            "open": (close * 0.999).astype("float32"),
            "high": (close * 1.01).astype("float32"),
            "low": (close * 0.99).astype("float32"),
            "close": close.astype("float32"),
            "adj_close": close.astype("float32"),
            "volume": np.int64(1_000_000),
        }
    )
    cal = pd.DataFrame(
        {
            "date": dates,
            "next_date": np.roll(dates, -1),
            "prev_date": np.roll(dates, 1),
            "m_overnight": rng.normal(0, 0.002, n),
            "m_intraday": rng.normal(0, 0.006, n),
            "m_cc": rng.normal(0, 0.008, n),
        }
    )
    prices_p = tmp_path / "prices.parquet"
    cal_p = tmp_path / "calendar.parquet"
    stats_p = tmp_path / "stats.parquet"
    prices.to_parquet(prices_p, index=False)
    cal.to_parquet(cal_p, index=False)
    monkeypatch.setattr(config, "PRICES_PARQUET", prices_p)
    monkeypatch.setattr(config, "CALENDAR_PARQUET", cal_p)
    monkeypatch.setattr(config, "TICKER_STATS_PARQUET", stats_p)

    mod.stage_a()

    stats = pd.read_parquet(stats_p).set_index("date")
    jump_day = dates[jump_i]
    after = dates[jump_i + 1]

    # sigma dated the jump day used only data <= day-1 -> still calm
    assert stats.loc[jump_day, "sigma_cc"] < 0.05
    # the day after, the 60% move is inside the window -> sigma explodes
    assert stats.loc[after, "sigma_cc"] > 2 * stats.loc[jump_day, "sigma_cc"]

    # beta must exist and be Blume-clipped into [0, 3]
    b = stats["beta"].dropna()
    assert len(b) > 0
    assert float(b.min()) >= 0.0 and float(b.max()) <= 3.0
