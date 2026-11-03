#!/usr/bin/env python3
"""Recompute the time-to-recover constants in long_run.py and app.js's
INV_REC_DEPTH from public tickers only (docs/recovery_research.md).

    uv run python scripts/study_recovery.py [--cache closes.json]

Fetches each ticker's max daily history from Yahoo, a few at a time with a pause
(about a minute); --cache keeps it in a JSON file for repeat runs. Prints the card
curve's RECOVERY_PHI, RECOVERY_MU, RECOVERY_VOL_SLOPE and RECOVERY_N0, the slope in
separate periods and out of sample after later dates, the Kaplan-Meier table by depth,
and the running fall's FROM_HERE coefficients. After changing a constant, bump
long_run.VERSION so the kept records are rebuilt.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import optimize, special

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from convexity.long_run import (  # noqa: E402
    MIN_FALL,
    RECOVERY_VOL_REF,
    VOL_DAYS,
    VOL_MIN_DAYS,
    dip_days,
    repair_splits,
    usable_start,
)

PANEL = [
    "AAPL",
    "MSFT",
    "NVDA",
    "SPY",
    "JPM",
    "XOM",
    "KO",
    "PG",
    "JNJ",
    "WMT",
    "DIS",
    "BA",
    "GE",
    "IBM",
    "INTC",
    "CSCO",
    "PFE",
    "MRK",
    "T",
    "VZ",
    "CVX",
    "HD",
    "MCD",
    "NKE",
    "MMM",
    "CAT",
    "GS",
    "AXP",
    "C",
    "BAC",
    "WFC",
    "UNH",
    "ORCL",
    "ADBE",
    "AMGN",
    "GILD",
    "COST",
    "TGT",
    "F",
    "GM",
]
FOLDS = 5
CHECKPOINTS = (63, 126, 252, 378, 504, 605, 756, 1008, 1260, 1512, 2016, 2520)  # days into a fall


def closes(cache: str | None) -> dict[str, pd.Series]:
    if cache and Path(cache).exists():
        raw = json.loads(Path(cache).read_text())
        return {s: pd.Series(v["c"], index=pd.to_datetime(v["d"])) for s, v in raw.items()}
    import yfinance as yf

    out = {}
    for i in range(0, len(PANEL), 6):
        chunk = PANEL[i : i + 6]
        df = yf.download(
            chunk, period="max", auto_adjust=True, progress=False, threads=False, group_by="ticker"
        )
        for s in chunk:
            out[s] = df[s]["Close"].dropna()
        time.sleep(2)
    if cache:
        body = {
            s: {"d": [str(d)[:10] for d in v.index], "c": [float(x) for x in v]}
            for s, v in out.items()
        }
        Path(cache).write_text(json.dumps(body))
    return out


def gated(ser: pd.Series) -> pd.Series:
    ser = ser[ser > 0].sort_index()
    ser = pd.Series(repair_splits(ser.index, ser.to_numpy()), index=ser.index)
    bad = usable_start(ser.index, ser.to_numpy())
    return ser[ser.index > bad] if bad is not None else ser


def falls(px: np.ndarray) -> list[tuple[int, int, int]]:
    """(start, end, finished) of each fall of MIN_FALL or more, peak to next peak."""
    dd = px / np.maximum.accumulate(px) - 1
    hi = np.flatnonzero(dd == 0)
    out = [
        (int(a), int(b), 1) for a, b in zip(hi, hi[1:], strict=False) if dd[a:b].min() <= -MIN_FALL
    ]
    if dd[hi[-1] :].min() <= -MIN_FALL:
        out.append((int(hi[-1]), len(px) - 1, 0))
    return out


def ig_logpdf(x, mu, phi):
    lam = phi * mu
    return 0.5 * np.log(lam / (2 * np.pi * x**3)) - lam * (x - mu) ** 2 / (2 * mu**2 * x)


def ig_logsf(x, mu, phi):
    r = np.sqrt(phi * mu / x)
    la, lb = special.log_ndtr(-r * (x / mu - 1)), 2 * phi + special.log_ndtr(-r * (x / mu + 1))
    return la + np.log1p(-np.exp(np.clip(lb - la, -700, -1e-12)))


def own_scale(t, e, mu, phi):
    """The holding's log scale on the pooled curve, censored MLE."""

    def f(ls):
        hit = ig_logpdf(t[e == 1] / math.exp(ls), mu, phi) - ls
        return -(hit.sum() + ig_logsf(t[e == 0] / math.exp(ls), mu, phi).sum())

    return optimize.minimize_scalar(f, bounds=(-5, 5), method="bounded").x


