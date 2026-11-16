"""rebalance.py: the continuous solve against scipy, the rounding against brute
force, fewest trades against exhaustive subsets, and the edge cases (pence,
fractional exits, missing and stale prices, the cash floor, the minimum trade,
the gains estimate). Synthetic tickers only, no network."""

import itertools
import math
import time

import numpy as np
import pytest
from scipy.optimize import minimize

from convexity.rebalance import (
    Book,
    Line,
    Opts,
    RebalanceError,
    backward_path,
    rebalance,
    solve_continuous,
    te_of_set,
)

TODAY = "2026-11-12"


def make(seed, m, value=10_000.0, held_frac=0.8, px_range=(15, 900), cash=0.0):
    """A random book: m names, prices, a one-factor covariance, shares near a
    random allocation, a random target; some names held, some new, some out."""
    rng = np.random.default_rng(seed)
    px = np.exp(rng.uniform(np.log(px_range[0]), np.log(px_range[1]), m))
    vol = rng.uniform(0.15, 0.45, m)
    beta = rng.uniform(0.4, 1.0, m)
    corr = np.outer(beta, beta)
    np.fill_diagonal(corr, 1.0)
    S = corr * np.outer(vol, vol)
    held = rng.random(m) < held_frac
    w0 = rng.dirichlet(np.ones(m)) * held
    w0 /= w0.sum()
    q = np.floor(w0 * value / px)
    tgt = rng.dirichlet(np.ones(m))
    tgt[rng.random(m) < 0.15] = 0.0
    if tgt.sum() == 0:
        tgt[0] = 1.0
    lines = [Line(f"S{i:02d}", float(q[i]), float(px[i]), "EUR", 1.0, TODAY,
                  float(q[i] * px[i] * rng.uniform(-0.2, 0.3)), None) for i in range(m)]  # fmt: skip
    return lines, {f"S{i:02d}": float(tgt[i]) for i in range(m)}, cash, S


# ------------------------------------------------ 1. the continuous solve


@pytest.mark.parametrize("le", [False, True])
def test_continuous_matches_scipy(le):
    worst = 0.0
    for seed in range(20):
        lines, target, cash, S = make(seed, 8, value=50_000, cash=2_000)
        lines[seed % 8].price = None  # one locked name: the solve routes around it
        lines[seed % 8].value_fallback = lines[seed % 8].shares * 100.0
        bk = Book(lines, target, cash, S, Opts(mode="exact", tc_bps=0.0, today=TODAY))
        n = solve_continuous(bk, le=le)
        f = bk.movable  # names taken out are fixed at 0, locked ones as held
        ws = bk.wstar(bk.V)
        w_fix = bk.n_base * bk.v / bk.V
        B = (bk.V - bk.F - float(np.sum((bk.n_base * bk.v)[~f]))) / bk.V

        def obj(x, f=f, w_fix=w_fix, ws=ws, S=S):
            w = w_fix.copy()
            w[f] = x
            d = w - ws
            return d @ S @ d

        cons = ({"type": "ineq", "fun": lambda x, B=B: B - x.sum()} if le
                else {"type": "eq", "fun": lambda x, B=B: x.sum() - B})  # fmt: skip
        r = minimize(obj, np.full(f.sum(), B / f.sum()), method="SLSQP",
                     bounds=[(0, None)] * f.sum(), constraints=[cons],
                     options={"ftol": 1e-14, "maxiter": 500})  # fmt: skip
        worst = max(worst, abs(math.sqrt(max(bk.te2(n), 0)) - math.sqrt(max(r.fun, 0))))
        assert bk.cash_after(n) >= bk.F - 1e-6
    assert worst < 1e-5, worst


def test_exact_lands_on_the_target_with_costs():
    lines, target, cash, S = make(3, 10, value=80_000, cash=1_000)
    r = rebalance(lines, target, cash, S, Opts(mode="exact", tc_bps=25, today=TODAY))
    assert r.summary["te"] < 1e-6
    assert abs(r.summary["cash_after"] - cash) < 1e-4  # keep: cash stays what it was
    assert r.summary["cost"] > 0


# --------------------------------------------- 2. rounding vs brute force


