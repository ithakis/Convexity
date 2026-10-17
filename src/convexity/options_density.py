"""Real-world price distribution from listed options (the stock window's σ/μ).

docs/plans/real-world-density.md is the spec and scripts/density_prototype.py
the reference recipe this module ports. The pipeline per horizon (the listed
expiry nearest 91 or 365 days; only when none is near does the SSVI surface
give the horizon itself, `surface_at`):

    Yahoo chain -> forward from put-call parity, with the rate fixed from ^IRX
    (Yahoo's US chains are American-style; parity backs out nonsense rates)
    -> OTM implied total variance (quotes that break no-arbitrage dropped)
    -> raw SVI with Roger Lee's wing bound
    -> Breeden-Litzenberger risk-neutral density q on a log-strike grid,
       with GEV tails (Figlewski 2009) past the 5% / 95% points or the last
       quoted strike, whichever is nearer the money
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

import contextlib
import datetime as dt
import math
import threading
import time
import warnings

import numpy as np
from scipy.optimize import brentq, least_squares, root
from scipy.signal import fftconvolve
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
GRID_SD = 10  # the log-strike grid spans +-GRID_SD ATM standard deviations
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
# GEV tails (Figlewski 2009): attach at the 5% / 95% points, or at the last
# quoted strike when the quotes stop short of them, and match the density's
# decay 3 cdf points inside. The right tail's shape is capped below 1/2 so
# E[S] and Var[S] stay finite.
TAIL_ALPHA = 0.05
TAIL_GAP = 0.03
TAIL_XI = (-0.95, 0.45)
# The surface (SSVI) gate, for chains too sparse for a per-expiry SVI: enough
# quotes over enough expiries, a surface that fits them, a slice with some
# quotes of its own, and quotes that reach the 15% / 85% points (the GEV
# tails carry at most 15% each side).
SURFACE_MIN_QUOTES = 60
SURFACE_MIN_SLICES = 3
SLICE_MIN_QUOTES = 8
MAX_SURFACE_RMSE = 1.5
COVERAGE = 0.15
SURFACE_DAYS = (20, 1100)  # expiries the surface is fitted on
EXPIRY_TRIES = 4  # expiries in the window tried, nearest the horizon first
# With no expiry in the window, the horizon is read off the SSVI surface when
# the chain reaches at least this share of it (a European ADR listing to 7
# months): the shape is the surface's, the ATM variance interpolated in
# maturity, or grown at the last expiry's average rate past it (flat vol).
EXTRAP_MIN = 0.5
LINE_TTL = 86400.0  # the chosen chain listing, a day


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


def _monotone(rows, F: float) -> list:
    """Drop quotes that break static no-arbitrage by more than the spread.

    Walking out from the forward, an OTM put (call) further from the money
    can't bid above the ask of the one before it. DELL's 12M chain had one
    stale put (K=165 bid 12.15 while K=170 asked 4.40): the SVI bent into a
    Lee-slope kink to chase it, and the density's mean fell 9% below F."""
    keep = []
    for side in (
        sorted((r for r in rows if r[0] < F), key=lambda r: -r[0]),
        sorted((r for r in rows if r[0] >= F), key=lambda r: r[0]),
    ):
        ask = math.inf
        for r in side:
            _, _, spr, px = r
            if px - spr / 2 > ask:
                continue
            keep.append(r)
            ask = px + spr / 2
    return keep


def _gev(s: np.ndarray, mu: float, sig: float, xi: float) -> np.ndarray:
    """GEV density of the variable s (its right tail); 0 outside the support."""
    z = 1 + xi * (s - mu) / sig
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        t = np.maximum(z, 1e-300) ** (-1 / xi)
        g = t ** (1 + xi) * np.exp(-t) / sig
    return np.where((z > 0) & np.isfinite(g), g, 0.0)


def _gev_tail(s1: float, a1: float, f1: float, s0: float, f0: float) -> tuple:
    """(mu, sig, xi) of the GEV with G(s1) = a1 and g(s1) = f1 exactly, its
    shape xi chosen so g(s0) = f0 at the inner point (s0 < s1).

    Given xi, the first two conditions fix sig and mu in closed form, so the
    splice is continuous in both cdf and density whatever xi comes out. With
    no root inside TAIL_XI the nearest end is kept."""
    t1 = -math.log(a1)

    def par(xi):
        sig = t1 ** (1 + xi) * a1 / f1
        return s1 - sig * (t1 ** (-xi) - 1) / xi, sig

    def err(xi):
        mu, sig = par(xi)
        g0 = float(_gev(np.array([s0]), mu, sig, xi)[0])
        return math.log(g0 / f0) if g0 > 0 else -50.0

    grid = np.linspace(*TAIL_XI, 57)
    grid[np.abs(grid) < 1e-6] = 1e-6  # xi = 0 (Gumbel) is the limit, not a value
    e = [err(v) for v in grid]
    xi = float(grid[int(np.argmin(np.abs(e)))])
    for a, b, ea, eb in zip(grid[:-1], grid[1:], e[:-1], e[1:], strict=True):
        if ea * eb < 0:
            xi = brentq(err, a, b)
            break
    return (*par(xi), xi)


