"""Rebalance to a target: the trades that take the real book to a portfolio's
weights ("Rebalance to this portfolio", Phase 4).

Pure numpy, no I/O: the caller (``whatif.rebalance_payload``) hands in the
live book (shares, prices in the quote unit, the rate to the display
currency, cash), the target weights, the covariance and the options; this
module answers the trades and a summary. Nothing here places an order or
writes anything: it is arithmetic toward the owner's own target, never advice.

Notation (all money in the display currency):
    v_i   value of one share of i            = price_in_major(p_i) * fx_i
    q_i   shares held now (0 for a new name)
    n_i   shares after the trades (the decision)
    C     cash now;  F the cash floor (keep: C, invest: the reserve)
    V     the book's value now               = sum q_i v_i + C
    K(n)  trading cost                       = tc * sum |n_i - q_i| v_i
    V'    value after costs                  = V - K(n)
    w_i   weight after the trades            = n_i v_i / V'
    w*_i  target weight                      = t_i (V' - F) / V'
    TE^2  ex-ante tracking variance          = (w - w*)' S (w - w*)
    C'    cash after                         = C + sum (q_i - n_i) v_i - K(n)  >= F

The cost is paid out of the money being invested (w* scales with V' - F), so
Exact lands at TE 0 when nothing is locked.

Modes (``Opts.mode``):
    shares  whole shares (the default: most EU brokers trade single stocks in
            whole shares). n_i - q_i is an integer, and selling a fractional
            holding in full is always allowed.
    exact   fractional shares, straight to the target.
    step    fractional shares, every trade a multiple of a value step (EUR 100).
    fewest  the fewest trades within ``te_tol`` of TE: backward elimination of
            trades on the continuous solve (a re-solved budget each time, with
            a "<= budget" branch so proceeds may stay in cash), then
            ``rounding`` (shares by default) inside the chosen set. Names taken
            out are always sold and new names always bought (``force_exits``).

Why TE and not a cost penalty: a certainty-equivalent cost weight
(2/(delta h)) treats the target as optional (on a 27-name book at h = 1 month
it made 2 trades and left TE at 4.9% of 5.1%). The target is the owner's; the
trade-off between trades and closeness belongs to Fewest trades, where the
owner reads it as "within 0.5% tracking error". ``lam`` is only a tie-break.

Rounding is a lattice search (two starts, a cash repair, steepest descent over
single and pair moves, iterated local search); with at most ``ENUM_NAMES``
tradeable names the box around the continuous solution is enumerated exactly
(chunked, a few hundred ms at most), so small books get the proven optimum
within the box. Measured against brute force on 400+ random books
(tests/test_rebalance.py): optimal in 90%+ of cases, p95 gap under 0.1 bp.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from convexity.helpers import major_ccy, price_in_major

MODES = ("shares", "exact", "step", "fewest")
ROUNDINGS = ("shares", "exact", "step")
ENUM_NAMES = 8  # tradeable names up to which the lattice box is enumerated exactly
ENUM_GRID = 2_000_000  # ...unless the box is larger than this many points
_CHUNK = 200_000  # candidates scored at a time (memory: chunk x names floats)
_SWAPS_PER_STEP = 6  # fewest trades: 1-swap improvements tried after each drop


class RebalanceError(ValueError):
    """The request can't be computed (no target weight, nothing valued)."""


@dataclass
class Line:
    """One name: in the book, in the target, or both."""

    symbol: str
    shares: float  # held now; 0 for a name only in the target
    price: float | None  # last close in the QUOTE unit (pence for GBp); None = no price
    quote_ccy: str
    fx: float | None  # display units per one MAJOR unit of the quote currency
    price_date: str | None = None  # ISO date of that close
    profit_held: float | None = None  # unrealised profit at average cost, display ccy
    value_fallback: float | None = None  # the page's value when there is no price
    name: str | None = None


@dataclass
class Opts:
    mode: str = "shares"
    rounding: str = "shares"  # fewest: how to round inside the chosen set
    step: float = 100.0  # value step, display currency (mode / rounding "step")
    min_trade: float = 0.0  # display currency; a full exit is exempt
    cash: str = "keep"  # keep | invest
    reserve: float = 0.0  # invest: the cash kept back
    tc_bps: float = 10.0
    te_tol: float = 0.005  # annualised, for "fewest"
    n_trades: int | None = None  # fewest: a point picked on the path (the slider)
    force_exits: bool = True
    lam: float = 1e-3  # cost (fraction of V) in the rounding objective: a tie-break
    stale_days: int = 7  # calendar days a close may be old before the name is refused
    today: str | None = None
    kicks: int = 40  # iterated local search rounds
    seed: int = 0
    enumerate: bool = True


