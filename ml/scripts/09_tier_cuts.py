"""Calibrate the Market read onto its own LIVE-SHAPED history.

Why this was rewritten: the v1 cuts were fitted on a calibration refit's
per-(symbol, day, session) predictions, but the app shows a ticker-level
7-day recency-weighted mean of ~15 articles. That aggregate is centred near
-0.007 with sd 0.0035, so 93% of live holding-days fell in the "bearish" band
(meta.json's own holdout check already showed 63%).

Now the calibration distribution is built exactly the way the app scores:
one value per (ticker, as-of day) from ml/scripts/06's panel, whose features
come from ml_features.window_vector / weighted_sar — the serving functions.

  --model v1.1   score = the v1 encoder's per-article predictions,
                 recency/source/novelty/relevance-weighted over the 7-day
                 window (panel column enc_wmean). Horizon 1d. Calibrated on
                 Jul-Sep 2023 and verified on Oct-Dec 2023 — the only window
                 where the DEPLOYED booster is out of sample. Two earlier
                 attempts failed and are why: the deployed booster on Jan-Jun
                 2023 is in-sample (~5x more dispersed than live: holdout
                 tiers 2/13/81/1/2%), and the year-cross-fitted booster there
                 is a different model whose output level differs (3/26/68/1/2%).
                 Even this same-booster split misses the +/-3pp mass gate
                 (the score level drifts with the news mix), which is why the
                 app anchors the percentile to its own live history once it
                 has one (ml_sentiment.calibrate); these knots bootstrap it.
  --model v2     score = the window model (07 --stage window); horizon 5d
                 unless --horizon says otherwise (plan: prefer 5d).

Output, stored in <artifact>/tier_cuts.json:
  z   = (score - mu) / sigma                     (mu, sigma on the window)
  pct = percentile of z against 101 knots        (the live-shaped distribution)
  tier by pct: <=5 very_bearish, <=15 bearish, 15-85 no_edge, >=85 bullish,
      >=95 very_bullish — "no edge", not "neutral": the backtest's decile means
      are noise except in the tails.
  exp_sar: the realized mean SAR per 5-point percentile band in the
      calibration window, made monotone by isotonic regression (PAV) — the
      "expected move" the UI shows.

Windows (CAL_WINDOWS): v2 calibrates on [SEL_START, TEST_START) = Jan-Jun
2023 and verifies on the Jul-Dec 2023 holdout; v1.1 as described above. The
verification rows run through ml_sentiment.calibrate — the production
function — for the gates: every tier's mass within +/-3pp of design, and the
tail tiers' realized mean SAR of the right sign.

Usage:
    python ml/scripts/09_tier_cuts.py --model v1.1
    python ml/scripts/09_tier_cuts.py --model v2 [--horizon 1]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ml import config  # noqa: E402
from ml.scripts.train_utils import daily_ic, load_panel  # noqa: E402

TIER_PCT = [5.0, 15.0, 85.0, 95.0]
# (calibrate from, calibrate to / verify from). Verification runs to end of data.
CAL_WINDOWS = {"v2": (config.SEL_START, config.TEST_START),
               "v1.1": ("2023-07-01", "2023-10-01")}
DESIGN_MASS = [0.05, 0.10, 0.70, 0.10, 0.05]
PCT_EDGES = list(range(0, 101, 5))


def _pav(y, w):
    """Weighted pool-adjacent-violators: the non-decreasing fit to y."""
    blocks = [[float(v), float(n), 1] for v, n in zip(y, w)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0]:
            a, b = blocks[i], blocks[i + 1]
            n = a[1] + b[1]
            blocks[i] = [(a[0] * a[1] + b[0] * b[1]) / n, n, a[2] + b[2]]
            del blocks[i + 1]
            i = max(0, i - 1)
        else:
            i += 1
    out = []
    for v, _, k in blocks:
        out.extend([v] * k)
    return out


def _scores(model: str, horizon: int):
    import duckdb

    if model == "v1.1":
        df = load_panel(["symbol", "date", "enc_wmean AS score", f"sar_{horizon}d AS y"])
    else:
        df = duckdb.connect().execute(f"""
            SELECT symbol, date, score_{horizon}d AS score, sar_{horizon}d AS y
            FROM read_parquet('{config.FEATURES_DIR / 'window_pred.parquet'}')
            ORDER BY date, symbol""").df()
    return df.dropna(subset=["score"])


def main() -> None:
    import numpy as np

    from convexity.ml_sentiment import calibrate

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=["v1.1", "v2"], required=True)
    ap.add_argument("--horizon", type=int, default=None)
    args = ap.parse_args()
    if args.horizon is None:
        args.horizon = 1 if args.model == "v1.1" else 5
    version = "mlsent-v1.1" if args.model == "v1.1" else "mlsent-v2"
    t0 = time.time()

    df = _scores(args.model, args.horizon)
    d = df["date"].astype("datetime64[ns]").to_numpy()
    lo, hi = (np.datetime64(x) for x in CAL_WINDOWS[args.model])
    cal_m = (d >= lo) & (d < hi)
    ho_m = d >= hi
    s_cal = df.loc[cal_m, "score"].to_numpy("float64")
    y_cal = df.loc[cal_m, "y"].to_numpy("float64")
    mu, sigma = float(s_cal.mean()), float(s_cal.std())
    z_cal = (s_cal - mu) / sigma
    knots = [float(v) for v in np.quantile(z_cal, np.linspace(0, 1, 101))]

    cal = {"version": version, "score": "enc_wmean" if args.model == "v1.1" else "window",
           "horizon_days": args.horizon, "mu": mu, "sigma": sigma,
           "pct_knots": knots, "tier_pct": TIER_PCT, "tiers": [
               "very_bearish", "bearish", "no_edge", "bullish", "very_bullish"],
           "calibration_window": list(CAL_WINDOWS[args.model]),
           "n_calibration": int(cal_m.sum())}

    # expected-SAR table: realized mean label per percentile band, isotonic
    xs, idx = np.unique(np.asarray(knots), return_index=True)
    pct_cal = np.interp(z_cal, xs, np.arange(101, dtype="float64")[idx])
    ok = np.isfinite(y_cal)
    band = np.clip(np.searchsorted(PCT_EDGES, pct_cal[ok], side="right") - 1, 0,
                   len(PCT_EDGES) - 2)
    means, counts = [], []
    for k in range(len(PCT_EDGES) - 1):
        yy = y_cal[ok][band == k]
        means.append(float(yy.mean()) if len(yy) else 0.0)
        counts.append(int(len(yy)))
    cal["exp_sar"] = {"pct_edges": PCT_EDGES, "sar": [round(v, 4) for v in _pav(means, counts)],
                      "raw": [round(v, 4) for v in means], "n": counts}

    # ---- holdout, through the production function
    hs = df.loc[ho_m, "score"].to_numpy("float64")
    hy = df.loc[ho_m, "y"].to_numpy("float64")
    tiers = [calibrate(v, cal)["tier"] for v in hs]
    names = cal["tiers"]
    mass = [round(sum(1 for t in tiers if t == n) / max(1, len(tiers)), 4) for n in names]
    tmeans = []
    for n in names:
        yy = np.array([y for y, t in zip(hy, tiers) if t == n and np.isfinite(y)])
        tmeans.append(round(float(yy.mean()), 4) if len(yy) else None)
    ic = daily_ic(df.loc[ho_m, "date"].to_numpy(), hs, hy, horizon=args.horizon)
    gates = {
        "mass_within_3pp": all(abs(m - dm) <= 0.03 for m, dm in zip(mass, DESIGN_MASS)),
        "tail_signs": (tmeans[0] is not None and tmeans[0] < 0
                       and tmeans[4] is not None and tmeans[4] > 0),
    }
    cal["holdout_verification"] = {"mass": mass, "design_mass": DESIGN_MASS,
                                   "tier_mean_sar": tmeans, "n": int(len(hs)),
                                   "daily_ic": round(ic["mean"], 4), "t_nw": round(ic["t_nw"], 2),
                                   "n_days": ic["n_days"], "gates": gates}
    out = config.ARTIFACTS_DIR / version
    out.mkdir(parents=True, exist_ok=True)
    (out / "tier_cuts.json").write_text(json.dumps(cal, indent=2))
    report = {"model": args.model, "horizon": args.horizon, "mu": mu, "sigma": sigma,
              "exp_sar": cal["exp_sar"], "holdout": cal["holdout_verification"],
              "minutes": round((time.time() - t0) / 60, 1)}
    (config.REPORTS_DIR / f"tier_cuts_{version}.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)
    print("GATES PASS" if all(gates.values()) else f"GATES FAIL: {gates}", flush=True)


if __name__ == "__main__":
    main()
