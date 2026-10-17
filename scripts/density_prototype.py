"""Reference prototype for the real-world price distribution feature.

docs/plans/real-world-density.md is the spec; this script is the numerical
recipe it describes, kept small enough to read in one sitting. It is not
imported by the app. Run it to see the numbers for a few US tickers:

    uv run python scripts/density_prototype.py AAPL 1.2
    uv run python scripts/density_prototype.py SPY 1.0 --index

Pipeline per horizon (3M, 12M): Yahoo chain -> forward from put-call parity
(rate fixed from ^IRX, because Yahoo's US chains are American-style and parity
backs out nonsense rates) -> OTM implied total variance -> SVI fit with Roger
Lee's wing bound -> Breeden-Litzenberger risk-neutral density q -> real-world
density p by the minimum-entropy two-moment tilt (Approach B):

    p(x) ∝ q(x) exp(t1 x + t2 x^2),  x = ln(S_T / F)
    E_P[S_T] = S0 exp((r + beta * ERP) T),   Var_P[x] = (kappa * sigma_Q)^2 T

t2 = 0 is the one-parameter power tilt (Approach A), the fallback whenever the
two-moment solve fails or leaves its bounds.
"""

import argparse
import datetime as dt
import warnings

import numpy as np
import yfinance as yf
from scipy.optimize import brentq, least_squares, root
from scipy.stats import norm

warnings.filterwarnings("ignore")

ERP = 0.05  # equity premium default; the app should make this the owner's setting
KAPPA = {"index": 0.80, "stock": 0.92}  # physical / implied vol: placeholders, to be estimated
MAX_SPREAD = 0.6  # drop quotes whose bid-ask exceeds 60% of mid
MARTINGALE_GATE = 0.01  # |E_Q[S_T]/F - 1| above this = tails not trustworthy


def black_call(F, K, w, DF):
    sw = np.sqrt(np.maximum(w, 1e-12))
    d1 = (np.log(F / K) + 0.5 * w) / sw
    return DF * (F * norm.cdf(d1) - K * norm.cdf(d1 - sw))


def implied_w(price, F, K, DF, call):
    """Implied total variance from a Black-76 price; nan when below intrinsic."""
    intrinsic = DF * max(F - K, 0) if call else DF * max(K - F, 0)
    if price <= intrinsic + 1e-6:
        return np.nan

    def err(w):
        c = black_call(F, K, w, DF)
        return (c if call else c - DF * (F - K)) - price

    try:
        return brentq(err, 1e-8, 25.0)
    except ValueError:
        return np.nan


def svi(p, k):
    """Raw SVI with the wing slope b(1+|rho|) capped at 2 (Lee's moment bound).

    Without the cap the 12M SPY wing extrapolated beyond the last quoted strike
    and pushed the density's mean 11% above the forward.
    """
    a, u, rho, m, s = p
    b = u * 2.0 / (1 + abs(rho))
    return a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + s**2))


def risk_neutral(sym, target_days):
    t = yf.Ticker(sym)
    today = dt.date.today()
    exps = [(e, (dt.date.fromisoformat(e) - today).days) for e in t.options]
    if not exps:
        raise SystemExit(f"{sym}: no listed options on Yahoo (non-US line? use its US line)")
    expiry, days = min(exps, key=lambda x: abs(x[1] - target_days))
    T = days / 365
    chain = t.option_chain(expiry)
    S0 = float(t.fast_info.last_price)
    calls = chain.calls[(chain.calls.bid > 0) & (chain.calls.ask > 0)].set_index("strike")
    puts = chain.puts[(chain.puts.bid > 0) & (chain.puts.ask > 0)].set_index("strike")
    cm, pm = (calls.bid + calls.ask) / 2, (puts.bid + puts.ask) / 2

    rf = float(yf.Ticker("^IRX").history(period="5d")["Close"].iloc[-1]) / 100
    DF = np.exp(-rf * T)
    near = [k for k in cm.index.intersection(pm.index) if abs(k / S0 - 1) < 0.12]
    F = float(np.median([k + (cm[k] - pm[k]) / DF for k in near]))

    rows = []
    for K, px in pm.items():
        if K < F:
            rows.append(
                (K, implied_w(px, F, K, DF, False), puts.loc[K, "ask"] - puts.loc[K, "bid"], px)
            )
    for K, px in cm.items():
        if K >= F:
            rows.append(
                (K, implied_w(px, F, K, DF, True), calls.loc[K, "ask"] - calls.loc[K, "bid"], px)
            )
    rows = [
        r for r in rows if np.isfinite(r[1]) and r[2] / r[3] < MAX_SPREAD and 0.3 < r[0] / F < 2.5
    ]
    K = np.array([r[0] for r in rows])
    w = np.array([r[1] for r in rows])
    k = np.log(K / F)

    w_atm = float(np.median(w))
    weight = np.exp(-0.5 * (k / (2 * np.sqrt(w_atm))) ** 2)  # the body matters more than far wings
    fit = least_squares(
        lambda p: (svi(p, k) - w) * weight,
        [np.min(w), 0.1, -0.3, 0.0, 0.1],
        bounds=([-1, 1e-4, -0.999, -2, 1e-3], [5, 1.0, 0.999, 2, 5]),
    )

    x = np.linspace(-5 * np.sqrt(w_atm), 5 * np.sqrt(w_atm), 4001)
    Kg = F * np.exp(x)
    C = black_call(F, Kg, np.maximum(svi(fit.x, x), 1e-8), DF)
    qK = np.clip(np.gradient(np.gradient(C, Kg), Kg) / DF, 0, None)
    qx = qK * Kg
    qx /= np.trapezoid(qx, x)
    sigma_q = float(np.sqrt(np.trapezoid(qx * x**2, x) - np.trapezoid(qx * x, x) ** 2) / np.sqrt(T))
    return {
        "expiry": expiry,
        "days": days,
        "T": T,
        "S0": S0,
        "F": F,
        "rf": rf,
        "x": x,
        "q": qx,
        "sigma_q": sigma_q,
        "quotes": len(K),
        "svi_rmse_volpts": float(
            np.sqrt(np.mean((np.sqrt(svi(fit.x, k) / T) - np.sqrt(w / T)) ** 2)) * 100
        ),
        "martingale_err": float(np.trapezoid(qx * np.exp(x), x) - 1),
    }