@dataclass
class Result:
    trades: list[dict]
    summary: dict
    warnings: list[str] = field(default_factory=list)
    locked: list[dict] = field(default_factory=list)
    path: list[dict] | None = None
    n: np.ndarray | None = None


# --------------------------------------------------------------- the inputs


class Book:
    """The arrays the engine works on, built once per request."""

    def __init__(self, lines: list[Line], target: dict, cash: float, sigma, opts: Opts):
        if len({ln.symbol for ln in lines}) != len(lines):
            raise RebalanceError("each name may appear once")
        self.lines, self.opts, self.warn, self.locked_why = lines, opts, [], {}
        self.sym = [ln.symbol for ln in lines]
        m = len(lines)
        self.q = np.array([max(0.0, float(ln.shares or 0.0)) for ln in lines], dtype=float)
        self.v = np.zeros(m)
        self.locked = np.zeros(m, dtype=bool)
        today = date.fromisoformat(opts.today) if opts.today else date.today()

        def num(x) -> float:
            try:
                f = float(x)
            except (TypeError, ValueError):
                return 0.0
            return f if math.isfinite(f) and f > 0 else 0.0

        tot_t = sum(num(x) for x in target.values())
        if tot_t <= 0:
            raise RebalanceError("the target has no weight")
        self.t = np.array([num(target.get(s, 0.0)) / tot_t for s in self.sym])
        val_now = np.zeros(m)
        for i, ln in enumerate(lines):
            major = price_in_major(ln.price, ln.quote_ccy)
            why = None
            if major is None or not math.isfinite(major) or major <= 0:
                why = "no price"
            elif ln.fx is None or not math.isfinite(ln.fx) or ln.fx <= 0:
                why = f"no {major_ccy(ln.quote_ccy)} rate"
            elif ln.price_date:
                try:
                    age = (today - date.fromisoformat(ln.price_date[:10])).days
                except ValueError:
                    age = 0
                if age > opts.stale_days:
                    why = f"its last price is from {ln.price_date[:10]}"
            if why:
                # Held: kept as is, counted at the page's value. Not held: not
                # bought; its weight goes to the names that track it best (the
                # TE solve below routes around a fixed name).
                self.locked[i] = True
                self.locked_why[ln.symbol] = why
                val_now[i] = num(ln.value_fallback)
                if self.q[i] > 0 and val_now[i] > 0:
                    self.v[i] = val_now[i] / self.q[i]
                held = self.q[i] > 0
                self.warn.append(
                    f"{ln.symbol}: {why}, so it is "
                    + (
                        "left as it is."
                        if held
                        else "not bought: its weight goes to the names that track it best."
                    )  # fmt: skip
                )
                continue
            self.v[i] = major * ln.fx
            val_now[i] = self.q[i] * self.v[i]
        try:
            self.cash = float(cash)
        except (TypeError, ValueError):
            self.cash = 0.0
        if not math.isfinite(self.cash):
            self.cash = 0.0
        self.V = float(val_now.sum() + self.cash)
        if self.V <= 0:
            raise RebalanceError("the book has no value to rebalance")
        # Keep: cash stays what it is (never below today's). Invest: down to the reserve.
        self.F = self.cash if opts.cash != "invest" else max(0.0, float(opts.reserve or 0.0))
        self.tc = max(0.0, float(opts.tc_bps)) / 1e4
        S = np.asarray(sigma, dtype=float)
        if S.shape != (m, m) or not np.isfinite(S).all():
            raise RebalanceError("the covariance doesn't match the names")
        self.S = 0.5 * (S + S.T)
        self.free = ~self.locked
        self.exit = (self.t == 0) & (self.q > 0)
        self.new = (self.t > 0) & (self.q == 0)
        # A name taken out is sold in full in every mode (owner's rule): it is
        # fixed at 0, never a variable. Otherwise the TE solve would keep a bit
        # of a correlated exit rather than hold cash (seen in Fewest trades).
        self.n_base = np.where(self.exit & self.free, 0.0, self.q)
        self.movable = self.free & ~(self.exit & self.free)

    # ---- small helpers
    def cost(self, n) -> float:
        return self.tc * float(np.sum(np.abs(n - self.q) * self.v))

    def cash_after(self, n) -> float:
        return self.cash + float(np.sum((self.q - n) * self.v)) - self.cost(n)

    def wstar(self, Vp: float) -> np.ndarray:
        return self.t * (Vp - self.F) / Vp

    def te2(self, n) -> float:
        Vp = self.V - self.cost(n)
        d = n * self.v / Vp - self.wstar(Vp)
        return float(d @ self.S @ d)

    def objective(self, n) -> float:
        """The rounding objective, measured exactly (V' moves with the cost)."""
        if self.cash_after(n) < self.F - 1e-6:
            return math.inf
        return self.te2(n) + self.opts.lam * self.cost(n) / self.V


