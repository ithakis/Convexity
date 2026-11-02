"""Yahoo sometimes answers a share's ``.info`` without its ``financialData``
module (MU, 2026-10-30: 45 analysts, but the stock window read "no analyst
targets" and n/a fundamentals, and σ/μ dropped the analysts' α). Such an
answer is unknown, never "no analysts": it is asked again, kept briefly
only, and never overwrites a saved row's fundamentals. Synthetic numbers,
well-known public tickers (CLAUDE.md §18); no network."""

import pytest

from convexity import analyst_targets as at
from convexity import cache, fetcher, helpers, options_density, persistence
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


# --------------------------------------------------- an .info that said nothing
FULL_TARGETS = {"current": 100.0, "mean": 130.0, "median": 128.0, "low": 90.0, "high": 170.0}


@pytest.mark.parametrize("info", [{}, {"market_cap": 1e9, "currency": "USD", "last_price": 100.0}])
def test_a_failed_info_is_never_read_as_no_analysts(info):
    # tk.info raised, so σ/μ passes {} (or _safe_info's fast_info gaps): the
    # targets are still asked for, not recorded as "none" for half an hour.
    block = at.consensus("MU", tk=Tk("MU", FULL_TARGETS), info=info)
    assert block and block["mean"] == 130.0
    cache._CACHE.pop("analyst_targets|MU", None)
    assert at.consensus("MU", tk=Tk("MU", {}), info=info) is None
    assert at.unknown("MU")


def test_a_real_answer_without_targets_is_still_a_real_none():
    info = {"quoteType": "EQUITY", "financialCurrency": "USD", "totalRevenue": 1e9}
    assert at.consensus("MU", tk=Tk("MU", {}), info=info) is None
    assert not at.unknown("MU")


# ------------------------------------------------------- the research line
def _research(monkeypatch, infos):
    from convexity import symbol_db as sdb

    monkeypatch.setattr(fetcher, "analyst_line", lambda s: None)
    monkeypatch.setattr(sdb, "research_candidates", lambda s: list(infos))
    monkeypatch.setattr(fetcher, "_research_info", lambda line: infos[line])


def test_one_degraded_line_blocks_the_no_research_line_record(monkeypatch):
    # MU has the analysts but didn't say; its other line said it has none.
    _research(monkeypatch, {"MU": {**DEGRADED, "_fundamentals_missing": True},
                            "MU.MX": {**HEALTHY, "numberOfAnalystOpinions": 0}})  # fmt: skip
    assert fetcher.research_line("MTE1.DE") is None
    assert "MTE1.DE" not in fetcher._research_load()
    # Yahoo recovers: the answer is there at once, not a day later.
    _research(monkeypatch, {"MU": HEALTHY, "MU.MX": {**HEALTHY, "numberOfAnalystOpinions": 0}})
    assert fetcher.research_line("MTE1.DE") == "MU"
    assert fetcher._research_load()["MTE1.DE"][1] == "MU"


def test_a_healthy_line_with_fewer_analysts_is_used_but_not_recorded(monkeypatch):
    _research(monkeypatch, {"MU": {**DEGRADED, "_fundamentals_missing": True},
                            "MU.MX": {**HEALTHY, "numberOfAnalystOpinions": 3}})  # fmt: skip
    assert fetcher.research_line("MTE2.DE") == "MU.MX"
    assert "MTE2.DE" not in fetcher._research_load()


def test_an_empty_answer_blocks_the_record_too(monkeypatch):
    _research(monkeypatch, {"MU": {}, "MU.MX": {**HEALTHY, "numberOfAnalystOpinions": 0}})
    assert fetcher.research_line("MTE3.DE") is None
    assert "MTE3.DE" not in fetcher._research_load()


# ---------------------------------------- a line that never carries the module
class Info:
    """A yf.Ticker with a fixed .info and nothing else."""

    answers: list = []

    def __init__(self, ticker):
        self.ticker = ticker
        self.fast_info = None

    @property
    def info(self):
        Info.asks += 1
        return dict(Info.answers[min(Info.asks - 1, len(Info.answers) - 1)])


@pytest.fixture
def info_ticker(monkeypatch):
    monkeypatch.setattr(fetcher.yf, "Ticker", Info)
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: None)
    helpers._THIN_RUN.clear()
    Info.asks = 0
    Info.answers = [DEGRADED]
    yield
    helpers._THIN_RUN.clear()


def test_only_the_first_answer_of_a_run_is_asked_twice(info_ticker):
    assert fetcher._safe_info(Info("KDP.MX"))["_fundamentals_missing"]
    assert Info.asks == 2  # the retry
    assert fetcher._safe_info(Info("KDP.MX"))["_fundamentals_missing"]
    assert Info.asks == 3  # no second retry, no sleep on every fetch