def _tails(Kg: np.ndarray, fK: np.ndarray, cdf: np.ndarray, kmin: float, kmax: float):
    """Replace the density beyond the quoted range with GEV tails.

    ``fK`` is the Breeden-Litzenberger density in price and ``cdf`` the
    cumulative from the call slope, 1 + e^(rT) dC/dK: it reads the smile at K
    only. Integrating fK from the left instead would count the SVI's
    extrapolated wing, whose false hump past SPY's last strike (and the
    negative density after it) pushed the mean 2.1% above F. The left tail is
    the right tail of -S. Returns (density, info) or (fK, None) when the
    quotes leave no room between the two attachment points."""
    mono = np.maximum.accumulate(np.clip(cdf, 0, 1))
    aR = min(1 - TAIL_ALPHA, float(np.interp(kmax, Kg, mono)))
    aL = max(TAIL_ALPHA, float(np.interp(kmin, Kg, mono)))
    if aR - aL < 2 * TAIL_GAP + 0.05:
        return fK, None
    # Attach on the grid inside the quoted range, never a step past it.
    iR1 = min(int(np.searchsorted(mono, aR)), int(np.searchsorted(Kg, kmax, "right")) - 1)
    iL1 = max(int(np.searchsorted(mono, aL)), int(np.searchsorted(Kg, kmin)))
    iR0 = int(np.searchsorted(mono, mono[iR1] - TAIL_GAP))
    iL0 = int(np.searchsorted(mono, mono[iL1] + TAIL_GAP))
    if not (0 < iL1 < iL0 < iR0 < iR1 < len(Kg) - 1) or min(fK[[iL1, iL0, iR0, iR1]]) <= 0:
        return fK, None
    out = fK.copy()
    muR, sR, xiR = _gev_tail(Kg[iR1], mono[iR1], fK[iR1], Kg[iR0], fK[iR0])
    out[iR1:] = _gev(Kg[iR1:], muR, sR, xiR)
    muL, sL, xiL = _gev_tail(-Kg[iL1], 1 - mono[iL1], fK[iL1], -Kg[iL0], fK[iL0])
    out[:iL1] = _gev(-Kg[:iL1], muL, sL, xiL)
    info = {
        "left": {"strike": float(Kg[iL1]), "cdf": float(mono[iL1]), "xi": float(xiL)},
        "right": {"strike": float(Kg[iR1]), "cdf": float(mono[iR1]), "xi": float(xiR)},
    }
    return out, info


# ------------------------------ Risk-neutral fit -----------------------------


def _slice(S0: float, T: float, rf: float, calls, puts) -> dict:
    """One expiry's usable quotes as implied total variance on k = ln(K/F).

    The forward is the median of K + (C - P)/DF near spot, with the rate an
    input (from ^IRX), never backed out of parity: American-style chains
    gave -4.8% at 3M. Quotes: two-sided, OTM only, spread < 60% of mid,
    0.3 < K/F < 2.5, minus any that break static no-arbitrage (`_monotone`).
    Raises DensityError("thin") when fewer than 6 remain."""
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
    rows = _monotone(rows, F)
    if len(rows) < 6:
        raise DensityError("thin", f"{len(rows)} usable quotes")
    rows.sort()
    K = np.array([r[0] for r in rows])
    return {"T": T, "S0": float(S0), "rf": float(rf), "F": F, "K": K, "k": np.log(K / F),
            "w": np.array([r[1] for r in rows])}  # fmt: skip


def _density(sl: dict, smile) -> dict:
    """Breeden-Litzenberger on a smile ``smile(k) -> total variance``, with
    GEV tails past the quoted strikes, and the numbers the gate reads.

    The smile is trusted only where it was fitted: past the quoted strikes
    its wing is a guess (SPY 12M's SVI bent up into a false hump and the mean
    came out 2.1% above F), so GEV tails take over there. ``tails`` says
    where they attach and their shapes, or is None when the quotes are too
    narrow to place them (the gate then judges the smile's own wings)."""
    T, F, K, k, w = sl["T"], sl["F"], sl["K"], sl["k"], sl["w"]
    DF = math.exp(-sl["rf"] * T)
    w_atm = float(np.median(w))
    # +-10 ATM sd (S from 1% to 20x F at most): the GEV tails are analytic, and a
    # stressed chain's left tail runs well past 5 sd of its ATM vol.
    span = GRID_SD * np.sqrt(w_atm)
    x = np.linspace(max(-span, math.log(0.01)), min(span, math.log(20.0)), GRID)
    Kg = F * np.exp(x)
    C = black_call(F, Kg, np.maximum(smile(x), 1e-8), DF)
    dC = np.gradient(C, Kg)
    qK = np.clip(np.gradient(dC, Kg) / DF, 0, None)
    qK, tails = _tails(Kg, qK, 1 + dC / DF, float(K.min()), float(K.max()))
    qx = qK * Kg
    qx /= np.trapezoid(qx, x)
    m1 = np.trapezoid(qx * x, x)
    sigma_q = float(np.sqrt(max(np.trapezoid(qx * x**2, x) - m1**2, 1e-12)) / np.sqrt(T))
    return {
        "T": T,
        "S0": sl["S0"],
        "F": F,
        "rf": sl["rf"],
        "x": x,
        "q": qx,
        "sigma_q": sigma_q,
        "w_atm": w_atm,
        "quotes": len(K),
        "svi_rmse_volpts": float(np.sqrt(np.mean((np.sqrt(np.maximum(smile(k), 0) / T) - np.sqrt(w / T)) ** 2)) * 100),
        "martingale_err": float(np.trapezoid(qx * np.exp(x), x) - 1),
        "atm_iv": float(np.sqrt(max(float(np.atleast_1d(smile(np.array([0.0])))[0]), 0.0) / T)),
        "tails": tails,
        "smile": "svi",
    }  # fmt: skip


def fit_density(S0: float, T: float, rf: float, calls, puts) -> dict:
    """The risk-neutral density of x = ln(S_T/F) from one expiry's quotes:
    raw SVI (Lee-capped) on the slice, then `_density`.

    ``calls``/``puts`` are iterables of {strike, bid, ask}. Raises
    DensityError("thin") when too few usable OTM quotes remain to fit a
    smile at all; the softer gate is `quality`."""
    return _svi_density(_slice(S0, T, rf, calls, puts))


def _svi_density(sl: dict) -> dict:
    k, w = sl["k"], sl["w"]
    w_atm = float(np.median(w))
    weight = np.exp(-0.5 * (k / (2 * np.sqrt(w_atm))) ** 2)  # the body matters more than far wings
    fit = least_squares(
        lambda p: (svi(p, k) - w) * weight,
        [float(np.min(w)), 0.1, -0.3, 0.0, 0.1],
        bounds=([-1, 1e-4, -0.999, -2, 1e-3], [5, 1.0, 0.999, 2, 5]),
    )
    return _density(sl, lambda kk: svi(fit.x, kk))


def ssvi(theta, k, rho: float, eta: float, gamma: float):
    """SSVI total variance (Gatheral & Jacquier 2014) with the power-law
    phi(theta) = eta / (theta^gamma (1 + theta)^(1 - gamma))."""
    phi = eta / (theta**gamma * (1 + theta) ** (1 - gamma))
    return theta / 2 * (1 + rho * phi * k + np.sqrt((phi * k + rho) ** 2 + 1 - rho**2))


