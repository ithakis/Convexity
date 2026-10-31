"""Yahoo sometimes answers a share's ``.info`` without its ``financialData``
module (MU, 2026-10-30: 45 analysts, but the stock window read "no analyst
targets" and n/a fundamentals, and σ/μ dropped the analysts' α). Such an
answer is unknown, never "no analysts": it is asked again, kept briefly
only, and never overwrites a saved row's fundamentals. Synthetic numbers,
well-known public tickers (CLAUDE.md §18); no network."""

import pytest

from convexity import analyst_targets as at
from convexity import cache, fetcher, options_density, persistence
from convexity.helpers import fundamentals_missing

HEALTHY = {"quoteType": "EQUITY", "longName": "Micron Technology, Inc.", "currency": "USD",
           "financialCurrency": "USD", "totalRevenue": 1.3e11, "targetMeanPrice": 150.0,
           "numberOfAnalystOpinions": 45, "returnOnEquity": 0.3, "recommendationMean": 1.4}  # fmt: skip
# What Yahoo sent: identity and multiples, nothing from financialData.
DEGRADED = {"quoteType": "EQUITY", "longName": "Micron Technology, Inc.", "currency": "USD",
            "trailingPE": 12.0}  # fmt: skip


@pytest.fixture(autouse=True)
def _clean_cache():
    keys = [k for k in cache._CACHE if k.startswith(("analyst_targets|", "research_info|"))]
    for k in keys:
        cache._CACHE.pop(k, None)
    yield
    keys = [k for k in cache._CACHE if k.startswith(("analyst_targets|", "research_info|"))]
    for k in keys:
        cache._CACHE.pop(k, None)


def test_the_detector_flags_a_share_without_financial_data_only():
    assert fundamentals_missing(DEGRADED)
    assert not fundamentals_missing(HEALTHY)
    # A secondary line keeps the module, without targets (NVD.DE).
    assert not fundamentals_missing({"quoteType": "EQUITY", "financialCurrency": "USD"})
    assert not fundamentals_missing({"quoteType": "ETF"})  # a fund never has one
    assert not fundamentals_missing({})  # a failed answer is handled elsewhere


class Tk:
    def __init__(self, ticker, apt):
        self.ticker = ticker
        self.analyst_price_targets = apt
        self.upgrades_downgrades = None
        self.splits = None


def test_an_empty_answer_for_a_share_is_unknown_and_kept_briefly(monkeypatch):
    puts = []
    real = at._cache_put
    monkeypatch.setattr(
        at, "_cache_put", lambda k, v, ttl: (puts.append((v, ttl)), real(k, v, ttl))
    )
    assert at.consensus("MU", tk=Tk("MU", {})) is None
    assert puts == [({"v": None, "unknown": True}, at.UNKNOWN_TTL)]
    assert at.unknown("MU") and not at.unknown("NVDA")


def test_a_secondary_line_without_targets_is_a_real_none(monkeypatch):
    puts = []
    monkeypatch.setattr(at, "_cache_put", lambda k, v, ttl: puts.append((v, ttl)))
    assert at.consensus("NVD.DE", tk=Tk("NVD.DE", {"current": 180.0})) is None
    assert puts == [({"v": None}, at.MISS_TTL)]


def test_a_fund_is_never_unknown(monkeypatch):
    puts = []
    monkeypatch.setattr(at, "_cache_put", lambda k, v, ttl: puts.append((v, ttl)))
    assert at.consensus("SPY", tk=Tk("SPY", {}), info={"quoteType": "ETF"}) is None
    assert puts == [({"v": None}, at.MISS_TTL)]


def test_degraded_info_falls_back_to_financial_data_alone():
    """The detail path passes its .info; without fundamentals it is not
    read as "no targets": financialData is asked on its own."""
    b = at.consensus("MU", tk=Tk("MU", {"current": 100.0, "mean": 150.0}), info=DEGRADED)
    assert b and b["mean"] == 150.0


def test_a_cached_miss_never_outranks_a_healthy_answer():
    at.consensus("MU", tk=Tk("MU", {}))
    assert at.unknown("MU")
    b = at.consensus("MU", tk=Tk("MU", {}), info=HEALTHY)
    assert b and b["mean"] == 150.0


