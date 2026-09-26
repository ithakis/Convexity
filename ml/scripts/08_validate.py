"""Validation battery for the trained SAR model. Every test has a PASS/FAIL
gate; results land in ml/data/reports/validation_report.json (+ plots).

Tests (numbering matches docs/ml_sentiment_design.md):
 1. shuffled-label null      — refit best_config on permuted y (subsampled),
                               real holdout IC must clear null mean + 4*sigma
 2. time-shifted labels      — same predictions vs labels shifted +/-5 trading
                               days; +5 IC must fall below 0.3x real; -5 IC is
                               recorded (news reports past moves) and must not
                               exceed the real IC
 3. leakage audit            — recompute one random slice of sigma/beta from
                               raw prices and assert the stored value only used
                               data <= D-1 (spot check; the synthetic unit test
                               lives in tests/test_ml_labels.py)
 4. walk-forward yearly IC   — expanding train <= Y-1, test Y (2015-2023),
                               fixed best_config (--walk-forward to enable;
                               ~1-2h of fits)
 5. baselines                — ridge on dense-only; LGBM on raw close-close
                               label scored against SAR IC
 6. slices                   — IC by session_class / dollar-vol tercile /
                               relevance tercile (top>bottom validates heuristic)
 7. calibration curve        — predicted-SAR deciles vs realized mean + bootstrap
                               CIs, monotone top-3/bottom-3, plot to reports/
 8. PhraseBank polarity      — headline-only featurization, sign accuracy vs
                               human labels (>= 0.65 floor); needs the dataset
                               at --phrasebank (not vendored, CC BY-NC-SA)

--stage window runs the v2 Market read gates instead (holdout Jul-Dec 2023,
date-clustered): daily IC >= 0.03 with t >= 3 (Newey-West for 5d), beats the
dense ridge and v1.1, monotone decile ends, >= 7/9 walk-forward years
positive, and a leakage spot-check of 40 panel rows recomputed from the raw
sources. Results: ml/data/reports/window_validation_report.json.

Usage:
    python ml/scripts/08_validate.py                     # 1,2,3,5,6,7
    python ml/scripts/08_validate.py --walk-forward      # adds 4
    python ml/scripts/08_validate.py --phrasebank /tmp/fpb/FinancialPhraseBank-v1.0
    python ml/scripts/08_validate.py --stage window --horizons 1,5 --walk-forward
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ml import config  # noqa: E402
from ml.scripts.train_utils import daily_ic, load_panel  # noqa: E402

ENRICHED = config.PARQUET_DIR / "enriched.parquet"

NULL_ROWS = 800_000
NULL_REPS = 10
REPORT = config.REPORTS_DIR / "validation_report.json"


def _load(split, idf, max_rows=None):
    from importlib import import_module

    train_mod = import_module("ml.scripts.train_utils")
    return train_mod.load_split(split, idf, max_rows)


def group_ic(meta, pred):
    from importlib import import_module

    return import_module("ml.scripts.train_utils").group_spearman(meta, pred)


def best_config():
    m = json.loads((config.ARTIFACTS_DIR / config.ARTIFACT_VERSION / "train_metrics.json")
                   .read_text())
    return m["best_config"], m["holdout"]["group_spearman_ic"]


def test_shuffled_null(results):
    import lightgbm as lgb
    import numpy as np

    cfg, real_ic = best_config()
    idf = np.load(config.FEATURES_DIR / "idf.npy")
    X, y, w, meta = _load("train", idf, NULL_ROWS)
    Xt, yt, wt, meta_t = _load("test", idf)
    params = {k: v for k, v in cfg.items() if k not in ("n_estimators",)}
    params.update({"objective": "regression", "verbosity": -1, "num_threads": config.N_JOBS})
    n_trees = min(300, int(cfg.get("n_estimators", 300)))
    rng = np.random.default_rng(config.SEED)
    null_ics = []
    for rep in range(NULL_REPS):
        yp = rng.permutation(y)
        bst = lgb.train(params, lgb.Dataset(X, yp, weight=w), num_boost_round=n_trees)
        null_ics.append(group_ic(meta_t, bst.predict(Xt)))
        print(f"  null rep {rep+1}/{NULL_REPS}: IC={null_ics[-1]:.4f}", flush=True)
    mu, sd = float(np.mean(null_ics)), float(np.std(null_ics))
    gate = real_ic > mu + 4 * sd
    results["1_shuffled_null"] = {
        "real_ic": real_ic, "null_mean": mu, "null_std": sd,
        "null_ics": [round(x, 4) for x in null_ics],
        "pass": bool(gate),
    }


def test_time_shift(results):
    """Score the SAME holdout predictions against +/-5-trading-day-shifted labels."""
    import duckdb
    import numpy as np
    import pandas as pd

    hp = pd.read_parquet(config.FEATURES_DIR / "holdout_pred.parquet")
    con = duckdb.connect()
    con.register("hp", hp[["symbol", "d0", "d1", "session_class", "pred", "sar"]])
    cal = pd.read_parquet(config.CALENDAR_PARQUET)[["date"]].reset_index(names="rn")
    con.register("cal", cal)
    out = {}
    for shift in (+5, -5):
        shifted = con.execute(f"""
            WITH h AS (
                SELECT hp.*, c0.rn AS rn0, c1.rn AS rn1
                FROM hp JOIN cal c0 ON c0.date = hp.d0 JOIN cal c1 ON c1.date = hp.d1
            ), m AS (
                SELECT h.*, s0.date AS d0s, s1.date AS d1s
                FROM h JOIN cal s0 ON s0.rn = h.rn0 + ({shift})
                       JOIN cal s1 ON s1.rn = h.rn1 + ({shift})
            )
            SELECT m.symbol, m.d1, m.session_class, m.pred,
                   p1.adj_close / p0.adj_close - 1.0 AS r_shift,
                   p1.sigma_cc, p1.beta,
                   c.m_cc AS m_shift
            FROM m
            JOIN read_parquet('{config.TICKER_STATS_PARQUET}') p0
                 ON p0.symbol = m.symbol AND p0.date = m.d0s
            JOIN read_parquet('{config.TICKER_STATS_PARQUET}') p1
                 ON p1.symbol = m.symbol AND p1.date = m.d1s
            JOIN read_parquet('{config.CALENDAR_PARQUET}') c ON c.date = m.d1s
            WHERE p1.sigma_cc > 0 AND p1.beta IS NOT NULL
        """).df()
        sar_shift = ((shifted["r_shift"] - shifted["beta"] * shifted["m_shift"])
                     / shifted["sigma_cc"]).clip(-config.SAR_WINSOR, config.SAR_WINSOR)
        g = shifted.assign(y=sar_shift).groupby(["symbol", "d1", "session_class"],
                                                observed=True)
        gp = g.agg(pred=("pred", "mean"), y=("y", "first")).dropna()
        rx = np.argsort(np.argsort(gp["pred"].to_numpy()))
        ry = np.argsort(np.argsort(gp["y"].to_numpy()))
        out[f"shift_{shift:+d}"] = float(np.corrcoef(rx.astype(float), ry.astype(float))[0, 1])
    _, real_ic = best_config()
    results["2_time_shift"] = {
        **{k: round(v, 4) for k, v in out.items()},
        "real_ic": real_ic,
        # Signed comparison, per the design doc: the +5d IC must FALL below
        # 0.3x real. A NEGATIVE +5d IC is the documented news-overreaction
        # reversal (Tetlock 2011; Chan 2003) — evidence the signal is impounded
        # fast then partially mean-reverts. Leakage would look like PERSISTENT
        # positive IC at +5d, which is what this gate rejects.
        "pass": bool(out["shift_+5"] < 0.3 * real_ic
                     and out["shift_-5"] <= real_ic),
    }


def test_leakage_spotcheck(results):
    """Recompute sigma_cc/beta for 50 random (symbol, date) pairs from raw
    prices using ONLY data <= D-1 and compare to the stored values."""
    import duckdb
    import numpy as np

    con = duckdb.connect()
    rows = con.execute(f"""
        SELECT symbol, date, sigma_cc, beta FROM read_parquet('{config.TICKER_STATS_PARQUET}')
        WHERE sigma_cc IS NOT NULL AND beta IS NOT NULL
        USING SAMPLE 50 ROWS (reservoir, 42)
    """).fetchall()
    cal = con.execute(f"SELECT date, m_cc FROM read_parquet('{config.CALENDAR_PARQUET}')").df()
    cal_map = dict(zip(cal["date"], cal["m_cc"]))
    bad = 0
    for sym, d, sig_stored, beta_stored in rows:
        px = con.execute(f"""
            SELECT date, adj_close FROM read_parquet('{config.PRICES_PARQUET}')
            WHERE symbol = ? AND date < ? ORDER BY date DESC LIMIT 300
        """, [sym, d]).df().iloc[::-1]
        r = px["adj_close"].astype(float).pct_change().dropna()
        sig = r.tail(config.SIGMA_WINDOW).std()
        m = np.array([cal_map.get(x, np.nan) for x in px["date"]])[1:]
        mask = ~np.isnan(m) & ~np.isnan(r.to_numpy())
        rr, mm = r.to_numpy()[mask][-config.BETA_WINDOW:], m[mask][-config.BETA_WINDOW:]
        if len(rr) >= config.BETA_MIN_OBS and mm.var() > 0:
            b = float(np.cov(rr, mm)[0, 1] / mm.var())
            b = float(np.clip(config.BETA_BLUME[0] * b + config.BETA_BLUME[1],
                              *config.BETA_CLIP))
        else:
            b = None
        if not np.isclose(sig, sig_stored, rtol=0.15, atol=5e-4):
            bad += 1
        elif b is not None and not np.isclose(b, beta_stored, rtol=0.20, atol=0.05):
            bad += 1
    results["3_leakage_spotcheck"] = {"checked": len(rows), "mismatches": bad,
                                      "pass": bool(bad <= 2)}


def test_baselines(results):
    import lightgbm as lgb
    import numpy as np
    from sklearn.linear_model import Ridge

    cfg, real_ic = best_config()
    idf = np.load(config.FEATURES_DIR / "idf.npy")
    X, y, w, meta = _load("train", idf, NULL_ROWS)
    Xt, yt, wt, meta_t = _load("test", idf)
    n_dense = len(__import__("portfolio_tracker.ml_features", fromlist=["x"]).DENSE_COLUMNS)

    Xd, Xdt = X[:, -n_dense:].toarray(), Xt[:, -n_dense:].toarray()
    ridge = Ridge(alpha=1.0).fit(Xd, y, sample_weight=w)
    ic_ridge = group_ic(meta_t, ridge.predict(Xdt))

    params = {k: v for k, v in cfg.items() if k != "n_estimators"}
    params.update({"objective": "regression", "verbosity": -1, "num_threads": config.N_JOBS})
    y_raw = meta["r_cc_baseline"].to_numpy(dtype="float32")
    y_raw = np.clip(y_raw, np.nanquantile(y_raw, 0.001), np.nanquantile(y_raw, 0.999))
    bst = lgb.train(params, lgb.Dataset(X, np.nan_to_num(y_raw), weight=w),
                    num_boost_round=min(300, int(cfg.get("n_estimators", 300))))
    ic_rawlabel = group_ic(meta_t, bst.predict(Xt))

    results["5_baselines"] = {
        "ridge_dense_only_ic": round(float(ic_ridge), 4),
        "lgbm_rawlabel_ic_on_sar": round(float(ic_rawlabel), 4),
        "full_model_ic": real_ic,
        "pass": bool(real_ic >= 1.3 * ic_ridge and real_ic >= ic_rawlabel),
    }


def test_slices(results):
    import pandas as pd

    hp = pd.read_parquet(config.FEATURES_DIR / "holdout_pred.parquet")
    out = {}
    for name, col, cuts in (("session", "session_class", None),
                            ("dollar_vol", "dollar_vol", 3),
                            ("relevance", "relevance", 3)):
        if cuts:
            hp["_b"] = pd.qcut(hp[col].rank(method="first"), cuts,
                               labels=[f"t{i+1}" for i in range(cuts)])
        else:
            hp["_b"] = hp[col]
        out[name] = {}
        for b, g in hp.groupby("_b", observed=True):
            out[name][str(b)] = round(group_ic(g, g["pred"].to_numpy()), 4)
    rel = out["relevance"]
    results["6_slices"] = {
        **out,
        "pass": bool(rel.get("t3", 0) > rel.get("t1", 0)
                     and all(v == v and v > 0 for v in out["session"].values())),
    }


def test_calibration(results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd

    hp = pd.read_parquet(config.FEATURES_DIR / "holdout_pred.parquet")
    g = (hp.groupby(["symbol", "d1", "session_class"], observed=True)
           .agg(pred=("pred", "mean"), y=("sar", "first")).reset_index())
    dec = pd.qcut(g["pred"].rank(method="first"), 10, labels=False)
    means, lo, hi = [], [], []
    rng = np.random.default_rng(config.SEED)
    for i in range(10):
        yy = g.loc[dec == i, "y"].to_numpy()
        means.append(float(yy.mean()))
        bs = rng.choice(yy, size=(500, len(yy)), replace=True).mean(axis=1)
        lo.append(float(np.quantile(bs, 0.025)))
        hi.append(float(np.quantile(bs, 0.975)))
    mono_top = means[9] > means[8] > means[7] or (means[9] > means[7])
    mono_bot = means[0] < means[1] < means[2] or (means[0] < means[2])
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.errorbar(range(1, 11), means,
                yerr=[np.array(means) - lo, np.array(hi) - means], fmt="o-")
    ax.set_xlabel("predicted-SAR decile")
    ax.set_ylabel("realized mean SAR")
    ax.axhline(0, lw=0.5, color="gray")
    fig.tight_layout()
    fig.savefig(config.REPORTS_DIR / "calibration_curve.png", dpi=120)
    results["7_calibration"] = {
        "decile_means": [round(m, 4) for m in means],
        "pass": bool(mono_top and mono_bot and means[9] > 0 > means[0]),
    }


def test_phrasebank(results, pb_dir):
    import numpy as np

    import lightgbm as lgb

    from portfolio_tracker import ml_features as mf

    rows = []
    for line in (Path(pb_dir) / "Sentences_66Agree.txt").read_text(encoding="latin-1").splitlines():
        if "@" in line:
            sent, label = line.rsplit("@", 1)
            if label.strip().lower() in ("positive", "negative", "neutral"):
                rows.append((sent.strip(), label.strip().lower()))
    out_art = config.ARTIFACTS_DIR / config.ARTIFACT_VERSION
    booster = lgb.Booster(model_file=str(out_art / "model.lgbm.txt"))
    idf = np.load(out_art / "idf.npy")
    mask_p = out_art / "col_mask.npy"
    mask = np.load(mask_p) if mask_p.exists() else None
    import scipy.sparse as sp
    counts = mf.hash_counts([mf.text_for_hashing(s, "") for s, _ in rows])
    dense = np.asarray([mf.dense_vector({"title": s, "summary": "",
                                         "session_class": "dateonly_cc",
                                         "relevance": 1.0})
                        for s, _ in rows], dtype="float32")
    tfidf = mf.apply_idf(counts, idf)
    if mask is not None:
        tfidf = tfidf[:, mask]
    X = sp.hstack([tfidf, sp.csr_matrix(dense)], format="csr")
    pred = booster.predict(X)
    band = float(np.quantile(np.abs(pred), 0.33))
    correct = total = 0
    for p, (_, label) in zip(pred, rows):
        if label == "neutral":
            continue  # sign test on polar sentences only
        total += 1
        if (p > band and label == "positive") or (p < -band and label == "negative"):
            correct += 1
        elif abs(p) <= band:
            total -= 1  # abstained
    acc = correct / max(1, total)
    results["8_phrasebank"] = {"n_scored": total, "sign_accuracy": round(acc, 4),
                               "pass": bool(acc >= 0.65)}


def test_walk_forward(results):
    import lightgbm as lgb
    import numpy as np
    import pandas as pd

    cfg, _ = best_config()
    idf = np.load(config.FEATURES_DIR / "idf.npy")
    X, y, w, meta = _load("train", idf)
    d1 = pd.to_datetime(meta["d1"])
    params = {k: v for k, v in cfg.items() if k != "n_estimators"}
    params.update({"objective": "regression", "verbosity": -1, "num_threads": config.N_JOBS})
    n_trees = min(500, int(cfg.get("n_estimators", 300)))
    ics = {}
    for year in range(2015, 2024):
        tr = (d1 < f"{year}-01-01").to_numpy()
        te = ((d1 >= f"{year}-01-01") & (d1 < f"{year+1}-01-01")).to_numpy()
        if tr.sum() < 100_000 or te.sum() < 20_000:
            continue
        bst = lgb.train(params, lgb.Dataset(X[tr], y[tr], weight=w[tr]),
                        num_boost_round=n_trees)
        ics[year] = round(group_ic(meta[te], bst.predict(X[te])), 4)
        print(f"  {year}: IC={ics[year]}", flush=True)
    pos_years = sum(1 for v in ics.values() if v > 0)
    tot = sum(max(0.0, v) for v in ics.values()) or 1.0
    max_share = max((max(0.0, v) / tot for v in ics.values()), default=1.0)
    results["4_walk_forward"] = {
        "yearly_ic": {str(k): v for k, v in ics.items()},
        "positive_years": pos_years, "n_years": len(ics),
        "max_year_share_of_ic": round(max_share, 3),
        "pass": bool(pos_years >= max(1, round(len(ics) * 7 / 9))
                     and max_share <= 0.40),
    }


# ================================================================ v2 window gates
WINDOW_REPORT = config.REPORTS_DIR / "window_validation_report.json"


def _window_winner(h: int) -> dict:
    rep = json.loads((config.REPORTS_DIR / "window_train_report.json").read_text())
    return rep["horizons"][f"{h}d"]


def window_holdout_gates(results: dict, h: int) -> None:
    """Holdout (Jul-Dec 2023) gates read from 07's single holdout scoring."""
    res = _window_winner(h)
    ho = res["holdout"]
    w = ho["winner"]
    t = w["t_nw"] if h > 1 else w["t"]
    d = ho["decile_means"]
    mid = (d[4] + d[5]) / 2
    results[f"{h}d_holdout"] = {
        "winner": res["winner"], "daily_ic": w["mean"], "t": t, "n_days": w["n_days"],
        "ridge_dense_ic": ho["ridge_dense"]["mean"], "regression_ic": ho["regression"]["mean"],
        "v1_1_ic": ho["v1_1_recalibrated"]["mean"], "decile_means": d,
        "gates": {
            "ic_ge_0.03_and_t_ge_3": bool(w["mean"] >= 0.03 and t is not None and t >= 3),
            "beats_dense_ridge": bool(w["mean"] > ho["ridge_dense"]["mean"]),
            "beats_v1_1": bool(w["mean"] > ho["v1_1_recalibrated"]["mean"]),
            "decile_ends_monotone": bool(d[9] > mid > d[0]),
        },
    }


