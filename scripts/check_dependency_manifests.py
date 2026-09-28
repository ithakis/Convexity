#!/usr/bin/env python3
"""Assert every runtime dependency is declared in the dependency manifest.

This is the check that would have caught the v1.10.0 bug. The app had two
install paths that were never compared — requirements.txt (pip, what CI
installed) and environment.yml (conda, what install.sh/update.sh built the
desktop app's `pt` env from). lightgbm and scikit-learn were listed only in
the former, so CI ran with the ML model working while every installed copy of
the desktop app ran with it dead.

src/convexity/envcheck.REQUIRED is the single source of truth. Since v1.14 the one
manifest is pyproject.toml's `[project].dependencies` (what uv.lock, CI and the
`uv tool install` installers all resolve from); requirements.txt and
environment.yml are gone.

Run in CI (the `lint` job) and locally:

    python scripts/check_dependency_manifests.py

Deliberately dependency-free (tomllib, 3.11+), so it runs in the bare lint job. It only asks "does this
package name appear as a declared dependency", not "is the version range
right", which is the resolvers' job.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from convexity.envcheck import REQUIRED  # noqa: E402


def _base_name(spec: str) -> str:
    """'lightgbm>=4.0  # comment' -> 'lightgbm' (normalized, _ and . folded)."""
    spec = spec.split("#", 1)[0].strip()
    name = re.split(r"[<>=!~\[ ]", spec, 1)[0].strip()
    return name.lower().replace("_", "-").replace(".", "-")


def parse_pyproject(path: Path) -> set[str]:
    """Names in [project].dependencies. Extras don't count: a runtime
    dependency hidden behind `--extra` is exactly the v1.10.0 failure."""
    data = tomllib.loads(path.read_text())
    return {_base_name(d) for d in data.get("project", {}).get("dependencies", [])}


def main() -> int:
    path = REPO / "pyproject.toml"
    if not path.exists():
        print("pyproject.toml not found", file=sys.stderr)
        return 1
    declared = parse_pyproject(path)
    problems = [
        f"{r.module}: '{r.pip}' missing from pyproject.toml [project].dependencies "
        f"(needed for: {r.feature})"
        for r in REQUIRED if _base_name(r.pip) not in declared
    ]

    if problems:
        print("pyproject.toml is out of sync with "
              "src/convexity/envcheck.REQUIRED:\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("\nAdd the missing specs, or drop the entry from REQUIRED if the "
              "app genuinely no longer needs it. After editing pyproject.toml "
              "run `uv lock`.\n", file=sys.stderr)
        return 1

    print(f"OK — all {len(REQUIRED)} runtime dependencies are declared in "
          "pyproject.toml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
