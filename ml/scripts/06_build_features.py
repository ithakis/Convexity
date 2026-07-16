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
   (portfolio_tracker.relevance), dense block + hashed term counts
   (portfolio_tracker.ml_features), saved as counts_*.npz / dense_*.npy /
   meta_*.parquet under ml/data/features/{train,test}/.
4. idf fitted on TRAIN shards only -> features/idf.npy (ships in the artifact).

Usage:
    python ml/scripts/06_build_features.py            # full run
    python ml/scripts/06_build_features.py --sample   # 200k-row smoke test
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
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

    from portfolio_tracker import ml_features as mf
    from portfolio_tracker.relevance import load_company_names, relevance_score

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
        quotas[(int(r["year"]), r["session_class"])] = min(int(r["n"]),
                                                           max(prop, min(int(r["n"]), STRATUM_FLOOR if not sample else prop)))
    qdf = pd.DataFrame([(y, s, q) for (y, s), q in quotas.items()],
                       columns=["year", "session_class", "quota"])
    con.register("quotas", qdf)
    print(f"train pool {total:,} -> budget {budget:,} "
          f"(quota sum {int(qdf['quota'].sum()):,})", flush=True)

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
                (relevance_score(t, s, sym, names.get(sym), int(co), float(pt))
                 for t, s, sym, co, pt in zip(df["title"], df["summary"], df["symbol"],
                                              df["co_mention_count"], pub_tier)),
                dtype="float32", count=len(df))
            arts = ({"title": t, "summary": s, "publisher_tier": pt, "relevance": r,
                     "n_duplicates": nd, "co_mention_count": co, "session_class": sc,
                     "day_of_week": dw, "month": mo, "spy_ret_1d": s1, "spy_ret_5d": s5,
                     "spy_vol_20d": sv, "tkr_ret_5d": t5}
                    for t, s, pt, r, nd, co, sc, dw, mo, s1, s5, sv, t5 in zip(
                        df["title"], df["summary"], pub_tier, rel, df["n_duplicates"],
                        df["co_mention_count"], df["session_class"], df["day_of_week"],
                        df["month"], df["spy_ret_1d"], df["spy_ret_5d"],
                        df["spy_vol_20d"], df["tkr_ret_5d"]))
            dense = np.asarray([mf.dense_vector(a) for a in arts], dtype="float32")
            np.nan_to_num(dense, copy=False)
            counts = mf.hash_counts(
                [mf.text_for_hashing(t, s) for t, s in zip(df["title"], df["summary"])])

            sp.save_npz(out_dir / f"counts_{shard:04d}.npz", counts)
            np.save(out_dir / f"dense_{shard:04d}.npy", dense)
            df["relevance"] = rel
            df["publisher_tier"] = pub_tier
            meta_cols = ["symbol", "d0", "d1", "session_class", "sar", "abn",
                         "r_cc_baseline", "group_weight", "group_size", "n_duplicates",
                         "relevance", "publisher_tier", "dollar_vol", "year"]
            df[meta_cols].to_parquet(out_dir / f"meta_{shard:04d}.parquet", index=False)
            n_rows += len(df)
            shard += 1
            if shard % 5 == 0:
                print(f"  {split}: shard {shard}, {n_rows:,} rows", flush=True)
        print(f"{split}: {shard} shards, {n_rows:,} rows", flush=True)


def build_idf() -> None:
    import numpy as np
    import scipy.sparse as sp

    from portfolio_tracker import ml_features as mf

    train_dir = config.FEATURES_DIR / "train"
    mats = (sp.load_npz(p) for p in sorted(train_dir.glob("counts_*.npz")))
    idf = mf.fit_idf(mats)
    np.save(config.FEATURES_DIR / "idf.npy", idf)
    print(f"idf fitted: {int((idf > np.log(2)).sum()):,} buckets seen in <=50% of docs",
          flush=True)


def build_mask(k: int = 2 ** 16) -> None:
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
    df = np.zeros(2 ** 18, dtype="int64")
    for p in sorted(train_dir.glob("counts_*.npz")):
        m = sp.load_npz(p)
        m.data = (m.data > 0).astype("float32")
        df += np.asarray(m.sum(axis=0)).ravel().astype("int64")
    mask = np.sort(np.argsort(df)[::-1][:k]).astype("int32")
    np.save(config.FEATURES_DIR / "col_mask.npy", mask)
    kept = df[mask].sum() / max(1, df.sum())
    print(f"column mask: kept {k:,}/{len(df):,} buckets covering "
          f"{100*kept:.1f}% of doc-term incidences "
          f"(df cutoff ~{int(df[mask].min())})", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--stage", choices=["enrich", "shards", "idf", "mask"], default=None)
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
    print(f"done in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
