"""symbol_db.py — the symbol pack format, its download, and the lookup.

The fixture is a synthetic pack of well-known public listings (CLAUDE.md §18);
`install()` is shared with the search and resolver tests."""

import gzip
import json

import pytest

from convexity import reference_pack as rp, symbol_db as sdb
from tests.test_reference_pack import _Stub

C = sdb.COLUMNS
_FIN, _TECH, _HC = "Financial Services", "Technology", "Healthcare"
ROWS = [
    # ticker, name, type, exchange, region, sector, industry, mcap_usd, group, home
    [
        "MSFT",
        "Microsoft Corporation",
        "stock",
        "NMS",
        "us",
        _TECH,
        "Software—Infrastructure",
        3.8e12,
        1,
        1,
    ],
    [
        "MSF.DE",
        "Microsoft Corporation",
        "stock",
        "GER",
        "de",
        _TECH,
        "Software—Infrastructure",
        3.8e12,
        1,
        0,
    ],
    [
        "SMSI",
        "Smith Micro Software, Inc.",
        "stock",
        "NCM",
        "us",
        _TECH,
        "Software—Application",
        2e7,
        2,
        1,
    ],
    [
        "NOVO-B.CO",
        "Novo Nordisk A/S",
        "stock",
        "CPH",
        "dk",
        _HC,
        "Drug Manufacturers—General",
        3e11,
        3,
        1,
    ],
    [
        "NVO",
        "Novo Nordisk A/S",
        "stock",
        "NYQ",
        "us",
        _HC,
        "Drug Manufacturers—General",
        3e11,
        3,
        0,
    ],
    [
        "NOV.DE",
        "Novo Nordisk A/S",
        "stock",
        "GER",
        "de",
        _HC,
        "Drug Manufacturers—General",
        3e11,
        3,
        0,
    ],
    [
        "NSIS-B.CO",
        "Novonesis A/S",
        "stock",
        "CPH",
        "dk",
        "Basic Materials",
        "Specialty Chemicals",
        3e10,
        4,
        1,
    ],
    [
        "BRK-B",
        "Berkshire Hathaway Inc.",
        "stock",
        "NYQ",
        "us",
        _FIN,
        "Insurance—Diversified",
        1e12,
        5,
        1,
    ],
    [
        "BRK-A",
        "Berkshire Hathaway Inc.",
        "stock",
        "NYQ",
        "us",
        _FIN,
        "Insurance—Diversified",
        1e12,
        5,
        0,
    ],
    [
        "GOOGL",
        "Alphabet Inc.",
        "stock",
        "NMS",
        "us",
        "Communication Services",
        "Internet Content & Information",
        2e12,
        6,
        1,
    ],
    [
        "DBK.DE",
        "Deutsche Bank Aktiengesellschaft",
        "stock",
        "GER",
        "de",
        _FIN,
        "Banks—Regional",
        5e10,
        7,
        1,
    ],
    [
        "DB",
        "Deutsche Bank Aktiengesellschaft",
        "stock",
        "NYQ",
        "us",
        _FIN,
        "Banks—Regional",
        5e10,
        7,
        0,
    ],
    [
        "2330.TW",
        "Taiwan Semiconductor Manufacturing Company Limited",
        "stock",
        "TAI",
        "tw",
        _TECH,
        "Semiconductors",
        1.2e12,
        8,
        1,
    ],
    [
        "TSM",
        "Taiwan Semiconductor Manufacturing Company Limited",
        "stock",
        "NYQ",
        "us",
        _TECH,
        "Semiconductors",
        1.2e12,
        8,
        0,
    ],
    ["AAPL", "Apple Inc.", "stock", "NMS", "us", _TECH, "Consumer Electronics", 3.5e12, 9, 1],
    [
        "APLE",
        "Apple Hospitality REIT, Inc.",
        "stock",
        "NYQ",
        "us",
        "Real Estate",
        "REIT—Hotel & Motel",
        3e9,
        10,
        1,
    ],
    ["XLK", "Technology Select Sector SPDR Fund", "etf", "PCX", "us", None, None, 7e10, 11, 1],
    ["^GSPC", "S&P 500", "index", "SNP", "us", None, None, None, 12, 1],
    ["HSBA.L", "HSBC Holdings plc", "stock", "LSE", "gb", _FIN, "Banks—Diversified", 2e11, 13, 1],
    ["BNP.PA", "BNP Paribas SA", "stock", "PAR", "fr", _FIN, "Banks—Regional", 8e10, 14, 1],
    ["JPM", "JPMorgan Chase & Co.", "stock", "NYQ", "us", _FIN, "Banks—Diversified", 7e11, 15, 1],
    ["NVDA", "NVIDIA Corporation", "stock", "NMS", "us", _TECH, "Semiconductors", 4.4e12, 16, 1],
    [
        "LLY",
        "Eli Lilly and Company",
        "stock",
        "NYQ",
        "us",
        _HC,
        "Drug Manufacturers—General",
        7e11,
        17,
        1,
    ],
]
# adv_usd (average daily traded value) sits after mcap_usd; none of these need one.
ROWS = [r[:8] + [None] + r[8:] for r in ROWS]


