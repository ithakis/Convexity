"""Short listings no longer cut a basket's history (analytics.extend_history,
the ragged-start basket) and Yahoo's live Lookup covers names the symbol pack
lacks (symbol_db.yahoo_lookup, company search). Offline: prices, FX and both
lookups are stubbed; public tickers and made-up prices only."""

import numpy as np
import pandas as pd
import pytest

from convexity import analytics as A
from convexity import fx, investments, search
from convexity import symbol_db as sdb
from convexity.symbol_db import Hit

IDX = pd.bdate_range(end="2026-09-25", periods=300)


def _walk(seed, start=100.0):
    rng = np.random.default_rng(seed)
    return start * np.cumprod(1 + rng.normal(0.0004, 0.01, len(IDX)))


@pytest.fixture(autouse=True)
def _fresh_results():
    """Results are cached by symbols and weights; these tests reuse names."""
    from convexity import cache

    cache._CACHE.clear()
    yield
    cache._CACHE.clear()


@pytest.fixture
def market(monkeypatch):
    """YNG (home, USD) trades all along; YNG.DE (EUR) only the last 60 days,
    at EUR = 1.10 USD every day."""
    home = pd.Series(_walk(1), index=IDX)
    young = (home / 1.10).where(IDX >= IDX[-60])
    wide = pd.DataFrame({"YNG": home, "YNG.DE": young, "AAA": _walk(2), "BBB": _walk(3)})
    monkeypatch.setattr(
        A, "_bulk_close", lambda syms, period, adjusted=True: wide[[s for s in syms if s in wide]]
    )
    monkeypatch.setattr(
        fx,
        "fx_multipliers",
        lambda ccys, base, dates: {
            c: np.full(len(dates), 1 / 1.10 if base == "EUR" else 1.0) for c in ccys
        },
    )
    monkeypatch.setattr(investments, "_fetch_quote_ccy", lambda s: "USD" if "." not in s else "EUR")
    A._PROXY.clear()
    grp = {"YNG.DE": Hit("YNG.DE", "Young Co, Inc.", "stock", "GER", "de", None, None, None, 7, 100.0),
           "YNG": Hit("YNG", "Young Co, Inc.", "stock", "NMS", "us", None, None, None, 7, 100.0)}  # fmt: skip
    monkeypatch.setattr(sdb, "get", lambda t: grp.get(t))
    monkeypatch.setattr(sdb, "listings", lambda g: list(grp.values()) if g == 7 else [])
    monkeypatch.setattr(sdb, "yahoo_lookup", lambda q, **k: [])
    return wide


def test_a_short_line_is_extended_by_the_same_company_elsewhere(market):
    out, done = A.extend_history(market[["YNG.DE"]], ["YNG.DE"], {"YNG.DE": "EUR"}, {}, "2y")
    assert done["YNG.DE"][0] == "YNG"
    s = out["YNG.DE"]
    assert s.notna().all()
    # The company's returns throughout, in euros, and no jump at the join.
    np.testing.assert_allclose(s.to_numpy(), (market["YNG"] / 1.10).to_numpy(), rtol=1e-9)


def test_a_line_with_no_other_listing_is_left_alone(market, monkeypatch):
    monkeypatch.setattr(sdb, "listings", lambda g: [])
    out, done = A.extend_history(market[["YNG.DE"]], ["YNG.DE"], {"YNG.DE": "EUR"}, {}, "2y")
    assert done == {} and out["YNG.DE"].isna().sum() == len(IDX) - 60


def _holed(market):
    """YNG.DE with junk early prices, then no close for 100 rows (Yahoo's
    NOT.DE has none from Sep 2019 to Jan 2025), then the real line."""
    line = market["YNG"] / 1.10
    junk = pd.Series(0.5, index=IDX).where(IDX < IDX[100])
    return line.where(IDX >= IDX[200]).fillna(junk)


def test_a_hole_in_a_line_is_filled_by_the_same_company_elsewhere(market):
    frame = pd.DataFrame({"YNG.DE": _holed(market), "AAA": market["AAA"]})
    out, done = A.extend_history(frame, ["YNG.DE", "AAA"], {"YNG.DE": "EUR"}, {}, "2y")
    assert done["YNG.DE"] == ("YNG", str(IDX[200])[:10])
    # The junk before the hole is gone too: the company's own prices throughout.
    np.testing.assert_allclose(
        out["YNG.DE"].to_numpy(), (market["YNG"] / 1.10).to_numpy(), rtol=1e-9
    )


