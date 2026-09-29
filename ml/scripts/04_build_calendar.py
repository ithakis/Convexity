"""Build the trading calendar + market (SPY) window returns.

The calendar is simply SPY's trading dates from the FNSPID prices parquet
(SPY history is long and gap-free; no exchange-calendar dependency needed).
If SPY is absent from FNSPID, falls back to one cached yfinance download.

Output calendar.parquet, one row per trading date D:
    date, next_date, prev_date,
    m_overnight = spy_open(D)/spy_close(D-1) - 1     # close(D-1) -> open(D)
    m_intraday  = spy_close(D)/spy_open(D)  - 1      # open(D)    -> close(D)
    m_cc        = spy_close(D)/spy_close(D-1) - 1    # close(D-1) -> close(D)

Adjusted prices are used (adj factor applied to open as adj_close/close) so
window returns are dividend/split-consistent with the per-ticker label math.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ml import config  # noqa: E402


def _spy_frame():
    """Return a pandas frame (date, open, close) of adjusted SPY prices."""
    import duckdb

    con = duckdb.connect()
    if config.PRICES_PARQUET.exists():
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{config.PRICES_PARQUET}') WHERE symbol='SPY'"
        ).fetchone()[0]
        if n > 1000:
            print(f"SPY from FNSPID prices: {n} rows", flush=True)
            return con.execute(f"""
                SELECT date,
                       open * (adj_close / nullif(close, 0)) AS open,
                       adj_close AS close
                FROM read_parquet('{config.PRICES_PARQUET}')
                WHERE symbol='SPY' ORDER BY date
            """).df()
    if config.SPY_PARQUET.exists():
        print("SPY from cached yfinance parquet", flush=True)
        return con.execute(f"SELECT * FROM read_parquet('{config.SPY_PARQUET}') ORDER BY date").df()
    print("SPY missing from FNSPID — one-time yfinance download", flush=True)
    import yfinance as yf

    df = yf.download("SPY", period="max", auto_adjust=True, progress=False)
    df = df[["Open", "Close"]].reset_index()
    df.columns = ["date", "open", "close"]
    df["date"] = df["date"].dt.date
    df.to_parquet(config.SPY_PARQUET, index=False)
    return df


def main() -> None:
    import numpy as np
    import pandas as pd

    config.ensure_dirs()
    spy = _spy_frame()
    spy = spy.dropna().reset_index(drop=True)

    dates = pd.to_datetime(spy["date"])
    o = spy["open"].astype("float64").to_numpy()
    c = spy["close"].astype("float64").to_numpy()

    cal = pd.DataFrame(
        {
            "date": dates.dt.date,
            "next_date": dates.shift(-1).dt.date,
            "prev_date": dates.shift(1).dt.date,
            "m_overnight": np.concatenate([[np.nan], o[1:] / c[:-1] - 1.0]),
            "m_intraday": c / o - 1.0,
            "m_cc": np.concatenate([[np.nan], c[1:] / c[:-1] - 1.0]),
        }
    )
    cal.to_parquet(config.CALENDAR_PARQUET, index=False)
    report = {
        "rows": len(cal),
        "date_min": str(cal["date"].iloc[0]),
        "date_max": str(cal["date"].iloc[-1]),
        "m_cc_ann_vol_pct": round(float(np.nanstd(cal["m_cc"]) * np.sqrt(252) * 100), 2),
    }
    (config.REPORTS_DIR / "calendar_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
