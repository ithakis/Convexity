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


def test_undated_holdings_ask_since_when_and_today_is_not_a_reading(monkeypatch):
    today = date.today().isoformat()
    _model(monkeypatch, [_row(type="holding", date=today, date_guessed=True, qty=40, price=151.2)])
    out = im.read("Apple 40 avg 151.20")
    r = out["items"][0]
    assert r["date"] == "" and "date" in r["missing"]
    assert out["questions"][0]["id"] == "since" and out["questions"][0]["default"] == "today"


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
