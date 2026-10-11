"""My Investments import (importer.py): what plain code decides after the
models have read. No network: both NIM stages and the symbol pack are
stubbed; public tickers and made-up amounts only."""

import base64
import struct
import zlib
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from convexity import importer as im
from convexity import news_sentiment as ns
from convexity import symbol_db
from convexity.symbol_db import Hit

GOLD = Path(__file__).resolve().parent / "data" / "import"


def _hit(ticker, name, exchange, type_="stock"):
    return Hit(ticker, name, type_, exchange, None, None, None, None, 0, 100.0)


PACK = {h.ticker: h for h in [
    _hit("AAPL", "Apple Inc.", "NMS"), _hit("APLE", "Apple Hospitality REIT", "NYQ"),
    _hit("SHEL", "Shell plc", "NYQ"), _hit("SHEL.L", "Shell plc", "LSE"),
    _hit("MSFT", "Microsoft Corporation", "NMS"), _hit("NVDA", "NVIDIA Corporation", "NMS"),
    _hit("ASML", "ASML Holding N.V.", "NMS"), _hit("ASML.AS", "ASML Holding N.V.", "AMS"),
    _hit("VUSA.L", "Vanguard S&P 500 UCITS ETF", "LSE", "etf"),
]}  # fmt: skip
NAMES = {"apple": ["AAPL", "APLE"], "shell": ["SHEL", "SHEL.L"], "microsoft": ["MSFT"],
         "nvidia": ["NVDA"], "asml": ["ASML", "ASML.AS"]}  # fmt: skip


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(symbol_db, "get", lambda t: PACK.get((t or "").upper()))

    def lookup(q, *, limit=5, min_score=72.0):
        key = (q or "").lower().split()[0] if q else ""
        return [PACK[t] for t in NAMES.get(key, [])][:limit]

    monkeypatch.setattr(symbol_db, "lookup", lookup)
    monkeypatch.setattr(
        symbol_db, "listings", lambda g: [h for h in PACK.values() if h.group == g and g]
    )
    monkeypatch.setattr(symbol_db, "yahoo_lookup", lambda q, **k: [])
    monkeypatch.setattr(im, "_last_closes", lambda syms: {})
    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "test-key")

    def no_network(*a, **k):
        raise AssertionError("a test reached NVIDIA NIM")

    monkeypatch.setattr(im, "_vision_call", no_network)
    monkeypatch.setattr(im, "_structure", no_network)


def _model(monkeypatch, items, skipped=(), transcript="Apple | Buy | 10 shares at $182.40"):
    """Both NIM stages stubbed: the vision transcript and the rows read."""
    monkeypatch.setattr(im, "_vision_call", lambda data, mime, label: transcript)
    monkeypatch.setattr(
        im, "_structure", lambda text, today, base: {"items": items, "skipped": list(skipped)}
    )


def _row(**kw):
    base = {"type": "buy", "date": "2024-03-12", "date_guessed": False, "name": "Apple", "ticker": "AAPL",
            "ticker_guessed": False, "qty": 10, "price": 182.4, "currency": "USD", "total": None,
            "total_currency": "", "fee": None, "tax": None, "ratio": None, "source": "line 1"}  # fmt: skip
    return base | kw


def _file(name, data, mime):
    return {"name": name, "type": mime, "data": base64.b64encode(data).decode()}


# ------------------------------------------------------------------ matching


def test_pence_become_pounds_and_the_currency_picks_the_london_line(monkeypatch):
    _model(monkeypatch, [_row(name="Shell", ticker="SHEL", currency="GBX", price=2610.0, qty=40)])
    r = im.read("Shell SHEL 40 at 2610p")["items"][0]
    assert r["symbol"] == "SHEL.L" and r["ccy"] == "GBP"
    assert r["price"] == pytest.approx(26.10)
    # The ticker was read and the line chosen by code from the currency: not an AI guess.
    assert "symbol" not in r["guessed"]


