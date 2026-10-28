"""The app's one expected-return model: one beta, one mu, one model price.

Before 1.29 the app carried several "expected returns" and two betas: the
stock window centred its σ/μ curve on CAPM with Yahoo's ``info["beta"]`` (5Y
monthly, USD returns vs the S&P 500, unshrunk) and the US bill, blended one
curve per analyst firm on top behind a trust control, and the grid, the export,
search and the news model each read Yahoo's beta or the detail's own beta vs
SPY. Now every surface reads this module:

    mu = rf_local + beta * ERP + alpha
    model price at T = S0 * exp(mu * T)

* ``rf_local`` is the short rate in the listing's own currency
  (``options_density._local_rate``: ^IRX for USD, CME futures for the majors).
  A home-index beta implies a local CAPM, so the rate is local too.
* ``beta`` is estimated here (below), cached per symbol per day.
* ``ERP`` is the owner's ``dist_erp`` (5% by default), or with
  ``model_erp_mode`` "historical" the long-run premium of the market the beta
  is measured against (``erp_for``, below).
* ``alpha`` is the analysts' small relative tilt,
  ``analyst_targets.relative_alpha`` (IC x 0.42 x z of relative upside, z
  capped at +/-3): at most about +/-2.5% a year. It is 0 with no targets, or
  with mu off (pure CAPM). A target's raw upside is never a return forecast
  (IC ~0.01-0.03 point in time, backend.md "What analyst targets predict").

**The beta (backtest, 2026-10-23: 164 names, US/Europe/UK/Japan/EU banks,
yearly origins 2008-2025, scored against the realised next-3M and next-12M
beta; ``scripts/backtest_beta.py`` on the research branch):**

1. Proxy: the listing's **home country index in local currency** (``HOME``).
   It beat the S&P 500 for every non-US group: EU banks R² .52 vs .29, and
   Japan's daily beta vs the S&P is 0.25 against 1.02 vs TOPIX (Tokyo closes
   before New York opens). Unmapped European listings take the STOXX 600,
   anything else the S&P 500 with the stock's returns in USD.
2. Estimator: Welch's (2022) slope-winsorised, age-weighted beta (BSWA) on
   the last 3 years of daily returns on joint trading days: the stock's return
   clipped to [-2 r_m, 4 r_m], weights exp(-2 age / 252) (half-life ~87
   trading days), a weighted OLS slope. With 151-251 joint days, the plain
   1-year daily OLS; with fewer, the prior beta = 1 (labelled "prior"). Yahoo's
   beta is no fallback: for European banks it did worse than beta = 1.
3. Shrink: beta = 1 + 0.9 (beta_hat - 1); 0.9 was the optimum within 0.001
   in every group (Blume's 0.67 is +7% MSE for this estimator).
   The weight is scaled down (``w_scale``, 2026-10-27 study of 172 names in
   30 markets, 5,247 quarterly forecasts 2018-2025): to 0 as the share of
   zero-return days reaches 25% (thin trading reads a beta too low), and to
   0.6x for a stock's weekly beta on an index in another session.

Result: MSE vs the realised beta -51% at 12M and -57% at 3M against Yahoo's.
Levi & Welch (2017), Hollstein & Prokopczuk (2016) and Welch (2022) agree
that daily, recent, shrunk betas beat 5-year monthly ones.

**Why a module of its own, not ``analytics.py``:** analytics is the portfolio
engine (period returns, benchmarks, drift); this is a per-asset model that the
stock window, the grid, the export, the optimizer's hover, the news Track
record and the reference pack all read. It owns one disk cache
(``state/betas.json``, market data only) and one pure estimator that CI's
reference-pack builder imports, so it has to load without the analytics
stack.

Network: never on ``fetch_one``'s path. ``cached`` is a disk/memory read;
``ensure`` computes the missing ones in one bulk download (the analytics'
``_bulk_close``, per-symbol cached), and runs from the model-price route, the
density route, the refresh job and the export.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import threading
from functools import lru_cache

import numpy as np

# Listing suffix -> (index, name, currency). The backtested map, plus the same
# rule for the other home markets the app lists (marked "not backtested" in
# the methodology): HK, Australia, Canada, Korea, India, Brazil.
HOME: dict[str, tuple[str, str, str]] = {
    "PA": ("^FCHI", "CAC 40", "EUR"),
    **dict.fromkeys(
        ("DE", "F", "BE", "DU", "HM", "MU", "SG", "HA"), ("^GDAXI", "DAX", "EUR")
    ),  # fmt: skip
    "MC": ("^IBEX", "IBEX 35", "EUR"),
    "MI": ("FTSEMIB.MI", "FTSE MIB", "EUR"),
    "AS": ("^AEX", "AEX", "EUR"),
    "BR": ("^BFX", "BEL 20", "EUR"),
    "HE": ("^OMXH25", "OMX Helsinki 25", "EUR"),
    "VI": ("^ATX", "ATX", "EUR"),
    "SW": ("^SSMI", "SMI", "CHF"),
    "ST": ("^OMX", "OMX Stockholm 30", "SEK"),
    "CO": ("^OMXC25", "OMX Copenhagen 25", "DKK"),
    "L": ("^FTAS", "FTSE All-Share", "GBP"),
    "T": ("1306.T", "TOPIX", "JPY"),
    "HK": ("^HSI", "Hang Seng", "HKD"),
    "AX": ("^AXJO", "ASX 200", "AUD"),
    "TO": ("^GSPTSE", "S&P/TSX", "CAD"),
    "KS": ("^KS11", "KOSPI", "KRW"),
    "NS": ("^NSEI", "Nifty 50", "INR"),
    "BO": ("^NSEI", "Nifty 50", "INR"),
    "SA": ("^BVSP", "Bovespa", "BRL"),
    # Probed live on Yahoo (2026-10-23): a European listing whose market has
    # an index of its own is measured against it, not the STOXX 600 (Equinor
    # read -0.52 vs STOXX over a year oil and equities moved apart; it is
    # ~30% of OSEBX), and an Asian one never against the S&P on daily data.
    "OL": ("OSEBX.OL", "Oslo Børs Benchmark", "NOK"),
    "IR": ("^ISEQ", "ISEQ", "EUR"),
    "LS": ("PSI20.LS", "PSI 20", "EUR"),
    "SI": ("^STI", "Straits Times", "SGD"),
    **dict.fromkeys(("TW", "TWO"), ("^TWII", "TAIEX", "TWD")),
    "NZ": ("^NZ50", "NZX 50", "NZD"),
    "JK": ("^JKSE", "Jakarta Composite", "IDR"),
    "KL": ("^KLSE", "FTSE Bursa Malaysia KLCI", "MYR"),
    "MX": ("^MXX", "S&P/BMV IPC", "MXN"),
    "TA": ("^TA125.TA", "TA-125", "ILS"),
    "JO": ("^J203.JO", "FTSE/JSE All Share", "ZAR"),
    # 2026-10-27: these four sat on the S&P 500 (weekly USD), where a stock
    # explains ~1-5% of its weekly variance against 20-57% on its own index
    # (Moutai R² .01 vs .27, Turkish Airlines .02 vs .54, 16 names). Saudi,
    # Thai, Polish and Chilean indices answer on Yahoo with today's quote only.
    "SS": ("000001.SS", "SSE Composite", "CNY"),
    "SZ": ("399001.SZ", "SZSE Component", "CNY"),
    "IS": ("XU100.IS", "BIST 100", "TRY"),
    "KQ": ("^KQ11", "KOSDAQ", "KRW"),
}
BACKTESTED = {"PA", "DE", "MC", "MI", "AS", "BR", "HE", "VI", "SW", "ST", "CO", "L", "T"}
# European listings with no mapped country index: the region.
EUROPE = {"WA", "PR", "BD", "AT", "IC", "RG", "TL", "VS", "MT"}
REGIONAL_EU = ("^STOXX", "STOXX Europe 600", "EUR")
US = ("^GSPC", "S&P 500", "USD")
# A fund holds a market that is often not its listing's (an S&P 500 tracker
# on London read beta 0.47 against the FTSE, a world fund on Xetra 0.59
# against the DAX): it is measured against whichever of its home chain and
# these explains it best (weekly R², asynchrony-robust), when one explains
# it at all (FUND_MIN_R2; a bond or gold fund stays on its home market).
FUND_TYPES = {"etf", "fund"}
FUND_CANDIDATES = (US, REGIONAL_EU, ("ACWI", "MSCI ACWI", "USD"),
                   ("EEM", "MSCI Emerging Markets", "USD"))  # fmt: skip
FUND_MIN_R2 = 0.3

WINDOW_YEARS = 3  # default; prefs model_beta_years (1, 2 or 3)
SHRINK = 0.9  # default; prefs model_beta_shrink
RHO = 2.0 / 252.0  # age decay per trading day (Welch: half-life ~87 days)
MIN_BSWA = 252  # joint price points for BSWA (the backtest's rule)
MIN_OLS = 151  # ... for the 1-year daily OLS below that; fewer = prior
GLITCH = 1.0  # |log return| above this is a data glitch, not a move
SPIKE = 0.15  # ... and one this big that the next bar undoes (a bad print)
TTL_DAYS = 1  # a beta is recomputed once a day
# Shrink scales (2026-10-27 study, below ``finish``): the effective weight is
# shrink x LIQ x WEEKLY, recorded per estimate as ``w_scale``.
LIQ_Z0 = 0.25  # zero-return share at which a daily beta is all prior
WEEKLY_W = 0.6  # a stock's weekly beta against an index in another session

_LOCK = threading.Lock()
# One computation at a time: the grid, the analytics and a stock window ask
# for the same names at once on a page load, and each used to download and
# compute all of them (three bulk downloads of 27 names, 2026-10-24). A
# waiter re-reads the cache first and computes only what is still missing.
_COMPUTE = threading.Lock()
_MEM: dict[str, dict] = {}


# ------------------------------- proxy map --------------------------------


def home_proxy(symbol: str) -> dict:
    """The market a listing's beta is measured against, by its Yahoo suffix:
    ``{proxy, name, ccy, kind}`` with kind "home" (a country index), "regional"
    (STOXX 600 for an unmapped European exchange) or "us" (the S&P 500, which
    is also every suffix-less US listing's home)."""
    s = (symbol or "").upper()
    suf = s.rsplit(".", 1)[1] if "." in s and not s.startswith("^") else ""
    if suf in HOME:
        p, n, c = HOME[suf]
        return {"proxy": p, "name": n, "ccy": c, "kind": "home", "backtested": suf in BACKTESTED}
    if suf in EUROPE:
        p, n, c = REGIONAL_EU
        return {"proxy": p, "name": n, "ccy": c, "kind": "regional", "backtested": True}
    p, n, c = US
    return {"proxy": p, "name": n, "ccy": c, "kind": "us" if not suf else "fallback",
            "backtested": True}  # fmt: skip


def fallbacks(symbol: str) -> list[dict]:
    """The proxies to try in order: home, then the region, then the S&P 500."""
    first = home_proxy(symbol)
    out = [first]
    if first["kind"] == "home" and first["ccy"] in ("EUR", "CHF", "SEK", "DKK", "GBP"):
        out.append(dict(zip(("proxy", "name", "ccy"), REGIONAL_EU, strict=True), kind="regional"))
    if first["proxy"] != US[0]:
        out.append(dict(zip(("proxy", "name", "ccy"), US, strict=True), kind="fallback"))
    return out


# Exchange-traded commodities and notes are "stock" to Yahoo (iShares
# Physical Gold ETC, Xetra-Gold) but hold no company: the fund rule.
_ETC = re.compile(r"\b(ETC|ETN|ETP)\b|\bphysical (gold|silver|platinum|palladium|precious"
                  r"|swiss gold|uranium|metals?)\b|^xetra-gold\b", re.IGNORECASE)  # fmt: skip
_COMPANY = re.compile(r"\b(plc|inc|corp|corporation|ltd|ag|sa|se|nv)\.?$", re.IGNORECASE)


def is_fund(symbol: str) -> bool:
    """An ETF, fund or exchange-traded commodity by the symbol pack (no
    network; False without a pack)."""
    try:
        from convexity import symbol_db

        h = symbol_db.get(symbol)
    except Exception:
        return False
    if not h:
        return False
    name = (h.name or "").strip()
    return (h.type or "").lower() in FUND_TYPES or (
        bool(_ETC.search(name)) and not _COMPANY.search(name)
    )


def fund_candidates(symbol: str) -> list[dict]:
    """A fund's benchmarks to fit: its home chain, then the broad markets."""
    out = fallbacks(symbol)
    seen = {p["proxy"] for p in out}
    for p, n, c in FUND_CANDIDATES:
        if p not in seen:
            out.append({"proxy": p, "name": n, "ccy": c, "kind": "fund", "backtested": False})
            seen.add(p)
    return out


def _weekly(px: dict, symbol: str) -> bool:
    """Weekly returns when the proxy trades in another session than the
    listing (a non-US line against a US index, or any non-home index that is
    not the European region for a European line); same-session pairs daily."""
    home = home_proxy(symbol)
    if px["proxy"] == home["proxy"]:
        return home["kind"] == "fallback"  # an unmapped exchange on the S&P
    if px["proxy"] == REGIONAL_EU[0] and (home["kind"] == "regional" or home["ccy"] in
                                          ("EUR", "CHF", "SEK", "DKK", "GBP", "NOK")):  # fmt: skip
        return False
    return home["kind"] != "us" or px["ccy"] != "USD"


def weekly_r2(stock, market, years: int = WINDOW_YEARS) -> float | None:
    """R² of Friday-to-Friday returns over the window (None under 53 weeks)."""
    import pandas as pd

    if stock is None or market is None:
        return None
    df = pd.concat([stock.rename("s"), market.rename("m")], axis=1, join="inner").dropna()
    df = df[(df["s"] > 0) & (df["m"] > 0)].iloc[-(252 * max(int(years), 2) + 1) :]
    wk = df.resample("W-FRI").last().dropna()
    if len(wk) < 53:
        return None
    y, x = np.diff(_log_index(wk["s"])), np.diff(_log_index(wk["m"]))
    if y.std() == 0 or x.std() == 0:
        return None
    c = float(np.corrcoef(y, x)[0, 1])
    return c * c if math.isfinite(c) else None


# ------------------------------- estimators -------------------------------


def _log_index(s) -> np.ndarray:
    """Log prices with glitch returns (|log r| > 1: Yahoo's x52 and negative
    bars) zeroed out, as in the backtest, and one-bar bad prints: a move over
    ``SPIKE`` the next bar takes back (net under a quarter of it), both bars
    zeroed. An S&P 500 ETF on London has a +30%/-28% pair on a Friday
    (2025-10-24) that cut its weekly R² with the S&P from ~0.8 to 0.18."""
    lp = np.log(np.asarray(s, dtype=float))
    r = np.diff(lp)
    r[~np.isfinite(r) | (np.abs(r) > GLITCH)] = 0.0
    if len(r) > 1:
        a, b = r[:-1], r[1:]
        bad = (np.abs(a) > SPIKE) & (np.sign(a) == -np.sign(b)) & (np.abs(a + b) < 0.25 * np.abs(a))
        idx = np.flatnonzero(bad)
        r[idx] = 0.0
        r[idx + 1] = 0.0
    return np.concatenate([[0.0], np.cumsum(r)])


def bswa(y: np.ndarray, x: np.ndarray, rho: float = RHO) -> float:
    """Welch's slope-winsorised, age-weighted beta of returns ``y`` on ``x``
    (oldest first): y clipped to [-2x, 4x] (that is, within 3 beta-units of a
    beta of 1 on each day), weights exp(-rho age), weighted OLS slope."""
    y, x = np.asarray(y, float), np.asarray(x, float)
    lo, hi = np.minimum(-2 * x, 4 * x), np.maximum(-2 * x, 4 * x)
    yw = np.clip(y, lo, hi)
    w = np.exp(-rho * np.arange(len(y))[::-1])
    w /= w.sum()
    xm, ym = (w * x).sum(), (w * yw).sum()
    sxx = (w * (x - xm) ** 2).sum()
    return float((w * (x - xm) * (yw - ym)).sum() / sxx) if sxx > 0 else math.nan


def ols(y: np.ndarray, x: np.ndarray) -> float:
    y, x = np.asarray(y, float), np.asarray(x, float)
    xm, ym = x.mean(), y.mean()
    sxx = ((x - xm) ** 2).sum()
    return float(((x - xm) * (y - ym)).sum() / sxx) if sxx > 0 else math.nan


def shrink(raw: float, w: float = SHRINK) -> float:
    return 1.0 + w * (raw - 1.0)


def dimson(y: np.ndarray, x: np.ndarray) -> float:
    """OLS of y_t on x_t and x_(t-1), the two slopes summed (Dimson 1979):
    what an asynchronous close misses this period it shows the next."""
    y, x = np.asarray(y, float), np.asarray(x, float)
    if len(y) < 3:
        return math.nan
    X = np.column_stack([np.ones(len(x) - 1), x[1:], x[:-1]])
    try:
        b = np.linalg.lstsq(X, y[1:], rcond=None)[0]
    except np.linalg.LinAlgError:
        return math.nan
    return float(b[1] + b[2])


def estimate(
    stock,
    market,
    years: int = WINDOW_YEARS,
    weekly: bool = False,
    plain: bool = False,
    lag: bool = True,
) -> dict:
    """Raw beta of a stock's closes on its market's closes (pandas Series
    indexed by date, same currency): ``{raw, method, n_days, asof}`` where
    method is "bswa", "ols1y", "weekly" or "prior" (raw None). Joint trading
    days only, so a holiday in one market merges into a two-day return in the
    other.

    ``weekly``: the last resort for a non-US listing measured against the
    S&P 500 (an exchange with no home index here). Its close and New York's
    are hours apart, and daily returns then read a beta near zero (Japan
    0.25 vs 1.02 against TOPIX in the backtest; DBS 0.14): Friday-to-Friday
    returns over the window, the backtest's W104 family, with one lag
    (``dimson``): a week's last hours still land in the next week's return
    (an S&P 500 tracker on London read 0.86 plain, 0.99 with the lag).

    ``plain``: daily OLS instead of BSWA (method "ols"), for a fund no market
    index explains (gold, bonds): Welch's winsorising is built for stocks,
    and its band around the market's move pulls a weakly correlated asset
    toward 1 (gold 0.74 against the S&P, OLS 0.28).

    ``lag=False`` (a stock, ``_compute_lines``): weekly OLS without the lag,
    and ``w_scale`` ``WEEKLY_W``. Out of sample (46 non-US names against the
    S&P in USD, quarterly 2018-2024, 1,210 forecasts) the lag added more
    noise than the closing gap's bias: MSE 0.336 vs 0.366 against the next
    year's lagged weekly beta, 0.242 vs 0.277 against the next two years'
    fortnightly one, and both wanted a shrink near 0.5, not 0.9 (R² is low).
    A fund fitted to its index keeps the lag: its R² is high, so the noise
    is small and the bias is what is left (London S&P tracker 0.86 -> 0.99).

    Daily methods record ``zero``, the share of the stock's daily returns
    that are exactly 0 (no trade, or a coarse tick: TSMC 8%), and ``w_scale``
    falls linearly to 0 at ``LIQ_Z0``: a price that does not move on the
    market's day reads a beta too low (-0.38 against the weekly beta over 10%
    zero days), and there every estimator lost to beta = 1."""
    import pandas as pd

    if stock is None or market is None:
        return {"raw": None, "method": "prior", "n_days": 0, "asof": None}
    df = pd.concat([stock.rename("s"), market.rename("m")], axis=1, join="inner").dropna()
    df = df[(df["s"] > 0) & (df["m"] > 0)]
    if weekly:
        # At least two years of weeks (the backtest's W104): one year is ~52
        # weeks, under the 53 needed, so a 1Y window read every weekly beta as
        # the prior.
        years = max(int(years), 2)
        wk = df.iloc[-(252 * int(years) + 1) :].resample("W-FRI").last().dropna()
        asof = str(df.index[-1].date()) if len(df) else None
        if len(wk) < 53:
            return {
                "raw": None,
                "method": "prior",
                "n_days": len(df.iloc[-(252 * int(years) + 1) :]),
                "asof": asof,
            }
        y, x = np.expm1(np.diff(_log_index(wk["s"]))), np.expm1(np.diff(_log_index(wk["m"])))
        b = dimson(y, x) if lag else ols(y, x)
        ok = math.isfinite(b)
        out = {"raw": b if ok else None, "method": "weekly" if ok else "prior",
               "n_days": len(df.iloc[-(252 * int(years) + 1) :]), "asof": asof}  # fmt: skip
        if ok and not lag:
            out["w_scale"] = WEEKLY_W
        return out
    n_win = 252 * int(years) + 1
    d = df.iloc[-n_win:]
    asof = str(d.index[-1].date()) if len(d) else None
    if len(d) >= MIN_BSWA:
        y, x = np.expm1(np.diff(_log_index(d["s"]))), np.expm1(np.diff(_log_index(d["m"])))
        b, method = (ols(y, x), "ols") if plain else (bswa(y, x), "bswa")
    elif len(d) >= MIN_OLS:
        d = d.iloc[-253:]
        y, x = np.expm1(np.diff(_log_index(d["s"]))), np.expm1(np.diff(_log_index(d["m"])))
        b, method = ols(y, x), "ols1y"
    else:
        return {"raw": None, "method": "prior", "n_days": len(d), "asof": asof}
    if not math.isfinite(b):
        return {"raw": None, "method": "prior", "n_days": len(d), "asof": asof}
    zero = float(np.mean(np.abs(y) < 1e-12))
    return {"raw": b, "method": method, "n_days": len(d), "asof": asof,
            "zero": round(zero, 4), "w_scale": round(max(0.0, 1.0 - zero / LIQ_Z0), 4)}  # fmt: skip


def settings(prefs: dict | None = None) -> dict:
    """The model's parameters from prefs.json (bounded there), the defaults
    where unset. Every cache key that depends on the model includes these."""
    from convexity import analyst_targets, options_density

    p = prefs or {}
    years = p.get("model_beta_years", WINDOW_YEARS)
    return {
        "erp": float(p.get("dist_erp", options_density.ERP_DEFAULT)),
        "erp_mode": p.get("model_erp_mode") if p.get("model_erp_mode") in ERP_MODES else "assumed",
        "shrink": float(p.get("model_beta_shrink", SHRINK)),
        "years": int(years) if years in (1, 2, 3, 1.0, 2.0, 3.0) else WINDOW_YEARS,
        "ic": float(p["model_ic"]) if isinstance(p.get("model_ic"), int | float) else None,
        "z_cap": float(p.get("model_z_cap", analyst_targets.Z_CAP)),
    }


def finish(raw: dict, proxy: dict, st: dict) -> dict:
    """A cached raw estimate as the payload's ``beta_meta``: shrunk with the
    current setting (a shrink change never needs a recompute)."""
    r = raw.get("raw")
    w = st["shrink"] * float(raw.get("w_scale", 1.0))
    beta = 1.0 if r is None else shrink(float(r), w)
    return {
        "beta": round(beta, 4),
        "raw": None if r is None else round(float(r), 4),
        "proxy": proxy["proxy"],
        "proxy_name": proxy["name"],
        "proxy_kind": proxy.get("kind"),
        "backtested": proxy.get("backtested", True),
        "n_days": int(raw.get("n_days") or 0),
        "method": raw.get("method") or "prior",
        "asof": raw.get("asof"),
        "fit_r2": raw.get("fit_r2"),
        "fit_best_r2": raw.get("fit_best_r2"),
        "years": st["years"],
        "shrink": st["shrink"],
        "shrink_eff": round(w, 4),
        "zero": raw.get("zero"),
    }


# ------------------------------ cache + batch ------------------------------


def _path():
    from convexity import paths

    return paths.state_file("betas")


def _load() -> dict:
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and isinstance(d.get("items"), dict) else {"items": {}}
    except (OSError, ValueError):
        return {"items": {}}


def _key(symbol: str, years: int) -> str:
    # "f": funds fitted to their benchmark (2026-10-24); "w": shrink scales
    # (2026-10-27). Older entries recompute.
    return f"{(symbol or '').upper()}|{int(years)}|fw"


def _fresh(ent) -> bool:
    return isinstance(ent, dict) and ent.get("day") == dt.date.today().isoformat()


_LINE_LEARNED: dict[str, str] = {}
_LINES_DISK: dict[str, str] | None = None


def beta_line(symbol: str) -> str:
    """See ``_pack_line``; plus what ``learn_line`` found by name for a
    listing the pack doesn't know (a US company's Milan line -> its US line),
    this process or, for a week, on disk (``state/beta_lines.json``)."""
    s = (symbol or "").upper()
    return _LINE_LEARNED.get(s) or _saved_lines().get(s) or _pack_line(s)


LINES_TTL_DAYS = 7


def _lines_path():
    from convexity import paths

    return paths.state_file("beta_lines")


def _saved_lines() -> dict[str, str]:
    """Learned home lines on disk, a week each: without them a restart
    measured a US company's Milan line against the FTSE MIB until the next
    name lookup answered
    (one timed out, and that beta was then kept for the day)."""
    global _LINES_DISK
    if _LINES_DISK is None:
        out = {}
        try:
            d = json.loads(_lines_path().read_text(encoding="utf-8"))
            cut = (dt.date.today() - dt.timedelta(days=LINES_TTL_DAYS)).isoformat()
            for k, v in (d if isinstance(d, dict) else {}).items():
                if (
                    isinstance(v, dict)
                    and isinstance(v.get("line"), str)
                    and str(v.get("day")) >= cut
                ):
                    out[k.upper()] = v["line"].upper()
        except (OSError, ValueError):
            pass
        _LINES_DISK = out
    return _LINES_DISK


def _save_line(symbol: str, line: str) -> None:
    from convexity.persistence import _atomic_write

    _saved_lines()[symbol] = line
    today = dt.date.today().isoformat()
    with _LOCK:
        try:
            p = _lines_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            body = {k: {"line": v, "day": today} for k, v in _saved_lines().items()}
            try:
                old = json.loads(p.read_text(encoding="utf-8"))
                for k in body:
                    if k != symbol and isinstance(old.get(k), dict) and old[k].get("day"):
                        body[k]["day"] = old[k]["day"]
            except (OSError, ValueError, AttributeError):
                pass
            _atomic_write(p, json.dumps(body, indent=1, sort_keys=True))
        except OSError as e:
            print(f"[beta] lines save: {type(e).__name__}")


def unresolved(symbol: str) -> bool:
    """A listing whose company line is still unknown: not in the pack, not
    learned, no definite miss. Its beta on its own line is shown but never
    kept, so the next call (with a name) can still find the company's line."""
    s = (symbol or "").upper()
    if "." not in s or s.startswith("^") or s in _LINE_LEARNED or s in _saved_lines():
        return False
    try:
        from convexity import symbol_db as sdb

        # Without a pack nothing is ever resolved: keep betas as before.
        return sdb.get(s) is None and bool(sdb.status().get("installed"))
    except Exception:
        return False


def _fold(name: str) -> str:
    import re

    return " ".join(re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split())


def learn_line(symbol: str, name: str | None) -> str | None:
    """A listing the symbol pack doesn't know (a US company's line
    on Borsa Italiana's global segment): Yahoo's live Lookup by the company's name, accepted only when a
    hit is a stock the pack does know under the same name, whose home line
    then carries the beta. Network: the batch path only, never the streaming
    one; the answer (a miss too) is kept for the process."""
    from convexity import symbol_db as sdb

    s = (symbol or "").upper()
    if s in _LINE_LEARNED or "." not in s or not name:
        return _LINE_LEARNED.get(s)
    try:
        if sdb.get(s) is not None:
            return None
        want = _fold(name)
        hits = sdb.yahoo_lookup(name)
        if not hits:
            return None  # no answer (a network blip, or Yahoo's own empty): ask again later
        for h in hits:
            known = sdb.get(h.ticker) if h.type == "stock" else None
            if known is None or known.group == sdb.YAHOO_GROUP or _fold(known.name) != want:
                continue
            home = sdb.home(known.group)
            line = (home.ticker if home else known.ticker).upper()
            if line != s:
                _LINE_LEARNED[s] = line
                _save_line(s, line)
                print(f"[beta] {s}: no pack entry; measured on {line} ({name})")
                return line
    except Exception as e:
        print(f"[beta] {s}: lookup {type(e).__name__}")
    _LINE_LEARNED[s] = ""
    return None


@lru_cache(maxsize=8192)
def _pack_line(symbol: str) -> str:
    """The listing whose returns carry the company's beta: its home line when
    ``symbol`` is a secondary listing of a stock (the symbol pack's company
    group; NVD.DE -> NVDA, CCC3.DE -> KO, NSRGY -> NESN.SW, NOV.DE ->
    NOVO-B.CO), else ``symbol`` itself. Measured on the Vienna line against
    the ATX, Micron read as an Austrian stock; the home principle the
    backtest chose is the company's market, not the venue's. Funds keep their
    own line (a UCITS ETF's other listings are the same fund). Pack only, no
    network, cached: safe on the streaming path."""
    s = (symbol or "").upper()
    try:
        from convexity import symbol_db as sdb

        me = sdb.get(s)
        if me is None or me.type != "stock" or me.group == sdb.YAHOO_GROUP:
            return s
        if _ETC.search(me.name or "") and not _COMPANY.search(me.name or ""):
            return s  # an ETC is "stock" to Yahoo; its other lines are other notes
        h = sdb.home(me.group)
        return h.ticker.upper() if h and h.ticker else s
    except Exception:
        return s


def _via(meta: dict | None, symbol: str, line: str) -> dict | None:
    if meta is None:
        return None
    return {**meta, "via": line if line != (symbol or "").upper() else None}


def cached(symbol: str, prefs: dict | None = None) -> dict | None:
    """Today's beta for ``symbol`` (measured on its ``beta_line``) from memory
    or ``state/betas.json``, or None. No network: safe on ``fetch_one``'s
    streaming path."""
    line = beta_line(symbol)
    return _via(_cached_line(line, prefs), symbol, line)


def _cached_line(symbol: str, prefs: dict | None = None) -> dict | None:
    st = settings(prefs)
    k = _key(symbol, st["years"])
    ent = _MEM.get(k)
    if not _fresh(ent):
        ent = _load()["items"].get(k)
        if not _fresh(ent):
            return None
        _MEM[k] = ent
    return finish(ent, ent["proxy"], st)


def _to_ccy(series, frm: str, to: str, fx_series):
    """Closes from currency ``frm`` into ``to`` with the ``usd_per`` series."""
    if frm == to:
        return series
    out = series.copy()
    for c, sign in ((frm, 1.0), (to, -1.0)):
        if c == "USD":
            continue
        f = fx_series.get(c)
        if f is None or f.empty:
            return None
        f = f.reindex(out.index, method="ffill")
        out = out * f if sign > 0 else out / f
    return out.dropna()


def ensure(
    symbols,
    prefs: dict | None = None,
    ccy: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
) -> dict[str, dict]:
    """``{symbol: beta_meta}`` for every symbol, each measured on its
    ``beta_line`` (``beta_meta["via"]`` names the home line when it is not the
    listing itself), computing the missing ones in one bulk download and
    writing them to ``state/betas.json``. A symbol whose data never came back
    gets the prior (beta 1) without being cached, so the next call tries
    again (§9: never cache a failure)."""
    syms = [s.upper() for s in dict.fromkeys(symbols or []) if s]
    for s, n in (names or {}).items():
        learn_line(s, n)
    lines = {s: beta_line(s) for s in syms}
    # A caller's currency belongs to its own listing, never to the home line.
    hints = {k.upper(): v for k, v in (ccy or {}).items()}
    by_line = _ensure_lines(sorted(set(lines.values())), prefs,
                            {ln: hints[ln] for ln in set(lines.values()) if ln in hints},
                            keep=lambda ln: not unresolved(ln))  # fmt: skip
    return {s: _via(by_line.get(lines[s]), s, lines[s]) for s in syms}


def _ensure_lines(
    symbols, prefs: dict | None = None, ccy: dict[str, str] | None = None, keep=None
) -> dict[str, dict]:
    syms = [s.upper() for s in dict.fromkeys(symbols or []) if s]
    out = {s: _cached_line(s, prefs) for s in syms}
    if all(v is not None for v in out.values()):
        return out
    with _COMPUTE:
        return _compute_lines(syms, prefs, ccy, keep)


def _compute_lines(
    symbols, prefs: dict | None = None, ccy: dict[str, str] | None = None, keep=None
) -> dict[str, dict]:
    from convexity import analytics, fx
    from convexity.helpers import major_ccy
    from convexity.persistence import _atomic_write

    st = settings(prefs)
    syms = [s.upper() for s in dict.fromkeys(symbols or []) if s]
    out = {s: _cached_line(s, prefs) for s in syms}
    todo = [s for s, v in out.items() if v is None]
    if not todo:
        return out
    funds = {s for s in todo if is_fund(s)}
    chains = {s: fund_candidates(s) if s in funds else fallbacks(s) for s in todo}
    proxies = sorted({p["proxy"] for c in chains.values() for p in c})
    period = {1: "2y", 2: "5y", 3: "5y"}[st["years"]]
    closes = analytics._bulk_close(todo + proxies, period)
    ccy = {k.upper(): v for k, v in (ccy or {}).items()}

    def listing_ccy(s: str) -> str:
        # The caller's currency (the row's or the chain's); without one, the
        # home index's: right for every listing quoted in its market's money.
        c = ccy.get(s)
        return major_ccy(c) if c else home_proxy(s)["ccy"]

    fx_cache: dict[str, object] = {}

    def usd_per(c: str):
        if c not in fx_cache:
            fx_cache[c] = fx._fx_usd_series(c, period)
        return fx_cache[c]

    fresh = {}
    for s in todo:
        col = closes[s].dropna() if s in getattr(closes, "columns", []) else None
        if col is None or col.empty:
            out[s] = finish({"raw": None, "method": "prior"}, chains[s][0], st)
            continue
        c_s = listing_ccy(s)

        def pair(px):
            # The stock in the index's currency: a GBP index for a USD-quoted
            # London line, USD for the S&P fallback (the backtest's SPX_usd).
            m = closes[px["proxy"]].dropna() if px["proxy"] in closes.columns else None
            if m is None or m.empty:
                return None
            stock = col
            if c_s != px["ccy"]:
                need = {c for c in (c_s, px["ccy"]) if c != "USD"}
                stock = _to_ccy(col, c_s, px["ccy"], {c: usd_per(c) for c in need})
            return None if stock is None else (stock, m)

        best, complete, fit, low = None, True, None, None
        if s in funds:
            scored = []
            for px in chains[s]:
                pm = pair(px)
                r2 = weekly_r2(*pm, st["years"]) if pm else None
                complete &= pm is not None
                if r2 is not None:
                    scored.append((r2, px, pm))
            top = max(scored, key=lambda t: t[0], default=None)
            if top and top[0] >= FUND_MIN_R2:
                fit, px, pm = top
                best = (estimate(*pm, st["years"], weekly=_weekly(px, s), lag=True), px)
            elif top:
                low = top[0]
        if best is None:
            for px in chains[s]:
                pm = pair(px)
                if pm is None:
                    complete = False
                    continue
                best = (
                    estimate(
                        *pm,
                        st["years"],
                        weekly=_weekly(px, s),
                        plain=low is not None,
                        lag=s in funds,
                    ),
                    px,
                )
                complete &= px["proxy"] == chains[s][0]["proxy"]
                break
        if best is None:
            out[s] = finish({"raw": None, "method": "prior"}, chains[s][0], st)
            continue
        raw, px = best
        ent = {**raw, "proxy": px, "day": dt.date.today().isoformat()}
        if fit is not None:
            ent["fit_r2"] = round(fit, 4)
        if low is not None:
            ent["fit_best_r2"] = round(low, 4)
        out[s] = finish(ent, px, st)
        # Every home index answers on Yahoo (probed 2026-10-23), so a beta on a
        # later proxy (or a fund fitted without all its candidates) means a
        # download failed this time: shown, never kept for the day, so the next
        # call measures it again.
        if complete and (keep is None or keep(s)):
            fresh[_key(s, st["years"])] = ent
    if fresh:
        _MEM.update(fresh)
        with _LOCK:
            store = _load()
            today = dt.date.today().isoformat()
            items = {k: v for k, v in store["items"].items() if _fresh(v) or v.get("day") == today}
            items.update(fresh)
            try:
                p = _path()
                p.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(p, json.dumps({"v": 1, "items": items}, indent=1, sort_keys=True))
            except OSError as e:
                print(f"[beta] save: {type(e).__name__}")
        shown = [
            f"{k.split('|')[0]} {v['method']} "
            f"{'-' if v.get('raw') is None else f'{v["raw"]:.2f}'} vs {v['proxy']['proxy']}"
            for k, v in list(fresh.items())[:12]
        ]
        print(f"[beta] {len(fresh)} computed: {', '.join(shown)}")
    return out


def beta_for(
    symbol: str, prefs: dict | None = None, ccy: str | None = None, name: str | None = None
) -> dict:
    return ensure(
        [symbol], prefs, {symbol: ccy} if ccy else None, {symbol: name} if name else None
    )[(symbol or "").upper()]


SCATTER_N = 250


def scatter(
    symbol: str, prefs: dict | None = None, ccy: str | None = None, n: int = SCATTER_N
) -> dict | None:
    """The return pairs behind a symbol's beta, for the model card's chart
    (``GET /api/beta-scatter``): its beta line against the proxy it was
    measured on, in the proxy's currency, glitch-cleaned like ``estimate``,
    the last ``n`` days (weeks for a weekly beta). ``{x, y, freq, proxy,
    proxy_name, raw}``, or None for a prior beta or missing data. Detail
    path only: one cached bulk read, never on ``fetch_one``'s."""
    from convexity import analytics, fx
    from convexity.helpers import major_ccy

    bm = beta_for(symbol, prefs, ccy)
    if not bm or bm.get("raw") is None or bm.get("method") == "prior":
        return None
    line = beta_line(symbol)
    st = settings(prefs)
    ent = _MEM.get(_key(line, st["years"])) or _load()["items"].get(_key(line, st["years"]))
    px = (ent or {}).get("proxy") or {}
    if px.get("proxy") != bm["proxy"]:
        return None
    period = {1: "2y", 2: "5y", 3: "5y"}[st["years"]]
    closes = analytics._bulk_close([line, px["proxy"]], period)
    if line not in closes.columns or px["proxy"] not in closes.columns:
        return None
    stock, mkt = closes[line].dropna(), closes[px["proxy"]].dropna()
    c_s = major_ccy(ccy) if ccy and line == (symbol or "").upper() else home_proxy(line)["ccy"]
    if c_s != px["ccy"]:
        need = {c for c in (c_s, px["ccy"]) if c != "USD"}
        stock = _to_ccy(stock, c_s, px["ccy"], {c: fx._fx_usd_series(c, period) for c in need})
        if stock is None:
            return None
    import pandas as pd

    df = pd.concat([stock.rename("s"), mkt.rename("m")], axis=1, join="inner").dropna()
    df = df[(df["s"] > 0) & (df["m"] > 0)]
    weekly = bm["method"] == "weekly"
    if weekly:
        df = df.iloc[-(252 * max(st["years"], 2) + 1) :].resample("W-FRI").last().dropna()
    else:
        df = df.iloc[-(n + 1) :]
    y, x = np.expm1(np.diff(_log_index(df["s"]))), np.expm1(np.diff(_log_index(df["m"])))
    if len(x) < 20:
        return None
    return {
        "x": [round(float(v), 5) for v in x[-n:]],
        "y": [round(float(v), 5) for v in y[-n:]],
        "freq": "weekly" if weekly else "daily",
        "proxy": px["proxy"],
        "proxy_name": px.get("name"),
        "raw": bm["raw"],
    }


# --------------------------- the equity premium ---------------------------
# Two ways to set it (Settings -> Model price, prefs ``model_erp_mode``):
#
# * "assumed" (the default): one premium for every market, the owner's
#   ``dist_erp``, 5% unless changed.
# * "historical": the long-run premium of the market the beta is measured
#   against, from data/erp_history.json (JST Macrohistory, 16 markets,
#   1870-2020, scripts/build_erp_history.py). A market without a long record
#   of its own (Hong Kong, Canada, Korea...) and a world fund take the pooled
#   16-market premium; STOXX Europe 600 the pooled European one.
#
# Why assumed is the default (the build script's evidence, 2026-10-26): out of
# sample a market's own history forecast its next 10-20 years worse than a
# flat 5% (MSE +14% / +33%), and the gaps between markets are noise (Cochran's
# Q p = 0.90). The premium is the arithmetic-mean one the model price needs:
# S0 * exp(mu T) is a mean, so ERP = ln E[(1 + R_eq) / (1 + R_bill)].
ERP_MODES = ("assumed", "historical")
HIST_MARKET = {
    "^GSPC": "USA", "^FCHI": "France", "^GDAXI": "Germany", "^IBEX": "Spain",
    "FTSEMIB.MI": "Italy", "^AEX": "Netherlands", "^BFX": "Belgium", "^OMXH25": "Finland",
    "^SSMI": "Switzerland", "^OMX": "Sweden", "^OMXC25": "Denmark", "^FTAS": "UK",
    "1306.T": "Japan", "^AXJO": "Australia", "OSEBX.OL": "Norway", "PSI20.LS": "Portugal",
    "^STOXX": "europe",
}  # fmt: skip
POOL_NAMES = {"world": "16 developed markets", "europe": "13 European markets"}
# World funds are measured against these: the pooled premium is their own.
_WORLD_PROXIES = {"ACWI", "EEM"}


@lru_cache(maxsize=1)
def erp_history() -> dict | None:
    """The shipped table (data/erp_history.json), or None when it is missing
    or malformed: the historical mode then falls back to the assumed premium
    and says so (``erp_meta.reason``)."""
    from importlib import resources

    try:
        doc = json.loads(
            resources.files("convexity").joinpath("data/erp_history.json").read_text("utf-8")
        )
        if isinstance(doc.get("markets"), dict) and isinstance(doc.get("pools"), dict):
            return doc
    except Exception as exc:
        print(f"[capm] erp_history.json unreadable: {type(exc).__name__}: {exc}", flush=True)
    return None


def erp_for(beta_meta: dict | None, st: dict) -> tuple[float, dict]:
    """The premium for one name and what it is: ``(erp, erp_meta)``. In the
    historical mode the market is the beta's own proxy, so beta and premium
    describe the same market (a London S&P tracker: the S&P's beta and the US
    premium)."""
    if st.get("erp_mode") != "historical":
        return st["erp"], {"mode": "assumed"}
    h = erp_history()
    if h is None:
        return st["erp"], {"mode": "assumed", "reason": "no_history"}
    proxy = (beta_meta or {}).get("proxy") or US[0]
    key = HIST_MARKET.get(proxy, "world")
    pooled = key in h["pools"]
    row = h["pools"][key] if pooled else h["markets"].get(key) or h["pools"]["world"]
    meta = {
        "mode": "historical",
        "market": POOL_NAMES.get(key, key),
        "pooled": pooled,
        # A home market with no long record of its own, as opposed to a
        # world fund whose market IS the pool.
        "fallback": key == "world" and proxy not in _WORLD_PROXIES,
        "proxy_name": (beta_meta or {}).get("proxy_name"),
        "from": row["from"],
        "to": row["to"],
        "years": row["years"],
        "se": row["se"],
        "geometric": row["geometric"],
    }
    return float(row["erp"]), meta


# ------------------------------- the model --------------------------------


def alpha_for(upside: float | None, st: dict) -> tuple[float, dict]:
    """The analysts' tilt for one name: ``relative_alpha`` on its total upside
    (target / price - 1 + dividend yield, as the optimizer's views), with the
    IC the owner set or the app's measured/prior one, z capped at ``z_cap``.
    The reference is the long-run universe mean upside blended with the
    name's own (``SHRINK_N0`` : 1), so one name gets one alpha wherever it is
    shown."""
    from convexity import analyst_targets as at

    if st["ic"] is not None:
        ic, src = st["ic"], "set"
    else:
        ic, src = at.view_ic()
    meta = {"ic": ic, "ic_source": src, "z": None, "upside": upside, "ref_upside": None,
            "z_cap": st["z_cap"], "sigma_xs": at.SIGMA_XS, "upside_sd": at.UPSIDE_SD}  # fmt: skip
    if upside is None or not math.isfinite(upside):
        meta["reason"] = "no_targets"
        return 0.0, meta
    al, ref = at.relative_alpha({"x": upside}, ic, z_cap=st["z_cap"])
    if "x" not in al:
        meta["reason"] = "no_targets"
        return 0.0, meta
    meta.update(z=round(al["x"]["z"], 4), ref_upside=ref.get("ref_upside"))
    return float(al["x"]["alpha"]), meta


def model(
    symbol: str,
    T: float,
    *,
    s0: float,
    ccy: str | None,
    target: float | None = None,
    div_yield: float | None = None,
    prefs: dict | None = None,
    beta_meta: dict | None = None,
    rate: tuple[float, str] | None = None,
    name: str | None = None,
) -> dict:
    """THE model block at horizon ``T`` (years) for a listing at price ``s0``:
    ``{mu, mu_capm, rf, rf_source, beta, beta_meta, erp, erp_meta, alpha,
    alpha_meta, capm_price, model_price, T, s0}``. ``mu``/``model_price`` include the
    analysts' alpha (mu on), ``mu_capm``/``capm_price`` are pure CAPM (mu
    off), so the page's toggle needs no refetch. ``target`` is the 12-month
    mean target in the same currency and units as ``s0``."""
    from convexity import options_density

    st = settings(prefs)
    bm = beta_meta or beta_for(symbol, prefs, ccy, name)
    rf, rsrc = rate or options_density._local_rate(ccy or "")
    upside = None
    if target and s0 and target > 0 and s0 > 0:
        dy = div_yield if isinstance(div_yield, int | float) and 0 <= div_yield < 0.5 else 0.0
        upside = target / s0 - 1.0 + dy
    alpha, am = alpha_for(upside, st)
    erp, em = erp_for(bm, st)
    mu_capm = rf + bm["beta"] * erp
    mu = mu_capm + alpha
    return {
        "mu": round(mu, 6),
        "mu_capm": round(mu_capm, 6),
        "rf": round(rf, 6),
        "rf_source": rsrc,
        "beta": bm["beta"],
        "beta_meta": bm,
        "erp": round(erp, 6),
        "erp_meta": em,
        "alpha": round(alpha, 6),
        "alpha_meta": am,
        "T": round(float(T), 6),
        "s0": s0,
        "capm_price": round(model_price(s0, mu_capm, T), 6),
        "model_price": round(model_price(s0, mu, T), 6),
    }


def model_price(s0: float, mu: float, T: float) -> float:
    """S0 exp(mu T): the one formula every surface uses."""
    return float(s0) * math.exp(float(mu) * float(T))


def for_rows(rows: list[dict], prefs: dict | None = None, T: float = 1.0) -> dict[str, dict]:
    """The model block at ``T`` for every row (grid, export): one batch beta,
    the row's own price, currency, mean target and dividend yield, one rate per
    currency."""
    from convexity import options_density

    def num(v):
        return float(v) if isinstance(v, int | float) and math.isfinite(v) else None

    rows = [r for r in rows or [] if isinstance(r, dict) and isinstance(r.get("symbol"), str)
            and r["symbol"].strip() and not r.get("error")]  # fmt: skip
    bm = ensure([r["symbol"] for r in rows], prefs, {r["symbol"]: r.get("currency") for r in rows},
                {r["symbol"]: r["name"] for r in rows if isinstance(r.get("name"), str)})  # fmt: skip
    rates: dict[str, tuple] = {}
    out = {}
    for r in rows:
        s = r["symbol"].upper()
        px = num(r.get("price"))
        if not (px and px > 0):
            continue
        c = r.get("currency") if isinstance(r.get("currency"), str) else ""
        if c not in rates:
            rates[c] = options_density._local_rate(c)
        out[s] = model(s, T, s0=px, ccy=c, target=num(r.get("target_mean_price")),
                       div_yield=num(r.get("dividend_yield")), prefs=prefs, beta_meta=bm.get(s),
                       rate=rates[c])  # fmt: skip
    return out
