"""`convexity build-symbols` — build the symbol pack (run weekly by CI).

Sweeps Yahoo's screener for everything it lists and writes
`symbols-manifest.json` + `symbols.json.gz` (format: symbol_db.py) to `--out`:

1. Equities, all regions, paged 250 at a time (~236k listings).
2. The same universe once per industry (~145), which is how each listing
   gets its industry and sector: screener quotes carry neither.
3. ETFs (~57k) and mutual funds (~338k) from their own screeners.
4. The indices in data/indices.json (the screener has none).

A listing's region comes from its exchange code (yfinance's own region ->
exchange maps), never from the quote's `region` field, which only echoes the
request. Market caps arrive in the listing's currency and are converted to
USD here, once (CLAUDE.md §0, one unit at ingestion). ETFs, funds and indices
carry no size: the screener does not return one.

Listings of one company share a group: same cleaned name and same reporting
currency. Its home listing is, in order: on a primary venue (not a German
regional exchange, OTC or an international board); quoted in the reporting
currency, unless that currency is USD — many foreign companies report in USD
(Shell, Zurich, Genmab), so there it says nothing about home; then the
highest traded value (3-month average volume x price, in USD). So NOVO-B.CO
over NVO, 2330.TW over TSM, LLY over LLY.DE, ZURN.SW over ZURVY.

A degraded run is never published: a sweep that returns fewer than 95% of
the rows Yahoo says exist, or a total more than 20% below `--previous`,
exits non-zero with nothing written. No key is needed — Yahoo only.
"""

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from convexity import reference_pack as rp, symbol_db as sdb
from convexity.helpers import major_ccy, price_in_major

_PAGE = 250
_PAUSE_S = 0.25
_ATTEMPTS = 4
_MIN_SHARE = 0.95
_MAX_DROP = 0.20
_TEST = re.compile(r"\btest\b", re.IGNORECASE)
# Venues that mostly re-list companies whose home is elsewhere: German
# regional exchanges, Cboe/LSE international order books, OTC, and the Latin
# American foreign-share boards (NVDACL.SN).
SECONDARY = {
    "FRA",
    "STU",
    "BER",
    "MUN",
    "HAM",
    "DUS",
    "IOB",
    "CXE",
    "NEO",
    "PNK",
    "OQB",
    "OQX",
    "MEX",
    "SGO",
}


def _screen(query, offset: int, size: int) -> dict:
    import yfinance as yf

    for attempt in range(_ATTEMPTS):
        try:
            time.sleep(_PAUSE_S)
            return yf.screen(query, offset=offset, size=size)
        except Exception as e:
            if attempt == _ATTEMPTS - 1:
                raise SystemExit(f"Yahoo screener failed at offset {offset}: {e}") from None
            time.sleep(5 * (attempt + 1))
    raise AssertionError("unreachable")


def sweep(query, label: str, limit: int | None = None) -> list[dict]:
    """Every quote a query matches, in pages. Fails on a short sweep."""
    out: list[dict] = []
    total = None
    while True:
        r = _screen(query, len(out), _PAGE if limit is None else min(_PAGE, limit - len(out)))
        total = r.get("total") or 0
        page = r.get("quotes") or []
        out += page
        if not page or len(out) >= total or (limit is not None and len(out) >= limit):
            break
    want = total if limit is None else min(total, limit)
    if len(out) < _MIN_SHARE * want:
        raise SystemExit(f"{label}: got {len(out)} of {want} rows from Yahoo")
    print(f"  {label}: {len(out)} rows", flush=True)
    return out


def _maps():
    from yfinance import const

    regions = sorted(const.EQUITY_SCREENER_EQ_MAP["region"])
    exch_region = {
        x: r
        for m in (
            const.EQUITY_SCREENER_EQ_MAP,
            const.ETF_SCREENER_EQ_MAP,
            const.FUND_SCREENER_EQ_MAP,
        )
        for r, xs in m["exchange"].items()
        for x in xs
    }
    sector_of = {i: s for s, inds in const.EQUITY_SCREENER_EQ_MAP["industry"].items() for i in inds}
    fund_x = sorted({x for xs in const.FUND_SCREENER_EQ_MAP["exchange"].values() for x in xs})
    etf_x = sorted({x for xs in const.ETF_SCREENER_EQ_MAP["exchange"].values() for x in xs})
    return regions, exch_region, sector_of, fund_x, etf_x


def _name(q: dict) -> str | None:
    n = " ".join(str(q.get("longName") or q.get("shortName") or "").split())[:160]
    return n if n and n.isprintable() else None


def _usd(amount, ccy) -> float | None:
    """A major-unit amount (market cap) in USD, or None."""
    from convexity import fx

    if not isinstance(amount, (int, float)) or amount <= 0:
        return None
    rate = fx.usd_per_unit(ccy)
    return round(float(amount) * rate, -3) if rate else None


def _traded_usd(q: dict) -> float:
    """3-month average daily traded value in USD; 0 when unknown. Prices of
    pence/cent listings are in the minor unit, so they go through
    price_in_major first."""
    vol = q.get("averageDailyVolume3Month")
    px = price_in_major(q.get("regularMarketPrice"), q.get("currency"))
    if not isinstance(vol, (int, float)) or not px:
        return 0.0
    return _usd(vol * px, q.get("currency")) or 0.0