def test_a_ticker_counts_as_read_only_if_it_is_in_the_source(monkeypatch):
    _model(monkeypatch, [_row()])
    assert im.read("Bought 10 AAPL at 182.40 on 12 March 2024")["items"][0]["guessed"] == []
    assert "symbol" in im.read("Bought 10 Apple at 182.40 on 12 March 2024")["items"][0]["guessed"]


def test_the_euro_line_of_a_dual_listing(monkeypatch):
    _model(
        monkeypatch, [_row(name="ASML Holding", ticker="ASML", currency="EUR", price=885.4, qty=3)]
    )
    assert im.read("ASML 3 @ 885.40 EUR")["items"][0]["symbol"] == "ASML.AS"


def test_a_remembered_name_wins_and_is_not_a_guess(monkeypatch):
    _model(
        monkeypatch,
        [_row(name="Shell", ticker="", currency="", price=24.1, qty=150, type="holding", date="")],
    )
    r = im.read("Shell 150 avg 24.10", aliases={"shell": "SHEL.L"})["items"][0]
    assert r["symbol"] == "SHEL.L" and "symbol" not in r["guessed"]


def test_no_listing_question_for_a_name_alike(monkeypatch):
    # Apple Hospitality is another company, not another listing of Apple.
    _model(monkeypatch, [_row(name="Apple", ticker="", currency="")])
    out = im.read("10 Apple at 182.40 on 12 March 2024")
    assert out["items"][0]["symbol"] == "AAPL" and not out["questions"]


# -------------------------------------------------------------------- checks


def test_qty_times_price_is_checked_against_the_total(monkeypatch):
    _model(monkeypatch, [_row(ticker="MSFT", name="Microsoft", qty=20, price=405.05, total=8100.0)])
    r = im.read("MSFT 20 405.05 8100")["items"][0]
    assert r["checks"] and r["fix"] == {"price": 405.0}


def test_a_fee_inside_the_total_is_not_a_mismatch(monkeypatch):
    _model(
        monkeypatch,
        [_row(ticker="MSFT", name="Microsoft", qty=8, price=370.6, total=2965.8, fee=1.0)],
    )
    assert im.read("MSFT")["items"][0]["checks"] == []


def test_undated_holdings_start_today_as_a_quiet_default_not_a_question(monkeypatch):
    # No "Since when" question (owner, 2026-10-10): each row has its own date.
    today = date.today().isoformat()
    _model(monkeypatch, [_row(type="holding", date=today, date_guessed=True, qty=40, price=151.2),
                         _row(type="buy", date="", qty=1, price=100.0)])  # fmt: skip
    out = im.read("Apple 40 avg 151.20")
    held, buy = out["items"]
    assert held["date"] == today and held.get("date_default") and "date" not in held["missing"]
    assert "date" not in held["guessed"]
    # A trade with no date still needs the owner.
    assert buy["date"] == "" and "date" in buy["missing"] and not buy.get("date_default")
    assert not any(q["id"] == "since" for q in out["questions"])


def test_a_holding_with_no_share_count_is_left_out(monkeypatch):
    _model(
        monkeypatch,
        [_row(), _row(type="holding", name="Portfolio value", ticker="XXX", qty=None, price=None)],
    )
    out = im.read("AAPL")
    assert len(out["items"]) == 1 and any("no share count" in s for s in out["skipped"])


def test_a_holdings_list_is_reconciled_against_the_book(monkeypatch):
    _model(monkeypatch, [_row(type="holding", date="", qty=40, price=151.2),
                         _row(type="holding", date="", ticker="MSFT", name="Microsoft", qty=25, price=300.0)])  # fmt: skip
    a, m = im.read("AAPL MSFT", held={"AAPL": 40.0, "MSFT": 20.0})["items"]
    assert a["include"] is False and "Already in your book" in a["checks"][0]
    assert m["include"] is True and m["fix"] == {"qty": 5.0}


# --------------------------------------------------------------------- input


