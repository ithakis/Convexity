"""Model-class selection after gate 5 failed (ridge-dense beat the full model).

Protocol (holdout discipline): candidates are trained on d1 < 2023-01-01 and
compared by group IC on the SELECTION window [2023-01-01, 2023-07-01) — the
same protocol the tier-cut calibration used. The Jul-Dec 2023 holdout is NOT
touched here; the winner is retrained on the full training window by the
caller and evaluated on the holdout exactly once.

Candidates:
  ridge_dense   — Ridge(alpha=1) on the 33 dense features (gate-5 baseline)
  lgbm_dense    — FLAML lgbm on dense features only (trials are seconds at
                  33 columns, so a short budget buys a real search)
  lgbm_full_56  — the deployed config (56 trees / 17 leaves) on full features
  lgbm_full_300 — higher-capacity fixed config (300 trees / 64 leaves /
                  lr 0.05) on full features: "was the search just starved?"
  blend         — mean of rank-transformed ridge_dense + lgbm_full_300

Usage:
    python ml/scripts/12_model_selection.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ml import config  # noqa: E402
from ml.scripts.train_utils import group_spearman, load_split  # noqa: E402

SEL_START = "2023-01-01"


def main() -> None:
    import lightgbm as lgb
    import numpy as np
    from sklearn.linear_model import Ridge

    from portfolio_tracker.ml_features import DENSE_COLUMNS

    t0 = time.time()
    idf = np.load(config.FEATURES_DIR / "idf.npy")
    X, y, w, meta = load_split("train", idf)
    d1 = meta["d1"].astype(str)
    tr = (d1 < SEL_START).to_numpy()
    va = ~tr
    Xtr, ytr, wtr = X[tr], y[tr], w[tr]
    Xva, meta_va = X[va], meta.loc[va].reset_index(drop=True)
    n_dense = len(DENSE_COLUMNS)
    print(f"sel-train {tr.sum():,} rows, sel-valid {va.sum():,} rows", flush=True)

    results: dict[str, float] = {}

    def ic_of(pred) -> float:
        return group_spearman(meta_va, np.asarray(pred, dtype="float64"))

    # ---- ridge dense (reference)
    Xd_tr, Xd_va = Xtr[:, -n_dense:].toarray(), Xva[:, -n_dense:].toarray()
    ridge = Ridge(alpha=1.0).fit(Xd_tr, ytr, sample_weight=wtr)
    pred_ridge = ridge.predict(Xd_va)
    results["ridge_dense"] = ic_of(pred_ridge)
    print(f"ridge_dense IC: {results['ridge_dense']:.4f}", flush=True)

    # ---- FLAML on dense only (cheap trials -> real search in a short budget)
    from flaml import AutoML

    automl = AutoML()
    automl.fit(X_train=Xd_tr, y_train=ytr, sample_weight=wtr,
               task="regression", metric="mse", estimator_list=["lgbm"],
               time_budget=1200, eval_method="cv", split_type="time",
               n_splits=config.N_CV_FOLDS, n_jobs=config.N_JOBS,
               seed=config.SEED, retrain_full=True, early_stop=True,
               verbose=1)
    pred_ldense = automl.predict(Xd_va)
    results["lgbm_dense"] = ic_of(pred_ldense)
    print(f"lgbm_dense IC: {results['lgbm_dense']:.4f} "
          f"(config {automl.best_config})", flush=True)

    # ---- deployed config on full features (reference for the comparison)
    tm = json.loads((config.ARTIFACTS_DIR / config.ARTIFACT_VERSION /
                     "train_metrics.json").read_text())
    cfg56 = {k: v for k, v in tm["best_config"].items()
             if k not in ("n_estimators", "log_max_bin")}
    cfg56.update({"objective": "regression", "verbosity": -1,
                  "num_threads": config.N_JOBS, "max_bin": 31, "seed": config.SEED})
    b56 = lgb.train(cfg56, lgb.Dataset(Xtr, ytr, weight=wtr),
                    num_boost_round=int(tm["best_config"]["n_estimators"]))
    results["lgbm_full_56"] = ic_of(b56.predict(Xva))
    print(f"lgbm_full_56 IC: {results['lgbm_full_56']:.4f}", flush=True)

    # ---- higher-capacity full-features config
    cfg300 = {"objective": "regression", "verbosity": -1,
              "num_threads": config.N_JOBS, "max_bin": 31, "seed": config.SEED,
              "learning_rate": 0.05, "num_leaves": 64, "min_child_samples": 100,
              "colsample_bytree": 0.8, "reg_lambda": 3.0}
    b300 = lgb.train(cfg300, lgb.Dataset(Xtr, ytr, weight=wtr),
                     num_boost_round=300)
    pred_300 = b300.predict(Xva)
    results["lgbm_full_300"] = ic_of(pred_300)
    print(f"lgbm_full_300 IC: {results['lgbm_full_300']:.4f}", flush=True)

    # ---- rank blend of the two families
    def rank01(a):
        a = np.asarray(a, dtype="float64")
        return np.argsort(np.argsort(a)) / max(1, len(a) - 1)

    results["blend_ridge_300"] = ic_of(rank01(pred_ridge) + rank01(pred_300))
    print(f"blend_ridge_300 IC: {results['blend_ridge_300']:.4f}", flush=True)

    report = {
        "protocol": f"train < {SEL_START}, select on [{SEL_START}, {config.TEST_START})",
        "sel_valid_rows": int(va.sum()),
        "ic": {k: round(float(v), 4) for k, v in results.items()},
        "lgbm_dense_best_config": automl.best_config,
        "minutes": round((time.time() - t0) / 60, 1),
    }
    (config.REPORTS_DIR / "model_selection.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
