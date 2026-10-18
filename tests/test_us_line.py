"""Finnhub's free plan covers US listings only, so the Rec, MSPR and news
columns ask it for the company's US line (symbol_db.us_line). Synthetic
pack of well-known public listings (CLAUDE.md §18)."""

import io
import urllib.error

import pytest

from convexity import finnhub_adapter as fh
from convexity import news_sentiment as ns
from convexity import symbol_db as sdb
from tests.test_symbol_db import ROWS, install

_FIN = "Financial Services"
_EXTRA = [
    # ticker, name, type, exchange, region, sector, industry, mcap, adv, group, home
    ["TTE.PA", "TotalEnergies SE", "stock", "PAR", "fr", "Energy", "Oil & Gas", 1.4e11, 4e8, 90, 1],
    ["TTFNF", "TotalEnergies SE", "stock", "PNK", "us", "Energy", "Oil & Gas", 1.4e11, 9e8, 90, 0],
    ["TTE", "TotalEnergies SE", "stock", "NYQ", "us", "Energy", "Oil & Gas", 1.4e11, 1e8, 90, 0],
    ["REP.MC", "Repsol, S.A.", "stock", "MCE", "es", "Energy", "Oil & Gas", 2e10, 5e7, 91, 1],
    ["REPYY", "Repsol, S.A.", "stock", "OQX", "us", "Energy", "Oil & Gas", 2e10, 3e6, 91, 0],
    ["IWDA.AS", "iShares Core MSCI World", "etf", "AMS", "nl", None, None, None, 1e8, 92, 1],
]


@pytest.fixture()
def db():
    install(ROWS + _EXTRA)
    sdb._US_LEARNED.clear()
    yield
    sdb._US_LEARNED.clear()
    sdb._invalidate()


@pytest.mark.parametrize(
    "symbol, us",
    [
        ("MSF.DE", "MSFT"),  # a US company's German line: its home
        ("NOV.DE", "NVO"),  # a Frankfurt line of a foreign company: the ADR
        ("NOVO-B.CO", "NVO"),
        ("2330.TW", "TSM"),
        ("TTE.PA", "TTE"),  # the exchange line beats a busier pink sheet
        ("REP.MC", "REPYY"),  # an OTC ADR when that is all there is
        ("BNP.PA", None),  # no US line in the pack
        ("IWDA.AS", None),  # a UCITS fund
        ("NVDA", "NVDA"),
        ("BRK-B", "BRK-B"),
        ("ZZZZ", "ZZZZ"),  # not in the pack: a plain ticker is taken as US
        ("ZZZZ.F", None),
    ],
)
def test_us_line(db, symbol, us):
    assert sdb.us_line(symbol) == us


def test_a_missing_adr_is_found_by_name_and_remembered(db, monkeypatch):
    calls = []

    def lookup(q, limit=6, timeout=8.0):
        calls.append(q)
        return [
            sdb.Hit("BNP.PA", "BNP Paribas", "stock", "PAR", "fr", _FIN, None, None, -1, 90.0),
            sdb.Hit("BNPQF", "BNP Paribas", "stock", "OID", None, _FIN, None, None, -1, 90.0),
            sdb.Hit("BNPQY", "BNP Paribas", "stock", "OID", None, _FIN, None, None, -1, 90.0),
            sdb.Hit("BNPX", "BNP Media Group", "stock", "NMS", None, None, None, None, -1, 90.0),
        ]

    monkeypatch.setattr(sdb, "yahoo_lookup", lookup)
    assert sdb.us_line("BNP.PA") is None
    assert sdb.discover_us_line("BNP.PA") == "BNPQY"  # the ADR, not the pink sheet
    assert sdb.us_line("BNP.PA") == "BNPQY"
    monkeypatch.setattr(sdb, "yahoo_lookup", lambda *a, **k: [])
    assert sdb.discover_us_line("HSBA.L") is None
    assert sdb.discover_us_line("HSBA.L") is None
    assert len(calls) == 1  # a miss is not asked again either


@pytest.fixture()
def finnhub(monkeypatch):
    calls = []

    def call(path, params, symbol):
        calls.append(params["symbol"])
        if path == "stock/recommendation":
            return [{"period": "2026-09-01", "buy": 3}, {"period": "2026-08-01", "buy": 2}]
        return {"data": [{"year": 2026, "month": 8, "mspr": 12.5, "change": 10}]}

    monkeypatch.setattr(fh, "FINNHUB_API_KEY", "test")
    monkeypatch.setattr(fh, "_fh_call", call)
    fh._FH_CACHE.clear()
    yield calls
    fh._FH_CACHE.clear()


def test_finnhub_is_asked_for_the_us_line(db, finnhub):
    assert fh.get_recommendation_trend("NOV.DE")[0]["buy"] == 3
    ins = fh.get_insider_sentiment("MSF.DE")
    assert ins["mspr"] == 12.5 and ins["via"] == "MSFT"
    assert "via" not in fh.get_insider_sentiment("MSFT")  # the same cache entry
    assert fh.finnhub_symbol("BRK-B") == "BRK.B"
    assert finnhub == ["NVO", "MSFT"]