@pytest.mark.parametrize("name,mime,data,msg", [
    ("IMG_1.HEIC", "image/heic", b"x", "HEIC"),
    ("old.xls", "", b"x", "old Excel"),
    ("notes.docx", "", b"x", "use screenshots"),
    ("big.png", "image/png", b"x" * (im.MAX_FILE_BYTES + 1), "larger than"),
])  # fmt: skip
def test_files_are_refused_in_plain_words(name, mime, data, msg):
    with pytest.raises(im.ImportRefused) as ex:
        im.read("", [_file(name, data, mime)])
    assert msg in ex.value.message


def test_bad_base64_and_nothing_given_are_refused():
    with pytest.raises(im.ImportRefused):
        im.read("", [{"name": "a.png", "type": "image/png", "data": "%%%"}])
    with pytest.raises(im.ImportRefused):
        im.read("   ")


def test_no_key_is_a_409_with_a_way_out(monkeypatch):
    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "")
    with pytest.raises(im.ImportRefused) as ex:
        im.read("10 Apple")
    assert ex.value.status == 409 and "by hand" in ex.value.message


def test_screenshots_go_through_the_vision_stage(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        im,
        "_vision_call",
        lambda data, mime, label: seen.setdefault("t", "AAPL | Buy | 10 at 182.40"),
    )
    monkeypatch.setattr(
        im,
        "_structure",
        lambda text, today, base: (
            seen.setdefault("text", text) and {"items": [_row()], "skipped": []}
        ),
    )
    out = im.read("", [_file("shot.png", b"\x89PNG fake", "image/png")])
    assert "Image 1 (shot.png)" in seen["text"] and out["summary"].startswith(
        "Found 1 buy in 1 screenshot"
    )


def test_pdf_text_and_scanned_pages():
    text, scans = im.pdf_parts("statement.pdf", (GOLD / "statement.pdf").read_bytes())
    assert "JPM" in text and scans == []
    text, scans = im.pdf_parts("scanned.pdf", (GOLD / "scanned.pdf").read_bytes())
    assert text == "" and len(scans) == 1 and scans[0][:8] == b"\x89PNG\r\n\x1a\n"


def test_a_broken_pdf_is_refused():
    with pytest.raises(im.ImportRefused):
        im.pdf_parts("x.pdf", b"%PDF-1.4 not really")


def test_png_bytes_is_a_valid_png():
    px = np.zeros((3, 5, 4), dtype=np.uint8)
    px[..., 0] = 200
    png = im.png_bytes(px)
    w, h = struct.unpack(">II", png[16:24])
    assert (w, h) == (5, 3)
    idat = png[png.index(b"IDAT") + 4 : png.index(b"IEND") - 8]
    raw = zlib.decompress(idat)
    assert len(raw) == 3 * (1 + 5 * 3) and raw[1] == 200


def test_sheet_parts_read_csv_and_each_xlsx_sheet_with_its_header():
    import io

    import pandas as pd

    [(label, text)] = im.sheet_parts("a.csv", (GOLD / "broker_a.csv").read_bytes())
    assert label == "a.csv" and "AAPL" in text
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as xw:
        pd.DataFrame({"Ticker": ["KO"] * 200, "Qty": range(200)}).to_excel(
            xw, sheet_name="Trades", index=False
        )
        pd.DataFrame({"Ticker": ["JNJ"], "Qty": [5]}).to_excel(
            xw, sheet_name="Dividends", index=False
        )
    parts = im.sheet_parts("b.xlsx", buf.getvalue())
    assert [lbl for lbl, _ in parts] == ["b.xlsx, sheet Trades", "b.xlsx, sheet Dividends"]
    # Review finding: a "# sheet" line used to head the block, so every chunk
    # after the first lost the column names. Now each chunk starts with them.
    chunks = im._chunks(parts[0][1])
    assert len(chunks) == -(-200 // (im._CHUNK_LINES - 1)) and all(
        c.startswith("Ticker,Qty") for c in chunks
    )


def test_long_text_is_chunked_with_its_header():
    lines = ["Date,Ticker,Qty"] + [f"2024-01-{i % 28 + 1:02d},AAPL,{i}" for i in range(200)]
    chunks = im._chunks("\n".join(lines))
    assert len(chunks) == -(-200 // (im._CHUNK_LINES - 1)) and all(
        c.startswith("Date,Ticker,Qty") for c in chunks
    )


