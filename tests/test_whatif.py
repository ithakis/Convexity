"""What-if portfolios (whatif.py, Phase 4): the copy, its block through saves,
renames and a deleted book, the book's weights with same-company matching, the
comparison (the green line, one calendar, one risk-free rate, the book's total
row), the rebalance route's inputs and refusals, and the optimizer's marks,
picks and weight sets (frontier.py). Public or synthetic tickers, made-up
prices, no network."""

import http.server
import json
import threading
import urllib.error
import urllib.request
from datetime import date

import numpy as np
import pandas as pd
import pytest

from convexity import analytics as A
from convexity import books, frontier, persistence, server, whatif
from convexity import investments as inv
from convexity import symbol_db as sdb
from convexity.symbol_db import Hit

TODAY = date.today().isoformat()
IDX = pd.bdate_range(end=pd.Timestamp(TODAY), periods=260)


def _walk(seed, n=len(IDX), start=100.0):
    rng = np.random.default_rng(seed)
    return start * np.cumprod(1 + rng.normal(0.0004, 0.01, n))


def _pos(sym, shares, price, qccy, fx, profit=0.0, **kw):
    major = price / 100.0 if qccy == "GBp" else price
    return {"symbol": sym, "name": sym, "shares": shares, "price": price, "quote_ccy": qccy,
            "value": shares * major * fx, "profit_held": profit, "price_date": TODAY, **kw}  # fmt: skip


def _page(rev=3, cash=0.0, **kw):
    """A computed My Investments page in EUR: a USD share, a euro share and an
    LSE line in pence, a TWR index that starts a third of the way into IDX."""
    dates = [d.date().isoformat() for d in IDX[90:]]
    out = {
        "empty": False, "rev": rev, "ccy": "EUR",
        "positions": [_pos("AAPL", 10, 200.0, "USD", 0.9, 300.0), _pos("SIE.DE", 20, 150.0, "EUR", 1.0, -100.0),
                      _pos("VOD.L", 1000, 80.0, "GBp", 1.15, 50.0)],
        "headline": {"cash": cash, "cash_weight": 0.0},
        "entries": [{"id": "e1", "date": "2025-01-02", "type": "buy", "symbol": "AAPL", "qty": 10}],
        "series": {"dates": dates, "twr_index": list(_walk(9, len(dates), 1.0))},
    }  # fmt: skip
    return {**out, **kw}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    persistence._write_views_raw({})  # views.json binds its path at import
    persistence.save_watchlists({})
    persistence._write_mpt_raw({})
    whatif._SIGMA.clear()
    page = {"v": _page()}
    monkeypatch.setattr(whatif, "_page", lambda book_id, ccy: page["v"])
    monkeypatch.setattr(sdb, "get", lambda t: None)
    yield page
    persistence._write_views_raw({})
    persistence.save_watchlists({})


# ----------------------------------------------------------------- the copy


def test_a_copy_is_one_portfolio_with_its_block_preset_and_rows():
    persistence.save_view("__book__", "AAPL, SIE.DE, VOD.L", [{"symbol": "AAPL", "price": 1.0}],
                          set_last=False)  # fmt: skip
    out = whatif.create("main", None, "EUR")
    assert out["name"] == "What-if 1" and out["n_holdings"] == 3 and not out["rows_complete"]
    origin = out["whatif"]["origin"]
    total = 1800 + 3000 + 920
    assert origin == pytest.approx(
        {"AAPL": 1800 / total, "SIE.DE": 3000 / total, "VOD.L": 920 / total}
    )
    assert out["active"].startswith("My Investments · ") and "entry_ids" not in out["whatif"]
    view = persistence.load_view("What-if 1")
    assert view["whatif"]["entry_ids"] == ["e1"] and view["whatif"]["rev"] == 3
    assert view["active_weight_preset"] == out["active"] and view["stale"]
    assert view["weight_presets"][0]["weights"] == pytest.approx(origin)
    assert persistence.load_watchlists()["What-if 1"] == "SIE.DE, AAPL, VOD.L"
    assert [r["symbol"] for r in view["rows"]] == ["AAPL"]
    assert whatif.create("main", None, "EUR")["name"] == "What-if 2"
    listed = persistence.list_views()["views"]["What-if 1"]["whatif"]
    assert listed["book_id"] == "main" and "entry_ids" not in listed