def pack(rows=None, date="2026-10-01"):
    """(manifest bytes, data bytes) for these rows."""
    blob = sdb.encode(rows or ROWS)
    m = {
        "schema_version": sdb.SCHEMA_VERSION,
        "date": date,
        "rows": len(rows or ROWS),
        "files": {sdb.DATA: {"sha256": rp.sha256(blob), "bytes": len(blob)}},
    }
    return json.dumps(m).encode(), blob


def install(rows=None):
    raw, blob = pack(rows)
    sdb.write_db(sdb.iter_rows(blob), raw, expect=len(rows or ROWS))
    sdb._invalidate()


@pytest.fixture()
def db():
    install()
    yield
    sdb._invalidate()


# ------------------------------------------------------------------ format
@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda r: r.__setitem__(0, "bad ticker"), "ticker"),
        (lambda r: r.__setitem__(1, "x\nimport os"), "printable"),
        (lambda r: r.__setitem__(1, "x" * 161), "printable"),
        (lambda r: r.__setitem__(2, "crypto"), "type"),
        (lambda r: r.__setitem__(7, "1e9"), "finite"),
        (lambda r: r.__setitem__(10, 2), "home"),
        (lambda r: r.append("extra"), "fields"),
    ],
)
def test_validator_rejects_bad_rows(mutate, match):
    rows = [list(r) for r in ROWS]
    mutate(rows[0])
    with pytest.raises(rp.PackError, match=match):
        list(sdb.iter_rows(sdb.encode(rows)))


def test_repeated_tickers_extra_columns_and_bad_counts_are_refused():
    raw, _ = pack()
    with pytest.raises(rp.PackError, match="twice"):
        sdb.write_db(sdb.iter_rows(sdb.encode([ROWS[0], ROWS[0]])), raw)
    with pytest.raises(rp.PackError, match="manifest says"):
        sdb.write_db(sdb.iter_rows(sdb.encode(ROWS)), raw, expect=len(ROWS) + 1)
    assert not sdb.db_path().exists()  # nothing half-written was swapped in
    bad = gzip.compress(json.dumps({"schema": 1, "columns": [*C, "summary"]}).encode() + b"\n")
    with pytest.raises(rp.PackError, match="columns"):
        list(sdb.iter_rows(bad))


def test_the_stream_reader_refuses_bombs_long_lines_and_truncation(monkeypatch):
    blob = sdb.encode(ROWS)
    with pytest.raises(rp.PackError, match="truncated"):
        list(sdb.iter_rows(blob[:-12]))
    with pytest.raises(rp.PackError, match="longer than"):
        list(sdb.iter_rows(gzip.compress(b"x" * 10_000)))
    monkeypatch.setattr(rp, "MAX_JSON_BYTES", 1000)
    with pytest.raises(rp.PackError, match="cap"):
        list(sdb.iter_rows(blob))


def test_an_old_format_file_reads_as_empty(tmp_path):
    import sqlite3

    sdb.db_path().parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(sdb.db_path()) as con:  # the pre-2.0 NASDAQ/SEC table
        con.execute("CREATE TABLE symbols (provider TEXT, ticker TEXT, name TEXT)")
        con.execute("INSERT INTO symbols VALUES ('yfinance', 'MSFT', 'Microsoft')")
    assert sdb.lookup("MSFT") == []
    install()
    assert sdb.lookup("MSFT")[0].ticker == "MSFT"


