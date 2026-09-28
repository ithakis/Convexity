"""Model training. Two stages:

--stage encoder (default) — the v1 per-article model, FLAML/LightGBM on the
SAR label with strict time-ordered CV. Since v2 it is the TEXT ENCODER: its
per-article predictions are summarised into window features (06 --stage
encoder cross-fits it by year so those features are out-of-sample).

--stage window — the v2 Market read at the unit the app scores, one row per
(ticker, as-of day). See train_window() for the candidate set and protocol.

Encoder notes:

- Shards are loaded in d1 order (06 wrote them ordered), so FLAML's
  split_type="time" (sklearn TimeSeriesSplit under the hood: ordered,
  expanding folds) sees genuinely chronological data.
- Train = d1 < TEST_START; the 6-month holdout is NEVER passed to fit().
- retrain_full=True: FLAML refits the best config on all of train after the
  search — automl.model IS the final model.
- The deployment artifact is the raw LightGBM Booster (model_to_string), so
  production needs lightgbm but not flaml.

Usage:
    python ml/scripts/07_train_flaml.py                    # encoder, 4h budget (config)
    python ml/scripts/07_train_flaml.py --budget 300       # encoder smoke run, 5 min
    python ml/scripts/07_train_flaml.py --max-rows 400000  # memory-escalation lever
    python ml/scripts/07_train_flaml.py --stage window     # v2 window model (~20 min)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ml import config  # noqa: E402


from ml.scripts.train_utils import (daily_ic, decile_means, group_spearman,  # noqa: E402
                                    load_panel, load_split)


def train_encoder(args) -> None:
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


# ================================================================ v2 window model
# Protocol (holdout discipline, same as the v1 model-selection replication):
#   select   train on [2011, SEL_START), score the SELECTION window
#            [SEL_START, TEST_START) — candidates and boosting rounds are
#            chosen here, by date-clustered daily IC.
#   final    refit the winner on [2011, TEST_START); the Jul-Dec 2023 holdout
#            is scored exactly once, for the gates in 08 --stage window.
# Candidates: LightGBM lambdarank and rank_xendcg (query = date, label =
# per-date quintile grade: rank tickers WITHIN a day, the decision the Market
# read supports), LightGBM L2 regression on the SAR label, and a dense ridge.
WINDOW_DIR = config.ARTIFACTS_DIR / "mlsent-v2"
_ROUNDS = (50, 100, 200, 400, 700)


def _grades(dates, y, n: int = 5):
    import numpy as np
    import pandas as pd

    s = pd.Series(y).groupby(np.asarray(dates)).rank(pct=True, method="average")
    return np.clip(np.floor(s.to_numpy() * n - 1e-9), 0, n - 1).astype("int32")


def _group_sizes(dates):
    import numpy as np

    _, counts = np.unique(np.asarray(dates), return_counts=True)  # dates are sorted
    return counts


def _lgb_params(objective: str) -> dict:
    p = {"objective": objective, "learning_rate": 0.03, "num_leaves": 31,
         "min_data_in_leaf": 2000, "feature_fraction": 0.8, "bagging_fraction": 0.7,
         "bagging_freq": 1, "lambda_l2": 10.0, "verbosity": -1, "seed": config.SEED,
         "num_threads": config.N_JOBS, "max_bin": 63}
    if objective == "lambdarank":
        p["lambdarank_truncation_level"] = 50
    return p


class _Ridge:
    """Dense ridge baseline: train-median imputation, 1/99% clipping, z-scoring.
    Everything is fitted on the training rows only."""

    def fit(self, X, y):
        import numpy as np
        from sklearn.linear_model import Ridge

        self.med = np.nanmedian(X, axis=0)
        Xf = np.where(np.isnan(X), self.med, X)
        self.lo, self.hi = np.nanpercentile(Xf, 1, axis=0), np.nanpercentile(Xf, 99, axis=0)
        Xf = np.clip(Xf, self.lo, self.hi)
        self.mu, self.sd = Xf.mean(axis=0), Xf.std(axis=0) + 1e-12
        self.m = Ridge(alpha=10.0).fit((Xf - self.mu) / self.sd, y)
        return self

    def predict(self, X):
        import numpy as np

        Xf = np.clip(np.where(np.isnan(X), self.med, X), self.lo, self.hi)
        return self.m.predict((Xf - self.mu) / self.sd)


def _fit_lgb(objective, X, y, dates, rounds):
    import lightgbm as lgb

    if objective in ("lambdarank", "rank_xendcg"):
        ds = lgb.Dataset(X, _grades(dates, y), group=_group_sizes(dates), free_raw_data=True)
    else:
        ds = lgb.Dataset(X, y, free_raw_data=True)
    return lgb.train(_lgb_params(objective), ds, num_boost_round=rounds)


def _ic(dates, score, y, h):
    r = daily_ic(dates, score, y, horizon=h)
    return {k: (round(v, 4) if isinstance(v, float) else v)
            for k, v in r.items() if k != "series"}


def train_window(args) -> None:
    import duckdb
    import numpy as np

    from convexity.ml_features import WINDOW_COLUMNS

    t0 = time.time()
    cols = ["symbol", "date", *WINDOW_COLUMNS, "v1_wmean", "sar_1d", "sar_5d"]
    panel = load_panel(cols)
    dates = panel["date"].astype("datetime64[ns]").to_numpy()
    X_all = panel[WINDOW_COLUMNS].to_numpy("float32")
    sel, test = np.datetime64(config.SEL_START), np.datetime64(config.TEST_START)
    print(f"window: panel {len(panel):,} rows, {len(WINDOW_COLUMNS)} features "
          f"({(time.time()-t0)/60:.1f} min)", flush=True)

    report = {"protocol": {"train": f"[{config.ENCODER_FIRST_YEAR}, {config.SEL_START})",
                           "select": f"[{config.SEL_START}, {config.TEST_START})",
                           "holdout": f"[{config.TEST_START}, end)"},
              "n_rows": int(len(panel)), "features": WINDOW_COLUMNS, "horizons": {}}
    WINDOW_DIR.mkdir(parents=True, exist_ok=True)
    preds_out = panel[["symbol", "date", "v1_wmean", "sar_1d", "sar_5d"]].copy()

    for h in config.HORIZONS:
        ycol = f"sar_{h}d"
        y_all = panel[ycol].to_numpy("float32")
        ok = np.isfinite(y_all)
        tr = ok & (dates < sel)
        va = ok & (dates >= sel) & (dates < test)
        fin = ok & (dates < test)
        ho = ok & (dates >= test)
        res = {"n_train": int(tr.sum()), "n_select": int(va.sum()), "n_holdout": int(ho.sum()),
               "select": {}, "holdout": {}}

        # ---- selection: every candidate x rounds on the selection window
        best = None
        for obj in ("lambdarank", "rank_xendcg", "regression"):
            bst = _fit_lgb(obj, X_all[tr], y_all[tr], dates[tr], max(_ROUNDS))
            for k in _ROUNDS:
                ic = _ic(dates[va], bst.predict(X_all[va], num_iteration=k), y_all[va], h)
                res["select"][f"lgbm_{obj}@{k}"] = ic
                if best is None or ic["mean"] > best[2]:
                    best = (obj, k, ic["mean"])
            print(f"  h={h}d {obj}: " + ", ".join(
                f"{k}:{res['select'][f'lgbm_{obj}@{k}']['mean']:.4f}" for k in _ROUNDS)
                + f" ({(time.time()-t0)/60:.1f} min)", flush=True)
        ridge = _Ridge().fit(X_all[tr], y_all[tr])
        res["select"]["ridge"] = _ic(dates[va], ridge.predict(X_all[va]), y_all[va], h)
        res["select"]["enc_wmean(v1.1-shaped)"] = _ic(
            dates[va], X_all[va, WINDOW_COLUMNS.index("enc_wmean")], y_all[va], h)
        if res["select"]["ridge"]["mean"] > best[2]:
            best = ("ridge", 0, res["select"]["ridge"]["mean"])
        res["winner"] = {"model": best[0], "rounds": best[1], "select_ic": round(best[2], 4)}
        print(f"  h={h}d winner: {res['winner']}", flush=True)

        # ---- final refit on [2011, TEST_START) and the one holdout scoring
        if best[0] == "ridge":
            final = _Ridge().fit(X_all[fin], y_all[fin])
            score = final.predict(X_all)
        else:
            final = _fit_lgb(best[0], X_all[fin], y_all[fin], dates[fin], best[1])
            final.save_model(str(WINDOW_DIR / f"window_{h}d.lgbm.txt"))
            score = final.predict(X_all)
            imp = final.feature_importance("gain")
            res["importance_gain"] = {c: round(float(v), 1) for c, v in
                                      sorted(zip(WINDOW_COLUMNS, imp), key=lambda t: -t[1])}
        preds_out[f"score_{h}d"] = score.astype("float32")
        ridge_f = _Ridge().fit(X_all[fin], y_all[fin])
        reg_f = (final if best[0] == "regression"
                 else _fit_lgb("regression", X_all[fin], y_all[fin], dates[fin],
                               max(best[1], 200)))
        res["holdout"] = {
            "winner": _ic(dates[ho], score[ho], y_all[ho], h),
            "ridge_dense": _ic(dates[ho], ridge_f.predict(X_all[ho]), y_all[ho], h),
            "regression": _ic(dates[ho], reg_f.predict(X_all[ho]), y_all[ho], h),
            "v1_1_recalibrated": _ic(dates[ho], panel["v1_wmean"].to_numpy()[ho],
                                     y_all[ho], h),
            "decile_means": [round(v, 4) for v in
                             decile_means(dates[ho], score[ho], y_all[ho])],
        }
        report["horizons"][f"{h}d"] = res
        print(f"  h={h}d holdout: " + json.dumps(res["holdout"]), flush=True)

    c = duckdb.connect()
    c.register("p", preds_out)
    c.execute(f"COPY p TO '{config.FEATURES_DIR / 'window_pred.parquet'}' (FORMAT parquet)")
    report["minutes"] = round((time.time() - t0) / 60, 1)
    (config.REPORTS_DIR / "window_train_report.json").write_text(json.dumps(report, indent=2))
    (WINDOW_DIR / "train_metrics.json").write_text(json.dumps(report, indent=2))
    print(f"window: done in {report['minutes']} min", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["encoder", "window"], default="encoder")
    ap.add_argument("--budget", type=int, default=config.TIME_BUDGET_S)
    ap.add_argument("--max-rows", type=int, default=None)
    args = ap.parse_args()
    if args.stage == "window":
        train_window(args)
    else:
        train_encoder(args)


if __name__ == "__main__":
    main()