def brute(bk: Book, unit_val, box=1, also=None):
    """Every combination of lattice points within +-box steps of the
    continuous solution's bracket (and 0, q, ``also``), exact objective,
    cash floor."""
    n_c = solve_continuous(bk)
    cands = []
    for i in range(len(bk.q)):
        if not bk.movable[i]:
            cands.append([bk.n_base[i]])
            continue
        u = 1.0 if unit_val is None else unit_val / bk.v[i]
        k0 = (n_c[i] - bk.q[i]) / u
        ks = range(math.floor(k0) - box, math.ceil(k0) + box + 1)
        pts = {round(bk.q[i] + k * u, 10) for k in ks if bk.q[i] + k * u >= -1e-9}
        pts |= {0.0, bk.q[i]}
        if also is not None:
            pts.add(round(float(also[i]), 10))
        if bk.opts.min_trade:
            pts = {p for p in pts if p in (bk.q[i], 0.0)
                   or abs(p - bk.q[i]) * bk.v[i] >= bk.opts.min_trade - 1e-9}  # fmt: skip
        cands.append(sorted(pts))
    grid = np.array(list(itertools.product(*cands)))
    K = bk.tc * (np.abs(grid - bk.q) * bk.v).sum(axis=1)
    Vp = bk.V - K
    cash = bk.cash + ((bk.q - grid) * bk.v).sum(axis=1) - K
    D = grid * bk.v / Vp[:, None] - bk.t[None, :] * ((Vp - bk.F) / Vp)[:, None]
    J = np.einsum("ij,jk,ik->i", D, bk.S, D) + bk.opts.lam * K / bk.V
    J[cash < bk.F - 1e-6] = np.inf
    return float(J.min())


def _gap_stats(m, seeds, unit_val, value, min_trade=0.0, enumerate_=False):
    gaps, hits = [], 0
    for seed in seeds:
        lines, target, _, S = make(seed, m, value=value)
        o = Opts(mode="shares" if unit_val is None else "step", step=unit_val or 0.0,
                 min_trade=min_trade, tc_bps=25.0, today=TODAY, enumerate=enumerate_)  # fmt: skip
        r = rebalance(lines, target, 0.0, S, o)
        bk = Book(lines, target, 0.0, S, o)
        jb = brute(bk, unit_val, also=r.n)
        jh = bk.objective(r.n)
        assert jh >= jb - 1e-9
        hits += jh <= jb + 1e-12
        gaps.append((math.sqrt(max(jh, 0)) - math.sqrt(max(jb, 0))) * 1e4)
    return np.array(gaps), hits


def test_the_search_alone_is_near_optimal():
    """No enumeration: the lattice search by itself, 6 names, a 10k book."""
    gaps, hits = _gap_stats(6, range(30), None, 10_000)
    assert np.percentile(gaps, 95) < 5.0, gaps
    assert hits >= 24


@pytest.mark.parametrize(("unit", "value", "min_trade"),
                         [(None, 10_000, 0.0), (None, 3_000, 0.0), (None, 10_000, 300.0),
                          (100.0, 10_000, 0.0)])  # fmt: skip
def test_enumeration_is_the_optimum_of_its_box(unit, value, min_trade):
    """With at most 8 tradeable names the box is enumerated: never worse than
    brute force over the same box (the extreme 3k book included, where the
    search alone was once 47 bp off)."""
    gaps, hits = _gap_stats(6, range(12), unit, value, min_trade, enumerate_=True)
    assert hits == 12, gaps


def test_whole_shares_and_steps():
    lines, target, cash, S = make(5, 12, value=60_000)
    for mode, step in (("shares", 0), ("step", 100.0)):
        r = rebalance(lines, target, cash, S, Opts(mode=mode, step=step, tc_bps=10, today=TODAY))
        for x in r.trades:
            if mode == "shares" and not x["all"]:
                assert abs(x["shares"] - round(x["shares"])) < 1e-9
            if mode == "step" and not x["all"]:
                assert abs(x["value"] / 100.0 - round(x["value"] / 100.0)) < 1e-6
        assert r.summary["cash_after"] >= -1e-6


