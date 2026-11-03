"""The monthly long-history cache (long_run.py): what it reads, what it refuses
to save, and when it runs. Synthetic closes only; nothing here touches Yahoo."""

import threading

import numpy as np
import pandas as pd
import pytest

from convexity import jobs, long_run


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(long_run, "_path", lambda: tmp_path / "long_run.json")
    long_run.reset_memo()
    monkeypatch.setitem(long_run._STATE, "tried", {})
    monkeypatch.setitem(long_run._STATE, "next", 0.0)
    monkeypatch.setattr(long_run, "PAUSE_S", 0)
    yield
    long_run.reset_memo()


def _closes(n=3000, seed=1, start="2010-01-04"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.02, n))), index=idx)


def test_episodes_keeps_the_running_fall_apart():
    px = np.array([100, 80, 90, 100, 110, 95, 90], float)
    e = long_run.episodes(px)
    assert e["days"] == [3]  # 100 back to 100 took three days
    assert e["depths"] == [-20.0]
    assert e["open_days"] == 2 and e["open_depth"] == pytest.approx(-18.2, abs=0.1)
    assert e["open_now"] == e["open_depth"]


def test_episodes_ignores_a_small_dip():
    e = long_run.episodes(np.array([100, 95, 100, 105], float))
    assert e["days"] == [] and e["open_days"] is None


def test_old_is_measured_from_the_date_asked():
    idx = pd.bdate_range("2000-01-03", periods=4000)
    px = _closes(4000).to_numpy().copy()
    px[-200:] *= 0.1
    assert long_run.usable_start(idx, px) is None  # recent, by its own last row
    assert long_run.usable_start(idx, px, idx[-1] + pd.Timedelta(days=3650)) == idx[-200]


def test_every_stretch_below_the_running_high_is_a_dip():
    px = np.array([100, 99, 101, 102, 101, 100, 103, 90, 104, 95], float)
    assert long_run.dip_days(px) == ([2, 3, 2], 1)  # 104 then 95: one still running
    assert long_run.dip_days(np.array([1, 2, 3], float)) == ([], None)  # new highs aren't dips
    assert long_run.dip_days(np.array([100, 90, 100, 90, 100], float))[0] == [2, 2]  # a tie is back


def test_dip_bars_start_at_two_days_and_the_curve_shares_their_scale():
    days = [2] * 40 + [3] * 20 + [4] * 12 + [6] * 9 + [15] * 8 + [60] * 5 + [400] * 3
    d = long_run.dip_histogram(days, None, [60, 400, 400])
    assert (
        sum(d["counts"]) == d["n"] == len(days) and len(d["bins"]) == 31 and len(d["curve"]) == 30
    )
    assert d["counts"][0] == 40 and d["counts"][1] == 20  # a bar each for two days and three
    assert sum(d["big"]) == 3 and all(b <= c for b, c in zip(d["big"], d["counts"], strict=True))
    assert sum(d["curve"]) == pytest.approx(len(days), abs=0.1)
    assert d["curve"][0] == max(d["curve"])  # most of the shape is the shortest
    assert d["q50"] == 3 and d["q90"] == 15 and d["running"] is None
    assert d["lo"] == 2 and d["hi"] == 400


def test_a_running_dip_is_counted_and_stretches_the_curve():
    days = [2, 3, 4, 5, 8, 12, 20, 40] * 5
    plain = long_run.dip_histogram(days, None, [])
    still = long_run.dip_histogram(days, 3000, [])
    assert sum(still["curve"]) == pytest.approx(len(days) + 1, abs=0.1) and still["running"] == 3000
    assert sum(still["curve"][-8:]) > sum(plain["curve"][-8:])
    assert long_run.dip_histogram([], 30, []) is None
    assert plain["big"] == [0] * 30


def test_fit_says_how_long_a_fall_takes():
    f = long_run.recovery_fit([10, 20, 40, 80, 160, 320, 640], None)
    assert 0 < f["q50"] < f["q90"]