def test_a_hole_with_no_stand_in_keeps_the_lines_own_data(market, monkeypatch):
    monkeypatch.setattr(sdb, "listings", lambda g: [])
    frame = pd.DataFrame({"YNG.DE": _holed(market), "AAA": market["AAA"]})
    out, done = A.extend_history(frame, ["YNG.DE", "AAA"], {"YNG.DE": "EUR"}, {}, "2y")
    assert done == {}
    pd.testing.assert_series_equal(out["YNG.DE"], frame["YNG.DE"])


def test_a_stand_ins_own_hole_starts_its_usable_stretch():
    idx = pd.bdate_range("2019-01-01", "2026-01-30")
    col = pd.Series(np.linspace(10, 20, len(idx)), index=idx)
    col = col[(col.index < "2019-09-20") | (col.index >= "2025-01-02")]
    out = A._clean_tail(col)
    assert str(out.index[0])[:10] == "2025-01-02" and out.iloc[-1] == col.iloc[-1]


def test_a_short_gap_is_not_a_hole(market):
    # A month's suspension is the market, not missing data: left alone.
    line = (market["YNG"] / 1.10).where((IDX < IDX[150]) | (IDX >= IDX[170]))
    frame = pd.DataFrame({"YNG.DE": line, "AAA": market["AAA"]})
    out, done = A.extend_history(frame, ["YNG.DE", "AAA"], {"YNG.DE": "EUR"}, {}, "2y")
    assert done == {} and out["YNG.DE"].isna().sum() == 20


def test_a_hole_before_the_book_held_the_line_needs_no_stand_in(market):
    frame = pd.DataFrame({"YNG.DE": _holed(market), "AAA": market["AAA"]})
    need = {"YNG.DE": str(IDX[250])[:10]}
    _, done = A.extend_history(frame, ["YNG.DE", "AAA"], {"YNG.DE": "EUR"}, {}, "2y", need=need)
    assert done == {}
    need = {"YNG.DE": str(IDX[120])[:10]}
    _, done = A.extend_history(frame, ["YNG.DE", "AAA"], {"YNG.DE": "EUR"}, {}, "2y", need=need)
    assert done["YNG.DE"][0] == "YNG"


def _analyze(monkeypatch, wide, rows, weights):
    monkeypatch.setattr(
        A, "_bulk_close", lambda syms, period, adjusted=True: wide[[s for s in syms if s in wide]]
    )
    monkeypatch.setattr(A, "_apply_fx_to_closes", lambda df, *a, **k: df)
    monkeypatch.setattr(A, "_analyst_for", lambda s, row=None: {})
    return A.analyze_portfolios_multi(rows, {"w": weights}, "1Y")["w"]


def test_the_portfolio_keeps_its_full_history_with_a_spliced_line(market, monkeypatch):
    a = _analyze(monkeypatch, market, [{"symbol": "AAA"}, {"symbol": "YNG.DE", "name": "Young Co"}],
                 {"AAA": 1, "YNG.DE": 1})  # fmt: skip
    first = pd.Timestamp(a["series"]["portfolio"][0][0], unit="ms")
    assert first < IDX[-60] - pd.Timedelta(days=200)
    assert any("YNG" in w for w in a["warnings"])


def test_a_young_holding_with_no_proxy_does_not_cut_the_others(market, monkeypatch):
    monkeypatch.setattr(sdb, "listings", lambda g: [])
    a = _analyze(monkeypatch, market, [{"symbol": "AAA"}, {"symbol": "BBB"}, {"symbol": "YNG.DE"}],
                 {"AAA": 1, "BBB": 1, "YNG.DE": 1})  # fmt: skip
    pts = a["series"]["portfolio"]
    assert pd.Timestamp(pts[0][0], unit="ms") < IDX[-60] - pd.Timedelta(days=200)
    assert any("has prices only since" in w for w in a["warnings"])
    # Before YNG.DE lists, the basket is AAA and BBB at half each.
    early = market[["AAA", "BBB"]].pct_change().mean(axis=1)
    t1 = pd.Timestamp(pts[1][0], unit="ms")
    assert pts[1][1] / pts[0][1] - 1 == pytest.approx(early.loc[t1], abs=1e-12)
    # The parts still add up to the whole.
    parts = sum(c["contribution"] for c in a["contribution"])
    assert parts == pytest.approx(a["stats"]["total_return"], abs=1e-6)


