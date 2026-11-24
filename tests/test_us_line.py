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
    [
        "SAN.PA",
        "Sanofi",
        "stock",
        "PAR",
        "fr",
        "Healthcare",
        "Drug Manufacturers",
        1.4e11,
        4e8,
        90,
        1,
    ],
    [
        "SNYNF",
        "Sanofi",
        "stock",
        "PNK",
        "us",
        "Healthcare",
        "Drug Manufacturers",
        1.4e11,
        9e8,
        90,
        0,
    ],
    ["SNY", "Sanofi", "stock", "NMS", "us", "Healthcare", "Drug Manufacturers", 1.4e11, 1e8, 90, 0],
    [
        "IBE.MC",
        "Iberdrola, S.A.",
        "stock",
        "MCE",
        "es",
        "Utilities",
        "Utilities—Diversified",
        2e10,
        5e7,
        91,
        1,
    ],
    [
        "IBDRY",
        "Iberdrola, S.A.",
        "stock",
        "OQX",
        "us",
        "Utilities",
        "Utilities—Diversified",
        2e10,
        3e6,
        91,
        0,
    ],
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
        ("GE9.DE", "GMAB"),  # a Frankfurt line of a foreign company: the ADR
        ("GMAB.CO", "GMAB"),
        ("2330.TW", "TSM"),
        ("SAN.PA", "SNY"),  # the exchange line beats a busier pink sheet
        ("IBE.MC", "IBDRY"),  # an OTC ADR when that is all there is
        ("ACA.PA", None),  # no US line in the pack
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
            sdb.Hit("ACA.PA", "Credit Agricole", "stock", "PAR", "fr", _FIN, None, None, -1, 90.0),
            sdb.Hit("CRARF", "Credit Agricole", "stock", "OID", None, _FIN, None, None, -1, 90.0),
            sdb.Hit("CRARY", "Credit Agricole", "stock", "OID", None, _FIN, None, None, -1, 90.0),
            sdb.Hit("CRAX", "Credit Media Group", "stock", "NMS", None, None, None, None, -1, 90.0),
        ]

    monkeypatch.setattr(sdb, "yahoo_lookup", lookup)
    assert sdb.us_line("ACA.PA") is None
    assert sdb.discover_us_line("ACA.PA") == "CRARY"  # the ADR, not the pink sheet
    assert sdb.us_line("ACA.PA") == "CRARY"
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
    assert fh.get_recommendation_trend("GE9.DE")[0]["buy"] == 3
    ins = fh.get_insider_sentiment("MSF.DE")
    assert ins["mspr"] == 12.5 and ins["via"] == "MSFT"
    assert "via" not in fh.get_insider_sentiment("MSFT")  # the same cache entry
    assert fh.finnhub_symbol("BRK-B") == "BRK.B"
    assert finnhub == ["GMAB", "MSFT"]


def test_no_us_line_means_no_call(db, finnhub):
    assert fh.get_recommendation_trend("ACA.PA") is None
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
    assert ns.collect_company_news("BC.MI", 7, finnhub_symbol="") == ([], True, 0)


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
                {"title": "PepsiCo raises its outlook", "publisher": "Reuters",
                 "link": "https://example.com/a", "providerPublishTime": now - 3600,
                 "relatedTickers": ["PEP", "KDP"]},
                {"title": "Ten stocks to watch", "publisher": "Blog",
                 "link": "https://example.com/b", "providerPublishTime": now - 3600,
                 "relatedTickers": ["KDP"]},
            ]  # fmt: skip

    monkeypatch.setattr(yf, "Ticker", Ticker)
    monkeypatch.setattr(yf, "Search", Search)
    out = ns._fetch_yf_news("PEP", 7)
    assert [a["headline"] for a in out] == ["PepsiCo raises its outlook"]
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
            self.news = [] if q != "iberdrola" else [
                item("Iberdrola weighs a stake sale", ["IBE.MC", "NEE"]),
                item("Iberdrola ADR volume jumps", ["IBDRY"]),
                item("Utilities to watch", ["NEE", "DUK"]),
            ]  # fmt: skip

    monkeypatch.setattr(yf, "Ticker", Ticker)
    monkeypatch.setattr(yf, "Search", Search)
    out = ns._fetch_yf_news("IBE.MC", 7)
    assert sorted(a["headline"] for a in out) == [
        "Iberdrola ADR volume jumps",
        "Iberdrola weighs a stake sale",
    ]
    assert asked == ["IBE.MC", "iberdrola"]
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
        ("GE9.DE", ["GMAB.CO", "GMAB"]),  # home first, then the US line
        ("GMAB.CO", ["GMAB"]),  # a home line: only its US line
        ("GMAB", ["GMAB.CO"]),
        ("SAN.PA", ["SNY"]),
        ("IWDA.AS", []),  # a fund: no research line
        ("ZZZZ", []),  # not in the pack
    ],
)
def test_research_candidates(db, symbol, cands):
    assert sdb.research_candidates(symbol) == cands


@pytest.mark.parametrize(
    "symbol, tickers",
    [
        ("GE9.DE", ["GMAB", "GE9.DE", "GMAB.CO"]),  # never the bare GE9
        ("SAN.PA", ["SNY", "SAN.PA"]),
        ("IBE.MC", ["IBDRY", "IBE.MC"]),
        ("MSF.DE", ["MSFT", "MSF.DE"]),
        ("ACA.PA", ["ACA.PA"]),  # no US line: its own full ticker, nothing shorter
        ("IWDA.AS", ["IWDA.AS"]),
        ("NVDA", ["NVDA"]),
        ("BRK-B", ["BRK-B"]),
        ("ZZZZ", ["ZZZZ"]),
        ("ZZZZ.F", ["ZZZZ.F"]),  # not in the pack: still not cut to ZZZZ
        ("", []),
    ],
)
def test_logo_tickers_name_the_company(db, symbol, tickers):
    assert sdb.logo_tickers(symbol) == tickers


def test_logo_tickers_without_a_pack(tmp_path, monkeypatch):
    monkeypatch.setattr(sdb, "db_path", lambda: tmp_path / "missing.sqlite")
    assert sdb.logo_tickers("AI.PA") == ["AI.PA"]
    assert sdb.logo_tickers("AAPL") == ["AAPL"]