def holding(ser: pd.Series) -> dict:
    """A line's falls: t days, e finished, lv the log of volatility / RECOVERY_VOL_REF
    over the three years before each fall's high (0 with too little to read)."""
    px = ser.to_numpy()
    r = np.diff(np.log(px))
    spans = falls(px)

    def lv(a):
        v = r[max(0, a - VOL_DAYS) : a]
        return math.log(v.std() * 252**0.5 / RECOVERY_VOL_REF) if len(v) >= VOL_MIN_DAYS else 0.0

    return {"idx": ser.index, "start": np.array([a for a, _, _ in spans], int),
            "end": np.array([b for _, b, _ in spans], int),
            "t": np.array([b - a + 0.5 for a, b, _ in spans]),
            "e": np.array([d for _, _, d in spans]), "lv": np.array([lv(a) for a, _, _ in spans])}  # fmt: skip


def before(h: dict, date: str) -> dict:
    """The line as it stood on ``date``: only the falls finished by then."""
    i = int(h["idx"].searchsorted(pd.Timestamp(date)))
    keep = h["end"] <= i
    return {**h, **{k: h[k][keep] for k in ("start", "end", "t", "e", "lv")}}


def fit_panel(H):
    """The pooled scale and shape and the slope on volatility: one censored fit of every
    fall on the volatility just before it, not a slope per stock on one number for all
    its history (which dilutes it, and loses out of sample)."""
    t = np.concatenate([h["t"] for h in H])
    e = np.concatenate([h["e"] for h in H])
    v = np.concatenate([h["lv"] for h in H])

    def nll(th):
        mu, phi = math.exp(th[0]), math.exp(th[1])
        z = t / np.exp(th[2] * v)
        hit = ig_logpdf(z[e == 1], mu, phi) - th[2] * v[e == 1]
        return -(hit.sum() + ig_logsf(z[e == 0], mu, phi).sum())

    th = optimize.minimize(
        nll, [5.5, -1.2, -1.0], method="Nelder-Mead", options={"maxiter": 4000}
    ).x
    return math.exp(th[0]), math.exp(th[1]), float(th[2])


def scale(h, j, mu, phi, c, n0):
    """Log scale of fall j from what was known when it began."""
    done = h["end"][:j] <= h["start"][j]
    t, e, lv = h["t"][:j][done], h["e"][:j][done], h["lv"][:j][done]
    n = int((e == 1).sum())
    own = own_scale(t / np.exp(c * lv), e, mu, phi) if n else 0.0
    return c * h["lv"][j] + n / (n + n0) * own


def log_score(h, j, ls, mu, phi):
    x = np.array([h["t"][j] / math.exp(ls)])
    if h["e"][j]:
        return float((ig_logpdf(x, mu, phi) + np.log(x))[0])
    return float(ig_logsf(x, mu, phi)[0])


def score(H, n0, c_on=True):
    """Log score of each fall from the other tickers' fit and its own earlier falls."""
    perm = np.random.default_rng(1).permutation(len(H))
    v = []
    for f in (perm[i::FOLDS] for i in range(FOLDS)):
        mu, phi, c = fit_panel([H[i] for i in range(len(H)) if i not in set(f)])
        for i in f:
            h = H[i]
            v += [log_score(h, j, scale(h, j, mu, phi, c, n0), mu, phi) for j in range(len(h["t"]))]
    return float(np.mean(v))