def test_holdings_that_start_together_change_nothing(market, monkeypatch):
    a = _analyze(monkeypatch, market, [{"symbol": "AAA"}, {"symbol": "BBB"}], {"AAA": 1, "BBB": 1})
    port = pd.Series([v for _, v in a["series"]["portfolio"]])
    want = market[["AAA", "BBB"]].pct_change().fillna(0).mean(axis=1).add(1).cumprod()
    want = want.iloc[-len(port) :]
    np.testing.assert_allclose(port.to_numpy(), (100 * want / want.iloc[0]).to_numpy(), rtol=1e-12)
    assert not any("only since" in w for w in a["warnings"])


# ------------------------------------------------------------ company search


def _yh(t, name, ex):
    return Hit(t, name, "stock", ex, None, None, None, None, sdb.YAHOO_GROUP, 100.0)


def test_search_falls_back_to_yahoo_for_a_name_the_pack_lacks(monkeypatch):
    search._CACHE.clear()
    monkeypatch.setattr(sdb, "lookup", lambda q, **k: [])
    monkeypatch.setattr(sdb, "status", lambda: {"installed": True})
    live = [_yh("RICHT.BD", "Richter Gedeon Chemical Factory", "BUD"),
            _yh("RIG2.F", "Richter Gedeon Chemical Factory", "FRA")]  # fmt: skip
    monkeypatch.setattr(sdb, "yahoo_lookup", lambda q, **k: live)
    monkeypatch.setattr(search, "_fill_values", lambda cards, q: None)
    out = search.run(search.empty("richter"))
    assert [c["ticker"] for c in out["results"]] == ["RICHT.BD"]
    assert out["results"][0]["alternates"][0]["ticker"] == "RIG2.F"
    assert "Yahoo" in out["results"][0]["why"]


def test_a_good_pack_match_does_not_ask_yahoo(monkeypatch):
    search._CACHE.clear()
    h = Hit("OTP.BD", "OTP Bank Nyrt.", "stock", "BUD", "hu", None, None, None, 3, 94.0)
    monkeypatch.setattr(sdb, "lookup", lambda q, **k: [h])
    monkeypatch.setattr(sdb, "top", lambda g, ex=(): h)
    monkeypatch.setattr(sdb, "alternates", lambda g, exclude="": [])
    monkeypatch.setattr(search, "_fill_values", lambda cards, q: None)

    def boom(*a, **k):
        raise AssertionError("asked Yahoo for a name the pack has")

    monkeypatch.setattr(sdb, "yahoo_lookup", boom)
    assert [c["ticker"] for c in search.run(search.empty("otp bank"))["results"]] == ["OTP.BD"]


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [("Iberdrola, S.A.", "Iberdrola", True), ("Richter Gedeon", "Richter Gedeon Chemical Factory", True),
     ("Apple Inc.", "Apple Hospitality REIT, Inc.", False), ("Airbus SE", "Airbus", True),
     ("Shell plc", "Shell plc", True), ("", "Shell", False),
     ("Richter Gedeon Vegyeszeti Gyar", "Richter Gedeon Chemical Factory", True),
     ("First Citizens BancShares", "First Republic Bank", False),
     ("Acme Acquisition Corp II", "Acme Acquisition Corp. III", False),
     ("Acme Acquisition Corp II", "Acme Acquisition Corp. II", True),
     ("Acme Acquisition Corp", "Acme Acquisition Corp. III", False)],
)  # fmt: skip
def test_same_company(a, b, same):
    assert sdb.same_company(a, b) is same