# ------------------------------------------------------------- the .info read
class InfoTk:
    def __init__(self, ticker, info):
        self.ticker = ticker
        self.info = info
        self.fast_info = None


def test_safe_info_asks_once_more_and_keeps_the_good_answer(monkeypatch):
    answers = [dict(HEALTHY)]
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: None)
    monkeypatch.setattr(fetcher.yf, "Ticker", lambda s: InfoTk(s, answers.pop(0)))
    info = fetcher._safe_info(InfoTk("MU", dict(DEGRADED)))
    assert info["totalRevenue"] == 1.3e11 and not info.get("_fundamentals_missing")


def test_safe_info_marks_an_answer_that_stays_degraded(monkeypatch):
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: None)
    monkeypatch.setattr(fetcher.yf, "Ticker", lambda s: InfoTk(s, dict(DEGRADED)))
    assert fetcher._safe_info(InfoTk("MU", dict(DEGRADED)))["_fundamentals_missing"]
    # A healthy share and a fund are read once.
    monkeypatch.setattr(fetcher.yf, "Ticker", lambda s: pytest.fail("no retry expected"))
    assert not fetcher._safe_info(InfoTk("MU", dict(HEALTHY))).get("_fundamentals_missing")
    assert not fetcher._safe_info(InfoTk("SPY", {"quoteType": "ETF"})).get("_fundamentals_missing")


def test_a_degraded_research_answer_is_kept_briefly(monkeypatch):
    puts = []
    monkeypatch.setattr(
        fetcher, "_safe_info", lambda tk: {**DEGRADED, "_fundamentals_missing": True}
    )
    monkeypatch.setattr(fetcher.yf, "Ticker", lambda s: None)
    monkeypatch.setattr(fetcher, "_cache_put", lambda k, v, ttl: puts.append(ttl))
    fetcher._research_info("MU")
    assert puts == [fetcher._DEGRADED_TTL]


def test_a_degraded_candidate_never_records_no_research_line(monkeypatch):
    from convexity import symbol_db as sdb

    monkeypatch.setattr(fetcher, "analyst_line", lambda s: None)
    monkeypatch.setattr(sdb, "research_candidates", lambda s: ["NVDA"])
    monkeypatch.setattr(
        fetcher, "_research_info", lambda line: {**DEGRADED, "_fundamentals_missing": True}
    )
    assert fetcher.research_line("NVD.DE") is None
    assert "NVD.DE" not in fetcher._research_load()  # asked again next time


# ------------------------------------------------------------- the saved row
def test_a_degraded_row_keeps_the_last_saved_fundamentals():
    saved = {"symbol": "MU", "price": 90.0, "n_analysts": 45.0, "target_mean_price": 150.0,
             "target_source": "firms", "roe": 0.3, "rec_key": "strong_buy"}  # fmt: skip
    persistence.save_view("Test", "MU", [saved], set_last=False)
    out = {"symbol": "MU", "price": 100.0, "n_analysts": None, "target_mean_price": None,
           "roe": None, "rec_key": None}  # fmt: skip
    fetcher._carry_fundamentals(out, "MU")
    assert out["price"] == 100.0  # this fetch's price stands
    assert out["n_analysts"] == 45.0 and out["target_mean_price"] == 150.0 and out["roe"] == 0.3
    assert out["fundamentals_asof"]
    fresh = {"symbol": "MU", "n_analysts": 44.0}
    fetcher._carry_fundamentals(fresh, "MU")
    assert fresh["n_analysts"] == 44.0  # only gaps are filled


# ------------------------------------------------------------------ σ/μ
def test_density_knows_when_analysts_were_withheld(monkeypatch):
    monkeypatch.setattr(fetcher, "analyst_line", lambda s: None)
    at.consensus("MU", tk=Tk("MU", {}))
    assert options_density._analysts_unknown("MU", "MU")
    assert not options_density._analysts_unknown("NVDA", "NVDA")
    assert options_density.UNKNOWN_TTL < options_density.TTL


def test_a_fund_known_only_to_the_pack_is_never_unknown(monkeypatch):
    from convexity import capm

    monkeypatch.setattr(capm, "is_fund", lambda s: s == "SPY")
    at.consensus("SPY", tk=Tk("SPY", {}))
    assert not at.unknown("SPY")
