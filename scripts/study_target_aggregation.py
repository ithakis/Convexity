"""Exploratory: how should per-firm 12M analyst targets be aggregated, given
their publication dates? Point-in-time backtest on large-cap US names.

For each month-end D (2021-01 .. 2025-09) and stock, rebuild the consensus
from the firm targets KNOWN at D (each firm's latest action <= D, <= 365 days
old, split-adjusted onto today's share basis like the closes), under several
methods, and compare the implied 12M log return ln(T/P_D) with the realized
ln(P_{D+12M}/P_D) (price return, split-adjusted, no dividends: targets are
price targets).

Metrics per method: mean bias (forecast - realized), MAE, and the monthly
cross-sectional Spearman IC (does the method RANK stocks' future returns),
averaged with a Newey-West t (lag 11: 12M windows overlap month to month).

Caveats: survivorship (today's large caps), one bull-heavy window, Yahoo's
per-firm feed is incomplete; it compares methods on equal footing only.

Usage (network: ~80 tickers' upgrades_downgrades + 6 years of closes):
    uv run python scripts/study_target_aggregation.py /tmp/study
writes /tmp/study.csv (one row per stock-month) and /tmp/study_methods.csv.
Result of 2026-10-22 (80 names, 57 months, 4,534 obs): the equal-weight mean
(the app's) IC +0.023 (t 0.5); return-preserving IC +0.085 (t 1.6), with
recency weights +0.080 (t 1.9); rolling a target forward by its age or
annualising its remaining horizon made things worse.
"""

import json
import math
import sys
import time
import warnings

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

UNIVERSE = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "AVGO",
    "TSLA",
    "JPM",
    "V",
    "MA",
    "UNH",
    "XOM",
    "LLY",
    "JNJ",
    "PG",
    "HD",
    "COST",
    "ABBV",
    "MRK",
    "PEP",
    "KO",
    "ADBE",
    "CRM",
    "NFLX",
    "AMD",
    "INTC",
    "CSCO",
    "ORCL",
    "QCOM",
    "TXN",
    "AMAT",
    "MU",
    "NOW",
    "INTU",
    "IBM",
    "CAT",
    "DE",
    "GE",
    "HON",
    "BA",
    "LMT",
    "RTX",
    "GS",
    "MS",
    "BAC",
    "WFC",
    "C",
    "SCHW",
    "BLK",
    "PFE",
    "TMO",
    "DHR",
    "ABT",
    "MDT",
    "ISRG",
    "AMGN",
    "GILD",
    "VRTX",
    "REGN",
    "CVX",
    "COP",
    "SLB",
    "NKE",
    "SBUX",
    "MCD",
    "LOW",
    "TGT",
    "WMT",
    "DIS",
    "CMCSA",
    "T",
    "VZ",
    "PYPL",
    "UBER",
    "PANW",
    "CRWD",
    "SNOW",
    "SHOP",
    "PLTR",
]

OUT = sys.argv[1]
G = 0.08  # CAPM-ish drift used to roll a target forward by its age


def load():
    px = yf.download(UNIVERSE, start="2019-06-01", auto_adjust=False, progress=False)["Close"]
    uds, spl = {}, {}
    for s in UNIVERSE:
        for k in range(3):
            try:
                tk = yf.Ticker(s)
                ud = tk.upgrades_downgrades
                sp = tk.splits
                break
            except Exception:
                time.sleep(1 + k)
        else:
            continue
        if ud is None or ud.empty:
            continue
        d = ud.reset_index()
        d["GradeDate"] = pd.to_datetime(d["GradeDate"]).dt.tz_localize(None)
        uds[s] = d.sort_values("GradeDate")
        spl[s] = [
            (pd.Timestamp(i).tz_localize(None), float(r)) for i, r in sp.items() if r and r != 1
        ]
        time.sleep(0.15)
    return px, uds, spl


def factor(splits, when):
    f = 1.0
    for d, r in splits:
        if d > when:
            f *= r
    return f


def firms_at(d: pd.DataFrame, splits, D, px_series):
    """[(target, age_days, px_at_issue)] known at D, on today's share basis."""
    h = d[d["GradeDate"] <= D]
    out = []
    for _, g in h.groupby("Firm"):
        last = g.iloc[-1]
        use = last
        t = last["currentPriceTarget"]
        if not (t and t == t and t > 0):
            unchanged = (not last.get("FromGrade")) or last.get("FromGrade") == last.get("ToGrade")
            if last.get("Action") not in ("main", "reit") or not unchanged:
                continue
            v = g[g["currentPriceTarget"].fillna(0) > 0]
            if v.empty:
                continue
            use = v.iloc[-1]
        when = use["GradeDate"]
        age = (D - when).days
        if age > 365:
            continue
        tgt = float(use["currentPriceTarget"]) / factor(splits, when)
        p_iss = px_series.asof(when)
        if not (p_iss == p_iss and p_iss > 0):
            continue
        out.append((tgt, age, float(p_iss)))
    return out