def test_a_failed_scan_does_not_sink_a_pdfs_text_pages(monkeypatch):
    # Review finding: the "nothing could be read" refusal ignored the text
    # already taken from a PDF. Now the text is read and the scan is named.
    monkeypatch.setattr(
        im,
        "pdf_parts",
        lambda name, data: ("page 1:\nBought 15 JPM at 198.40 on 3 Jun 2024", [b"png"]),
    )
    monkeypatch.setattr(im, "_vision_call", lambda data, mime, label: None)
    monkeypatch.setattr(
        im,
        "_structure",
        lambda text, today, base: {"items": [_row(ticker="JPM", name="JPMorgan")], "skipped": []},
    )
    out = im.read("", [_file("statement.pdf", b"%PDF", "application/pdf")])
    assert len(out["items"]) == 1 and out["unread"] == ["statement.pdf (scanned page 1)"]


def test_an_entry_left_out_out_of_doubt_is_read_again(monkeypatch):
    calls = []

    def structure(text, today, base):
        calls.append(text)
        if "Last time these entries" not in text:
            return {"items": [_row(type="holding", date="", name="Shell", ticker="SHEL", currency="GBP", qty=300, price=25.4)],
                    "skipped": ["Coca-Cola holding skipped: can't confirm the ticker"]}  # fmt: skip
        return {"items": [_row(type="holding", date="", name="Shell", ticker="SHEL", currency="GBP", qty=300, price=25.4),
                          _row(type="holding", date="", name="Coca-Cola", ticker="KO", qty=40, price=61.15)],
                "skipped": []}  # fmt: skip

    monkeypatch.setattr(im, "_structure", structure)
    out = im.read("300 Shell at 25.40 pounds and 40 Coca-Cola at 61.15")
    rereads = [c for c in calls if "Last time these entries" in c]
    assert len(rereads) == 1 and "Coca-Cola holding skipped" in rereads[0]
    assert [r["name"] for r in out["items"]] == ["Shell plc", "Coca-Cola"]


def test_a_line_left_out_as_a_balance_is_not_read_again(monkeypatch):
    calls = []
    monkeypatch.setattr(
        im,
        "_structure",
        lambda text, today, base: (
            calls.append(text) or {"items": [_row()], "skipped": ["Portfolio value: a balance"]}
        ),
    )
    im.read("AAPL")
    assert not any("Last time these entries" in c for c in calls)


def test_a_short_block_is_read_twice_and_the_fuller_reading_wins(monkeypatch):
    import itertools

    answers = itertools.cycle([
        {"items": [_row()], "skipped": []},
        {"items": [_row(), _row(ticker="MSFT", name="Microsoft", qty=5, price=405.0)], "skipped": []},
    ])  # fmt: skip
    monkeypatch.setattr(im, "_structure", lambda text, today, base: next(answers))
    out = im.read("AAPL and MSFT")
    assert [r["symbol"] for r in out["items"]] == ["AAPL", "MSFT"]


def test_other_listings_are_the_same_company_only(monkeypatch):
    # Verify finding: the picker offered Apple Hospitality as an Apple listing.
    _model(monkeypatch, [_row()])
    assert im.read("Bought 10 AAPL at 182.40")["items"][0]["alts"] == []
    _model(monkeypatch, [_row(name="Shell", ticker="SHEL", currency="GBP", price=24.1)])
    assert [a["symbol"] for a in im.read("Shell SHEL")["items"][0]["alts"]] == ["SHEL"]


# ---------------------------------------------------- the exchange shown


def _grp(ticker, name, exchange, group):
    return Hit(ticker, name, "stock", exchange, None, None, None, None, group, 100.0)