def test_the_block_survives_a_refresh_and_a_rename_and_undo_removes_everything():
    whatif.create("main", "Plan B", "EUR")
    persistence.save_view("Plan B", "AAPL", [{"symbol": "AAPL"}])
    assert persistence.load_view("Plan B")["whatif"]["book_id"] == "main"
    persistence.rename_watchlist("Plan B", "Plan C")
    persistence.rename_view("Plan B", "Plan C")
    assert persistence.load_view("Plan C")["whatif"]["origin"]
    persistence.delete_watchlist("Plan C")  # the toast's Undo: the ordinary delete
    assert persistence.load_view("Plan C") == {} and "Plan C" not in persistence.load_watchlists()


@pytest.mark.parametrize(("page", "status", "code"), [
    ({"splits_pending": [{"symbol": "X"}]}, 409, "BookWaiting"),
    ({"waiting": "Waiting"}, 409, "BookWaiting"),
    ({"broken": "Bad."}, 422, "BookBroken"),
    ({"empty": True, "positions": []}, 422, "BookEmpty"),
])  # fmt: skip
def test_a_book_that_isnt_whole_is_not_copied(clean, page, status, code):
    clean["v"] = _page(**page)
    with pytest.raises(whatif.WhatIfError) as e:
        whatif.create("main", None, "EUR")
    assert (e.value.status, e.value.code) == (status, code)
    assert persistence.load_watchlists() == {} and persistence.list_views()["views"] == {}


def test_a_copy_for_a_book_no_longer_showing_or_a_taken_name_is_refused():
    books.set_enabled(True)
    books.add("Partner")
    with pytest.raises(books.BookMoved):
        whatif.create("main", None, "EUR")
    books.show("main")
    persistence.upsert_watchlist("Mine", "AAPL")
    with pytest.raises(whatif.WhatIfError) as e:
        whatif.create("main", "Mine", "EUR")
    assert e.value.status == 409 and persistence.load_watchlists() == {"Mine": "AAPL"}
    with pytest.raises(whatif.WhatIfError):
        whatif.create("main", "__book__", "EUR")


def test_deleting_a_book_detaches_its_what_ifs_and_keeps_them():
    books.set_enabled(True)
    books.add("Partner")
    pid = books.showing()
    whatif.create(pid, "Theirs", "EUR")
    books.show("main")
    whatif.create("main", "Ours", "EUR")
    books.delete(pid)
    theirs = persistence.load_view("Theirs")
    assert (
        "whatif" not in theirs
        and theirs["weight_presets"]
        and "Theirs" in persistence.load_watchlists()
    )
    assert persistence.load_view("Ours")["whatif"]["book_id"] == "main"


# ------------------------------------------------------ book weights + match


def test_same_company_other_listing_is_matched_once_and_never_a_fund(monkeypatch):
    hits = {
        "AAPL": Hit("AAPL", "Apple Inc.", "stock", "NMS", "us", None, None, None, 7, 100.0),
        "APC.DE": Hit("APC.DE", "Apple Inc.", "stock", "GER", "de", None, None, None, 7, 100.0),
        "SAN.PA": Hit("SAN.PA", "Sanofi", "stock", "PAR", "fr", None, None, None, 8, 100.0),
        "SANP.L": Hit("SANP.L", "Sanofi", "etf", "LSE", "gb", None, None, None, 8, 100.0),
    }  # fmt: skip
    monkeypatch.setattr(sdb, "get", lambda t: hits.get(t))
    weights = {"APC.DE": 0.3, "SANP.L": 0.2, "MSFT": 0.5}
    by, only = whatif.match(["AAPL", "SAN.PA", "MSFT", "NVDA"], weights)
    assert by["AAPL"] == {"weight": 0.3, "via": "APC.DE"}
    assert by["SAN.PA"] == {"weight": 0.0, "via": None}  # a fund never stands for a share
    assert by["MSFT"] == {"weight": 0.5, "via": None} and by["NVDA"]["weight"] == 0.0
    assert only == ["SANP.L"]
    # The portfolio holding the book's own line too: no double match.
    by, _ = whatif.match(["AAPL", "APC.DE"], weights)
    assert by["AAPL"]["via"] is None and by["APC.DE"]["via"] is None and by["AAPL"]["weight"] == 0.0


