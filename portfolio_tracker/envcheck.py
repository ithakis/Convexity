"""Runtime dependency manifest + self-check.

Why this module exists: v1.10.0 "fixed" the dead ML sentiment model by adding
lightgbm/scikit-learn to environment.yml. That fixed the *declaration* and
nothing else — the installed `pt` conda env was never re-solved, so the app kept
running without the packages, `ml_sentiment.available()` kept returning False,
and the only signal was one red line in a log pane nobody opens. A dependency
declaration is not a dependency.

Three separate places used to answer "what does this app need?" independently:
requirements.txt (pip, what CI installs), environment.yml (conda, what the
desktop app installs), and the import sites themselves. They drifted, and
nothing compared them. REQUIRED below is now the single source of truth:

  * scripts/check_dependency_manifests.py asserts (in CI) that every entry here
    is declared in BOTH requirements.txt and environment.yml.
  * server.start_server() calls check() at boot and logs a loud block naming
    each missing package and the feature it kills.
  * /api/runtime-status serves check() to the UI (Settings -> Models & Data,
    plus a banner under the topbar) so a broken env is visible in the app.
  * install.sh / update.sh (and the PowerShell twins) run
    `python -m portfolio_tracker.envcheck` against the freshly-solved env, so a
    half-built env fails the installer instead of exiting 0.

Deliberately import-only: no version parsing, no pkg_resources, no network. The
question this answers is "can the running interpreter import what the app needs",
which is exactly the failure mode that shipped. Version *ranges* are the
manifests' job and are enforced by the conda/pip solvers.
"""
from __future__ import annotations

import importlib.util
import sys
from typing import NamedTuple


class Requirement(NamedTuple):
    """One runtime dependency.

    module   — what `import` name to probe (may differ from the package name,
               e.g. scikit-learn -> sklearn).
    conda    — spec as it must appear in environment.yml.
    pip      — spec as it must appear in requirements.txt.
    feature  — what breaks without it, in the user's language. This is what the
               startup log and the in-app banner show, so keep it concrete.
    critical — True: a core surface is dead without it. False: degraded but the
               app is still useful. Only `critical` misses make env_ok False.
    """
    module: str
    conda: str
    pip: str
    feature: str
    critical: bool = True


# Ordered roughly by how loudly the app breaks without each one.
REQUIRED: tuple[Requirement, ...] = (
    Requirement("pandas", "pandas", "pandas",
                "quotes, analytics, and every table in the app"),
    Requirement("numpy", "numpy", "numpy",
                "all numeric computation"),
    Requirement("yfinance", "yfinance", "yfinance",
                "market data — no prices, no rows"),
    Requirement("requests", "requests", "requests",
                "Finnhub news and symbol-database downloads"),
    Requirement("numba", "numba", "numba",
                "the mean-CVaR optimizer (Optimize tab) JIT kernels"),
    Requirement("scipy", "scipy", "scipy",
                "ML feature assembly (scipy.sparse) and the LP reference solver"),
    # The two that were missing for weeks. ml_sentiment._load() imports both,
    # so either one absent silently demotes every ml_* field to null.
    Requirement("lightgbm", "lightgbm", "lightgbm",
                "ML news sentiment — the primary NS signal (falls back to LLM)"),
    Requirement("sklearn", "scikit-learn", "scikit-learn",
                "ML news sentiment feature hashing (needed with lightgbm)"),
    Requirement("openai", "openai", "openai",
                "LLM news sentiment via the NVIDIA NIM endpoint"),
    Requirement("lxml", "lxml", "lxml",
                "the EPS Surprise column (yfinance parses Yahoo HTML via lxml)"),
    Requirement("openpyxl", "openpyxl", "openpyxl",
                "Excel export"),
    Requirement("rapidfuzz", "rapidfuzz", "rapidfuzz",
                "fuzzy ticker lookup and news de-duplication",
                critical=False),
)


def _importable(module: str) -> bool:
    """True if `module` can be imported without actually importing it.

    find_spec keeps this cheap and side-effect-free — importing numba or
    lightgbm here would add seconds to every server boot for a question we only
    need answered structurally. Already-imported modules short-circuit.
    """
    if module in sys.modules:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        # A broken/partial install can raise instead of returning None; that is
        # still "not usable", which is the answer the caller wants.
        return False


def check() -> list[Requirement]:
    """Return the REQUIRED entries this interpreter cannot import (empty = OK)."""
    return [r for r in REQUIRED if not _importable(r.module)]


def status() -> dict:
    """JSON-serializable summary for /api/runtime-status and /api/health.

    `ok` ignores non-critical misses so a missing rapidfuzz never raises an
    alarm banner over a dashboard that works fine without it.
    """
    missing = check()
    return {
        "ok": not any(r.critical for r in missing),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "missing": [
            {"module": r.module, "conda": r.conda, "pip": r.pip,
             "feature": r.feature, "critical": r.critical}
            for r in missing
        ],
    }


def format_report(missing: list[Requirement]) -> str:
    """Multi-line human report for the terminal and the Settings -> Logs pane."""
    if not missing:
        return "[envcheck] all runtime dependencies present"
    lines = [
        "",
        "=" * 72,
        "[envcheck] MISSING RUNTIME DEPENDENCIES — the app is running degraded.",
        "=" * 72,
    ]
    for r in missing:
        tag = "REQUIRED" if r.critical else "optional"
        lines.append(f"  {r.module:<12} ({tag})  disables: {r.feature}")
    lines += [
        "",
        f"  Interpreter: {sys.executable}",
        "  Fix: run ./update.sh (syncs the conda env to environment.yml),",
        "       then fully quit and relaunch the app — a failed model load is",
        "       cached for the process lifetime and will not retry in place.",
        "=" * 72,
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI used by install.sh / update.sh to verify a freshly-solved env."""
    missing = check()
    print(format_report(missing))
    # Only critical misses fail the installer — a degraded-but-usable env should
    # not abort an otherwise successful install.
    return 1 if any(r.critical for r in missing) else 0


if __name__ == "__main__":
    raise SystemExit(main())
