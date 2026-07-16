"""Shadow comparison: ML signal vs LLM signal on the LIVE app's history.

FNSPID ends Dec 2023, so there is no historical overlap with the live LLM
pipeline — the comparison has to run forward. Once the mlsent artifact is
deployed, every get_news_sentiment() call appends ml_sar/ml_tier to
.portfolio_tracker_sentiment_history.json alongside the LLM fields. This
script reads that history and computes, for BOTH signals, the Spearman rank
IC against forward 1d/5d idiosyncratic returns using the same _fwd_idio
convention as /api/news-diagnostics.

Run it after 2-4 weeks of shadow accumulation (the plan's test #9 gate:
ML IC within noise of, or above, LLM IC before ml_tier is surfaced in UI).

Usage:
    <QF12 python> ml/scripts/11_shadow_compare_llm.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ml import config  # noqa: E402


def main() -> None:
    import numpy as np

    from portfolio_tracker import news_sentiment as ns

    records = [r for r in ns._history_load()
               if r.get("symbol") and r["symbol"] != "__market__"]
    with_ml = [r for r in records if r.get("ml_sar") is not None]
    print(f"history records: {len(records)}, with ml_sar: {len(with_ml)}", flush=True)
    if len(with_ml) < 30:
        print("Not enough ML shadow records yet — keep accumulating "
              "(refresh the News tab daily; each symbol/day adds one).", flush=True)
        return

    from portfolio_tracker.analytics import _bulk_close

    symbols = sorted({r["symbol"] for r in with_ml})
    closes = _bulk_close(symbols + ["SPY"], "1Y")

    def fwd_idio(rec, horizon):
        s = closes.get(rec["symbol"])
        spy = closes.get("SPY")
        if s is None or spy is None or s.empty or spy.empty:
            return None
        try:
            ts = np.datetime64(rec["date"])
            pos = s.index.searchsorted(ts, side="right") - 1
            if pos < 0 or pos + horizon >= len(s):
                return None
            r_i = float(s.iloc[pos + horizon] / s.iloc[pos] - 1.0)
            posm = spy.index.searchsorted(ts, side="right") - 1
            if posm < 0 or posm + horizon >= len(spy):
                return None
            r_m = float(spy.iloc[posm + horizon] / spy.iloc[posm] - 1.0)
            beta = rec.get("beta") or 1.0
            return r_i - beta * r_m
        except Exception:
            return None

    def spearman(x, y):
        x, y = np.asarray(x, float), np.asarray(y, float)
        if len(x) < 5 or x.std() == 0 or y.std() == 0:
            return None
        rx = np.argsort(np.argsort(x)).astype(float)
        ry = np.argsort(np.argsort(y)).astype(float)
        return float(np.corrcoef(rx, ry)[0, 1])

    report = {"n_ml_records": len(with_ml)}
    for horizon in (1, 5):
        rows = [(r.get("s_total"), r.get("ml_sar"), fwd_idio(r, horizon))
                for r in with_ml]
        rows = [(a, b, f) for a, b, f in rows
                if a is not None and b is not None and f is not None]
        if len(rows) < 10:
            report[f"{horizon}d"] = {"n": len(rows), "note": "too few matured records"}
            continue
        llm, ml, fwd = zip(*rows)
        report[f"{horizon}d"] = {
            "n": len(rows),
            "llm_ic": round(spearman(llm, fwd) or float("nan"), 4),
            "ml_ic": round(spearman(ml, fwd) or float("nan"), 4),
        }
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.REPORTS_DIR / "shadow_compare_report.json").write_text(
        json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
