"""symbol_build.py — the weekly symbol pack builder, with Yahoo stubbed."""

import json

import pytest

from convexity import cli, symbol_build as sb, symbol_db as sdb


def L(ticker, name, exchange, ccy, fin, traded=0.0, typ="stock", industry="Industry"):
    return {"ticker": ticker, "name": name, "type": typ, "exchange": exchange, "region": None,
            "mcap_usd": None, "ccy": ccy, "fin_ccy": fin, "traded_usd": traded,
            "industry": industry}  # fmt: skip


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
    # No reporting currency on a regional German line: joins Baxter.
    L("BAX", "Baxter International Inc.", "NYQ", "USD", "USD", 4e8),
    L("BTL.SG", "Baxter International Inc", "STU", "EUR", None, 1e5),
    L(
        "XLK",
        "Technology Select Sector SPDR Fund",
        "PCX",
        "USD",
        None,
        5e9,
        typ="etf",
        industry=None,
    ),
    L(
        "XLK.MX",
        "Technology Select Sector SPDR Fund",
        "MEX",
        "MXN",
        None,
        1e6,
        typ="etf",
        industry=None,
    ),
    # A thin RMB counter is "local" (CNY = CNY) but not liquid: not home.
    L("9988.HK", "Alibaba Group Holding Limited", "HKG", "HKD", "CNY", 1.2e9),
    L("89988.HK", "Alibaba Group Holding Limited", "HKG", "CNY", "CNY", 9e5),
    L("BABA", "Alibaba Group Holding Limited", "NYQ", "USD", "CNY", 1.1e9),
    # Yahoo files Canadian depositary receipts as ETFs: still Novo Nordisk.
    L("NOVO.TO", "Novo Nordisk A/S", "TOR", "CAD", "DKK", 2e5, typ="etf"),
    # An LSE international-order-book line with no primary listing: dropped.
    L("0KZC.L", "SPDR S&P 500 ETF Trust", "LSE", "USD", "USD", 5e7, typ="etf", industry=None),
    # A CEDEAR still named as before a rename: SPY trades in the US -> dropped.
    L(
        "SPY",
        "State Street SPDR S&P 500 ETF Trust",
        "PCX",
        "USD",
        "USD",
        3e10,
        typ="etf",
        industry=None,
    ),
    L("SPY.BA", "SPDR S&P 500 ETF Trust", "BUE", "ARS", "USD", 1e6, typ="etf", industry=None),
    # A warrant-like line: no size and no industry anywhere -> dropped.
    L("12345.HK", "HSBC Call Warrant 2027", "HKG", "HKD", None, 0.0, industry=None),
]


def _homes(rows):
    return {r[0] for r in rows if r[-1] == 1}


@pytest.mark.parametrize(
    "ticker", ["NOVO-B.CO", "2330.TW", "LLY", "SAP.DE", "ZURN.SW", "XLK", "9988.HK"]
)
def test_home_listing_gold(ticker):
    assert ticker in _homes(sb.group(LISTINGS))


def test_etf_listings_group_and_bare_warrants_are_dropped():
    rows = sb.group(LISTINGS)
    xlk = {r[0]: r[-1] for r in rows if r[1].startswith("Technology Select")}
    assert xlk == {"XLK": 1, "XLK.MX": 0}
    assert not {"12345.HK", "0KZC.L", "SPY.BA"} & {r[0] for r in rows}


def test_one_home_per_company_and_alternates_share_a_group():
    rows = sb.group(LISTINGS)
    groups = {}
    for r in rows:
        groups.setdefault(r[-2], []).append(r)
    assert all(sum(r[-1] for r in g) == 1 for g in groups.values())
    novo = next(g for g in groups.values() if any(r[0] == "NVO" for r in g))
    assert {r[0] for r in novo} == {"NOVO-B.CO", "NVO", "NOV.DE", "NOVO.TO"}
    bax = next(g for g in groups.values() if any(r[0] == "BTL.SG" for r in g))
    assert {r[0]: r[-1] for r in bax} == {"BAX": 1, "BTL.SG": 0}