# ------------------------------------------------- continuous solve (exact)


def _qp_active(S, wstar, fixed, w_fixed, budget):
    """min (w-w*)'S(w-w*) over the free names, the others at ``w_fixed``,
    free weights >= 0; with ``budget`` the free weights sum to it (one
    equality: KKT in closed form), with None the sum is free. Bounds by an
    active set: a name driven below 0 is pinned at 0 and the rest re-solved."""
    m = len(wstar)
    w = np.where(fixed, w_fixed, 0.0).astype(float)
    free = ~fixed.copy()
    for _ in range(m + 1):
        idx = np.flatnonzero(free)
        if not len(idx):
            return w
        fx_ = np.flatnonzero(~free)
        dF = w[fx_] - wstar[fx_]
        Sss = S[np.ix_(idx, idx)] + 1e-12 * np.eye(len(idx))
        rhs_b = S[np.ix_(idx, fx_)] @ dF if len(fx_) else np.zeros(len(idx))
        b = np.linalg.solve(Sss, rhs_b)
        if budget is None:
            e = -b
        else:
            a = np.linalg.solve(Sss, np.ones(len(idx)))
            # ``budget`` is the originally free names' total; those pinned at 0 add 0
            half_nu = (budget - wstar[idx].sum() + b.sum()) / a.sum()
            e = half_nu * a - b
        y = wstar[idx] + e
        if (y >= -1e-12).all():
            w[idx] = np.maximum(y, 0.0)
            return w
        free[idx[y < 0]] = False
        w[idx[y < 0]] = 0.0
    return w


def _qp_budget(S, wstar, fixed, w_fixed, budget, le=False):
    """The budget as an equality, or with ``le`` as "at most": if the best
    weights with a free sum already fit, the rest stays in cash (the convex
    problem's constraint is then inactive), else the equality is the answer."""
    if le:
        w = _qp_active(S, wstar, fixed, w_fixed, None)
        if w[~fixed].sum() <= budget + 1e-12:
            return w
    return _qp_active(S, wstar, fixed, w_fixed, budget)


def solve_continuous(bk: Book, active=None, n_fix=None, le: bool = False, iters: int = 8):
    """Fractional shares closest to the target, the trading cost solved as a
    fixed point (it is paid out of the money invested; the map contracts by
    tc, 2-3 rounds). ``active``: the names allowed to move; the others stay at
    ``n_fix`` (default: as held). ``le``: proceeds may stay in cash."""
    act = bk.movable.copy() if active is None else (active & bk.movable)
    nf = bk.n_base if n_fix is None else n_fix
    K = 0.0
    n = nf.copy()
    for _ in range(iters):
        Vp = bk.V - K
        ws = bk.wstar(Vp)
        fixed = ~act
        w_fixed = nf * bk.v / Vp
        budget = (bk.V - bk.F - K - float(np.sum((nf * bk.v)[fixed]))) / Vp
        budget = max(budget, 0.0)  # the floor can't be met even selling all that may trade
        w = _qp_budget(bk.S, ws, fixed, w_fixed, budget, le)
        n = np.where(act, np.divide(w * Vp, bk.v, out=np.zeros_like(w), where=bk.v > 0), nf)
        K_new = bk.cost(n)
        if abs(K_new - K) < 1e-6:
            break
        K = K_new
    return n


# ----------------------------------------------------- the lattice search