# ------------------------------------------------------------------ lookup
@pytest.mark.parametrize(
    "query, ticker",
    [
        ("microsft", "MSFT"),
        ("microsoft", "MSFT"),
        ("novo nordsk", "NOVO-B.CO"),
        ("berkshire", "BRK-B"),
        ("google", "GOOGL"),
        ("deutsche bank", "DBK.DE"),
        ("tsmc", "2330.TW"),
        ("apple", "AAPL"),
        ("NVO", "NVO"),  # an exact ticker wins, even a secondary listing
        ("jp morgan", "JPM"),
    ],
)
def test_gold_queries_rank_first(db, query, ticker):
    assert sdb.lookup(query)[0].ticker == ticker


def test_names_show_home_listings_only(db):
    tickers = [h.ticker for h in sdb.lookup("novo nordisk")]
    assert "NOVO-B.CO" in tickers and "NVO" not in tickers and "NOV.DE" not in tickers


def test_alternates_and_category(db):
    alts = [a["ticker"] for a in sdb.alternates(3, exclude="NOVO-B.CO")]
    assert sorted(alts) == ["NOV.DE", "NVO"]
    banks = sdb.category(
        industries=["Banks—Regional", "Banks—Diversified"], regions=["de", "fr", "gb"]
    )
    assert [h.ticker for h in banks] == ["HSBA.L", "BNP.PA", "DBK.DE"]


def test_no_file_means_no_hits():
    assert sdb.lookup("microsoft") == [] and sdb.category(sectors=["Technology"]) == []


# ------------------------------------------------------------------ download
@pytest.fixture()
def stub(monkeypatch):
    s = _Stub()
    monkeypatch.setenv("CONVEXITY_REFERENCE_PACK", "1")
    monkeypatch.setenv("CONVEXITY_REFERENCE_URL", s.url)
    monkeypatch.setattr(rp, "_BACKOFF_S", 0.01)
    sdb.reset_for_tests()
    yield s
    sdb.reset_for_tests()
    s.httpd.shutdown()
    s.httpd.server_close()


def _fetch(force=False):
    sdb.start(force=force)
    return sdb.wait(10)


def test_download_installs_then_rechecks_without_redownloading(stub):
    raw, blob = pack()
    stub.files = {sdb.MANIFEST: raw, sdb.DATA: blob}
    assert _fetch()["state"] == "installed"
    assert sdb.lookup("microsoft")[0].ticker == "MSFT"
    assert sdb.status()["installed"] == {"date": "2026-10-01", "rows": len(ROWS)}
    assert _fetch()["state"] == "up_to_date"  # fresh: no thread at all
    stub.hits.clear()
    assert _fetch(force=True)["state"] == "up_to_date"
    assert stub.hits == [sdb.MANIFEST]


def test_a_tampered_file_is_rejected_and_the_old_copy_stays(stub):
    install()
    raw, blob = pack(ROWS[:3], date="2026-10-08")
    bad = bytearray(blob)
    bad[len(bad) // 2] ^= 0xFF
    stub.files = {sdb.MANIFEST: raw, sdb.DATA: bytes(bad)}
    st = _fetch(force=True)
    assert st["state"] == "failed" and "checksum" in st["error"]
    assert sdb.lookup("apple")[0].ticker == "AAPL"


def test_switched_off_means_no_download(stub, monkeypatch):
    monkeypatch.setenv("CONVEXITY_REFERENCE_PACK", "0")
    assert _fetch()["state"] == "disabled" and stub.hits == []


def test_market_read_names_read_through_one_ticker_at_a_time(db):
    from convexity import relevance

    relevance.load_company_names.cache_clear()
    names = relevance.load_company_names()
    assert names.get("msft") == "Microsoft Corporation"
    assert names.get("NOPE") is None and names.get("NOPE", "x") == "x"
    install(ROWS[:1])  # a new pack clears the cache: the next read sees it
    assert relevance.load_company_names().get("AAPL") is None


def test_this_python_has_sqlite_fts5_with_the_trigram_tokenizer():
    """Name search depends on it (SQLite >= 3.34 with FTS5). CI runs this on
    macOS, Linux and Windows."""
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram', content='', detail='none')"
    )
    con.close()