def test_fit_needs_a_finished_fall():
    assert long_run.recovery_fit([], 100) is None


def test_running_fall_stretches_the_fit():
    days = [30, 40, 50, 60]
    assert long_run.recovery_fit(days, 2000)["q50"] > long_run.recovery_fit(days, None)["q50"]


def test_gate_keeps_a_recent_crash():
    idx = pd.bdate_range("2010-01-04", periods=4000)
    px = _closes(4000).to_numpy().copy()
    for ago in (100, 300):
        crash = px.copy()
        crash[-ago:] *= 0.2  # -80% in a day, a real one: the card exists for this
        assert long_run.usable_start(idx, crash) is None
        e = long_run.episodes(crash)
        assert e["open_days"] and e["open_now"] < -60


def test_an_unadjusted_split_is_repaired_by_its_shape():
    idx = pd.bdate_range("2000-01-03", periods=4000)
    px = np.full(4000, 100.0) * np.exp(np.cumsum(np.full(4000, 0.0001) * (-1) ** np.arange(4000)))
    px = px * (1 + 0.002 * (-1) ** np.arange(4000))
    split = px.copy()
    split[1000:] *= 0.5
    fixed = long_run.repair_splits(idx, split)
    assert fixed[1000:] == pytest.approx(split[1000:])
    assert fixed[:1000] == pytest.approx(split[:1000] * 0.5)  # the years before stay, rescaled
    assert long_run.usable_start(idx, fixed) is None and not long_run.episodes(fixed)["days"]
    loud = split.copy()
    loud[1001:] *= 0.9  # a crash with a loud day beside it isn't a split
    assert long_run.repair_splits(idx, loud) == pytest.approx(loud)
    recent = px.copy()
    recent[-200:] *= 0.5  # nor is a recent one: the card is for those
    assert long_run.repair_splits(idx, recent) == pytest.approx(recent)


def test_a_thin_young_line_leaves_its_stand_ins_years_alone():
    idx = pd.bdate_range("2005-01-03", periods=4000)
    px = _closes(4000).to_numpy().copy()
    px[1000:1200] *= 0.7  # a fall in the stand-in's stretch
    px[3400:3800] = px[3400]  # the holding's own line, hardly trading at first
    own_from = str(idx[3400])[:10]
    alone = long_run._record(_pts(idx, px), "2026-11", "AAPL")
    kept = long_run._record(_pts(idx, px), "2026-11", "AAPL", own_from)
    assert alone["used"] < 300 and alone["since"] > own_from
    assert kept["since"] == str(idx[0])[:10] and kept["used"] > 3400 and kept["n"] == 4000
    assert len(kept["days"]) > len(alone["days"]) and kept["proxy"] == "AAPL"
    # a fake fall just before the handover isn't "recent" because that stretch ended there
    fake = px.copy()
    fake[2600:2800] *= 0.1  # over five years from the last row, under five from the handover
    cut = long_run._record(_pts(idx, fake), "2026-11", "AAPL", own_from)
    assert cut["since"] > str(idx[2600])[:10]
    # a bad sign before the handover is the stand-in's own: it cuts as before
    px[500:] *= 3
    early = long_run._record(_pts(idx, px), "2026-11", "AAPL", own_from)
    assert early["since"] > str(idx[500])[:10]


def test_volatility_sets_the_scale():
    days = [40, 60, 90]
    calm = long_run.recovery_fit(days, None, [0.3] * 3, 0.15)
    wild = long_run.recovery_fit(days, None, [0.3] * 3, 0.6)
    base = long_run.recovery_fit(days, None, [0.3] * 3, 0.3)
    assert calm["q50"] > base["q50"] > wild["q50"]
    assert long_run.recovery_fit(days, None)["q50"] == base["q50"]  # none to read: the middle
    # clamped to the panel's range, and past falls are read against their own volatility
    assert long_run.recovery_fit(days, None, [0.3] * 3, 5.0)["q50"] == long_run.recovery_fit(days, None, [0.3] * 3, 0.73)["q50"]  # fmt: skip
    assert long_run.recovery_fit(days, None, [0.6] * 3, 0.3)["q50"] > base["q50"]