def _points(bk: Book, i: int, unit: float, hi_shares: float) -> np.ndarray:
    """Every share count name i may end at: q + k*unit (>= 0), 0 (a full
    exit, always allowed, so a fractional holding can be sold whole), q
    itself, minus the trades smaller than the minimum."""
    q, v, mt = bk.q[i], bk.v[i], bk.opts.min_trade
    kmin = math.ceil(-q / unit - 1e-9)
    kmax = math.ceil((hi_shares - q) / unit) + 3
    pts = q + np.arange(kmin, kmax + 1) * unit
    pts = pts[pts > -1e-9]
    if mt > 0:
        pts = pts[(np.abs(pts - q) * v >= mt - 1e-9) | (np.abs(pts - q) < 1e-12)]
    return np.unique(np.round(np.r_[pts, 0.0, q], 10))


class _Lattice:
    """Each name's allowed share counts and a steepest-descent search over
    single moves (one name one step) and pair moves (one up, another down: a
    swap that keeps the cash about level), always within the cash floor.
    Objective J = TE^2 + lam * K / V, with V' and w* frozen at the continuous
    solution's cost during the search (a second-order effect; the result is
    re-measured exactly)."""

    def __init__(self, bk: Book, n_cont, unit_val, active, le: bool = False):
        self.bk, self.n_cont, self.le = bk, n_cont, le
        self.act = bk.movable.copy() if active is None else (active & bk.movable)
        self.units, self.pts = [], []
        for i in range(len(bk.q)):
            if not self.act[i] or bk.v[i] <= 0:
                self.units.append(1.0)
                self.pts.append(np.array([bk.n_base[i]]))
                continue
            unit = 1.0 if unit_val is None else unit_val / bk.v[i]
            self.units.append(unit)
            self.pts.append(_points(bk, i, unit, max(n_cont[i], bk.q[i]) * 1.3 + 1))
        self.V0 = bk.V - bk.cost(n_cont)
        self.ws = bk.wstar(self.V0)
        self.diagS = np.diag(bk.S)

    def n_of(self, ix) -> np.ndarray:
        return np.array([p[j] for p, j in zip(self.pts, ix, strict=True)])

    def J(self, n) -> float:
        bk = self.bk
        d = n * bk.v / self.V0 - self.ws
        return float(d @ bk.S @ d) + bk.opts.lam * bk.cost(n) / bk.V

    def nearest(self):
        ix = np.zeros(len(self.pts), dtype=int)
        for i, p in enumerate(self.pts):
            j = int(np.argmin(np.abs(p - self.n_cont[i])))
            if self.bk.t[i] == 0 and self.n_cont[i] < 1e-9:
                j = int(np.flatnonzero(p == 0.0)[0])  # a name taken out: sold in full
            ix[i] = j
        return ix

    def sequential(self):
        """Fix one name at a time on its lattice, the one whose continuous
        value is nearest a lattice point in TE terms (shares off x value x
        vol), re-solving the others with the budget each time (Babai's
        nearest-plane rounding, with the budget)."""
        bk, pts = self.bk, self.pts
        ix = self.nearest()
        open_ = self.act & np.array([len(p) > 1 for p in pts])
        n_fix = self.n_of(ix)
        n_fix[open_] = bk.n_base[open_]
        nc = self.n_cont.copy()
        vol = np.sqrt(np.maximum(self.diagS, 0.0))
        while open_.any():
            cand = np.flatnonzero(open_)
            dist = [np.min(np.abs(pts[i] - nc[i])) * bk.v[i] * vol[i] for i in cand]
            i = cand[int(np.argmin(dist))]
            j = int(np.argmin(np.abs(pts[i] - nc[i])))
            ix[i], n_fix[i] = j, pts[i][j]
            open_[i] = False
            if open_.any():
                nc = solve_continuous(bk, open_, n_fix=n_fix, le=self.le)
        return ix

    def descend(self, ix, max_iter: int = 10000):
        bk, pts, S, v, q = self.bk, self.pts, self.bk.S, self.bk.v, self.bk.q
        tc, lam, V0, ws, diagS = bk.tc, bk.opts.lam, self.V0, self.ws, self.diagS
        m = len(pts)
        ix = ix.copy()
        n = self.n_of(ix)

        def grad(n):
            return S @ (n * v / V0 - ws)

        # Over the cash floor (rounding up bought too much): step down the
        # name whose step down costs least TE per unit of cash it frees.
        feasible, guard = True, 0
        while bk.cash_after(n) < bk.F - 1e-6 and guard < 100000:
            guard += 1
            g = grad(n)
            best, bi = None, -1
            for i in range(m):
                if ix[i] == 0:
                    continue
                dn = pts[i][ix[i] - 1] - n[i]
                dl = dn * v[i] / V0
                score = (2 * dl * g[i] + dl * dl * diagS[i]) / max(-dn * v[i], 1e-12)
                if best is None or score < best:
                    best, bi = score, i
            if bi < 0:
                feasible = False
                break
            ix[bi] -= 1
            n[bi] = pts[bi][ix[bi]]
        for _ in range(max_iter):
            g = grad(n)
            cash = bk.cash_after(n)
            tr = np.abs(n - q)
            up = np.array([pts[i][ix[i] + 1] - n[i] if ix[i] + 1 < len(pts[i]) else np.nan
                           for i in range(m)])  # fmt: skip
            dn = np.array([pts[i][ix[i] - 1] - n[i] if ix[i] > 0 else np.nan for i in range(m)])
            best = (-1e-15, None)
            mv = {}
            for tag, D in (("u", up), ("d", dn)):
                ok = np.isfinite(D)
                Dz = np.where(ok, D, 0.0)
                dl = Dz * v / V0
                dcost = tc * v * (np.abs(n + Dz - q) - tr)
                dJ = 2 * dl * g + dl * dl * diagS + lam * dcost / bk.V
                dcash = -Dz * v - dcost
                mv[tag] = (ok, dl, dJ, dcash)
                feas = ok & (cash + dcash >= bk.F - 1e-6)
                if feas.any():
                    k = int(np.argmin(np.where(feas, dJ, np.inf)))
                    if dJ[k] < best[0]:
                        best = (dJ[k], ("s", tag, k))
            okU, dlU, dJU, dcU = mv["u"]
            okD, dlD, dJD, dcD = mv["d"]
            pairJ = dJU[:, None] + dJD[None, :] + 2 * np.outer(dlU, dlD) * S
            feas = okU[:, None] & okD[None, :] & (cash + dcU[:, None] + dcD[None, :] >= bk.F - 1e-6)
            np.fill_diagonal(feas, False)
            if feas.any():
                pj = np.where(feas, pairJ, np.inf)
                a, b = divmod(int(np.argmin(pj)), m)
                if pj[a, b] < best[0]:
                    best = (pj[a, b], ("p", a, b))
            if best[1] is None:
                break
            if best[1][0] == "s":
                _, tag, k = best[1]
                ix[k] += 1 if tag == "u" else -1
                n[k] = pts[k][ix[k]]
            else:
                _, a, b = best[1]
                ix[a] += 1
                ix[b] -= 1
                n[a], n[b] = pts[a][ix[a]], pts[b][ix[b]]
        return ix, feasible, (self.J(n) if feasible else math.inf)

    def search(self, kicks: int, seed: int):
        """Both starts, then ``kicks`` rounds of iterated local search: move
        three random names one step each, descend again, keep the better."""
        res = [self.descend(ix) for ix in (self.nearest(), self.sequential())]
        best = min(res, key=lambda r: r[2])
        movable = np.flatnonzero([len(p) > 1 for p in self.pts])
        rng = np.random.default_rng(seed)
        for _ in range(kicks if len(movable) > 1 else 0):
            ix = best[0].copy()
            for i in rng.choice(movable, size=min(3, len(movable)), replace=False):
                ix[i] = int(np.clip(ix[i] + rng.choice((-1, 1)), 0, len(self.pts[i]) - 1))
            r = self.descend(ix)
            if r[2] < best[2] - 1e-15:
                best = r
        return self.n_of(best[0]), best[1]

    def box(self, extra=None) -> list[np.ndarray] | None:
        """Each tradeable name's candidates for exact enumeration: the lattice
        points within one step of the continuous solution's bracket, 0, q and
        ``extra`` (the heuristic's own point, so enumeration can't do worse)."""
        out = []
        for i, p in enumerate(self.pts):
            if len(p) <= 1:
                out.append(p)
                continue
            u = self.units[i]
            lo, hi = self.n_cont[i] - 2 * u - 1e-9, self.n_cont[i] + 2 * u + 1e-9
            keep = (p >= lo) & (p <= hi)
            keep |= np.isclose(p, 0.0) | np.isclose(p, self.bk.q[i])
            if extra is not None:
                keep |= np.isclose(p, extra[i])
            out.append(p[keep])
        return out


