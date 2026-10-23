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
* ``ERP`` is the owner's ``dist_erp`` (5% by default).
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
import threading

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
}
BACKTESTED = {"PA", "DE", "MC", "MI", "AS", "BR", "HE", "VI", "SW", "ST", "CO", "L", "T"}
# European listings with no mapped country index: the region.
EUROPE = {"LS", "OL", "IR", "WA", "PR", "BD", "AT", "IC", "RG", "TL", "VS", "MT"}
REGIONAL_EU = ("^STOXX", "STOXX Europe 600", "EUR")
US = ("^GSPC", "S&P 500", "USD")

WINDOW_YEARS = 3  # default; prefs model_beta_years (1, 2 or 3)
SHRINK = 0.9  # default; prefs model_beta_shrink
RHO = 2.0 / 252.0  # age decay per trading day (Welch: half-life ~87 days)
MIN_BSWA = 252  # joint price points for BSWA (the backtest's rule)
MIN_OLS = 151  # ... for the 1-year daily OLS below that; fewer = prior
GLITCH = 1.0  # |log return| above this is a data glitch, not a move
TTL_DAYS = 1  # a beta is recomputed once a day

_LOCK = threading.Lock()
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


# ------------------------------- estimators -------------------------------


def _log_index(s) -> np.ndarray:
    """Log prices with glitch returns (|log r| > 1: Yahoo's x52 and negative
    bars) zeroed out, as in the backtest."""
    lp = np.log(np.asarray(s, dtype=float))
    r = np.diff(lp)
    r[~np.isfinite(r) | (np.abs(r) > GLITCH)] = 0.0
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


def estimate(stock, market, years: int = WINDOW_YEARS) -> dict:
    """Raw beta of a stock's closes on its market's closes (pandas Series
    indexed by date, same currency): ``{raw, method, n_days, asof}`` where
    method is "bswa", "ols1y" or "prior" (raw None). Joint trading days only,
    so a holiday in one market merges into a two-day return in the other."""
    import pandas as pd

    if stock is None or market is None:
        return {"raw": None, "method": "prior", "n_days": 0, "asof": None}
    df = pd.concat([stock.rename("s"), market.rename("m")], axis=1, join="inner").dropna()
    df = df[(df["s"] > 0) & (df["m"] > 0)]
    n_win = 252 * int(years) + 1
    d = df.iloc[-n_win:]
    asof = str(d.index[-1].date()) if len(d) else None
    if len(d) >= MIN_BSWA:
        y, x = np.expm1(np.diff(_log_index(d["s"]))), np.expm1(np.diff(_log_index(d["m"])))
        b, method = bswa(y, x), "bswa"
    elif len(d) >= MIN_OLS:
        d = d.iloc[-253:]
        y, x = np.expm1(np.diff(_log_index(d["s"]))), np.expm1(np.diff(_log_index(d["m"])))
        b, method = ols(y, x), "ols1y"
    else:
        return {"raw": None, "method": "prior", "n_days": len(d), "asof": asof}
    if not math.isfinite(b):
        return {"raw": None, "method": "prior", "n_days": len(d), "asof": asof}
    return {"raw": b, "method": method, "n_days": len(d), "asof": asof}


def settings(prefs: dict | None = None) -> dict:
    """The model's parameters from prefs.json (bounded there), the defaults
    where unset. Every cache key that depends on the model includes these."""
    from convexity import analyst_targets, options_density

    p = prefs or {}
    years = p.get("model_beta_years", WINDOW_YEARS)
    return {
        "erp": float(p.get("dist_erp", options_density.ERP_DEFAULT)),
        "shrink": float(p.get("model_beta_shrink", SHRINK)),
        "years": int(years) if years in (1, 2, 3, 1.0, 2.0, 3.0) else WINDOW_YEARS,
        "ic": float(p["model_ic"]) if isinstance(p.get("model_ic"), int | float) else None,
        "z_cap": float(p.get("model_z_cap", analyst_targets.Z_CAP)),
    }


def finish(raw: dict, proxy: dict, st: dict) -> dict:
    """A cached raw estimate as the payload's ``beta_meta``: shrunk with the
    current setting (a shrink change never needs a recompute)."""
    r = raw.get("raw")
    beta = 1.0 if r is None else shrink(float(r), st["shrink"])
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
        "years": st["years"],
        "shrink": st["shrink"],
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
    return f"{(symbol or '').upper()}|{int(years)}"