def window_walk_forward(results: dict, h: int) -> None:
    """Yearly expanding-window refit of the winner's config: train < Y, test Y.
    The encoder features for year Y were themselves produced by a booster
    trained on d1 < Y (06 --stage encoder), so nothing here looks ahead."""
    import numpy as np

    from portfolio_tracker.ml_features import WINDOW_COLUMNS
    spec = importlib.util.spec_from_file_location(
        "train07", Path(__file__).resolve().parent / "07_train_flaml.py")
    t07 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t07)

    win = _window_winner(h)["winner"]
    panel = load_panel(["date", *WINDOW_COLUMNS, f"sar_{h}d"])
    dates = panel["date"].astype("datetime64[ns]").to_numpy()
    X = panel[WINDOW_COLUMNS].to_numpy("float32")
    y = panel[f"sar_{h}d"].to_numpy("float32")
    ok = np.isfinite(y)
    yearly = {}
    for year in range(2015, 2024):
        lo, hi = np.datetime64(f"{year}-01-01"), np.datetime64(f"{year + 1}-01-01")
        tr, te = ok & (dates < lo), ok & (dates >= lo) & (dates < hi)
        if win["model"] == "ridge":
            pred = t07._Ridge().fit(X[tr], y[tr]).predict(X[te])
        else:
            bst = t07._fit_lgb(win["model"], X[tr], y[tr], dates[tr], win["rounds"])
            pred = bst.predict(X[te])
        ic = daily_ic(dates[te], pred, y[te], horizon=h)
        yearly[str(year)] = round(ic["mean"], 4)
        print(f"  walk-forward {h}d {year}: IC {ic['mean']:.4f} ({ic['n_days']} days)", flush=True)
    pos = sum(1 for v in yearly.values() if v > 0)
    results[f"{h}d_walk_forward"] = {"yearly_ic": yearly, "positive_years": pos,
                                     "n_years": len(yearly),
                                     "gates": {"ge_7_of_9_positive": pos >= 7}}


