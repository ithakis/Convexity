"""Build the training/holdout feature shards from labeled.parquet.

Pipeline (all deterministic, seed in ml/config.py):
1. DuckDB enrichment — co-mention counts (rows sharing a story key on the same
   window day), trailing-only market context (SPY 1d/5d returns and 20d vol as
   of D0; ticker 5d return as of D0), calendar features of D1.
2. Stratified training sample — rows with d1 < TEST_START, budget
   TRAIN_ROW_BUDGET, strata (year x session_class), proportional quotas with a
   per-stratum floor, deterministic md5-hash ordering (re-runs pick the same
   rows). The holdout (d1 >= TEST_START) is NEVER sampled — every row kept.
3. Shard featurization — 200k-row chunks ordered by d1: relevance heuristic
   (convexity.relevance), dense block + hashed term counts
   (convexity.ml_features), saved as counts_*.npz / dense_*.npy /
   meta_*.parquet under ml/data/features/{train,test}/.
4. idf fitted on TRAIN shards only -> features/idf.npy (ships in the artifact).

Usage:
    python ml/scripts/06_build_features.py            # full run
    python ml/scripts/06_build_features.py --sample   # 200k-row smoke test
"""

import argparse
import json
import math
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ml import config  # noqa: E402

ENRICHED_PARQUET = config.PARQUET_DIR / "enriched.parquet"
CHUNK = 200_000
STRATUM_FLOOR = 20_000  # keep sparse early years represented


def enrich() -> None:
    import duckdb
    import pandas as pd

    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='3GB'")
    con.execute("PRAGMA threads=4")

    # Small calendar-side context frame computed in pandas (7.8k rows).
    cal = pd.read_parquet(config.CALENDAR_PARQUET)
    cal["spy_ret_1d"] = cal["m_cc"]
    cal["spy_ret_5d"] = (1.0 + cal["m_cc"]).rolling(5).apply(lambda x: x.prod(), raw=True) - 1.0
    cal["spy_vol_20d"] = cal["m_cc"].rolling(20).std()
    ctx = cal[["date", "spy_ret_1d", "spy_ret_5d", "spy_vol_20d"]]
    con.register("spy_ctx", ctx)

    con.execute(f"""
        CREATE TEMP TABLE tkr5 AS
        SELECT symbol, date,
               adj_close / lag(adj_close, 5) OVER (PARTITION BY symbol ORDER BY date) - 1.0
                   AS tkr_ret_5d
        FROM read_parquet('{config.TICKER_STATS_PARQUET}')
    """)

    con.execute(f"""
        COPY (
            SELECT l.*,
                   count(*) OVER (PARTITION BY coalesce(nullif(l.url, ''), l.title), l.d1)
                       AS co_mention_count,
                   c.spy_ret_1d, c.spy_ret_5d, c.spy_vol_20d,
                   t.tkr_ret_5d,
                   dayofweek(l.d1) AS day_of_week,
                   month(l.d1)     AS month,
                   year(l.d1)      AS year,
                   md5(l.symbol || l.title || CAST(l.d1 AS VARCHAR)) AS row_key
            FROM read_parquet('{config.LABELED_PARQUET}') l
            LEFT JOIN spy_ctx c ON c.date = l.d0
            LEFT JOIN tkr5 t ON t.symbol = l.symbol AND t.date = l.d0
        ) TO '{ENRICHED_PARQUET}' (FORMAT parquet, COMPRESSION zstd)
    """)
    n = con.execute(f"SELECT count(*) FROM read_parquet('{ENRICHED_PARQUET}')").fetchone()[0]
    print(f"enriched.parquet: {n:,} rows", flush=True)


