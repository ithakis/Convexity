#!/usr/bin/env python3
"""Assert every runtime dependency is declared in BOTH dependency manifests.

This is the check that would have caught the v1.10.0 bug. The app has two
install paths that were never compared:

  * requirements.txt  -> pip, what CI installs
  * environment.yml   -> conda, what install.sh/update.sh build the desktop
                         app's `pt` env from

lightgbm and scikit-learn were listed only in requirements.txt, which
install.sh never pip-installs. So CI ran with the ML model working while every
installed copy of the desktop app ran with it dead — and nothing anywhere
noticed the two files disagreed.

portfolio_tracker/envcheck.REQUIRED is the single source of truth; this script
asserts both manifests cover it. Run in CI (the `lint` job) and locally:

    python scripts/check_dependency_manifests.py

Deliberately dependency-free: parses both files with the standard library so it
can run in the bare lint job without installing PyYAML. The parsing is
intentionally loose — it only asks "does this package name appear as a declared
dependency", not "is the version range right", which is the solvers' job.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from portfolio_tracker.envcheck import REQUIRED  # noqa: E402


def _base_name(spec: str) -> str:
    """'lightgbm>=4.0  # comment' -> 'lightgbm' (normalized, _ and . folded)."""
    spec = spec.split("#", 1)[0].strip()
    name = re.split(r"[<>=!~\[ ]", spec, 1)[0].strip()
    return name.lower().replace("_", "-").replace(".", "-")


def parse_requirements(path: Path) -> set[str]:
    out = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        name = _base_name(line)
        if name:
            out.add(name)
    return out


def parse_environment_yml(path: Path) -> set[str]:
    """Collect names from the conda `dependencies:` list AND its nested `pip:`
    block — a dep satisfied via pip inside the conda env still counts as
    declared for the `pt` env.
    """
    out = set()
    in_deps = False
    for raw in path.read_text().splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^dependencies:\s*$", stripped):
            in_deps = True
            continue
        # A new top-level key (column 0, not a list item) ends the block.
        if in_deps and not raw.startswith((" ", "\t")) and not stripped.startswith("-"):
            in_deps = False
            continue
        if not in_deps or not stripped.startswith("- "):
            continue
        item = stripped[2:].strip()
        if item.rstrip(":") == "pip":
            continue  # the `- pip:` header itself, not a package
        name = _base_name(item)
        if name:
            out.add(name)
    return out


def main() -> int:
    req_path = REPO / "requirements.txt"
    env_path = REPO / "environment.yml"
    pip_declared = parse_requirements(req_path)
    conda_declared = parse_environment_yml(env_path)

    problems: list[str] = []
    for r in REQUIRED:
        if _base_name(r.pip) not in pip_declared:
            problems.append(
                f"{r.module}: '{r.pip}' missing from requirements.txt "
                f"(needed for: {r.feature})")
        if _base_name(r.conda) not in conda_declared:
            problems.append(
                f"{r.module}: '{r.conda}' missing from environment.yml "
                f"(needed for: {r.feature})")

    if problems:
        print("Dependency manifests are out of sync with "
              "portfolio_tracker/envcheck.REQUIRED:\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("\nAdd the missing specs, or drop the entry from REQUIRED if the "
              "app genuinely no longer needs it.\n", file=sys.stderr)
        return 1

    print(f"OK — all {len(REQUIRED)} runtime dependencies are declared in both "
          f"requirements.txt and environment.yml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
