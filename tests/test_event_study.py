"""The Events view's pipeline (P6, event_study.py): which moves count, the
Market Reaction and its label, the event outcomes, the market bands, the
code's sentences, the checks on the model's clause, the rail's pick, the
caches and the background pass. Synthetic data and public tickers only."""

import json
import math
import threading
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from convexity import event_study as es
from convexity import investments, jobs

TODAY = date(2026, 11, 18)


# ------------------------------------------------------------- fixtures


def _days(n, end="2026-11-17"):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(end=end, periods=n)]


def _bars(closes, days, vol=1e6):
    c = np.asarray(closes, float)
    return es.clean_bars(days, c, c * 1.01, c * 0.99, c, np.full(len(c), vol))


def _pair(n=700, seed=3, beta=0.8, noise=0.012, end="2026-11-17"):
    """A stock and an index: the stock is beta x the index plus its own noise."""
    rng = np.random.default_rng(seed)
    m = rng.normal(0, 0.01, n)
    r = beta * m + rng.normal(0, noise, n)
    return m, r, _days(n, end)


def _frame(m, r, days):
    ci, cs = 100 * np.exp(np.cumsum(m)), 50 * np.exp(np.cumsum(r))
    idx = _bars(ci, days)
    idx["symbol"] = "^GSPC"
    return _bars(cs, days), idx, ci


def _index_z(ci, days):
    lr = np.concatenate([[np.nan], np.diff(np.log(ci))])
    return {d: float(z) for d, z in zip(days, es.z_scores(lr), strict=True) if np.isfinite(z)}


# ---------------------------------------------------------- arithmetic


def test_broad_index_is_the_sp_for_us_lines_and_the_stoxx_for_european_ones():
    assert es.broad_index("AAPL") == ("^GSPC", "S&P 500")
    assert es.broad_index("ZZNOVN.SW")[0] == "^STOXX"
    assert es.broad_index("ZZSHEL.L")[0] == "^STOXX"
    assert es.broad_index("ZZ.T")[0] == "1306.T"  # elsewhere: capm's home index


def test_a_bad_print_is_dropped_and_its_returns_merge():
    days = _days(6)
    c = [100, 101, 130, 102, 103, 104]  # +25% then back: a bad print
    b = _bars(c, days)
    assert days[2] not in b["d"] and len(b["d"]) == 5
    real = _bars([100, 101, 120, 119, 118, 117], days)  # a jump that stays is a move
    assert len(real["d"]) == 6


def test_a_glitch_and_a_non_positive_close_are_dropped():
    days = _days(5)
    b = _bars([100, 0, 101, 101 * 52, 102], days)
    assert b["c"].tolist() == [100, 101, 102]


def test_sigma_before_needs_sixty_days_excludes_the_day_and_is_floored():
    x = np.r_[np.full(59, 0.01) * np.tile([1, -1], 30)[:59], 0.5]
    assert es.sigma_before(x, 59) is None
    flat = np.zeros(100)
    assert es.sigma_before(flat, 99) == es.SIG_FLOOR
    y = np.r_[np.random.default_rng(0).normal(0, 0.02, 300), 1.0]
    assert abs(es.sigma_before(y, 300) - np.std(y[48:300], ddof=1)) < 1e-12


def test_beta_is_shrunk_like_capm_and_floored_at_zero():
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.01, 400)
    assert es.shrunk_beta(2.0 * x, x) == pytest.approx(1 + 0.9 * (2.0 - 1), rel=1e-9)
    assert es.shrunk_beta(-0.5 * x, x) == 0.0
    assert es.shrunk_beta(x[:30], x[:30]) is None


def test_category_cuts_at_one_and_two_and_a_half():
    assert [es.category(z) for z in (-3, -2.5, -2.49, -1, -0.99, 0, 0.99, 1, 2.49, 2.5)] == [
        -2, -2, -1, -1, 0, 0, 0, 1, 1, 2]  # fmt: skip
    assert es.category(None) is None and es.category(float("nan")) is None
    assert [es.LABELS[c] for c in (-2, -1, 0, 1, 2)] == [
        "Big drop",
        "Fell",
        "Shrugged off",
        "Rose",
        "Big rise",
    ]


def test_follow_through():
    assert es.follow_through(0.05, 0.09, 1) == "kept going"
    assert es.follow_through(0.05, 0.05, 1) == "held"
    assert es.follow_through(0.05, 0.02, 1) == "gave back"
    assert es.follow_through(-0.05, 0.01, -1) == "gave back"  # reversed
    assert es.follow_through(0.05, 0.09, 0) is None  # shrugged off: nothing to follow
    assert es.follow_through(0.05, None, 1) is None


def test_reaction_takes_out_the_market_with_its_own_beta():
    m, r, days = _pair()
    i = len(days) - 20
    r[i], m[i] = 0.06, 0.05  # the stock moved with a big market day
    stock, idx, _ = _frame(m, r, days)
    fr = es.align(stock, idx)
    rx = es.reaction(fr, i, fund=False)
    assert 0.5 < rx["beta"] < 1.1
    # Most of the 6% was the market: abnormal is much smaller than raw.
    assert abs(rx["z_day"]) < 0.06 / (rx["sigma_pct"] / 100)
    assert rx["day_pct"] == pytest.approx(math.expm1(0.06) * 100, abs=0.01)
    assert rx["sessions_after"] == 5 and rx["provisional"] is False
    fund = es.reaction(fr, i, fund=True)
    assert fund["beta"] is None and fund["abn_pct_0_1"] is None and fund["abnormal"] is False


def test_reaction_on_day_zero_has_no_label_and_day_one_is_final():
    m, r, days = _pair()
    stock, idx, _ = _frame(m, r, days)
    fr = es.align(stock, idx)
    last = len(fr["d"]) - 1
    d0 = es.reaction(fr, last, False)
    assert d0["category"] is None and d0["label"] is None and d0["sessions_after"] == 0
    d1 = es.reaction(fr, last - 1, False)
    assert d1["category"] is not None and d1["sessions_after"] == 1 and d1["follow"] is None
    assert d1["provisional"] is True


def test_the_label_is_decided_by_days_zero_and_one():
    m, r, days = _pair(noise=0.01)
    i = len(days) - 30
    r[i], r[i + 1] = 0.08, -0.07  # a big first day almost all given back the next
    stock, idx, _ = _frame(m, r, days)
    rx = es.reaction(es.align(stock, idx), i, False)
    assert abs(rx["z_day"]) > 2.5
    assert rx["category"] in (-1, 0, 1)  # not "Big rise": the two days decide