def test_a_stand_ins_bad_old_data_is_never_spliced_in(market, monkeypatch):
    bad = market.copy()
    bad.loc[IDX[50], "YNG"] = -5.0  # a negative close, as Yahoo has for RIG2.F in 2009
    monkeypatch.setattr(
        A, "_bulk_close", lambda syms, period, adjusted=True: bad[[s for s in syms if s in bad]]
    )
    out, done = A.extend_history(bad[["YNG.DE"]], ["YNG.DE"], {"YNG.DE": "EUR"}, {}, "2y")
    s = out["YNG.DE"]
    assert done and s.loc[: IDX[51]].isna().all() and (s.dropna() > 0).all()
    np.testing.assert_allclose(
        s.loc[IDX[52] :].to_numpy(), (market["YNG"].loc[IDX[52] :] / 1.10).to_numpy(), rtol=1e-9
    )


def test_a_ticker_nobody_knows_is_not_fetched_twice(market, monkeypatch):
    asked = []

    def fetch(syms, period, adjusted=True):
        asked.append((tuple(syms), period))
        return market[[s for s in syms if s in market]]

    monkeypatch.setattr(A, "_bulk_close", fetch)
    A.extend_history(market[["AAA"]], ["AAA", "NOPE.BD"], {}, {}, "2y")
    assert not any("NOPE.BD" in s for s, _ in asked)


@pytest.mark.parametrize(
    ("a", "b"),
    [("Bank of America Corporation", "Bank of Ireland Group plc"),
     ("China Resources Beverage", "China Resources Land Limited"),
     ("Grupo Financiero Banorte", "Grupo Financiero Inbursa"),
     ("Royal Bank of Canada", "Royal Bank of Scotland"),
     ("Hong Kong Exchanges and Clearing", "Hong Kong and China Gas")],
)  # fmt: skip
def test_different_companies_that_share_two_words(a, b):
    assert not sdb.same_company(a, b)


def test_a_pasted_name_prefers_a_yahoo_company_with_that_word(monkeypatch):
    weak = Hit("RR", "Richtech Robotics Inc.", "stock", "NMS", "us", None, None, None, 9, 77.0)
    monkeypatch.setattr(sdb, "get", lambda t: None)
    monkeypatch.setattr(sdb, "lookup", lambda q, **k: [weak] if q == "richter" else [])
    monkeypatch.setattr(sdb, "status", lambda: {"installed": True})
    live = [_yh("RICHT.BD", "Richter Gedeon Chemical Factory", "BUD")]
    monkeypatch.setattr(sdb, "yahoo_lookup", lambda q, **k: live if q == "richter" else [])
    out = search.run({**search.empty("AAPLX, richter"), "kind": "list"})
    assert [c["ticker"] for c in out["results"]] == ["RICHT.BD"] and "AAPLX" in out["warnings"][0]


def test_an_ai_pick_yahoo_answers_with_another_company_is_dropped(monkeypatch):
    monkeypatch.setattr(sdb, "get", lambda t: None)
    monkeypatch.setattr(sdb, "lookup", lambda q, **k: [])
    monkeypatch.setattr(
        sdb, "yahoo_lookup", lambda q, **k: [_yh("ACME.F", "Acme Mining Corp", "FRA")]
    )
    cards, dropped = search._verify_picks(
        [{"ticker": "ZZQ", "name": "Zeta Quantum Labs", "why": "x"}]
    )
    assert cards == [] and dropped == 1


def test_stand_in_candidates_are_fetched_without_retry_sleeps(monkeypatch):
    # A candidate Yahoo has no batch data for is simply not a candidate: the
    # one-by-one retries (up to ~5 s each) were most of a cold book's wait.
    from convexity import cache

    monkeypatch.setattr(A.yf, "download", lambda **k: pd.DataFrame())
    monkeypatch.setattr(A.time, "sleep", lambda s: pytest.fail("slept"))
    monkeypatch.setattr(A.yf, "Ticker", lambda s: pytest.fail("retried one by one"))
    assert A._bulk_close(["NOPE1.X", "NOPE2.X"], "2y", adjusted=False, retry=False).empty
    assert all(k[0] != "NOPE1.X" or v[1].empty for k, v in cache._BULK_CLOSE_CACHE.items())