def _fit_ssvi(slices: list[dict]):
    """SSVI on every slice (sorted by T): (theta per slice, rho, eta, gamma,
    RMSE in vol points, quote count)."""
    n = len(slices)
    th0 = []
    for s in slices:
        th0.append(max(float(np.interp(0.0, s["k"], s["w"])), 1e-4))
    th0 = np.maximum.accumulate(np.array(th0))
    d0 = np.diff(np.concatenate([[0.0], th0]))
    d0[0] = max(d0[0], 1e-4)

    def unpack(p):
        th = np.cumsum(np.maximum(p[:n], 0.0)) + 1e-6
        rho, u, gamma = p[n], p[n + 1], p[n + 2]
        return th, rho, u * 2 / (1 + abs(rho)), gamma

    def res(p):
        th, rho, eta, gamma = unpack(p)
        out = []
        for s, t in zip(slices, th, strict=True):
            wm = np.maximum(ssvi(t, s["k"], rho, eta, gamma), 1e-10)
            wt = np.exp(-0.5 * (s["k"] / (2 * np.sqrt(t))) ** 2)
            out.append((np.sqrt(wm / s["T"]) - np.sqrt(s["w"] / s["T"])) * wt)
        return np.concatenate(out)

    p0 = np.concatenate([np.maximum(d0, 1e-6), [-0.5, 0.5, 0.3]])
    lo = np.concatenate([np.zeros(n), [-0.999, 1e-3, 0.01]])
    hi = np.concatenate([np.full(n, 5.0), [0.999, 1.0, 0.5]])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = least_squares(res, np.clip(p0, lo, hi), bounds=(lo, hi))
    th, rho, eta, gamma = unpack(fit.x)
    err = [
        np.sqrt(np.maximum(ssvi(t, s["k"], rho, eta, gamma), 0) / s["T"]) - np.sqrt(s["w"] / s["T"])
        for s, t in zip(slices, th, strict=True)
    ]
    rmse = float(np.sqrt(np.mean(np.concatenate(err) ** 2)) * 100)
    return th, float(rho), float(eta), float(gamma), rmse, sum(len(s["k"]) for s in slices)


def _surface_rn(rn: dict, fit) -> dict:
    th, rho, eta, gamma, rmse, allk = fit
    rn.update({
        "smile": "ssvi",
        "surface_quotes": allk,
        "surface_slices": len(th),
        "surface_rmse_volpts": rmse,
        "ssvi": {"rho": rho, "eta": eta, "gamma": gamma},
    })  # fmt: skip
    return rn


def fit_surface(slices: list[dict], target: dict) -> dict:
    """The density at the ``target`` slice (one of ``slices``, from `_slice`)
    from an SSVI fitted to every slice.

    For chains that list ~20 strikes per expiry (PG, PEP, JNJ at 12M): one
    slice is too few points for SVI's five parameters (a 0.3 vol-pt fit
    missed PEP's forward by 2.8% on 14 quotes), but the whole
    surface has 100-200 quotes and SSVI only three shared parameters plus
    one ATM variance per expiry. No arbitrage by construction: eta(1+|rho|)
    <= 2 with gamma <= 1/2 (no butterfly, G&J Remark 4.4) and theta
    non-decreasing in T (no calendar spread)."""
    slices = sorted(slices, key=lambda s: s["T"])
    tgt = next(i for i, s in enumerate(slices) if s is target)
    fit = _fit_ssvi(slices)
    th, rho, eta, gamma = fit[:4]
    t = float(th[tgt])
    return _surface_rn(_density(slices[tgt], lambda kk: ssvi(t, kk, rho, eta, gamma)), fit)


def surface_at(slices: list[dict], T: float) -> dict:
    """The density at a maturity no expiry has, off the SSVI surface.

    Total ATM variance theta is linear in T between the fitted expiries
    (non-decreasing, so still no calendar arbitrage) and grows at the last
    expiry's average rate past it: flat implied vol beyond the chain, the
    standard extrapolation. The carry (r - q) is the nearest expiry's,
    held constant. The nearest slice's quotes, rescaled to the same ATM
    standard deviations, place the GEV tails; ``extrapolated`` says from
    which maturity."""
    slices = sorted(slices, key=lambda s: s["T"])
    fit = _fit_ssvi(slices)
    th, rho, eta, gamma = fit[:4]
    Ts = np.array([s["T"] for s in slices])
    if T <= Ts[-1]:
        t = float(np.interp(T, np.concatenate([[0.0], Ts]), np.concatenate([[0.0], th])))
    else:
        t = float(th[-1] * T / Ts[-1])
    i = int(np.argmin(np.abs(Ts - T)))
    near = slices[i]
    carry = math.log(near["F"] / near["S0"]) / near["T"]
    F = near["S0"] * math.exp(carry * T)
    k = near["k"] * math.sqrt(t / float(th[i]))
    sl = {"T": T, "S0": near["S0"], "rf": near["rf"], "F": F, "K": F * np.exp(k), "k": k,
          "w": ssvi(t, k, rho, eta, gamma)}  # fmt: skip
    rn = _surface_rn(_density(sl, lambda kk: ssvi(t, kk, rho, eta, gamma)), fit)
    rn["extrapolated"] = {"from_days": round(float(Ts[-1]) * 365), "beyond": bool(T > Ts[-1])}
    return rn


def quality(rn: dict) -> tuple[bool, str]:
    """The gate: enough quotes, a tight smile fit, and a density whose mean is
    the forward. A failure is shown as "Thin options data", never drawn."""
    if not all(math.isfinite(rn[k]) for k in ("F", "sigma_q", "svi_rmse_volpts", "martingale_err")):
        return False, "fit failed"  # a NaN passes every comparison below
    if rn.get("smile") == "ssvi":
        if rn["surface_slices"] < SURFACE_MIN_SLICES or rn["surface_quotes"] < SURFACE_MIN_QUOTES:
            return False, f"{rn['surface_quotes']} quotes over {rn['surface_slices']} expiries"
        if rn["quotes"] < SLICE_MIN_QUOTES:
            return False, f"{rn['quotes']} quotes"
        if not rn["surface_rmse_volpts"] < MAX_SURFACE_RMSE:
            return False, f"surface fit {rn['surface_rmse_volpts']:.1f} vol pts"
        t = rn.get("tails")
        if not t or t["left"]["cdf"] > COVERAGE or t["right"]["cdf"] < 1 - COVERAGE:
            span = f" {t['left']['cdf']:.0%}–{t['right']['cdf']:.0%}" if t else ""
            return False, f"quotes cover only{span or ' the middle'}"
    elif rn["quotes"] < MIN_QUOTES:
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
    # The residuals are the test, not sol.success: hybr reports "not making
    # good progress" when it starts on the answer (kappa 1, no premium).
    if np.all(np.isfinite(sol.x)) and abs(sol.x[1]) < 50 and max(abs(v) for v in eqs(sol.x)) < 1e-4:
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


