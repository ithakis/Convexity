"""The `convexity` command (and `python -m convexity`).

    convexity                   run the dashboard server (browser mode)
    convexity build-symbols     build the weekly symbol pack (CI)
    convexity build-reference-pack
                                build the daily S&P 500 reference pack (CI)

The desktop window is the separate `convexity-app` command (desktop.py).

Both builders run in CI and publish to the `reference-pack` release; the app
only downloads what they publish. Their imports are deferred so starting the
server pays nothing for them.
"""

import sys

USAGE = """\
usage: convexity [build-symbols --out DIR [--previous DIR] [--limit N]]
       convexity build-reference-pack (--out DIR [--previous DIR] | --returns-only) [--limit N]

  (no command)          run the dashboard server and print its URL
  build-symbols         build the symbol pack (every Yahoo listing) — run
                        by CI (`--help` for its options)
  build-reference-pack  build the reference pack (the Market read of the
                        S&P 500) — run by CI; needs FINNHUB_API_KEY in the
                        environment (`--help` for its options)
"""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if not args:
        from .server import main as serve

        serve()
        return 0
    if args[0] == "build-symbols":
        from .symbol_build import main as build_symbol_pack

        return build_symbol_pack(args[1:])
    if args[0] == "build-reference-pack":
        from .reference_build import main as build_reference_pack

        return build_reference_pack(args[1:])
    if args[0] in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0
    sys.stderr.write(f"convexity: unknown command {args[0]!r}\n{USAGE}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