def test_found_stand_ins_are_kept_across_launches(monkeypatch):
    A._PROXY.clear()
    assert A.known_proxies("YNG.DE") == ()
    A._proxy_disk_put("YNG.DE", ("YNG", "YNG.F"))
    assert A.known_proxies("YNG.DE") == ("YNG", "YNG.F")  # from disk, no lookup
    monkeypatch.setattr(A, "_PROXY_DISK_TTL_S", -1.0)
    assert A.known_proxies("YNG.DE") == ()  # too old: looked up again


# ---- The stock window: a listing Yahoo has barely any prices for


@pytest.fixture
def young_window(monkeypatch):
    """YNG.DE (EUR) has one close, at EUR = 1.10 USD; YNG (USD) trades all
    along, with bars; OLD stopped trading long ago; AAA is another line of
    the same company that trades on days YNG doesn't."""
    idx = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=800)
    home = pd.Series(_walk_n(11, len(idx)), index=idx)
    other = (
        pd.Series(_walk_n(12, len(idx)), index=idx).iloc[::-1].set_axis(idx + pd.Timedelta(days=1))
    )
    wide = pd.DataFrame({"YNG": home}).join(other.rename("YNA"), how="outer")
    wide["OLD"] = pd.Series(_walk_n(13, 200), index=idx[:200])
    bars = pd.DataFrame({"Open": home * 0.99, "High": home * 1.02, "Low": home * 0.98,
                         "Close": home, "Volume": 1000.0})  # fmt: skip

    def bulk(syms, period, adjusted=True, retry=True):
        return wide[[s for s in syms if s in wide]]

    monkeypatch.setattr(A, "_bulk_close", bulk)
    monkeypatch.setattr(A, "_bulk_bars_get", lambda s, p: bars if s == "YNG" else None)
    monkeypatch.setattr(
        fx,
        "fx_multipliers",
        lambda ccys, base, dates: {
            c: np.full(len(dates), 1 / 1.10 if base == "EUR" else 1.0) for c in ccys
        },
    )
    monkeypatch.setattr(investments, "_fetch_quote_ccy", lambda s: "USD" if "." not in s else "EUR")
    monkeypatch.setattr(A, "_proxy_candidates", lambda s, n: ("OLD", "YNG", "YNA"))
    last = idx[-1]
    tz = "Europe/Berlin"
    own = pd.Series([float(home.iloc[-1] / 1.10)], index=pd.DatetimeIndex([last]).tz_localize(tz))
    return home, own


def _walk_n(seed, n):
    rng = np.random.default_rng(seed)
    return 100.0 * np.cumprod(1 + rng.normal(0.0004, 0.01, n))


def test_window_one_close_gets_the_company_history(young_window):
    home, own = young_window
    got = A.window_stand_in("YNG.DE", own, "EUR")
    assert got["proxy"] == "YNG"  # OLD stopped trading long before the join
    assert got["since"] == str(home.index[-1])[:10]
    close = got["close"]
    # The listing's own close is untouched, on its own stamp, last.
    assert close.iloc[-1] == own.iloc[0] and close.index[-1] == own.index[0]
    assert str(close.index.tz) == "Europe/Berlin" and close.index.is_monotonic_increasing
    # Only the stand-in's own trading days: no flat steps from the other line.
    assert len(close) == len(home)
    # At a constant rate the scale is exact: the returns are the company's.
    naive = close.tz_localize(None)
    assert naive.iloc[0] == pytest.approx(home.iloc[0] / 1.10)
    r = naive.iloc[-1] / naive.iloc[-253]
    assert r == pytest.approx(home.iloc[-1] / home.iloc[-253])


def test_window_stand_in_bars_scale_like_its_closes(young_window):
    home, own = young_window
    got = A.window_stand_in("YNG.DE", own, "EUR")
    day = got["ohlc"].iloc[10]
    assert day["High"] == pytest.approx(home.iloc[10] * 1.02 / 1.10)
    assert day["Low"] == pytest.approx(home.iloc[10] * 0.98 / 1.10)
    assert got["ohlc"].index[10] == got["close"].index[10]  # the chart joins them by stamp


def test_window_leaves_an_established_line_alone(young_window):
    home, _ = young_window
    assert A.window_stand_in("YNG", home.tz_localize("America/New_York"), "USD") is None