def build_shards(sample: bool) -> None:
    import duckdb
    import numpy as np
    import pandas as pd
    import scipy.sparse as sp

    from convexity import ml_features as mf
    from convexity.relevance import load_company_names, relevance_score

    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='3GB'")
    names = load_company_names()

    # ---- training quotas per (year, session_class), deterministic selection
    strata = con.execute(f"""
        SELECT year, session_class, count(*) AS n
        FROM read_parquet('{ENRICHED_PARQUET}')
        WHERE d1 < DATE '{config.TEST_START}'
        GROUP BY year, session_class
    """).df()
    total = int(strata["n"].sum())
    budget = 200_000 if sample else config.TRAIN_ROW_BUDGET
    quotas = {}
    for _, r in strata.iterrows():
        prop = int(round(r["n"] * budget / total))
        quotas[(int(r["year"]), r["session_class"])] = min(
            int(r["n"]), max(prop, min(int(r["n"]), STRATUM_FLOOR if not sample else prop))
        )
    qdf = pd.DataFrame(
        [(y, s, q) for (y, s), q in quotas.items()], columns=["year", "session_class", "quota"]
    )
    con.register("quotas", qdf)
    print(
        f"train pool {total:,} -> budget {budget:,} (quota sum {int(qdf['quota'].sum()):,})",
        flush=True,
    )

    for split, where, order in (
        ("train", f"""d1 < DATE '{config.TEST_START}' AND rn <= quota""", "d1, row_key"),
        ("test", f"""d1 >= DATE '{config.TEST_START}'""", "d1, row_key"),
    ):
        out_dir = config.FEATURES_DIR / split
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)

        if split == "train":
            src = f"""
                SELECT * FROM (
                    SELECT e.*, row_number() OVER (
                               PARTITION BY e.year, e.session_class ORDER BY e.row_key
                           ) AS rn, q.quota
                    FROM read_parquet('{ENRICHED_PARQUET}') e
                    JOIN quotas q ON q.year = e.year AND q.session_class = e.session_class
                    WHERE e.d1 < DATE '{config.TEST_START}'
                ) WHERE rn <= quota
            """
        else:
            limit = "LIMIT 100000" if sample else ""
            src = f"""
                SELECT * FROM read_parquet('{ENRICHED_PARQUET}')
                WHERE d1 >= DATE '{config.TEST_START}' {limit}
            """
        reader = con.execute(f"SELECT * FROM ({src}) ORDER BY {order}").fetch_record_batch(CHUNK)

        shard = n_rows = 0
        while True:
            try:
                batch = reader.read_next_batch()
            except StopIteration:
                break
            df = batch.to_pandas()
            if not len(df):
                continue
            pub_tier = df["publisher"].map(mf.publisher_tier).astype("float32")
            rel = np.fromiter(
                (
                    relevance_score(t, s, sym, names.get(sym), int(co), float(pt))
                    for t, s, sym, co, pt in zip(
                        df["title"], df["summary"], df["symbol"], df["co_mention_count"], pub_tier
                    )
                ),
                dtype="float32",
                count=len(df),
            )
            arts = (
                {
                    "title": t,
                    "summary": s,
                    "publisher_tier": pt,
                    "relevance": r,
                    "n_duplicates": nd,
                    "co_mention_count": co,
                    "session_class": sc,
                    "day_of_week": dw,
                    "month": mo,
                    "spy_ret_1d": s1,
                    "spy_ret_5d": s5,
                    "spy_vol_20d": sv,
                    "tkr_ret_5d": t5,
                }
                for t, s, pt, r, nd, co, sc, dw, mo, s1, s5, sv, t5 in zip(
                    df["title"],
                    df["summary"],
                    pub_tier,
                    rel,
                    df["n_duplicates"],
                    df["co_mention_count"],
                    df["session_class"],
                    df["day_of_week"],
                    df["month"],
                    df["spy_ret_1d"],
                    df["spy_ret_5d"],
                    df["spy_vol_20d"],
                    df["tkr_ret_5d"],
                )
            )
            dense = np.asarray([mf.dense_vector(a) for a in arts], dtype="float32")
            np.nan_to_num(dense, copy=False)
            counts = mf.hash_counts(
                [mf.text_for_hashing(t, s) for t, s in zip(df["title"], df["summary"])]
            )

            sp.save_npz(out_dir / f"counts_{shard:04d}.npz", counts)
            np.save(out_dir / f"dense_{shard:04d}.npy", dense)
            df["relevance"] = rel
            df["publisher_tier"] = pub_tier
            meta_cols = [
                "symbol",
                "d0",
                "d1",
                "session_class",
                "sar",
                "abn",
                "r_cc_baseline",
                "group_weight",
                "group_size",
                "n_duplicates",
                "relevance",
                "publisher_tier",
                "dollar_vol",
                "year",
            ]
            df[meta_cols].to_parquet(out_dir / f"meta_{shard:04d}.parquet", index=False)
            n_rows += len(df)
            shard += 1
            if shard % 5 == 0:
                print(f"  {split}: shard {shard}, {n_rows:,} rows", flush=True)
        print(f"{split}: {shard} shards, {n_rows:,} rows", flush=True)