def test_book_view_reads_weights_without_cash_and_what_changed(clean):
    whatif.create("main", "W", "EUR")
    clean["v"] = _page(rev=4, cash=1000.0, entries=[
        {"id": "e2", "date": "2026-11-12", "type": "buy", "symbol": "VOD.L", "qty": 5}])  # fmt: skip
    out = whatif.book_view("W", ["AAPL", "NEW1"], "EUR")
    assert out["status"] == "ok" and sum(out["weights"].values()) == pytest.approx(1.0)
    assert out["match"]["NEW1"]["weight"] == 0.0 and {x["symbol"] for x in out["book_only"]} == {
        "SIE.DE",
        "VOD.L",
    }
    ch = out["whatif"]["changes"]
    assert out["whatif"]["changed"] and ch["n_added"] == 1 and ch["n_removed"] == 1
    assert ch["added"][0]["symbol"] == "VOD.L"
    clean["v"] = _page(splits_pending=[{"symbol": "X"}])
    assert whatif.book_view("W", None, "EUR")["status"] == "waiting"


def test_a_what_if_of_another_book_reads_nothing():
    whatif.create("main", "W", "EUR")
    books.set_enabled(True)
    books.add("Partner")
    out = whatif.book_view("W", None, "EUR")
    assert out["status"] == "other_book" and "weights" not in out


# ------------------------------------------------------------- comparison


def _analytics(P, spy, totals=None):
    pts = lambda s: [[int(t.timestamp() * 1000), float(v)] for t, v in s.items()]  # noqa: E731
    return {"series": {"portfolio": pts(P)}, "benchmarks": {"SPY": {"series": pts(spy)}},
            "stats": {"rf_source": "0 (no short-rate series)"}, "trading": {"rebalance": "M"},
            "totals": totals or {"contrib": {"rule": "contrib", "v": 12.0}}}  # fmt: skip


@pytest.fixture
def analytics_stub(monkeypatch):
    P = pd.Series(100 * _walk(1) / _walk(1)[0], index=IDX)
    spy = pd.Series(100 * _walk(2) / _walk(2)[0], index=IDX)
    calls = []

    def fake(rows, sets, period, ccy, rebalance="M", tc_bps=10, as_traded=False):
        calls.append({"rows": [r["symbol"] for r in rows], "sets": sets, "as_traded": as_traded})
        return {
            k: _analytics(P, spy, {"contrib": {"v": 7.0}} if k == "book" else None) for k in sets
        }

    monkeypatch.setattr(A, "analyze_portfolios_multi", fake)
    return P, calls


def test_the_green_line_starts_with_the_book_and_follows_its_twr(clean, analytics_stub):
    P, _ = analytics_stub
    out = whatif.compare("main", [{"symbol": "AAPL"}], {"AAPL": 1}, "1Y", "EUR")
    page = clean["v"]
    twr = pd.Series(page["series"]["twr_index"], index=pd.DatetimeIndex(page["series"]["dates"]))
    line = out["line"]
    assert line["mid_chart"] and line["starts"] == page["series"]["dates"][0]
    d0 = pd.Timestamp(line["starts"])
    for ms, v in line["points"][::40]:
        t = pd.Timestamp(ms, unit="ms")
        assert v == pytest.approx(P[d0] * twr[t] / twr[d0], rel=1e-6)
    # Never backfilled: nothing before the book's first day.
    assert pd.Timestamp(line["points"][0][0], unit="ms") == d0
    # The window is longer than the book: no book column on it, a shared one.
    assert out["columns"]["book"] is None and out["columns"]["whatif"]["total_return"] is not None
    sh = out["shared"]
    assert sh["since"] == line["starts"] and sh["book"]["total_return"] == pytest.approx(
        (twr.iloc[-1] / twr.iloc[0] - 1) * 100, rel=1e-6)  # fmt: skip
    assert sh["between"]["te"] is not None and out["rf_source"].startswith("0")


def test_a_window_inside_the_book_has_three_columns(clean, analytics_stub):
    dates = [d.date().isoformat() for d in IDX]
    clean["v"] = _page(series={"dates": dates, "twr_index": list(_walk(9, len(dates), 1.0))})
    out = whatif.compare("main", [{"symbol": "AAPL"}], {"AAPL": 1}, "1Y", "EUR")
    assert out["shared"] is None and not out["line"]["mid_chart"]
    cols = out["columns"]
    assert cols["book"]["sharpe"] is not None and cols["spy"]["total_return"] is not None
    assert cols["whatif"]["rel"]["beta"] is not None and cols["book"]["rel"]["te"] is not None