def _enumerate(bk: Book, cands: list[np.ndarray]):
    """The best point of the box by the exact objective (cost moves V'),
    within the cash floor, scored a chunk at a time over the tradeable names
    only: with a = n v + t F, TE^2 V'^2 = a'Sa - 2V' t'Sa + V'^2 t'St, where
    the fixed names' part of each product is a constant. None when the box
    is empty of feasible points."""
    tr_idx = np.array([i for i, c in enumerate(cands) if len(c) > 1], dtype=int)
    base = np.array([c[0] if len(c) == 1 else 0.0 for c in cands])
    if not len(tr_idx):
        return base if bk.cash_after(base) >= bk.F - 1e-6 else None
    fx_idx = np.array([i for i, c in enumerate(cands) if len(c) <= 1], dtype=int)
    S, v, t, q = bk.S, bk.v, bk.t, bk.q
    a_f = base[fx_idx] * v[fx_idx] + t[fx_idx] * bk.F
    c0 = float(a_f @ S[np.ix_(fx_idx, fx_idx)] @ a_f) if len(fx_idx) else 0.0
    g = 2.0 * S[np.ix_(tr_idx, fx_idx)] @ a_f if len(fx_idx) else np.zeros(len(tr_idx))
    Stt = S[np.ix_(tr_idx, tr_idx)]
    St = S @ t
    h0 = float(St[fx_idx] @ a_f) if len(fx_idx) else 0.0
    tSt = float(t @ St)
    dq_f = base[fx_idx] - q[fx_idx]
    k_fixed = float(np.abs(dq_f) @ v[fx_idx])  # what the fixed names trade (min-trade, fewest)
    cash_fixed = bk.cash - float(dq_f @ v[fx_idx])
    sizes = [len(cands[i]) for i in tr_idx]
    total = int(np.prod(sizes))
    best_j, best_n = math.inf, None
    vt, qt, tt = v[tr_idx], q[tr_idx], t[tr_idx]
    for start in range(0, total, _CHUNK):
        flat = np.arange(start, min(total, start + _CHUNK))
        cols = []
        rem = flat
        for sz in reversed(sizes):  # mixed-radix digits, last name fastest
            rem, d = np.divmod(rem, sz)
            cols.append(d)
        cols.reverse()
        N = np.column_stack([cands[i][d] for i, d in zip(tr_idx, cols, strict=True)])
        traded = np.abs(N - qt) @ vt + k_fixed
        K = bk.tc * traded
        Vp = bk.V - K
        cash = cash_fixed + (qt - N) @ vt - K
        A = N * vt + tt * bk.F
        aSa = c0 + A @ g + np.einsum("ij,jk,ik->i", A, Stt, A)
        tSa = h0 + A @ St[tr_idx]
        te2 = (aSa - 2 * Vp * tSa + Vp * Vp * tSt) / (Vp * Vp)
        J = te2 + bk.opts.lam * K / bk.V
        J[cash < bk.F - 1e-6] = np.inf
        k = int(np.argmin(J))
        if J[k] < best_j:
            best_j = float(J[k])
            best_n = base.copy()
            best_n[tr_idx] = N[k]
    return best_n