def build_idf() -> None:
    import numpy as np
    import scipy.sparse as sp

    from convexity import ml_features as mf

    train_dir = config.FEATURES_DIR / "train"
    mats = (sp.load_npz(p) for p in sorted(train_dir.glob("counts_*.npz")))
    idf = mf.fit_idf(mats)
    np.save(config.FEATURES_DIR / "idf.npy", idf)
    print(f"idf fitted: {int((idf > np.log(2)).sum()):,} buckets seen in <=50% of docs", flush=True)


def build_mask(k: int = 2**16) -> None:
    """Keep the top-k hash buckets by TRAIN document frequency.

    LightGBM's sparse multi-val bin construction cost scales with column
    count x bins; at 262k columns it dominated the entire FLAML run
    (verified by stack sampling — 16h of pure PushDataToMultiValBin). The
    top-64k columns carry almost all the nnz mass; the mask ships in the
    artifact so serve-time featurization stays byte-identical.
    """
    import numpy as np
    import scipy.sparse as sp

    train_dir = config.FEATURES_DIR / "train"
    df = np.zeros(2**18, dtype="int64")
    for p in sorted(train_dir.glob("counts_*.npz")):
        m = sp.load_npz(p)
        m.data = (m.data > 0).astype("float32")
        df += np.asarray(m.sum(axis=0)).ravel().astype("int64")
    mask = np.sort(np.argsort(df)[::-1][:k]).astype("int32")
    np.save(config.FEATURES_DIR / "col_mask.npy", mask)
    kept = df[mask].sum() / max(1, df.sum())
    print(
        f"column mask: kept {k:,}/{len(df):,} buckets covering "
        f"{100 * kept:.1f}% of doc-term incidences "
        f"(df cutoff ~{int(df[mask].min())})",
        flush=True,
    )


# =========================================================== v2: ticker-day
# The Market read v2 is trained per (ticker, as-of day) on ALL of a ticker's
# articles in its trailing window, so every article needs a text-encoder
# prediction — not just the 3M-row stratified sample the v1 shards hold.
#
#   articles  featurize every enriched row (same ml_features/relevance code as
#             serving) -> features/articles/{counts,dense,meta}_NNNN
#   encoder   per-article v1-config predictions, CROSS-FITTED: rows in year Y
#             are scored by a booster trained only on rows with d1 < Y. The
#             deployed v1 booster was trained on the very labels v2 learns
#             from (a dateonly article's window IS the as-of day's next-1d
#             return), so its in-sample predictions leak: train IC 0.058 vs
#             holdout 0.026. Holdout rows (>= TEST_START) use the deployed
#             booster, which never saw them. Also writes the deployed
#             booster's prediction for every row (v1.1 recalibration input).
#   panel     one row per (symbol, trading day D) with >= 1 article dated
#             D-6..D: ml_features.window_vector (the serving function) + the
#             point-in-time price context + next-1d/5d SAR labels.

ARTICLES_DIR = config.FEATURES_DIR / "articles"
ART_CHUNK = 100_000