def test_window_young_line_without_a_stand_in_stays_as_it_is(young_window, monkeypatch):
    _, own = young_window
    monkeypatch.setattr(A, "_proxy_candidates", lambda s, n: ())
    assert A.window_stand_in("YNG.DE", own, "EUR") is None
    monkeypatch.setattr(A, "_proxy_candidates", lambda s, n: ("OLD",))  # stopped: not a stand-in
    assert A.window_stand_in("YNG.DE", own, "EUR") is None


def test_window_needs_the_listing_currency(young_window):
    _, own = young_window
    assert A.window_stand_in("YNG.DE", own, None) is None


def test_window_stand_in_younger_than_the_reach_keeps_its_dividends(monkeypatch):
    """A line that simply doesn't reach back five years isn't broken: the
    as-traded fallback must not take its dividend adjustment away."""
    idx = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=600)  # about 2.3 years
    adj = pd.Series(_walk_n(21, len(idx)), index=idx)
    raw = adj * np.linspace(1.3, 1.0, len(idx))  # adjusted closes sit below as traded

    def bulk(syms, period, adjusted=True, retry=True):
        return (adj if adjusted else raw).to_frame("YNG")[[s for s in syms if s == "YNG"]]

    monkeypatch.setattr(A, "_bulk_close", bulk)
    monkeypatch.setattr(A, "_bulk_bars_get", lambda s, p: None)
    monkeypatch.setattr(fx, "fx_multipliers", lambda c, b, d: {x: np.ones(len(d)) for x in c})
    monkeypatch.setattr(investments, "_fetch_quote_ccy", lambda s: "USD")
    monkeypatch.setattr(A, "_proxy_candidates", lambda s, n: ("YNG",))
    own = pd.Series([float(adj.iloc[-1])], index=idx[-1:])
    close = A.window_stand_in("YNG.DE", own, "USD")["close"]
    assert close.iloc[0] == pytest.approx(adj.iloc[0])  # not raw.iloc[0]


@pytest.mark.parametrize(
    "ticker", ["FITB-PM", "BAC-PL", "ACHR-WT", "XYZ-WS", "AAC-UN", "XYZ-RI", "ABC-PR-A.TO", "ABC-WT.TO",
               "RGTIW", "BRRWW", "BTSGU", "GENVR"],
)  # fmt: skip
def test_preferreds_warrants_rights_and_units_are_not_the_share(ticker):
    assert sdb.not_the_share(ticker)


@pytest.mark.parametrize(
    "ticker", ["AAPL", "SNOW", "BRK-B", "HEI-A", "BF-B", "PBR-A", "MOG-A", "GOOGL", "FWONK", "VOW3.DE",
               "SAN.PA", "REI-UN.TO", "AMZN-USD.SW", "0TDF.L", "RIGHT.DE", "SHOP.TO"],
)  # fmt: skip
def test_shares_and_their_classes_are_the_share(ticker):
    assert not sdb.not_the_share(ticker)


def test_a_warrant_never_stands_in_for_a_share_nor_the_share_for_it(monkeypatch):
    A._PROXY.clear()
    grp = [Hit(t, "Young Co, Inc.", "stock", "NMS", "us", None, None, None, 7, 1.0)
           for t in ("YOUNG", "YOUNGW", "YOUN-PA", "YNG.DE")]  # fmt: skip
    monkeypatch.setattr(sdb, "get", lambda t: next((h for h in grp if h.ticker == t), None))
    monkeypatch.setattr(sdb, "listings", lambda g: grp)
    monkeypatch.setattr(sdb, "relisting", lambda t, e: False)
    monkeypatch.setattr(A, "_proxy_disk_put", lambda *a: None)
    monkeypatch.setattr(A, "_proxy_disk", dict)
    assert A._proxy_candidates("YOUNG", None) == ("YNG.DE",)
    assert A._proxy_candidates("YOUNGW", None) == ()


def test_saved_stand_ins_lose_their_warrants_on_read(monkeypatch):
    import time

    A._PROXY.clear()
    monkeypatch.setattr(A, "_proxy_disk", lambda: {"YNG.DE": [time.time(), ["YOUNGW", "YNG"]]})
    assert A.known_proxies("YNG.DE") == ("YNG",)