def affine_mix(xs: np.ndarray, ps: np.ndarray, x: np.ndarray, scale: float, var: float):
    """Density on grid ``x`` of scale * X + Z, X ~ ps on grid xs, Z ~ N(0, var),
    shifted so that E[e^.] stays E_ps[e^X] (no drift added).

    Two uses: FX (a US line held in euros: ln S_EUR = (1 + b) ln S_USD + e,
    b and e from the joint FX / stock moves) and the history estimate (a
    stock as beta x the market's options-implied curve plus its own
    residual)."""
    dx = x[1] - x[0]
    y = np.interp(x / scale, xs, ps, left=0.0, right=0.0) / scale
    if var > 0:
        sd = math.sqrt(var)
        half = int(min(8 * sd / dx, len(x) - 1))
        ker = np.exp(-0.5 * (np.arange(-half, half + 1) * dx / sd) ** 2)
        y = fftconvolve(y, ker / ker.sum(), mode="same")
    y = np.clip(y, 0, None)
    y /= np.trapezoid(y, x)
    shift = math.log(np.trapezoid(ps * np.exp(xs), xs) / np.trapezoid(y * np.exp(x), x))
    y = np.interp(x - shift, x, y, left=0.0, right=0.0)
    return y / np.trapezoid(y, x)


# ------------------------------ Network layer -------------------------------


_RATE: dict[str, tuple] = {}  # "irx" -> (t, r); a currency -> (t, r, source)
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
    """(chain ticker, its yfinance Ticker, expiries).

    A listing with its own chain is used as is. Otherwise (Yahoo has no
    Eurex or Euronext chains) every US line of the company stands in: the
    pack's, plus Yahoo's Lookup by name. When more than one lists options
    (an ADR and a pink-sheet line), the one with the most open interest
    wins, judged on the expiry nearest 3 months: the deepest chain prices
    the tails best. The choice is kept for a day."""
    import yfinance as yf

    from convexity import symbol_db

    key = f"density-line|{symbol}"
    hit = cache._cache_get(key)
    if hit:
        tk = yf.Ticker(hit)
        with contextlib.suppress(Exception):
            exps = _expiries(tk)
            if exps:
                return hit, tk, exps

    def probe(cand):
        tk = yf.Ticker(cand)
        try:
            return cand, tk, _expiries(tk)
        except Exception:
            return cand, tk, []

    own = probe(symbol)
    if own[2]:
        return own
    cands = []
    with contextlib.suppress(Exception):
        cands += symbol_db.us_lines(symbol)
    with contextlib.suppress(Exception):
        found = symbol_db.discover_us_line(symbol)
        if found:
            cands.append(found)
    cands = [c for i, c in enumerate(cands) if c and c != symbol and c not in cands[:i]]
    if not cands:
        raise DensityError("no_options")
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(4) as ex:
        live = [p for p in ex.map(probe, cands) if p[2]]
    if not live:
        raise DensityError("no_options")
    if len(live) > 1:
        with ThreadPoolExecutor(4) as ex:
            oi = list(ex.map(lambda p: _open_interest(p[1], p[2]), live))
        live = [p for _, p in sorted(zip(oi, live, strict=True), key=lambda z: -z[0])]
        print(
            f"[density] {symbol}: chains on {', '.join(p[0] for p in live)}; open interest {sorted(oi, reverse=True)}"
        )
    cache._cache_put(key, live[0][0], LINE_TTL)
    return live[0]


def _open_interest(tk, exps) -> float:
    """Calls + puts open interest at the expiry nearest 3 months."""
    e = min(exps, key=lambda e: abs(e[1] - 91))
    try:
        ch = tk.option_chain(e[0])
    except Exception:
        return 0.0
    return float(ch.calls["openInterest"].fillna(0).sum() + ch.puts["openInterest"].fillna(0).sum())


def _hist_vol(sym: str) -> float | None:
    """Annualised volatility of the last year's daily log returns
    (sample sd x sqrt(252)): per year, like an implied vol, so the two
    compare directly. Measured on the chain's own listing, in its currency,
    the same instrument the implied vol is quoted on."""
    try:
        h = _close(sym, "1y")
    except Exception:
        return None
    if h is None or len(h) < 120:
        return None
    r = np.diff(np.log(h.to_numpy(dtype=float)))
    r = r[np.isfinite(r)]
    return round(float(np.std(r, ddof=1) * math.sqrt(252)), 6) if len(r) >= 100 else None


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


# ------------------------------ Chains and rates ------------------------------

# Currency -> CME futures root (quoted in USD per unit): two quarterly
# contracts settle together, so ln(F_far / F_near) / dT is r_USD - r_local
# without the spot-vs-futures timing gap that made Yahoo's spot unusable.
_FX_FUT = {"EUR": "6E", "GBP": "6B", "CHF": "6S", "JPY": "6J", "CAD": "6C", "AUD": "6A"}
# Currency -> US-listed currency ETF with options (its implied vol is the pair's).
_FX_ETF = {"EUR": "FXE", "GBP": "FXB", "CHF": "FXF", "JPY": "FXY", "CAD": "FXC", "AUD": "FXA"}
# European currencies without a futures quote: the euro's rate stands in.
_EUROPE = {"DKK", "SEK", "NOK", "PLN", "CZK", "HUF", "ISK", "RON"}
_MINOR = {"GBP": "GBP", "GBX": "GBP", "GBp": "GBP", "ILA": "ILS", "ZAc": "ZAR", "ZAC": "ZAR"}
# The market proxy for the history estimate: an index ETF with long options,
# by listing suffix (FEZ: Euro Stoxx 50; VGK lists nothing past 5 months).
_PROXY = {
    **dict.fromkeys(
        (
            "PA",
            "DE",
            "F",
            "MC",
            "MI",
            "AS",
            "BR",
            "LS",
            "VI",
            "SW",
            "L",
            "ST",
            "CO",
            "HE",
            "OL",
            "IR",
            "WA",
            "PR",
            "BD",
            "AT",
        ),
        "FEZ",
    ),  # fmt: skip
    **dict.fromkeys(("T",), "EWJ"),
    **dict.fromkeys(("HK", "SS", "SZ"), "FXI"),
    **dict.fromkeys(("AX",), "EWA"),
    **dict.fromkeys(("TO", "V"), "EWC"),
    **dict.fromkeys(("KS", "KQ"), "EWY"),
    **dict.fromkeys(("NS", "BO"), "INDA"),
    **dict.fromkeys(("SA",), "EWZ"),
}
HISTORY_YEARS = "3y"
MIN_WEEKS = 130  # 2.5 years of weekly returns before a listing's own record is enough
VAR_HALF_LIFE = 0.25  # years: how fast today's variance reverts to the 3-year level


