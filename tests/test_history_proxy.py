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
     ("First Citizens BancShares", "First Republic Bank", False)],
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
