"""The `convexity` command (and `python -m convexity`).

    convexity                   run the dashboard server (browser mode)
    convexity build-symbols     build / refresh the local fuzzy ticker DB

The desktop window is the separate `convexity-app` command (desktop.py).

`build-symbols` replaces the old root-level `build_symbol_db.py` script, which
an installed app could not run: it lived in the checkout, not the package.
Its imports are deferred so starting the server pays nothing for it.
"""

from __future__ import annotations

import sys

USAGE = """\
usage: convexity [build-symbols [--sources S1,S2] [--db PATH]]

  (no command)     run the dashboard server and print its URL
  build-symbols    build / refresh the local symbol database
                   (`convexity build-symbols --help` for its options)
"""


def build_symbols(argv: list[str]) -> int:
    """Pull every requested source into the symbol DB (see symbol_db.SOURCES
    for the per-source pattern). The dashboard reads the file through
    ``symbol_db.lookup()`` — no other wiring is needed once a source is in."""
    import argparse
    import time
    from pathlib import Path

    from . import symbol_db as sdb

    ap = argparse.ArgumentParser(
        prog="convexity build-symbols",
        description="Build the local symbol database.",
    )
    ap.add_argument(
        "--sources",
        default=",".join(sdb.SOURCES.keys()),
        help=f"Comma-separated list of sources to pull. "
        f"Available: {','.join(sdb.SOURCES.keys())}. Default: all.",
    )
    ap.add_argument(
        "--db",
        default=str(sdb.write_path()),
        help="Output SQLite path. Default: symbol_db.sqlite in the Convexity "
        "data folder (CONVEXITY_HOME / PORTFOLIO_SYMBOL_DB override).",
    )
    args = ap.parse_args(argv)

    requested = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in requested if s not in sdb.SOURCES]
    if not requested:
        # `--sources ""` (e.g. an unset shell variable) would otherwise create
        # an empty DB and report success.
        sys.stderr.write("error: --sources is empty\n")
        sys.stderr.write(f"available: {list(sdb.SOURCES.keys())}\n")
        return 2
    if unknown:
        sys.stderr.write(f"error: unknown source(s): {unknown}\n")
        sys.stderr.write(f"available: {list(sdb.SOURCES.keys())}\n")
        return 2

    import requests

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
        print(f"[{name}] wrote {wrote} rows in {time.time() - t1:.1f}s")

    print(f"\n✓ {total} rows total in {time.time() - t0:.1f}s")
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


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if not args:
        from .server import main as serve

        serve()
        return 0
    if args[0] == "build-symbols":
        return build_symbols(args[1:])
    if args[0] in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0
    sys.stderr.write(f"convexity: unknown command {args[0]!r}\n{USAGE}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