def tilt(rn, premium, kappa):
    """Approach B (two moments); falls back to Approach A (t2 = 0) when it fails."""
    x, q, T = rn["x"], rn["q"], rn["T"]
    R = rn["F"] * np.exp(x) / rn["S0"]
    mean_target = np.exp((rn["rf"] + premium) * T)
    var_target = (kappa * rn["sigma_q"]) ** 2 * T

    def dens(t1, t2):
        z = t1 * x + t2 * x**2
        p = q * np.exp(z - z.max())
        return p / np.trapezoid(p, x)

    g = brentq(lambda t1: np.trapezoid(dens(t1, 0) * R, x) - mean_target, -20, 20)

    def eqs(th):
        p = dens(*th)
        m = np.trapezoid(p * x, x)
        return [
            np.trapezoid(p * R, x) / mean_target - 1,
            np.trapezoid(p * (x - m) ** 2, x) / var_target - 1,
        ]

    sol = root(eqs, [g, 0.0])
    if sol.success and abs(sol.x[1]) < 50:
        return dens(*sol.x), "B", [float(v) for v in sol.x]
    return dens(g, 0), "A (fallback)", [float(g), 0.0]


def summary(rn, p):
    x, S0 = rn["x"], rn["S0"]
    price = rn["F"] * np.exp(x)
    cdf = np.cumsum(p) * (x[1] - x[0])

    def at(pr):
        return float(price[min(np.searchsorted(cdf, pr), len(price) - 1)])

    def prob(mask):
        return float(np.trapezoid(p * mask, x))

    return (
        f"median {at(0.5):.0f} | 50% {at(0.25):.0f}-{at(0.75):.0f} | 90% {at(0.05):.0f}-{at(0.95):.0f}"
        f" | P(up) {prob(price > S0):.0%} | P(<=-20%) {prob(price < 0.8 * S0):.1%}"
        f" | P(>=+20%) {prob(price > 1.2 * S0):.1%}"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbol")
    ap.add_argument("beta", type=float)
    ap.add_argument("--index", action="store_true", help="use the index kappa")
    a = ap.parse_args()
    kappa = KAPPA["index" if a.index else "stock"]
    for days in (91, 365):
        rn = risk_neutral(a.symbol, days)
        p, method, theta = tilt(rn, a.beta * ERP, kappa)
        gate = (
            "ok"
            if abs(rn["martingale_err"]) < MARTINGALE_GATE and rn["quotes"] >= 30
            else "LOW QUALITY"
        )
        print(
            f"\n{a.symbol} {rn['expiry']} ({rn['days']}d) S0 {rn['S0']:.2f} F {rn['F']:.2f} r {rn['rf']:.2%}"
            f" | quotes {rn['quotes']} | SVI rmse {rn['svi_rmse_volpts']:.2f} vol pts"
            f" | martingale {rn['martingale_err']:+.2%} [{gate}]"
        )
        print(f"  options (Q)   vol {rn['sigma_q']:.1%}  {summary(rn, rn['q'])}")
        print(
            f"  real (P, {method}) vol {kappa * rn['sigma_q']:.1%} theta {np.round(theta, 2)}  {summary(rn, p)}"
        )


if __name__ == "__main__":
    main()
