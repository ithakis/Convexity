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

# Each row holds only its own fields; build_book() fills the rest from BLANK.
# Prices are in the major unit (GBP, not pence); "amount" is for cash events.
LEDGER = [
    {"date": "2023-01-03", "type": "deposit", "amount": 50000.0},
    {"date": "2023-01-04", "type": "buy", "symbol": "AAPL", "qty": 60, "price": 126.36, "fee": 1.0},
    {"date": "2023-02-15", "type": "buy", "symbol": "MSFT", "qty": 40, "price": 269.32, "fee": 1.0},
    {"date": "2023-05-31", "type": "buy", "symbol": "NVDA", "qty": 20, "price": 378.34, "fee": 1.0},
    {"date": "2023-09-12", "type": "buy", "symbol": "SHEL.L", "qty": 300, "price": 25.40, "fee": 3.0, "ccy": "GBP"},
    {"date": "2024-01-02", "type": "deposit", "amount": 20000.0},
    {"date": "2024-01-03", "type": "buy", "symbol": "VWCE.DE", "qty": 120, "price": 105.12, "fee": 2.0, "ccy": "EUR"},
    {"date": "2024-05-16", "type": "dividend", "symbol": "AAPL", "amount": 15.00, "tax": 2.25},
    {"date": "2024-08-15", "type": "dividend", "symbol": "MSFT", "amount": 30.00, "tax": 4.50},
    {"date": "2025-02-03", "type": "sell", "symbol": "NVDA", "qty": 80, "price": 116.66, "fee": 1.0},
    {"date": "2025-06-02", "type": "buy", "symbol": "MSFT", "qty": 10, "price": 461.97, "fee": 1.0},
    {"date": "2025-09-30", "type": "fee", "amount": 25.0},
    {"date": "2025-11-13", "type": "dividend", "symbol": "AAPL", "amount": 15.60, "tax": 2.34},
    {"date": "2026-03-02", "type": "withdrawal", "amount": 5000.0},
]  # fmt: skip
BLANK = dict.fromkeys(("symbol", "qty", "price", "fee", "amount", "tax", "ratio", "fx_rate"))
BLANK |= {"ccy": "USD", "qty_basis": None, "source": "demo", "batch": "b_demo", "note": ""}


def build_book() -> dict:
    """The demo book in the agreed schema (roadmap Appendix B). Trades carry
    qty_basis "trade" (contract-note shares), so the pre-split NVDA buy
    exercises the split logic."""
    entries = [
        BLANK | row | {"id": f"e_demo_{i:03d}", "created_at": CREATED, "updated_at": CREATED}
        | ({"qty_basis": "trade"} if row["type"] in ("buy", "sell") else {})
        for i, row in enumerate(LEDGER, 1)
    ]  # fmt: skip
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