def test_both_sides_read_one_risk_free_rate_in_usd(clean, analytics_stub, monkeypatch):
    dates = [d.date().isoformat() for d in IDX]
    clean["v"] = _page(
        ccy="USD", series={"dates": dates, "twr_index": list(_walk(9, len(dates), 1.0))}
    )
    monkeypatch.setattr(
        A, "_bulk_close", lambda syms, period, **k: pd.DataFrame({"^IRX": 4.0}, index=IDX)
    )
    cols = whatif.compare("main", [{"symbol": "AAPL"}], {"AAPL": 1}, "1Y", "USD")["columns"]
    assert (
        cols["whatif"]["rf_ann"] == pytest.approx(cols["book"]["rf_ann"])
        and cols["book"]["rf_ann"] > 3.9
    )


def test_the_book_total_row_is_the_red_tabs_with_live_weights(clean, analytics_stub):
    _, calls = analytics_stub
    persistence.save_view("__book__", "AAPL, SIE.DE, VOD.L",
                          [{"symbol": "SIE.DE", "name": "saved"}, {"symbol": "ZZZ"}], set_last=False)  # fmt: skip
    out = whatif.compare("main", [{"symbol": "AAPL"}, {"symbol": "NEW1"}], {"AAPL": 0.5, "NEW1": 0.5},
                         "1Y", "EUR")  # fmt: skip
    book = next(c for c in calls if "book" in c["sets"])
    assert book["as_traded"] and sorted(book["rows"]) == ["AAPL", "SIE.DE"]
    assert set(book["sets"]["book"]) == {"AAPL", "SIE.DE", "VOD.L"}
    bt = out["book_total"]
    assert (
        bt["totals"]["contrib"]["v"] == 7.0 and bt["missing"] == ["VOD.L"] and bt["n_holdings"] == 3
    )


def test_a_comparison_on_a_waiting_book_says_so(clean, analytics_stub):
    clean["v"] = _page(waiting="w")
    out = whatif.compare("main", [{"symbol": "AAPL"}], {"AAPL": 1}, "1Y", "EUR")
    assert out["status"] == "waiting" and "line" not in out
    books.set_enabled(True)
    books.add("Partner")
    with pytest.raises(whatif.WhatIfError) as e:
        whatif.compare("main", [{"symbol": "AAPL"}], {"AAPL": 1}, "1Y", "EUR")
    assert e.value.code == "BookMoved" and "saved" not in e.value.message


# ------------------------------------------------------------- rebalance


@pytest.fixture
def market(monkeypatch):
    closes = {"NEW1": pd.Series(_walk(5), index=IDX)}
    monkeypatch.setattr(A, "_bulk_close",
                        lambda syms, period, **k: pd.DataFrame({s: closes[s] for s in syms if s in closes}))  # fmt: skip
    monkeypatch.setattr(whatif, "_sigma", lambda syms, qccy, names, disp, span: (
        np.eye(len(syms)) * 0.04 + 0.01, {"n_obs": 250, "short": [], "stand_ins": {}}))  # fmt: skip
    return closes


def test_rebalance_values_both_sides_at_the_pages_snapshot(clean, market):
    persistence.save_view(
        "W", "AAPL, NEW1", [{"symbol": "NEW1", "currency": "USD", "name": "New One"}]
    )
    before = persistence._views_path().read_bytes()
    out = whatif.rebalance_payload({"book_id": "main", "view": "W", "ccy": "EUR", "mode": "exact",
                                    "weights": {"AAPL": 0.25, "SIE.DE": 0.25, "VOD.L": 0.25, "NEW1": 0.25},
                                    "min_trade": 0})  # fmt: skip
    assert persistence._views_path().read_bytes() == before  # read-only
    new = next(t for t in out["trades"] if t["symbol"] == "NEW1")
    last = float(market["NEW1"].iloc[-1])
    # A new USD name at the rate the page's own USD holding implies (0.9).
    assert new["price"] == pytest.approx(last) and new["value"] == pytest.approx(
        new["shares"] * last * 0.9
    )
    assert new["new"] and new["name"] == "New One" and new["price_date"] == TODAY
    vod = next((t for t in out["trades"] if t["symbol"] == "VOD.L"), None)
    assert vod is None or vod["trade_ccy"] == "GBP"
    assert out["summary"]["te"] < 1e-6 and out["rev"] == 3 and out["prices_as_of"] == TODAY
    assert out["target"] == "weights" and out["options"]["min_trade"] == 0