def _norm_ccy(c: str | None) -> str:
    return _MINOR.get(c or "", (c or "").upper())


def _next_quarters(n: int = 2) -> list[tuple[str, dt.date]]:
    """The next ``n`` CME quarterly codes (e.g. "Z26") and their third Wednesdays,
    skipping one that expires within a week."""
    out, today = [], dt.date.today()
    y, m = today.year, today.month
    while len(out) < n:
        if m in (3, 6, 9, 12):
            first = dt.date(y, m, 1)
            wed = first + dt.timedelta(days=(2 - first.weekday()) % 7 + 14)
            if (wed - today).days > 7:
                out.append(("HMUZ"[(m // 3) - 1] + f"{y % 100:02d}", wed))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _close(sym: str, period: str = "5d"):
    import yfinance as yf

    h = yf.Ticker(sym).history(period=period, auto_adjust=True)
    return h["Close"].dropna() if h is not None and len(h) else None


def _local_rate(ccy: str) -> tuple[float, str]:
    """(rate, source) for a currency: ^IRX for USD, the CME futures spread
    for majors, the euro's for other European currencies, else the US bill
    (said so). Cached an hour."""
    ccy = _norm_ccy(ccy)
    if ccy in ("", "USD"):
        return _rate(), "US 3-month bill"
    if ccy in _EUROPE:
        r, _ = _local_rate("EUR")
        return r, "euro rate (no local quote)"
    hit = _RATE.get(ccy)
    if hit and time.time() - hit[0] < RATE_TTL:
        return hit[1], hit[2]
    r_usd = _rate()
    root = _FX_FUT.get(ccy)
    if root:
        try:
            (c1, d1), (c2, d2) = _next_quarters()
            f1, f2 = _close(f"{root}{c1}.CME"), _close(f"{root}{c2}.CME")
            r = r_usd - math.log(float(f2.iloc[-1]) / float(f1.iloc[-1])) / ((d2 - d1).days / 365)
            if -0.02 < r < 0.2:
                _RATE[ccy] = (time.time(), r, "CME futures")
                return r, "CME futures"
        except Exception as e:
            print(f"[density] {ccy} rate: {type(e).__name__}")
    return r_usd, "US 3-month bill (no local rate)"


def _chains(tk, sym: str) -> list[tuple[str, int, list, list]]:
    """Every expiry's quotes within SURFACE_DAYS, fetched in parallel and
    cached for TTL: both horizons and the surface share them."""
    key = f"density-chains|{sym}"
    hit = cache._cache_get(key)
    if hit is not None:
        return hit
    from concurrent.futures import ThreadPoolExecutor

    exps = [e for e in _expiries(tk) if SURFACE_DAYS[0] <= e[1] <= SURFACE_DAYS[1]]

    def get(e):
        try:
            ch = tk.option_chain(e[0])
            return (e[0], e[1], _quotes(ch.calls), _quotes(ch.puts))
        except Exception:
            return None

    with ThreadPoolExecutor(6) as ex:
        out = [c for c in ex.map(get, exps) if c]
    if out:
        cache._cache_put(key, out, TTL)
    return out


def _options_rn(tk, sym: str, exps, horizon: int, S0: float, rf: float):
    """The risk-neutral fit for a horizon: (rn, expiry, days, ok, why).

    Tries the EXPIRY_TRIES expiries in the window nearest the horizon. On
    each, SVI when the slice has MIN_QUOTES, then the SSVI surface over all
    expiries; the first fit that passes the gate wins. Otherwise the nearest
    expiry's first fit comes back with why it failed."""
    lo, hi = EXPIRY_WINDOW[horizon]
    usable = sorted((e for e in exps if lo <= e[1] <= hi), key=lambda e: abs(e[1] - horizon))
    if not usable and not _can_extrapolate(exps, horizon):
        raise DensityError("no_expiry", f"longest expiry {max(e[1] for e in exps)} days")
    slices = {}
    for e, d, calls, puts in _chains(tk, sym):
        with contextlib.suppress(DensityError):
            slices[e] = _slice(S0, d / 365, rf, calls, puts)
    surface = list(slices.values())
    first = None
    for e, d in usable[:EXPIRY_TRIES]:
        sl = slices.get(e)
        if sl is None:
            continue
        fits = []
        if len(sl["K"]) >= MIN_QUOTES or len(surface) < SURFACE_MIN_SLICES:
            fits.append(lambda sl=sl: _svi_density(sl))
        if len(surface) >= SURFACE_MIN_SLICES:
            fits.append(lambda sl=sl: fit_surface(surface, sl))
        for fit in fits:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                rn = fit()
            ok, why = quality(rn)
            if ok:
                return rn, e, d, True, ""
            if first is None:
                first = (rn, e, d, False, why)
    if first:
        return first
    if not usable:
        # No listed expiry near the horizon: read it off the surface.
        if not surface:
            raise DensityError("thin", "every expiry's quotes too wide or one-sided")
        if len(surface) < SURFACE_MIN_SLICES:
            raise DensityError("thin", f"{len(surface)} usable expiries for a surface")
        rn = surface_at(surface, horizon / 365)
        ok, why = quality(rn)
        return rn, (dt.date.today() + dt.timedelta(days=horizon)).isoformat(), horizon, ok, why
    raise DensityError("thin", f"no usable quotes in the {lo}–{hi} day window")


def _can_extrapolate(exps, horizon: int) -> bool:
    return bool(exps) and max(e[1] for e in exps) >= EXTRAP_MIN * horizon


def _weekly(series):
    """Weekly log returns (Friday closes): European and US closes are hours
    apart, and daily returns across them understate beta."""
    if getattr(series.index, "tz", None) is not None:
        series = series.tz_localize(None)  # Paris, London and New York closes share one calendar
    w = series.resample("W-FRI").last().dropna()
    return np.log(w).diff().dropna()


def _winsor(r, k: float = 8.0):
    """Clip weekly returns to the median +- k robust sd: Yahoo's secondary
    listings carry glitch weeks (one Xetra line showed +19% in a week its
    company never
    had), and one of them is enough to swamp a 3-year beta."""
    med = float(np.median(r))
    mad = float(np.median(np.abs(r - med))) * 1.4826
    return r.clip(med - k * mad, med + k * mad) if mad > 0 else r


def _patchy(r) -> bool:
    """Fewer than 85% of the weeks between the series' first and last week."""
    if len(r) < 2:
        return True
    return len(r) < 0.85 * ((r.index[-1] - r.index[0]).days / 7 + 1)


def _forecast_var(r, T: float) -> float:
    """Annual variance over the next T years from weekly returns: the last
    13 weeks' level reverting to the full sample's with a VAR_HALF_LIFE
    half-life, averaged over the horizon."""
    lr = float(np.mean(r**2)) * 52
    st = float(np.mean(r[-13:] ** 2)) * 52 if len(r) >= 13 else lr
    a = math.log(2) / VAR_HALF_LIFE
    return lr + (st - lr) * (1 - math.exp(-a * T)) / (a * T)


def _fx(chain_ccy: str, ccy: str, chain_sym: str, T: float) -> dict | None:
    """The FX layer for a chain priced in one currency and shown in another.

    ln S_shown = ln S_chain + ln X, X the chain->shown rate. With b the
    regression of ln X on the stock's return and e the rest, S_shown's log
    return is (1 + b) x + e. b and corr come from 2 years of weekly returns;
    the FX vol from the currency ETF's options (FXE for EUR) when it has any,
    else from history. No expected FX move is assumed."""
    a, z = _norm_ccy(chain_ccy), _norm_ccy(ccy)
    if not a or not z or a == z:
        return None
    key = f"density-fx|{a}|{z}|{chain_sym}|{round(T, 2)}"
    hit = cache._cache_get(key)
    if hit is not None:
        return hit
    import yfinance as yf

    fx = _close(f"{a}{z}=X", "2y")
    st = _close(chain_sym, "2y")
    if fx is None or st is None or len(fx) < 200:
        return None
    rx, rs = _weekly(fx), _weekly(st)
    j = rx.index.intersection(rs.index)
    rx, rs = rx.loc[j].to_numpy(), rs.loc[j].to_numpy()
    if len(j) < 40:
        return None
    sx, ss = float(np.std(rx) * math.sqrt(52)), float(np.std(rs) * math.sqrt(52))
    rho = float(np.corrcoef(rx, rs)[0, 1])
    sigma, source = sx, "history"
    other = z if a == "USD" else a if z == "USD" else None
    etf = _FX_ETF.get(other or "")
    if etf:
        try:
            tk = yf.Ticker(etf)
            exps = _expiries(tk)
            e = min(exps, key=lambda e: abs(e[1] / 365 - T))
            px, _ = _last(tk)
            w = _atm_w(tk, e[0], e[1], px, _rate())
            if w:
                sigma, source = math.sqrt(w / (e[1] / 365)), f"{etf} options"
        except Exception as e:
            print(f"[density] fx {etf}: {type(e).__name__}")
    out = {
        "pair": f"{a}/{z}",
        "sigma": round(sigma, 4),
        "rho": round(rho, 3),
        "b": round(rho * sigma / ss, 4) if ss > 0 else 0.0,
        "var": round(sigma**2 * (1 - rho**2) * T, 6),
        "source": source,
    }
    cache._cache_put(key, out, RATE_TTL)
    return out


# ------------------------------------ API ------------------------------------


def density(
    symbol: str, horizon: int = 365, beta: float | None = None, prefs: dict | None = None
) -> dict:
    """The `/api/density` payload. Never raises.

    Options first (``source: "options"``). When they can't be used (no chain,
    no expiry near the horizon, or a chain that fails the gate) the history
    estimate stands in (``source: "history"``, the options' reason kept in
    ``fallback``); when that fails too it is
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
        fail = {"ok": False, "reason": "error", "detail": "", "symbol": symbol, "horizon": horizon}
        out = None
        try:
            out = _compute(symbol, horizon, beta, st)
        except DensityError as e:
            fail.update(reason=e.reason, detail=e.detail)
            if getattr(e, "chain", None):
                fail["chain_symbol"] = e.chain
        except Exception as e:  # a Yahoo hiccup: say so, try again next open
            print(f"[density] {symbol} {horizon}d failed: {type(e).__name__}: {e}")
            fail["detail"] = type(e).__name__
        if out is not None and not out["ok"]:
            fail = out
        if out is None or not out["ok"]:
            if fail["reason"] != "error":
                try:
                    out = _history(symbol, horizon, beta, st)
                    out["fallback"] = {
                        "reason": fail["reason"],
                        "detail": fail.get("detail", ""),
                        "chain_symbol": fail.get("chain_symbol"),
                        "why": (fail.get("quality") or {}).get("why", "")
                        or (fail.get("detail", "") if fail["reason"] == "thin" else ""),
                    }
                    # A chain too thin for tails still quotes a fair ATM vol.
                    fv = fail.get("vol") or {}
                    if fv.get("implied"):
                        out["vol"].update(
                            implied=fv["implied"], implied_days=fv.get("implied_days")
                        )
                        out["vol"]["implied_symbol"] = fv.get("symbol")
                except DensityError as e:
                    print(f"[density] {symbol} {horizon}d history: {e.reason} {e.detail}")
                    out = None
                except Exception as e:
                    print(f"[density] {symbol} {horizon}d history failed: {type(e).__name__}: {e}")
                    out = None
            if out is None:
                print(f"[density] {symbol} {horizon}d: {fail['reason']} {fail.get('detail', '')}")
                return fail
        print(
            f"[density] {symbol} {horizon}d {out.get('source', 'options')} via {out.get('chain_symbol')}"
            f" in {time.time() - t0:.1f}s"
        )
        cache._cache_put(key, out, TTL)
        return out


def _payload_curves(rn: dict, curves: dict, fx: dict | None) -> dict:
    """{name: downsampled curve + stats}, each pushed through the FX layer."""
    out = {}
    for name, (p, method) in curves.items():
        if p is None:
            out[name] = None
            continue
        if fx:
            p = affine_mix(rn["x"], p, rn["x"], 1 + fx["b"], fx["var"])
        out[name] = {**downsample(rn["x"], p), **curve_stats(rn, p), "method": method}
    return out


def _analysts_curve(rn, an, horizon, beta, st, kappa):
    if not an or horizon != 365:
        return None
    targets = [f["target"] for f in an["firms"]] or [an["consensus"]]
    try:
        mx = analysts_mixture(rn, targets, beta, st["erp"], kappa, st["haircut"])
    except DensityError:
        return None
    return mx["p"], mx["method"]


def _compute(symbol: str, horizon: int, beta: float, st: dict) -> dict:
    import yfinance as yf

    chain_sym, tk, exps = _resolve_chain(symbol)
    lo, hi = EXPIRY_WINDOW[horizon]
    if not any(lo <= e[1] <= hi for e in exps) and not _can_extrapolate(exps, horizon):
        raise DensityError("no_expiry", f"longest expiry {max(e[1] for e in exps)} days")
    S0, chain_ccy = _last(tk)
    if chain_sym == symbol:
        ratio, ccy = 1.0, chain_ccy
    else:
        local, ccy = _last(yf.Ticker(symbol))
        ratio = local / S0
    if not (math.isfinite(S0) and S0 > 0 and math.isfinite(ratio) and ratio > 0):
        raise DensityError("error", "no last price")
    rf = _rate()
    try:
        rn, expiry, days, ok, why = _options_rn(tk, chain_sym, exps, horizon, S0, rf)
    except DensityError as e:
        e.chain = chain_sym  # the fallback names the listing that was too thin
        raise

    try:
        qtype = (tk.info or {}).get("quoteType") or ""
    except Exception:
        qtype = ""
    is_index = qtype.upper() in _INDEX_TYPES
    kappa = st["kappa_index"] if is_index else st["kappa_stock"]
    tails = rn.get("tails")
    base = {
        "ok": ok,
        "reason": "" if ok else "thin",
        "source": "options",
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
        "rate_source": "US 3-month bill",
        "sigma_q": round(rn["sigma_q"], 6),
        "kappa": kappa,
        "is_index": is_index,
        "beta": beta,
        "erp": st["erp"],
        "measure": st["measure"],
        "haircut": st["haircut"],
        "horizon": horizon,
        "fx": None,
        "quality": {
            "smile": rn.get("smile", "svi"),
            "quotes": rn["quotes"],
            "svi_rmse_volpts": round(rn["svi_rmse_volpts"], 3),
            "martingale_err": round(rn["martingale_err"], 5),
            "surface_quotes": rn.get("surface_quotes"),
            "surface_slices": rn.get("surface_slices"),
            "surface_rmse_volpts": round(rn["surface_rmse_volpts"], 3)
            if rn.get("surface_rmse_volpts") is not None
            else None,
            "tails": {side: {k: round(v, 4) for k, v in t.items()} for side, t in tails.items()}
            if tails
            else None,
            "extrapolated": rn.get("extrapolated"),
            "ok": ok,
            "why": why,
        },
        "vol": {
            "implied": round(rn["atm_iv"], 6)
            if math.isfinite(rn.get("atm_iv", math.nan))
            else None,
            "implied_days": days,
            "hist": _hist_vol(chain_sym),
            "symbol": chain_sym,
        },
    }
    if not ok:
        return base

    fx = None
    try:
        fx = _fx(chain_ccy, ccy, chain_sym, rn["T"])
    except Exception as e:
        print(f"[density] fx {chain_sym}: {type(e).__name__}")
    mk = market_curve(rn, beta, st["erp"], kappa)
    an = None
    try:
        an = _analysts(tk)
    except Exception as e:
        print(f"[density] analysts {chain_sym}: {type(e).__name__}")
    curves = _payload_curves(
        rn,
        {
            "rn": (rn["q"], "Q"),
            "market": (mk["p"], mk["method"]),
            "analysts": _analysts_curve(rn, an, horizon, beta, st, kappa) or (None, None),
        },
        fx,
    )
    try:
        earnings = _earnings(tk, chain_sym, exps, S0, rf, days)
    except Exception as e:
        print(f"[density] earnings {chain_sym}: {type(e).__name__}")
        earnings = []
    return {**base, "fx": fx, "curves": curves, "analysts": an, "earnings": earnings}


def _history(symbol: str, horizon: int, beta: float, st: dict) -> dict:
    """The estimate for a stock with no usable options, from its own history
    and its market's options.

        x = beta_m * y + e

    y is the market proxy's options-implied curve (an index ETF by listing:
    FEZ for Europe), re-centred and scaled to the market's real-world vol in
    the stock's currency (kappa_index x implied, the FX share taken out);
    beta_m and the residual e come from 3 years of weekly returns against
    that proxy, e's variance reverting from its last quarter to its 3-year
    level. Without the proxy's options y is a normal with the proxy's own
    forecast vol. The mean is CAPM in the stock's currency, with that
    currency's rate. It is a real-world curve only: there is no
    risk-neutral one without options."""
    import yfinance as yf

    tk = yf.Ticker(symbol)
    S0, ccy = _last(tk)
    if not (math.isfinite(S0) and S0 > 0):
        raise DensityError("error", "no last price")
    hist = _close(symbol, HISTORY_YEARS)
    if hist is None or len(hist) < 250:
        raise DensityError("no_history", f"{0 if hist is None else len(hist)} days of prices")
    T = horizon / 365
    lc = _norm_ccy(ccy)
    try:
        info = tk.info or {}
    except Exception:
        info = {}
    name = info.get("longName") or info.get("shortName") or ""
    suffix = symbol.rsplit(".", 1)[1] if "." in symbol else ""
    proxy = (
        "EEM" if "emerging" in name.lower() or " EM " in f" {name} " else _PROXY.get(suffix, "SPY")
    )

    ph = _close(proxy, HISTORY_YEARS)
    if ph is None:
        raise DensityError("no_history", f"no {proxy} history")
    rpx = _weekly(ph)
    fxw = None
    if lc not in ("", "USD"):
        fxh = _close(f"USD{lc}=X", HISTORY_YEARS)
        if fxh is None:
            raise DensityError("no_history", f"no USD/{lc} history")
        fxw = _weekly(fxh)
    rs, series = _weekly(hist), symbol
    if _patchy(rs) or len(rs) < MIN_WEEKS:
        # A thin or young secondary listing (76 weeks, with a +19% week the
        # company never had): the company's US line in this currency
        # is the same exposure with a longer, cleaner record.
        from convexity import symbol_db

        us = None
        with contextlib.suppress(Exception):
            us = symbol_db.us_line(symbol)
        uh = _close(us, HISTORY_YEARS) if us and us != symbol else None
        ru = _weekly(uh) if uh is not None else None
        if ru is not None and len(ru) > len(rs) and not _patchy(ru):
            rs = (ru + fxw).dropna() if fxw is not None else ru
            series = f"{us} in {lc or 'USD'}"
    rs, rpx = _winsor(rs), _winsor(rpx)
    j = rs.index.intersection(rpx.index)
    if fxw is not None:
        j = j.intersection(fxw.index)
    if len(j) < 52:
        raise DensityError("no_history", f"{len(j)} weeks")
    s = rs.loc[j].to_numpy()
    m = rpx.loc[j].to_numpy() + (fxw.loc[j].to_numpy() if fxw is not None else 0.0)
    beta_m = float(np.cov(s, m)[0, 1] / np.var(m, ddof=1))
    resid = s - beta_m * m
    var_e = _forecast_var(resid, T)
    var_m_hist = _forecast_var(m, T)

    # The market factor in the stock's currency, real-world.
    mkt_src, xs, ps, sd_m = "history", None, None, math.sqrt(var_m_hist)
    try:
        ptk = yf.Ticker(proxy)
        pS0, _ = _last(ptk)
        prn, pexp, _, pok, _ = _options_rn(ptk, proxy, _expiries(ptk), horizon, pS0, _rate())
        if pok:
            sd_usd = prn["sigma_q"]
            if fxw is not None:
                rx = fxw.loc[j].to_numpy()
                sx = float(np.std(rx) * math.sqrt(52))
                rho = float(np.corrcoef(m, rx)[0, 1])
                disc = sd_usd**2 - sx**2 * (1 - rho**2)
                sd_loc = rho * sx + math.sqrt(disc) if disc > 0 else sd_usd
            else:
                sd_loc = sd_usd
            sd_m = st["kappa_index"] * sd_loc
            xs, ps = prn["x"], prn["q"]
            xs = xs - float(np.trapezoid(ps * xs, xs))  # centred: CAPM sets the mean below
            ps_scale = sd_m / sd_usd
            mkt_src = f"{proxy} options to {pexp}"
    except DensityError:
        pass
    except Exception as e:
        print(f"[density] proxy {proxy}: {type(e).__name__}")

    sd_tot = math.sqrt(beta_m**2 * sd_m**2 + var_e) * math.sqrt(T)
    span = GRID_SD * sd_tot
    x = np.linspace(max(-span, math.log(0.01)), min(span, math.log(20.0)), GRID)
    if xs is None:
        xs = x
        ps = np.exp(-0.5 * (x / (sd_m * math.sqrt(T))) ** 2)
        ps /= np.trapezoid(ps, x)
        ps_scale = 1.0
    q = affine_mix(xs, ps, x, max(beta_m * ps_scale, 1e-3), var_e * T)
    r_loc, r_src = _local_rate(lc)
    F = S0 * math.exp(r_loc * T)
    m1 = float(np.trapezoid(q * x, x))
    sig = float(math.sqrt(max(np.trapezoid(q * (x - m1) ** 2, x), 1e-12)) / math.sqrt(T))
    rn = {"T": T, "S0": S0, "F": F, "rf": r_loc, "x": x, "q": q, "sigma_q": sig}

    mk = market_curve(rn, beta, st["erp"], 1.0)
    an = None
    try:
        an = _analysts(tk)
    except Exception as e:
        print(f"[density] analysts {symbol}: {type(e).__name__}")
    curves = _payload_curves(
        rn,
        {
            "rn": (None, None),
            "market": (mk["p"], mk["method"]),
            "analysts": _analysts_curve(rn, an, horizon, beta, st, 1.0) or (None, None),
        },
        None,
    )
    earnings = []
    try:
        from convexity import attention

        e = (attention.fetch_events(symbol) or {}).get("earnings")
        if e:
            d = (dt.date.fromisoformat(e) - dt.date.today()).days
            first = True
            while 0 <= d <= horizon:
                day = (dt.date.today() + dt.timedelta(days=d)).isoformat()
                earnings.append({"date": day, "days": d, "var": 0.0, "estimated": not first})
                first, d = False, d + 91
    except Exception as e:
        print(f"[density] earnings {symbol}: {type(e).__name__}")
    return {
        "ok": True,
        "reason": "",
        "source": "history",
        "symbol": symbol,
        "chain_symbol": symbol,
        "ratio": 1.0,
        "currency": ccy,
        "chain_currency": ccy,
        "s0": round(S0, 6),
        "chain_s0": round(S0, 6),
        "forward": round(F, 6),
        "expiry": (dt.date.today() + dt.timedelta(days=horizon)).isoformat(),
        "days": horizon,
        "T": round(T, 6),
        "rf": round(r_loc, 6),
        "rate_source": r_src,
        "sigma_q": round(sig, 6),
        "kappa": 1.0,
        "is_index": False,
        "beta": beta,
        "erp": st["erp"],
        "measure": "P",
        "haircut": st["haircut"],
        "horizon": horizon,
        "fx": None,
        "quality": {
            "smile": "history",
            "proxy": proxy,
            "market_source": mkt_src,
            "beta_market": round(beta_m, 3),
            "market_vol": round(sd_m, 4),
            "own_vol": round(math.sqrt(var_e), 4),
            "weeks": len(j),
            "series": series,
            "ok": True,
            "why": "",
        },
        "vol": {"implied": None, "implied_days": None, "hist": _hist_vol(symbol), "symbol": symbol},
        "curves": curves,
        "analysts": an,
        "earnings": earnings,
    }