def test_eps_outcome():
    assert es.eps_outcome({"actual": 1.1, "surprise_pct": 5.0}) == ("beat", None)
    assert es.eps_outcome({"actual": 1.0, "surprise_pct": 1.9}) == ("in line", None)
    assert es.eps_outcome({"actual": 0.9, "surprise_pct": -2.5}) == ("miss", None)
    assert es.eps_outcome({"actual": 9.0, "surprise_pct": 60.0}) == (
        None,
        "estimate not comparable",
    )
    assert es.eps_outcome({"actual": None})[0] is None
    assert es.eps_outcome({"actual": 1.0, "surprise_pct": None}) == (None, "no estimate")


def _divs(pairs):
    return [{"date": d, "amount": a} for d, a in pairs]


def test_a_quarterly_payer_is_compared_with_its_last_payment():
    q = _divs([("2024-02-01", 0.5), ("2024-05-01", 0.5), ("2024-08-01", 0.5), ("2024-11-01", 0.5),
               ("2025-02-01", 0.55), ("2025-05-01", 0.55), ("2025-08-01", 0.4)])  # fmt: skip
    assert es.div_outcome(q, 4)[0] == "raised"
    assert es.div_outcome(q, 5)[0] == "held"
    oc, ch, basis = es.div_outcome(q, 6)
    assert oc == "cut" and ch == pytest.approx(0.4 / 0.55 - 1) and basis == "last"


def test_a_new_interim_reads_as_a_raise_of_the_year_not_a_cut_of_the_final():
    d = _divs([("2023-05-20", 4.0), ("2024-05-20", 4.4), ("2025-05-22", 4.6), ("2025-10-01", 1.0)])
    assert es.div_outcome(d, 2)[0] == "raised"
    oc, _ch, basis = es.div_outcome(d, 3)
    assert basis == "year" and oc == "raised"


def test_a_special_is_found_by_its_timing_and_a_first_is_first():
    d = _divs([("2025-01-10", 1.0), ("2025-04-10", 1.0), ("2025-07-10", 1.0), ("2025-07-25", 3.0)])
    assert es.div_outcome(d, 3)[0] == "special"
    assert es.div_outcome(d, 0)[0] == "first"


QDATES = ["2024-02-15", "2024-05-16", "2024-08-15", "2024-11-14", "2025-02-13", "2025-05-15",
          "2025-08-14", "2025-11-13", "2026-02-12", "2026-05-14", "2026-08-13", "2026-11-12"]  # fmt: skip


def test_a_dividend_declared_in_dollars_is_compared_in_dollars():
    """A London line of a company that reports and declares in USD: Yahoo
    gives pence, converted at a rate that moves with the pound."""
    usd = [0.344] * 5 + [0.358] * 4 + [0.372] * 3  # raised 4% in May 2025, again in May 2026
    usd_per_gbp = [1.26, 1.25, 1.31, 1.27, 1.25, 1.33, 1.35, 1.31, 1.36, 1.37, 1.34, 1.31]
    # Yahoo's pence: converted at the announcement's rate, a little off the ex-date's.
    pence = [
        u / (r * (1 + 0.004 * (-1) ** k)) * 100
        for k, (u, r) in enumerate(zip(usd, usd_per_gbp, strict=True))
    ]
    divs = _divs(list(zip(QDATES, pence, strict=True)))
    naive = [es.div_outcome(divs, k)[0] for k in range(4, 12)]
    assert "cut" in naive  # the pound's moves read as cuts and raises
    conv, cmp_in = es.div_in_reporting_ccy(
        divs, "GBp", "USD", dict(zip(QDATES, usd_per_gbp, strict=True))
    )
    assert cmp_in == "USD" and conv[0]["cmp"] == pytest.approx(0.344, rel=0.005)
    got = [es.div_outcome(conv, k, es.DIV_TOL_FX)[0] for k in range(4, 12)]
    assert got == ["held", "raised", "held", "held", "held", "raised", "held", "held"]
    # A missing rate is "held", never a guess.
    gap, _ = es.div_in_reporting_ccy(divs, "GBp", "USD", dict.fromkeys(QDATES[:-1], 1.3))
    assert es.div_outcome(gap, 11, es.DIV_TOL_FX) == ("held", None, "no_fx")
    # Same currency: nothing to convert.
    assert es.div_in_reporting_ccy(divs, "USD", "USD") == (divs, None)


def test_the_us_lines_own_dollars_are_the_cleaner_source():
    """The pence are fixed when the company announces, so the ex-date's rate
    is up to ~2% off and a flat dividend can read "raised 3%". An ADR's USD
    (two shares each here) is exact; its ratio cancels in a change."""
    usd = [0.344] * 4 + [0.358] * 4 + [0.372, 0.39, 0.39, 0.39]
    usd_per_gbp = [1.26, 1.25, 1.31, 1.27, 1.25, 1.33, 1.35, 1.31, 1.36, 1.37, 1.34, 1.31]
    off = [0, 0.022, -0.02, 0.01, 0.015, -0.022, 0.02, -0.015, 0.0, 0.01, -0.01, 0.0]
    pence = [u / (r * (1 + e)) * 100 for u, r, e in zip(usd, usd_per_gbp, off, strict=True)]
    divs = _divs(list(zip(QDATES, pence, strict=True)))
    rates = dict(zip(QDATES, usd_per_gbp, strict=True))
    # The US line's ex-dates are a day later.
    adr = [
        {"date": (es._d(d) + timedelta(days=1)).isoformat(), "amount": 2 * u}
        for d, u in zip(QDATES, usd, strict=True)
    ]
    fx_only, _ = es.div_in_reporting_ccy(divs, "GBp", "USD", rates)
    conv, cin = es.div_in_reporting_ccy(divs, "GBp", "USD", rates, us_divs=adr, since=QDATES[0])
    assert cin == "USD" and conv[4]["cmp"] == pytest.approx(0.716)
    got = [es.div_outcome(conv, k, es.DIV_TOL_FX)[0] for k in range(4, 12)]
    assert got == ["raised", "held", "held", "held", "raised", "raised", "held", "held"]
    assert [
        es.div_outcome(fx_only, k, es.DIV_TOL_FX)[0] for k in range(4, 12)
    ] != got  # the FX alone misreads
    # A US line that covers too few payments isn't used (an ADR's ratio must not mix with converted pence).
    part, _ = es.div_in_reporting_ccy(divs, "GBp", "USD", rates, us_divs=adr[-3:], since=QDATES[0])
    assert part[0]["cmp"] == pytest.approx(fx_only[0]["cmp"])


