"""Real-world price distribution from listed options (the stock window's σ/μ).

docs/plans/real-world-density.md is the spec and scripts/density_prototype.py
the reference recipe this module ports. The pipeline per horizon (the listed
expiry nearest 91 or 365 days, never an interpolated maturity):

    Yahoo chain -> forward from put-call parity, with the rate fixed from ^IRX
    (Yahoo's US chains are American-style; parity backs out nonsense rates)
    -> OTM implied total variance -> raw SVI with Roger Lee's wing bound
    -> Breeden-Litzenberger risk-neutral density q on a log-strike grid
    -> real-world density p by the minimum-entropy two-moment tilt:

        p(x) ∝ q(x) exp(t1 x + t2 x^2),  x = ln(S_T / F)
        E_P[S_T] = S0 exp(mu T),  Var_P[x] = (kappa * sigma_Q)^2 T

t2 = 0 is the one-parameter power tilt (Approach A), the fallback whenever the
two-moment solve fails or leaves its bounds. Two means are offered:

- the market's: mu = r + beta * ERP (CAPM);
- the analysts': one tilted curve per firm, its mean that firm's 12-month
  target, blended equally. Their common variance is shrunk by the
  between-firm variance so the blend's total variance is still
  (kappa sigma_Q)^2 T: disagreement lives inside the options' width, never
  on top of it. Targets run optimistic on average (Brav & Lehavy 2003), so
  an optional haircut halves each target's excess return over CAPM.

A quality gate (quotes, SVI fit, martingale error) hides chains whose tails
can't be trusted. This is a network-heavy detail path: it is never called
from `fetch_one`'s streaming hot path, and `/api/density` caches successes
only, for 15 minutes (§9: never cache a failure for long).

The pure functions (`fit_density`, `tilt`, `market_curve`,
`analysts_mixture`, `quality`) take plain numbers and quote lists so tests
can feed them a synthetic Black-Scholes chain without the network.
"""

from __future__ import annotations

import datetime as dt
import math
import threading
import time
import warnings

import numpy as np
from scipy.optimize import brentq, least_squares, root
from scipy.stats import norm

from convexity import cache

ERP_DEFAULT = 0.05
KAPPA_DEFAULT = {"index": 0.80, "stock": 0.92}  # physical / implied vol: placeholders (Phase 2)
HAIRCUT = 0.5  # share of the analysts' excess return over CAPM removed when the haircut is on
MAX_SPREAD = 0.6  # drop quotes whose bid-ask exceeds 60% of mid
MIN_QUOTES = 30
MAX_SVI_RMSE = 1.0  # vol points
MARTINGALE_GATE = 0.01  # |E_Q[S_T]/F - 1| above this = tails not trustworthy
GRID = 4001
OUT_POINTS = 401
TTL = 900.0
RATE_TTL = 3600.0
TARGET_MAX_AGE = 365  # days: a firm's target older than this is dropped
TARGET_STALE = 182  # days: older than this is drawn faded
HORIZONS = (91, 365)
# The expiry a horizon may use. Small caps list nothing past 5-7 months
# (OPRX's longest is 153 days): drawn as "12M", its analysts' 12-month
# targets would set a 5-month mean. Outside the window the horizon is
# "no_expiry", and the window falls back to the other one.
EXPIRY_WINDOW = {91: (45, 182), 365: (270, 550)}
_INDEX_TYPES = {"ETF", "INDEX", "MUTUALFUND"}