def _round(bk: Book, n_cont, rounding: str, active=None):
    """Round the continuous solution in mode ``rounding``; (n, method, feasible)."""
    opts = bk.opts
    if rounding == "exact":
        n = n_cont.copy()
        if opts.min_trade > 0:
            # Trades under the minimum (full exits exempt) are not placed; the
            # rest re-solved, until every trade is at least the minimum.
            act = bk.movable.copy() if active is None else active & bk.movable
            for _ in range(len(n)):
                small = act & (np.abs(n - bk.q) * bk.v < opts.min_trade) & ~(bk.exit & (n == 0))
                small &= np.abs(n - bk.q) > 1e-12
                if not small.any():
                    break
                act &= ~small
                n = solve_continuous(bk, act, le=opts.mode == "fewest")
        return n, "exact", True
    unit = None if rounding == "shares" else float(opts.step)
    if unit is not None and not (math.isfinite(unit) and unit > 0):
        raise RebalanceError("the value step must be above 0")
    L = _Lattice(bk, n_cont, unit, active, le=opts.mode == "fewest")
    n, feasible = L.search(opts.kicks, opts.seed)
    method = "search"
    tradeable = sum(1 for p in L.pts if len(p) > 1)
    if opts.enumerate and 0 < tradeable <= ENUM_NAMES:
        cands = L.box(n if feasible else None)
        if np.prod([len(c) for c in cands], dtype=float) <= ENUM_GRID:
            e = _enumerate(bk, cands)
            if e is not None and bk.objective(e) <= bk.objective(n) + 1e-15:
                n, feasible, method = e, True, "enumerated"
    return n, method, feasible