def test_a_27_name_book_is_quick():
    lines, target, cash, S = make(11, 27, value=270_000, held_frac=0.9)
    t = time.perf_counter()
    r = rebalance(
        lines, target, cash, S, Opts(mode="shares", min_trade=100, tc_bps=25, today=TODAY)
    )
    assert time.perf_counter() - t < 3.0
    assert r.summary["te"] < 0.002 and r.summary["method"] == "search"


# ------------------------------------------- 3. fewest trades vs exhaustive


def test_fewest_path_is_near_exhaustive():
    ratios = []
    for seed in range(8):
        lines, target, cash, S = make(seed, 8, value=200_000, held_frac=1.0)
        o = Opts(mode="fewest", rounding="exact", force_exits=False, tc_bps=25.0, today=TODAY)
        bk = Book(lines, target, cash, S, o)
        n_full = solve_continuous(bk)
        path = backward_path(bk, n_full)
        trades = np.flatnonzero(bk.free & (np.abs(n_full - bk.q) * bk.v > 0.5))
        best = {}
        for k in range(1, len(trades)):
            b = math.inf
            for comb in itertools.combinations(trades, k):
                act = np.zeros(len(bk.q), dtype=bool)
                act[list(comb)] = True
                b = min(b, te_of_set(bk, act)[0])
            best[k] = b
        ratios += [te / best[k] for k, te, _ in path if k in best and best[k] > 1e-9]
    assert np.percentile(ratios, 95) < 1.10, ratios


def test_fewest_keeps_forced_trades_and_meets_the_tolerance():
    lines, target, cash, S = make(4, 14, value=150_000, held_frac=1.0)
    out = next(
        s
        for s, w in target.items()
        if w == 0 and any(ln.symbol == s and ln.shares > 0 for ln in lines)
    )
    r = rebalance(lines, target, cash, S, Opts(mode="fewest", te_tol=0.01, tc_bps=25, today=TODAY))
    assert out in {x["symbol"] for x in r.trades if x["side"] == "sell" and x["all"]}
    full = rebalance(lines, target, cash, S, Opts(mode="shares", tc_bps=25, today=TODAY))
    assert r.summary["n_trades"] < full.summary["n_trades"]
    assert r.path[0]["trades"] >= r.path[-1]["trades"]
    assert r.summary["te"] < 0.012  # the path's 1%, plus whole-share rounding


