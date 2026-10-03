"""symbol_build.py — the weekly symbol pack builder, with Yahoo stubbed."""

import json

import pytest

from convexity import cli, symbol_build as sb, symbol_db as sdb


def L(ticker, name, exchange, ccy, fin, traded=0.0, typ="stock"):
    return {
        "ticker": ticker,
        "name": name,
        "type": typ,
        "exchange": exchange,
        "region": None,
        "mcap_usd": None,
        "ccy": ccy,
        "fin_ccy": fin,
        "traded_usd": traded,
    }


LISTINGS = [
    # Traded values are illustrative; the ADRs are the busier line where that
    # is the hard case (TSM) — the reporting currency must still win there.
    L("NOVO-B.CO", "Novo Nordisk A/S", "CPH", "DKK", "DKK", 4e8),
    L("NVO", "Novo Nordisk A/S", "NYQ", "USD", "DKK", 5e8),
    L("NOV.DE", "Novo Nordisk A/S", "GER", "EUR", "DKK", 1e7),
    L("2330.TW", "Taiwan Semiconductor Manufacturing Company Limited", "TAI", "TWD", "TWD", 1e9),
    L("TSM", "Taiwan Semiconductor Manufacturing Company Limited", "NYQ", "USD", "TWD", 3e9),
    L("LLY", "Eli Lilly and Company", "NYQ", "USD", "USD", 2e9),
    L("LLY.F", "Eli Lilly and Company", "FRA", "EUR", "USD", 1e6),
    L("LLY.DE", "Eli Lilly and Company", "GER", "EUR", "USD", 5e6),
    L("SAP.DE", "SAP SE", "GER", "EUR", "EUR", 3e8),
    L("SAP.F", "SAP SE", "FRA", "EUR", "EUR", 1e6),
    # Reports in USD, listed at home in CHF: USD reporting says nothing.
    L("ZURN.SW", "Zurich Insurance Group AG", "EBS", "CHF", "USD", 2e8),
    L("ZURVY", "Zurich Insurance Group AG", "OQX", "USD", "USD", 3e6),
    L("XLK", "Technology Select Sector SPDR Fund", "PCX", "USD", None, None, typ="etf"),
]


def _homes(rows):
    return {r[0] for r in rows if r[-1] == 1}


@pytest.mark.parametrize("ticker", ["NOVO-B.CO", "2330.TW", "LLY", "SAP.DE", "ZURN.SW", "XLK"])
def test_home_listing_gold(ticker):
    assert ticker in _homes(sb.group(LISTINGS))


def test_one_home_per_company_and_alternates_share_a_group():
    rows = sb.group(LISTINGS)
    groups = {}
    for r in rows:
        groups.setdefault(r[-2], []).append(r)
    assert all(sum(r[-1] for r in g) == 1 for g in groups.values())
    novo = next(g for g in groups.values() if any(r[0] == "NVO" for r in g))
    assert {r[0] for r in novo} == {"NOVO-B.CO", "NVO", "NOV.DE"}


def test_sweep_pages_until_total_and_fails_short(monkeypatch):
    data = [{"symbol": f"T{i}"} for i in range(600)]
    monkeypatch.setattr(
        sb, "_screen", lambda q, off, size: {"total": 600, "quotes": data[off : off + size]}
    )
    assert len(sb.sweep(None, "x")) == 600
    assert len(sb.sweep(None, "x", limit=300)) == 300
    monkeypatch.setattr(
        sb, "_screen", lambda q, off, size: {"total": 600, "quotes": data[:250] if off == 0 else []}
    )
    with pytest.raises(SystemExit, match="250 of 600"):
        sb.sweep(None, "x")


def test_cli_builds_a_pack_the_app_can_install(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sb, "collect", lambda limit=None: LISTINGS)
    out = tmp_path / "pack"
    assert cli.main(["build-symbols", "--out", str(out)]) == 0
    raw = (out / sdb.MANIFEST).read_bytes()
    m = sdb.validate_manifest(json.loads(raw))
    rows = sdb.validate_rows(sdb.rp.gunzip_json((out / sdb.DATA).read_bytes()))
    assert m["rows"] == len(rows) == len(LISTINGS)
    sdb.write_db(rows, raw)
    sdb._invalidate()
    assert sdb.lookup("novo nordisk")[0].ticker == "NOVO-B.CO"
    assert "13 listings" in capsys.readouterr().out


def test_a_big_drop_against_the_previous_pack_is_not_published(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sb, "collect", lambda limit=None: LISTINGS)
    prev = tmp_path / "prev"
    prev.mkdir()
    (prev / sdb.MANIFEST).write_text(json.dumps({"rows": 100}))
    out = tmp_path / "pack"
    assert cli.main(["build-symbols", "--out", str(out), "--previous", str(prev)]) == 1
    assert "down from 100" in capsys.readouterr().err
    assert not out.exists()
