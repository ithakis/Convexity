#!/usr/bin/env python
"""Fill missing ml_* fields in the sentiment history from cached articles.

Why this exists: the ML news-sentiment model could not load in the desktop
app's conda env (no lightgbm / scikit-learn — see environment.yml), and the
failure was swallowed silently. Every sentiment history record written in that
period carries `ml_sar: null`, so the News tab's Model Diagnostics panels —
live rank IC, calibration, ML-vs-LLM agreement, coverage — have nothing to
compute from and would stay empty for weeks after the env is fixed.

This re-scores those records from the articles still in
`.portfolio_tracker_news.json`, so the panels have real data immediately.

Honest about its limits:
  * The news cache holds the CURRENT article set per symbol on a 30-day TTL,
    not a point-in-time snapshot. Only records whose window overlaps what is
    still cached can be recovered; older dates are skipped, not invented.
  * Recency weighting is re-anchored to the record's own date (aggregate(...,
    now=)), so an old record is weighted as it would have been that day.
  * Backfilled records are stamped `ml_backfilled: true`. They are
    reconstructed, not live predictions, and that distinction should survive.

Idempotent: records that already carry a non-null ml_sar are left alone, so
re-running is safe. Writes via a temp file + atomic rename.

`--force` recomputes EVERY record with cached articles, including ones that
already have ml_* fields — use this after retuning a scoring constant (e.g.
ml_sentiment._CONF_SCALE) so already-scored history reflects the new value
instead of being stuck with whatever was true at first-score time.

    python scripts/backfill_ml_history.py [--dry-run] [--lookback-days 7] [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_tracker import ml_sentiment as ml  # noqa: E402
from portfolio_tracker.helpers import _repo_root  # noqa: E402

HISTORY_FILE = _repo_root() / ".portfolio_tracker_sentiment_history.json"
NEWS_FILE = _repo_root() / ".portfolio_tracker_news.json"


def load_cached_articles() -> dict[str, list[dict]]:
    """{SYMBOL: [article, ...]} from the disk-backed news cache."""
    if not NEWS_FILE.exists():
        return {}
    data = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
    out: dict[str, list[dict]] = defaultdict(list)
    for key, entry in (data.get("news") or {}).items():
        # Keys look like "news|AAPL" or "news|AAPL|7" depending on version.
        parts = key.split("|")
        if len(parts) < 2:
            continue
        sym = parts[1].upper()
        if sym == "__MARKET__":
            continue
        for art in (entry.get("value") or []):
            if isinstance(art, dict):
                out[sym].append(art)
    # De-duplicate by url within a symbol (the same article can appear under
    # more than one cache key).
    for sym, arts in out.items():
        seen: set[str] = set()
        uniq = []
        for a in arts:
            k = a.get("url") or a.get("headline") or ""
            if k in seen:
                continue
            seen.add(k)
            uniq.append(a)
        out[sym] = uniq
    return dict(out)


def day_end_epoch(date_str: str) -> float | None:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (d + timedelta(days=1)).timestamp()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    ap.add_argument("--lookback-days", type=int, default=7,
                    help="article window per record (default: 7, the app default)")
    ap.add_argument("--force", action="store_true",
                    help="recompute records that already have ml_* fields too "
                         "(use after retuning a scoring constant)")
    args = ap.parse_args()

    status = ml.runtime_status()
    if not status["available"]:
        print(f"ML model unavailable ({status['reason']}) — nothing to backfill.\n"
              f"Fix the environment first (mamba env update -f environment.yml "
              f"--prune, or ./update.sh), then re-run.", file=sys.stderr)
        return 1
    print(f"model: {status['version']} @ {status['model_dir']}")

    if not HISTORY_FILE.exists():
        print(f"no history file at {HISTORY_FILE}", file=sys.stderr)
        return 1
    hist = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    records = hist.get("records") or []
    articles = load_cached_articles()
    print(f"history: {len(records)} records · news cache: {len(articles)} symbols")

    filled_by_date: dict[str, int] = defaultdict(int)
    skipped_no_articles = 0
    skipped_no_score = 0
    already = 0

    for rec in records:
        sym = (rec.get("symbol") or "").upper()
        if not sym or sym == "__MARKET__":
            continue
        if isinstance(rec.get("ml_sar"), (int, float)) and not args.force:
            already += 1
            continue
        end = day_end_epoch(rec.get("date") or "")
        if end is None:
            continue
        start = end - args.lookback_days * 86400
        pool = [a for a in articles.get(sym, [])
                if isinstance(a.get("datetime"), (int, float))
                and start <= a["datetime"] <= end]
        if not pool:
            skipped_no_articles += 1
            continue
        scored = []
        for a in pool:
            s = ml.score_article(a, sym)
            if s is not None:
                scored.append({**s, "datetime": a.get("datetime"),
                               "source": a.get("source"),
                               "n_duplicates": a.get("n_duplicates", 0)})
        fields = ml.aggregate(scored, now=end)
        if not fields:
            skipped_no_score += 1
            continue
        rec["ml_sar"] = fields["ml_sar"]
        rec["ml_score"] = fields["ml_score"]
        rec["ml_tier"] = fields["ml_tier"]
        rec["ml_confidence"] = fields["ml_confidence"]
        rec["ml_backfilled"] = True
        filled_by_date[rec["date"]] += 1

    total = sum(filled_by_date.values())
    for date in sorted(filled_by_date):
        print(f"  {date}: filled {filled_by_date[date]}")
    print(f"filled {total} · already scored {already} · "
          f"no cached articles in window {skipped_no_articles} · "
          f"scored but no aggregate {skipped_no_score}")

    if args.dry_run:
        print("dry run — nothing written")
        return 0
    if not total:
        print("nothing to write")
        return 0

    # Atomic replace: a half-written history file would take the diagnostics
    # panel down with it.
    hist["records"] = records
    fd, tmp = tempfile.mkstemp(dir=str(HISTORY_FILE.parent),
                               prefix=".sentiment_history.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(hist, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, HISTORY_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(f"wrote {HISTORY_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