def test_rebalance_to_the_active_preset_by_default_and_names_left_out(clean, market):
    whatif.create("main", "W", "EUR")
    out = whatif.rebalance_payload({"book_id": "main", "view": "W", "ccy": "EUR"})
    assert out["target"].startswith("preset:My Investments") and out["summary"]["n_trades"] == 0
    out = whatif.rebalance_payload({"book_id": "main", "view": "W", "ccy": "EUR",
                                    "weights": {"AAPL": 1, "SIE.DE": 1}, "mode": "fewest"})  # fmt: skip
    assert out["not_in_target"] == ["VOD.L"] and out["path"]
    vod = next(t for t in out["trades"] if t["symbol"] == "VOD.L")
    assert vod["all"] and vod["gain_est"] is not None


def test_a_stale_new_price_is_refused_with_its_date(clean, market):
    old = IDX[:-20]
    market["NEW1"] = market["NEW1"].loc[old]
    out = whatif.rebalance_payload(
        {"book_id": "main", "ccy": "EUR", "weights": {"AAPL": 1, "NEW1": 1}}
    )
    assert "NEW1" not in {t["symbol"] for t in out["trades"]}
    assert (
        out["locked"][0]["symbol"] == "NEW1"
        and old[-1].date().isoformat() in out["locked"][0]["reason"]
    )


@pytest.mark.parametrize("page", [{"splits_pending": [{"symbol": "X"}]}, {"broken": "x"}])
def test_rebalance_is_refused_on_a_book_that_isnt_whole(clean, market, page):
    clean["v"] = _page(**page)
    with pytest.raises(whatif.WhatIfError):
        whatif.rebalance_payload({"book_id": "main", "ccy": "EUR", "weights": {"AAPL": 1}})


def test_the_risk_model_keeps_a_short_name_from_cutting_the_window(monkeypatch):
    idx = pd.bdate_range(end=TODAY, periods=300)
    raw = pd.DataFrame({"A": _walk(1, 300), "B": _walk(2, 300), "C": pd.Series(_walk(3, 300)).where(
        np.arange(300) >= 270).to_numpy()}, index=idx)  # fmt: skip
    monkeypatch.setattr(
        A, "_bulk_close", lambda syms, period, **k: raw[[s for s in syms if s in raw]]
    )
    monkeypatch.setattr(A, "extend_history", lambda w, *a, **k: (w, {}))
    from convexity import fx

    monkeypatch.setattr(fx, "_apply_fx_to_closes", lambda w, *a: w)
    S, info = whatif._sigma(["A", "B", "C"], {}, {}, "EUR", "2y")
    assert info["short"] == ["C"] and info["n_obs"] >= 240
    assert np.allclose(S, S.T) and np.all(np.linalg.eigvalsh(S) > 0)
    assert S[2, 2] == pytest.approx(raw["C"].pct_change().std() ** 2 * 252, rel=0.05)


# -------------------------------------------------------------- optimizer


@pytest.fixture
def fr_market(monkeypatch):
    rng = np.random.default_rng(0)
    idx = pd.bdate_range(end=TODAY, periods=400)
    data = {s: 100 * np.cumprod(1 + rng.normal(0.0005 * (i + 1), 0.01 + 0.004 * i, 400))
            for i, s in enumerate(["AAA", "BBB", "CCC", "DDD"])}  # fmt: skip
    data["YNG"] = pd.Series(data["AAA"] * 1.02, index=idx).where(np.arange(400) >= 380).to_numpy()
    data["LONGY"] = data["AAA"] * 1.02 / 1.0
    closes = pd.DataFrame(data, index=idx)
    monkeypatch.setattr(
        frontier, "_bulk_close", lambda syms, period, **k: closes[[s for s in syms if s in closes]]
    )
    monkeypatch.setattr(frontier, "_analyst_views", lambda active, by_sym, rf: ({}, {}, {}))
    return closes


def _run(rows, **kw):
    return frontier.compute_efficient_frontier(
        rows, lookback="1Y", budget="light", max_seconds=0, **kw
    )


