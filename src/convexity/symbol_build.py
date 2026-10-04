"""`convexity build-symbols` — build the symbol pack (run weekly by CI).

Sweeps Yahoo's screener for everything it lists and writes
`symbols-manifest.json` + `symbols.ndjson.gz` (format: symbol_db.py) to `--out`:

1. Equities, all regions (~233k listings), 250 per page and split into price
   bands under Yahoo's 10,000-results-per-query cap (`sweep`).
2. The same universe once per industry (~145), which is how each listing
   gets its industry and sector: screener quotes carry neither.
3. ETFs (~57k) and mutual funds (~338k) from their own screeners.
4. The indices in data/indices.json (the screener has none).

A listing's region comes from its exchange code (yfinance's own region ->
exchange maps), never from the quote's `region` field, which only echoes the
request. Market caps arrive in the listing's currency and are converted to
USD here, once (CLAUDE.md §0, one unit at ingestion). ETFs, funds and indices
carry no size: the screener does not return one.

Listings of one company share a group: the same cleaned name (see group()).
Its home listing is, in order: on a primary venue (not a German regional
exchange, OTC, an international board or a depositary-receipt line); quoted
in the reporting currency, unless that currency is USD — many foreign
companies report in USD (Shell, Zurich, Genmab), so there it says nothing
about home — and only if liquid; then the highest traded value (3-month
average volume x price, in USD). So NOVO-B.CO over NVO, 2330.TW over TSM,
LLY over LLY.DE, ZURN.SW over ZURVY, 9988.HK/BABA over 89988.HK.

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
_CAP = 10_000  # results Yahoo serves per query (last page at offset 9,750)
_PAUSE_S = 0.25
_ATTEMPTS = 4
_MIN_SHARE = 0.95
_MAX_DROP = 0.20
_TEST = re.compile(r"\btest\b", re.IGNORECASE)


def _screen(query, offset: int = 0, size: int = 1, asc: bool = True) -> dict:
    import yfinance as yf

    for attempt in range(_ATTEMPTS):
        try:
            time.sleep(_PAUSE_S)
            return yf.screen(query, offset=offset, size=size, sortField="ticker", sortAsc=asc)
        except Exception as e:
            if attempt == _ATTEMPTS - 1:
                raise SystemExit(f"Yahoo screener failed at offset {offset}: {e}") from None
            time.sleep(5 * (attempt + 1))
    raise AssertionError("unreachable")


def _pages(query, n: int, seen: dict, asc: bool = True) -> None:
    """Up to the first `_CAP` results of one query, by ticker."""
    for off in range(0, min(n, _CAP), _PAGE):
        page = _screen(query, off, _PAGE, asc).get("quotes") or []
        for q in page:
            seen.setdefault(q.get("symbol"), q)
        if not page:  # NOT len(page) < _PAGE: Yahoo returns 246-249 rows mid-stream
            break


def sweep(query, label: str, limit: int | None = None) -> list[dict]:
    """Every listing a query matches.

    Yahoo serves only the first 10,000 results of a query: past offset 9,750
    every page repeats the last one (a 233k-row sweep came back as 22k unique
    listings). So a query over `_CAP` is split into price bands — every
    listing has an intraday price — decade by decade, each halved until it
    fits. A band that cannot be split further (a thousand funds at exactly
    $1.00) is read ascending and descending by ticker. Fails when the unique
    listings fall short of the total Yahoo reports."""
    Q = type(query)  # EquityQuery / ETFQuery / FundQuery: bands use the same kind
    total = _screen(query).get("total") or 0
    seen: dict = {}
    if limit is not None or total <= _CAP:
        _pages(query, total if limit is None else min(total, limit), seen)
    else:
        bands = [(0.0, 0.01)] + [(10.0**e, 10.0 ** (e + 1)) for e in range(-2, 12)]
        while bands:
            lo, hi = bands.pop()
            band = Q(
                "and", [query, Q("gte", ["intradayprice", lo]), Q("lt", ["intradayprice", hi])]
            )
            n = _screen(band).get("total") or 0
            if n > _CAP and hi - lo > 1e-4:
                bands += [(lo, (lo + hi) / 2), ((lo + hi) / 2, hi)]
            elif n:
                _pages(band, n, seen)
                if n > _CAP:
                    _pages(band, n, seen, asc=False)
    want = total if limit is None else min(total, limit)
    if len(seen) < _MIN_SHARE * want:
        raise SystemExit(f"{label}: got {len(seen)} of {want} listings from Yahoo")
    print(f"  {label}: {len(seen)} listings", flush=True)
    return list(seen.values())[:want]


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
    vol = q.get("averageDailyVolume3Month") or q.get("regularMarketVolume")
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
                "traded_usd": _traded_usd(s),
            }
    data = json.loads(resources.files("convexity").joinpath("data/indices.json").read_text("utf-8"))
    for tk, name, region in data["rows"]:
        rows.setdefault(tk, {"ticker": tk, "name": name, "type": "index", "region": region})
    return list(rows.values())


_US = {"NYQ", "NMS", "NGM", "NCM", "PCX", "ASE", "BTS"}


def _secondary(r: dict, us: set[str]) -> bool:
    """A re-listing: by venue, by ticker pattern, or an Argentine CEDEAR (a
    .BA line whose base ticker trades on a US exchange: SPY.BA, AAPL.BA)."""
    tk = r["ticker"]
    cedear = tk.endswith(".BA") and tk[:-3] in us
    return cedear or sdb.relisting(tk, r.get("exchange"))


def _record(r: dict) -> bool:
    return r["type"] == "index" or (r["type"] == "fund" and r["ticker"].startswith("0P"))


def _home_score(r: dict, top: float) -> tuple:
    """Among primary listings: quoted in the reporting currency (not USD, and
    only if liquid: Alibaba's thin RMB counter 89988.HK is "local" too), then
    the most traded."""
    fin = major_ccy(r["fin_ccy"]) if r.get("fin_ccy") else None
    traded = r.get("traded_usd") or 0.0
    local = (
        fin not in (None, "USD") and major_ccy(r.get("ccy") or "") == fin and traded >= 0.1 * top
    )
    return (local, traded, r["ticker"])


def group(listings: list[dict]) -> list[list]:
    """Pack rows: listings grouped into companies, one home listing each.

    A company is every listing with the same cleaned name, whatever Yahoo
    calls its type (it files Canadian depositary receipts like NOVO.TO as
    ETFs): NOVO-B.CO, NVO, NOV.DE and NOVO.TO; SPY and SPY.BA. Its sector,
    industry and size fill in from whichever listing has them. Dropped:
    - stock/ETF groups with no primary listing at all (re-listings under a
      variant name: MOH.SG for LVMH, 0KZC.L for SPY),
    - stock groups with no size and no industry anywhere: in the data those
      are warrants, CBBCs and re-listings with nothing to screen on (~5.5k
      Hong Kong rows, ~1.4k Vienna re-listings) that only drown real names.
    Funds and indices are kept as they are."""
    us = {r["ticker"] for r in listings if r.get("exchange") in _US}
    companies: dict[str, list[dict]] = {}
    for r in listings:
        companies.setdefault(
            r["ticker"] if r["type"] == "index" else sdb.name_key(r["name"]), []
        ).append(r)
    rows, gid = [], 0
    for members in companies.values():
        fill = {k: next((m[k] for m in members if m.get(k)), None) for k in _FILL}
        # Venue rules are for exchange lines. Morningstar fund records (0P…)
        # and indices have no venue; a UCITS ETF on Stuttgart that Yahoo files
        # as a fund (SPY5.SG) is a re-listing like any other.
        primary = [m for m in members if _record(m) or not _secondary(m, us)]
        if not primary:
            continue
        if {m["type"] for m in members} == {"stock"} and not (fill["mcap_usd"] or fill["industry"]):
            continue
        top = max((m.get("traded_usd") or 0.0) for m in primary)
        home = max(primary, key=lambda m: _home_score(m, top))
        for r in sorted(members, key=lambda m: m["ticker"]):
            v = {k: r.get(k) or fill[k] for k in _FILL}
            rows.append([r["ticker"], r["name"], r["type"], r.get("exchange"), r.get("region"),
                         v["sector"], v["industry"], v["mcap_usd"], r.get("traded_usd") or None,
                         gid, int(r is home)])  # fmt: skip
        gid += 1
    return rows


_FILL = ("sector", "industry", "mcap_usd")


def write(out: Path, rows: list[list]) -> dict:
    """Encode, read back exactly as the app will (allow-list, unique
    tickers), then write the data file and its manifest (manifest last)."""
    blob = sdb.encode(rows)
    tickers = [r[0] for r in sdb.iter_rows(blob)]
    if len(set(tickers)) != len(rows):
        raise SystemExit("the pack would repeat a ticker or lose a row")
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
    ap.add_argument(
        "--raw",
        default=None,
        help="Development: cache the swept listings in this JSON file (read it if it exists), "
        "to work on grouping without sweeping Yahoo again.",
    )
    args = ap.parse_args(argv)
    t0 = time.time()
    try:
        raw = Path(args.raw) if args.raw else None
        if raw and raw.exists():
            listings = json.loads(raw.read_text("utf-8"))
        else:
            listings = collect(args.limit)
            if raw:
                raw.write_text(json.dumps(listings), encoding="utf-8")
        rows = group(listings)
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
