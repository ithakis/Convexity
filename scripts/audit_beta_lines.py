"""Live audit of the line and index each listing's beta is measured on
(capm.beta_line, capm.home_proxy, capm.learn_country_line).

Four checks, all against the installed symbol pack and Yahoo:

0. Golden set: hand-checked listings and the line their beta must use.
1. Coverage: every primary stock exchange in the pack, by suffix, with the
   index it maps to; unmapped suffixes (the S&P 500 fallback) ranked by the
   market cap they hold.
2. Indices: every home index answers on Yahoo with 3+ years of closes.
3. Choice: for the largest companies with liquid lines in two or more
   countries, each country's most traded line is scored by the weekly R² of
   its returns on its own home index (3 years). The line the rule picks
   should explain its market about as well as the best candidate; the
   pack's old choice is scored the same way, and each loss is printed.

Needs a temp CONVEXITY_HOME with the symbol pack installed (CLAUDE.md §3).
Re-run after any change to the home-line rules, the HOME map or the pack:

    CONVEXITY_HOME=<temp home with pack> uv run python scripts/audit_beta_lines.py [--top 300]
"""

import argparse
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pandas as pd
import yfinance as yf

from convexity import capm
from convexity import symbol_db as sdb

TIE = 0.05  # an R² this close to the best candidate's counts as as good
MAX_CAP = 1e13  # no company is worth $10tn: a bigger size is a currency left unconverted


# listing -> the line its beta must be measured on (hand-checked 2026-10-28):
# secondary listings, receipts, companies reporting in another currency than
# their market's, A/H shares, and companies listed only abroad.
GOLDEN = {
    "WMT.DE": "WMT", "NVD.DE": "NVDA", "TSM": "2330.TW", "ASML": "ASML.AS",
    "SAP": "SAP.DE", "SHEL": "SHEL.L", "SHEL.L": "SHEL.L", "SHELL.AS": "SHEL.L",
    "BP": "BP.L", "UL": "ULVR.L", "UNA.AS": "ULVR.L", "INFY": "INFY.NS",
    "INFY.NS": "INFY.NS", "HDB": "HDFCBANK.NS", "WIT": "WIPRO.NS", "BABA": "9988.HK",
    "2318.HK": "2318.HK", "601318.SS": "601318.SS", "NSRGY": "NESN.SW", "TM": "7203.T",
    "SONY": "6758.T", "NVO": "NOVO-B.CO", "AZN": "AZN.L", "GSK": "GSK.L", "DEO": "DGE.L",
    "BCS": "BARC.L", "HSBC": "HSBA.L", "0005.HK": "HSBA.L", "RIO": "RIO.L", "BHP": "BHP.AX",
    "TTE": "TTE.PA", "SAN": "SAN.MC", "ING": "INGA.AS", "RY": "RY.TO", "TD": "TD.TO",
    "CNI": "CNR.TO", "STLA": "STLAM.MI", "RACE": "RACE.MI", "FER": "FER.MC",
    "SPOT": "SPOT", "ARM": "ARM", "MELI": "MELI", "AAPL": "AAPL", "MC.PA": "MC.PA",
}  # fmt: skip


def golden() -> int:
    from convexity import capm as c

    with ThreadPoolExecutor(6) as ex:
        list(ex.map(c.learn_country_line, GOLDEN))
    bad = [(s, c.beta_line(s), want) for s, want in GOLDEN.items() if c.beta_line(s) != want]
    print(f"0. Golden set: {len(GOLDEN) - len(bad)}/{len(GOLDEN)} on the expected line")
    for s, got, want in bad:
        print(f"   FAIL {s}: {got}, expected {want}")
    return len(bad)


def closes(tickers: list[str]) -> pd.DataFrame:
    out = []
    for i in range(0, len(tickers), 150):
        part = tickers[i : i + 150]
        df = yf.download(part, period="5y", auto_adjust=True, progress=False, threads=True)
        c = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]]
        if not isinstance(df.columns, pd.MultiIndex):
            c.columns = part
        out.append(c)
    return pd.concat(out, axis=1)


def coverage() -> None:
    con = sdb._connect()
    if con is None:
        sys.exit("No symbol pack in CONVEXITY_HOME.")
    with closing(con):
        rows = con.execute(
            # Home lines only: a depositary line inherits its company's size
            # (NVDA03.BK carries NVIDIA's), so summing every line counts it twice.
            "SELECT ticker, exchange, COALESCE(mcap_usd, 0) FROM symbols"
            " WHERE type = 'stock' AND home = 1"
        ).fetchall()
    glitch = [tk for tk, _ex, cap in rows if cap > MAX_CAP]
    rows = [r for r in rows if r[2] <= MAX_CAP]
    by = defaultdict(lambda: [0, 0.0])
    for tk, ex, cap in rows:
        if sdb.relisting(tk, ex):
            continue
        suf = tk.rsplit(".", 1)[1] if "." in tk else ""
        by[suf][0] += 1
        by[suf][1] += cap
    print("\n1. Coverage: companies' home listings by suffix")
    if glitch:
        print(
            f"   skipped {len(glitch)} with a size over $10tn (a unit error): {', '.join(glitch[:5])}"
        )
    miss = []
    for suf, (n, cap) in sorted(by.items(), key=lambda kv: -kv[1][1]):
        p = capm.home_proxy(f"X.{suf}" if suf else "X")
        if p["kind"] == "fallback":
            miss.append((suf, n, cap))
    total = sum(c for _n, c in by.values())
    lost = sum(c for _s, _n, c in miss)
    print(f"   {len(by)} suffixes; {len(miss)} fall back to the S&P 500, holding "
          f"{lost / total:.2%} of home-listed market cap:")  # fmt: skip
    for suf, n, cap in sorted(miss, key=lambda t: -t[2])[:20]:
        print(f"   .{suf:5} {n:6} listings  ${cap / 1e9:9,.0f}bn")