def _articles_worker(args):
    """Featurize one ordered chunk; writes its own shard files."""
    import duckdb
    import numpy as np
    import scipy.sparse as sp

    from convexity import ml_features as mf
    from convexity.relevance import is_boilerplate, load_company_names, relevance_score

    shard, df = args
    names = load_company_names()
    pub_tier = df["publisher"].map(mf.publisher_tier).astype("float32")
    rel = np.fromiter(
        (
            relevance_score(t, s, sym, names.get(sym), int(co), float(pt))
            for t, s, sym, co, pt in zip(
                df["title"], df["summary"], df["symbol"], df["co_mention_count"], pub_tier
            )
        ),
        dtype="float32",
        count=len(df),
    )
    arts = (
        {
            "title": t,
            "summary": s,
            "publisher_tier": pt,
            "relevance": r,
            "n_duplicates": nd,
            "co_mention_count": co,
            "session_class": sc,
            "day_of_week": dw,
            "month": mo,
            "spy_ret_1d": s1,
            "spy_ret_5d": s5,
            "spy_vol_20d": sv,
            "tkr_ret_5d": t5,
        }
        for t, s, pt, r, nd, co, sc, dw, mo, s1, s5, sv, t5 in zip(
            df["title"],
            df["summary"],
            pub_tier,
            rel,
            df["n_duplicates"],
            df["co_mention_count"],
            df["session_class"],
            df["day_of_week"],
            df["month"],
            df["spy_ret_1d"],
            df["spy_ret_5d"],
            df["spy_vol_20d"],
            df["tkr_ret_5d"],
        )
    )
    dense = np.asarray([mf.dense_vector(a) for a in arts], dtype="float32")
    np.nan_to_num(dense, copy=False)
    counts = mf.hash_counts([mf.text_for_hashing(t, s) for t, s in zip(df["title"], df["summary"])])
    sp.save_npz(ARTICLES_DIR / f"counts_{shard:04d}.npz", counts)
    np.save(ARTICLES_DIR / f"dense_{shard:04d}.npy", dense)

    lm_col = mf.DENSE_COLUMNS.index("lm_score")
    miss_col = mf.DENSE_COLUMNS.index("lm_missing")
    unc_col = mf.DENSE_COLUMNS.index("uncertainty_ratio")
    meta = df[
        [
            "row_key",
            "symbol",
            "edate",
            "d1",
            "year",
            "session_class",
            "sar",
            "group_weight",
            "n_duplicates",
        ]
    ].copy()
    meta["publisher_tier"] = pub_tier.to_numpy()
    meta["relevance"] = rel
    meta["lm"] = np.where(dense[:, miss_col] > 0, np.nan, dense[:, lm_col])
    meta["unc"] = dense[:, unc_col]
    meta["boiler"] = np.fromiter(
        (is_boilerplate(t) for t in df["title"]), dtype="int8", count=len(df)
    )
    con = duckdb.connect()
    con.register("m", meta)
    con.execute(f"COPY m TO '{ARTICLES_DIR / f'meta_{shard:04d}.parquet'}' (FORMAT parquet)")
    return shard, len(df)