def _thin_run(monkeypatch, symbol, answers=5, every=2 * 3600.0):
    """``answers`` answers without the module, ``every`` seconds apart."""
    t = [1000.0]
    monkeypatch.setattr(helpers.time, "time", lambda: t[0])
    for i in range(answers):
        if i:
            t[0] += every
        got = fetcher._safe_info(Info(symbol))
    return t, got


def test_a_line_answering_so_for_hours_is_the_line(info_ticker, monkeypatch):
    _thin_run(monkeypatch, "KDP.MX")
    assert helpers.thin_line("KDP.MX")
    # Now it is the line: no flag, so its "no analysts" is cached as the truth.
    assert not fetcher._safe_info(Info("KDP.MX")).get("_fundamentals_missing")
    assert at.consensus("KDP.MX", tk=Tk("KDP.MX", {}), info=DEGRADED) is None
    assert not at.unknown("KDP.MX")
    cache._CACHE.pop("analyst_targets|KDP.MX", None)
    assert at.consensus("KDP.MX", tk=Tk("KDP.MX", {})) is None
    assert not at.unknown("KDP.MX")


@pytest.mark.parametrize(("answers", "every"), [
    (3, 1800.0),  # half an hour: a long lapse
    (20, 300.0),  # 95 minutes of asking: a very long lapse
    (2, 8 * 3600.0),  # two lapses a working day apart
    (6, 7 * 3600.0),  # a name asked rarely: each gap starts over
])  # fmt: skip
def test_a_lapse_is_not_a_thin_line(info_ticker, monkeypatch, answers, every):
    _, got = _thin_run(monkeypatch, "MU", answers, every)
    assert got.get("_fundamentals_missing")
    assert not helpers.thin_line("MU")


def test_a_stale_thin_verdict_expires(info_ticker, monkeypatch):
    t, _ = _thin_run(monkeypatch, "MU")
    t[0] += helpers._THIN_GAP_S + 1
    assert not helpers.thin_line("MU") and helpers.fundamentals_retry_due("MU")


def test_a_good_answer_ends_the_run(info_ticker, monkeypatch):
    _thin_run(monkeypatch, "MU")
    Info.asks, Info.answers = 0, [HEALTHY]
    assert not fetcher._safe_info(Info("MU")).get("_fundamentals_missing")
    assert not helpers.thin_line("MU") and helpers.fundamentals_retry_due("MU")


# ------------------------------------------------- an .info that raised (a 429)
class Raises(Info):
    @property
    def info(self):
        Info.asks += 1
        raise RuntimeError("Too Many Requests")


@pytest.mark.parametrize("cls", [Raises])
def test_a_failed_info_is_flagged_not_a_row_with_no_analysts(info_ticker, cls):
    got = fetcher._safe_info(cls("MU"))
    assert got["_fundamentals_missing"]
    assert Info.asks == 1  # a throttled ask isn't repeated at once


def test_failed_infos_never_add_up_to_a_thin_line(info_ticker, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(helpers.time, "time", lambda: t[0])
    for _ in range(10):
        t[0] += 3600.0
        assert fetcher._safe_info(Raises("MU"))["_fundamentals_missing"]
    assert not helpers.thin_line("MU") and "MU" not in helpers._THIN_RUN


def test_a_failed_info_keeps_the_saved_rows_fundamentals(info_ticker, monkeypatch):
    persistence.save_view("Test", "MU", [{"symbol": "MU", "price": 90.0, "n_analysts": 45.0,
                                          "target_mean_price": 150.0, "operating_margin": 0.3}],
                          set_last=False)  # fmt: skip
    info = fetcher._safe_info(Raises("MU"))
    out = {"symbol": "MU", "price": 100.0, "n_analysts": None, "target_mean_price": None,
           "operating_margin": None}  # fmt: skip
    if info.get("_fundamentals_missing"):
        fetcher._carry_fundamentals(out, "MU")
    assert out["n_analysts"] == 45.0 and out["operating_margin"] == 0.3 and out["price"] == 100.0


def test_a_fund_answering_normally_is_not_flagged(info_ticker):
    Info.answers = [{"quoteType": "ETF", "longName": "SPDR S&P 500 ETF Trust"}]
    assert not fetcher._safe_info(Info("SPY")).get("_fundamentals_missing")


def test_the_analysts_unknown_miss_is_kept_briefly_in_the_window():
    # Mutation check: the window's payload must not outlive an unknown miss.
    assert at.UNKNOWN_TTL < at.MISS_TTL < at.TTL