def test_a_falls_volatility_is_from_before_its_high():
    calm = np.exp(np.cumsum(np.tile([0.012, -0.01], 500)))
    wild = calm[-1] * np.exp(np.cumsum(np.tile([-0.2, 0.1], 20)))
    e = long_run.episodes(np.r_[calm, wild])
    assert e["open_days"] and e["vol_now"] == pytest.approx(0.011 * 252**0.5, rel=0.05)


def test_gate_cuts_a_hole_an_old_jump_and_a_dead_stretch():
    idx = pd.bdate_range("2000-01-03", periods=4000)
    px = _closes(4000).to_numpy().copy()
    assert long_run.usable_start(idx, px) is None
    jump = px.copy()
    jump[800:] *= 3  # a +200% day, long ago: an unadjusted split, or bad data
    assert long_run.usable_start(idx, jump) == idx[800]
    hole = idx.delete(range(500, 560))
    assert long_run.usable_start(hole, px[: len(hole)]) == hole[500]
    dead = px.copy()
    dead[:600] = dead[0]  # no trades for two years
    assert long_run.usable_start(idx, dead) >= idx[251]


def test_gate_cuts_a_whipsaw():
    idx = pd.bdate_range("2000-01-03", periods=1000)
    px = _closes(1000).to_numpy().copy()
    for i in (200, 240, 280):  # three moves undone within days, none alone a jump
        px[i : i + 3] *= 1.6
    assert long_run.usable_start(idx, px) >= idx[280]


def _pts(idx, px):
    return [(str(d)[:10], float(v)) for d, v in zip(idx, px, strict=True)]


def test_record_starts_after_the_gate():
    idx = pd.bdate_range("2000-01-03", periods=4000)
    px = _closes(4000).to_numpy().copy()
    px[1000:] *= 3
    r = long_run._record(_pts(idx, px), "2026-11", None)
    assert r["since"] == str(idx[1001])[:10] and r["n"] == 4000 and r["used"] == 2999


def test_record_with_too_little_left_defers_to_the_ten_years():
    idx = pd.bdate_range("2000-01-03", periods=500).delete(range(470, 490))  # a hole near the end
    px = _closes(len(idx)).to_numpy()
    long_run._save(
        {
            "v": long_run.VERSION,
            "symbols": {"AAPL": long_run._record(_pts(idx, px), "2026-11", None)},
            "cov": {},
        }
    )
    assert long_run.recovery("AAPL") is None


def test_from_here_follows_the_distance_left():
    near = long_run.from_here(-5.0, 800, -60.0)
    far = long_run.from_here(-50.0, 800, -60.0)
    assert near["q50"] < far["q50"] and far["q90"] == pytest.approx(far["q50"] * 4.68, rel=0.01)


def test_from_here_matches_the_study_examples():
    assert long_run.from_here(-50.0, 605, -50.0)["q50"] == pytest.approx(1186, rel=0.01)
    assert long_run.from_here(-12.0, 42, -12.0)["q50"] == pytest.approx(136, rel=0.02)


def test_from_here_gives_no_figure_past_every_fall_on_record():
    assert long_run.from_here(-95.0, 2000, -95.0) == {"q50": None, "q90": None, "beyond": True}


def test_from_here_says_when_it_outruns_the_record():
    assert long_run.from_here(-50.0, 605, -50.0)["beyond"] is True
    assert long_run.from_here(-12.0, 42, -12.0)["beyond"] is False


def test_from_here_needs_a_running_fall():
    assert long_run.from_here(None, None, None) is None


def _wide(n=900):
    a, b = _closes(n, 1, "2022-01-03"), _closes(n, 2, "2022-01-03")
    return pd.DataFrame({"AAPL": a, "MSFT": b})