def build_articles() -> None:
    import multiprocessing as mp
    import shutil as _sh

    import duckdb

    if ARTICLES_DIR.exists():
        _sh.rmtree(ARTICLES_DIR)
    ARTICLES_DIR.mkdir(parents=True)
    con = duckdb.connect()
    # A global ORDER BY over the text columns OOMs on an 8 GB box; one year
    # (<= 754k rows) at a time keeps the sort small, and year = year(d1) so the
    # concatenated shards are still in d1 order.
    con.execute("PRAGMA memory_limit='2GB'")
    con.execute("PRAGMA threads=2")
    tmp = config.DATA_DIR / "duckdb_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"PRAGMA temp_directory='{tmp}'")
    years = [
        y
        for (y,) in con.execute(
            f"SELECT DISTINCT year FROM read_parquet('{ENRICHED_PARQUET}') ORDER BY 1"
        ).fetchall()
    ]
    n_proc = max(1, config.N_JOBS - 1)
    t0 = time.time()
    done = 0
    shard = 0
    with mp.get_context("spawn").Pool(n_proc) as pool:
        pending = []
        for year in years:
            res = con.execute(f"""
                SELECT row_key, symbol, title, summary, publisher,
                       CAST(ts_et AS DATE) AS edate,
                       d1, year, session_class, sar, group_weight, n_duplicates,
                       co_mention_count, day_of_week, month,
                       spy_ret_1d, spy_ret_5d, spy_vol_20d, tkr_ret_5d
                FROM read_parquet('{ENRICHED_PARQUET}')
                WHERE year = {year}
                ORDER BY d1, row_key
            """)
            while True:
                df = res.fetch_df_chunk(ART_CHUNK // 2048 + 1)
                if df is None or not len(df):
                    break
                pending.append(pool.apply_async(_articles_worker, ((shard, df),)))
                shard += 1
                # bounded in-flight queue: never hold more than 2 chunks per worker
                while len(pending) >= 2 * n_proc:
                    done += pending.pop(0).get()[1]
            print(
                f"  articles: year {year} queued ({shard} shards), {done:,} rows done "
                f"({(time.time() - t0) / 60:.1f} min)",
                flush=True,
            )
        for p in pending:
            done += p.get()[1]
    print(
        f"articles: {shard} shards, {done:,} rows in {(time.time() - t0) / 60:.1f} min", flush=True
    )


def _article_shards():
    return sorted(int(p.stem.split("_")[1]) for p in ARTICLES_DIR.glob("meta_*.parquet"))


def _read_meta(i: int, cols: str = "*"):
    import duckdb

    return (
        duckdb.connect()
        .execute(f"SELECT {cols} FROM read_parquet('{ARTICLES_DIR / f'meta_{i:04d}.parquet'}')")
        .df()
    )


def _shard_matrix(i: int, idf, mask, rows=None):
    """tfidf[:, mask] | dense — the exact matrix layout the v1 booster expects."""
    import numpy as np
    import scipy.sparse as sp

    from convexity import ml_features as mf

    counts = sp.load_npz(ARTICLES_DIR / f"counts_{i:04d}.npz")
    dense = np.load(ARTICLES_DIR / f"dense_{i:04d}.npy")
    if rows is not None:
        counts, dense = counts[rows], dense[rows]
    tfidf = mf.apply_idf(counts, idf)
    if mask is not None:
        tfidf = tfidf[:, mask]
    return sp.hstack([tfidf, sp.csr_matrix(dense)], format="csr", dtype=np.float32)


def build_encoder_preds() -> None:
    import lightgbm as lgb
    import numpy as np
    import scipy.sparse as sp

    art = config.ARTIFACTS_DIR / config.ARTIFACT_VERSION
    idf = np.load(art / "idf.npy")
    mask = np.load(art / "col_mask.npy")
    cfg = json.loads((art / "train_metrics.json").read_text())["best_config"]
    deployed = lgb.Booster(model_file=str(art / "model.lgbm.txt"))
    params = {k: v for k, v in cfg.items() if k not in ("n_estimators", "log_max_bin")}
    params.update(
        {
            "objective": "regression",
            "verbosity": -1,
            "seed": config.SEED,
            "num_threads": config.N_JOBS,
            "max_bin": 2 ** int(cfg["log_max_bin"]) - 1,
        }
    )
    n_trees = int(cfg["n_estimators"])

    shards = _article_shards()
    metas = {i: _read_meta(i, "d1, year") for i in shards}
    d1s = {i: np.asarray(m["d1"].astype("datetime64[ns]")) for i, m in metas.items()}
    xfit = {i: np.full(len(m), np.nan, dtype="float32") for i, m in metas.items()}
    t0 = time.time()

    # 1) deployed booster on every row (v1.1 calibration + holdout encoder)
    v1 = {}
    for i in shards:
        v1[i] = deployed.predict(_shard_matrix(i, idf, mask)).astype("float32")
    print(f"encoder: deployed preds for all rows ({(time.time() - t0) / 60:.1f} min)", flush=True)

    # 2) expanding-window cross-fits, one per calendar year (2023: H1 only)
    test_start = np.datetime64(config.TEST_START)
    rng = np.random.default_rng(config.SEED)
    for year in range(config.ENCODER_FIRST_YEAR, 2024):
        lo = np.datetime64(f"{year}-01-01")
        hi = min(np.datetime64(f"{year + 1}-01-01"), test_start)
        n_prefix = sum(int((d < lo).sum()) for d in d1s.values())
        p_keep = min(1.0, config.ENCODER_MAX_ROWS / max(1, n_prefix))
        Xs, ys, ws = [], [], []
        for i in shards:
            sel = np.flatnonzero(d1s[i] < lo)
            if not len(sel):
                continue
            sel = sel[rng.random(len(sel)) < p_keep]
            m = _read_meta(i, "sar, group_weight")
            Xs.append(_shard_matrix(i, idf, mask, sel))
            ys.append(m["sar"].to_numpy("float32")[sel])
            ws.append(m["group_weight"].to_numpy("float32")[sel])
        X = sp.vstack(Xs, format="csr")
        y, w = np.concatenate(ys), np.concatenate(ws)
        del Xs
        bst = lgb.train(
            params, lgb.Dataset(X, y, weight=w, free_raw_data=True), num_boost_round=n_trees
        )
        del X
        n_pred = 0
        for i in shards:
            sel = np.flatnonzero((d1s[i] >= lo) & (d1s[i] < hi))
            if len(sel):
                xfit[i][sel] = bst.predict(_shard_matrix(i, idf, mask, sel))
                n_pred += len(sel)
        print(
            f"  encoder {year}: trained on {len(y):,} rows (d1 < {lo}), "
            f"scored {n_pred:,} ({(time.time() - t0) / 60:.1f} min)",
            flush=True,
        )

    for i in shards:
        hold = d1s[i] >= test_start
        xfit[i][hold] = v1[i][hold]
        np.save(
            ARTICLES_DIR / f"pred_{i:04d}.npy", np.stack([xfit[i], v1[i]], axis=1).astype("float32")
        )
    print(f"encoder: done in {(time.time() - t0) / 60:.1f} min", flush=True)


PANEL_PARQUET = config.FEATURES_DIR / "panel.parquet"
# FNSPID is 98.5% date-only, so training rows need a proxy clock: an article
# dated E is placed at 12:00 ET on E and the as-of instant is 16:00 ET on D.
# The recency tau is 3 days, so the hour convention moves weights by <2%.
_NOON_ET_S = 17 * 3600
_CLOSE_ET_S = 21 * 3600
_LABEL_COLS = ["sar_1d", "sar_5d"]


def _panel_worker(batch):
    """batch: list of (symbol, art_cols dict of arrays, day_cols dict of arrays)."""
    import numpy as np

    from convexity import ml_features as mf
    from convexity.relevance import window_sample

    syms, nums = [], []
    for sym, A, Dd in batch:
        ed = A["eday"]
        arts = [
            {
                "sar_pred": float(p) if p == p else None,
                "sar_v1": float(v),
                "lm": float(lm) if lm == lm else None,
                "unc": float(u),
                "publisher_tier": float(pt),
                "n_duplicates": int(nd),
                "relevance": float(r),
                "boiler": bool(b),
                "datetime": float(e) * 86400.0 + _NOON_ET_S,
            }
            for p, v, lm, u, pt, nd, r, b, e in zip(
                A["pred"],
                A["v1"],
                A["lm"],
                A["unc"],
                A["publisher_tier"],
                A["n_duplicates"],
                A["relevance"],
                A["boiler"],
                ed,
            )
        ]
        days = Dd["dday"]
        lo_a = np.searchsorted(ed, days - (mf.WINDOW_DAYS - 1), "left")
        hi_a = np.searchsorted(ed, days, "right")
        base_a = np.searchsorted(ed, days - (mf.WINDOW_DAYS - 1 + mf.ATTN_BASE_DAYS), "left")
        for j in np.flatnonzero(hi_a > lo_a):
            lo, hi = int(lo_a[j]), int(hi_a[j])
            now = float(days[j]) * 86400.0 + _CLOSE_ET_S
            # newest-first, then the app's exact retention cap (parity)
            win = window_sample(arts[lo:hi][::-1], mf.WINDOW_CAP, mf.WINDOW_MIN_RECENT)
            price = {c: Dd[c][j] for c in mf.PRICE_COLUMNS}
            feats = mf.window_vector(
                win,
                now,
                (hi - lo) / mf.WINDOW_DAYS,
                (lo - int(base_a[j])) / mf.ATTN_BASE_DAYS,
                price,
            )
            v1_wmean, v1_wsum = mf.weighted_sar(win, now, key="sar_v1")
            syms.append(sym)
            nums.append(
                (
                    float(days[j]),
                    *feats,
                    math.nan if v1_wmean is None else v1_wmean,
                    v1_wsum,
                    hi - lo,
                    *[Dd[c][j] for c in _LABEL_COLS],
                )
            )
    # float64 so the day number (~19k) and tiny SAR predictions stay exact;
    # the parent narrows to float32 per column after assembly.
    return syms, np.asarray(nums, dtype="float64").reshape(len(nums), -1)


def build_panel() -> None:
    import multiprocessing as mp

    import duckdb
    import numpy as np
    import pandas as pd

    from convexity import ml_features as mf

    t0 = time.time()
    frames = []
    for i in _article_shards():
        m = _read_meta(i, "symbol, edate, publisher_tier, relevance, n_duplicates, lm, unc, boiler")
        pr = np.load(ARTICLES_DIR / f"pred_{i:04d}.npy")
        m["pred"], m["v1"] = pr[:, 0], pr[:, 1]
        frames.append(m)
    arts = pd.concat(frames, ignore_index=True)
    del frames
    arts["eday"] = arts["edate"].astype("datetime64[s]").astype("int64") // 86400
    arts = arts.drop(columns=["edate"]).sort_values(["symbol", "eday"], kind="stable")
    print(f"panel: {len(arts):,} articles loaded ({(time.time() - t0) / 60:.1f} min)", flush=True)

    con = duckdb.connect()
    td = con.execute(f"""
        SELECT symbol, date, {", ".join(_LABEL_COLS)}, {", ".join(mf.PRICE_COLUMNS)}
        FROM read_parquet('{config.PARQUET_DIR / "ticker_day.parquet"}')
        ORDER BY symbol, date
    """).df()
    td["dday"] = td["date"].astype("datetime64[s]").astype("int64") // 86400
    print(f"panel: {len(td):,} ticker-days loaded", flush=True)

    a_groups = dict(arts.groupby("symbol", sort=False))
    a_cols = [
        "eday",
        "pred",
        "v1",
        "lm",
        "unc",
        "publisher_tier",
        "n_duplicates",
        "relevance",
        "boiler",
    ]
    d_cols = ["dday", *_LABEL_COLS, *mf.PRICE_COLUMNS]
    items = []
    for sym, g in td.groupby("symbol", sort=False):
        ag = a_groups.get(sym)
        if ag is None:
            continue
        items.append(
            (sym, {c: ag[c].to_numpy() for c in a_cols}, {c: g[c].to_numpy() for c in d_cols})
        )
    del arts, a_groups, td
    batches = [items[k::64] for k in range(64)]
    cols = ["symbol", "dday", *mf.WINDOW_COLUMNS, "v1_wmean", "v1_wsum", "n_win", *_LABEL_COLS]
    sym_parts, num_parts = [], []
    with mp.get_context("spawn").Pool(max(1, config.N_JOBS - 1)) as pool:
        for k, (syms, nums) in enumerate(pool.imap_unordered(_panel_worker, batches)):
            if len(syms):
                sym_parts.append(np.asarray(syms, dtype=object))
                num_parts.append(nums.astype("float32"))
            if (k + 1) % 8 == 0:
                print(
                    f"  panel: {k + 1}/64 batches, "
                    f"{sum(len(x) for x in sym_parts):,} rows "
                    f"({(time.time() - t0) / 60:.1f} min)",
                    flush=True,
                )
    num = np.concatenate(num_parts)
    del num_parts
    panel = pd.DataFrame(num[:, 1:], columns=cols[2:])
    panel.insert(0, "dday", num[:, 0].astype("int64"))  # day numbers are exact in f32
    panel.insert(0, "symbol", np.concatenate(sym_parts))
    del num, sym_parts
    panel["date"] = pd.to_datetime(panel["dday"] * 86400, unit="s").dt.date
    panel = panel.drop(columns=["dday"]).sort_values(["date", "symbol"], kind="stable")
    for c in panel.columns:
        if panel[c].dtype == "float64":
            panel[c] = panel[c].astype("float32")
    con.register("p", panel)
    con.execute(f"COPY p TO '{PANEL_PARQUET}' (FORMAT parquet, COMPRESSION zstd)")
    print(
        f"panel: {len(panel):,} ticker-days, {panel['symbol'].nunique():,} symbols "
        f"({(time.time() - t0) / 60:.1f} min)",
        flush=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true")
    ap.add_argument(
        "--stage",
        choices=["enrich", "shards", "idf", "mask", "articles", "encoder", "panel"],
        default=None,
    )
    args = ap.parse_args()
    config.ensure_dirs()
    t0 = time.time()
    stages = [args.stage] if args.stage else ["enrich", "shards", "idf", "mask"]
    if "enrich" in stages:
        enrich()
    if "shards" in stages:
        build_shards(args.sample)
    if "idf" in stages:
        build_idf()
    if "mask" in stages:
        build_mask()
    if "articles" in stages:
        build_articles()
    if "encoder" in stages:
        build_encoder_preds()
    if "panel" in stages:
        build_panel()
    print(f"done in {(time.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
