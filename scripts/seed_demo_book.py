#!/usr/bin/env python3
"""Seed a synthetic "My Investments" book into a temp data folder.

Every My Investments prototype, screenshot and manual test runs on this book,
never on the owner's real one (CLAUDE.md §3, §18). It uses public tickers and
made-up quantities only, so screenshots of it are safe to publish.

The book is shaped to exercise the engine's hard cases (roadmap Appendix B):
a pre-split NVDA buy on the contract-note basis (10:1 split on 2024-06-10),
an LSE line quoted in pence, a EUR ETF, a partial sell, dividends with
withholding tax, a later top-up, a standalone fee and a withdrawal. A
quick-added holding (GOOGL, ``opening``) at an average price far below its
Since-day close shows the Phase 3 rule: its performance starts at that day's
value. Most payouts are left unrecorded, so Yahoo's dividends get estimated.

``--crafted`` adds rows that trip the Attention rail's X-ray rules (roadmap
Phase 7): a quick-added Pfizer at an average price far above today's (down
from cost) on top of the demo's tech-heavy, NVIDIA-heavy mix (single name,
sector) and its pound and euro lines (currency, viewed in USD). It also
writes plans (roadmap Phase 8, ``state/plans.json``) whose levels are far
enough from any recent price to fire: Apple past its target, Microsoft
below its review level, the world ETF above its own max weight, and an
NVIDIA price alert that has crossed; Alphabet has a plan that stays quiet.

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
    "GOOGL": {"name": "Alphabet Inc.", "quote_ccy": "USD", "ccy": "USD"},
    "PFE": {"name": "Pfizer Inc.", "quote_ccy": "USD", "ccy": "USD"},
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
    {"date": "2025-03-03", "type": "buy", "symbol": "GOOGL", "qty": 30, "price": 95.00, "fee": 0.0, "opening": True},
    {"date": "2025-06-02", "type": "buy", "symbol": "MSFT", "qty": 10, "price": 461.97, "fee": 1.0},
    {"date": "2025-09-30", "type": "fee", "amount": 25.0},
    {"date": "2025-11-13", "type": "dividend", "symbol": "AAPL", "amount": 15.60, "tax": 2.34},
    {"date": "2026-03-02", "type": "withdrawal", "amount": 5000.0},
]  # fmt: skip
# --crafted: the Attention rail's rules fire on this book (made-up prices).
CRAFTED = [
    {"date": "2025-06-02", "type": "buy", "symbol": "PFE", "qty": 200, "price": 60.00, "fee": 0.0, "opening": True},
]  # fmt: skip
# Levels far from any recent price, so each fires (or stays quiet) whatever
# the day's prices are; "set_price" is made up, like the trades.
CRAFTED_PLANS = {
    "AAPL": {"target": 120.0, "review": 60.0, "set_on": "2026-03-03", "set_price": 110.0,
             "why": "Services keep growing faster than hardware"},
    "MSFT": {"target": 2000.0, "review": 1000.0, "set_on": "2026-06-12", "set_price": 1100.0,
             "why": "Sell if cloud growth stalls"},
    "VWCE.DE": {"max_weight": 5.0},
    "NVDA": {"alerts": [{"id": "demo0001", "level": 20.0, "side": "above", "set_on": "2026-09-01"}]},
    "GOOGL": {"target": 5000.0, "review": 5.0, "set_on": "2026-05-04", "set_price": 160.0},
}  # fmt: skip
BLANK = dict.fromkeys(("symbol", "qty", "price", "fee", "amount", "tax", "ratio", "fx_rate"))
BLANK |= {"ccy": "USD", "qty_basis": None, "source": "demo", "batch": "b_demo", "note": ""}


def build_book(crafted: bool = False) -> dict:
    """The demo book in the agreed schema (roadmap Appendix B). Trades carry
    qty_basis "trade" (contract-note shares), so the pre-split NVDA buy
    exercises the split logic."""
    entries = [
        BLANK | row | {"id": f"e_demo_{i:03d}", "created_at": CREATED, "updated_at": CREATED}
        | ({"qty_basis": "current" if row.get("opening") else "trade"} if row["type"] in ("buy", "sell") else {})
        for i, row in enumerate(LEDGER + (CRAFTED if crafted else []), 1)
    ]  # fmt: skip
    used = {e["symbol"] for e in entries if e["symbol"]}
    return {
        "version": SCHEMA_VERSION,
        "rev": 1,
        "settings": {"base_ccy": "USD", "benchmark": "SPY"},
        "instruments": {k: v for k, v in INSTRUMENTS.items() if k in used},
        "entries": entries,
        "journal": [],
        "redo": [],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="overwrite an existing book")
    ap.add_argument("--crafted", action="store_true", help="add rows that trip the Attention rules")
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
    book = build_book(args.crafted)
    target.write_text(json.dumps(book, indent=2) + "\n", encoding="utf-8")
    print(f"Seeded {len(book['entries'])} demo entries into {target}")
    if args.crafted:
        plans = target.parent / "plans.json"
        plans.write_text(
            json.dumps({"version": 1, "plans": CRAFTED_PLANS}, indent=1) + "\n", encoding="utf-8"
        )
        print(f"Seeded {len(CRAFTED_PLANS)} demo plans into {plans}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