def test_weekly_cov_is_symmetric_and_positive():
    names, m = long_run.weekly_cov(_wide())
    assert names == ["AAPL", "MSFT"]
    assert np.allclose(m, m.T) and np.linalg.eigvalsh(m).min() > 0


def test_weekly_cov_leaves_out_a_line_too_young():
    w = _wide()
    w["NVDA"] = _closes(900, 3, "2022-01-03")
    w.loc[w.index[:-30], "NVDA"] = np.nan  # six weeks of closes
    assert long_run.weekly_cov(w)[0] == ["AAPL", "MSFT"]


def test_weekly_cov_survives_no_overlap():
    w = _wide()
    w.iloc[450:, 0] = np.nan
    w.iloc[:450, 1] = np.nan
    names, m = long_run.weekly_cov(w)
    assert names == ["AAPL", "MSFT"] and np.isfinite(m).all()


def _kept(symbols, cov, month="2026-11", ccy="USD"):
    long_run._save(
        {
            "v": long_run.VERSION,
            "symbols": {},
            "cov": {ccy: {"month": month, "symbols": symbols, "asked": symbols, "cov": cov}},
        }
    )


def test_book_view_shares_add_up_and_the_month_is_scaled():
    _kept(["AAPL", "MSFT"], [[0.09, 0.03], [0.03, 0.04]])
    v = long_run.book_view(
        [{"symbol": "AAPL", "value": 600.0}, {"symbol": "MSFT", "value": 400.0}], "USD"
    )
    assert sum(v["shares"].values()) == pytest.approx(1)
    sigma = np.sqrt(0.36 * 0.09 + 0.16 * 0.04 + 2 * 0.24 * 0.03)
    assert v["bad_month"]["pct"] == pytest.approx(1.645 * sigma * np.sqrt(21 / 252) * 100, abs=0.01)
    assert v["bad_month"]["value"] == pytest.approx(v["bad_month"]["pct"] * 10, abs=0.1)


def test_book_view_can_read_negative_shares():
    _kept(["AAPL", "SPY"], [[0.04, -0.03], [-0.03, 0.09]])
    v = long_run.book_view(
        [{"symbol": "AAPL", "value": 900.0}, {"symbol": "SPY", "value": 100.0}], "USD"
    )
    assert sum(v["shares"].values()) == pytest.approx(1)
    assert min(v["shares"].values()) < 0


def test_book_view_stays_quiet_when_too_little_is_measured():
    _kept(["AAPL"], [[0.09]])
    pos = [{"symbol": "AAPL", "value": 500.0}, {"symbol": "MSFT", "value": 500.0}]
    assert long_run.book_view(pos, "USD") == {}
    assert long_run.book_view(pos, "EUR") == {}  # another currency's matrix isn't this one


@pytest.mark.parametrize(
    "body",
    [
        {"v": long_run.VERSION, "symbols": [1]},
        {"v": long_run.VERSION, "symbols": {"AAPL": 5}},
        {"v": long_run.VERSION, "symbols": "x", "cov": "y"},
        [1],
        {"v": 99},
    ],
)
def test_a_wrong_shape_reads_as_missing(tmp_path, body):
    import json

    (tmp_path / "long_run.json").write_text(json.dumps(body))
    long_run.reset_memo()
    assert long_run.recovery("AAPL") is None
    assert long_run.due(["AAPL"], "USD", month="2026-11") == (["AAPL"], True)
    assert long_run.book_view([{"symbol": "AAPL", "value": 1.0}], "USD") == {}


def test_an_absurd_matrix_is_no_figure():
    _kept(["AAPL"], [[100.0]])
    assert long_run.book_view([{"symbol": "AAPL", "value": 1.0}], "USD") == {}


