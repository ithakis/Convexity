"""FLAML/LightGBM training on the SAR label with strict time-ordered CV.

- Shards are loaded in d1 order (06 wrote them ordered), so FLAML's
  split_type="time" (sklearn TimeSeriesSplit under the hood: ordered,
  expanding folds) sees genuinely chronological data.
- Train = d1 < TEST_START; the 6-month holdout is NEVER passed to fit().
- retrain_full=True: FLAML refits the best config on all of train after the
  search — automl.model IS the final model.
- The deployment artifact is the raw LightGBM Booster (model_to_string), so
  production needs lightgbm but not flaml.

Usage:
    python ml/scripts/07_train_flaml.py                    # 4h budget (config)
    python ml/scripts/07_train_flaml.py --budget 300       # smoke run, 5 min
    python ml/scripts/07_train_flaml.py --max-rows 400000  # memory-escalation lever
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ml import config  # noqa: E402


from ml.scripts.train_utils import group_spearman, load_split  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=config.TIME_BUDGET_S)
    ap.add_argument("--max-rows", type=int, default=None)
    args = ap.parse_args()

    import numpy as np
    import psutil

    t0 = time.time()
    idf = np.load(config.FEATURES_DIR / "idf.npy")

    X, y, w, meta = load_split("train", idf, args.max_rows)
    assert meta["d1"].is_monotonic_increasing, "rows must be time-ordered for split_type='time'"
    rss = psutil.Process().memory_info().rss / 1e9
    print(f"train X: {X.shape}, nnz={X.nnz:,} ({X.nnz/len(y):.0f}/row), "
          f"RSS {rss:.1f} GB", flush=True)

    from flaml import AutoML, tune

    # Search-space overrides, learned from the first (killed) run: LightGBM's
    # sparse multi-val bin construction dominated 16h of wall time because the
    # sklearn API re-bins per trial x fold and FLAML varies log_max_bin.
    # 32 bins fixed (plenty for tf-idf values) + a tree-count cap keep every
    # trial's cost bounded so the 4h budget buys search, not re-binning.
    # Caps sized from a measured probe: 100 trees x 64 leaves on 1M rows =
    # ~8.6 min at 8 threads => a 300-tree/3M-row fit is ~1.3h, bounding both
    # the worst single trial and the final retrain_full.
    custom_hp = {"lgbm": {
        "log_max_bin": {"domain": 5},
        "n_estimators": {"domain": tune.lograndint(lower=20, upper=300),
                         "init_value": 50, "low_cost_init_value": 20},
        "num_leaves": {"domain": tune.lograndint(lower=4, upper=256),
                       "init_value": 32, "low_cost_init_value": 4},
    }}

    automl = AutoML()
    automl.fit(
        X_train=X, y_train=y, sample_weight=w,
        task="regression", metric="mse",
        estimator_list=["lgbm"], time_budget=args.budget,
        eval_method="cv", split_type="time", n_splits=config.N_CV_FOLDS,
        n_jobs=config.N_JOBS, seed=config.SEED,
        retrain_full=True, early_stop=True,
        mem_thres=3 * 1024 ** 3,
        model_history=False,
        custom_hp=custom_hp,
        log_file_name=str(config.ARTIFACTS_DIR / "flaml.log"),
        verbose=3,
    )
    print(f"best config: {automl.best_config}", flush=True)
    print(f"best CV mse: {automl.best_loss:.6f}", flush=True)

    # Out-of-fold-ish predictions for tier cuts come from a chronological
    # refit below; the deployment Booster is the retrain_full model.
    booster = automl.model.estimator.booster_

    # ---- holdout evaluation (never seen by fit)
    Xt, yt, wt, meta_t = load_split("test", idf)
    pred = booster.predict(Xt).astype("float32")
    resid = yt - pred
    mse = float(np.mean(resid ** 2))
    mae = float(np.mean(np.abs(resid)))
    r2 = 1.0 - mse / float(np.var(yt))
    ic = group_spearman(meta_t, pred)
    # decile spread of realized SAR across predicted deciles (rank-based so
    # ties/near-constant predictions still fill all ten buckets)
    order = np.argsort(pred, kind="stable")
    dec = np.empty(len(pred), dtype=int)
    dec[order] = np.arange(len(pred)) * 10 // len(pred)
    dec_means = [float(np.mean(yt[dec == i])) for i in range(10)]

    train_pred = booster.predict(X).astype("float32")
    metrics = {
        "train_rows": int(len(y)), "test_rows": int(len(yt)),
        "nnz_per_row": round(X.nnz / len(y), 1),
        "best_config": automl.best_config,
        "cv_mse": float(automl.best_loss),
        "holdout": {"mse": mse, "mae": mae, "r2": r2,
                    "group_spearman_ic": ic, "decile_mean_sar": dec_means},
        "train_ic_insample": group_spearman(meta, train_pred),
        "budget_s": args.budget,
        "minutes_total": round((time.time() - t0) / 60, 1),
    }
    config.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    out = config.ARTIFACTS_DIR / config.ARTIFACT_VERSION
    out.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(out / "model.lgbm.txt"))
    np.save(out / "idf.npy", idf)
    mask_path = config.FEATURES_DIR / "col_mask.npy"
    if mask_path.exists():
        import shutil as _sh
        _sh.copy2(mask_path, out / "col_mask.npy")
    (out / "train_metrics.json").write_text(json.dumps(metrics, indent=2, default=str))
    # holdout predictions cached for 08_validate / 09_tier_cuts
    meta_t.assign(pred=pred).to_parquet(config.FEATURES_DIR / "holdout_pred.parquet",
                                        index=False)
    meta.assign(pred=train_pred).to_parquet(config.FEATURES_DIR / "train_pred.parquet",
                                            index=False)
    print(json.dumps(metrics, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
