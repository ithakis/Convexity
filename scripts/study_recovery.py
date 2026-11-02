#!/usr/bin/env python3
"""Recompute the time-to-recover constants in long_run.py and app.js's
INV_REC_DEPTH from public tickers only (docs/recovery_research.md).

    uv run python scripts/study_recovery.py [--cache closes.json]

Fetches each ticker's max daily history from Yahoo, a few at a time with a pause
(about a minute); --cache keeps it in a JSON file for repeat runs. Prints the card
curve's RECOVERY_PHI, RECOVERY_MU and RECOVERY_N0, the Kaplan-Meier table by depth,
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

from convexity.long_run import MIN_FALL, usable_start  # noqa: E402

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


def shrunk(t, e, mu, phi, n0):
    n = int((e == 1).sum())
    return n / (n + n0) * own_scale(t, e, mu, phi) if n else 0.0


def fit_panel(H, n0, rounds=3):
    """The pooled shape and scale, alternating with each holding's shrunk scale."""
    mu, phi, scales = 300.0, 0.3, [0.0] * len(H)
    for _ in range(rounds):
        t = np.concatenate([h[0] / math.exp(s) for h, s in zip(H, scales, strict=True)])
        e = np.concatenate([h[1] for h in H])

        def nll(th):
            m, p = np.exp(th)
            return -(ig_logpdf(t[e == 1], m, p).sum() + ig_logsf(t[e == 0], m, p).sum())

        mu, phi = np.exp(
            optimize.minimize(nll, [math.log(mu), math.log(phi)], method="Nelder-Mead").x
        )
        scales = [shrunk(h[0], h[1], mu, phi, n0) for h in H]
    return float(mu), float(phi)


def score(H, n0):
    """Log score of each fall from the other tickers' curve and its own earlier falls."""
    perm = np.random.default_rng(1).permutation(len(H))
    v = []
    for f in (perm[i::FOLDS] for i in range(FOLDS)):
        mu, phi = fit_panel([H[i] for i in range(len(H)) if i not in set(f)], n0)
        for i in f:
            t, e = H[i]
            for j in range(len(t)):
                ls = shrunk(t[:j], e[:j], mu, phi, n0) if j else 0.0
                x = np.array([t[j] / math.exp(ls)])
                v.append(
                    float((ig_logpdf(x, mu, phi) + np.log(x) if e[j] else ig_logsf(x, mu, phi))[0])
                )
    return float(np.mean(v))


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
    for ser in data.values():
        px = ser.to_numpy()
        dd = px / np.maximum.accumulate(px) - 1
        spans = falls(px)
        H.append((np.array([b - a + 0.5 for a, b, _ in spans]), np.array([d for _, _, d in spans])))
        for a, b, done in spans:
            table.append((b - a, -dd[a : b + 1].min(), done))
            path = px[a : b + 1] / px[a] - 1
            worst = np.minimum.accumulate(path)
            for t in CHECKPOINTS:
                if t < b - a and -worst[t] >= MIN_FALL:
                    a_ = max(-math.log1p(path[t]), 0.005)
                    rows.append((a_, t, -worst[t], b - a - t + 0.5, done))
    H = [h for h in H if len(h[0])]
    scan = {n0: round(score(H, n0), 4) for n0 in (5, 8, 12, 20, 40)}
    n0 = max(scan, key=scan.get)
    mu, phi = fit_panel(H, n0)
    tb = pd.DataFrame(table, columns=["t", "d", "e"])
    print(f"{len(tb)} falls, {int((tb.e == 0).sum())} running, longest {int(tb.t.max())} days")
    print(
        json.dumps(
            {
                "RECOVERY_PHI": round(phi, 3),
                "RECOVERY_MU": round(mu, 1),
                "RECOVERY_N0": n0,
                "n0_scan": scan,
            }
        )
    )
    for lo, hi in ((0.1, 0.2), (0.2, 0.3), (0.3, 0.5), (0.5, 1.01)):
        m = tb[(tb.d >= lo) & (tb.d < hi)]
        print(f"depth {lo:.0%}-{hi:.0%}: median {km(m.t.to_numpy(), m.e.to_numpy(), 0.5)}, "
              f"p90 {km(m.t.to_numpy(), m.e.to_numpy(), 0.9)}, n={len(m)}")  # fmt: skip
    print("FROM_HERE", json.dumps(from_here(rows)), f"rows={len(rows)}")


if __name__ == "__main__":
    main()
