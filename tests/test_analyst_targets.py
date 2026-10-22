"""analyst_targets: the app's one analyst number (CRWD, 2026-10-22)."""

import datetime as dt

import pandas as pd
import pytest

from convexity import analyst_targets as at

NOW = dt.datetime(2026, 10, 22, 12)


def _ud(rows):
    """rows: (days_ago, firm, action, from_grade, to_grade, target, prior)."""
    idx = pd.Index([pd.Timestamp(NOW - dt.timedelta(days=r[0])) for r in rows], name="GradeDate")
    return pd.DataFrame(
        {
            "Firm": [r[1] for r in rows],
            "Action": [r[2] for r in rows],
            "FromGrade": [r[3] for r in rows],
            "ToGrade": [r[4] for r in rows],
            "currentPriceTarget": [r[5] for r in rows],
            "priorPriceTarget": [r[6] for r in rows],
        },
        index=idx,
    )


def test_targets_before_a_split_are_split_adjusted():
    """CRWD split 4:1 on 2026-07-02; a June target of 800 is 200 today."""
    splits = pd.Series(
        [4.0], index=pd.DatetimeIndex([pd.Timestamp("2026-07-02 09:30", tz="America/New_York")])
    )
    ud = _ud(
        [
            (140, "Susquehanna", "main", "Positive", "Positive", 800.0, 475.0),
            (6, "Citi", "main", "Buy", "Buy", 295.0, 260.0),
        ]
    )
    firms = at.firm_targets(ud, splits, price=243.0, today=NOW)
    by = {f["firm"]: f for f in firms}
    assert by["Susquehanna"]["target"] == 200.0 and by["Susquehanna"]["split_adjusted"]
    assert by["Susquehanna"]["prior"] == pytest.approx(118.75)
    assert by["Citi"]["target"] == 295.0 and not by["Citi"]["split_adjusted"]
    assert at.summarise(firms)["mean"] == pytest.approx(247.5)


def test_a_firm_that_moves_without_a_target_has_none():
    """Guggenheim moved to Neutral with no target: its old Buy target must not
    keep counting (the old code dropped blank rows before taking the latest)."""
    ud = _ud(
        [
            (300, "Guggenheim", "main", "Buy", "Buy", 400.0, 380.0),
            (56, "Guggenheim", "down", "Buy", "Neutral", 0.0, 0.0),
            (10, "UBS", "main", "Buy", "Buy", 250.0, 235.0),
        ]
    )
    assert [f["firm"] for f in at.firm_targets(ud, None, 243.0, NOW)] == ["UBS"]


def test_a_reiteration_without_a_target_keeps_the_previous_one():
    ud = _ud(
        [
            (60, "RBC", "main", "Outperform", "Outperform", 260.0, 240.0),
            (10, "RBC", "reit", "Outperform", "Outperform", 0.0, 0.0),
        ]
    )
    (f,) = at.firm_targets(ud, None, 243.0, NOW)
    assert f["target"] == 260.0 and f["date"] == (NOW - dt.timedelta(days=60)).date().isoformat()


def test_old_and_out_of_band_targets_are_dropped_and_stale_flagged():
    ud = _ud(
        [
            (400, "Old", "main", "Buy", "Buy", 250.0, 0.0),
            (200, "Faded", "main", "Buy", "Buy", 240.0, 0.0),
            (5, "OtherLine", "init", "", "Buy", 2000.0, 0.0),
        ]
    )
    firms = at.firm_targets(ud, None, 243.0, NOW)
    assert [f["firm"] for f in firms] == ["Faded"] and firms[0]["stale"]


def test_no_firms_falls_back_to_the_summary():
    block = at.summarise([], {"mean": 246.65, "median": 250.0, "low": 132.0, "high": 325.0})
    assert block["source"] == "summary" and block["mean"] == 246.65 and block["n"] is None
    assert at.summarise([], {}) is None


def test_mean_median_low_high_come_from_one_list():
    firms = [{"target": t, "stale": False} for t in (100.0, 110.0, 150.0, 160.0)]
    b = at.summarise(firms, {"mean": 999.0})
    assert (b["mean"], b["median"], b["low"], b["high"], b["n"]) == (130.0, 130.0, 100.0, 160.0, 4)


