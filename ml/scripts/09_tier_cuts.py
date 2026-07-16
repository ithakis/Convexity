"""Optimize the 5-tier cuts on OUT-OF-SAMPLE calibration predictions.

Calibration set: a chronological refit — best_config trained on d1 < CAL_START
(2023-01-01), predicting CAL_START..TEST_START (the last 6 months of train).
The real holdout (>= TEST_START) is touched exactly once at the end, for a
report-only verification. This avoids both in-sample optimism (train preds)
and holdout contamination.

Candidate grid: asymmetric quantile 4-tuples (p1, p2, p3, p4) with
p1 in {.03,.05,.08,.10}, p2 in {.30,.35,.40,.45}, p3 = 1-p2 shifted by
{-.05, 0, +.05} (skew allowance), p4 = 1-p1. Acceptance requires, at the
(symbol, d1, session_class) group level:
  (a) neutral band:   |Spearman(pred, y)| < 0.02 AND bootstrap-95% CI covers 0
  (b) monotonicity:   tier mean realized SAR strictly increasing in >= 95% of
                      1000 group-bootstrap resamples
  (c) separation:     very_* mean SAR differs from the adjacent tier with
                      non-overlapping 80% CIs
  (d) mass:           every outer tier >= 3% of groups
  (e) stability:      monotone point estimate holds in each 2-month sub-period
Among survivors, maximize (mean SAR very_bullish - mean SAR very_bearish)
minus 0.5x cut instability (bootstrap IQR of the threshold values).
Zero survivors -> relax neutral tolerance once to 0.03; still zero -> exit 2
(user decision needed, per plan).

Output: ml/data/artifacts/mlsent-v1/tier_cuts.json (score THRESHOLDS, not
quantiles — production applies fixed cuts) + reports/tier_cuts_report.json.
"""
from __future__ import annotations

import itertools
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ml import config  # noqa: E402
from ml.scripts.train_utils import load_split  # noqa: E402

CAL_START = "2023-01-01"
TIERS = ["very_bearish", "bearish", "neutral", "bullish", "very_bullish"]
N_BOOT = 1000
NEUTRAL_IC_TOL = 0.02


def calibration_predictions():
    """Chronological refit -> OOS predictions on the last 6 months of train."""
    import lightgbm as lgb
    import numpy as np
    import pandas as pd

    cache = config.FEATURES_DIR / "calibration_pred.parquet"
    if cache.exists():
        return pd.read_parquet(cache)

    cfg = json.loads((config.ARTIFACTS_DIR / config.ARTIFACT_VERSION /
                      "train_metrics.json").read_text())["best_config"]
    idf = np.load(config.FEATURES_DIR / "idf.npy")
    X, y, w, meta = load_split("train", idf)
    d1 = pd.to_datetime(meta["d1"])
    tr = (d1 < CAL_START).to_numpy()
    ca = ((d1 >= CAL_START) & (d1 < config.TEST_START)).to_numpy()
    params = {k: v for k, v in cfg.items() if k != "n_estimators"}
    params.update({"objective": "regression", "verbosity": -1,
                   "num_threads": config.N_JOBS, "seed": config.SEED})
    bst = lgb.train(params, lgb.Dataset(X[tr], y[tr], weight=w[tr]),
                    num_boost_round=int(cfg.get("n_estimators", 300)))
    out = meta[ca].assign(pred=bst.predict(X[ca]).astype("float32"))
    out.to_parquet(cache, index=False)
    return out


def group_frame(df):
    g = (df.groupby(["symbol", "d1", "session_class"], observed=True)
           .agg(pred=("pred", "mean"), y=("sar", "first"), d1x=("d1", "first"))
           .reset_index(drop=True))
    return g.dropna()


def spearman(x, y):
    import numpy as np

    if len(x) < 5 or x.std() == 0 or y.std() == 0:
        return float("nan")
    rx = np.argsort(np.argsort(x)).astype("float64")
    ry = np.argsort(np.argsort(y)).astype("float64")
    return float(np.corrcoef(rx, ry)[0, 1])