class _FakeYahoo:
    """The screener as it behaves: filters on intradayprice bands, sorts by
    ticker, and serves at most 10,000 results — deeper offsets repeat the
    last page (the cap that once cut a 233k sweep to 22k)."""

    def __init__(self, prices):
        self.rows = [{"symbol": f"T{i:06d}", "p": p} for i, p in enumerate(prices)]
        self.calls = 0

    def __call__(self, query, offset=0, size=1, asc=True):
        self.calls += 1
        lo, hi = 0.0, float("inf")
        for op in query.to_dict().get("operands", []):
            if isinstance(op, dict) and op["operands"][0] == "intradayprice":
                lo = op["operands"][1] if op["operator"] == "GTE" else lo
                hi = op["operands"][1] if op["operator"] == "LT" else hi
        rows = sorted(
            (r for r in self.rows if lo <= r["p"] < hi), key=lambda r: r["symbol"], reverse=not asc
        )
        offset = min(offset, 9_750)
        page = rows[offset : min(offset + size, 10_000)]
        # Real pages are often a few rows short mid-stream (246 of 250).
        return {"total": len(rows), "quotes": page[:-1] if len(page) == size and size > 1 else page}


def test_sweep_splits_past_yahoos_10k_cap(monkeypatch):
    from yfinance import EquityQuery as Q

    prices = [1.0 + (i % 997) * 0.37 for i in range(25_000)] + [
        1.0
    ] * 12_000  # 12k at $1.00: read both ways
    fake = _FakeYahoo(prices)
    monkeypatch.setattr(sb, "_screen", fake)
    got = sb.sweep(Q("eq", ["region", "us"]), "x")
    # every page drops one row, as Yahoo's do: the sweep still gets >99%
    assert len(got) == len({r["symbol"] for r in got}) > 0.99 * len(prices)


def test_sweep_limit_and_short_sweeps(monkeypatch):
    from yfinance import EquityQuery as Q

    monkeypatch.setattr(sb, "_screen", _FakeYahoo([5.0] * 600))
    assert len(sb.sweep(Q("eq", ["region", "us"]), "x", limit=300)) == 300
    short = _FakeYahoo([5.0] * 600)
    monkeypatch.setattr(
        sb,
        "_screen",
        lambda q, offset=0, size=1, asc=True: {**short(q, offset, size, asc), "total": 900},
    )
    with pytest.raises(SystemExit, match="of 900 listings"):
        sb.sweep(Q("eq", ["region", "us"]), "x")


def test_cli_builds_a_pack_the_app_can_install(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sb, "collect", lambda limit=None: LISTINGS)
    out = tmp_path / "pack"
    assert cli.main(["build-symbols", "--out", str(out)]) == 0
    raw = (out / sdb.MANIFEST).read_bytes()
    m = sdb.validate_manifest(json.loads(raw))
    blob = (out / sdb.DATA).read_bytes()
    assert (
        m["rows"] == len(list(sdb.iter_rows(blob))) == len(LISTINGS) - 3
    )  # warrant, 0KZC.L, SPY.BA
    sdb.write_db(sdb.iter_rows(blob), raw, expect=m["rows"])
    sdb._invalidate()
    assert sdb.lookup("novo nordisk")[0].ticker == "NOVO-B.CO"
    assert f"{len(LISTINGS) - 3} listings" in capsys.readouterr().out


def test_a_big_drop_against_the_previous_pack_is_not_published(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sb, "collect", lambda limit=None: LISTINGS)
    prev = tmp_path / "prev"
    prev.mkdir()
    (prev / sdb.MANIFEST).write_text(json.dumps({"rows": 100}))
    out = tmp_path / "pack"
    assert cli.main(["build-symbols", "--out", str(out), "--previous", str(prev)]) == 1
    assert "down from 100" in capsys.readouterr().err
    assert not out.exists()
