"""Convert FNSPID full_history.zip (one OHLCV CSV per ticker) to one sorted parquet.

Streams zip members one at a time via zipfile + pyarrow.csv — the zip is never
extracted to disk and no more than one ticker's history is in memory at once.
Ticker symbol comes from the member filename (AAPL.csv -> AAPL). A final DuckDB
pass sorts by (symbol, date) so downstream rolling-window code can scan
contiguous per-ticker blocks.

Usage:
    python ml/scripts/02_convert_prices.py            # full run
    python ml/scripts/02_convert_prices.py --sample   # first 50 tickers
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ml import config  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="convert only the first 50 tickers")
    args = ap.parse_args()
    config.ensure_dirs()

    import duckdb
    import pyarrow as pa
    import pyarrow.csv as pacsv
    import pyarrow.parquet as pq

    zip_path = config.RAW_DIR / config.HF_PRICE_FILE
    if not zip_path.exists():
        sys.exit(f"ABORT: {zip_path} not found — run 00_download_fnspid.py --prices first")

    out_path = config.PRICES_PARQUET if not args.sample else config.PARQUET_DIR / "prices_sample.parquet"
    tmp_path = out_path.with_suffix(".unsorted.parquet")

    schema = pa.schema([
        ("symbol", pa.string()),
        ("date", pa.date32()),
        ("open", pa.float32()),
        ("high", pa.float32()),
        ("low", pa.float32()),
        ("close", pa.float32()),
        ("adj_close", pa.float32()),
        ("volume", pa.int64()),
    ])
    # FNSPID per-ticker CSVs use yfinance column names; map case-insensitively.
    col_map = {"date": "date", "open": "open", "high": "high", "low": "low",
               "close": "close", "adj close": "adj_close", "adj_close": "adj_close",
               "adjclose": "adj_close", "volume": "volume"}

    t0 = time.time()
    n_tickers = n_rows = n_skipped = 0
    writer = pq.ParquetWriter(tmp_path, schema, compression="zstd")
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.namelist() if m.lower().endswith(".csv")]
        if args.sample:
            members = members[:50]
        for m in members:
            sym = Path(m).stem.upper().strip()
            if not sym:
                n_skipped += 1
                continue
            try:
                raw = zf.read(m)
                tbl = pacsv.read_csv(io.BytesIO(raw))
            except Exception:
                n_skipped += 1
                continue
            names = {c.lower(): c for c in tbl.column_names}
            if "date" not in names or "close" not in names:
                n_skipped += 1
                continue
            arrays, ok = [], True
            for field in schema:
                if field.name == "symbol":
                    arrays.append(pa.array([sym] * len(tbl), pa.string()))
                    continue
                src = next((names[k] for k, v in col_map.items() if v == field.name and k in names), None)
                if src is None:
                    # adj_close missing -> fall back to close; anything else missing -> skip file
                    if field.name == "adj_close" and "close" in names:
                        src = names["close"]
                    else:
                        ok = False
                        break
                try:
                    arrays.append(tbl.column(src).cast(field.type))
                except Exception:
                    ok = False
                    break
            if not ok:
                n_skipped += 1
                continue
            writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
            n_tickers += 1
            n_rows += len(tbl)
    writer.close()

    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='3GB'")
    con.execute(f"""
        COPY (SELECT * FROM read_parquet('{tmp_path}') ORDER BY symbol, date)
        TO '{out_path}' (FORMAT parquet, COMPRESSION zstd)
    """)
    tmp_path.unlink()

    n_out = con.execute(f"SELECT count(*) FROM read_parquet('{out_path}')").fetchone()[0]
    has_spy = con.execute(
        f"SELECT count(*) FROM read_parquet('{out_path}') WHERE symbol='SPY'"
    ).fetchone()[0]
    report = {
        "tickers": n_tickers,
        "rows": n_out,
        "rows_written": n_rows,
        "skipped_members": n_skipped,
        "spy_rows": has_spy,
        "output_gb": round(out_path.stat().st_size / 1e9, 2),
        "minutes": round((time.time() - t0) / 60, 1),
        "sample": args.sample,
    }
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.REPORTS_DIR / "convert_prices_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)
    if n_out != n_rows:
        sys.exit("FAIL: sorted row count != written row count")
    print("OK — safe to delete the raw zip after eyeballing the report", flush=True)


if __name__ == "__main__":
    main()