def collect(limit: int | None = None) -> list[dict]:
    """Every listing as {ticker, name, type, exchange, region, sector,
    industry, mcap_usd, ccy, fin_ccy}; funds and ETFs after equities."""
    from yfinance import EquityQuery as Q
    from yfinance.screener.query import ETFQuery, FundQuery

    regions, exch_region, sector_of, fund_x, etf_x = _maps()
    every = Q("is-in", ["region", *regions])
    industry: dict[str, str] = {}
    for ind in sorted(sector_of):
        q = Q("and", [every, Q("eq", ["industry", ind])])
        for s in sweep(q, ind, limit):
            industry.setdefault(s.get("symbol"), ind)
    rows: dict[str, dict] = {}
    parts = [
        ("stock", every, "equities"),
        ("etf", ETFQuery("is-in", ["exchange", *etf_x]), "ETFs"),
        ("fund", FundQuery("is-in", ["exchange", *fund_x]), "mutual funds"),
    ]
    for typ, q, label in parts:
        for s in sweep(q, label, limit):
            tk, name = str(s.get("symbol") or "").upper(), _name(s)
            # ^XYZ-IV lines are ETF indicative values, not tradable listings;
            # Nasdaq lists test funds ("Test Mutual Fund - PARA").
            if not name or tk in rows or tk.startswith("^") or not sdb._TICKER.match(tk):
                continue
            if typ != "stock" and _TEST.search(name):
                continue
            ind = industry.get(s.get("symbol"))
            rows[tk] = {
                "ticker": tk,
                "name": name,
                "type": typ,
                "exchange": s.get("exchange"),
                "region": exch_region.get(s.get("exchange")),
                "sector": sector_of.get(ind),
                "industry": ind,
                "mcap_usd": _usd(s.get("marketCap"), s.get("currency")) if typ == "stock" else None,
                "ccy": s.get("currency"),
                "fin_ccy": s.get("financialCurrency"),
                "traded_usd": _traded_usd(s) if typ == "stock" else 0.0,
            }
    data = json.loads(resources.files("convexity").joinpath("data/indices.json").read_text("utf-8"))
    for tk, name, region in data["rows"]:
        rows.setdefault(tk, {"ticker": tk, "name": name, "type": "index", "region": region})
    return list(rows.values())


def _home_score(r: dict) -> tuple:
    fin = major_ccy(r["fin_ccy"]) if r.get("fin_ccy") else None
    local = fin not in (None, "USD") and major_ccy(r.get("ccy") or "") == fin
    return (r.get("exchange") not in SECONDARY, local, r.get("traded_usd") or 0.0, r["ticker"])


def group(listings: list[dict]) -> list[list]:
    """Pack rows: listings grouped into companies, one home listing each.
    Only stocks group; every ETF, fund and index is its own home."""
    companies: dict[tuple, list[dict]] = {}
    for r in listings:
        key = (
            (sdb.name_key(r["name"]), r.get("fin_ccy"))
            if r["type"] == "stock"
            else ("", r["ticker"])
        )
        companies.setdefault(key, []).append(r)
    rows = []
    for gid, members in enumerate(companies.values()):
        home = max(members, key=_home_score)
        for r in sorted(members, key=lambda m: m["ticker"]):
            rows.append(
                [
                    r["ticker"],
                    r["name"],
                    r["type"],
                    *(r.get(k) for k in ("exchange", "region", "sector", "industry", "mcap_usd")),
                    gid,
                    int(r is home),
                ]
            )
    return rows


def write(out: Path, rows: list[list]) -> dict:
    """Validate, then write the data file and its manifest (manifest last)."""
    sdb.validate_rows({"schema": sdb.SCHEMA_VERSION, "columns": list(sdb.COLUMNS), "rows": rows})
    blob = rp.gzip_json({"schema": sdb.SCHEMA_VERSION, "columns": list(sdb.COLUMNS), "rows": rows})
    manifest = {
        "schema_version": sdb.SCHEMA_VERSION,
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "rows": len(rows),
        "files": {sdb.DATA: {"sha256": rp.sha256(blob), "bytes": len(blob)}},
    }
    sdb.validate_manifest(manifest)
    out.mkdir(parents=True, exist_ok=True)
    (out / sdb.DATA).write_bytes(blob)
    (out / sdb.MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="convexity build-symbols",
        description="Build the symbol pack (every Yahoo listing) into a directory. Run by CI.",
    )
    ap.add_argument("--out", required=True, help="Output directory.")
    ap.add_argument("--limit", type=int, default=None, help="At most N rows per sweep (testing).")
    ap.add_argument("--previous", default=None, help="Directory with the previous manifest.")
    args = ap.parse_args(argv)
    t0 = time.time()
    try:
        rows = group(collect(args.limit))
        if args.previous and args.limit is None:
            prev = json.loads((Path(args.previous) / sdb.MANIFEST).read_text("utf-8"))
            if len(rows) < (1 - _MAX_DROP) * int(prev["rows"]):
                raise SystemExit(f"{len(rows)} rows, down from {prev['rows']}: not published")
        m = write(Path(args.out), rows)
    except SystemExit as e:
        if isinstance(e.code, str):
            sys.stderr.write(f"error: {e.code}\n")
            return 1
        raise
    except (OSError, ValueError, KeyError) as e:
        sys.stderr.write(f"error: {type(e).__name__}: {e}\n")
        return 1
    homes = sum(r[-1] for r in rows)
    kb = m["files"][sdb.DATA]["bytes"] // 1024
    print(f"✓ {len(rows)} listings, {homes} home listings, {kb} KB, {time.time() - t0:.0f}s")
    return 0