def test_fewest_picks_a_point_from_the_path():
    lines, target, cash, S = make(9, 12, value=120_000, held_frac=1.0)
    base = rebalance(lines, target, cash, S, Opts(mode="fewest", tc_bps=10, today=TODAY))
    k = base.path[len(base.path) // 2]["trades"]
    r = rebalance(lines, target, cash, S, Opts(mode="fewest", n_trades=k, rounding="exact",
                                               tc_bps=10, today=TODAY))  # fmt: skip
    assert r.summary["picked"] == k and r.summary["n_trades"] <= k


def test_fewest_may_leave_proceeds_in_cash():
    """The "<= budget" branch: selling a name and buying a smaller new one
    leaves the rest as cash rather than piling it into the new name."""
    S = np.diag([0.04, 0.09, 0.0625]) + 0.01
    lines = [Line("AAA", 100, 50.0, "EUR", 1.0, TODAY), Line("BBB", 50, 100.0, "EUR", 1.0, TODAY),
             Line("CCC", 0, 10.0, "EUR", 1.0, TODAY)]  # fmt: skip
    target = {"AAA": 0.5, "CCC": 0.1}  # BBB out; most of its 5000 isn't needed for CCC
    r = rebalance(lines, target, 0.0, S, Opts(mode="fewest", rounding="exact", te_tol=1.0,
                                              tc_bps=0, today=TODAY))  # fmt: skip
    eq = rebalance(lines, target, 0.0, S, Opts(mode="exact", tc_bps=0, today=TODAY))
    assert r.summary["cash_after"] > 100.0
    assert {x["symbol"] for x in r.trades} == {"BBB", "CCC"}
    bk = Book(lines, target, 0.0, S, Opts(today=TODAY))
    forced = np.array([False, True, True])
    te_le = te_of_set(bk, forced, le=True)[0]
    te_eq = te_of_set(bk, forced, le=False)[0]
    assert te_le <= te_eq + 1e-12
    assert eq.summary["te"] < 1e-6


# ------------------------------------------------------------ 4. edges


def _book():
    S = np.diag([0.04, 0.09, 0.0625, 0.04]) + 0.01
    lines = [
        Line("AAA", 100, 50.0, "EUR", 1.0, TODAY, 500.0),
        Line("BBB", 10.5, 1899.5, "GBp", 1.17, TODAY, -50.0),  # pence, fractional holding
        Line("CCC", 20, 120.0, "USD", 0.86, TODAY, 300.0),
        Line("DDD", 0, 300.0, "EUR", 1.0, TODAY),  # new
    ]
    return lines, S


def test_pence_are_valued_in_pounds():
    lines, S = _book()
    bk = Book(lines, {"AAA": 1}, 0.0, S, Opts(today=TODAY))
    assert abs(bk.q[1] * bk.v[1] - 10.5 * 18.995 * 1.17) < 1e-6
    r = rebalance(lines, {"AAA": 1, "BBB": 1, "CCC": 1, "DDD": 1}, 0.0, S,
                  Opts(mode="exact", tc_bps=0, today=TODAY))  # fmt: skip
    t = next(x for x in r.trades if x["symbol"] == "BBB")
    assert t["trade_ccy"] == "GBP" and abs(t["value_quote"] - t["shares"] * 18.995) < 1e-3


def test_a_fractional_holding_can_be_sold_whole_in_share_mode():
    lines, S = _book()
    r = rebalance(lines, {"AAA": 1, "CCC": 1, "DDD": 1}, 0.0, S,
                  Opts(mode="shares", tc_bps=10, today=TODAY))  # fmt: skip
    t = next(x for x in r.trades if x["symbol"] == "BBB")
    assert t["side"] == "sell" and t["all"] and abs(t["shares"] - 10.5) < 1e-9
    assert all(
        abs(x["shares"] - round(x["shares"])) < 1e-9 for x in r.trades if x["symbol"] != "BBB"
    )


def test_a_missing_price_locks_the_name_and_says_so():
    lines, S = _book()
    lines[2].price, lines[2].value_fallback = None, 2000.0
    r = rebalance(lines, {"AAA": 1, "BBB": 1, "CCC": 1, "DDD": 1}, 0.0, S,
                  Opts(mode="shares", tc_bps=10, today=TODAY))  # fmt: skip
    assert "CCC" not in {x["symbol"] for x in r.trades}
    assert any("CCC: no price" in w for w in r.warnings)
    assert r.locked == [{"symbol": "CCC", "reason": "no price"}]


def test_an_unpriced_new_name_goes_to_the_names_that_track_it():
    """A new name with no price isn't bought; its weight lands on the held
    name most correlated with it, not spread pro rata."""
    vol = np.array([0.2, 0.2, 0.2])
    corr = np.array([[1, 0.95, 0.1], [0.95, 1, 0.1], [0.1, 0.1, 1]])
    S = corr * np.outer(vol, vol)
    lines = [Line("TWIN", 0, None, "EUR", 1.0, TODAY), Line("AAA", 50, 100.0, "EUR", 1.0, TODAY),
             Line("BBB", 50, 100.0, "EUR", 1.0, TODAY)]  # fmt: skip
    r = rebalance(lines, {"TWIN": 0.4, "AAA": 0.3, "BBB": 0.3}, 0.0, S,
                  Opts(mode="exact", tc_bps=0, today=TODAY))  # fmt: skip
    after = {x["symbol"]: x["weight_after"] for x in r.trades}
    assert after["AAA"] > 0.6 and "TWIN" not in after
    assert any("TWIN" in w and "not bought" in w for w in r.warnings)


def test_a_stale_price_is_refused_with_its_date():
    lines, S = _book()
    lines[3].price_date = "2026-10-01"
    r = rebalance(lines, {"AAA": 1, "BBB": 1, "CCC": 1, "DDD": 1}, 0.0, S,
                  Opts(mode="shares", tc_bps=10, today=TODAY))  # fmt: skip
    assert "DDD" not in {x["symbol"] for x in r.trades}
    assert any("DDD" in w and "2026-10-01" in w for w in r.warnings)


@pytest.mark.parametrize("mode", ["exact", "shares", "step", "fewest"])
@pytest.mark.parametrize("cash_mode", ["keep", "invest"])
def test_sells_come_first_and_cash_never_goes_below_the_floor(mode, cash_mode):
    lines, S = _book()
    r = rebalance(lines, {"AAA": 1, "BBB": 2, "CCC": 1, "DDD": 3}, 1000.0, S,
                  Opts(mode=mode, step=100, cash=cash_mode, tc_bps=25, today=TODAY))  # fmt: skip
    sides = [x["side"] for x in r.trades]
    assert sides == sorted(sides, key=lambda s: s != "sell")
    floor = 1000.0 if cash_mode == "keep" else 0.0
    assert r.summary["cash_after"] >= floor - 0.01
    if cash_mode == "invest" and mode == "exact":
        assert r.summary["cash_after"] < 1.0


@pytest.mark.parametrize("mode", ["exact", "shares", "step"])
def test_no_trade_under_the_minimum_except_a_full_exit(mode):
    lines, S = _book()
    r = rebalance(lines, {"AAA": 1.05, "BBB": 1, "CCC": 1, "DDD": 1}, 0.0, S,
                  Opts(mode=mode, step=50, min_trade=400, tc_bps=10, today=TODAY))  # fmt: skip
    for x in r.trades:
        assert x["value"] >= 400 - 1e-6 or x["all"], x


def test_a_shortfall_scales_the_buys_and_says_so():
    lines, S = _book()
    for i in range(3):
        lines[i].price, lines[i].value_fallback = None, 1000.0
    r = rebalance(lines, {"AAA": 1, "DDD": 1}, 500.0, S,
                  Opts(mode="exact", cash="invest", reserve=800.0, tc_bps=10, today=TODAY))  # fmt: skip
    assert r.summary["cash_after"] >= 800 - 0.01 or r.summary["shortfall"] > 0


def test_the_gain_is_estimated_at_average_cost():
    lines, S = _book()
    r = rebalance(lines, {"BBB": 1, "CCC": 1, "DDD": 1}, 0.0, S,
                  Opts(mode="exact", tc_bps=10, today=TODAY))  # fmt: skip
    t = next(x for x in r.trades if x["symbol"] == "AAA")
    assert (
        abs(t["gain_est"] - (500.0 - 0.001 * 5000.0)) < 1e-6
    )  # all sold: the whole profit, less cost
    assert abs(r.summary["gain_est"] - sum(x["gain_est"] or 0 for x in r.trades)) < 1e-9


def test_bad_requests_are_refused():
    lines, S = _book()
    with pytest.raises(RebalanceError):
        rebalance(lines, {"AAA": 0}, 0.0, S, Opts(today=TODAY))
    with pytest.raises(RebalanceError):
        rebalance(lines, {"AAA": 1}, 0.0, S, Opts(mode="magic", today=TODAY))
    with pytest.raises(RebalanceError):
        rebalance(lines, {"AAA": 1}, 0.0, S[:2, :2], Opts(today=TODAY))
    with pytest.raises(RebalanceError):
        rebalance(lines + [lines[0]], {"AAA": 1}, 0.0, np.eye(5), Opts(today=TODAY))


@pytest.mark.parametrize("mode", ["exact", "shares", "step", "fewest"])
def test_a_name_taken_out_is_always_sold_in_full(mode):
    """Even when keeping a bit of it would track the target better than cash
    (a correlated exit, whole shares, a minimum trade)."""
    vol = np.array([0.2, 0.2, 0.2])
    corr = np.array([[1, 0.2, 0.9], [0.2, 1, 0.2], [0.9, 0.2, 1]])
    S = corr * np.outer(vol, vol)
    lines = [Line("AAA", 10, 180.0, "EUR", 1.0, TODAY), Line("BBB", 20, 150.0, "EUR", 1.0, TODAY),
             Line("OUT", 1000, 0.92, "EUR", 1.0, TODAY, 10.0)]  # fmt: skip
    r = rebalance(lines, {"AAA": 1, "BBB": 1}, 0.0, S,
                  Opts(mode=mode, min_trade=100, tc_bps=10, today=TODAY))  # fmt: skip
    out = next(x for x in r.trades if x["symbol"] == "OUT")
    assert out["all"] and out["shares"] == 1000
