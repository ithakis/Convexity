"""My Investments: the store, undo/redo, the page payload and the routes
(investments.py). Every network call is stubbed; public tickers only."""

import http.server
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from convexity import investments as inv
from convexity import paths, server

SEED = Path(__file__).resolve().parents[1] / "scripts" / "seed_demo_book.py"
QUOTE = {"AAPL": "USD", "MSFT": "USD", "NVDA": "USD", "SHEL.L": "GBp", "VWCE.DE": "EUR"}
LAST = {"AAPL": 250.0, "MSFT": 500.0, "NVDA": 180.0, "SHEL.L": 2700.0, "VWCE.DE": 140.0}
FX = {"USD": 1.0, "GBP": 1.3, "EUR": 1.1}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No network: quote currencies, names, splits, closes and FX are fixed."""
    inv._SPLITS.clear()
    monkeypatch.setattr(inv, "_fetch_quote_ccy", lambda s: QUOTE.get(s))
    monkeypatch.setattr(inv, "_pack_hit", lambda t: None)
    monkeypatch.setattr(inv, "_resolve", lambda text: text.strip().upper())
    monkeypatch.setattr(
        inv, "_fetch_splits", lambda s: [("2024-06-10", 10.0)] if s == "NVDA" else []
    )
    monkeypatch.setattr(
        inv, "_fx_series", lambda ccy: pd.Series([FX[ccy]], index=pd.to_datetime(["2000-01-03"]))
    )
    monkeypatch.setattr(inv, "_fx_now", lambda ccy, base: FX[ccy] / FX[base])

    def closes(symbols):
        days = pd.to_datetime([date.today() - timedelta(days=45), date.today() - timedelta(days=1)])
        return pd.DataFrame({s: [LAST[s] * 0.9, LAST[s]] for s in symbols if s in LAST}, index=days)

    monkeypatch.setattr(inv, "_closes", closes)


def _seed():
    spec = importlib.util.spec_from_file_location("seed_demo_book", SEED)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    path = paths.state_file("investments")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mod.build_book()), encoding="utf-8")


def _raw():
    return json.loads(paths.state_file("investments").read_text(encoding="utf-8"))


BUY = {"type": "buy", "symbol": "AAPL", "date": "2025-01-02", "qty": 10, "price": 200, "fee": 1}


# ------------------------------------------------------------------- store


def test_no_book_is_empty():
    assert inv.book() == {"empty": True, "rev": 0, "undo_label": None, "redo_label": None}
    assert inv.book_symbols() == []


def test_add_update_delete_undo_redo():
    b = inv.add(BUY, 0)
    assert b["rev"] == 1 and b["positions"][0]["shares"] == 10
    assert b["undo_label"] == "Added buy · 10 AAPL" and b["redo_label"] is None
    eid = b["entries"][0]["id"]
    b = inv.update(eid, {"qty": 12}, 1)
    assert b["positions"][0]["shares"] == 12 and b["undo_label"].startswith("Edited buy")
    b = inv.undo(2)
    assert b["positions"][0]["shares"] == 10 and b["redo_label"].startswith("Edited")
    b = inv.redo(3)
    assert b["positions"][0]["shares"] == 12
    b = inv.delete(eid, 4)
    assert b["empty"] is True and b["undo_label"].startswith("Deleted buy")
    b = inv.undo(5)
    assert b["positions"][0]["shares"] == 12
    # A new change after an undo clears redo.
    inv.undo(6)
    b = inv.add(BUY | {"symbol": "MSFT"}, 7)
    assert b["redo_label"] is None
    assert len(_raw()["redo"]) == 0


def test_undo_restores_an_entry_at_its_old_position():
    inv.add(BUY, 0)
    inv.add(BUY | {"qty": 5}, 1)
    first = _raw()["entries"][0]["id"]
    inv.delete(first, 2)
    inv.undo(3)
    assert _raw()["entries"][0]["id"] == first


def test_stale_rev_is_refused():
    inv.add(BUY, 0)
    with pytest.raises(inv.StaleRev):
        inv.add(BUY, 0)
    with pytest.raises(inv.StaleRev):
        inv.undo("not a number")
    assert _raw()["rev"] == 1


def test_journal_is_capped():
    rev = 0
    for _ in range(inv.JOURNAL_MAX + 5):
        rev = inv.add(BUY | {"qty": 1}, rev)["rev"]
    assert len(_raw()["journal"]) == inv.JOURNAL_MAX


def test_oversell_is_refused_in_plain_words_and_nothing_is_saved():
    inv.add(BUY, 0)
    with pytest.raises(inv.Refused) as ex:
        inv.add(
            {"type": "sell", "symbol": "AAPL", "date": "2025-02-03", "qty": 11, "price": 210}, 1
        )
    assert ex.value.status == 422
    assert ex.value.message == "That sells 11 AAPL on 3 Feb 2025, but you hold 10 then."
    assert _raw()["rev"] == 1 and len(_raw()["entries"]) == 1


def test_an_edit_that_breaks_a_later_sale_names_that_sale():
    b = inv.add(BUY, 0)
    eid = b["entries"][0]["id"]
    inv.add({"type": "sell", "symbol": "AAPL", "date": "2025-03-03", "qty": 10, "price": 210}, 1)
    with pytest.raises(inv.Refused) as ex:
        inv.update(eid, {"qty": 5}, 2)
    assert "sale of 10" in ex.value.message and "3 Mar 2025" in ex.value.message


@pytest.mark.parametrize(
    "fields, words",
    [
        (
            {"type": "buy", "symbol": "AAPL", "date": "2025-01-02", "qty": 0, "price": 1},
            "more than zero",
        ),
        ({"type": "buy", "symbol": "AAPL", "date": "2999-01-02", "qty": 1, "price": 1}, "future"),
        (
            {"type": "buy", "symbol": "", "date": "2025-01-02", "qty": 1, "price": 1},
            "Choose a company",
        ),
        ({"type": "deposit", "date": "2025-01-02", "amount": "abc"}, "number"),
        (
            {"type": "dividend", "symbol": "AAPL", "date": "2025-01-02", "amount": 5, "tax": 6},
            "tax",
        ),
        ({"type": "split", "symbol": "AAPL", "date": "2025-01-02", "ratio": 1}, "ratio"),
        ({"type": "nonsense", "date": "2025-01-02"}, "kind of entry"),
        ({"type": "buy", "symbol": "ZZZZ", "date": "2025-01-02", "qty": 1, "price": 1}, "Yahoo"),
    ],
)
def test_bad_fields_are_refused(fields, words):
    with pytest.raises(inv.BookError) as ex:
        inv.add(fields, 0)
    assert words in ex.value.message
    assert not paths.state_file("investments").exists()


def test_a_malformed_book_is_never_overwritten():
    path = paths.state_file("investments")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    b = inv.book()
    assert b["empty"] is True and "damaged" in b["error"]
    with pytest.raises(inv.BookUnreadable) as ex:
        inv.add(BUY, 0)
    assert ex.value.status == 409
    assert path.read_text(encoding="utf-8") == "{not json"
    assert inv.book_symbols() == []


def test_a_newer_schema_is_refused_not_rewritten():
    path = paths.state_file("investments")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 99, "entries": []}), encoding="utf-8")
    with pytest.raises(inv.BookUnreadable):
        inv.add(BUY, 0)


def test_every_write_keeps_a_backup_of_the_previous_file():
    inv.add(BUY, 0)
    inv.add(BUY | {"qty": 3}, 1)
    bak = json.loads(paths.state_file("investments").with_name("investments.json.bak").read_text())
    assert bak["rev"] == 1


def test_logs_never_carry_amounts(capsys):
    b = inv.add(BUY | {"qty": 123457, "price": 98765.4}, 0)
    inv.update(b["entries"][0]["id"], {"price": 55555}, 1)
    out = capsys.readouterr().out
    assert "[investments] add" in out and "AAPL" in out
    for secret in ("123457", "98765", "55555"):
        assert secret not in out


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"version": "x", "entries": []}),
        json.dumps({"rev": "x", "entries": []}),
        json.dumps({"entries": [1]}),
        json.dumps({"entries": [{"type": "buy"}]}),
        json.dumps({"entries": [{"type": "buy", "date": "2025-01-02"}]}),
    ],
)
def test_a_book_with_a_bad_shape_is_unreadable_not_a_crash(text):
    path = paths.state_file("investments")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    assert "error" in inv.book() and inv.book_symbols() == []
    with pytest.raises(inv.BookUnreadable):
        inv.add(BUY, 0)
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    "patch, words",
    [
        ({"qty": True}, "number"),
        ({"price": 1e300}, "too large"),
        ({"ccy": "<b>x</b>"}, "currency"),
        ({"ccy": ["USD"]}, "currency"),
        ({"symbol": "A" * 500}, "company"),
    ],
)
def test_odd_values_are_refused(patch, words):
    with pytest.raises(inv.BookError) as ex:
        inv.add(BUY | patch, 0)
    assert words in ex.value.message


def test_only_known_sources_are_stored():
    inv.add(BUY | {"source": {"x": 1}}, 0)
    inv.add(BUY | {"source": "quick"}, 1)
    assert [e["source"] for e in _raw()["entries"]] == ["manual", "quick"]


def test_changing_the_company_of_an_entry_takes_the_new_currency():
    b = inv.add(BUY | {"symbol": "SHEL.L", "fx_rate": 1.25}, 0)
    eid = b["entries"][0]["id"]
    inv.update(eid, {"symbol": "VWCE.DE"}, 1)
    e = _raw()["entries"][0]
    assert e["symbol"] == "VWCE.DE" and e["ccy"] == "EUR" and e["fx_rate"] is None


def test_a_pence_holding_without_a_yahoo_price_is_shown_in_pence(monkeypatch):
    monkeypatch.setattr(inv, "_closes", lambda symbols: pd.DataFrame())
    b = inv.add(BUY | {"symbol": "SHEL.L", "price": 25.0, "fee": 0}, 0)
    p = b["positions"][0]
    assert p["quote_ccy"] == "GBp" and p["price"] == pytest.approx(2500)
    assert p["avg_price"] == pytest.approx(2500) and p["note"]


# ----------------------------------------------------------------- payload


def test_seeded_demo_book_replays_through_the_engine():
    _seed()
    b = inv.book()
    assert b["demo"] is True and b["empty"] is False and not b["warnings"]
    pos = {p["symbol"]: p for p in b["positions"]}
    assert pos["NVDA"]["shares"] == 120  # 20 pre-split x 10, less 80 sold
    assert pos["MSFT"]["shares"] == 50 and pos["SHEL.L"]["shares"] == 300
    assert pos["SHEL.L"]["avg_price"] == pytest.approx(2541.0)  # pence, fee included
    h = b["headline"]
    # Value = positions + cash; weights + cash = 100%; profit = value - money in.
    assert sum(p["value"] for p in b["positions"]) + h["cash"] == pytest.approx(h["value"])
    assert sum(p["weight"] for p in b["positions"]) + h["cash_weight"] == pytest.approx(100)
    assert h["profit_total"] == pytest.approx(h["value"] - h["net_deposits"])
    assert h["profit_held"] + h["profit_sold"] + h["dividends"] - h["fees"] == pytest.approx(
        h["profit_total"]
    )
    assert h["net_deposits"] == pytest.approx(65000)
    # Performance arrives in Phase 3: empty, never made up.
    assert h["mwr_ann"] is None and set(h["twr"].values()) == {None} and "series" not in b
    # FX split only for holdings quoted in another currency.
    assert pos["MSFT"]["profit_fx"] is None
    assert pos["VWCE.DE"]["profit_price"] + pos["VWCE.DE"]["profit_fx"] == pytest.approx(
        pos["VWCE.DE"]["profit_held"]
    )
    assert inv.book_symbols() == sorted(pos)


def test_a_real_book_is_computed_and_never_demo():
    b = inv.add(BUY, 0)
    assert b["demo"] is False
    p = b["positions"][0]
    assert p["value"] == pytest.approx(2500) and p["profit_held"] == pytest.approx(2500 - 2001)
    assert b["headline"]["implied_deposits"] == pytest.approx(2001)


def test_period_starts_clamp_month_ends_and_use_jan_1_for_ytd():
    dates = ["2025-12-31", "2026-01-02", "2026-02-27", "2026-03-02", "2026-03-31"]
    st = inv.period_starts(dates)
    assert dates[st["1M"]] == "2026-02-27"
    assert dates[st["YTD"]] == "2025-12-31"
    assert st["ALL"] == 0


# ------------------------------------------------------------------ routes


@pytest.fixture
def srv():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _post(base, path, body, ctype="application/json"):
    req = urllib.request.Request(base + path, method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": ctype})  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _delete(base, path):
    req = urllib.request.Request(base + path, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_routes_round_trip(srv):
    code, b = _post(srv, "/api/investments/entries", {"base_rev": 0, "entry": BUY})
    assert code == 200 and b["rev"] == 1
    code, b2 = _post(srv, "/api/investments/entries", {"base_rev": 0, "entry": BUY})
    assert code == 409 and b2["error"] == "StaleRev"
    code, b3 = _post(
        srv,
        "/api/investments/entries",
        {"base_rev": 1, "entry": BUY | {"type": "sell", "qty": 99, "date": "2025-02-03"}},
    )
    assert code == 422 and "you hold 10" in b3["message"]
    code, b4 = _post(srv, "/api/investments/undo", {"base_rev": 1})
    assert code == 200 and b4["empty"] is True
    code, b5 = _post(srv, "/api/investments/redo", {"base_rev": 2})
    assert code == 200 and b5["positions"][0]["symbol"] == "AAPL"
    with urllib.request.urlopen(srv + "/api/investments/symbols") as r:
        assert json.loads(r.read()) == {"symbols": ["AAPL"]}
    assert _post(srv, "/api/investments/undo", {"base_rev": 3}, "text/plain")[0] == 403


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/watchlists", {"name": "__book__", "entries": "AAPL"}),
        ("/api/portfolio/rename", {"old": "__book__", "new": "Mine"}),
        ("/api/portfolio/rename", {"old": "Mine", "new": "__book__"}),
        ("/api/mpt-runs", {"view": "__book__", "run": {}}),
        ("/api/weight-presets", {"view": "__book__", "name": "p", "weights": {"AAPL": 1}}),
        ("/api/weight-presets/active", {"view": "__book__", "name": None}),
    ],
)
def test_the_book_tab_name_is_reserved(srv, path, body):
    code, out = _post(srv, path, body)
    assert code == 400 and "read-only" in out["error"]


def test_the_book_tab_cannot_be_deleted_but_its_rows_can_be_cached(srv):
    assert _delete(srv, "/api/views/__book__") == 400
    assert _delete(srv, "/api/weight-presets?view=__book__&name=p") == 400
    code, _ = _post(srv, "/api/views/__book__", {"entries": "AAPL", "rows": [{"symbol": "AAPL"}]})
    assert code == 200


def test_seed_refuses_without_convexity_home(monkeypatch):
    import subprocess
    import sys

    monkeypatch.delenv("CONVEXITY_HOME", raising=False)
    r = subprocess.run([sys.executable, str(SEED)], capture_output=True, text=True)
    assert r.returncode == 2
    assert "Refusing" in r.stdout


@pytest.mark.parametrize("entry", [[1, 2], "buy", 5])
def test_an_entry_that_is_not_an_object_is_a_400(srv, entry):
    code, out = _post(srv, "/api/investments/entries", {"base_rev": 0, "entry": entry})
    assert code == 400


def test_a_preset_may_be_called_like_the_book_tab(srv):
    _post(srv, "/api/views/Mine", {"entries": "AAPL", "rows": [{"symbol": "AAPL"}]})
    body = {"view": "Mine", "name": "__book__", "weights": {"AAPL": 1}}
    code, out = _post(srv, "/api/weight-presets", body)
    assert code == 200, out