def test_saved_sets_are_marked_like_the_anchors_and_partial_ones_flagged(fr_market):
    rows = [{"symbol": s, "currency": "USD"} for s in ("AAA", "BBB", "CCC", "DDD")]
    eq = dict.fromkeys(("AAA", "BBB", "CCC", "DDD"), 0.25)
    res = _run(rows, current_weights=eq, weight_sets={"Even": eq, "Tilt": {"AAA": 0.5, "ZZZ": 0.5},
                                                      "Gone": {"ZZZ": 1}})  # fmt: skip
    even, tilt = res["sets"]["Even"], res["sets"]["Tilt"]
    assert even["ret"] == pytest.approx(res["anchors"]["equal"]["ret"])
    assert (
        even["cvar30"] == pytest.approx(res["anchors"]["equal"]["cvar30"]) and not even["partial"]
    )
    assert "ret_net" in even and "turnover_ann" in even
    assert tilt["partial"] and tilt["coverage"] == pytest.approx(0.5) and tilt["dropped"] == ["ZZZ"]
    assert tilt["weights"]["AAA"] == pytest.approx(1.0)
    assert res["sets"]["Gone"]["error"]
    pick = res["picks"]["same_risk"]
    cur = res["anchors"]["current"]
    pts = res["frontier"]
    got = pts[pick["index"]]
    # By the LP's own risk (daily CVaR), never the displayed cvar30.
    assert got["cvar"] <= cur["cvar"] + 1e-12
    assert all(p["ret"] <= got["ret"] + 1e-12 for p in pts if p["cvar"] <= cur["cvar"])
    # Equal weight is inside the feasible set: the pick returns at least as
    # much, or says the grid left it below.
    assert got["ret"] >= cur["ret"] - 1e-12 or "note" in pick
    assert pick["current_cvar30"] == pytest.approx(cur["cvar30"])
    assert res["picks"]["min_cvar"]["index"] == min(range(len(pts)), key=lambda i: pts[i]["cvar"])


def test_same_risk_reads_the_lps_cvar_not_the_displayed_one():
    """A current set above the drawn frontier on the cvar30 axis (another
    estimator) but inside it on the LP's daily CVaR gets the higher point."""
    fr = [{"ret": 0.05, "cvar": 0.12, "cvar30": 0.03}, {"ret": 0.067, "cvar": 0.21, "cvar30": 0.09},
          {"ret": 0.072, "cvar": 0.25, "cvar30": 0.10}]  # fmt: skip
    cur = {"ret": 0.065, "cvar": 0.245, "cvar30": 0.07}
    p = frontier.pick_points(fr, cur)["same_risk"]
    assert p == {"index": 1, "fallback": False, "current_cvar30": 0.07}  # by cvar30 it was 0
    p = frontier.pick_points(fr, {**cur, "ret": 0.07})["same_risk"]
    assert p["index"] == 1 and p["note"] == frontier.SAME_RISK_NOTE  # the grid left it below


def test_a_young_listing_is_extended_not_left_out_and_a_short_one_is_held_out(
    fr_market, monkeypatch
):
    monkeypatch.setattr(A, "_proxy_candidates", lambda s, n: ("LONGY",) if s == "YNG" else ())
    monkeypatch.setattr(inv, "_fetch_quote_ccy", lambda s: "USD")
    rows = [{"symbol": s, "currency": "USD"} for s in ("AAA", "BBB", "CCC", "YNG")]
    res = _run(rows, current_weights={"AAA": 0.4, "BBB": 0.2, "CCC": 0.2, "YNG": 0.2})
    assert "YNG" in res["symbols"] and res["stand_ins"]["YNG"]["proxy"] == "LONGY"
    assert any("LONGY" in w for w in res["meta"]["warnings"])
    monkeypatch.setattr(A, "_proxy_candidates", lambda s, n: ())
    res = _run(rows, current_weights={"AAA": 0.4, "BBB": 0.2, "CCC": 0.2, "YNG": 0.2})
    assert res["missing"] == ["YNG"] and res["held_out"] == [
        {"symbol": "YNG", "weight": pytest.approx(0.2)}
    ]
    assert res["stand_ins"] == {}