def test_a_line_that_pays_what_was_declared_stays_as_it_is():
    """A company reporting in USD that declares in euros: its own line's
    amounts repeat, its US line's are converted and drift."""
    eur = [0.74, 0.74, 0.79, 0.79, 0.79, 0.79, 0.85, 0.85, 0.85, 0.85, 0.85, 0.9]
    usd_per_eur = [1.09, 1.08, 1.07, 1.11, 1.04, 1.08, 1.15, 1.17, 1.17, 1.15, 1.16, 1.13]
    divs = _divs(list(zip(QDATES, eur, strict=True)))
    adr = [{"date": d, "amount": e * r} for d, e, r in zip(QDATES, eur, usd_per_eur, strict=True)]
    assert es._clean_share([(x["date"], x["amount"]) for x in divs], None) > 0.6
    out, cin = es.div_in_reporting_ccy(divs, "EUR", "USD", {}, us_divs=adr, since=QDATES[0])
    assert (out, cin) == (divs, None)
    assert [es.div_outcome(out, k)[0] for k in (6, 7, 11)] == ["raised", "held", "raised"]


def test_an_uneven_schedule_needs_the_year_before_to_agree():
    """A company whose fourth payment is the big one: against the last
    payment alone it read "raised 12.5%" then "cut 5.6%"."""
    amounts = [15, 15, 15, 16, 16, 16, 16, 18, 17, 17, 17, 19]
    d = _divs(list(zip(QDATES, amounts, strict=True)))
    got = {QDATES[k]: es.div_outcome(d, k)[:1] + es.div_outcome(d, k)[2:] for k in range(4, 12)}
    assert got["2025-11-13"] == ("raised", "last")  # 18 vs 16, and vs 16 a year before
    assert got["2026-02-12"] == ("held", "mixed")  # 17 vs 18 down, vs 16 a year before up
    assert got["2026-05-14"] == ("held", "last")  # the same as the last one
    assert got["2026-11-12"] == ("raised", "last")  # 19 vs 17, and vs 18
    cut = _divs(list(zip(QDATES, [15] * 8 + [12] * 4, strict=True)))
    assert es.div_outcome(cut, 8)[0] == "cut"  # down against both


def test_a_fund_has_no_big_moves_but_its_market_days_count():
    m, r, days = _pair(n=700, noise=0.004, beta=1.0)
    r[640] = 0.08  # its market's move, far beyond its own sigma
    r[660], m[660] = -0.07, -0.065  # a market day
    stock, idx, ci = _frame(m, r, days)
    divs = [{"date": days[k], "amount": 0.5 + 0.1 * (k % 2)} for k in range(100, 700, 120)]
    rec = es.build_line(
        "ZZEM.L", True, stock, idx, _index_z(ci, days), [], divs, TODAY, None, "STOXX Europe 600"
    )
    assert not [e for e in rec["events"].values() if e["type"] == "M"]
    assert {e["outcome"] for e in rec["events"].values() if e["type"] == "D"} == {"paid"}
    assert [d["date"] for d in rec["drops"]] == [days[660]]
    share = es.build_line(
        "ZZEM.L", False, stock, idx, _index_z(ci, days), [], divs, TODAY, None, "STOXX"
    )
    assert f"M|ZZEM.L|{days[640]}" in share["events"]


def test_decluster_keeps_the_largest_of_a_cluster():
    assert es.decluster([(10, 4.2), (12, 6.0), (14, 4.5), (40, 5.0)]) == [
        (12, 6.0, 3),
        (40, 5.0, 1),
    ]


def test_decluster_never_merges_a_fall_with_a_rise():
    """A -14% day and a +31.5% day three sessions later are two stories."""
    assert es.decluster([(10, -4.6), (11, -4.1), (13, 10.4)]) == [(10, -4.6, 2), (13, 10.4, 1)]


def test_a_yearly_payer_is_compared_in_its_own_currency():
    """A company reporting in USD that declares once a year in francs: the
    ex-date's rate would turn a +5.7% raise into +19.6%."""
    divs = _divs([("2024-03-08", 3.30), ("2025-03-10", 3.50), ("2026-03-09", 3.70)])
    rates = {"2024-03-08": 1.13, "2025-03-10": 1.13, "2026-03-09": 1.28}
    out, cin = es.div_in_reporting_ccy(divs, "CHF", "USD", rates)
    assert (out, cin) == (divs, None)
    got, ch, _ = es.div_outcome(out, 2)
    assert got == "raised" and ch == pytest.approx(3.70 / 3.50 - 1)


def test_big_moves_skip_earnings_and_market_days_and_need_four_sigma():
    m, r, days = _pair(n=600, noise=0.01)
    a, b, c, e = 450, 500, 530, 560
    r[a] = 0.15  # a company move
    r[b] = -0.15  # on an earnings reaction session
    r[c] = -0.085  # with the market's own 3-sigma fall: the market explains it
    m[c] = -0.07
    r[e] = 0.02  # small: not a move
    stock, idx, ci = _frame(m, r, days)
    fr = es.align(stock, idx)
    kept, drops = es.big_moves(fr, _index_z(ci, days), [b], days[0])
    assert [k["date"] for k in kept] == [days[a]]
    assert kept[0]["z"] >= 4 and kept[0]["pct"] > 0
    assert [d["date"] for d in drops] == [days[c]]


def test_the_market_day_has_to_go_the_same_way():
    m, r, days = _pair(n=600, noise=0.01)
    r[500], m[500] = 0.15, -0.06  # up 15% on a market crash: the company's own news
    stock, idx, ci = _frame(m, r, days)
    kept, drops = es.big_moves(es.align(stock, idx), _index_z(ci, days), [], days[0])
    assert [k["date"] for k in kept] == [days[500]] and not drops


def test_a_huge_move_of_its_own_on_a_market_day_is_still_a_move():
    m, r, days = _pair(n=600, noise=0.01, beta=1.5)
    up, flat, opp = 500, 530, 560
    r[up], m[up] = 0.27, 0.046  # +31% on a +4.7% index day: 4x of its own, beyond the market
    r[flat], m[flat] = -0.03, -0.06  # a defensive name that fell less than the crash: raw z under 4
    r[opp], m[opp] = -0.11, -0.08  # 4x raw, but less than its beta says: abnormal points up
    stock, idx, ci = _frame(m, r, days)
    fr = es.align(stock, idx)
    iz = _index_z(ci, days)
    assert iz[days[up]] >= 3 and iz[days[opp]] <= -3
    kept, drops = es.big_moves(fr, iz, [], days[0])
    assert [k["date"] for k in kept] == [days[up]] and kept[0]["market_z"] >= 3
    assert es.abn_z(fr, up) >= 4 and es.abn_z(fr, opp) > 0
    assert [d["date"] for d in drops] == [days[opp]]


