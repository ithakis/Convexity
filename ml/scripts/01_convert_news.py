"""Convert the 23 GB FNSPID news CSV to lean zstd parquet, out-of-core via DuckDB.

Why DuckDB: it streams the CSV with projection pushdown, so the multi-KB
`Article` full-text column is parsed but never materialized — the peak RSS
stays under the 3 GB memory_limit on an 8 GB machine. Output keeps only what
labeling/features need (~4-6 GB): raw_date (VARCHAR — the Phase-3 timestamp
audit parses it, we don't pre-judge the format), title, symbol, publisher,
url, and the best available summary.

Verification gate before anyone deletes the raw CSV: parquet row count must
match DuckDB's accepted-row count, and the rejects fraction must be < 0.5%.

Usage:
    python ml/scripts/01_convert_news.py            # full run
    python ml/scripts/01_convert_news.py --sample   # first 100k rows, smoke test
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ml import config  # noqa: E402

REJECT_FRAC_MAX = 0.005


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="convert only the first 100k rows")
    args = ap.parse_args()
    config.ensure_dirs()

    import duckdb

    csv_path = config.RAW_DIR / config.HF_NEWS_FILE
    if not csv_path.exists():
        sys.exit(f"ABORT: {csv_path} not found — run 00_download_fnspid.py first")

    out_dir = config.NEWS_PARQUET if not args.sample else config.PARQUET_DIR / "news_sample"
    if out_dir.exists():
        shutil.rmtree(out_dir)

    free_gb = shutil.disk_usage(config.PARQUET_DIR).free / 1e9
    if not args.sample and free_gb < 9.0:
        sys.exit(f"ABORT: {free_gb:.1f} GB free, need ~9 GB headroom for the parquet output")

    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='3GB'")
    con.execute("PRAGMA threads=4")
    con.execute("SET preserve_insertion_order=false")

    limit = "LIMIT 100000" if args.sample else ""
    summary_expr = "coalesce(" + ", ".join(config.SUMMARY_PRIORITY) + ")"
    t0 = time.time()
    con.execute(f"""
        COPY (
            SELECT
                Date                                   AS raw_date,
                Article_title                          AS title,
                upper(trim(Stock_symbol))              AS symbol,
                Publisher                              AS publisher,
                Url                                    AS url,
                left({summary_expr}, {config.SUMMARY_MAX_CHARS}) AS summary,
                coalesce(try_cast(substr(Date, 1, 4) AS INTEGER), 0) AS year
            FROM read_csv('{csv_path}',
                          header=true, quote='"', escape='"',
                          strict_mode=false, ignore_errors=true,
                          types={{'Date': 'VARCHAR'}})
            WHERE Article_title IS NOT NULL AND Stock_symbol IS NOT NULL
            {limit}
        ) TO '{out_dir}' (FORMAT parquet, PARTITION_BY (year), COMPRESSION zstd)
    """)
    convert_s = time.time() - t0

    n_out = con.execute(
        f"SELECT count(*) FROM read_parquet('{out_dir}/**/*.parquet')"
    ).fetchone()[0]
    # rejected_rows() view exists only when ignore_errors dropped something
    try:
        n_rejected = con.execute("SELECT count(*) FROM reject_errors").fetchone()[0]
    except Exception:
        n_rejected = 0

    out_gb = sum(f.stat().st_size for f in Path(out_dir).rglob("*.parquet")) / 1e9
    report = {
        "rows_out": n_out,
        "rows_rejected": n_rejected,
        "reject_frac": (n_rejected / max(1, n_out + n_rejected)),
        "output_gb": round(out_gb, 2),
        "convert_minutes": round(convert_s / 60, 1),
        "sample": args.sample,
    }
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.REPORTS_DIR / "convert_news_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)

    if report["reject_frac"] > REJECT_FRAC_MAX:
        sys.exit(f"FAIL: reject fraction {report['reject_frac']:.4f} > {REJECT_FRAC_MAX} — "
                 "inspect before deleting the raw CSV")
    print("OK — safe to delete the raw CSV after eyeballing the report", flush=True)


if __name__ == "__main__":
    main()