def test_cov_ignores_a_single_bad_day(monkeypatch):
    from convexity import fx, investments

    frames = _wide(1300)
    held = [{"symbol": s, "quote_ccy": "USD", "value": 100.0} for s in frames.columns]
    bad = frames.copy()
    bad.iloc[1200, 0] *= 52  # the x52 day Yahoo really sends
    monkeypatch.setattr(fx, "_apply_fx_to_closes", lambda c, *a: c)
    for name, frame in (("clean", frames), ("bad", bad)):
        monkeypatch.setattr(investments, "_total_closes", lambda s, p, retry=True, f=frame: f)
        long_run._fill_cov(held, "USD", threading.Event(), "2026-11")
        got = long_run.book_view(held, "USD")["bad_month"]["pct"]
        if name == "clean":
            base = got
    assert got == pytest.approx(base, rel=0.05)


def test_a_cancelled_cov_saves_nothing(monkeypatch):
    from convexity import investments

    ev = threading.Event()
    ev.set()
    monkeypatch.setattr(investments, "_total_closes", lambda s, p, retry=True: _wide())
    long_run._fill_cov([{"symbol": "AAPL", "quote_ccy": "USD", "value": 1.0}], "USD", ev, "2026-11")
    assert long_run.load()["cov"] == {}


def test_a_pass_writes_onto_what_is_kept_now():
    long_run._update(symbols={"AAPL": {"month": "2026-11"}})
    long_run._update(cov={"USD": {"month": "2026-11"}})
    long_run._update(symbols={"MSFT": {"month": "2026-11"}})
    body = long_run.load()
    assert set(body["symbols"]) == {"AAPL", "MSFT"} and "USD" in body["cov"]


def test_the_card_never_calls_a_falling_holding_unfallen(monkeypatch):
    from convexity import investments

    rec = {"fit": None, "falls": 0, "open_days": None}
    monkeypatch.setattr(long_run, "recovery", lambda s: rec)
    assert investments._long_record("AAPL", {"open_days": 30}) is None
    assert investments._long_record("AAPL", {"open_days": None}) is rec


def test_check_now_doesnt_wait_for_the_monthly_pass(monkeypatch):
    from convexity import notify

    class Long:
        phases = ("longrun",)

    seen = []
    monkeypatch.setattr(notify, "settings", lambda: {"notify_on": True})
    monkeypatch.setattr(jobs, "current", lambda: Long())
    monkeypatch.setattr(notify, "_submit", lambda c="reject": seen.append(c) or (None, "created"))
    notify.run_now()
    assert seen == ["supersede"]


def test_malformed_file_reads_as_empty(tmp_path):
    (tmp_path / "long_run.json").write_text("{nope")
    long_run.reset_memo()
    assert long_run.load()["symbols"] == {}
    assert long_run.recovery("AAPL") is None


def test_due_follows_the_month_and_new_holdings():
    long_run._save({"v": long_run.VERSION, "symbols": {"AAPL": {"month": "2026-11"}, "MSFT": {"month": "2026-10"}},
                    "cov": {"USD": {"month": "2026-11", "symbols": ["AAPL"], "asked": ["AAPL", "MSFT"]}}})  # fmt: skip
    assert long_run.due(["AAPL", "MSFT"], "USD", month="2026-11") == (["MSFT"], False)
    assert long_run.due(["AAPL", "MSFT", "NVDA"], "USD", month="2026-11") == (
        ["MSFT", "NVDA"],
        True,
    )
    assert long_run.due(["AAPL"], "USD", month="2026-12") == (["AAPL"], True)
    assert long_run.due(["AAPL"], "EUR", month="2026-11")[1] is True


def _stub(monkeypatch, frames, calls=None):
    from convexity import analytics, investments

    def closes(symbols, period, retry=True):
        if calls is not None:
            calls.append((list(symbols), period, retry))
        cols = {s: frames[s] for s in symbols if s in frames}
        return pd.DataFrame(cols)

    monkeypatch.setattr(investments, "_total_closes", closes)
    monkeypatch.setattr(analytics, "extend_history", lambda w, *a, **k: (w, {}))


