#!/usr/bin/env python3
"""Seed a synthetic "My Investments" book into a temp data folder.

Every My Investments prototype, screenshot and manual test runs on this book,
never on the owner's real one (CLAUDE.md §3, §18). It uses public tickers and
made-up quantities only, so screenshots of it are safe to publish.

The book is shaped to exercise the engine's hard cases (roadmap Appendix B):
a pre-split NVDA buy on the contract-note basis (10:1 split on 2024-06-10),
an LSE line quoted in pence, a EUR ETF, a partial sell, dividends with
withholding tax, a later top-up, a standalone fee and a withdrawal.

Refuses to run without CONVEXITY_HOME, so it can never write into the real
data folder, and refuses to overwrite an existing book without --force.

    CONVEXITY_HOME=$(mktemp -d) uv run python scripts/seed_demo_book.py
"""

import argparse
import json
import os
import sys
from pathlib import Path

SCHEMA_VERSION = 1
CREATED = "2026-10-05T00:00:00Z"

INSTRUMENTS = {
    "AAPL": {"name": "Apple Inc.", "quote_ccy": "USD", "ccy": "USD"},
    "MSFT": {"name": "Microsoft Corporation", "quote_ccy": "USD", "ccy": "USD"},
    "NVDA": {"name": "NVIDIA Corporation", "quote_ccy": "USD", "ccy": "USD"},
    "SHEL.L": {"name": "Shell plc", "quote_ccy": "GBp", "ccy": "GBP"},
    "VWCE.DE": {"name": "Vanguard FTSE All-World UCITS ETF", "quote_ccy": "EUR", "ccy": "EUR"},
}

# (date, type, symbol, qty, price, fee, amount, tax, ccy, qty_basis)
# Prices are in the major unit (GBP, not pence). Amounts are for cash events.
LEDGER = [
    ("2023-01-03", "deposit", None, None, None, None, 50000.0, None, "USD", None),
    ("2023-01-04", "buy", "AAPL", 60, 126.36, 1.0, None, None, "USD", "trade"),
    ("2023-02-15", "buy", "MSFT", 40, 269.32, 1.0, None, None, "USD", "trade"),
    ("2023-05-31", "buy", "NVDA", 20, 378.34, 1.0, None, None, "USD", "trade"),
    ("2023-09-12", "buy", "SHEL.L", 300, 25.40, 3.0, None, None, "GBP", "trade"),
    ("2024-01-02", "deposit", None, None, None, None, 20000.0, None, "USD", None),
    ("2024-01-03", "buy", "VWCE.DE", 120, 105.12, 2.0, None, None, "EUR", "trade"),
    ("2024-05-16", "dividend", "AAPL", None, None, None, 15.00, 2.25, "USD", None),
    ("2024-08-15", "dividend", "MSFT", None, None, None, 30.00, 4.50, "USD", None),
    ("2025-02-03", "sell", "NVDA", 80, 116.66, 1.0, None, None, "USD", "trade"),
    ("2025-06-02", "buy", "MSFT", 10, 461.97, 1.0, None, None, "USD", "trade"),
    ("2025-09-30", "fee", None, None, None, None, 25.0, None, "USD", None),
    ("2025-11-13", "dividend", "AAPL", None, None, None, 15.60, 2.34, "USD", None),
    ("2026-03-02", "withdrawal", None, None, None, None, 5000.0, None, "USD", None),
]


def build_book() -> dict:
    entries = []
    for i, (day, kind, sym, qty, price, fee, amount, tax, ccy, basis) in enumerate(LEDGER, 1):
        entries.append(
            {
                "id": f"e_demo_{i:03d}",
                "type": kind,
                "date": day,
                "symbol": sym,
                "qty": qty,
                "price": price,
                "fee": fee,
                "amount": amount,
                "tax": tax,
                "ratio": None,
                "ccy": ccy,
                "fx_rate": None,
                "qty_basis": basis,
                "source": "demo",
                "batch": "b_demo",
                "note": "",
                "created_at": CREATED,
                "updated_at": CREATED,
            }
        )
    return {
        "version": SCHEMA_VERSION,
        "rev": 1,
        "settings": {"base_ccy": "USD", "benchmark": "SPY"},
        "instruments": INSTRUMENTS,
        "entries": entries,
        "journal": [],
        "redo": [],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="overwrite an existing book")
    args = ap.parse_args()

    home = os.environ.get("CONVEXITY_HOME", "").strip()
    if not home:
        print("Refusing to run: set CONVEXITY_HOME to a temp folder (never the real data).")
        return 2
    target = Path(home).expanduser() / "state" / "investments.json"
    if target.exists() and not args.force:
        print(f"{target} already exists; pass --force to overwrite it.")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build_book(), indent=2) + "\n", encoding="utf-8")
    print(f"Seeded {len(LEDGER)} demo entries into {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