def test_market_days_merge_into_bands():
    days = [{"date": "2025-04-03", "index": "^GSPC", "index_pct": -4.8, "holdings": ["AAPL"]},
            {"date": "2025-04-04", "index": "^GSPC", "index_pct": -6.0, "holdings": ["MSFT"]},
            {"date": "2025-04-07", "index": "^GSPC", "index_pct": -0.2, "holdings": ["AAPL"]},
            {"date": "2026-03-03", "index": "^STOXX", "index_pct": -3.1, "holdings": ["ZZA.PA"]}]  # fmt: skip
    b = es.merge_bands(days)
    assert [(x["start"], x["end"]) for x in b] == [
        ("2025-04-03", "2025-04-07"),
        ("2026-03-03", "2026-03-03"),
    ]
    assert b[0]["index_pct"] == -6.0 and b[0]["holdings"] == ["AAPL", "MSFT"]


def test_a_band_is_consecutive_sessions_the_same_way():
    days = [{"date": "2026-11-20", "index": "^GSPC", "index_pct": -3.7, "holdings": ["AAPL"]},
            {"date": "2026-11-23", "index": "^GSPC", "index_pct": 4.7, "holdings": ["MSFT"]},
            {"date": "2026-11-24", "index": "^GSPC", "index_pct": 3.2, "holdings": ["AAPL"]},
            {"date": "2026-11-27", "index": "^GSPC", "index_pct": 3.5, "holdings": ["JNJ"]}]  # fmt: skip
    b = es.merge_bands(days)
    # A fall and the rebound are two bands; a same-way day three sessions later is a third.
    assert [(x["start"], x["end"], x["index_pct"]) for x in b] == [
        ("2026-11-20", "2026-11-20", -3.7),
        ("2026-11-23", "2026-11-24", 4.7),
        ("2026-11-27", "2026-11-27", 3.5),
    ]


# ------------------------------------------------------------ sentences


def _ev(**k):
    rx = {"category": -2, "label": "Big drop", "z": -3.1, "z_day": -4.0, "pct_0_1": -8.8, "abn_pct_0_1": -9.6,
          "follow": "held", "sessions_after": 5}  # fmt: skip
    rx.update(k.pop("rx", {}))
    return {"type": "E", "reaction": rx, **k}


def test_the_size_sentence_is_one_template():
    assert es.size_sentence(_ev(), "Zed") == (
        "Shares fell 8.8% over two days, 9.6% worse than the market: 3.1× a normal two-day move. "
        "By day five it had held.")  # fmt: skip
    flat = es.size_sentence(
        _ev(rx={"category": 0, "z": 0.4, "pct_0_1": 0.4, "abn_pct_0_1": -1.8, "follow": None})
    )
    assert (
        flat
        == "Shares rose 0.4% over two days, 1.8% worse than the market: within a normal two-day move."
    )
    d0 = es.size_sentence(_ev(rx={"category": None, "z_day": -1.8}))
    assert d0.startswith("Day 0 only: 1.8× a normal day so far.")
    react = es.size_sentence(
        {"type": "E", "reacting": True, "timing": "after", "reaction": None}, "Zed"
    )
    assert react.startswith("Zed reported after the close")
    assert es.size_sentence({"type": "D", "reaction": None}) == ""


def test_the_market_sentence_never_names_a_cause():
    assert es.market_sentence({"pct": -10.9, "market_z": -0.06, "market_pct": -0.05}, "STOXX Europe 600") == (
        "The market does not explain it")  # fmt: skip
    s = es.market_sentence({"pct": -9.0, "market_z": -2.0, "market_pct": -2.1}, "S&P 500")
    assert s == "The market moved the same way (S&P 500 −2.1%)"


# ------------------------------------------------- the model's clause


HEADS = [{"headline": "Zed Pharma Late-Stage Heart Study Fails, Stock Down", "summary": "The trial missed its primary goal."},
         {"headline": "Markets wrap: stocks mixed", "summary": ""}]  # fmt: skip


def _check(why, pick=1, direction=-1, tone=-2, heads=HEADS):
    return es.check_why({"headline": pick, "why": why, "tone": tone}, heads, direction)


def test_a_clause_from_the_picked_headline_passes():
    got, why = _check("The news: Zed Pharma's late-stage heart study failed its primary goal")
    assert why == "" and got["why"].endswith(".") and got["tone"] == -2


def test_the_checks_refuse_what_the_owner_ruled_out():
    assert _check("The study likely failed its primary goal")[1] == "hedged"
    assert _check("Headline 1 reports the study failed")[1] == "names a headline number"
    assert _check("The late-stage study failed after 12 weeks")[1] == "a number not in the headline"
    assert _check("Zed Pharma shares jumped as the study failed", direction=-1)[1] == "direction"
    assert _check("Investors cheered unrelated macro data today")[1] == "not from the headline"
    assert (
        _check("The news: Zed Pharma's late-stage heart study failed", tone=1)[1]
        == "tone against the move"
    )
    assert _check("x", pick=3)[1] == "bad pick"
    assert es.check_why("nope", HEADS, -1)[1] == "no answer"


def test_zero_means_no_headline_fits():
    got, why = _check("", pick=0)
    assert why == "" and got == {"headline": 0, "why": None, "tone": None}


def test_only_the_headlines_numbers_are_allowed():
    heads = [
        {
            "headline": "Zed Pharma Phase 3 Heart Study Misses Goal After 52 Weeks",
            "summary": "A blow.",
        }
    ]
    got, _ = _check(
        "The news: Zed Pharma's phase 3 heart study missed its goal after 52 weeks", heads=heads
    )
    assert got is not None
    # The code's own size sentence is in the model's input; restating its figure isn't the headline's fact.
    bad = _check(
        "The news: Zed Pharma's phase 3 heart study missed; shares fell 10.9%", heads=heads
    )
    assert bad == (None, "a number not in the headline")


def test_the_hedge_check_is_case_aware():
    heads = [
        {"headline": "Zed Retail Results For The Quarter To May Miss Forecasts", "summary": ""}
    ]
    got, _ = _check(
        "The news: Zed Retail's results for the quarter to May missed forecasts", heads=heads
    )
    assert got is not None  # the month
    assert (
        _check("The news: Zed Retail's results may have missed forecasts", heads=heads)[1]
        == "hedged"
    )
    assert _check("Likely: Zed Retail's results missed forecasts", heads=heads)[1] == "hedged"