class _FakeTicker:
    """What fetch_detail reads from a Ticker, for one close of a young line."""

    recommendations = calendar = balance_sheet = quarterly_balance_sheet = None
    income_stmt = quarterly_income_stmt = None
    news = ()

    def __init__(self, frame):
        self.frame = frame

    def history(self, period=None, **k):
        return self.frame if period != "max" else self.frame.iloc[:0]  # as Yahoo answers them


def _detail(monkeypatch, own, info=None):
    from convexity import fetcher

    c = own.to_numpy()
    frame = pd.DataFrame(
        {"Open": c, "High": c, "Low": c, "Close": c, "Volume": 500.0}, index=own.index
    )
    monkeypatch.setattr(fetcher.yf, "Ticker", lambda s: _FakeTicker(frame))
    monkeypatch.setattr(fetcher, "_safe_info", lambda tk: {"currency": "EUR", **(info or {})})
    monkeypatch.setattr(fetcher, "_set_beta", lambda out, s: None)
    monkeypatch.setattr(fetcher, "_apply_research_detail", lambda out, s: None)
    monkeypatch.setattr(fetcher, "_fetch_bench_history", lambda s, p="10y": None)
    monkeypatch.setattr(fetcher._targets, "consensus", lambda *a, **k: None)
    monkeypatch.setattr(
        sdb,
        "get",
        lambda t: Hit(t, "Young Co, Inc.", "stock", "NMS", "us", None, None, None, 7, 1.0),
    )
    return fetcher


def test_detail_window_draws_and_measures_through_the_stand_in(young_window, monkeypatch):
    home, own = young_window
    d = _detail(monkeypatch, own).fetch_detail("YNG.DE")
    assert d["stand_in"]["symbol"] == "YNG" and d["stand_in"]["exchange"] == "NMS"
    assert "close" not in d["stand_in"] and "ohlc" not in d["stand_in"]  # plain JSON only
    assert len(d["history"]) == len(home) and d["history"][-1][1] == own.iloc[0]
    assert len(d["volume"]) == 1  # a stand-in's volume isn't this listing's
    assert d["price"] == own.iloc[0]
    assert d["pct_1d"] == pytest.approx((home.iloc[-1] / home.iloc[-2] - 1) * 100)
    year = home.iloc[-260:]
    assert d["w52_high"] == pytest.approx(
        year[year.index >= home.index[-1] - pd.Timedelta(days=365)].max() / 1.10
    )
    assert d["w52_high"] != d["w52_low"]
    base = home[home.index <= home.index[-1] - pd.Timedelta(days=365)].iloc[-1]
    assert d["performance"]["1y"]["stock"] == pytest.approx((home.iloc[-1] / base - 1) * 100)


def test_detail_window_without_a_stand_in_still_has_no_chart(young_window, monkeypatch):
    _, own = young_window
    monkeypatch.setattr(A, "_proxy_candidates", lambda s, n: ())
    d = _detail(monkeypatch, own).fetch_detail("YNG.DE")
    assert "stand_in" not in d
    assert len(d["history"]) == 1  # what the chart's "No data" branch reads
    assert d["w52_high"] == d["w52_low"] == d["ath"]


def _intraday(monkeypatch, days):
    from convexity import fetcher

    start = 1_700_000_000_000
    bars = {"history": [[start, 1.0], [start + days * 86_400_000, 2.0]], "volume": []}
    monkeypatch.setattr(fetcher, "intraday_history", lambda s, i, p: bars)
    return fetcher


def test_intraday_tab_falls_back_to_the_daily_stand_in(monkeypatch):
    fetcher = _intraday(monkeypatch, 2)
    monkeypatch.setattr(A, "known_proxies", lambda s: ("YNG",))
    got = fetcher.range_history("YNG.DE", "1M")
    assert got["fallback"] is True and "history" not in got
    monkeypatch.setattr(A, "known_proxies", lambda s: ())
    assert fetcher.range_history("YNG.DE", "1M")["fallback"] is False  # nothing better to draw


def test_intraday_tab_stays_when_the_listing_fills_the_range(monkeypatch):
    fetcher = _intraday(monkeypatch, 60)
    monkeypatch.setattr(A, "known_proxies", lambda s: ("YNG",))
    assert fetcher.range_history("YNG.DE", "1M")["fallback"] is False
    assert fetcher.range_history("YNG.DE", "6M")["fallback"] is True