def indices() -> None:
    proxies = sorted({p for p, _n, _c in capm.HOME.values()} | {capm.US[0], capm.REGIONAL_EU[0]})
    px = closes(proxies)
    print("\n2. Indices: years of closes on Yahoo")
    bad = 0
    for p in proxies:
        s = px[p].dropna() if p in px.columns else pd.Series(dtype=float)
        yrs = (s.index[-1] - s.index[0]).days / 365.25 if len(s) else 0.0
        if yrs < 3:
            bad += 1
        print(f"   {'OK ' if yrs >= 3 else 'BAD'} {p:12} {yrs:4.1f}y")
    print(f"   {len(proxies) - bad}/{len(proxies)} indices have 3+ years")


def r2(stock: pd.Series, market: pd.Series) -> float | None:
    return capm.weekly_r2(stock, market, 3)


def choice(top: int) -> None:
    con = sdb._connect()
    with closing(con):
        groups = con.execute(
            "SELECT grp, MAX(COALESCE(mcap_usd, 0)) m FROM symbols WHERE type = 'stock'"
            " GROUP BY grp ORDER BY m DESC LIMIT ?",
            (top * 6,),
        ).fetchall()
    cases = []
    for g, _m in groups:
        lines = [h for h in sdb.company_lines(g) if h.type == "stock" and h.region]
        if not lines:
            continue
        best = max(h.adv_usd or 0.0 for h in lines)
        liquid = [h for h in lines if (h.adv_usd or 0.0) >= 0.05 * best]
        per_region = {}
        for h in liquid:  # most traded first
            per_region.setdefault(h.region, h)
        if len(per_region) >= 2:
            cases.append((g, list(per_region.values())))
        if len(cases) >= top:
            break
    print(f"\n3. Choice: {len(cases)} companies with liquid lines in 2+ countries")
    home = {g: sdb.home(g) for g, _c in cases}
    with ThreadPoolExecutor(8) as ex:
        country = dict(
            zip(
                [g for g, _c in cases],
                ex.map(
                    lambda g: capm._country(home[g].ticker) if home[g] else None,
                    [g for g, _c in cases],
                ),  # fmt: skip
                strict=True,
            )
        )
    lines = sorted({h.ticker for _g, c in cases for h in c})
    idx = sorted({capm.home_proxy(t)["proxy"] for t in lines})
    px = closes(lines + idx)

    def score(t):
        p = capm.home_proxy(t)["proxy"]
        if t not in px.columns or p not in px.columns:
            return None
        return r2(px[t].dropna(), px[p].dropna())

    scores = {t: score(t) for t in lines}
    tally = defaultdict(lambda: [0, 0])  # rule -> [as good as best, scored]
    losses = defaultdict(list)
    for g, cands in cases:
        sc = {h.ticker: scores.get(h.ticker) for h in cands}
        sc = {t: v for t, v in sc.items() if v is not None}
        if len(sc) < 2:
            continue
        top_r2 = max(sc.values())
        top_line = max(sc, key=sc.get)
        regions = capm.COUNTRY_REGIONS.get(country.get(g) or "", ())
        lead = max(h.adv_usd or 0.0 for h in cands)
        picks = {"pack": home[g].ticker if home[g] else None}
        for share in (0.05, 0.10, 0.25):
            there = [h for h in cands if h.region in regions and (h.adv_usd or 0) >= share * lead]
            picks[f"country@{share:.2f}"] = there[0].ticker if there else picks["pack"]
        for rule, t in picks.items():
            if t not in sc:
                continue
            tally[rule][1] += 1
            if sc[t] >= top_r2 - TIE:
                tally[rule][0] += 1
            else:
                losses[rule].append(
                    f"{t} {sc[t]:.2f} vs {top_line} {top_r2:.2f} ({country.get(g) or '?'})"
                )
    for rule, (ok, n) in tally.items():
        print(f"   {rule:14} as good as the best line (R² within {TIE}): {ok}/{n} = {ok / n:.0%}")
    for rule in ("pack", f"country@{capm.COUNTRY_SHARE:.2f}"):
        print(f"   {rule} losses:")
        for line in losses[rule][:25]:
            print(f"     {line}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=300)
    a = ap.parse_args()
    failed = golden()
    coverage()
    indices()
    choice(a.top)
    sys.exit(1 if failed else 0)