class DensityError(Exception):
    """A chain we can't use. ``reason`` is the empty state the page shows."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


# ----------------------------- Pricing primitives ----------------------------


def black_call(F, K, w, DF):
    sw = np.sqrt(np.maximum(w, 1e-12))
    d1 = (np.log(F / K) + 0.5 * w) / sw
    return DF * (F * norm.cdf(d1) - K * norm.cdf(d1 - sw))


def implied_w(price: float, F: float, K: float, DF: float, call: bool) -> float:
    """Implied total variance from a Black-76 price; nan when at or below intrinsic."""
    intrinsic = DF * max(F - K, 0.0) if call else DF * max(K - F, 0.0)
    if not price > intrinsic + 1e-6:
        return float("nan")

    def err(w):
        c = black_call(F, K, w, DF)
        return (c if call else c - DF * (F - K)) - price

    try:
        return brentq(err, 1e-8, 25.0)
    except ValueError:
        return float("nan")


def svi(p, k):
    """Raw SVI with the wing slope b(1+|rho|) capped at 2 (Lee's moment bound).

    Without the cap the 12M SPY wing extrapolated beyond the last quoted
    strike and pushed the density's mean 11% above the forward.
    """
    a, u, rho, m, s = p
    b = u * 2.0 / (1 + abs(rho))
    return a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + s**2))


def _mid_map(quotes) -> dict[float, tuple[float, float]]:
    """{strike: (mid, spread)} for two-sided quotes only."""
    out = {}
    for q in quotes or ():
        try:
            k, b, a = float(q["strike"]), float(q["bid"]), float(q["ask"])
        except (KeyError, TypeError, ValueError):
            continue
        if b > 0 and a > 0 and a >= b and math.isfinite(k):
            out[k] = ((a + b) / 2, a - b)
    return out


# ------------------------------ Risk-neutral fit -----------------------------


def fit_density(S0: float, T: float, rf: float, calls, puts) -> dict:
    """The risk-neutral density of x = ln(S_T/F) from one expiry's quotes.

    ``calls``/``puts`` are iterables of {strike, bid, ask}. The rate is an
    input (from ^IRX), never backed out of parity: American-style chains
    gave -4.8% at 3M. Raises DensityError("thin") when too few usable OTM
    quotes remain to fit a smile at all; the softer gate is `quality`.
    """
    if not (S0 > 0 and T > 0):
        raise DensityError("thin", "bad spot or maturity")
    DF = math.exp(-rf * T)
    cm, pm = _mid_map(calls), _mid_map(puts)
    near = [k for k in cm.keys() & pm.keys() if abs(k / S0 - 1) < 0.12]
    if not near:
        raise DensityError("thin", "no two-sided strikes near spot")
    F = float(np.median([k + (cm[k][0] - pm[k][0]) / DF for k in near]))

    rows = []
    for K, (px, spr) in pm.items():
        if K < F:
            rows.append((K, implied_w(px, F, K, DF, False), spr, px))
    for K, (px, spr) in cm.items():
        if K >= F:
            rows.append((K, implied_w(px, F, K, DF, True), spr, px))
    rows = [
        r for r in rows if np.isfinite(r[1]) and r[2] / r[3] < MAX_SPREAD and 0.3 < r[0] / F < 2.5
    ]
    if len(rows) < 6:
        raise DensityError("thin", f"{len(rows)} usable quotes")
    K = np.array([r[0] for r in rows])
    w = np.array([r[1] for r in rows])
    k = np.log(K / F)

    w_atm = float(np.median(w))
    weight = np.exp(-0.5 * (k / (2 * np.sqrt(w_atm))) ** 2)  # the body matters more than far wings
    fit = least_squares(
        lambda p: (svi(p, k) - w) * weight,
        [float(np.min(w)), 0.1, -0.3, 0.0, 0.1],
        bounds=([-1, 1e-4, -0.999, -2, 1e-3], [5, 1.0, 0.999, 2, 5]),
    )

    x = np.linspace(-5 * np.sqrt(w_atm), 5 * np.sqrt(w_atm), GRID)
    Kg = F * np.exp(x)
    C = black_call(F, Kg, np.maximum(svi(fit.x, x), 1e-8), DF)
    qK = np.clip(np.gradient(np.gradient(C, Kg), Kg) / DF, 0, None)
    qx = qK * Kg
    qx /= np.trapezoid(qx, x)
    m1 = np.trapezoid(qx * x, x)
    sigma_q = float(np.sqrt(max(np.trapezoid(qx * x**2, x) - m1**2, 1e-12)) / np.sqrt(T))
    return {
        "T": T,
        "S0": float(S0),
        "F": F,
        "rf": float(rf),
        "x": x,
        "q": qx,
        "sigma_q": sigma_q,
        "w_atm": w_atm,
        "quotes": len(K),
        "svi_rmse_volpts": float(np.sqrt(np.mean((np.sqrt(np.maximum(svi(fit.x, k), 0) / T) - np.sqrt(w / T)) ** 2)) * 100),
        "martingale_err": float(np.trapezoid(qx * np.exp(x), x) - 1),
    }  # fmt: skip


def quality(rn: dict) -> tuple[bool, str]:
    """The gate: enough quotes, a tight smile fit, and a density whose mean is
    the forward. A failure is shown as "Thin options data", never drawn."""
    if not all(math.isfinite(rn[k]) for k in ("F", "sigma_q", "svi_rmse_volpts", "martingale_err")):
        return False, "fit failed"  # a NaN passes every comparison below
    if rn["quotes"] < MIN_QUOTES:
        return False, f"{rn['quotes']} quotes"
    if rn["svi_rmse_volpts"] >= MAX_SVI_RMSE:
        return False, f"smile fit {rn['svi_rmse_volpts']:.1f} vol pts"
    if abs(rn["martingale_err"]) >= MARTINGALE_GATE:
        return False, f"tails off by {rn['martingale_err']:+.1%}"
    return True, ""


# ---------------------------------- Tilts -----------------------------------


def _moments(x, p):
    m = float(np.trapezoid(p * x, x))
    return m, float(np.trapezoid(p * (x - m) ** 2, x))


def tilt(rn: dict, mean_ratio: float, var_target: float) -> tuple[np.ndarray, str, list[float]]:
    """Approach B (two moments) with Approach A (t2 = 0) as the fallback.

    ``mean_ratio`` is the target E[S_T]/S0, ``var_target`` the target Var[x].
    """
    x, q = rn["x"], rn["q"]
    R = rn["F"] * np.exp(x) / rn["S0"]

    def dens(t1, t2):
        z = t1 * x + t2 * x**2
        p = q * np.exp(z - z.max())
        return p / np.trapezoid(p, x)

    try:
        g = brentq(lambda t1: np.trapezoid(dens(t1, 0) * R, x) - mean_ratio, -40, 40)
    except ValueError:
        # The target mean lies outside what an exponential tilt of q can reach
        # (a far-off analyst target): fall back to the nearest end.
        g = 40.0 if np.trapezoid(dens(40, 0) * R, x) < mean_ratio else -40.0

    def eqs(th):
        p = dens(*th)
        m = np.trapezoid(p * x, x)
        return [
            np.trapezoid(p * R, x) / mean_ratio - 1,
            np.trapezoid(p * (x - m) ** 2, x) / var_target - 1,
        ]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sol = root(eqs, [g, 0.0])
    if sol.success and abs(sol.x[1]) < 50 and max(abs(v) for v in eqs(sol.x)) < 1e-4:
        return dens(*sol.x), "B", [float(v) for v in sol.x]
    return dens(g, 0), "A", [float(g), 0.0]


def capm_ratio(rn: dict, beta: float, erp: float) -> float:
    return math.exp((rn["rf"] + beta * erp) * rn["T"])


def market_curve(rn: dict, beta: float, erp: float, kappa: float) -> dict:
    """The real-world curve with the market's (CAPM) mean."""
    var_target = (kappa * rn["sigma_q"]) ** 2 * rn["T"]
    p, method, theta = tilt(rn, capm_ratio(rn, beta, erp), var_target)
    return {"p": p, "method": method, "theta": theta}


def analysts_mixture(
    rn: dict, targets, beta: float, erp: float, kappa: float, haircut: bool = False
) -> dict:
    """The analysts' curve: one options-shaped tilt per firm, blended equally.

    ``targets`` are 12-month prices in the chain's currency. Each firm's
    curve has its target as the mean and a common variance V_w shrunk by the
    between-firm variance of ln(target), floored at 30% of V, so the blend's
    total variance stays at V = (kappa sigma_Q)^2 T.

    The listed expiry is rarely exactly a year out (AAPL 335 days, ARKK 461):
    a target's log return is scaled to T geometrically up to a year, and the
    market's (CAPM) return carries it on after that.
    """
    t = np.array([float(v) for v in targets if v and math.isfinite(v) and v > 0])
    if not len(t):
        raise DensityError("no_targets")
    T = rn["T"]
    V = (kappa * rn["sigma_q"]) ** 2 * T
    lr = np.log(t / rn["S0"]) * min(T, 1.0) + max(T - 1.0, 0.0) * (rn["rf"] + beta * erp)
    if haircut:
        lc = math.log(capm_ratio(rn, beta, erp))
        lr = lc + (1 - HAIRCUT) * (lr - lc)
    between = float(np.var(lr)) if len(lr) > 1 else 0.0
    Vw = max(0.3, 1 - between / V) * V
    parts = [tilt(rn, float(math.exp(v)), Vw) for v in lr]
    p = np.mean([pp[0] for pp in parts], axis=0)
    p /= np.trapezoid(p, rn["x"])
    methods = {pp[1] for pp in parts}
    return {
        "p": p,
        "method": "B" if methods == {"B"} else "A" if methods == {"A"} else "B/A",
        "targets": t.tolist(),
    }


def downsample(x: np.ndarray, p: np.ndarray, n: int = OUT_POINTS) -> dict:
    """The curve on ``n`` points over its 0.01%..99.99% range, rounded for JSON."""
    cdf = np.cumsum(p) * (x[1] - x[0])
    lo = x[min(np.searchsorted(cdf, 1e-4), len(x) - 1)]
    hi = x[min(np.searchsorted(cdf, 1 - 1e-4), len(x) - 1)]
    xs = np.linspace(lo, hi, n)
    ps = np.interp(xs, x, p)
    if not (np.all(np.isfinite(ps)) and math.isfinite(lo) and math.isfinite(hi) and hi > lo):
        raise DensityError("error", "non-finite curve")  # json.dumps would write NaN
    return {"x": [round(float(v), 6) for v in xs], "p": [round(float(v), 6) for v in ps]}


def curve_stats(rn: dict, p: np.ndarray) -> dict:
    """Mean ratio and variance of x, so the page can check what it draws."""
    m, v = _moments(rn["x"], p)
    mean_ratio = float(np.trapezoid(p * rn["F"] * np.exp(rn["x"]), rn["x"]) / rn["S0"])
    return {"mean_ratio": round(mean_ratio, 6), "var_x": round(v, 6), "mean_x": round(m, 6)}


# ------------------------------ Network layer -------------------------------


_RATE: dict[str, tuple[float, float]] = {}
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _rate() -> float:
    """3-month T-bill yield from ^IRX, an hour at a time."""
    hit = _RATE.get("irx")
    if hit and time.time() - hit[0] < RATE_TTL:
        return hit[1]
    import yfinance as yf

    h = yf.Ticker("^IRX").history(period="5d")
    r = float(h["Close"].dropna().iloc[-1]) / 100
    if not (-0.01 < r < 0.2):
        raise DensityError("error", f"^IRX {r}")
    _RATE["irx"] = (time.time(), r)
    return r


def _quotes(df) -> list[dict]:
    if df is None or df.empty:
        return []
    return [
        {"strike": float(k), "bid": float(b), "ask": float(a)}
        for k, b, a in zip(df["strike"], df["bid"], df["ask"], strict=False)
        if b == b and a == a
    ]


def _expiries(tk) -> list[tuple[str, int]]:
    today = dt.date.today()
    out = []
    for e in tk.options or ():
        try:
            d = (dt.date.fromisoformat(e) - today).days
        except ValueError:
            continue
        if d > 0:
            out.append((e, d))
    return out


def _atm_w(tk, expiry: str, days: int, S0: float, rf: float) -> float | None:
    """ATM implied total variance of one expiry (the call nearest the forward)."""
    try:
        ch = tk.option_chain(expiry)
    except Exception:
        return None
    T = days / 365
    DF = math.exp(-rf * T)
    F = S0 / DF
    cm = _mid_map(_quotes(ch.calls))
    if not cm:
        return None
    K = min(cm, key=lambda k: abs(k - F))
    w = implied_w(cm[K][0], F, K, DF, True)
    return w if math.isfinite(w) else None


def _earnings(tk, chain_sym: str, exps, S0: float, rf: float, horizon: int) -> list[dict]:
    """Earnings dates inside the horizon, with the options' implied jump.

    sigma_e^2 = w_after - w_before * T_after / T_before from the ATM total
    variance of the expiries either side of the next report (the earlier one
    holds no event, its vol is taken as the diffusion). Later reports are
    estimated a quarter apart with the same jump.
    """
    from convexity import attention

    ev = attention.fetch_events(chain_sym) or {}
    e = ev.get("earnings")
    if not e:
        return []
    try:
        ed = (dt.date.fromisoformat(e) - dt.date.today()).days
    except ValueError:
        return []
    if ed < 0 or ed > horizon:
        return []
    before = [x for x in exps if 2 <= x[1] < ed]
    after = [x for x in exps if x[1] > ed]
    var_e = 0.0
    if before and after:
        b, a = before[-1], after[0]
        wb, wa = _atm_w(tk, b[0], b[1], S0, rf), _atm_w(tk, a[0], a[1], S0, rf)
        if wb and wa:
            var_e = max(0.0, wa - wb * a[1] / b[1])
            var_e = min(var_e, 0.09)  # a 30% one-day sd is a data error, not a report
    out, d, first = [], ed, True
    while d <= horizon:
        day = (dt.date.today() + dt.timedelta(days=d)).isoformat()
        out.append({"date": day, "days": d, "var": round(var_e, 6), "estimated": not first})
        first, d = False, d + 91
    return out


def _analysts(tk) -> dict | None:
    """Each firm's latest 12-month target (yfinance ``upgrades_downgrades``),
    Yahoo's summary as the fallback. Prices are in the chain's currency."""
    today = dt.datetime.now()
    firms = []
    try:
        ud = tk.upgrades_downgrades
    except Exception:
        ud = None
    if ud is not None and not ud.empty and "currentPriceTarget" in ud.columns:
        df = ud.reset_index()
        datecol = "GradeDate" if "GradeDate" in df.columns else df.columns[0]
        df = df[df["currentPriceTarget"].fillna(0).between(1e-9, 1e12)]
        df = df.sort_values(datecol).groupby("Firm").tail(1)
        for _, r in df.iterrows():
            when = r[datecol].to_pydatetime().replace(tzinfo=None)
            age = (today - when).days
            if age > TARGET_MAX_AGE:
                continue
            prior = r.get("priorPriceTarget")
            firms.append({
                "firm": str(r["Firm"]),
                "rating": str(r.get("ToGrade") or ""),
                "target": round(float(r["currentPriceTarget"]), 4),
                "prior": round(float(prior), 4) if prior and prior == prior and prior > 0 else None,
                "date": when.date().isoformat(),
                "stale": age > TARGET_STALE,
                "raised_90d": bool(age <= 90 and prior and prior == prior and r["currentPriceTarget"] > prior),
            })  # fmt: skip
    if not firms:
        try:
            s = tk.analyst_price_targets or {}
        except Exception:
            s = {}

        def num(k):
            try:
                v = float(s.get(k))
            except (TypeError, ValueError):
                return None
            return v if math.isfinite(v) and v > 0 else None

        med = num("median") or num("mean")
        if not med:
            return None
        return {"firms": [], "consensus": med, "low": num("low"), "high": num("high"), "n": None}
    t = sorted(f["target"] for f in firms)
    return {
        "firms": sorted(firms, key=lambda f: f["target"]),
        "consensus": float(np.median(t)),
        "low": t[0],
        "high": t[-1],
        "n": len(firms),
    }


def _resolve_chain(symbol: str):
    """(chain ticker, its yfinance Ticker, expiries). Non-US listings have no
    chains on Yahoo: their company's US line (or ADR) stands in."""
    import yfinance as yf

    from convexity import symbol_db

    tried = []
    for cand in (symbol, symbol_db.us_line(symbol), None):
        if cand is None:
            try:
                cand = symbol_db.discover_us_line(symbol)
            except Exception:
                cand = None
        if not cand or cand in tried:
            continue
        tried.append(cand)
        tk = yf.Ticker(cand)
        try:
            exps = _expiries(tk)
        except Exception:
            exps = []
        if exps:
            return cand, tk, exps
    raise DensityError("no_options")


def _last(tk) -> tuple[float, str | None]:
    fi = tk.fast_info
    px = float(fi.last_price)
    try:
        ccy = fi.currency
    except Exception:
        ccy = None
    return px, ccy


def _settings(prefs: dict | None) -> dict:
    p = prefs or {}
    return {
        "erp": float(p.get("dist_erp", ERP_DEFAULT)),
        "kappa_stock": float(p.get("dist_kappa_stock", KAPPA_DEFAULT["stock"])),
        "kappa_index": float(p.get("dist_kappa_index", KAPPA_DEFAULT["index"])),
        "measure": p.get("dist_measure", "P") if p.get("dist_measure") in ("P", "Q") else "P",
        "haircut": bool(p.get("dist_haircut", False)),
    }


def _lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def density(
    symbol: str, horizon: int = 365, beta: float | None = None, prefs: dict | None = None
) -> dict:
    """The `/api/density` payload. Never raises: an unusable chain is
    ``{"ok": False, "reason": "no_options" | "no_expiry" | "thin" | "error"}``.

    Cached per (symbol, horizon, beta, settings) for 15 minutes, successes
    only; one computation per key at a time (a double-click must not fetch
    the chain twice)."""
    symbol = (symbol or "").strip().upper()
    horizon = min(HORIZONS, key=lambda h: abs(h - int(horizon or 365)))
    beta = 1.0 if beta is None or not math.isfinite(beta) else min(max(float(beta), 0.0), 3.0)
    st = _settings(prefs)
    key = f"density|{symbol}|{horizon}|{beta:.3f}|{sorted(st.items())}"
    hit = cache._cache_get(key)
    if hit is not None:
        return hit
    with _lock(key):
        hit = cache._cache_get(key)
        if hit is not None:
            return hit
        t0 = time.time()
        try:
            out = _compute(symbol, horizon, beta, st)
        except DensityError as e:
            print(f"[density] {symbol} {horizon}d: {e.reason} {e.detail}")
            return {
                "ok": False,
                "reason": e.reason,
                "detail": e.detail,
                "symbol": symbol,
                "horizon": horizon,
            }
        except Exception as e:  # a Yahoo hiccup: say so, try again next open
            print(f"[density] {symbol} {horizon}d failed: {type(e).__name__}: {e}")
            return {
                "ok": False,
                "reason": "error",
                "detail": type(e).__name__,
                "symbol": symbol,
                "horizon": horizon,
            }
        print(f"[density] {symbol} {horizon}d via {out['chain_symbol']} in {time.time() - t0:.1f}s")
        if out["ok"]:
            cache._cache_put(key, out, TTL)
        return out


def _compute(symbol: str, horizon: int, beta: float, st: dict) -> dict:
    import yfinance as yf

    chain_sym, tk, exps = _resolve_chain(symbol)
    lo, hi = EXPIRY_WINDOW[horizon]
    usable = [e for e in exps if lo <= e[1] <= hi]
    if not usable:
        raise DensityError("no_expiry", f"longest expiry {max(e[1] for e in exps)} days")
    expiry, days = min(usable, key=lambda e: abs(e[1] - horizon))
    S0, chain_ccy = _last(tk)
    if chain_sym == symbol:
        ratio, ccy = 1.0, chain_ccy
    else:
        local, ccy = _last(yf.Ticker(symbol))
        ratio = local / S0
    if not (math.isfinite(S0) and S0 > 0 and math.isfinite(ratio) and ratio > 0):
        raise DensityError("error", "no last price")
    rf = _rate()
    ch = tk.option_chain(expiry)
    rn = fit_density(S0, days / 365, rf, _quotes(ch.calls), _quotes(ch.puts))
    ok, why = quality(rn)

    try:
        qtype = (tk.info or {}).get("quoteType") or ""
    except Exception:
        qtype = ""
    is_index = qtype.upper() in _INDEX_TYPES
    kappa = st["kappa_index"] if is_index else st["kappa_stock"]

    base = {
        "ok": ok,
        "reason": "" if ok else "thin",
        "symbol": symbol,
        "chain_symbol": chain_sym,
        "ratio": round(ratio, 8),
        "currency": ccy,
        "chain_currency": chain_ccy,
        "s0": round(S0 * ratio, 6),
        "chain_s0": round(S0, 6),
        "forward": round(rn["F"] * ratio, 6),
        "expiry": expiry,
        "days": days,
        "T": round(rn["T"], 6),
        "rf": round(rf, 6),
        "sigma_q": round(rn["sigma_q"], 6),
        "kappa": kappa,
        "is_index": is_index,
        "beta": beta,
        "erp": st["erp"],
        "measure": st["measure"],
        "haircut": st["haircut"],
        "horizon": horizon,
        "quality": {
            "quotes": rn["quotes"],
            "svi_rmse_volpts": round(rn["svi_rmse_volpts"], 3),
            "martingale_err": round(rn["martingale_err"], 5),
            "ok": ok,
            "why": why,
        },
    }
    if not ok:
        return base

    mk = market_curve(rn, beta, st["erp"], kappa)
    curves = {
        "rn": {**downsample(rn["x"], rn["q"]), **curve_stats(rn, rn["q"]), "method": "Q"},
        "market": {
            **downsample(rn["x"], mk["p"]),
            **curve_stats(rn, mk["p"]),
            "method": mk["method"],
        },
        "analysts": None,
    }
    an = None
    try:
        an = _analysts(tk)
    except Exception as e:
        print(f"[density] analysts {chain_sym}: {type(e).__name__}")
    if an:
        an = {**an, "consensus": an["consensus"]}
        if horizon == 365:
            targets = [f["target"] for f in an["firms"]] or [an["consensus"]]
            try:
                mx = analysts_mixture(rn, targets, beta, st["erp"], kappa, st["haircut"])
                curves["analysts"] = {
                    **downsample(rn["x"], mx["p"]),
                    **curve_stats(rn, mx["p"]),
                    "method": mx["method"],
                }
            except DensityError:
                pass
    try:
        earnings = _earnings(tk, chain_sym, exps, S0, rf, days)
    except Exception as e:
        print(f"[density] earnings {chain_sym}: {type(e).__name__}")
        earnings = []
    return {**base, "curves": curves, "analysts": an, "earnings": earnings}