def test_no_us_line_means_no_call(db, finnhub):
    assert fh.get_recommendation_trend("BNP.PA") is None
    assert fh.get_insider_sentiment("IWDA.AS") is None
    assert finnhub == []


def test_a_403_is_remembered(db, monkeypatch):
    opened = []

    def urlopen(url, timeout=0):
        opened.append(url)
        raise urllib.error.HTTPError(url, 403, "no access", {}, io.BytesIO(b""))

    monkeypatch.setattr(fh, "FINNHUB_API_KEY", "test")
    monkeypatch.setattr(fh.urllib.request, "urlopen", urlopen)
    fh._FH_CACHE.clear()
    assert fh.get_recommendation_trend("ZZZZ") is None
    assert fh.get_recommendation_trend("ZZZZ") is None
    assert len(opened) == 1
    fh._FH_CACHE.clear()


def test_news_skips_finnhub_without_a_us_line(monkeypatch):
    monkeypatch.setattr(ns, "_fh_call", lambda *a, **k: pytest.fail("Finnhub was asked"))
    monkeypatch.setattr(ns, "_fetch_yf_news", lambda *a, **k: [])
    assert ns.collect_company_news("PHIL.MI", 7, finnhub_symbol="") == ([], True, 0)


def test_yahoo_search_news_backs_up_an_empty_get_news(monkeypatch):
    import time

    import yfinance as yf

    now = int(time.time())

    class Ticker:
        def __init__(self, s):
            pass

        def get_news(self, count, tab):
            return []  # Yahoo's xhr/ncp answers 404: yfinance returns []

    class Search:
        def __init__(self, q, news_count, max_results):
            self.news = [
                {"title": "Coca-Cola raises its outlook", "publisher": "Reuters",
                 "link": "https://example.com/a", "providerPublishTime": now - 3600,
                 "relatedTickers": ["KO", "PEP"]},
                {"title": "Ten stocks to watch", "publisher": "Blog",
                 "link": "https://example.com/b", "providerPublishTime": now - 3600,
                 "relatedTickers": ["PEP"]},
            ]  # fmt: skip

    monkeypatch.setattr(yf, "Ticker", Ticker)
    monkeypatch.setattr(yf, "Search", Search)
    out = ns._fetch_yf_news("KO", 7)
    assert [a["headline"] for a in out] == ["Coca-Cola raises its outlook"]
    assert out[0]["source"] == "Reuters" and out[0]["url"] == "https://example.com/a"


def test_a_european_line_searches_the_company_name(db, monkeypatch):
    import time

    import yfinance as yf

    now, asked = int(time.time()), []

    class Ticker:
        def __init__(self, s):
            pass

        def get_news(self, count, tab):
            return []

    def item(title, tags):
        return {"title": title, "publisher": "Reuters", "link": f"https://example.com/{len(title)}",
                "providerPublishTime": now - 3600, "relatedTickers": tags}  # fmt: skip

    class Search:
        def __init__(self, q, news_count, max_results):
            asked.append(q)
            self.news = [] if q != "repsol" else [
                item("Repsol weighs a stake sale", ["REP.MC", "BP"]),
                item("Repsol ADR volume jumps", ["REPYY"]),
                item("Oil majors to watch", ["BP", "SHEL"]),
            ]  # fmt: skip

    monkeypatch.setattr(yf, "Ticker", Ticker)
    monkeypatch.setattr(yf, "Search", Search)
    out = ns._fetch_yf_news("REP.MC", 7)
    assert sorted(a["headline"] for a in out) == [
        "Repsol ADR volume jumps",
        "Repsol weighs a stake sale",
    ]
    assert asked == ["REP.MC", "repsol"]
    asked.clear()
    assert ns._fetch_yf_news("IWDA.AS", 7) == [] and asked == ["IWDA.AS"]  # a fund: no name search


def test_an_otc_adr_with_no_region_is_a_us_line(db):
    # Yahoo's screener leaves `region` empty on its OTC "OID" board, where
    # sponsored ADRs like NSRGY live; they used to be invisible here.
    install(ROWS + _EXTRA + [
        ["NESN.SW", "Nestlé S.A.", "stock", "EBS", "ch", "Consumer Defensive", None, 2.4e11, 4e8, 93, 1],
        ["NSRGY", "Nestlé S.A.", "stock", "OID", None, "Consumer Defensive", None, 2.4e11, 2e7, 93, 0],
    ])  # fmt: skip
    assert sdb.us_line("NESN.SW") == "NSRGY"


@pytest.mark.parametrize(
    "symbol, cands",
    [
        ("NOV.DE", ["NOVO-B.CO", "NVO"]),  # home first, then the US line
        ("NOVO-B.CO", ["NVO"]),  # a home line: only its US line
        ("NVO", ["NOVO-B.CO"]),
        ("TTE.PA", ["TTE"]),
        ("IWDA.AS", []),  # a fund: no research line
        ("ZZZZ", []),  # not in the pack
    ],
)
def test_research_candidates(db, symbol, cands):
    assert sdb.research_candidates(symbol) == cands