def test_pick_points_falls_back_to_the_lowest_risk():
    fr = [{"ret": 0.05, "cvar": 0.10}, {"ret": 0.07, "cvar": 0.12}, {"ret": 0.09, "cvar": 0.15}]
    assert frontier.pick_points(fr, {"cvar": 0.13, "ret": 0.06})["same_risk"] == {
        "index": 1, "fallback": False, "current_cvar30": 0.13}  # fmt: skip
    p = frontier.pick_points(fr, {"cvar": 0.05})
    assert p["same_risk"]["index"] == 0 and p["same_risk"]["fallback"]
    assert p["same_risk"]["note"] == frontier.SAME_RISK_FALLBACK_NOTE and p["min_cvar"] == {
        "index": 0
    }
    assert frontier.pick_points([], {})["same_risk"] is None


def test_a_point_becomes_a_clean_set_with_held_out_names_kept():
    w = frontier.set_from_point({"AAA": 0.6004, "BBB": 0.3991, "CCC": 0.0005},
                                [{"symbol": "YNG", "weight": 0.1}])  # fmt: skip
    assert "CCC" not in w and sum(w.values()) == pytest.approx(1.0, abs=1e-12)
    assert w["YNG"] == 0.1 and w["AAA"] == pytest.approx(0.6004 / 0.9995 * 0.9, abs=1e-4)
    assert all(round(v, 4) == v for v in w.values())
    with pytest.raises(ValueError):
        frontier.set_from_point({"AAA": 0.0}, [])
    d = date(2026, 11, 13)
    assert frontier.set_name("same_risk", [], d) == "Optimized · same risk · 13 Nov"
    taken = ["Optimized · lowest risk · 13 Nov", "Optimized · lowest risk · 13 Nov (2)"]
    assert frontier.set_name("min_cvar", taken, d) == "Optimized · lowest risk · 13 Nov (3)"


def test_plug_params_clean_legacy_keys():
    runs = [{"params": {"lookback": "1y", "alpha": 0.9, "rf": 0.03, "frequency": "weekly",
                        "budget": "exhaustive", "diversified": True, "cov_model": "EWMA",
                        "haircut": 7, "fully_invested": False, "bounds": {"AAA": {"max": 0.2}}}}]  # fmt: skip
    p = frontier.plug_params(runs)
    assert p == {"lookback": "1Y", "alpha": 0.9, "rf": 0.03, "fully_invested": False, "cov_model": "ewma",
                 "haircut": 0.25, "bounds": {"AAA": {"max": 0.2}}, "budget": "light"}  # fmt: skip
    assert frontier.plug_params([]) == {**frontier.PLUG_DEFAULTS, "budget": "light"}


def test_a_preset_source_is_allow_listed_and_follows_the_weights():
    persistence.save_view("P", "AAA, BBB", [])
    src = {"kind": "optimizer", "rule": "same_risk", "index": 4, "run_id": "run_1",
           "params": {"lookback": "3Y", "bounds": {"AAA": 1}, "evil": "x"}, "point": {"ret": 0.1, "x": 1},
           "held_out": [{"symbol": "yng", "weight": 0.2}, {"symbol": "B", "weight": 7}],
           "warnings": ["ok", 5], "script": "<b>"}  # fmt: skip
    out = persistence.upsert_weight_preset("P", "Opt", {"AAA": 0.5, "BBB": 0.5}, source=src)
    got = out["presets"][0]["source"]
    assert got == {"kind": "optimizer", "rule": "same_risk", "index": 4, "run_id": "run_1",
                   "params": {"lookback": "3Y"}, "point": {"ret": 0.1},
                   "held_out": [{"symbol": "YNG", "weight": 0.2}], "warnings": ["ok"]}  # fmt: skip
    assert persistence.clean_preset_source({"kind": "other"}) is None
    out = persistence.upsert_weight_preset(
        "P", "Opt 2", {"AAA": 0.5, "BBB": 0.5}, rename_from="Opt"
    )
    assert out["presets"][0]["source"]["index"] == 4  # a rename keeps it
    out = persistence.upsert_weight_preset("P", "Opt 2", {"AAA": 0.6, "BBB": 0.4})
    assert "source" not in out["presets"][0]  # weights edited by hand: the owner's now


