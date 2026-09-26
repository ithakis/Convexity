#!/usr/bin/env python3
"""Assert every runtime dependency is declared in the dependency manifests.

This is the check that would have caught the v1.10.0 bug. The app had two
install paths that were never compared — requirements.txt (pip, what CI
installed) and environment.yml (conda, what install.sh/update.sh built the
desktop app's `pt` env from). lightgbm and scikit-learn were listed only in
the former, so CI ran with the ML model working while every installed copy of
the desktop app ran with it dead.

convexity/envcheck.REQUIRED is the single source of truth. Since Phase 2 of
docs/plans/distribution-roadmap.md the authoritative manifest is
pyproject.toml's `[project].dependencies` (what `uv sync` / uv.lock and CI
install). requirements.txt and environment.yml are deprecated but still exist
for one release, because an existing checkout's update.sh still builds `pt`
from environment.yml — so while either file exists it is checked too. Once
Phase 4 deletes them, their checks simply drop out.

Run in CI (the `lint` job) and locally:

    python scripts/check_dependency_manifests.py

Deliberately dependency-free: tomllib (3.11+) for pyproject, loose line parsing
for the others, so it runs in the bare lint job. It only asks "does this
package name appear as a declared dependency", not "is the version range
right", which is the resolvers' job.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

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
    # (file, parser, Requirement field naming the package there, must exist)
    manifests = [
        (REPO / "pyproject.toml", parse_pyproject, "pip", True),
        (REPO / "requirements.txt", parse_requirements, "pip", False),
        (REPO / "environment.yml", parse_environment_yml, "conda", False),
    ]

    problems: list[str] = []
    checked: list[str] = []
    for path, parse, field, mandatory in manifests:
        if not path.exists():
            if mandatory:
                problems.append(f"{path.name} not found")
            continue
        declared = parse(path)
        checked.append(path.name)
        for r in REQUIRED:
            spec = getattr(r, field)
            if _base_name(spec) not in declared:
                problems.append(
                    f"{r.module}: '{spec}' missing from {path.name} "
                    f"(needed for: {r.feature})")

    if problems:
        print("Dependency manifests are out of sync with "
              "convexity/envcheck.REQUIRED:\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("\nAdd the missing specs, or drop the entry from REQUIRED if the "
              "app genuinely no longer needs it. After editing pyproject.toml "
              "run `uv lock`.\n", file=sys.stderr)
        return 1

    print(f"OK — all {len(REQUIRED)} runtime dependencies are declared in "
          f"{', '.join(checked)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