def test_a_clause_that_only_repeats_its_headline_is_left_out():
    h = "ZZ Corp H1 Adj. EPS $0.95 Misses $0.96 Estimate, Sales $10.460B Miss $11.111B Estimate"
    assert es.restates(
        "The news: ZZ Corp H1 Adj. EPS $0.95 Misses $0.96 Estimate, Sales $10.460B Miss.", h
    )
    assert not es.restates(
        "The news: Acme Energy's production missed what analysts expected.",
        "Acme Energy Shares Fall After Production Misses Expectations",
    )


def test_results_like_headlines():
    assert es.results_like("Zed Q2 2026 Earnings Call Transcript")
    assert es.results_like("Zed half-year results beat")
    assert not es.results_like("Zed Pharma Late-Stage Heart Study Fails")


def test_note_stages_and_the_inputs_hash():
    e = _ev()
    assert es.note_stage(e) == "final"
    assert es.note_stage(_ev(rx={"sessions_after": 2})) == "d1"
    assert es.note_stage(_ev(rx={"category": None})) is None
    h = es.inputs_hash(e, HEADS, "final")
    assert h == es.inputs_hash(e, HEADS, "final")
    assert h != es.inputs_hash(e, HEADS, "d1")
    assert h != es.inputs_hash(e, HEADS[:1], "final")


def test_only_plain_https_links_are_kept():
    assert es.safe_url("https://finnhub.io/api/news?id=abc") == "https://finnhub.io/api/news?id=abc"
    assert es.safe_url("http://example.com/x") is None
    assert es.safe_url("https://user@evil.example/") is None
    assert es.safe_url("javascript:alert(1)") is None


# ------------------------------------------------------------- the rail


def test_the_rail_ranks_by_reaction_times_root_weight_and_lists_newest_first():
    def ev(sym, day, z, typ="E", outcome="beat"):
        return {"id": f"{typ}|{sym}|{day}", "type": typ, "symbol": sym, "date": day, "session": day,
                "outcome": outcome, "reaction": {"category": 1 if z else None, "z": z} if typ != "D" else None}  # fmt: skip

    evs = [ev("AAPL", "2026-11-10", 1.0), ev("MSFT", "2026-11-12", 3.0), ev("NVDA", "2026-11-01", 2.0),
           ev("AMZN", "2026-10-25", 1.2), ev("META", "2026-10-30", 0.3), ev("JNJ", "2026-11-05", 2.5),
           ev("JNJ", "2026-11-06", None, "D", "held"), ev("PEP", "2026-11-07", None, "D", "raised"),
           ev("AAPL", "2026-09-01", 9.0)]  # fmt: skip
    w = {"AAPL": 25.0, "MSFT": 4.0, "NVDA": 1.0, "AMZN": 9.0, "META": 30.0, "JNJ": 1.0, "PEP": 1.0}
    out = es.rail_pick(evs, w, TODAY)
    ids = [e["id"] for e in out["items"]]
    assert "D|JNJ|2026-11-06" not in ids  # a held dividend is no news
    assert "E|AAPL|2026-09-01" not in ids  # older than 30 days
    assert ids == sorted(ids, key=lambda i: i.split("|")[2], reverse=True)
    top = [e["id"] for e in out["items"] if e["top"]]
    # |z| x sqrt(weight): MSFT 6, AAPL 5, AMZN 3.6, JNJ 2.5, NVDA 2 ... META's big weight shrugged it off (1.6)
    assert top == [
        "E|MSFT|2026-11-12",
        "E|AAPL|2026-11-10",
        "E|JNJ|2026-11-05",
        "E|NVDA|2026-11-01",
        "E|AMZN|2026-10-25",
    ]


# ------------------------------------------------------- caches and payload


def _line(line, events, *, held=None, drops=None, fund=False):
    return {line: {"asof": "2026-11-17", "computed_at": 1e12, "fund": fund, "index": "^GSPC",
                   "index_name": "S&P 500", "ccy": "USD", "events": {e["id"]: e for e in events},
                   "drops": drops or [], "held": held or {}}}  # fmt: skip


def _write_history(lines):
    es._atomic_write(es._hist_path(), json.dumps({"v": es.VERSION, "lines": lines}))


def _write_notes(notes):
    es._atomic_write(es._notes_path(), json.dumps({"v": es.NOTES_VERSION, "notes": notes}))


def _move(line, day, pct=-10.9):
    return {"id": f"M|{line}|{day}", "type": "M", "line": line, "date": day, "session": day,
            "outcome": "down" if pct < 0 else "up",
            "move": {"date": day, "pct": pct, "z": -9.0, "market_pct": -0.05, "market_z": -0.06,
                     "index": "^STOXX", "index_name": "STOXX Europe 600"},
            "reaction": {"category": -2, "label": "Big drop", "z": -6.25, "z_day": -11.0, "pct_0_1": -11.0,
                         "abn_pct_0_1": -7.5, "follow": "held", "sessions_after": 5, "sigma_pct": 0.9,
                         "beta": 0.6, "abnormal": True}}  # fmt: skip


@pytest.fixture
def lines(monkeypatch):
    monkeypatch.setattr(
        es, "_primary", lambda s: {"ZZN.DE": "ZZN.SW", "ZZN.MI": "ZZN.SW"}.get(s, s)
    )
    monkeypatch.setattr(es, "_keys", lambda: (True, True))
    monkeypatch.setattr(es, "_rail_move_z", lambda: 3.0)
    monkeypatch.setattr(es, "_NIM", {"stopped": False, "fails": 0, "until": 0.0})
    monkeypatch.setitem(es._STATE, "next", 0.0)
    es.reset_memo()


def test_a_company_held_through_two_listings_shows_once_under_the_bigger(lines):
    _write_history(
        _line("ZZN.SW", [_move("ZZN.SW", "2026-09-08")], held={"ZZN.DE": {"2026-09-08": -11.44}})
    )
    pos = [
        {"symbol": "ZZN.MI", "name": "Zed (Milan)", "weight": 1.0},
        {"symbol": "ZZN.DE", "name": "Zed", "weight": 9.0},
    ]
    out = es.for_book(pos, TODAY, upcoming_dates={})
    assert [(e["symbol"], e["held_pct"]) for e in out["events"]] == [("ZZN.DE", -11.44)]
    assert out["events"][0]["say"].startswith("Shares fell 11.0% over two days")
    assert "window" not in out["events"][0]