def after(H, date, n0):
    """Everything fitted on the falls finished by ``date``, scored on falls that begin
    later, with the plain curve (volatility's slope at zero) alongside."""
    past = [x for x in (before(h, date) for h in H) if len(x["t"])]
    mu, phi, c = fit_panel(past)
    flat = fit_panel([{**h, "lv": h["lv"] * 0} for h in past])
    with_vol, without = [], []
    for h in H:
        for j in np.flatnonzero(h["idx"][h["start"]] >= pd.Timestamp(date)):
            with_vol.append(log_score(h, j, scale(h, j, mu, phi, c, n0), mu, phi))
            without.append(
                log_score(h, j, scale(h, j, flat[0], flat[1], 0.0, n0), flat[0], flat[1])
            )
    return c, float(np.mean(with_vol)), float(np.mean(without)), len(with_vol)


def dip_split(px):
    """A line's dips below its running high: all, running, the first 60% of its days', later."""
    days, running = dip_days(px)
    cut = int(len(px) * 0.6)
    highs = np.flatnonzero(px >= np.maximum.accumulate(px))
    gaps = np.diff(highs)
    later = gaps[(gaps > 1) & (highs[:-1] >= cut)].astype(float)
    return np.array(days, float), running, np.array(dip_days(px[:cut])[0], float), later


def lognormal_sf(k, mu, sigma):
    """P(days > k) of a lognormal on whole days, truncated below two days."""
    z = lambda x: (np.log(np.asarray(x, float) + 0.5) - mu) / sigma  # noqa: E731
    return np.exp(special.log_ndtr(-z(k)) - special.log_ndtr(-z(1)))


def lognormal_fit(k):
    """As long_run.dip_histogram, from the same start; its running stretch is left out."""

    def nll(x):
        s = math.exp(x[1])
        return -np.log(
            np.maximum(lognormal_sf(k - 1, x[0], s) - lognormal_sf(k, x[0], s), 1e-300)
        ).sum()

    best = min(
        (
            optimize.minimize(nll, x0, method="Nelder-Mead", options={"maxiter": 2000})
            for x0 in ([0.0, 0.7], [-5.0, 1.3])
        ),
        key=lambda r: r.fun,
    )
    return float(best.x[0]), math.exp(best.x[1])


def blended_score(te, own, pan, w):
    """Log probability of later dips under the two curves blended as long_run does."""
    sf = lambda x: lognormal_sf(x, *own) ** w * lognormal_sf(x, *pan) ** (1 - w)  # noqa: E731
    return np.log(np.maximum(sf(te - 1) - sf(te), 1e-300))


def km(t, e, q):
    o = np.argsort(t)
    t, e, s = t[o], e[o], 1.0
    for i in range(len(t)):
        s *= 1 - e[i] / (len(t) - i)
        if s <= 1 - q:
            return int(t[i])
    return None


