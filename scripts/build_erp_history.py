"""Build ``src/convexity/data/erp_history.json``: each market's historical
equity premium over bills, and the out-of-sample evidence for and against
using it, from the Jordà-Schularick-Taylor Macrohistory Database.

    uv run python scripts/build_erp_history.py path/to/JSTdatasetR6.xlsx

The dataset is not in the repo (CC BY-NC-SA 4.0; download it from
https://www.macrohistory.net/database/). Only the derived premia are written,
with the citation and licence inside the file.

**The premium is the one the app's mu needs.** The model price
S0 * exp(mu T) is the MEAN price, so for the market itself (beta = 1)
exp(rf + ERP) = E[(1 + R_eq)] and

    ERP = ln E[(1 + R_eq) / (1 + R_bill)]

on annual total returns. The geometric premium, mean(ln(1 + R_eq) - ln(1 +
R_bill)), is reported alongside: it is what most tables quote and is lower by
about half the variance (US 4.6% geometric vs 6.1% here).

**The evidence (2026-10-26, JST R6, 16 markets with equity and bill series):**

* Out of sample, each market's own expanding-window history forecasts its next
  10 and 20 years WORSE than a flat 5%: MSE +14% and +33%, a country-block
  bootstrap puts it worse with probability 0.96-0.97. Its cross-country rank
  correlation with the future premium is negative (-0.12, -0.16): markets with
  a high past premium went on to a lower one.
* The cross-market differences are noise: Cochran's Q = 8.6 on 15 df
  (p = 0.90; 6.4, p = 0.97 on 1950-2020 alone), so one common premium fits
  every market; the implied true dispersion is 0.
* A real-time pooled world history ties with a flat 5% (MSE -2% at 10y, -5% at
  20y, P(better) 0.70-0.75, not significant).
So the assumed premium stays the default and the historical one is the option.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "src/convexity/data/erp_history.json"
EUROPE = ("Belgium", "Denmark", "Finland", "France", "Germany", "Italy", "Netherlands", "Norway",
          "Portugal", "Spain", "Sweden", "Switzerland", "UK")  # fmt: skip
CITATION = (
    "Jordà, Ò., Knoll, K., Kuvshinov, D., Schularick, M. and Taylor, A. M. (2019), "
    "'The Rate of Return on Everything, 1870-2015', Quarterly Journal of Economics 134(3), "
    "1225-1298. Data: Jordà-Schularick-Taylor Macrohistory Database, release 6, "
    "https://www.macrohistory.net/database/"
)


def load(path: str) -> pd.DataFrame:
    df = pd.read_excel(path)
    d = df[["country", "year", "eq_tr", "bill_rate"]].dropna().copy()
    # Germany 1922: +2069% in the hyperinflation against a 6% bill, a
    # different monetary regime rather than a premium.
    d = d[(d.bill_rate <= 0.5) & (d.eq_tr.abs() <= 3)]
    d["x"] = (1 + d.eq_tr) / (1 + d.bill_rate)
    return d


def summarise(x: pd.Series, years: pd.Series) -> dict:
    n = len(x)
    return {
        "erp": round(float(np.log(x.mean())), 5),
        "geometric": round(float(np.log(x).mean()), 5),
        "se": round(float(x.std() / np.sqrt(n) / x.mean()), 5),
        "from": int(years.min()),
        "to": int(years.max()),
        "years": n,
    }


def pooled(d: pd.DataFrame, countries=None) -> dict:
    g = d if countries is None else d[d.country.isin(countries)]
    yr = g[g.year >= 1900].groupby("year").x.mean()  # equal weight per year
    return summarise(yr, pd.Series(yr.index))


def evidence(d: pd.DataFrame) -> dict:
    out = {}
    rng = np.random.default_rng(0)
    for H in (10, 20):
        rows = []
        for t in range(1950, 2021 - H):
            past = d[d.year <= t]
            glob = np.log(past[past.year >= 1900].groupby("year").x.mean().mean())
            for c, g in d.groupby("country"):
                fut, gp = g[(g.year > t) & (g.year <= t + H)], g[g.year <= t]
                if len(fut) < 0.8 * H or len(gp) < 30:
                    continue
                rows.append((t, c, np.log(fut.x.mean()), np.log(gp.x.mean()), glob))
        r = pd.DataFrame(rows, columns=["t", "c", "real", "local", "world"])
        r["assumed"] = 0.05
        mse = {k: float(((r.real - r[k]) ** 2).mean()) for k in ("local", "world", "assumed")}
        cs = r.c.unique()
        p_better = {}
        for k in ("local", "world"):
            wins = 0
            for _ in range(1000):
                rr = pd.concat([r[r.c == c] for c in rng.choice(cs, len(cs))])
                wins += ((rr.real - rr[k]) ** 2).mean() < ((rr.real - rr.assumed) ** 2).mean()
            p_better[k] = round(wins / 1000, 2)
        rank = r.groupby("t")[["real", "local"]].apply(
            lambda g: g.real.corr(g.local, method="spearman")
        )
        out[f"{H}y"] = {
            "cases": len(r),
            "origins": f"{r.t.min()}-{r.t.max()}",
            "mse_vs_assumed": {
                k: round(mse[k] / mse["assumed"] - 1, 3) for k in ("local", "world")
            },
            "p_better_than_assumed": p_better,
            "rank_corr_local_future": round(float(rank.mean()), 3),
        }
    g = d.groupby("country")
    erp = np.log(g.x.mean())
    se = g.x.std() / np.sqrt(g.x.count()) / g.x.mean()
    w = 1 / se**2
    q = float((w * (erp - (w * erp).sum() / w.sum()) ** 2).sum())
    from scipy.stats import chi2

    out["homogeneity"] = {"cochran_q": round(q, 2), "df": len(erp) - 1,
                          "p": round(float(1 - chi2.cdf(q, len(erp) - 1)), 3)}  # fmt: skip
    return out


def main(path: str) -> None:
    d = load(path)
    markets = {c: summarise(g.x, g.year) for c, g in d.groupby("country")}
    doc = {
        "source": "Jordà-Schularick-Taylor Macrohistory Database R6",
        "citation": CITATION,
        "license": "CC BY-NC-SA 4.0 (https://creativecommons.org/licenses/by-nc-sa/4.0/); "
        "derived from the dataset above, shared under the same terms",
        "consulted": dt.date.today().isoformat(),
        "definition": "ERP = ln E[(1+R_eq)/(1+R_bill)], annual total returns in local currency, "
        "each market's full sample; se = sd/sqrt(n)/mean. geometric = mean ln((1+R_eq)/(1+R_bill)).",
        "markets": markets,
        "pools": {"world": pooled(d), "europe": pooled(d, EUROPE)},
        "evidence": evidence(d),
    }
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: doc[k] for k in ("pools", "evidence")}, indent=1))
    print(f"wrote {OUT} ({len(markets)} markets)")


if __name__ == "__main__":
    main(sys.argv[1])
