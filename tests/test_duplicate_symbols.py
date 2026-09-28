"""A portfolio whose rows name the same symbol twice must not break analytics.

Regression for the "Data Center Builders" 500: its saved view carried 20 rows
for 16 distinct tickers (GDS, DLR, IRM and IREN twice each), and
POST /api/portfolio-analytics-multi died with
``float() argument must be a string or a real number, not 'Series'``.

The duplicates were written by the frontend (build()'s streaming append racing
a refresh job's row patches) and then persisted wholesale by save_view, so
every later analytics call re-read them. The fix is layered — dedupe on save
and load, and again at the top of every consumer that indexes a price frame by
symbol — and each layer is pinned here. No network: every yfinance-touching
seam is monkeypatched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from convexity import analytics, helpers, jobs, persistence  # noqa: E402

# The real repro, in the order the saved view actually held it.
_DUP_SYMBOLS = [
    "IRM",
    "EQIX",
    "IREN",
    "DLR",
    "GDS",
    "ETN",
    "VRT",
    "POWL",
    "SMCI",
    "CARR",
    "ACM",
    "J",
    "VPN",
    "DTCR",
    "GDS",
    "AMT",
    "DLR",
    "IRM",
    "IREN",
    "BABA",
]
_UNIQUE = list(dict.fromkeys(_DUP_SYMBOLS))


def _closes(symbols, n=300, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    cols = list(symbols) + ["SPY", "QQQ"]
    data = np.cumprod(1 + rng.standard_normal((n, len(cols))) * 0.01, axis=0) * 100
    return pd.DataFrame(data, index=idx, columns=cols)


@pytest.fixture()
def fake_market(monkeypatch):
    """Stand-ins for the network seams analyze_portfolios_multi touches.

    The fake _bulk_close honours the real one's contract — one column per
    DISTINCT symbol, because the real one builds its frame from a dict — so the
    duplicate columns in the original crash had to come from the caller's own
    list selection, exactly as they did in production.
    """
    closes = _closes(_UNIQUE)
    calls = []

    def _bulk_close(symbols, period):
        calls.append(list(symbols))
        keep = [s for s in dict.fromkeys(symbols) if s in closes.columns]
        return closes[keep]

    monkeypatch.setattr(analytics, "_bulk_close", _bulk_close)
    monkeypatch.setattr(analytics, "_analyst_for", lambda s, row=None: {})
    monkeypatch.setattr(analytics, "_cache_get", lambda key: None)
    monkeypatch.setattr(analytics, "_cache_put", lambda key, val, ttl=None: None)
    return calls


def _rows(symbols):
    return [
        {
            "symbol": s,
            "price": 10.0 + i,
            "market_cap": 1e9 * (i + 1),
            "currency": "USD",
            "sector": "",
        }
        for i, s in enumerate(symbols)
    ]


# ------------------------------ analytics -----------------------------------


def test_analytics_multi_survives_duplicate_rows(fake_market):
    out = analytics.analyze_portfolios_multi(
        _rows(_DUP_SYMBOLS), {"equal": {}, "custom": {"GDS": 3.0, "DLR": 1.0}}, "1Y"
    )
    assert "error" not in out, out
    eq = out["equal"]
    assert eq["active_symbols"] == _UNIQUE
    # Equal weight is over DISTINCT names: a duplicate must not double a
    # holding's weight, and the weights must still sum to one.
    w = eq["weights_applied"]
    assert set(w) == set(_UNIQUE)
    assert all(v == pytest.approx(1.0 / len(_UNIQUE)) for v in w.values())
    assert sum(w.values()) == pytest.approx(1.0)
    assert len(eq["contribution"]) == len(_UNIQUE)
    assert sum(out["custom"]["weights_applied"].values()) == pytest.approx(1.0)
    # The price fetch never sees a symbol twice either.
    assert all(len(c) == len(set(c)) for c in fake_market)


def test_single_weight_analytics_survives_duplicate_rows(fake_market):
    out = analytics.analyze_portfolio(_rows(["IRM", "EQIX", "IRM"]), {}, "1Y")
    assert "error" not in out, out
    assert out["active_symbols"] == ["IRM", "EQIX"]


# ------------------------------ frontier ------------------------------------


def test_frontier_dedupes_rows_before_counting_assets(monkeypatch):
    """Two rows for ONE ticker used to pass the `need at least 2 symbols` gate
    and then fail later with a confusing "1 assets" data error."""
    from convexity import frontier

    seen = []
    monkeypatch.setattr(
        frontier, "_bulk_close", lambda syms, period: seen.append(list(syms)) or pd.DataFrame()
    )
    msgs = list(
        frontier.compute_efficient_frontier_stream(
            [{"symbol": "AAA"}, {"symbol": "AAA"}], budget="light"
        )
    )
    assert msgs[-1]["type"] == "error"
    assert "at least 2 symbols" in msgs[-1]["error"]
    assert seen == []  # rejected before any fetch


# ------------------------------ helper --------------------------------------


def test_dedupe_rows_keeps_first_position_and_latest_row():
    rows = [{"symbol": "A", "price": 1}, {"symbol": "B", "price": 2}, {"symbol": "A", "price": 3}]
    assert helpers._dedupe_rows_by_symbol(rows) == [
        {"symbol": "A", "price": 3},
        {"symbol": "B", "price": 2},
    ]


def test_dedupe_rows_never_trades_a_good_row_for_an_error_row():
    good = {"symbol": "A", "price": 1.0}
    bad = {"symbol": "A", "error": "no data"}
    assert helpers._dedupe_rows_by_symbol([good, bad]) == [good]
    # ...but an error row IS upgraded when a good one follows.
    assert helpers._dedupe_rows_by_symbol([bad, good]) == [good]


def test_dedupe_rows_passes_symbol_less_entries_through():
    rows = [{"symbol": "A"}, {"price": 1}, None, {"symbol": "A"}]
    assert helpers._dedupe_rows_by_symbol(rows) == [{"symbol": "A"}, {"price": 1}, None]
    assert helpers._dedupe_rows_by_symbol(None) == []


# ------------------------------ persistence ---------------------------------


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "_VIEWS_FILE", tmp_path / "views.json")
    monkeypatch.setattr(persistence, "_WATCHLISTS_FILE", tmp_path / "watch.json")
    return tmp_path


def test_save_view_never_persists_duplicate_rows(isolated_state):
    saved = persistence.save_view(
        "DC", "A,B", [{"symbol": "A", "price": 1}, {"symbol": "B"}, {"symbol": "A", "price": 2}]
    )
    assert [r["symbol"] for r in saved["rows"]] == ["A", "B"]
    assert [r["symbol"] for r in persistence.load_view("DC")["rows"]] == ["A", "B"]
    assert persistence.list_views()["views"]["DC"]["row_count"] == 2


def test_load_view_heals_a_view_saved_before_the_fix(isolated_state):
    """Views already on disk with duplicates (the real one) must load clean —
    the table, analytics and the Excel export all read them through here."""
    import json

    (isolated_state / "views.json").write_text(
        json.dumps(
            {"views": {"DC": {"entries": "A,B", "rows": _rows(["A", "B", "A"]), "saved_at": "x"}}}
        )
    )
    assert [r["symbol"] for r in persistence.load_view("DC")["rows"]] == ["A", "B"]
    assert persistence.list_views()["views"]["DC"]["row_count"] == 2


# ------------------------------ entries / resolution ------------------------


def test_parse_entries_drops_literal_repeats_in_order():
    assert jobs._parse_entries("EQIX,DLR,IRM\nDLR, GDS ,IRM,,") == ["EQIX", "DLR", "IRM", "GDS"]


def test_stream_quotes_collapses_names_that_resolve_to_one_ticker(monkeypatch):
    """'microsoft' and 'MSFT' are one instrument: the build must fetch it once,
    or equal-weight silently doubles its allocation."""
    from convexity import fetcher, resolver

    monkeypatch.setattr(
        resolver,
        "resolve_symbol",
        lambda e: {"microsoft": "MSFT"}.get(e.strip(), e.strip().upper()),
    )
    monkeypatch.setattr(fetcher, "fetch_one", lambda s: {"symbol": s, "price": 1.0})
    msgs = list(fetcher.stream_quotes(["microsoft", "MSFT", "aapl", "AAPL"]))
    assert msgs[0]["symbols"] == ["MSFT", "AAPL"]
    assert sorted(m["row"]["symbol"] for m in msgs if m["type"] == "row") == ["AAPL", "MSFT"]
