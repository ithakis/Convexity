"""My Investments, Phase 1: the page payload (investments.book_preview).

The rule that matters: a real book is never shown made-up numbers. Only the
synthetic demo book (scripts/seed_demo_book.py) gets demo figures; anything
else answers ``empty`` until the ledger engine exists (roadmap Phase 2).
"""

import importlib.util
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from convexity import investments, paths

SEED = Path(__file__).resolve().parents[1] / "scripts" / "seed_demo_book.py"


def _write_book(entries):
    path = paths.state_file("investments")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")


def _seed():
    spec = importlib.util.spec_from_file_location("seed_demo_book", SEED)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _write_book(mod.build_book()["entries"])


def test_no_book_is_empty():
    assert investments.book_preview() == {"empty": True}


def test_unreadable_book_is_empty_and_untouched():
    path = paths.state_file("investments")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert investments.book_preview() == {"empty": True}
    assert path.read_text(encoding="utf-8") == "{not json"


def test_a_real_book_never_gets_demo_numbers():
    _write_book([{"id": "e1", "type": "buy", "symbol": "AAPL", "source": "manual"}])
    assert investments.book_preview() == {"empty": True}


def test_seeded_demo_book_is_coherent():
    _seed()
    b = investments.book_preview()
    assert b["demo"] is True and b["empty"] is False
    h, s = b["headline"], b["series"]
    # Weights plus cash add up to 100%, and the value to positions plus cash.
    total_w = sum(p["weight"] for p in b["positions"]) + h["cash"] / h["value"] * 100
    assert abs(total_w - 100) < 1e-9
    assert abs(sum(p["value"] for p in b["positions"]) + h["cash"] - h["value"]) < 1e-6
    # Profit = value - money put in: the chart's last point agrees with the cell.
    assert abs(s["value"][-1] - h["value"]) < 0.01
    assert abs(h["value"] - s["invested"][-1] - h["profit_total"]) < 0.01
    # Daily points (the owner asked for daily, not weekly): weekdays only.
    days = [date.fromisoformat(d) for d in s["dates"]]
    assert len(days) > 700 and all(d.weekday() < 5 for d in days)
    # Every headline return is read off the chart's own index at the base
    # the page slices with, so the two can never disagree.
    for p, i in s["period_start"].items():
        assert h["twr"][p] == round((s["twr_index"][-1] / s["twr_index"][i] - 1) * 100, 2)


def test_period_starts_clamp_month_ends_and_use_jan_1_for_ytd():
    dates = ["2025-12-31", "2026-01-02", "2026-02-27", "2026-03-02", "2026-03-31"]
    st = investments.period_starts(dates)
    assert dates[st["1M"]] == "2026-02-27"  # 31 Mar - 1M = 28 Feb (clamped), last on/before
    assert dates[st["YTD"]] == "2025-12-31"  # base is the last close before 1 Jan
    assert st["ALL"] == 0


def test_seed_refuses_without_convexity_home(monkeypatch):
    monkeypatch.delenv("CONVEXITY_HOME", raising=False)
    r = subprocess.run(
        [sys.executable, str(SEED)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2
    assert "Refusing" in r.stdout