def test_causes_never_guess(lines):
    old = _move("AAPL", "2025-06-02")
    fresh = _move("MSFT", "2026-11-02")
    noted = _move("NVDA", "2026-10-01")
    _write_history({**_line("AAPL", [old]), **_line("MSFT", [fresh]), **_line("NVDA", [noted])})
    _write_notes({noted["id"]: {"stage": "final", "inputs": "x", "cause": {"kind": "news", "headline": "NVDA guidance cut",
                  "source": "Wire", "url": "https://finnhub.io/x", "tone": -1}, "why": "The news: guidance was cut."}})  # fmt: skip
    pos = [{"symbol": s, "name": s, "weight": 5.0} for s in ("AAPL", "MSFT", "NVDA")]
    by = {e["symbol"]: e for e in es.for_book(pos, TODAY, upcoming_dates={})["events"]}
    assert by["AAPL"]["cause"]["kind"] == "none" and by["AAPL"]["cause"]["reason"] == "older"
    assert by["AAPL"]["cause"]["market"] == "The market does not explain it"
    assert by["MSFT"]["cause"]["kind"] == "pending"
    assert by["NVDA"]["cause"]["headline"] == "NVDA guidance cut" and by["NVDA"]["why"].startswith(
        "The news"
    )


def test_without_keys_a_move_says_so_not_pending(lines, monkeypatch):
    monkeypatch.setattr(es, "_keys", lambda: (False, False))
    _write_history(_line("MSFT", [_move("MSFT", "2026-11-02")]))
    e = es.for_book([{"symbol": "MSFT", "name": "MSFT", "weight": 1}], TODAY, upcoming_dates={})[
        "events"
    ][0]
    assert e["cause"]["reason"] == "no_key"
    assert es.notes_due([{"symbol": "MSFT", "name": "MSFT", "weight": 1}], TODAY) == []


def test_an_uncomputed_holding_is_pending_and_market_days_are_banded(lines):
    drops = [
        {
            "date": "2026-11-05",
            "pct": -6.0,
            "z": -4.4,
            "index": "^GSPC",
            "index_name": "S&P 500",
            "index_pct": -4.4,
        }
    ]
    _write_history(_line("AAPL", [], drops=drops))
    pos = [
        {"symbol": "AAPL", "name": "Apple", "weight": 5},
        {"symbol": "MSFT", "name": "Microsoft", "weight": 2},
    ]
    out = es.for_book(
        pos, TODAY, upcoming_dates={"MSFT": {"earnings": "2026-12-01", "ex_div": "2027-03-01"}}
    )
    assert out["pending"]["holdings"] == ["MSFT"]
    assert out["bands"][0]["start"] == "2026-11-05" and out["bands"][0]["holdings"] == ["AAPL"]
    assert [(u["type"], u["date"]) for u in out["upcoming"]] == [
        ("E", "2026-12-01")
    ]  # within 45 days only


def test_a_malformed_cache_reads_as_empty(lines):
    es._hist_path().parent.mkdir(parents=True, exist_ok=True)
    es._hist_path().write_text("{nope", encoding="utf-8")
    out = es.for_book([{"symbol": "AAPL", "name": "Apple", "weight": 5}], TODAY, upcoming_dates={})
    assert out["events"] == [] and out["pending"]["holdings"] == ["AAPL"]


def test_notes_are_due_once_per_stage_and_a_missing_headline_is_asked_again_for_a_week(lines):
    recent = _move("MSFT", "2026-11-12")
    recent["reaction"]["sessions_after"] = 3
    _write_history(_line("MSFT", [recent]))
    pos = [{"symbol": "MSFT", "name": "MSFT", "weight": 1}]
    assert [e["id"] for e in es.notes_due(pos, TODAY)] == [recent["id"]]
    _write_notes(
        {recent["id"]: {"stage": "d1", "inputs": "h", "checked_at": 2e9, "cause": {"kind": "none"}}}
    )
    es.reset_memo()
    assert es.notes_due(pos, TODAY) == []  # checked lately
    recent["reaction"]["sessions_after"] = 5
    _write_history(_line("MSFT", [recent]))
    es.reset_memo()
    assert [e["id"] for e in es.notes_due(pos, TODAY)] == [recent["id"]]  # the final stage


def test_the_merge_never_writes_back_a_stale_copy(lines):
    es._merge("history", _line("AAPL", []))
    es._merge("history", _line("MSFT", []))
    assert set(es.load_history()["lines"]) == {"AAPL", "MSFT"}


# ------------------------------------------------------------ the job


def test_a_bar_dated_today_waits_for_its_market_to_close():
    now = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)
    assert es._closed("AAPL", "2026-11-17", now)
    assert not es._closed("AAPL", "2026-11-18", now)
    assert not es._closed("ZZA.PA", "2026-11-18", now)
    assert es._closed("ZZA.PA", "2026-11-18", now.replace(hour=17))
    assert es._closed("AAPL", "2026-11-18", now.replace(hour=21, minute=30))


def test_a_note_writes_the_models_clause_after_the_checks(lines, monkeypatch):
    ev = {**_move("MSFT", "2026-11-12"), "symbol": "MSFT", "name": "MSFT"}
    heads = [{"headline": "Microsoft cloud outage hits customers", "summary": "An outage.", "source": "Wire",
              "url": "https://finnhub.io/a", "datetime": 0, "rel": 1.0}]  # fmt: skip
    monkeypatch.setattr(es, "_headlines_finnhub", lambda e, n, c: heads)
    monkeypatch.setattr(es, "_headlines_cached", lambda e, n: [])
    answers = iter([{"headline": 1, "why": "Shares probably fell on a cloud outage", "tone": -1},
                    {"headline": 1, "why": "The news: a Microsoft cloud outage hit customers", "tone": -1}])  # fmt: skip
    from convexity import news_sentiment as ns

    monkeypatch.setattr(ns, "_nvidia_call", lambda *a, **k: next(answers))
    cancel = threading.Event()
    monkeypatch.setattr(cancel, "wait", lambda s=None: False)
    assert es._write_note(ev, "Microsoft", cancel, {"nim": 5, "fh": 5})
    n = es.load_notes()["notes"][ev["id"]]
    assert n["cause"]["kind"] == "news" and n["why"].startswith(
        "The news: a Microsoft cloud outage"
    )


JOBS = [
    {
        "headline": "Microsoft cuts jobs",
        "summary": "",
        "source": "W",
        "url": None,
        "datetime": 0,
        "rel": 1,
    }
]


def _no_wait(monkeypatch):
    cancel = threading.Event()
    monkeypatch.setattr(cancel, "wait", lambda s=None: False)
    return cancel