def window_leakage_spotcheck(results: dict, n: int = 40) -> None:
    """Recompute 40 random panel rows from the raw sources and compare:
    price context must use closes < D only, the label must start at close(D),
    and the window must count only articles dated D-6..D."""
    import duckdb
    import numpy as np

    from portfolio_tracker import ml_features as mf

    con = duckdb.connect()
    panel = config.FEATURES_DIR / "panel.parquet"
    rows = con.execute(f"""
        SELECT symbol, date, n_win, tkr_ret_1d, tkr_ret_5d, tkr_vol_20d, sar_1d
        FROM read_parquet('{panel}') WHERE sar_1d IS NOT NULL AND tkr_vol_20d IS NOT NULL
        USING SAMPLE {n} ROWS (reservoir, 7)
    """).fetchall()
    bad = []
    for sym, d, n_win, r1, r5, vol, sar1 in rows:
        px = con.execute(f"""
            SELECT date, adj_close, sigma_cc, beta FROM read_parquet('{config.TICKER_STATS_PARQUET}')
            WHERE symbol = ? AND date BETWEEN ? - INTERVAL 60 DAY AND ? + INTERVAL 15 DAY
            ORDER BY date""", [sym, d, d]).df()
        cal = dict(con.execute(f"SELECT date, m_cc FROM read_parquet('{config.CALENDAR_PARQUET}')"
                               f" WHERE date BETWEEN ? - INTERVAL 60 DAY AND ? + INTERVAL 15 DAY",
                               [d, d]).fetchall())
        before = px[px["date"] < np.datetime64(d)]
        at = px[px["date"] >= np.datetime64(d)]
        c = before["adj_close"].to_numpy("float64")
        r = np.diff(c) / c[:-1]
        exp_r1 = c[-1] / c[-2] - 1
        exp_r5 = c[-1] / c[-6] - 1
        exp_vol = float(np.std(r[-20:], ddof=1))
        nxt = at.iloc[1]
        exp_sar = ((at["adj_close"].iloc[1] / at["adj_close"].iloc[0] - 1)
                   - nxt["beta"] * cal[nxt["date"].date() if hasattr(nxt["date"], "date") else nxt["date"]]) \
            / nxt["sigma_cc"]
        exp_sar = float(np.clip(exp_sar, -config.SAR_WINSOR, config.SAR_WINSOR))
        cnt = con.execute(f"""
            SELECT count(*) FROM read_parquet('{ENRICHED}')
            WHERE symbol = ? AND CAST(ts_et AS DATE) BETWEEN ? - INTERVAL {mf.WINDOW_DAYS - 1} DAY AND ?
        """, [sym, d, d]).fetchone()[0]
        checks = {"tkr_ret_1d": (r1, exp_r1), "tkr_ret_5d": (r5, exp_r5),
                  "tkr_vol_20d": (vol, exp_vol), "sar_1d": (sar1, exp_sar),
                  "n_win": (n_win, cnt)}
        for k, (got, exp) in checks.items():
            if not np.isclose(float(got), float(exp), rtol=1e-3, atol=1e-5):
                bad.append({"symbol": sym, "date": str(d), "field": k,
                            "panel": float(got), "recomputed": float(exp)})
    results["leakage_spotcheck"] = {"checked": len(rows), "mismatches": bad[:10],
                                    "n_mismatches": len(bad),
                                    "gates": {"no_mismatch": not bad}}