def test_save_optimized_reads_the_saved_run_and_keeps_held_out_names():
    persistence.save_view("P", "AAA, BBB, YNG", [])
    fr = [{"ret": 0.05, "cvar": 0.10, "cvar30": 0.10, "weights": {"AAA": 0.9, "BBB": 0.1}},
          {"ret": 0.07, "cvar": 0.12, "cvar30": 0.12, "weights": {"AAA": 0.4, "BBB": 0.6}}]  # fmt: skip
    run = persistence.save_mpt_run("P", {"params": {"lookback": "3Y"}, "symbols": ["AAA", "BBB"],
                                         "missing": ["YNG"], "frontier": fr,
                                         "anchors": {"current": {"ret": 0.06, "cvar": 0.125, "cvar30": 0.125}},
                                         "held_out": [{"symbol": "YNG", "weight": 0.1}],
                                         "meta": {"n_nonconv": 2}})  # fmt: skip
    out = whatif.save_optimized({"view": "P", "run_id": run["id"], "rule": "same_risk"})
    assert out["index"] == 1 and out["weights"] == {"AAA": 0.36, "BBB": 0.54, "YNG": 0.1}
    assert out["active"] == out["name"] and out["warnings"]  # n_nonconv: saved, with a warning
    saved = next(p for p in out["presets"] if p["name"] == out["name"])
    assert (
        saved["source"]["rule"] == "same_risk" and saved["source"]["held_out"][0]["symbol"] == "YNG"
    )
    for bad in ({"view": "P", "run_id": "nope", "rule": "same_risk"},
                {"view": "P", "run_id": run["id"], "rule": "selected", "index": 9},
                {"view": "P", "run_id": run["id"], "rule": "max_ratio"}):  # fmt: skip
        n = len(persistence.list_weight_presets("P")["presets"])
        with pytest.raises(whatif.WhatIfError):
            whatif.save_optimized(bad)
        assert len(persistence.list_weight_presets("P")["presets"]) == n  # nothing was saved


# ----------------------------------------------------------------- routes


@pytest.fixture
def srv():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _post(base, path, body):
    req = urllib.request.Request(base + path, method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_routes(srv, market):
    code, out = _post(srv, "/api/whatif", {"book_id": "main", "ccy": "EUR"})
    assert code == 200 and out["name"] == "What-if 1"
    assert _post(srv, "/api/whatif", {"book_id": "abcdefabcdef"})[0] == 409
    assert _post(srv, "/api/whatif", {"book_id": "main", "name": "__book__"})[0] == 400
    with urllib.request.urlopen(
        srv + "/api/whatif/book?ccy=EUR&view=What-if%201&symbols=AAPL,MSFT"
    ) as r:
        book = json.loads(r.read())
    assert book["match"]["AAPL"]["weight"] > 0 and book["whatif"]["changed"] is False
    code, out = _post(
        srv, "/api/whatif/rebalance", {"book_id": "main", "view": "What-if 1", "ccy": "EUR"}
    )
    assert code == 200 and out["summary"]["n_trades"] == 0
    code, out = _post(
        srv, "/api/whatif/rebalance", {"book_id": "main", "view": "nope", "ccy": "EUR"}
    )
    assert code == 400 and out["error"] == "NoTarget"
    assert (
        _post(srv, "/api/weight-presets/optimized", {"view": "__book__", "rule": "same_risk"})[0]
        == 400
    )
    code, out = _post(
        srv,
        "/api/weight-presets/optimized",
        {"view": "What-if 1", "rule": "same_risk", "run_id": "x"},
    )
    assert code == 404 and out["error"] == "RunGone"
    with urllib.request.urlopen(srv + "/api/mpt-runs?view=What-if%201") as r:
        assert json.loads(r.read())["plug"]["budget"] == "light"


def test_the_book_page_is_read_through_the_memo(monkeypatch):
    """whatif._page never computes the book when the memo has it, and never
    uses the disk copy (hours-old prices)."""
    monkeypatch.undo()
    calls = []
    monkeypatch.setattr(inv, "computed_page", lambda ccy: {"rev": 1, "stale": True})
    monkeypatch.setattr(inv, "page", lambda ccy: calls.append(ccy) or {"rev": 2})
    assert whatif._page("main", "EUR")["rev"] == 2 and calls == ["EUR"]
    monkeypatch.setattr(inv, "computed_page", lambda ccy: {"rev": 3})
    assert whatif._page("main", "EUR")["rev"] == 3 and calls == ["EUR"]


def test_a_refresh_save_keeps_the_block():
    """The block is written by the server only (create); every later save of
    the rows must carry it, or a what-if turns plain on its first refresh."""
    assert "whatif" in persistence._VIEW_CARRY
