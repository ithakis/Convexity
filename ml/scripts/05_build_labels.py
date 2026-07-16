"""Build SAR labels: vol-standardized, beta-adjusted, timing-aware abnormal returns.

Three stages, each checkpointed to parquet so a crash resumes cheaply:

A. ticker_daily_stats.parquet — per (symbol, date): adjusted open/close, window
   returns (r_cc/r_oc/r_co), trailing window-type sigmas (60 obs, min 40),
   rolling daily beta vs SPY (252 obs, min 120, Blume-shrunk, clipped [0,3]),
   trailing median dollar volume. Every trailing stat is shift(1)-ed: the value
   stored at date D uses ONLY data <= D-1 (point-in-time, no leakage).

B. labeled_pre.parquet — news rows session-classified and joined to windows:
     after-close (>=16:00 ET) or non-trading-day timed -> `overnight`
         close(D0) -> open(D1), sigma_co        (D0 = last trading day <= event date)
     pre-open (<09:30 ET, trading day)          -> `preopen_oc`
         open(D) -> close(D), sigma_oc
     intraday (09:30-16:00 ET, trading day)     -> `intraday_cc`
         close(D0=D) -> close(D1), sigma_cc     (starts 16:00 >= publication; captures
                                                  the after-hours + next-day reaction
                                                  without pre-publication contamination)
     date-only rows                             -> `dateonly_cc`
         close(D0) -> close(D1) — the only window guaranteed to start after any
         possible publication time on the article date (reverse-causality guard).
   Weekend/holiday events: D0 = previous trading day. The market is closed
   between D0's close and publication, so the window still starts at the last
   pre-publication price — contamination-free by construction.

   abn = r_win - beta * m_win     (same-window SPY return from calendar.parquet)
   SAR = abn / sigma_win_type     (training target, winsorized +/-5)
   r_cc_baseline = close(D0) -> close(D1) stored for every row (validation baseline).

C. labeled.parquet — within-group (symbol, D1, session_class) near-duplicate
   collapse (same normalization + rapidfuzz token_set_ratio >= 85 as the app's
   _dedup_articles), survivors carry n_duplicates; distinct survivors share the
   label with group_weight = 1/n_group (LightGBM sample_weight) so syndication
   can't manufacture training rows.

Usage:
    python ml/scripts/05_build_labels.py              # all stages
    python ml/scripts/05_build_labels.py --stage A    # one stage
    python ml/scripts/05_build_labels.py --sample     # cap news rows for smoke test
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ml import config  # noqa: E402

LABELED_PRE_PARQUET = config.PARQUET_DIR / "labeled_pre.parquet"
SYMBOL_BATCH = 300


# --------------------------------------------------------------------- stage A
def stage_a() -> None:
    import duckdb
    import numpy as np
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    t0 = time.time()
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='3GB'")
    cal = pd.read_parquet(config.CALENDAR_PARQUET)[["date", "m_cc"]]
    cal["date"] = pd.to_datetime(cal["date"])

    symbols = [s for (s,) in con.execute(
        f"SELECT DISTINCT symbol FROM read_parquet('{config.PRICES_PARQUET}') ORDER BY symbol"
    ).fetchall()]
    print(f"stage A: {len(symbols)} symbols", flush=True)

    schema = pa.schema([
        ("symbol", pa.string()), ("date", pa.date32()),
        ("adj_open", pa.float32()), ("adj_close", pa.float32()),
        ("sigma_cc", pa.float32()), ("sigma_oc", pa.float32()), ("sigma_co", pa.float32()),
        ("beta", pa.float32()), ("dollar_vol", pa.float32()),
    ])
    writer = pq.ParquetWriter(config.TICKER_STATS_PARQUET, schema, compression="zstd")
    w, mn = config.SIGMA_WINDOW, config.SIGMA_MIN_OBS
    bw, bmn = config.BETA_WINDOW, config.BETA_MIN_OBS
    b1, b0 = config.BETA_BLUME

    for i in range(0, len(symbols), SYMBOL_BATCH):
        batch = symbols[i:i + SYMBOL_BATCH]
        df = con.execute(f"""
            SELECT symbol, date, open, close, adj_close, volume
            FROM read_parquet('{config.PRICES_PARQUET}')
            WHERE symbol IN ({','.join('?' * len(batch))})
            ORDER BY symbol, date
        """, batch).df()
        df["date"] = pd.to_datetime(df["date"])
        df = df.merge(cal, on="date", how="left")
        out_frames = []
        for sym, g in df.groupby("symbol", sort=False):
            g = g.reset_index(drop=True)
            f = g["adj_close"] / g["close"].replace(0.0, np.nan)
            adj_open = g["open"] * f
            adj_close = g["adj_close"].astype("float64")
            r_cc = adj_close.pct_change()
            r_oc = adj_close / adj_open - 1.0
            r_co = adj_open / adj_close.shift(1) - 1.0
            sigma_cc = r_cc.rolling(w, min_periods=mn).std().shift(1)
            sigma_oc = r_oc.rolling(w, min_periods=mn).std().shift(1)
            sigma_co = r_co.rolling(w, min_periods=mn).std().shift(1)
            m = g["m_cc"].astype("float64")
            cov = r_cc.rolling(bw, min_periods=bmn).cov(m)
            var = m.rolling(bw, min_periods=bmn).var()
            beta_hat = (cov / var).shift(1)
            beta = (b1 * beta_hat + b0).clip(*config.BETA_CLIP)
            dollar_vol = (g["close"] * g["volume"]).rolling(w, min_periods=mn).median().shift(1)
            out_frames.append(pd.DataFrame({
                "symbol": sym, "date": g["date"].dt.date,
                "adj_open": adj_open.astype("float32"),
                "adj_close": adj_close.astype("float32"),
                "sigma_cc": sigma_cc.astype("float32"), "sigma_oc": sigma_oc.astype("float32"),
                "sigma_co": sigma_co.astype("float32"),
                "beta": beta.astype("float32"), "dollar_vol": dollar_vol.astype("float32"),
            }))
        chunk = pd.concat(out_frames, ignore_index=True)
        writer.write_table(pa.Table.from_pandas(chunk, schema=schema, preserve_index=False))
        print(f"  {i + len(batch)}/{len(symbols)} symbols", flush=True)
    writer.close()
    print(f"stage A done in {(time.time()-t0)/60:.1f} min", flush=True)


# --------------------------------------------------------------------- stage B
def stage_b(sample: bool) -> None:
    import duckdb

    t0 = time.time()
    audit = config.load_timestamp_audit()
    source_tz = audit["tz_inference"]["decision_source_tz"]
    print(f"stage B: source_tz={source_tz}", flush=True)

    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='3GB'")
    con.execute("PRAGMA threads=4")
    con.execute("SET TimeZone='America/New_York'")

    limit = "LIMIT 200000" if sample else ""
    # Local (ET) publication timestamp. Raw strings are naive; interpret them in
    # the audited source tz, then convert to America/New_York (DST-correct via ICU).
    if source_tz == "UTC":
        et_expr = "timezone('UTC', try_cast(raw_date AS TIMESTAMP))"
    else:
        et_expr = "try_cast(raw_date AS TIMESTAMP)"

    con.execute(f"""
        CREATE TEMP TABLE ev AS
        SELECT title, symbol, publisher, url, summary,
               ts_et,
               CAST(ts_et AS DATE) AS edate,
               (extract(hour FROM ts_et)=0 AND extract(minute FROM ts_et)=0
                AND extract(second FROM ts_et)=0)                    AS dateonly,
               extract(hour FROM ts_et) * 60 + extract(minute FROM ts_et) AS et_min
        FROM (
            SELECT *, {et_expr} AS ts_et
            FROM read_parquet('{config.NEWS_PARQUET}/**/*.parquet')
            {limit}
        )
        WHERE ts_et IS NOT NULL
    """)

    # Trading-day anchors: D_last = last trading day <= event date (ASOF),
    # cal row also tells us whether edate itself is a trading day.
    con.execute(f"""
        CREATE TEMP TABLE cal AS
        SELECT date, next_date, m_overnight, m_intraday, m_cc
        FROM read_parquet('{config.CALENDAR_PARQUET}')
    """)
    con.execute("""
        CREATE TEMP TABLE ev2 AS
        SELECT ev.*, cal.date AS d_last, cal.next_date AS d_last_next,
               (cal.date = ev.edate) AS is_trading_day
        FROM ev ASOF JOIN cal ON ev.edate >= cal.date
    """)

    # Session classification -> (session_class, D0, D1).
    # For preopen_oc the window lives entirely on D0=D1=edate.
    con.execute("""
        CREATE TEMP TABLE ev3 AS
        SELECT *,
            CASE
                WHEN dateonly THEN 'dateonly_cc'
                WHEN NOT is_trading_day THEN 'overnight'
                WHEN et_min >= 960 THEN 'overnight'      -- >= 16:00
                WHEN et_min < 570 THEN 'preopen_oc'      -- < 09:30
                ELSE 'intraday_cc'
            END AS session_class,
            d_last AS d0,
            CASE
                WHEN (NOT dateonly) AND is_trading_day AND et_min < 570 AND et_min > 0
                    THEN d_last            -- preopen: window is open(D)->close(D)
                ELSE d_last_next
            END AS d1
        FROM ev2
    """)

    # Join per-ticker stats at D0 (window-start prices) and D1 (end prices +
    # point-in-time sigma/beta as of the window's own day), market returns at D1.
    stats = str(config.TICKER_STATS_PARQUET)
    con.execute(f"""
        COPY (
            SELECT
                e.symbol, e.title, e.publisher, e.url, e.summary,
                CAST(e.ts_et AS TIMESTAMP) AS ts_et,
                e.session_class, e.d0, e.d1,
                s1.beta, s1.dollar_vol,
                CASE e.session_class
                    WHEN 'overnight'   THEN s1.adj_open / s0.adj_close - 1.0
                    WHEN 'preopen_oc'  THEN s1.adj_close / s1.adj_open - 1.0
                    ELSE                    s1.adj_close / s0.adj_close - 1.0
                END AS r_win,
                CASE e.session_class
                    WHEN 'overnight'   THEN c1.m_overnight
                    WHEN 'preopen_oc'  THEN c1.m_intraday
                    ELSE                    c1.m_cc
                END AS m_win,
                CASE e.session_class
                    WHEN 'overnight'   THEN s1.sigma_co
                    WHEN 'preopen_oc'  THEN s1.sigma_oc
                    ELSE                    s1.sigma_cc
                END AS sigma,
                s1.adj_close / s0.adj_close - 1.0 AS r_cc_baseline
            FROM ev3 e
            JOIN read_parquet('{stats}') s0 ON s0.symbol = e.symbol AND s0.date = e.d0
            JOIN read_parquet('{stats}') s1 ON s1.symbol = e.symbol AND s1.date = e.d1
            JOIN cal c1 ON c1.date = e.d1
        ) TO '{LABELED_PRE_PARQUET}' (FORMAT parquet, COMPRESSION zstd)
    """)

    n_ev, n_out = con.execute(f"""
        SELECT (SELECT count(*) FROM ev),
               (SELECT count(*) FROM read_parquet('{LABELED_PRE_PARQUET}'))
    """).fetchone()
    by_class = dict(con.execute(f"""
        SELECT session_class, count(*) FROM read_parquet('{LABELED_PRE_PARQUET}')
        GROUP BY session_class
    """).fetchall())
    report = {"events_in": n_ev, "rows_out": n_out,
              "joined_frac": round(n_out / max(1, n_ev), 4),
              "by_session_class": by_class,
              "minutes": round((time.time() - t0) / 60, 1)}
    (config.REPORTS_DIR / "labels_stage_b_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)


# --------------------------------------------------------------------- stage C
def stage_c() -> None:
    import duckdb
    import numpy as np
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    # Shared dedup logic with the app (same normalization + threshold as
    # news_sentiment._dedup_articles, exported via portfolio_tracker.relevance).
    from portfolio_tracker.relevance import cluster_titles

    t0 = time.time()
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='3GB'")
    # In-memory DuckDB cannot spill without an explicit temp dir; the ORDER BY
    # over 4.7M text-heavy rows needs out-of-core sorting on an 8 GB machine.
    tmp = config.DATA_DIR / "duckdb_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"PRAGMA temp_directory='{tmp}'")
    con.execute("SET preserve_insertion_order=false")

    schema = pa.schema([
        ("symbol", pa.string()), ("title", pa.string()), ("publisher", pa.string()),
        ("url", pa.string()), ("summary", pa.string()), ("ts_et", pa.timestamp("us")),
        ("session_class", pa.string()), ("d0", pa.date32()), ("d1", pa.date32()),
        ("beta", pa.float32()), ("dollar_vol", pa.float32()),
        ("r_win", pa.float32()), ("m_win", pa.float32()), ("sigma", pa.float32()),
        ("abn", pa.float32()), ("sar", pa.float32()), ("sar_raw", pa.float32()),
        ("r_cc_baseline", pa.float32()),
        ("n_duplicates", pa.int32()), ("group_size", pa.int32()),
        ("group_weight", pa.float32()),
    ])
    writer = pq.ParquetWriter(config.LABELED_PARQUET, schema, compression="zstd")

    # Group keys include d1, so a group never spans calendar years — process
    # one year at a time (2023 peak ~950k rows fits in RAM). Dedup is
    # vectorized: singleton groups (the overwhelming majority) bypass Python
    # entirely; only multi-article groups run the rapidfuzz clustering, driven
    # by numpy index arrays instead of per-group DataFrames (the per-group
    # pd.concat version of this loop got OOM-killed on an 8 GB machine).
    years = [y for (y,) in con.execute(
        f"SELECT DISTINCT year(d1) FROM read_parquet('{LABELED_PRE_PARQUET}') ORDER BY 1"
    ).fetchall()]

    n_in = n_kept = 0
    for y in years:
        df = con.execute(f"""
            SELECT * FROM read_parquet('{LABELED_PRE_PARQUET}')
            WHERE year(d1) = {y}
              AND sigma IS NOT NULL AND sigma > 0 AND beta IS NOT NULL
              AND r_win IS NOT NULL AND m_win IS NOT NULL
              AND isfinite(r_win) AND isfinite(m_win)
        """).df()
        if not len(df):
            continue
        n_in += len(df)
        grouped = df.groupby(["symbol", "d1", "session_class"], sort=False,
                             observed=True)
        gid = grouped.ngroup().to_numpy()
        counts = np.bincount(gid)

        keep = np.ones(len(df), dtype=bool)
        ndup = np.zeros(len(df), dtype="int32")
        titles = df["title"].to_numpy()
        for _, idx in grouped.indices.items():
            if len(idx) < 2:
                continue
            keep_local, dups = cluster_titles([titles[i] for i in idx])
            keep[idx] = False
            kept_idx = np.asarray(idx)[keep_local]
            keep[kept_idx] = True
            ndup[kept_idx] = dups

        gsize = np.bincount(gid[keep], minlength=len(counts))  # post-dedup sizes
        out = df[keep].copy()
        out["n_duplicates"] = ndup[keep]
        out["group_size"] = gsize[gid[keep]].astype("int32")
        out["group_weight"] = (1.0 / gsize[gid[keep]]).astype("float32")
        out["abn"] = (out["r_win"] - out["beta"] * out["m_win"]).astype("float32")
        sar_raw = (out["abn"] / out["sigma"]).astype("float32")
        out["sar_raw"] = sar_raw
        out["sar"] = sar_raw.clip(-config.SAR_WINSOR, config.SAR_WINSOR)
        out = out.sort_values(["symbol", "d1", "session_class"], kind="stable")
        n_kept += len(out)
        writer.write_table(pa.Table.from_pandas(out[[f.name for f in schema]],
                                                schema=schema, preserve_index=False))
        del df, out, gid, keep, ndup
        print(f"  year {y} done ({n_kept:,} kept so far)", flush=True)
    writer.close()

    stats = con.execute(f"""
        SELECT count(*), avg(sar), stddev(sar), median(sar),
               quantile_cont(sar, 0.01), quantile_cont(sar, 0.99),
               min(d1), max(d1)
        FROM read_parquet('{config.LABELED_PARQUET}')
    """).fetchone()
    report = {
        "rows_in_after_filters": n_in, "rows_kept": n_kept,
        "dedup_removed_frac": round(1 - n_kept / max(1, n_in), 4),
        "sar_mean": round(float(stats[1]), 4), "sar_std": round(float(stats[2]), 4),
        "sar_median": round(float(stats[3]), 4),
        "sar_p01": round(float(stats[4]), 3), "sar_p99": round(float(stats[5]), 3),
        "d1_min": str(stats[6]), "d1_max": str(stats[7]),
        "minutes": round((time.time() - t0) / 60, 1),
    }
    (config.REPORTS_DIR / "labels_stage_c_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["A", "B", "C"], default=None)
    ap.add_argument("--sample", action="store_true")
    args = ap.parse_args()
    config.ensure_dirs()
    stages = [args.stage] if args.stage else ["A", "B", "C"]
    if "A" in stages:
        stage_a()
    if "B" in stages:
        stage_b(args.sample)
    if "C" in stages:
        stage_c()


if __name__ == "__main__":
    main()
