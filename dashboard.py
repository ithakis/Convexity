"""Backward-compat shim — ``python dashboard.py`` still launches the server.

Re-exports a few symbols so existing ``from dashboard import X`` continues to
work (e.g. test_metrics.py).
"""

from convexity.helpers import _normalize_dividend_yield, _safe_num  # noqa: F401
from convexity.server import main

if __name__ == "__main__":
    main()
