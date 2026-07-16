"""Audit FNSPID news timestamps — this report GATES the label design.

Answers three questions the label builder must not guess at:
1. What fraction of rows are date-only (midnight / no time component), overall
   and by year and publisher? Date-only rows get the conservative close(D) ->
   close(D+1) window; timed rows get session-classified windows.
2. What timezone are the timed rows in? Inferred from the hour-of-day activity
   histogram: US financial news volume peaks during ET business hours, so a
   modal band around 13-21 implies UTC, around 8-17 implies ET-naive.
3. What fraction of news symbols have no price history in the prices parquet?

Writes ml/data/reports/timestamp_audit.json; downstream scripts read the
decisions (source_tz, dateonly_frac) from there via config.load_timestamp_audit().
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ml import config  # noqa: E402


def main() -> None:
    import duckdb

    news_glob = f"{config.NEWS_PARQUET}/**/*.parquet"
    con = duckdb.connect()
    con.execute("PRAGMA memory_limit='3GB'")
    con.execute("PRAGMA threads=4")

    con.execute(f"""
        CREATE VIEW news AS
        SELECT raw_date, title, symbol, publisher, year,
               try_cast(raw_date AS TIMESTAMP) AS ts
        FROM read_parquet('{news_glob}')
    """)

    total, unparseable = con.execute("""
        SELECT count(*), count(*) FILTER (WHERE ts IS NULL) FROM news
    """).fetchone()

    # date-only := parses to exactly midnight (00:00:00). A tiny number of true
    # midnight-published articles will be miscounted — acceptable noise.
    dateonly = con.execute("""
        SELECT count(*) FROM news
        WHERE ts IS NOT NULL AND extract(hour FROM ts)=0
          AND extract(minute FROM ts)=0 AND extract(second FROM ts)=0
    """).fetchone()[0]

    by_year = {str(y): {"n": n, "dateonly_frac": round(d / max(1, n), 4)}
               for y, n, d in con.execute("""
        SELECT year, count(*),
               count(*) FILTER (WHERE extract(hour FROM ts)=0 AND
                                extract(minute FROM ts)=0 AND extract(second FROM ts)=0)
        FROM news WHERE ts IS NOT NULL GROUP BY year ORDER BY year
    """).fetchall()}

    by_publisher = {p or "?": {"n": n, "dateonly_frac": round(d / max(1, n), 4)}
                    for p, n, d in con.execute("""
        SELECT publisher, count(*),
               count(*) FILTER (WHERE extract(hour FROM ts)=0 AND
                                extract(minute FROM ts)=0 AND extract(second FROM ts)=0)
        FROM news WHERE ts IS NOT NULL
        GROUP BY publisher ORDER BY count(*) DESC LIMIT 25
    """).fetchall()}

    hour_hist = {int(h): int(n) for h, n in con.execute("""
        SELECT extract(hour FROM ts) AS h, count(*) FROM news
        WHERE ts IS NOT NULL AND NOT (extract(hour FROM ts)=0 AND
              extract(minute FROM ts)=0 AND extract(second FROM ts)=0)
        GROUP BY h ORDER BY h
    """).fetchall()}

    # Timezone inference: compare mass in the UTC-implied band (13-21) vs the
    # ET-naive-implied band (8-17). US market news concentrates 08:00-17:00 ET.
    timed_total = sum(hour_hist.values()) or 1
    mass_13_21 = sum(n for h, n in hour_hist.items() if 13 <= h <= 21) / timed_total
    mass_8_17 = sum(n for h, n in hour_hist.items() if 8 <= h <= 17) / timed_total
    source_tz = "UTC" if mass_13_21 > mass_8_17 else "US/Eastern"

    # Earnings-headline probe: after-hours + pre-market share of earnings news
    # under each tz hypothesis. Earnings overwhelmingly release outside RTH
    # (BMO/AMC convention), so the correct tz shows the higher outside-RTH share.
    probe = {}
    for tz_name, shift in (("UTC", -5), ("US/Eastern", 0)):
        outside = con.execute(f"""
            SELECT count(*) FILTER (WHERE ((extract(hour FROM ts) + 24 + ({shift})) % 24) >= 16
                                        OR ((extract(hour FROM ts) + 24 + ({shift})) % 24) < 9),
                   count(*)
            FROM news
            WHERE ts IS NOT NULL
              AND NOT (extract(hour FROM ts)=0 AND extract(minute FROM ts)=0
                       AND extract(second FROM ts)=0)
              AND lower(title) LIKE '%earnings%'
        """).fetchone()
        probe[tz_name] = round(outside[0] / max(1, outside[1]), 4)

    prices_exists = config.PRICES_PARQUET.exists()
    unmatched_frac = None
    if prices_exists:
        unmatched_frac = con.execute(f"""
            WITH ns AS (SELECT DISTINCT symbol FROM news),
                 ps AS (SELECT DISTINCT symbol FROM read_parquet('{config.PRICES_PARQUET}'))
            SELECT round(count(*) FILTER (WHERE ps.symbol IS NULL) * 1.0 / count(*), 4)
            FROM ns LEFT JOIN ps USING (symbol)
        """).fetchone()[0]

    date_range = con.execute(
        "SELECT min(ts), max(ts) FROM news WHERE ts IS NOT NULL"
    ).fetchone()

    audit = {
        "total_rows": total,
        "unparseable_frac": round(unparseable / max(1, total), 5),
        "dateonly_frac": round(dateonly / max(1, total), 4),
        "date_min": str(date_range[0]),
        "date_max": str(date_range[1]),
        "by_year": by_year,
        "top_publishers": by_publisher,
        "hour_histogram_timed": hour_hist,
        "tz_inference": {
            "mass_13_21_utc_band": round(mass_13_21, 4),
            "mass_8_17_et_band": round(mass_8_17, 4),
            "earnings_outside_rth_share": probe,
            "decision_source_tz": source_tz,
        },
        "symbols_unmatched_in_prices_frac": unmatched_frac,
    }
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    config.TIMESTAMP_AUDIT_JSON.write_text(json.dumps(audit, indent=2))
    print(json.dumps({k: v for k, v in audit.items()
                      if k not in ("by_year", "top_publishers", "hour_histogram_timed")},
                     indent=2), flush=True)
    print(f"full audit -> {config.TIMESTAMP_AUDIT_JSON}", flush=True)


if __name__ == "__main__":
    main()