def test_a_failed_call_is_never_stored(lines, monkeypatch):
    ev = {**_move("MSFT", "2026-11-12"), "symbol": "MSFT", "name": "MSFT"}
    _write_history(_line("MSFT", [_move("MSFT", "2026-11-12")]))
    monkeypatch.setattr(es, "_headlines_finnhub", lambda e, n, c: None)  # Finnhub didn't answer
    cancel = _no_wait(monkeypatch)
    assert es._write_note(ev, "Microsoft", cancel, {"nim": 5, "fh": 5}) is False
    assert es.load_notes()["notes"] == {}
    monkeypatch.setattr(es, "_headlines_finnhub", lambda e, n, c: JOBS)
    monkeypatch.setattr(es, "_headlines_cached", lambda e, n: [])
    from convexity import news_sentiment as ns

    monkeypatch.setattr(ns, "_nvidia_call", lambda *a, **k: None)  # NIM didn't answer
    assert es._write_note(ev, "Microsoft", cancel, {"nim": 5, "fh": 5}) is False
    n = es.load_notes()["notes"][ev["id"]]
    # The headlines that came back are kept, with their time; no cause, no clause, no inputs.
    assert n["headlines"] == JOBS and n["headlines_at"] > 0 and n["headlines_stage"] == "final"
    assert "cause" not in n and "why" not in n and n["inputs"] is None
    pos = [{"symbol": "MSFT", "name": "Microsoft", "weight": 1}]
    assert es.for_book(pos, TODAY, upcoming_dates={})["events"][0]["cause"]["kind"] == "pending"


def test_kept_headlines_are_reused_and_the_model_backs_off(lines, monkeypatch):
    ev = {**_move("MSFT", "2026-11-12"), "symbol": "MSFT", "name": "MSFT"}
    _write_history(_line("MSFT", [_move("MSFT", "2026-11-12")]))
    fh = []
    monkeypatch.setattr(es, "_headlines_finnhub", lambda e, n, c: fh.append(1) or JOBS)
    monkeypatch.setattr(es, "_headlines_cached", lambda e, n: [])
    from convexity import news_sentiment as ns

    def nim_fails(*a, failure=None, **k):
        failure["code"] = 503
        return None

    monkeypatch.setattr(ns, "_nvidia_call", nim_fails)
    cancel = _no_wait(monkeypatch)
    assert es._write_note(ev, "Microsoft", cancel, {"nim": 5, "fh": 5}) is False
    assert len(fh) == 1 and not es.nim_ready()
    assert 590 < es._NIM["until"] - __import__("time").time() <= 600
    # Inside the backoff the move only waits: no Finnhub call, no NIM call, not even due.
    monkeypatch.setattr(ns, "_nvidia_call", lambda *a, **k: pytest.fail("no call in a backoff"))
    assert es._write_note(ev, "Microsoft", cancel, {"nim": 5, "fh": 5}) is None
    assert len(fh) == 1
    pos = [{"symbol": "MSFT", "name": "MSFT", "weight": 1}]
    assert es.notes_due(pos, TODAY) == []
    # Once it's over, the kept headlines go to the model; Finnhub isn't asked again.
    es._NIM["until"] = 0.0
    assert [e["id"] for e in es.notes_due(pos, TODAY)] == [ev["id"]]
    ok = {"headline": 1, "why": "The news: Microsoft cuts jobs", "tone": -1}
    monkeypatch.setattr(ns, "_nvidia_call", lambda *a, **k: ok)
    budget = {"nim": 5, "fh": 5}
    assert es._write_note(ev, "Microsoft", cancel, budget) is True
    assert len(fh) == 1 and budget == {"nim": 4, "fh": 5} and es._NIM["fails"] == 0
    assert es.load_notes()["notes"][ev["id"]]["cause"]["headline"] == "Microsoft cuts jobs"


def test_the_backoff_doubles_to_six_hours_and_an_answer_resets_it(monkeypatch):
    monkeypatch.setattr(es, "_NIM", {"stopped": False, "fails": 0, "until": 0.0})
    waits = []
    for _ in range(8):
        es._nim_result(False, 503, now=1000.0)
        waits.append(es._NIM["until"] - 1000.0)
    assert waits == [600, 1200, 2400, 4800, 9600, 19200, 21600, 21600]
    es._nim_result(True)
    assert es._NIM == {"stopped": False, "fails": 0, "until": 0.0} and es.nim_ready()


def test_a_rejected_key_stops_the_model_until_the_keys_change(lines, monkeypatch):
    calls = []
    from convexity import keys
    from convexity import news_sentiment as ns

    def rejected(*a, failure=None, **k):
        calls.append(1)
        failure["code"] = 403
        return None

    monkeypatch.setattr(ns, "_nvidia_call", rejected)
    monkeypatch.setattr(es, "_headlines_finnhub", lambda e, n, c: JOBS)
    monkeypatch.setattr(es, "_headlines_cached", lambda e, n: [])
    cancel = _no_wait(monkeypatch)
    evs = [{**_move(s, "2026-11-12"), "symbol": s, "name": s} for s in ("MSFT", "AAPL", "NVDA")]
    out = [es._write_note(e, e["symbol"], cancel, {"nim": 40, "fh": 30}) for e in evs]
    assert out == [False, None, None] and len(calls) == 1  # one call, then stopped
    assert es._NIM["stopped"] and es._NIM["until"] == 0.0
    # Every move's headlines are kept for when the key works.
    assert all(es.load_notes()["notes"][e["id"]]["headlines"] == JOBS for e in evs)
    from convexity import finnhub_adapter

    monkeypatch.setattr(ns, "reload_keys", lambda: None)  # the test's keys stay as they are
    monkeypatch.setattr(finnhub_adapter, "reload_keys", lambda: None)
    keys.reload_all()  # Settings -> API keys saved a key
    assert es.nim_ready() and not es._NIM["stopped"]


def test_the_pass_budget_counts_calls_not_events(lines, monkeypatch):
    ev = {**_move("MSFT", "2026-11-12"), "symbol": "MSFT", "name": "MSFT"}
    monkeypatch.setattr(es, "_headlines_finnhub", lambda e, n, c: JOBS)
    monkeypatch.setattr(es, "_headlines_cached", lambda e, n: [])
    from convexity import news_sentiment as ns

    answers = iter([{"headline": 1, "why": "Shares probably fell", "tone": -1},
                    {"headline": 1, "why": "The news: Microsoft cuts jobs", "tone": -1}])  # fmt: skip
    monkeypatch.setattr(ns, "_nvidia_call", lambda *a, **k: next(answers))
    cancel = _no_wait(monkeypatch)
    budget = {"nim": 3, "fh": 5}
    assert es._write_note(ev, "Microsoft", cancel, budget) is True
    assert budget["nim"] == 1  # a refused clause and its retry: two calls
    ev2 = {**_move("AAPL", "2026-11-12"), "symbol": "AAPL", "name": "AAPL"}
    monkeypatch.setattr(
        ns, "_nvidia_call", lambda *a, **k: pytest.fail("one call left: a unit may need two")
    )
    assert es._write_note(ev2, "Apple", cancel, budget) is False
    assert es.load_notes()["notes"][ev2["id"]]["headlines"] == JOBS  # kept for the next pass