EU = {h.ticker: h for h in [
    _grp("AIR.PA", "Airbus SE", "PAR", 11), _grp("AIR.DE", "Airbus SE", "GER", 11),
    _grp("ITX.MC", "Industria de Diseno Textil, S.A.", "MCE", 12),
    _grp("ITX.DE", "Industria de Diseno Textil, S.A.", "GER", 12),
    _grp("ALV.DE", "Allianz SE", "GER", 13), _grp("ALIZY", "Allianz SE", "PNK", 13),
    _grp("ULVR.L", "Unilever PLC", "LSE", 14), _grp("UL", "Unilever PLC", "NYQ", 14),
    _grp("VOW3.DE", "Volkswagen AG", "GER", 15), _grp("VOW3.F", "Volkswagen AG", "FRA", 15),
    _grp("YNG", "Young Co, Inc.", "NMS", 16),
]}  # fmt: skip
EU_NAMES = {"airbus": ["AIR.DE", "AIR.PA"], "industria": ["ITX.DE", "ITX.MC"], "allianz": ["ALV.DE", "ALIZY"],
            "unilever": ["ULVR.L", "UL"], "volkswagen": ["VOW3.DE", "VOW3.F"], "young": ["YNG"]}  # fmt: skip


@pytest.fixture
def eu_pack(monkeypatch):
    every = {**PACK, **EU}
    monkeypatch.setattr(symbol_db, "get", lambda t: every.get((t or "").upper()))

    def lookup(q, *, limit=5, min_score=72.0):
        key = (q or "").lower().split()[0] if q else ""
        return [every[t] for t in EU_NAMES.get(key, NAMES.get(key, []))][:limit]

    monkeypatch.setattr(symbol_db, "lookup", lookup)
    monkeypatch.setattr(symbol_db, "listings", lambda g: [h for h in EU.values() if h.group == g])


@pytest.mark.parametrize(
    ("code", "want"),
    [("SBF", "Euronext Paris"), ("IBIS", "Xetra"), ("IBIS2", "Xetra"), ("BM", "Madrid"),
     ("BVME", "Milan"), ("AEB", "Euronext Amsterdam"), ("LSE", "London"), ("NYSE", "NYSE"),
     ("NASDAQ.NMS", "Nasdaq"), ("BUX", "Budapest"), ("XPAR", "Euronext Paris"),
     ("euronext paris", "Euronext Paris"), ("Xetra", "Xetra"), ("ARCA", "NYSE Arca"),
     ("TSE", "Toronto"), ("TSEJ", "Tokyo"), ("Foo", None), ("", None)],
)  # fmt: skip
def test_the_exchanges_brokers_show(code, want):
    assert im.venue(code) == want


@pytest.mark.parametrize(
    ("line", "want"),
    [("AIR | SBF | 171.40 | 166.85", "SBF"), ("AIR SBF | 171.40", "SBF"), ("AIR NASDAQ.NMS | 181.20", "NASDAQ.NMS"),
     ("2024-01-02,AIR,Xetra,10", "Xetra"), ("I have 300 AIR shares in London", None),
     ("AIR | 171.40 | Moon", None), ("AIRP | SBF", None)],
)  # fmt: skip
def test_the_exchange_is_read_by_code_beside_the_ticker(line, want):
    assert im.shown_exchange("AIR", "header\n" + line) == want


def test_no_exchange_code_names_two_venues():
    keys = [
        k
        for _c, _s, _y, ks in im._VENUES.values()
        for k in ks.split("|")[0].split() + ks.split("|")[1:]
    ]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize(
    ("ticker", "name", "shown", "want"),
    [("AIR", "Airbus", "SBF", "AIR.PA"), ("ITX", "Industria de Diseno Textil", "BM", "ITX.MC"),
     ("ALV", "Allianz", "IBIS", "ALV.DE"), ("UL", "Unilever", "NYSE", "UL")],
)  # fmt: skip
def test_the_exchange_shown_decides_the_listing(monkeypatch, eu_pack, ticker, name, shown, want):
    # Owner's report: every euro row went to the first euro line (.DE).
    _model(monkeypatch, [_row(type="holding", date="", name=name, ticker=ticker, currency="",
                              exchange=shown, qty=100, price=50.0)])  # fmt: skip
    r = im.read(f"{ticker} {shown} 100 50")["items"][0]
    assert r["symbol"] == want and "symbol" not in r["guessed"]
    assert r["note"].startswith("On ") and r["exchange_shown"]
    assert r["ccy"] == im._VENUES[im.venue(shown)][2]