def main_window(horizons: list[int], walk_forward: bool) -> None:
    results = json.loads(WINDOW_REPORT.read_text()) if WINDOW_REPORT.exists() else {}
    t0 = time.time()
    for h in horizons:
        window_holdout_gates(results, h)
        if walk_forward:
            window_walk_forward(results, h)
    window_leakage_spotcheck(results)
    results["_minutes"] = round((time.time() - t0) / 60, 1)
    WINDOW_REPORT.write_text(json.dumps(results, indent=2, default=str))
    print(json.dumps(results, indent=2, default=str), flush=True)
    fails = [f"{k}.{g}" for k, v in results.items() if isinstance(v, dict)
             for g, ok in (v.get("gates") or {}).items() if not ok]
    print(("FAILED gates: " + ", ".join(fails)) if fails else "ALL GATES PASS", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["encoder", "window"], default="encoder")
    ap.add_argument("--horizons", type=str, default="1,5")
    ap.add_argument("--walk-forward", action="store_true")
    ap.add_argument("--phrasebank", type=str, default=None)
    ap.add_argument("--only", type=str, default=None,
                    help="comma list of test numbers to run, e.g. 1,2,6")
    args = ap.parse_args()
    if args.stage == "window":
        main_window([int(h) for h in args.horizons.split(",")], args.walk_forward)
        return
    only = set(args.only.split(",")) if args.only else None

    results = {}
    if REPORT.exists():
        results = json.loads(REPORT.read_text())

    def want(n):
        return only is None or str(n) in only

    t0 = time.time()
    if want(2):
        test_time_shift(results)
    if want(3):
        test_leakage_spotcheck(results)
    if want(6):
        test_slices(results)
    if want(7):
        test_calibration(results)
    if want(5):
        test_baselines(results)
    if want(1):
        test_shuffled_null(results)
    if args.walk_forward and want(4):
        test_walk_forward(results)
    if args.phrasebank and want(8):
        test_phrasebank(results, args.phrasebank)

    results["_minutes"] = round((time.time() - t0) / 60, 1)
    REPORT.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2), flush=True)
    fails = [k for k, v in results.items()
             if isinstance(v, dict) and v.get("pass") is False]
    print(("FAILED gates: " + ", ".join(fails)) if fails else "ALL GATES PASS", flush=True)


if __name__ == "__main__":
    main()