def _fresh(ent) -> bool:
    return isinstance(ent, dict) and ent.get("day") == dt.date.today().isoformat()


def cached(symbol: str, prefs: dict | None = None) -> dict | None:
    """Today's beta for ``symbol`` from memory or ``state/betas.json``, or
    None. No network: safe on ``fetch_one``'s streaming path."""
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
    symbols, prefs: dict | None = None, ccy: dict[str, str] | None = None
) -> dict[str, dict]:
    """``{symbol: beta_meta}`` for every symbol, computing the missing ones in
    one bulk download and writing them to ``state/betas.json``. A symbol whose
    data never came back gets the prior (beta 1) without being cached, so the
    next call tries again (§9: never cache a failure)."""
    from convexity import analytics, fx
    from convexity.helpers import major_ccy
    from convexity.persistence import _atomic_write

    st = settings(prefs)
    syms = [s.upper() for s in dict.fromkeys(symbols or []) if s]
    out = {s: cached(s, prefs) for s in syms}
    todo = [s for s, v in out.items() if v is None]
    if not todo:
        return out
    chains = {s: fallbacks(s) for s in todo}
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
        best = None
        for px in chains[s]:
            m = closes[px["proxy"]].dropna() if px["proxy"] in closes.columns else None
            if m is None or m.empty:
                continue
            # The stock in the index's currency: a GBP index for a USD-quoted
            # London line, USD for the S&P fallback (the backtest's SPX_usd).
            stock = col
            if c_s != px["ccy"]:
                need = {c for c in (c_s, px["ccy"]) if c != "USD"}
                stock = _to_ccy(col, c_s, px["ccy"], {c: usd_per(c) for c in need})
                if stock is None:
                    continue
            raw = estimate(stock, m, st["years"])
            best = (raw, px)
            break
        if best is None:
            out[s] = finish({"raw": None, "method": "prior"}, chains[s][0], st)
            continue
        raw, px = best
        ent = {**raw, "proxy": px, "day": dt.date.today().isoformat()}
        fresh[_key(s, st["years"])] = ent
        out[s] = finish(ent, px, st)
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


def beta_for(symbol: str, prefs: dict | None = None, ccy: str | None = None) -> dict:
    return ensure([symbol], prefs, {symbol: ccy} if ccy else None)[(symbol or "").upper()]


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
            "z_cap": st["z_cap"], "sigma_xs": at.SIGMA_XS}  # fmt: skip
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
) -> dict:
    """THE model block at horizon ``T`` (years) for a listing at price ``s0``:
    ``{mu, mu_capm, rf, rf_source, beta, beta_meta, erp, alpha, alpha_meta,
    capm_price, model_price, T, s0}``. ``mu``/``model_price`` include the
    analysts' alpha (mu on), ``mu_capm``/``capm_price`` are pure CAPM (mu
    off), so the page's toggle needs no refetch. ``target`` is the 12-month
    mean target in the same currency and units as ``s0``."""
    from convexity import options_density

    st = settings(prefs)
    bm = beta_meta or beta_for(symbol, prefs, ccy)
    rf, rsrc = rate or options_density._local_rate(ccy or "")
    upside = None
    if target and s0 and target > 0 and s0 > 0:
        dy = div_yield if isinstance(div_yield, int | float) and 0 <= div_yield < 0.5 else 0.0
        upside = target / s0 - 1.0 + dy
    alpha, am = alpha_for(upside, st)
    mu_capm = rf + bm["beta"] * st["erp"]
    mu = mu_capm + alpha
    return {
        "mu": round(mu, 6),
        "mu_capm": round(mu_capm, 6),
        "rf": round(rf, 6),
        "rf_source": rsrc,
        "beta": bm["beta"],
        "beta_meta": bm,
        "erp": st["erp"],
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

    rows = [r for r in rows or [] if isinstance(r.get("symbol"), str) and not r.get("error")]
    bm = ensure([r["symbol"] for r in rows], prefs, {r["symbol"]: r.get("currency") for r in rows})
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