def test_a_venue_yahoo_lacks_gets_the_same_currency_proxy_and_says_so(monkeypatch, eu_pack):
    # Volkswagen shown on Milan: no Milan line, so a euro line stands in.
    _model(monkeypatch, [_row(type="holding", date="", name="Volkswagen", ticker="VOW3",
                              currency="EUR", exchange="BVME", qty=10, price=90.0)])  # fmt: skip
    r = im.read("VOW3 BVME 10 90")["items"][0]
    assert r["symbol"] == "VOW3.DE" and "symbol" in r["guessed"]
    assert "no Milan line" in r["note"] and "same currency" in r["note"]


def test_no_same_currency_line_falls_back_to_the_home_listing(monkeypatch, eu_pack):
    _model(monkeypatch, [_row(type="holding", date="", name="Young Co", ticker="YNG",
                              currency="EUR", exchange="IBIS", qty=10, price=80.0)])  # fmt: skip
    r = im.read("YNG IBIS 10 80")["items"][0]
    assert r["symbol"] == "YNG" and "convert" in r["note"]


def test_a_line_only_yahoo_knows_is_found_live(monkeypatch, eu_pack):
    live = _grp("RICHT.BD", "Richter Gedeon Chemical Factory", "BUD", symbol_db.YAHOO_GROUP)
    monkeypatch.setattr(symbol_db, "yahoo_lookup", lambda q, **k: [live])
    _model(monkeypatch, [_row(type="holding", date="", name="Richter Gedeon", ticker="RICHT",
                              currency="", exchange="BUX", qty=20, price=12000.0)])  # fmt: skip
    r = im.read("RICHT BUX 20 12000")["items"][0]
    assert r["symbol"] == "RICHT.BD" and r["ccy"] == "HUF" and r["note"] == "On Budapest, as shown."


def test_typed_text_finds_a_line_only_yahoo_knows_by_currency(monkeypatch, eu_pack):
    # No exchange column ("40 Richter Gedeon at 9640 HUF"): the currency asks
    # for a line the pack lacks, and Yahoo finds it by the name's first word.
    live = _grp("RICHT.BD", "Richter Gedeon Chemical Factory", "BUD", symbol_db.YAHOO_GROUP)
    asked = []
    monkeypatch.setattr(
        symbol_db,
        "yahoo_lookup",
        lambda q, **k: asked.append(q) or ([live] if q == "Richter" else []),
    )
    _model(monkeypatch, [_row(type="holding", date="", name="Richter Gedeon", ticker="",
                              currency="HUF", exchange="", qty=40, price=9640.0)])  # fmt: skip
    r = im.read("40 Richter Gedeon at 9640 HUF")["items"][0]
    assert r["symbol"] == "RICHT.BD" and r["listing_ccy"] == "HUF"
    assert asked == ["Richter Gedeon", "Richter"]


def test_review_rows_carry_each_listings_currency(monkeypatch, eu_pack):
    _model(monkeypatch, [_row(name="Airbus", ticker="AIR", currency="EUR", exchange="SBF")])
    r = im.read("AIR SBF")["items"][0]
    assert r["listing_ccy"] == "EUR" and all("ccy" in a for a in r["alts"])


def test_an_unknown_exchange_keeps_the_currency_rule(monkeypatch, eu_pack):
    _model(monkeypatch, [_row(name="Airbus", ticker="AIR", currency="EUR", exchange="Moon")])
    r = im.read("AIR Moon")["items"][0]
    assert r["symbol"] == "AIR.DE" and r["note"] == "" and r["exchange_shown"] is None


