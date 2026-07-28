"""Kappa sensitivity study — how strongly should market sentiment tilt a
stock's total signal?

    s_total,i = clip(kappa * beta_i * s_mkt + s_idio,i, -1, +1)

Runs the REAL production pipeline (Finnhub+yfinance news -> per-article NIM
scoring -> deterministic aggregation) over a large-cap universe (~S&P 100)
to get an empirical cross-section of s_idio and beta for *today*, then
sweeps kappa x s_mkt (today's actual market score plus hypothetical stress
levels) and reports, per (kappa, s_mkt):

  - distribution of s_total (mean / std / min / max / P5 / P95)
  - fraction of names whose SIGN flips vs the pure-idio signal (kappa=0)
  - Spearman rank correlation of s_total vs s_idio (does the systematic
    term destroy the cross-sectional ordering the stock-picker cares about?)
  - fraction of names whose fixed-threshold TIER changes vs kappa=0

Rate-limit care: SELF_CONSISTENCY is forced OFF (1 NIM call/ticker instead
of 2), the module's own token-bucket limiters pace Finnhub (55/min) and NIM
(60/min), and the 30-day disk cache makes re-runs free. Expect ~5-8 min on
a cold cache for ~100 names.

Usage:
    <pt python> scripts/kappa_sensitivity.py [--limit N]
Writes JSON results next to this script's stdout report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from portfolio_tracker import news_sentiment as ns

# S&P 100 constituents (OEX, mid-2026 vintage; Yahoo ticker format).
UNIVERSE = [
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AIG", "AMD", "AMGN", "AMT", "AMZN",
    "AVGO", "AXP", "BA", "BAC", "BK", "BKNG", "BLK", "BMY", "BRK-B", "C",
    "CAT", "CHTR", "CL", "CMCSA", "COF", "COP", "COST", "CRM", "CSCO", "CVS",
    "CVX", "DE", "DHR", "DIS", "DUK", "EMR", "FDX", "GD", "GE", "GILD",
    "GM", "GOOGL", "GS", "HD", "HON", "IBM", "INTC", "INTU", "ISRG", "JNJ",
    "JPM", "KO", "LIN", "LLY", "LMT", "LOW", "MA", "MCD", "MDLZ", "MDT",
    "MET", "META", "MMM", "MO", "MRK", "MS", "MSFT", "NEE", "NFLX", "NKE",
    "NOW", "NVDA", "ORCL", "PEP", "PFE", "PG", "PLTR", "PM", "PYPL", "QCOM",
    "RTX", "SBUX", "SCHW", "SO", "SPG", "T", "TGT", "TMO", "TMUS", "TSLA",
    "TXN", "UNH", "UNP", "UPS", "USB", "V", "VZ", "WFC", "WMT", "XOM",
]

KAPPAS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.75, 1.0]
STRESS_S_MKT = [-0.6, -0.3, 0.3, 0.6]  # plus today's actual value

FIXED_TIERS = ns._FIXED_TIER_THRESHOLDS


def tier_of(s: float) -> str:
    for thresh, tier in FIXED_TIERS:
        if s <= thresh:
            return tier
    return "very_bullish"


def compute_betas(symbols: list[str]) -> dict[str, float]:
    """1y weekly beta vs SPY from one bulk download."""
    import yfinance as yf
    data = yf.download(symbols + ["SPY"], period="1y", interval="1d",
                       auto_adjust=True, progress=False, group_by="column")["Close"]
    weekly = data.resample("W-FRI").last()
    rets = weekly.pct_change().dropna(how="all")
    spy = rets["SPY"].dropna()
    betas: dict[str, float] = {}
    for s in symbols:
        if s not in rets.columns:
            continue
        r = rets[s].dropna()
        joined = r.align(spy, join="inner")
        if len(joined[0]) < 20:
            continue
        cov = float(np.cov(joined[0], joined[1])[0, 1])
        var = float(np.var(joined[1], ddof=1))
        if var > 0:
            betas[s] = cov / var
    return betas


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    return float(np.corrcoef(rx, ry)[0, 1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=len(UNIVERSE))
    args = ap.parse_args()
    universe = UNIVERSE[: args.limit]

    # One NIM pass per ticker for the study — halves the call budget; the
    # production default (2-pass self-consistency) is orthogonal to kappa.
    ns.SELF_CONSISTENCY = False

    t0 = time.time()
    mkt = ns.get_market_sentiment()
    s_mkt_today = mkt["score"] if mkt else None
    print(f"[kappa] market sentiment today: {s_mkt_today}", flush=True)

    idio: dict[str, float] = {}
    failed: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = {pool.submit(ns.get_news_sentiment, s): s for s in universe}
        for fut in as_completed(futs):
            sym = futs[fut]
            done += 1
            try:
                res = fut.result()
            except Exception:
                res = None
            if res and isinstance(res.get("s_idio"), (int, float)):
                idio[sym] = float(res["s_idio"])
            else:
                failed.append(sym)
            if done % 10 == 0:
                print(f"[kappa] scored {done}/{len(universe)} "
                      f"({len(idio)} ok) t={time.time()-t0:.0f}s", flush=True)

    print(f"[kappa] scoring done: {len(idio)} ok, {len(failed)} failed "
          f"({time.time()-t0:.0f}s). failed={failed}", flush=True)

    betas = compute_betas(sorted(idio))
    syms = [s for s in idio if s in betas]
    s_idio = np.array([idio[s] for s in syms])
    beta = np.array([betas[s] for s in syms])
    print(f"[kappa] usable cross-section: {len(syms)} names; "
          f"s_idio mean={s_idio.mean():+.3f} std={s_idio.std():.3f} "
          f"min={s_idio.min():+.3f} max={s_idio.max():+.3f}; "
          f"beta mean={beta.mean():.2f} range=[{beta.min():.2f},{beta.max():.2f}]",
          flush=True)

    mkt_levels = ([("today", s_mkt_today)] if s_mkt_today is not None else []) + \
        [(f"stress {v:+.1f}", v) for v in STRESS_S_MKT]

    results = []
    base_tiers = [tier_of(s) for s in s_idio]
    base_sign = np.sign(s_idio)
    for label, sm in mkt_levels:
        for k in KAPPAS:
            tot = np.clip(k * beta * sm + s_idio, -1.0, 1.0)
            sign_flip = float(np.mean((np.sign(tot) != base_sign) & (base_sign != 0)))
            tier_mig = float(np.mean([tier_of(t) != bt for t, bt in zip(tot, base_tiers)]))
            rho = spearman(tot, s_idio)
            results.append({
                "s_mkt_label": label, "s_mkt": round(float(sm), 4), "kappa": k,
                "mean": round(float(tot.mean()), 4),
                "std": round(float(tot.std()), 4),
                "min": round(float(tot.min()), 4),
                "max": round(float(tot.max()), 4),
                "p5": round(float(np.percentile(tot, 5)), 4),
                "p95": round(float(np.percentile(tot, 95)), 4),
                "sign_flip_frac": round(sign_flip, 4),
                "tier_migration_frac": round(tier_mig, 4),
                "rank_corr_vs_idio": round(rho, 4),
            })

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n_names": len(syms),
        "s_mkt_today": s_mkt_today,
        "s_idio_stats": {
            "mean": round(float(s_idio.mean()), 4), "std": round(float(s_idio.std()), 4),
            "min": round(float(s_idio.min()), 4), "max": round(float(s_idio.max()), 4),
            "p5": round(float(np.percentile(s_idio, 5)), 4),
            "p95": round(float(np.percentile(s_idio, 95)), 4),
        },
        "beta_stats": {"mean": round(float(beta.mean()), 4),
                       "min": round(float(beta.min()), 4),
                       "max": round(float(beta.max()), 4)},
        "per_name": {s: {"s_idio": round(idio[s], 4), "beta": round(betas[s], 3)}
                     for s in syms},
        "sweep": results,
        "failed": failed,
    }
    dest = os.environ.get("KAPPA_OUT",
                          str(Path(__file__).resolve().parent / "kappa_sensitivity_results.json"))
    Path(dest).write_text(json.dumps(out, indent=1))
    print(f"[kappa] wrote {dest}", flush=True)

    hdr = f"{'s_mkt':>12} {'kappa':>5} {'mean':>7} {'std':>6} {'min':>7} {'max':>6} {'p5':>7} {'p95':>6} {'flip%':>6} {'tierMig%':>8} {'rho':>6}"
    print(hdr)
    for r in results:
        print(f"{r['s_mkt_label']:>12} {r['kappa']:>5.2f} {r['mean']:>+7.3f} {r['std']:>6.3f} "
              f"{r['min']:>+7.3f} {r['max']:>+6.3f} {r['p5']:>+7.3f} {r['p95']:>+6.3f} "
              f"{r['sign_flip_frac']*100:>5.1f}% {r['tier_migration_frac']*100:>7.1f}% "
              f"{r['rank_corr_vs_idio']:>6.3f}")


if __name__ == "__main__":
    main()
