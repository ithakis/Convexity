"""Track record statistics on synthetic history with a KNOWN answer."""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from convexity import news_diagnostics as nd  # noqa: E402

N_DAYS, N_SYMS = 70, 30


def _world(seed=3):
    """Closes for 30 names + SPY over 80 business days, beta 1 everywhere."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2026-06-01", periods=N_DAYS + 10)
    m = rng.normal(0, 0.01, len(idx))
    data = {"SPY": 100 * np.cumprod(1 + m)}
    idio = {}
    for k in range(N_SYMS):
        e = rng.normal(0, 0.02, len(idx))
        idio[f"S{k}"] = e
        data[f"S{k}"] = 100 * np.cumprod(1 + m + e)
    return pd.DataFrame(data, index=idx), idio, idx


def _records(idx, closes, score_fn, tiers=True):
    recs = []
    for d_i in range(N_DAYS):
        d = idx[d_i]
        for k in range(N_SYMS):
            s = f"S{k}"
            fwd = (closes[s].iloc[d_i + 1] / closes[s].iloc[d_i] - 1.0) \
                - (closes["SPY"].iloc[d_i + 1] / closes["SPY"].iloc[d_i] - 1.0)
            score = score_fn(fwd)
            rec = {"date": d.strftime("%Y-%m-%d"), "symbol": s, "beta": 1.0,
                   "news_score": score, "market_score": score, "market_z": score}
            if tiers:
                t = "bullish" if score > 0.01 else "bearish" if score < -0.01 else "neutral"
                rec.update(news_tier=t, market_tier=t if t != "neutral" else "no_edge")
            recs.append(rec)
    return recs


def test_planted_signal_is_found():
    closes, _, idx = _world()
    rng = random.Random(1)
    recs = _records(idx, closes, lambda f: f + rng.gauss(0, 0.02))
    out = nd.compute(recs, closes=closes, market_horizon=1)
    news = out["news"]
    assert news["ic"]["1d"]["n_days"] == N_DAYS
    assert news["ic"]["1d"]["mean"] > 0.3 and news["ic"]["1d"]["t"] > 5
    assert news["verdict"]["key"] == "edge"
    assert out["market"]["verdict"]["key"] == "edge"
    # the long-short curve rises and the hit rates beat a coin
    assert news["long_short"][-1]["cum_pct"] > 0
    assert news["hit_rate"]["bullish"]["rate"] > 0.5


def test_shuffled_scores_show_no_evidence():
    closes, _, idx = _world()
    rng = random.Random(7)
    recs = _records(idx, closes, lambda f: rng.gauss(0, 0.02))
    out = nd.compute(recs, closes=closes, market_horizon=1)
    ic = out["news"]["ic"]["1d"]
    assert abs(ic["mean"]) < 0.05
    assert out["news"]["verdict"]["key"] == "none"


def test_too_early_below_forty_days():
    closes, _, idx = _world()
    recs = [r for r in _records(idx, closes, lambda f: f)
            if r["date"] <= idx[29].strftime("%Y-%m-%d")]
    v = nd.compute(recs, closes=closes)["news"]["verdict"]
    assert v["key"] == "too_early" and v["text"] == "Too early — 30 of ~60 trading days"


@pytest.mark.parametrize("n_days,t,mean,key", [
    (39, 9.0, 0.1, "too_early"), (40, 2.0, 0.01, "edge"), (40, 2.5, -0.01, "none"),
    (60, 1.0, 0.02, "weak"), (60, 1.99, 0.02, "weak"), (60, 0.99, 0.02, "none"),
    (60, None, None, "none"),
])
def test_verdict_rule(n_days, t, mean, key):
    assert nd.verdict({"n_days": n_days, "t": t, "mean": mean})["key"] == key


def test_wilson_interval_matches_reference():
    w = nd.wilson(8, 10)
    assert w["rate"] == 0.8
    assert w["lo"] == pytest.approx(0.4902, abs=1e-3)
    assert w["hi"] == pytest.approx(0.9433, abs=1e-3)
    small = nd.wilson(2, 3)
    assert 0 < small["lo"] < small["rate"] < small["hi"] < 1      # never below 0
    assert nd.wilson(0, 0)["rate"] is None


def test_clustered_t_plain_and_newey_west():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    st = nd.clustered_mean_t(x, 1)
    assert st["mean"] == 3.0
    assert st["t"] == pytest.approx(3.0 / (math.sqrt(2.5) / math.sqrt(5)))
    # Positively autocorrelated series: NW inflates the variance, shrinks t
    rng = np.random.default_rng(0)
    e = rng.normal(0.02, 0.05, 400)
    smooth = list(np.convolve(e, np.ones(5) / 5, mode="valid"))
    assert nd.clustered_mean_t(smooth, 5)["t"] < nd.clustered_mean_t(smooth, 1)["t"]
    assert nd.clustered_mean_t([0.1, 0.2], 1)["t"] is None


def test_one_date_is_one_observation():
    """Pooling would count 30 names x 70 days = 2100 observations; the
    clustered statistic must count 70."""
    closes, _, idx = _world()
    out = nd.compute(_records(idx, closes, lambda f: f), closes=closes, market_horizon=1)
    assert out["news"]["ic"]["1d"]["n_days"] == N_DAYS
    assert len(out["quants"]["daily_ic"]["news"]["1d"]) == N_DAYS


def test_compat_loader_maps_only_the_two_scores():
    raw = [{"date": "2026-08-01", "symbol": "A", "s_idio": 0.3, "ml_sar": -0.01,
            "tier": "bearish", "ml_tier": "bearish"},
           {"date": "2026-08-01", "symbol": "__market__", "s_idio": 0.1},
           {"date": "2026-09-25", "symbol": "B", "news_score": 1.2, "news_tier": "bullish"}]
    recs = nd.load_records(raw)
    assert [r["symbol"] for r in recs] == ["A", "B"]
    assert recs[0]["news_score"] == 0.3 and recs[0]["market_sar"] == -0.01
    assert "news_tier" not in recs[0] and "market_tier" not in recs[0]


def test_empty_history_is_well_formed():
    out = nd.compute([], closes=None)
    assert out["news"]["verdict"]["key"] == "too_early"
    assert out["market"]["long_short"] == [] and out["quants"]["market_calibration"] == []


def test_per_lens_hit_rates_use_lens_scores():
    closes, _, idx = _world()
    recs = _records(idx, closes, lambda f: f)
    for r in recs:
        r["lens"] = {"financials": 2.0 if r["news_score"] > 0 else -2.0, "outlook": None}
    out = nd.compute(recs, closes=closes, market_horizon=1)
    fin = out["news"]["lens_hit_rate"]["financials"]
    assert fin["all"]["n"] > 0 and fin["all"]["rate"] > 0.5
    assert out["news"]["lens_hit_rate"]["outlook"]["all"]["n"] == 0