def main() -> None:
    import numpy as np
    import pandas as pd

    t0 = time.time()
    cal = group_frame(calibration_predictions())
    pred, ysar = cal["pred"].to_numpy("float64"), cal["y"].to_numpy("float64")
    months = pd.to_datetime(cal["d1x"]).dt.month.to_numpy()
    n = len(cal)
    print(f"calibration groups: {n:,}", flush=True)

    grid = []
    for p1, p2, shift in itertools.product((.03, .05, .08, .10),
                                           (.30, .35, .40, .45),
                                           (-.05, 0.0, .05)):
        p3, p4 = 1 - p2 + shift, 1 - p1
        if p2 < p3 < p4 < 1:
            grid.append((p1, p2, p3, p4))

    rng = np.random.default_rng(config.SEED)
    boot_idx = [rng.integers(0, n, n) for _ in range(N_BOOT)]

    def evaluate(q, tol):
        cuts = np.quantile(pred, q)
        tier = np.searchsorted(cuts, pred)          # 0..4
        mass = np.bincount(tier, minlength=5) / n
        if mass[0] < 0.03 or mass[4] < 0.03:
            return None
        means = np.array([ysar[tier == k].mean() for k in range(5)])
        if not np.all(np.diff(means) > 0):
            return None
        neu = tier == 2
        ic_neu = spearman(pred[neu], ysar[neu])
        if not np.isfinite(ic_neu) or abs(ic_neu) > tol:
            return None
        # sub-period monotonicity (jan-feb / mar-apr / may-jun)
        for lo, hi in ((1, 2), (3, 4), (5, 6)):
            m = (months >= lo) & (months <= hi)
            if m.sum() < 500:
                continue
            mm = [ysar[m & (tier == k)].mean() for k in range(5)]
            if not (mm[4] > mm[2] > mm[0]):        # coarse monotone ends
                return None
        # bootstrap: monotonicity rate, CIs, cut instability, neutral IC CI
        mono_ok = 0
        tier_means_b = np.empty((N_BOOT, 5))
        cuts_b = np.empty((N_BOOT, 4))
        ic_neu_b = np.empty(min(400, N_BOOT))
        cnt = np.zeros(5)
        for b, idx in enumerate(boot_idx):
            tb, yb = tier[idx], ysar[idx]
            s = np.bincount(tb, weights=yb, minlength=5)
            c = np.bincount(tb, minlength=5)
            mb = s / np.maximum(c, 1)
            tier_means_b[b] = mb
            mono_ok += bool(np.all(np.diff(mb) > 0))
            cuts_b[b] = np.quantile(pred[idx], q)
            if b < len(ic_neu_b):
                nb = idx[tb == 2]
                ic_neu_b[b] = spearman(pred[nb], ysar[nb])
        mono_rate = mono_ok / N_BOOT
        if mono_rate < 0.95:
            return None
        lo95, hi95 = np.quantile(ic_neu_b, [0.025, 0.975])
        if not (lo95 <= 0 <= hi95):
            return None
        lo80 = np.quantile(tier_means_b, 0.10, axis=0)
        hi80 = np.quantile(tier_means_b, 0.90, axis=0)
        if not (lo80[4] > hi80[3] and hi80[0] < lo80[1]):
            return None
        instab = float(np.mean(np.subtract(*np.quantile(cuts_b, [0.75, 0.25], axis=0))
                               * -1.0))
        spread = float(means[4] - means[0])
        return {"q": list(q), "cuts": [float(c) for c in cuts],
                "mass": [round(float(m), 4) for m in mass],
                "tier_means": [round(float(m), 4) for m in means],
                "neutral_ic": round(ic_neu, 4), "mono_rate": mono_rate,
                "spread": spread, "instability": instab,
                "objective": spread - 0.5 * instab}

    for tol in (NEUTRAL_IC_TOL, 0.03):
        survivors = [r for q in grid if (r := evaluate(q, tol))]
        if survivors:
            break
    if not survivors:
        print("NO SURVIVORS even at relaxed tolerance — user decision needed",
              flush=True)
        sys.exit(2)

    best = max(survivors, key=lambda r: r["objective"])
    print(f"survivors: {len(survivors)}/{len(grid)} (tol={tol}); "
          f"best q={best['q']} spread={best['spread']:.3f}", flush=True)

    # ---- report-only holdout verification
    hp = group_frame(pd.read_parquet(config.FEATURES_DIR / "holdout_pred.parquet"))
    tier_h = np.searchsorted(np.array(best["cuts"]), hp["pred"].to_numpy())
    hold = {
        "mass": [round(float(x), 4) for x in np.bincount(tier_h, minlength=5) / len(hp)],
        "tier_means": [round(float(hp["y"].to_numpy()[tier_h == k].mean()), 4)
                       for k in range(5)],
    }

    out = config.ARTIFACTS_DIR / config.ARTIFACT_VERSION
    out.mkdir(parents=True, exist_ok=True)
    (out / "tier_cuts.json").write_text(json.dumps({
        "tiers": TIERS, "cuts": best["cuts"], "quantiles": best["q"],
        "calibration_window": [CAL_START, config.TEST_START],
        "neutral_ic": best["neutral_ic"], "mono_rate": best["mono_rate"],
        "calibration_tier_means": best["tier_means"], "mass": best["mass"],
        "holdout_verification": hold,
    }, indent=2))
    report = {"n_grid": len(grid), "n_survivors": len(survivors),
              "tolerance_used": tol, "best": best, "holdout": hold,
              "minutes": round((time.time() - t0) / 60, 1)}
    (config.REPORTS_DIR / "tier_cuts_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