def test_scaled_moves_every_price():
    b = at.summarise([{"target": 100.0, "prior": 90.0, "stale": False}])
    s = at.scaled(b, 0.5, via="X.US")
    assert s["mean"] == 50.0 and s["firms"][0]["prior"] == 45.0 and s["via"] == "X.US"
    assert at.scaled(b, 0) is None


def test_consensus_is_cached_and_a_summary_answer_is_asked_again_soon(monkeypatch):
    from convexity import cache

    puts = []
    monkeypatch.setattr(at, "_cache_put", lambda k, v, ttl: puts.append(ttl))

    class Tk:
        ticker = "ZZT1"
        upgrades_downgrades = None
        splits = None
        analyst_price_targets = {"current": 10.0, "mean": 12.0}

    assert at.consensus("ZZT1", tk=Tk())["source"] == "summary"
    assert puts == [at.MISS_TTL]  # might be a throttled answer
    cache._CACHE.pop("analyst_targets|ZZT1", None)


# ------------------------------------------------- relative views (optimizer)
def test_relative_alpha_is_small_centred_and_capped():
    a, ref = at.relative_alpha({"HI": 0.60, "MID": 0.11, "LO": -0.20})
    assert a["HI"]["alpha"] > 0 > a["LO"]["alpha"]
    cap = at.IC_PRIOR * at.SIGMA_XS * at.Z_CAP
    assert all(abs(v["alpha"]) <= cap + 1e-12 for v in a.values())
    assert abs(a["HI"]["z"]) <= at.Z_CAP and ref["ic"] == at.IC_PRIOR


def test_a_uniformly_bullish_street_is_not_a_bullish_view():
    """Every holding at +30%: the reference moves toward their own mean, so
    the tilts stay small; equal upsides give equal tilts."""
    a, _ = at.relative_alpha(dict.fromkeys("ABCDEFGHIJ", 0.3))
    vals = {round(v["alpha"], 12) for v in a.values()}
    assert len(vals) == 1 and next(iter(vals)) < at.IC_PRIOR * at.SIGMA_XS * at.Z_CAP


def test_view_ic_uses_the_prior_until_the_record_shows_evidence():
    from convexity import cache

    cache._CACHE.pop("analyst_targets|track_record", None)
    assert at.view_ic() == (at.IC_PRIOR, "prior")
    at._cache_put("analyst_targets|track_record", {"v": {"n_weeks": 60, "t": 2.4, "ic": 0.05}}, 60)
    assert at.view_ic() == (0.05, "measured")
    at._cache_put("analyst_targets|track_record", {"v": {"n_weeks": 30, "t": 3.0, "ic": 0.05}}, 60)
    assert at.view_ic() == (at.IC_PRIOR, "prior")
    cache._CACHE.pop("analyst_targets|track_record", None)


# ------------------------------------------------- the app's own record
def test_record_once_per_day_and_load_back():
    at._SEEN.clear()
    blk = {"mean": 120.0, "median": 118.0, "n": 9, "source": "firms", "age_days": 40}
    at.record("aaa", 100.0, blk)
    at.record("AAA", 101.0, blk)  # same day: ignored
    rows = at.load_history()
    assert len(rows) == 1 and rows[0]["s"] == "AAA" and rows[0]["p"] == 100.0


def test_track_record_scores_relative_upside_against_the_next_year():
    """Synthetic record a year and more old, where the most optimistic names
    did best: IC near 1; too few weeks for a verdict."""
    import json

    at._SEEN.clear()
    path = at._hist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    start = dt.date.today() - dt.timedelta(days=400)
    closes = {}
    lines = []
    for k in range(10):
        sym = f"S{k}"
        up = 0.02 * k
        idx = pd.date_range(start, periods=400, freq="D")
        closes[sym] = pd.Series(
            [100.0 if i < 300 else 100.0 * (1 + up) for i in range(400)], index=idx
        )
        for w in range(3):
            d = start + dt.timedelta(days=7 * w)
            lines.append(
                json.dumps({"d": d.isoformat(), "s": sym, "p": 100.0, "mean": 100 * (1 + up)})
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tr = at.track_record(closes=closes)
    assert tr["n_weeks"] == 3 and tr["ic"] == pytest.approx(1.0)
    assert tr["verdict"].startswith("Too early") and tr["n_symbols"] == 10