def test_pence_read_as_pounds_are_caught_by_the_market_price(monkeypatch, eu_pack):
    # No currency on screen: 4380.50 is pence next to a close of 45.12 GBP.
    monkeypatch.setattr(im, "_last_closes", lambda syms: {"ULVR.L": (45.12, "GBp")})
    _model(monkeypatch, [_row(type="holding", date="", name="Unilever", ticker="ULVR",
                              currency="", exchange="LSE", qty=25, price=4380.50)])  # fmt: skip
    r = im.read("ULVR LSE 25 4380.50")["items"][0]
    assert r["symbol"] == "ULVR.L" and r["ccy"] == "GBP"
    assert r["price"] == pytest.approx(43.805) and "price" in r["guessed"]
    assert "pence" in r["checks"][0] and r["fix"] == {"price": 4380.50}


def test_a_pounds_price_stays_pounds(monkeypatch, eu_pack):
    monkeypatch.setattr(im, "_last_closes", lambda syms: {"ULVR.L": (45.12, "GBp")})
    _model(monkeypatch, [_row(type="holding", date="", name="Unilever", ticker="ULVR",
                              currency="GBP", exchange="LSE", qty=25, price=43.81)])  # fmt: skip
    r = im.read("ULVR LSE 25 43.81")["items"][0]
    assert r["price"] == pytest.approx(43.81) and r["checks"] == []


def test_a_price_far_from_the_market_is_flagged(monkeypatch):
    monkeypatch.setattr(im, "_last_closes", lambda syms: {"MSFT": (400.0, "USD")})
    _model(monkeypatch, [_row(ticker="MSFT", name="Microsoft", qty=1, price=4.0)])
    assert "far from today's price" in im.read("MSFT 1 4")["items"][0]["checks"][0]


def test_the_listing_choices_never_repeat_the_pick(monkeypatch, eu_pack):
    _model(monkeypatch, [_row(type="holding", date="", name="Airbus", ticker="AIR", currency="",
                              qty=100, price=150.0)])  # fmt: skip
    r = im.read("AIR | SBF | 100 | 150")["items"][0]
    assert r["symbol"] == "AIR.PA" and [a["symbol"] for a in r["alts"]] == ["AIR.DE"]


@pytest.mark.parametrize(
    ("qty", "line", "want"),
    [(40, "AIR SBF | 171.40 | 158.85 | 40 | 502.00", (158.85, 171.40)),
     (120, "ITX BM | 48.92 | 51.30 | 120 | -285.60", (51.30, 48.92)),
     (25, "ULVR LSE | 4512.00 | 4380.50 | 25 | 32.88", (4380.50, 4512.00)),  # pence, P&L in pounds
     (10, "Apple | 10 shares at $182.40 | -$1,824.00", None),
     (40, "Apple | 40 shares · avg $151.20 | $10,016.00 | +65.6%", None)],
)  # fmt: skip
def test_a_positions_rows_arithmetic_tells_average_from_last(qty, line, want):
    assert im._avg_from_pnl(qty, line) == want


def test_todays_price_taken_for_the_average_is_put_right(monkeypatch, eu_pack):
    _model(monkeypatch, [_row(type="holding", date="", name="Airbus", ticker="AIR", currency="",
                              qty=40, price=171.40)])  # fmt: skip
    r = im.read("INSTRUMENT | LAST | AVG | POS | P&L\nAIR SBF | 171.40 | 158.85 | 40 | 502.00")[
        "items"
    ][0]
    assert r["price"] == pytest.approx(158.85) and "price" in r["guessed"]
    assert "today's price" in r["checks"][0] and r["symbol"] == "AIR.PA"


@pytest.mark.parametrize(
    ("qty", "line", "want"),
    [(40, "AIR SBF | 171,40 | 158,85 | 40 | 502,00", (158.85, 171.40)),  # decimal commas
     (40, "AIR SBF | 1.171,40 | 1.158,85 | 40 | 502,00", (1158.85, 1171.40)),
     (1000, "X | 12.50 | 10.00 | 1,000 | 2,500.00", (10.0, 12.5))],
)  # fmt: skip
def test_the_arithmetic_reads_either_number_format(qty, line, want):
    assert im._avg_from_pnl(qty, line) == want


