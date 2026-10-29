"""fetcher's research line: a secondary listing (NOV.DE, CCC3.DE)
has no analysts on Yahoo; the same company's best-covered line lends them,
its targets scaled by the two prices. Synthetic numbers, well-known public
tickers (CLAUDE.md §18); no network."""

import json

import pytest

from convexity import fetcher
from convexity import symbol_db as sdb

INFO = {
    "NOVO-B.CO": {"longName": "Novo Nordisk A/S", "currency": "DKK", "last_price": 300.0,
                  "numberOfAnalystOpinions": 24, "targetMeanPrice": 330.0, "targetLowPrice": 240.0,
                  "targetHighPrice": 450.0, "targetMedianPrice": 320.0, "recommendationMean": 2.5,
                  "recommendationKey": "hold", "sector": "Healthcare", "industry": "Drug Manufacturers",
                  "trailingPE": 10.0, "marketCap": 1.2e12},
    "NVO": {"longName": "Novo Nordisk A/S", "currency": "USD", "last_price": 45.0,
            "numberOfAnalystOpinions": 12, "targetMeanPrice": 50.0},
}  # fmt: skip


@pytest.fixture()
def stubs(monkeypatch):
    calls = []

    def info(line):
        calls.append(line)
        return dict(INFO.get(line, {}))

    monkeypatch.setattr(fetcher, "_research_info", info)
    monkeypatch.setattr(
        sdb, "research_candidates", lambda s: ["NOVO-B.CO", "NVO"] if s == "NOV.DE" else []
    )
    monkeypatch.setattr(fetcher, "_research_recs", lambda line: [])
    monkeypatch.setattr(fetcher, "_earnings_surprise_yf", lambda tk, s: None)
    monkeypatch.setattr(fetcher, "usd_per_unit", lambda c: {"DKK": 0.15, "EUR": 1.1}.get(c))
    monkeypatch.setattr(fetcher.yf, "Ticker", lambda s: None)
    return calls


def test_the_line_with_most_analysts_wins_and_is_remembered(stubs):
    assert fetcher.research_line("NOV.DE") == "NOVO-B.CO"
    n = len(stubs)
    assert fetcher.research_line("NOV.DE") == "NOVO-B.CO"
    assert len(stubs) == n  # from state/research_lines.json, no new reads
    store = json.loads(fetcher._research_store_path().read_text())
    assert store["NOV.DE"][1] == "NOVO-B.CO"
    assert fetcher.research_line("SAP.DE") is None  # no other line: a remembered miss


def test_a_thin_row_borrows_analysts_scaled_to_its_price(stubs):
    row = {"symbol": "NOV.DE", "price": 40.0, "currency": "EUR", "sector": "", "industry": "",
           "market_cap": None, "market_cap_usd": None, "n_analysts": None, "target_mean_price": None,
           "pe_ratio": 9.9}  # fmt: skip
    fetcher._apply_research(row, "NOV.DE")
    k = 40.0 / 300.0  # own price / research price: FX and units in one ratio
    assert row["target_mean_price"] == pytest.approx(330.0 * k)
    assert row["target_low"] == pytest.approx(240.0 * k)
    assert row["target_mean_price"] / row["price"] == pytest.approx(330.0 / 300.0)  # same upside
    assert row["n_analysts"] == 24 and row["rec_key"] == "hold"
    assert row["research_symbol"] == "NOVO-B.CO" and row["research_analysts"] is True
    assert row["sector"] == "Healthcare" and row["industry"] == "Drug Manufacturers"
    assert row["pe_ratio"] == 9.9  # the listing's own value is never replaced
    assert row["market_cap_usd"] == pytest.approx(1.2e12 * 0.15)


def test_a_covered_row_is_left_alone(stubs):
    row = {"symbol": "NOV.DE", "price": 40.0, "sector": "Healthcare", "market_cap_usd": 1.0,
           "n_analysts": 3, "target_mean_price": 44.0}  # fmt: skip
    fetcher._apply_research(row, "NOV.DE")
    assert "research_symbol" not in row and not stubs


# ------------------------------------------- the primary line's analysts win
# (2026-10-29) A listing that is not its company's primary line (a German line
# of a US company, a US receipt of a London company) shows the primary line's
# analysts, scaled, even when Yahoo gives it some of its own.

PRIMARY_INFO = {
    "WMT": {"longName": "Walmart Inc.", "currency": "USD", "last_price": 100.0,
            "numberOfAnalystOpinions": 40, "targetMeanPrice": 112.0, "targetLowPrice": 90.0,
            "targetHighPrice": 130.0, "targetMedianPrice": 111.0, "recommendationMean": 1.8,
            "recommendationKey": "buy", "sector": "Consumer Defensive"},
    "THIN": {"longName": "Thin Co", "last_price": 10.0, "numberOfAnalystOpinions": 2,
             "targetMeanPrice": 11.0},
}  # fmt: skip


@pytest.fixture()
def primary(monkeypatch):
    monkeypatch.setattr(fetcher, "_research_info", lambda line: dict(PRIMARY_INFO.get(line, {})))
    monkeypatch.setattr(sdb, "research_candidates", lambda s: [])
    monkeypatch.setattr(fetcher, "_research_recs", lambda line: [])
    monkeypatch.setattr(fetcher, "_earnings_surprise_yf", lambda tk, s: None)
    monkeypatch.setattr(fetcher.yf, "Ticker", lambda s: None)
    lines = {"WMT.DE": "WMT", "THN.DE": "THIN"}
    monkeypatch.setattr(fetcher, "_primary", lambda s: lines.get(s.upper(), s.upper()))


def test_the_primary_line_replaces_a_secondary_listings_own_analysts(primary):
    row = {"symbol": "WMT.DE", "price": 90.0, "currency": "EUR", "sector": "Consumer Defensive",
           "market_cap_usd": 1.0, "n_analysts": 1, "target_mean_price": 200.0,
           "rating_dist": {"buy": 1}}  # fmt: skip
    assert fetcher.analyst_line("WMT.DE") == "WMT"
    assert fetcher.research_line("WMT.DE") == "WMT"
    fetcher._apply_research(row, "WMT.DE")
    assert row["n_analysts"] == 40 and row["research_symbol"] == "WMT"
    assert row["target_mean_price"] == pytest.approx(112.0 * 0.9)  # same upside as WMT
    assert row["rating_dist"] is None  # the one firm's split is not the primary line's


def test_a_thin_primary_line_leaves_own_analysts_alone(primary):
    assert fetcher.analyst_line("THN.DE") is None  # 2 analysts: under the minimum
    row = {"symbol": "THN.DE", "price": 9.0, "sector": "X", "market_cap_usd": 1.0,
           "n_analysts": 5, "target_mean_price": 10.0}  # fmt: skip
    fetcher._apply_research(row, "THN.DE")
    assert row["n_analysts"] == 5 and row["target_mean_price"] == 10.0


def test_a_primary_line_is_its_own_analyst_line(primary):
    assert fetcher.analyst_line("WMT") is None