HELD = [
    {"symbol": s, "quote_ccy": "USD", "name": s, "value": 100.0}
    for s in ("AAPL", "MSFT", "NVDA", "SPY", "JPM")
]


def test_a_pass_asks_in_chunks_without_retries(monkeypatch):
    calls = []
    _stub(monkeypatch, {p["symbol"]: _closes(seed=i) for i, p in enumerate(HELD)}, calls)
    assert long_run._fill_symbols([p["symbol"] for p in HELD], HELD, threading.Event(), "2026-11")
    assert [len(c[0]) for c in calls] == [4, 1] and all(
        c[1] == "max" and c[2] is False for c in calls
    )
    rec = long_run.recovery("AAPL")
    assert rec["since"] == "2010-01-04" and rec["fit"]["q50"] > 0


def test_a_shorter_answer_never_replaces_the_record(monkeypatch):
    _stub(monkeypatch, {"AAPL": _closes(3000)})
    long_run._fill_symbols(["AAPL"], HELD, threading.Event(), "2026-10")
    _stub(monkeypatch, {"AAPL": _closes(400)})
    long_run._fill_symbols(["AAPL"], HELD, threading.Event(), "2026-11")
    assert long_run.load()["symbols"]["AAPL"]["month"] == "2026-10"
    assert long_run._STATE["tried"]["AAPL"] > 0  # and it isn't asked again every minute


def test_yahoo_silent_ends_the_pass(monkeypatch):
    calls = []
    _stub(monkeypatch, {}, calls)
    syms = [f"S{i}" for i in range(20)]
    assert long_run._fill_symbols(syms, HELD, threading.Event(), "2026-11") is False
    assert len(calls) == long_run.MAX_FAILED_CHUNKS


def test_a_cancelled_pass_stops_asking(monkeypatch):
    calls = []
    _stub(monkeypatch, {}, calls)
    ev = threading.Event()
    ev.set()
    assert long_run._fill_symbols(["AAPL"], HELD, ev, "2026-11") is True
    assert calls == []


def test_tick_waits_for_a_running_job(monkeypatch):
    class Busy:
        def is_terminal(self):
            return False

    monkeypatch.setattr(long_run, "_held_now", lambda: (HELD, "USD"))
    submitted = []
    monkeypatch.setattr(jobs, "current", lambda: Busy())
    assert long_run.tick(submit=lambda: submitted.append(1)) is None and not submitted
    monkeypatch.setattr(jobs, "current", lambda: None)
    long_run.tick(submit=lambda: submitted.append(1))
    assert submitted == [1]


def test_tick_is_quiet_when_nothing_is_due(monkeypatch):
    month = long_run._month()
    long_run._save({"v": long_run.VERSION, "symbols": {"AAPL": {"month": month}},
                    "cov": {"USD": {"month": month, "symbols": ["AAPL"], "asked": ["AAPL"], "cov": [[0.04]]}}})  # fmt: skip
    monkeypatch.setattr(long_run, "_held_now", lambda: ([{"symbol": "AAPL"}], "USD"))
    monkeypatch.setattr(jobs, "current", lambda: None)
    assert long_run.tick(submit=lambda: pytest.fail("nothing was due")) is None


def test_the_page_overlay_is_a_copy(monkeypatch):
    from convexity import investments

    _kept(["AAPL", "MSFT"], [[0.09, 0.03], [0.03, 0.04]])
    out = {
        "ccy": "USD",
        "positions": [{"symbol": "AAPL", "value": 600.0}, {"symbol": "MSFT", "value": 400.0}],
    }
    got = investments._with_long_run(out)
    assert got["bad_month"]["pct"] > 0 and got["positions"][0]["risk_share_3y"] > 0
    assert "bad_month" not in out and "risk_share_3y" not in out["positions"][0]


def test_the_page_runs_without_the_matrix():
    from convexity import investments

    out = {"ccy": "USD", "positions": [{"symbol": "AAPL", "value": 600.0}]}
    assert investments._with_long_run(out) is out