def test_a_price_the_arithmetic_cant_place_is_left_as_read(monkeypatch, eu_pack):
    # The model read 158.85 (the average) but the row parses another way: keep it.
    _model(monkeypatch, [_row(type="holding", date="", name="Airbus", ticker="AIR", currency="",
                              qty=40, price=99.0)])  # fmt: skip
    r = im.read("AIR SBF | 171.40 | 158.85 | 40 | 502.00")["items"][0]
    assert r["price"] == pytest.approx(99.0) and "price" not in r["guessed"]


def test_tse_with_a_numeric_ticker_is_tokyo():
    assert im.venue("TSE", "7203") == "Tokyo" and im.venue("TSE", "SHOP") == "Toronto"


def test_a_one_letter_ticker_is_a_whole_word():
    block = "h\nT-Mobile US | NASDAQ | 10\nAT&T Inc | NYSE | 5\nT | NYSE | 20"
    assert im.ticker_line("T", block) == "T | NYSE | 20" and im.shown_exchange("T", block) == "NYSE"


def test_an_exchange_column_may_name_the_venue():
    assert im.venue(im.shown_exchange("SHOP", "h\nSHOP | TSX | 10 | 120.00")) == "Toronto"


def test_percentages_are_not_numbers_for_the_arithmetic():
    assert [m.group() for m in im._NUM.finditer("+7.90% | −3.5% | 1.234,56 | 40")] == [
        "1.234,56",
        "40",
    ]


@pytest.mark.parametrize(("cur", "want"), [("ZAc", "ZAR"), ("ILA", "ILS"), ("GBX", "GBP")])
def test_minor_units_become_their_currency(monkeypatch, cur, want):
    _model(monkeypatch, [_row(ticker="MSFT", name="Microsoft", currency=cur, qty=10, price=1234.0)])
    r = im.read("MSFT 10 1234")["items"][0]
    assert r["ccy"] == want and r["price"] == pytest.approx(12.34)


def test_the_pence_reading_replaces_a_stale_total_fix(monkeypatch, eu_pack):
    monkeypatch.setattr(im, "_last_closes", lambda syms: {"ULVR.L": (45.12, "GBp")})
    _model(monkeypatch, [_row(type="holding", date="", name="Unilever", ticker="ULVR", currency="",
                              qty=10, price=4380.50, total=438.05)])  # fmt: skip
    r = im.read("ULVR LSE 10 4380.50 438.05")["items"][0]
    assert r["price"] == pytest.approx(43.805) and r["fix"] == {"price": 4380.50}
    assert not any("total shown" in c for c in r["checks"])


def test_a_currency_taken_from_the_exchange_is_marked(monkeypatch, eu_pack):
    _model(monkeypatch, [_row(type="holding", date="", name="Airbus", ticker="AIR", currency="",
                              qty=10, price=150.0)])  # fmt: skip
    r = im.read("AIR SBF 10 150")["items"][0]
    assert r["ccy"] == "EUR" and "ccy" in r["guessed"]


def test_a_name_that_is_only_ticker_and_exchange_is_not_a_company_name(monkeypatch, eu_pack):
    # A positions screen's instrument cell is "AIR SBF": the model copies it
    # as the name, and every correctly read row was marked as the AI's guess.
    _model(monkeypatch, [_row(type="holding", date="", name="AIR SBF", ticker="AIR",
                              currency="", exchange="SBF", qty=10, price=150.0)],
           transcript="INSTRUMENT | LAST | AVG | POS\nAIR SBF | 152.10 | 150.00 | 10")  # fmt: skip
    r = im.read("", [_file("shot.png", b"\x89PNG fake", "image/png")])["items"][0]
    assert r["symbol"] == "AIR.PA" and "symbol" not in r["guessed"]
    assert im._company_words("NVDA NASDAQ.NMS", "NVDA", "NASDAQ.NMS") == "NVDA"
    assert im._company_words("Airbus SE", "AIR", "SBF") == "Airbus SE"
    assert im._company_words("SBF", "", "SBF") == "SBF"  # no ticker: never an empty name
    assert im._company_words("Shell", "SHELL", "AEB") == "SHELL"