def test_a_move_before_its_headline_lookup_is_looking_not_none(lines):
    d0 = _move("MSFT", "2026-11-17")
    d0["reaction"].update(category=None, label=None, sessions_after=0)
    _write_history(_line("MSFT", [d0]))
    e = es.for_book([{"symbol": "MSFT", "name": "MSFT", "weight": 1}], TODAY, upcoming_dates={})[
        "events"
    ][0]
    assert e["cause"]["kind"] == "pending" and e["index_name"] == "S&P 500"


def test_an_old_move_is_settled_without_a_call(lines, monkeypatch):
    ev = {**_move("MSFT", "2024-12-20"), "symbol": "MSFT", "name": "MSFT"}
    monkeypatch.setattr(
        es,
        "_headlines_finnhub",
        lambda *a: pytest.fail("no call for a move the source can't reach"),
    )
    assert es._write_note(ev, "Microsoft", threading.Event(), {"nim": 5, "fh": 5})
    assert es.load_notes()["notes"][ev["id"]]["cause"] == {"kind": "none", "reason": "older"}


def test_build_line_from_synthetic_bars_and_yahoo_rows():
    m, r, days = _pair(n=700, noise=0.011)
    i_move, i_earn = 640, 600
    r[i_move] = 0.13
    r[i_earn] = -0.10
    stock, idx, ci = _frame(m, r, days)
    earnings = [{"date": days[i_earn], "react": days[i_earn], "timing": "before", "estimate": 1.0, "actual": 1.1,
                 "surprise_pct": 10.0},
                {"date": "2026-11-17", "react": "2026-11-18", "timing": "after", "estimate": 2.0, "actual": None},
                {"date": "2027-01-28", "react": "2027-01-28", "timing": "", "estimate": 2.1, "actual": None}]  # fmt: skip
    divs = [{"date": days[k], "amount": 0.5} for k in range(100, 700, 63)]
    rec = es.build_line(
        "ZZA", False, stock, idx, _index_z(ci, days), earnings, divs, TODAY, None, "S&P 500"
    )
    evs = rec["events"]
    e = evs[f"E|ZZA|{days[i_earn]}"]
    assert e["outcome"] == "beat" and e["reaction"]["category"] == -2  # beat, then a big drop
    assert len(e["window"]) == 11 and e["window"][5][0] == days[i_earn]
    assert (
        evs["E|ZZA|2026-11-17"]["reacting"] is True
    )  # after the close yesterday: day 0 not closed
    assert not any(k.endswith("2027-01-28") for k in evs)  # upcoming: the Upcoming list carries it
    assert (
        f"M|ZZA|{days[i_move]}" in evs and f"M|ZZA|{days[i_earn]}" not in evs
    )  # the E is coloured instead
    assert all(v["reaction"] is None for v in evs.values() if v["type"] == "D")


# ---------------------------------------------------------- scheduling


def test_the_events_phase_runs_the_pass(monkeypatch):
    seen = []
    monkeypatch.setattr(es, "run_pass", lambda cancel: seen.append(cancel) or "done")
    job, outcome = jobs.submit(scope="current", phases=["events"], days=7,
                               entries_by_view={jobs.BOOK_VIEW: ""}, context={"background": True})  # fmt: skip
    assert outcome == "created" and job.background
    job._thread.join(5)
    assert seen and job.state == "done"


def test_tick_waits_for_a_running_job_and_for_something_due(monkeypatch):
    class Running:
        def is_terminal(self):
            return False

    monkeypatch.setattr(jobs, "current", lambda: Running())
    assert es.tick(submit=lambda: pytest.fail("not while another job runs")) is None
    monkeypatch.setattr(jobs, "current", lambda: None)
    monkeypatch.setattr(es, "due", lambda now=None: False)
    assert es.tick(submit=lambda: pytest.fail("nothing due")) is None
    monkeypatch.setattr(es, "due", lambda now=None: True)
    assert es.tick(submit=lambda: "go") == "go"


def test_the_scheduler_is_off_in_the_test_suite():
    es.start()
    assert es._STATE["thread"] is None


def test_the_page_never_fetches_for_the_rail(monkeypatch):
    """with_attention reads the caches only: no download, no Yahoo events."""
    monkeypatch.setattr(
        es, "_download", lambda *a, **k: pytest.fail("no download on the page's path")
    )
    monkeypatch.setattr(
        es, "_ask_model", lambda *a, **k: pytest.fail("no model on the page's path")
    )
    out = investments.with_attention({"positions": [{"symbol": "AAPL", "name": "Apple", "weight": 100.0, "value": 1.0}],
                                      "ccy": "USD"})  # fmt: skip
    assert out["attention"]["recent"]["computed"] is False


def test_the_upcoming_window_is_45_days():
    held = [{"symbol": "AAPL", "name": "Apple"}]
    dates = {
        "AAPL": {
            "earnings": (TODAY + timedelta(days=45)).isoformat(),
            "ex_div": (TODAY + timedelta(days=46)).isoformat(),
        }
    }
    assert [u["type"] for u in es.upcoming(held, TODAY, dates, {})] == ["E"]


def test_the_excel_events_sheet_gives_a_dividend_its_currency():
    from openpyxl import Workbook

    from convexity import xlsx_export

    ws = Workbook().active
    d = {"id": "D|ZZSH.L|2026-11-12", "type": "D", "symbol": "ZZSH.L", "name": "Zed Shipping", "line": "ZZSH.L",
         "date": "2026-11-12", "session": "2026-11-12", "outcome": "held", "ccy": "GBp",
         "dividend": {"amount": 26.95, "change_pct": 0.4, "basis": "last", "compared_in": "USD"}}  # fmt: skip
    m = {**_move("AAPL", "2026-11-02"), "symbol": "AAPL", "name": "Apple", "ccy": "USD"}
    xlsx_export._write_events(ws, {"events": [d, m]})
    head = [c.value for c in ws[4]]
    row = dict(zip(head, [c.value for c in ws[5]], strict=True))
    assert row["Dividend a share"] == 26.95 and row["Dividend currency"] == "GBp"
    assert row["Change compared in"] == "USD"
    move = dict(zip(head, [c.value for c in ws[6]], strict=True))
    assert move["Dividend currency"] is None and move["Market reaction"] == "Big drop"
    assert ws.column_dimensions["V"].width == 70 and head[21] == "Why, in plain words"