# --------------------------------------------------------- fewest trades


def te_of_set(bk: Book, active, le: bool = True) -> tuple[float, np.ndarray]:
    n = solve_continuous(bk, active, le=le)
    return math.sqrt(max(bk.te2(n), 0.0)), n


def backward_path(bk: Book, n_full, swap: bool = True, le: bool = True):
    """Trades dropped one at a time, each time the one whose removal raises
    the re-solved continuous TE least; forced trades (names taken out, new
    names) are never dropped. After each drop, up to ``_SWAPS_PER_STEP``
    1-swaps (a kept trade for a dropped one) that lower TE. Returns
    [(k, te, active)] from all trades down to the forced ones. Against
    exhaustive subsets: exact on 8 names, within 0.4% at 12."""
    # Under half a unit of money is solver noise, not a trade: a book already
    # on target would otherwise show a path of 27 "trades" all at TE 0.
    trades = bk.free & (np.abs(n_full - bk.q) * bk.v > 0.5)
    forced = trades & (bk.exit | bk.new) if bk.opts.force_exits else np.zeros_like(trades)
    act = trades.copy()
    te, _ = te_of_set(bk, act, le)
    path = [(int(act.sum()), te, act.copy())]
    while True:
        cand = np.flatnonzero(act & ~forced)
        if not len(cand):
            break
        best = None
        for j in cand:
            a2 = act.copy()
            a2[j] = False
            t2, _ = te_of_set(bk, a2, le)
            if best is None or t2 < best[0]:
                best = (t2, j)
        act[best[1]] = False
        te = best[0]
        if swap:
            for _ in range(_SWAPS_PER_STEP):
                improved = False
                for a in np.flatnonzero(act & ~forced):
                    for b in np.flatnonzero(trades & ~act):
                        a2 = act.copy()
                        a2[a], a2[b] = False, True
                        t2, _ = te_of_set(bk, a2, le)
                        if t2 < te - 1e-12:
                            act, te, improved = a2, t2, True
                            break
                    if improved:
                        break
                if not improved:
                    break
        path.append((int(act.sum()), te, act.copy()))
    return path


# --------------------------------------------------------------- the run


def rebalance(lines: list[Line], target: dict, cash: float, sigma, opts: Opts) -> Result:
    """The trades from the book in ``lines`` to ``target`` ({symbol: weight},
    any scale; names in the book and not in it are sold in full)."""
    t0 = time.perf_counter()
    if opts.mode not in MODES:
        raise RebalanceError(f"unknown mode {opts.mode!r}")
    if opts.mode == "fewest" and opts.rounding not in ROUNDINGS:
        raise RebalanceError(f"unknown rounding {opts.rounding!r}")
    bk = Book(lines, target, cash, sigma, opts)
    warns = list(bk.warn)
    fewest = opts.mode == "fewest"
    n_cont = solve_continuous(bk)
    active, path = None, None
    if fewest:
        path_full = backward_path(bk, n_cont)
        path = [{"trades": k, "te": te} for k, te, _ in path_full]
        if opts.n_trades is not None:
            pick = min(path_full, key=lambda p: (abs(p[0] - int(opts.n_trades)), -p[0]))
        else:
            ok = [p for p in path_full if p[1] <= opts.te_tol + 1e-12]
            pick = min(ok, key=lambda p: p[0]) if ok else path_full[0]
        active = pick[2]
        n_cont = solve_continuous(bk, active, le=True)
    rounding = opts.rounding if fewest else opts.mode
    n, method, feasible = _round(bk, n_cont, rounding, active)
    if not feasible:
        warns.append("The cash floor could not be met by the trades allowed.")
    # Safety net: never below the floor. Scale the buys down and say by how
    # much (all sellers locked, or a reserve above the cash).
    shortfall = 0.0
    cash_after = bk.cash_after(n)
    if cash_after < bk.F - 0.01:
        buys = np.clip(n - bk.q, 0, None)
        need = float(np.sum(buys * bk.v) * (1 + bk.tc))
        shortfall = bk.F - cash_after
        s = max(0.0, (need - shortfall) / need) if need > 0 else 0.0
        n = np.where(n > bk.q, bk.q + buys * s, n)
        if rounding == "shares":
            n = np.where(n > bk.q, bk.q + np.floor(buys * s + 1e-9), n)
        warns.append(f"The target needs {shortfall:,.0f} more cash than there is: "
                     f"buys scaled to {s:.0%}.")  # fmt: skip
    out = _report(bk, n, warns, shortfall, path, time.perf_counter() - t0)
    out.summary["method"] = method
    if fewest:
        out.summary["picked"] = int(active.sum()) if active is not None else None
    return out


