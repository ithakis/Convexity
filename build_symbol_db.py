#!/usr/bin/env python3
"""CLI for building / refreshing the local symbol database.

Examples
--------
    # Default: pull every registered source into symbol_db.sqlite.
    python build_symbol_db.py

    # Just one source:
    python build_symbol_db.py --sources nasdaq

    # Custom output path:
    python build_symbol_db.py --db /tmp/syms.sqlite

To add new sources see ``symbol_db.SOURCES`` and the per-source pattern
documented in ``symbol_db.py``. The dashboard reads the SQLite file via
``symbol_db.lookup()`` — no other wiring needed once a source is in.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.stderr.write("error: the 'requests' package is required (pip install requests)\n")
    sys.exit(2)

from convexity import symbol_db as sdb


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the local symbol database.")
    ap.add_argument(
        "--sources", default=",".join(sdb.SOURCES.keys()),
        help=f"Comma-separated list of sources to pull. "
             f"Available: {','.join(sdb.SOURCES.keys())}. Default: all.",
    )
    ap.add_argument(
        "--db", default=str(sdb.db_path()),
        help="Output SQLite path. Default: ./symbol_db.sqlite",
    )
    args = ap.parse_args()

    requested = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in requested if s not in sdb.SOURCES]
    if unknown:
        sys.stderr.write(f"error: unknown source(s): {unknown}\n")
        sys.stderr.write(f"available: {list(sdb.SOURCES.keys())}\n")
        return 2

    db = Path(args.db)
    sdb.init_db(db)
    print(f"→ database: {db}")

    session = requests.Session()
    session.headers.update({"User-Agent": "Convexity/symbol-db builder"})

    total = 0
    t0 = time.time()
    for name in requested:
        print(f"\n[{name}] fetching…")
        t1 = time.time()
        try:
            rows = list(sdb.SOURCES[name](session))
        except Exception as exc:
            print(f"  ! source failed: {exc}")
            continue
        wrote = sdb.upsert_rows(rows, source=name, path=db)
        total += wrote
        print(f"[{name}] wrote {wrote} rows in {time.time()-t1:.1f}s")

    print(f"\n✓ {total} rows total in {time.time()-t0:.1f}s")
    stats = sdb.db_stats(db)
    print(f"  on-disk total: {stats['total']}")
    if stats["by_source"]:
        print("  by source:")
        for s, c in stats["by_source"].items():
            print(f"    {s:<12} {c:>7}")
    if stats["top_exchanges"]:
        print("  top exchanges:")
        for x, c in stats["top_exchanges"].items():
            print(f"    {x:<12} {c:>7}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
