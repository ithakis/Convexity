"""symbol_db.py — the symbol pack format, its download, and the lookup.

The fixture is a synthetic pack of well-known public listings (CLAUDE.md §18);
`install()` is shared with the search and resolver tests."""

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
        "Software - Infrastructure",
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
        "Software - Infrastructure",
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
        "Software - Application",
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
        "Drug Manufacturers - General",
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
        "Drug Manufacturers - General",
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
        "Drug Manufacturers - General",
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
        "Insurance - Diversified",
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
        "Insurance - Diversified",
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
        "Banks - Regional",
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
        "Banks - Regional",
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
        "REIT - Hotel & Motel",
        3e9,
        10,
        1,
    ],
    ["XLK", "Technology Select Sector SPDR Fund", "etf", "PCX", "us", None, None, 7e10, 11, 1],
    ["^GSPC", "S&P 500", "index", "SNP", "us", None, None, None, 12, 1],
    ["HSBA.L", "HSBC Holdings plc", "stock", "LSE", "gb", _FIN, "Banks - Diversified", 2e11, 13, 1],
    ["BNP.PA", "BNP Paribas SA", "stock", "PAR", "fr", _FIN, "Banks - Regional", 8e10, 14, 1],
    ["JPM", "JPMorgan Chase & Co.", "stock", "NYQ", "us", _FIN, "Banks - Diversified", 7e11, 15, 1],
    ["NVDA", "NVIDIA Corporation", "stock", "NMS", "us", _TECH, "Semiconductors", 4.4e12, 16, 1],
    [
        "LLY",
        "Eli Lilly and Company",
        "stock",
        "NYQ",
        "us",
        _HC,
        "Drug Manufacturers - General",
        7e11,
        17,
        1,
    ],
]


def pack(rows=None, date="2026-10-01"):
    """(manifest bytes, data bytes) for these rows."""
    blob = rp.gzip_json({"schema": sdb.SCHEMA_VERSION, "columns": list(C), "rows": rows or ROWS})
    m = {
        "schema_version": sdb.SCHEMA_VERSION,
        "date": date,
        "rows": len(rows or ROWS),
        "files": {sdb.DATA: {"sha256": rp.sha256(blob), "bytes": len(blob)}},
    }
    return json.dumps(m).encode(), blob


def install(rows=None):
    raw, _blob = pack(rows)
    sdb.write_db(sdb.validate_rows({"schema": 1, "columns": list(C), "rows": rows or ROWS}), raw)
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
        (lambda r: r.__setitem__(7, float("nan")), "finite"),
        (lambda r: r.__setitem__(9, 2), "home"),
        (lambda r: r.append("extra"), "fields"),
    ],
)
def test_validator_rejects_bad_rows(mutate, match):
    rows = [list(r) for r in ROWS]
    mutate(rows[0])
    with pytest.raises(rp.PackError, match=match):
        sdb.validate_rows({"schema": 1, "columns": list(C), "rows": rows})


def test_validator_rejects_repeated_tickers_and_extra_columns():
    with pytest.raises(rp.PackError, match="repeated"):
        sdb.validate_rows({"schema": 1, "columns": list(C), "rows": [ROWS[0], ROWS[0]]})
    with pytest.raises(rp.PackError, match="columns"):
        sdb.validate_rows({"schema": 1, "columns": [*C, "summary"], "rows": ROWS})


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
        industries=["Banks - Regional", "Banks - Diversified"], regions=["de", "fr", "gb"]
    )
    assert [h.ticker for h in banks] == ["HSBA.L", "BNP.PA", "DBK.DE"]
    assert "Semiconductors" in sdb.facets()["industry"]


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