def methods(fs, P):
    t = np.array([f[0] for f in fs])
    a = np.array([f[1] for f in fs], dtype=float)
    pi = np.array([f[2] for f in fs])
    keep = (t > P / 4) & (t < P * 4)
    t, a, pi = t[keep], a[keep], pi[keep]
    if len(t) < 3:
        return None
    w90 = 0.5 ** (a / 90)
    ret_pres = P * t / pi  # the analyst's implied return at issue, applied to today's price
    rolled = t * np.exp(G * a / 365)  # target date d+365 -> D+365 at the CAPM-ish drift
    # remaining-horizon: the target's return still to come over 365-age days, annualised to 12M
    rem = np.maximum(365 - a, 30) / 365
    horizon = P * (t / P) ** (1 / rem)
    horizon = np.clip(horizon, P / 4, P * 4)
    fresh = a <= 90
    m = {
        "mean_eq (current)": t.mean(),
        "median": np.median(t),
        "trimmed_mean_10": float(
            pd.Series(t)
            .sort_values()
            .iloc[int(len(t) * 0.1) : len(t) - int(len(t) * 0.1) or None]
            .mean()
        ),
        "recency_hl90": (w90 * t).sum() / w90.sum(),
        "only_<=90d": t[fresh].mean() if fresh.sum() >= 3 else t.mean(),
        "only_<=182d": t[a <= 182].mean() if (a <= 182).sum() >= 3 else t.mean(),
        "rolled_capm": rolled.mean(),
        "return_preserving": ret_pres.mean(),
        "ret_pres_recency_hl90": (w90 * ret_pres).sum() / w90.sum(),
        "remaining_horizon": horizon.mean(),
        "geo_mean": float(np.exp(np.log(t).mean())),
    }
    return m, len(t), float(np.median(a))


def nw_t(x, lag=11):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 5:
        return float("nan")
    e = x - x.mean()
    v = e @ e / n
    for k in range(1, lag + 1):
        v += 2 * (1 - k / (lag + 1)) * (e[k:] @ e[:-k]) / n
    return x.mean() / math.sqrt(v / n)


def main():
    px, uds, spl = load()
    px = px.dropna(how="all")
    dates = pd.date_range("2021-01-31", "2025-09-30", freq="ME")
    recs = []
    for D in dates:
        Dt = px.index[px.index <= D][-1]
        D1 = Dt + pd.DateOffset(years=1)
        if D1 > px.index[-1]:
            continue
        D1t = px.index[px.index <= D1][-1]
        for s, d in uds.items():
            ser = px[s].dropna()
            P, P1 = ser.asof(Dt), ser.asof(D1t)
            if not (P == P and P1 == P1 and P > 0):
                continue
            r = methods(firms_at(d, spl.get(s, []), Dt, ser), P)
            if not r:
                continue
            m, n, age_med = r
            rec = {"D": Dt, "sym": s, "n": n, "age_med": age_med, "real": math.log(P1 / P)}
            for k, v in m.items():
                rec[k] = math.log(v / P)
            recs.append(rec)
    df = pd.DataFrame(recs)
    keys = [c for c in df.columns if c not in ("D", "sym", "n", "age_med", "real")]
    rows = []
    for k in keys:
        err = df[k] - df["real"]
        ics = df.groupby("D").apply(lambda g, k=k: g[k].corr(g["real"], method="spearman"))
        hit = (np.sign(df[k]) == np.sign(df["real"])).mean()
        rows.append(
            {
                "method": k,
                "bias": err.mean(),
                "mae": err.abs().mean(),
                "rmse": math.sqrt((err**2).mean()),
                "ic_mean": ics.mean(),
                "ic_t_nw": nw_t(ics.values),
                "ic_pos_share": (ics > 0).mean(),
                "sign_hit": hit,
                "mean_forecast": df[k].mean(),
            }
        )
    res = pd.DataFrame(rows).sort_values("ic_mean", ascending=False)
    summ = {
        "n_obs": len(df),
        "n_stocks": df["sym"].nunique(),
        "n_months": df["D"].nunique(),
        "median_firms": float(df["n"].median()),
        "median_age_days": float(df["age_med"].median()),
        "mean_realized": float(df["real"].mean()),
        "share_obs_median_age_gt_182": float((df["age_med"] > 182).mean()),
    }
    # error by staleness: does the current method err more when the median target is old?
    df["age_bucket"] = pd.cut(df["age_med"], [0, 30, 60, 90, 182, 365])
    by_age = df.groupby("age_bucket", observed=True).apply(
        lambda g: pd.Series(
            {
                "n": len(g),
                "bias_current": (g["mean_eq (current)"] - g["real"]).mean(),
                "bias_ret_pres": (g["return_preserving"] - g["real"]).mean(),
                "mae_current": (g["mean_eq (current)"] - g["real"]).abs().mean(),
                "mae_ret_pres": (g["return_preserving"] - g["real"]).abs().mean(),
            }
        )
    )
    pd.set_option("display.width", 200)
    print(json.dumps(summ, indent=1))
    print(res.to_string(index=False, float_format=lambda v: f"{v:+.4f}"))
    print(by_age.to_string(float_format=lambda v: f"{v:+.4f}"))
    df.to_csv(OUT + ".csv", index=False)
    res.to_csv(OUT + "_methods.csv", index=False)


main()