def from_here(rows):
    r = pd.DataFrame(rows, columns=["a", "t", "w", "left", "e"])
    x = np.column_stack([np.ones(len(r)), np.log(r.a), np.log(r.t), np.log(r.w)])
    y, e = np.log(r.left.to_numpy()), r.e.to_numpy()

    def nll(th):
        s = math.exp(th[-1])
        z = (y - x @ th[:-1]) / s
        return -(np.sum(e * (-0.5 * z**2 - th[-1])) + np.sum((1 - e) * special.log_ndtr(-z)))

    th = optimize.minimize(nll, [6, 0, 0, 0, 0], method="L-BFGS-B").x
    return {
        k: round(float(v), 4) for k, v in zip(["b0", "b_a", "b_t", "b_w"], th, strict=False)
    } | {"sigma": round(math.exp(th[-1]), 4)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache")
    args = ap.parse_args()
    data = {s: gated(v) for s, v in closes(args.cache).items()}
    H, rows, table = [], [], []
    top, split = [], []
    for ser in data.values():
        px = ser.to_numpy()
        dd = px / np.maximum.accumulate(px) - 1
        spans = falls(px)
        H.append(holding(ser))
        top.append(len(spans))
        split.append(dip_split(px))
        for a, b, done in spans:
            table.append((b - a, -dd[a : b + 1].min(), done))
            path = px[a : b + 1] / px[a] - 1
            worst = np.minimum.accumulate(path)
            for t in CHECKPOINTS:
                if t < b - a and -worst[t] >= MIN_FALL:
                    a_ = max(-math.log1p(path[t]), 0.005)
                    rows.append((a_, t, -worst[t], b - a - t + 0.5, done))
    H = [h for h in H if len(h["t"])]
    scan = {n0: round(score(H, n0), 4) for n0 in (5, 8, 12, 20, 25, 40)}
    # Flat from about here on: where the data can't tell, keep the holding's own falls.
    n0 = min(k for k, v in scan.items() if v >= max(scan.values()) - 0.002)
    mu, phi, c = fit_panel(H)
    tb = pd.DataFrame(table, columns=["t", "d", "e"])
    print(f"{len(tb)} falls, {int((tb.e == 0).sum())} running, longest {int(tb.t.max())} days")
    print(
        json.dumps(
            {
                "RECOVERY_PHI": round(phi, 3),
                "RECOVERY_MU": round(mu, 1),
                "RECOVERY_VOL_SLOPE": round(c, 3),
                "RECOVERY_N0": n0,
                "n0_scan": scan,
            }
        )
    )
    lv = np.exp(np.concatenate([h["lv"] for h in H])) * RECOVERY_VOL_REF
    print(f"volatility before a fall, 1st to 99th percentile: {np.quantile(lv, 0.01):.2f}-{np.quantile(lv, 0.99):.2f}")  # fmt: skip
    print(f"falls per stock: {int(np.median(top))}")
    dips = np.concatenate([d for d, _, _, _ in split])
    print(f"dips per stock: {int(np.median([len(d) for d, _, _, _ in split]))}; median {np.median(dips):.0f} days, p90 {np.quantile(dips, 0.9):.0f}, {np.mean(dips <= 5):.0%} within a week")  # fmt: skip
    dmu, dsg = lognormal_fit(dips)
    scan_d = {}
    for k in (0, 30, 100, 300, 1000):
        v = [blended_score(te, lognormal_fit(tr), (dmu, dsg), len(tr) / (len(tr) + k))
             for _, _, tr, te in split if len(tr) >= 5 and len(te)]  # fmt: skip
        scan_d[k] = round(float(np.mean(np.concatenate(v))), 4)
    past = {t: (round(float(np.mean(dips > t)), 3), round(float(lognormal_sf(t, dmu, dsg)), 3)) for t in (5, 21, 63, 252, 756, 2520)}  # fmt: skip
    print(json.dumps({"DIP_MU": round(dmu, 3), "DIP_SIGMA": round(dsg, 3), "DIP_N0": min(k for k, v in scan_d.items() if v >= max(scan_d.values()) - 0.002), "n0_scan": scan_d, "past_seen_vs_curve": past}))  # fmt: skip
    for lo, hi in (("1962", "1999-12-31"), ("2000", "2012-12-31"), ("2013", "2100")):
        cut = [holding(v[(v.index >= lo) & (v.index <= hi)]) for v in data.values() if len(v[(v.index >= lo) & (v.index <= hi)]) > VOL_MIN_DAYS]  # fmt: skip
        cut = [h for h in cut if len(h["t"])]
        print(
            f"slope {lo}-{hi[:4]}: {fit_panel(cut)[2]:.2f}, {sum(len(h['t']) for h in cut)} falls"
        )
    for date in ("1995-01-01", "2005-01-01", "2015-01-01"):
        sl, w, wo, m = after(H, date, n0)
        print(f"fit by {date[:4]}, {m} falls later: slope {sl:.2f}, score {w:.3f} vs {wo:.3f} without volatility ({w - wo:+.3f})")  # fmt: skip
    for lo, hi in ((0.1, 0.2), (0.2, 0.3), (0.3, 0.5), (0.5, 1.01)):
        m = tb[(tb.d >= lo) & (tb.d < hi)]
        print(f"depth {lo:.0%}-{hi:.0%}: median {km(m.t.to_numpy(), m.e.to_numpy(), 0.5)}, "
              f"p90 {km(m.t.to_numpy(), m.e.to_numpy(), 0.9)}, n={len(m)}")  # fmt: skip
    print("FROM_HERE", json.dumps(from_here(rows)), f"rows={len(rows)}")


if __name__ == "__main__":
    main()