def _tidy(x: float) -> float:
    """Solver noise around zero (-1e-10 read "-0") is zero."""
    return 0.0 if abs(x) < 1e-6 else float(x)


def _report(bk: Book, n, warns, shortfall, path, secs) -> Result:
    K = bk.cost(n)
    Vp = bk.V - K
    w = n * bk.v / Vp
    ws = bk.wstar(Vp)
    w_now = bk.q * bk.v / bk.V
    ws_now = bk.wstar(bk.V)
    trades = []
    for i, ln in enumerate(bk.lines):
        dq = n[i] - bk.q[i]
        if abs(dq) < 1e-9:
            continue
        major = price_in_major(ln.price, ln.quote_ccy)
        val_d = abs(dq) * bk.v[i]
        gain = None
        if dq < 0 and ln.profit_held is not None and bk.q[i] > 0:
            # Average cost: the gain on the shares sold is their share of the
            # holding's unrealised profit, less this sale's own cost. Right for
            # the UK pool, France's PMP, Italy; not FIFO or specific lots.
            gain = (-dq / bk.q[i]) * float(ln.profit_held) - bk.tc * val_d
        trades.append({
            "symbol": ln.symbol, "name": ln.name or ln.symbol,
            "side": "sell" if dq < 0 else "buy",
            "shares": round(abs(dq), 6), "all": bool(n[i] == 0 and bk.q[i] > 0),
            "new": bool(bk.q[i] == 0),
            "price": ln.price, "quote_ccy": ln.quote_ccy, "price_date": ln.price_date,
            "trade_ccy": major_ccy(ln.quote_ccy),
            "value_quote": abs(dq) * major if major else None, "value": val_d,
            "weight_now": float(w_now[i]), "weight_after": float(w[i]),
            "weight_target": float(ws[i]), "gain_est": gain,
        })  # fmt: skip
    trades.sort(key=lambda x: (x["side"] != "sell", -x["value"]))
    gaps = sorted(
        ((bk.sym[i], float(w[i] - ws[i])) for i in range(len(n))), key=lambda x: -abs(x[1])
    )
    traded = float(np.sum(np.abs(n - bk.q) * bk.v))
    d_now = w_now - ws_now
    locked = [{"symbol": s, "reason": r} for s, r in bk.locked_why.items()]
    summary = {
        "n_trades": len(trades),
        "n_buys": sum(1 for x in trades if x["side"] == "buy"),
        "n_sells": sum(1 for x in trades if x["side"] == "sell"),
        "buys": float(sum(x["value"] for x in trades if x["side"] == "buy")),
        "sells": float(sum(x["value"] for x in trades if x["side"] == "sell")),
        "turnover": traded / 2 / bk.V,
        "cost": K,
        "cash_before": bk.cash,
        "cash_after": _tidy(bk.cash_after(n)),
        "cash_floor": bk.F,
        "te": math.sqrt(max(bk.te2(n), 0.0)),
        "te_before": math.sqrt(max(float(d_now @ bk.S @ d_now), 0.0)),
        "gaps": [{"symbol": s, "gap": g} for s, g in gaps[:3]],
        "shortfall": shortfall,
        "gain_est": float(sum(x["gain_est"] or 0.0 for x in trades if x["side"] == "sell")),
        "value": bk.V,
        "ms": round(secs * 1000, 1),
    }
    return Result(trades, summary, warns, locked, path, n)
