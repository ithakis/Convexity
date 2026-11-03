"""The long view of each holding, kept once a month (owner's request, 2026-11-02).

Two things on the page need more history than the page ever fetches: how long a
holding's falls took to recover (daily closes as far back as Yahoo goes) and the
holdings' three-year covariance (the steadier second reading of a holding's share
of the risk, and the holdings' own 1-in-20 month). Neither moves much in a month,
and max history for a whole book is a lot to ask of Yahoo, so:

- ``state/long_run.json`` keeps the results, never the closes. It is market data
  only, a cache: a malformed file is ignored and rebuilt, and the page does only
  algebra on it. No fetch ever happens on the page's path; what isn't there yet
  is simply left out of the page.
- A background job (``longrun``, single-flight with every refresh, hidden from
  the chip, gives way to the owner's Refresh) fills what is due: a holding with
  no record, or one from an earlier month. A new holding is picked up within a
  minute or two of being added, the month's turn at the next quiet moment.
- Limits: a few symbols at a time with a pause between, no one-by-one retries, a
  failed chunk or two ends the pass until the next try. A shorter answer than the
  one kept is a degraded answer (the jobs rule): the old record stays.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from convexity import paths
from convexity.persistence import _atomic_write

VERSION = 2  # of the kept file: bump when a constant below changes, and it is rebuilt
CHUNK = 4  # symbols per request
PAUSE_S = 4.0  # between requests
FIRST_TICK_S = 180  # after launch: the page and its warm-ups go first
TICK_S = 60
RETRY_S = 6 * 3600  # a symbol Yahoo didn't answer for
PASS_RETRY_S = 3600  # a pass that gave up
MAX_FAILED_CHUNKS = 2
MIN_DAYS = 60
SHORT_YEARS = 10  # a listing younger than this may borrow the company's longer line
KEEP_FRACTION = 0.9  # a new answer under this share of the old history is degraded

COV_SPAN = "5y"  # yfinance has no 3y period; the last COV_WEEKS are used
COV_WEEKS = 156
COV_MIN_WEEKS = 20
COV_SHRINK = 0.33  # towards constant correlation, the Ledoit-Wolf intensity on this window
COV_ANCHORS = ("W-MON", "W-TUE", "W-WED", "W-THU", "W-FRI")
COVERED = 0.95  # the book's figures need this share of its value measured
MAX_BOOK_VOL = 1.5  # above this the matrix is reading bad data, not a book
Z_95 = 1.645
MONTH_DAYS = 21

_LOCK = threading.RLock()
_MEMO: dict = {"v": None}
_STATE: dict = {"thread": None, "stop": threading.Event(), "next": 0.0, "tried": {}}


# ----------------------------------------------------------- time to recover

MIN_FALL = 0.10  # a smaller dip isn't a recovery worth timing

# A recovery is a first passage, so inverse Gaussian. The owner's peaks-over-threshold
# idea was tested against it on 40 public large caps, max history, about 1500 falls
# (docs/recovery_research.md): a lognormal body with a GPD tail scores +.008 (se .006)
# out of sample, not worth a threshold, and its tail index runs from .78 at 3 months
# to .02 at 6 years, so it has no one value to report. One holding's falls can't carry
# two parameters: the shape is the panel's. Its scale starts from its volatility, which
# rests on thousands of returns where its falls are a dozen: over every split tested, a
# fall's own three-year volatility says more than the holding's past falls do, and a
# calmer share takes longer, since 10% is further for it. Its own falls then move the
# scale by n / (n + RECOVERY_N0). Recompute with scripts/study_recovery.py, and bump
# VERSION so the kept records are rebuilt.
RECOVERY_PHI = 0.30  # lambda / mu
RECOVERY_MU = 269.0  # trading days, at RECOVERY_VOL_REF
RECOVERY_VOL_SLOPE = -1.02  # of the log scale on log volatility
RECOVERY_VOL_REF = 0.30
RECOVERY_VOL_RANGE = (0.14, 0.73)  # the panel's falls, 1st to 99th percentile: no extrapolating
RECOVERY_N0 = 25
VOL_DAYS = 756  # three years of daily returns before a fall's high
VOL_MIN_DAYS = 120

# Every stretch below the running all-time high, however small: the histogram (owner,
# 2026-11-03, after 10%+ falls alone left its bars empty and hid that half of all dips are
# back within a week). The curve is a lognormal truncated below two days: its extreme
# parameters are just a bending power law, and it holds the tail (2.8% past a year, 2.8%
# seen) where one that starts at two days halves it. Other two-parameter curves score the
# same. A holding's curve leans on the panel's by DIP_N0 dips: even at a hundred, its
# own later dips are told better by the panel's shape.
DIP_BINS = np.geomspace(2, 7560, 31)  # two days (down one, back the next) to thirty years
DIP_MU, DIP_SIGMA, DIP_N0 = -5.08, 3.66, 100

# A running fall gets its own estimate, never a ratio of the curve above: the curve
# doesn't know how far the price still has to go, and told a holding 50% down after
# 2.4 years that 6 in 10 are still out a year later when 96% were. Censored lognormal
# on the days left: ln(days left) = b0 + b_a ln a + b_t ln t + b_w ln w, with a the log
# distance to the high (floored), t the days so far, w the worst fall as a fraction.
FROM_HERE = {"b0": 5.1645, "b_a": 1.2995, "b_t": 0.3088, "b_w": -0.5965, "sigma": 1.2054}
FROM_HERE_FLOOR = 0.005
Z_90 = 1.2816
LONGEST_SEEN = 5949  # trading days, the panel's longest fall: past it the data says nothing

# Yahoo's oldest data is not always data. _xray_clean removes single-day glitches and
# nothing else on max history; these are series-level problems (usable_start).
GAP_DAYS = 30
JUMP_UP, JUMP_DOWN = 1.0, -0.75
JUMP_OLD_YEARS = 5  # bad data is old; a recent day like this is more likely a real crash
SPLIT_RATIOS = (1 / 2, 1 / 3, 1 / 4, 1 / 5)
SPLIT_NEAR, SPLIT_CALM = 0.03, 0.05
STALE_SHARE = 0.5
WHIPSAW = (0.30, 0.25, 10, 3)  # moves over 30% undone by 25% within 10 rows, 3 in a year


def repair_splits(dates: pd.DatetimeIndex, px: np.ndarray) -> np.ndarray:
    """``px`` with Yahoo's unadjusted splits undone: more than ``JUMP_OLD_YEARS`` ago, a
    fall to an exact fraction among calm days rescales everything before it. Cutting
    there threw away all the good years before. On 40 large caps this finds MCD's two 2:1
    splits and nothing else (AAPL's -52% in 2000 had a loud day beside it). A recent crash
    stays: it is what the card is for. The cost: a real -50% day among calm days, years
    ago, reads as a split. Yahoo's split list can't tell them apart, since a split it
    knows is already in its closes; what is left here is the one it missed."""
    out = px.astype(float)
    r = np.r_[0.0, px[1:] / px[:-1] - 1]
    old = dates < dates[-1] - pd.Timedelta(days=365 * JUMP_OLD_YEARS)
    for i in reversed(np.flatnonzero((r < -0.4) & old)):
        k = min(SPLIT_RATIOS, key=lambda x: abs(1 + r[i] - x))
        side = np.r_[r[max(1, i - 3) : i], r[i + 1 : i + 4]]
        if abs(1 + r[i] - k) < SPLIT_NEAR and (not len(side) or np.abs(side).max() < SPLIT_CALM):
            out[:i] *= k
    return out


def usable_start(
    dates: pd.DatetimeIndex, px: np.ndarray, asof: pd.Timestamp | None = None
) -> pd.Timestamp | None:
    """The last sign of bad old data, or None, once ``repair_splits`` has done what it
    can. A fake fall reads as one that never recovered: one line showed -92% "running"
    for thirty years before this. After the last of: a hole in the dates, a year with
    over half its returns exactly zero, a year of big moves that keep undoing themselves,
    or, more than ``JUMP_OLD_YEARS`` before ``asof`` (the last row), a day over +100% or
    under -75%."""
    r = np.r_[0.0, px[1:] / px[:-1] - 1]
    bad = list(
        np.flatnonzero(np.diff(dates.values).astype("timedelta64[D]").astype(int) > GAP_DAYS) + 1
    )
    bad += list(np.flatnonzero(pd.Series(r == 0).rolling(252).mean().to_numpy() > STALE_SHARE))
    big, undo, within, count = WHIPSAW
    undone = np.zeros(len(r))
    for i in np.flatnonzero(np.abs(r) > big):
        w = r[i + 1 : i + 1 + within]
        undone[i] = bool(np.any((np.sign(w) == -np.sign(r[i])) & (np.abs(w) > undo)))
    bad += list(np.flatnonzero(pd.Series(undone).rolling(252).sum().to_numpy() >= count))
    old = dates < (dates[-1] if asof is None else asof) - pd.Timedelta(days=365 * JUMP_OLD_YEARS)
    bad += [i for i in np.flatnonzero((r > JUMP_UP) | (r < JUMP_DOWN)) if old[i]]
    return dates[max(bad)] if bad else None


def from_here(now_pct: float | None, days: int | None, worst_pct: float | None) -> dict | None:
    """Trading days until half and nine in ten of falls like this one were back."""
    if not days or now_pct is None or worst_pct is None or now_pct <= -100 or worst_pct >= 0:
        return None
    a = max(-math.log1p(now_pct / 100), FROM_HERE_FLOOR)
    c = FROM_HERE
    mid = (
        c["b0"]
        + c["b_a"] * math.log(a)
        + c["b_t"] * math.log(days)
        + c["b_w"] * math.log(-worst_pct / 100)
    )
    q50, q90 = math.exp(mid), math.exp(mid + Z_90 * c["sigma"])
    if days + q50 > LONGEST_SEEN:  # past every fall on record: no figure is honest
        return {"q50": None, "q90": None, "beyond": True}
    return {"q50": round(q50), "q90": round(q90), "beyond": days + q90 > LONGEST_SEEN}


def _vol_before(returns: np.ndarray, i: int) -> float | None:
    """Annualised volatility over the three years before row ``i``."""
    r = returns[max(0, i - VOL_DAYS) : i]
    return round(float(r.std()) * 252**0.5, 4) if len(r) >= VOL_MIN_DAYS else None


def episodes(px: np.ndarray) -> dict:
    """Every fall of ``MIN_FALL`` or more from a high: ``days``, ``depths`` (percent) and
    ``vols`` (volatility before its high) of the finished ones, and the running one kept
    apart: a fall still running hasn't recovered, and leaving it out would make the
    holding that never came back look the quickest. ``vol_now`` is what the next fall
    starts from, or, in a fall, what this one did: the fall itself shouldn't inflate it."""
    returns = np.diff(np.log(px))
    dd = px / np.maximum.accumulate(px) - 1
    highs = np.flatnonzero(dd == 0)
    done = [(int(b - a), float(dd[a:b].min()) * 100, _vol_before(returns, int(a)))
            for a, b in zip(highs, highs[1:], strict=False) if dd[a:b].min() <= -MIN_FALL]  # fmt: skip
    last = int(highs[-1])
    running = bool(dd[last:].min() <= -MIN_FALL)
    return {"days": [d for d, _, _ in done], "depths": [round(x, 1) for _, x, _ in done],
            "vols": [v for _, _, v in done],
            "vol_now": (_vol_before(returns, last if running else len(returns))
                        or _vol_before(returns, len(returns))),  # a young line: what there is
            "open_days": len(px) - 1 - last if running else None,
            "open_depth": round(float(dd[last:].min()) * 100, 1) if running else None,
            "open_now": round(float(dd[-1]) * 100, 1) if running else None}  # fmt: skip


def dip_days(px: np.ndarray) -> tuple[list[int], int | None]:
    """A running all-time high, and the days each stretch below it took to get back: the
    finished ones, and the stretch still running, if any."""
    highs = np.flatnonzero(px >= np.maximum.accumulate(px))
    gaps = np.diff(highs)
    return gaps[gaps > 1].tolist(), (len(px) - 1 - int(highs[-1]) or None)


def _dip_sf(k, mu: float, sigma: float) -> np.ndarray:
    """P(days > k) on whole days of the lognormal truncated below two days."""
    from scipy.special import log_ndtr

    z = lambda x: (np.log(np.asarray(x, float) + 0.5) - mu) / sigma  # noqa: E731
    return np.exp(log_ndtr(-z(k)) - log_ndtr(-z(1)))


def dip_histogram(days: list[int], running: int | None, big: list[int]) -> dict | None:
    """Thirty bars of ``days``, how many of each were ``big`` (falls of ``MIN_FALL`` or
    more), the curve in expected dips per bar, and the plain median and 90th percentile:
    with this many, the sample says it directly. The running stretch is censored in the
    fit. None without a finished dip."""
    from scipy.optimize import minimize

    if not days:
        return None
    k = np.asarray(days, float)

    def nll(th):
        mu, sigma = th[0], math.exp(th[1])
        p = _dip_sf(k - 1, mu, sigma) - _dip_sf(k, mu, sigma)
        ll = np.log(np.maximum(p, 1e-300)).sum()
        return -ll - math.log(max(float(_dip_sf(running, mu, sigma)), 1e-300) if running else 1.0)

    own = minimize(nll, [DIP_MU, math.log(DIP_SIGMA)], method="Nelder-Mead").x
    edge, n = DIP_BINS, len(k) + (1 if running else 0)
    # Whole days in each bar; the last bar takes everything longer. The two curves are
    # blended as curves, since their parameters trade off along a ridge.
    last = np.ceil(edge[1:]).astype(int) - 1
    w = len(k) / (len(k) + DIP_N0)
    sf = _dip_sf(last, own[0], math.exp(own[1])) ** w * _dip_sf(last, DIP_MU, DIP_SIGMA) ** (1 - w)
    sf = np.r_[1.0, sf[:-1], 0.0]
    bars = lambda x: np.histogram(np.clip(x, edge[0], edge[-1]), edge)[0].tolist()  # noqa: E731
    return {"bins": [round(float(x), 1) for x in edge], "counts": bars(k),
            "big": bars(big) if big else [0] * (len(edge) - 1),
            "curve": [round(float(x) * n, 3) for x in -np.diff(sf)], "n": len(k), "running": running,
            "q50": round(float(np.quantile(k, 0.5))), "q90": round(float(np.quantile(k, 0.9))),
            "lo": int(k.min()), "hi": int(k.max())}  # fmt: skip


def _vol_scale(vol: float | None) -> float:
    """The log scale a volatility sets; the panel's middle when there's too little to read."""
    if not vol or not math.isfinite(vol):
        return 0.0
    lo, hi = RECOVERY_VOL_RANGE
    return RECOVERY_VOL_SLOPE * math.log(min(max(vol, lo), hi) / RECOVERY_VOL_REF)


def _ig_cdf(t, mu: float) -> np.ndarray:
    """Inverse Gaussian CDF with lambda = RECOVERY_PHI * mu."""
    from scipy.special import ndtr

    t, lam = np.asarray(t, float), RECOVERY_PHI * mu
    r = np.sqrt(lam / t)
    return ndtr(r * (t / mu - 1)) + math.exp(2 * RECOVERY_PHI) * ndtr(-r * (t / mu + 1))


def recovery_fit(
    days: list[int], open_days: int | None, vols: list | None = None, vol: float | None = None
) -> dict | None:
    """How long a fall of ``MIN_FALL`` or more takes: the median and 90th percentile of its
    curve. ``vols`` is each fall's volatility before its high, ``vol`` the next fall's (and
    the running one's): a fall is read against what its own volatility predicts, and the
    answer is for ``vol``. The running fall is censored: it says "at least this long".
    None without a finished fall."""
    from scipy.optimize import brentq, minimize_scalar

    if not days:
        return None
    vols = vols or [None] * len(days)
    d = (np.asarray(days, float) + 0.5) / np.exp([_vol_scale(v) for v in vols])
    still = (open_days + 0.5) / math.exp(_vol_scale(vol)) if open_days else None

    def nll(log_mu: float) -> float:
        mu = math.exp(log_mu)
        lam = RECOVERY_PHI * mu
        ll = np.sum(0.5 * np.log(lam / (2 * np.pi * d**3)) - lam * (d - mu) ** 2 / (2 * mu**2 * d))
        if still:
            ll += math.log(max(1e-300, 1 - float(_ig_cdf(still, mu))))
        return -float(ll)

    own = minimize_scalar(nll, bounds=(1.0, 12.0), method="bounded").x
    pooled = math.log(RECOVERY_MU)
    mu = math.exp(pooled + _vol_scale(vol) + len(d) / (len(d) + RECOVERY_N0) * (own - pooled))
    quant = lambda q: brentq(lambda x: float(_ig_cdf(x, mu)) - q, 0.1, 1e7)  # noqa: E731
    return {"q50": round(quant(0.5)), "q90": round(quant(0.9))}


# ---------------------------------------------------------------- covariance


def weekly_cov(closes: pd.DataFrame) -> tuple[list[str], np.ndarray] | None:
    """(symbols, annualised covariance) from the last three years of weekly
    returns, averaged over the five weekday anchors (one anchor's noise moved a
    share by up to 3.5 points, risk study 2026-11-02). Pairwise, so a young
    listing doesn't cut everyone's window; a pair with no overlap takes the
    average correlation, and shrinkage plus an eigenvalue floor keep it a real
    covariance. A line with too few weeks is left out."""
    covs = []
    for anchor in COV_ANCHORS:
        r = closes.resample(anchor).last().pct_change(fill_method=None).iloc[1:].iloc[-COV_WEEKS:]
        covs.append(r.cov(min_periods=COV_MIN_WEEKS).to_numpy() * 52)
    stack = np.stack(covs)
    seen = np.isfinite(stack).sum(axis=0)
    s = np.where(seen > 0, np.nansum(stack, axis=0) / np.maximum(seen, 1), np.nan)
    keep = [i for i in range(s.shape[0]) if np.isfinite(s[i, i]) and s[i, i] > 0]
    if len(keep) < 1:
        return None
    s = s[np.ix_(keep, keep)]
    sd = np.sqrt(np.diag(s))
    corr = s / np.outer(sd, sd)
    off = corr[~np.eye(len(keep), dtype=bool)]
    rho = float(np.nanmean(off)) if off.size and np.isfinite(off).any() else 0.0
    target = rho * np.outer(sd, sd)
    np.fill_diagonal(target, sd**2)
    out = np.where(np.isfinite(s), (1 - COV_SHRINK) * s + COV_SHRINK * target, target)
    out = (out + out.T) / 2
    vals, vecs = np.linalg.eigh(out)
    out = (vecs * np.clip(vals, 1e-8, None)) @ vecs.T
    return [str(closes.columns[i]) for i in keep], out


def book_view(positions: list[dict], ccy: str) -> dict:
    """{"shares": {symbol: share of the variance}, "bad_month": {value, pct}} for
    today's holdings on the kept covariance: algebra only, so the page may call
    it. Empty when the kept matrix misses too much of the book."""
    cov = (load().get("cov") or {}).get(ccy)
    if not cov:
        return {}
    where = {s: i for i, s in enumerate(cov["symbols"])}
    held = [(p["symbol"], float(p.get("value") or 0)) for p in positions if not p.get("pending")]
    total = sum(v for _, v in held)
    inside = [(s, v) for s, v in held if s in where and v > 0]
    if total <= 0 or sum(v for _, v in inside) < COVERED * total:
        return {}
    ids = [where[s] for s, _ in inside]
    m = np.array(cov["cov"], float)[np.ix_(ids, ids)]
    w = np.array([v for _, v in inside])
    w = w / w.sum()
    sw = m @ w
    var = float(w @ sw)
    if not math.isfinite(var) or var <= 0 or var > MAX_BOOK_VOL**2:
        return {}
    pct = Z_95 * math.sqrt(var * MONTH_DAYS / 252)  # zero drift: a month's mean is noise
    return {"shares": {s: float(w[i] * sw[i] / var) for i, (s, _) in enumerate(inside)},
            "bad_month": {"value": round(pct * total, 2), "pct": round(pct * 100, 2)}}  # fmt: skip


# --------------------------------------------------------------------- store


def _path():
    return paths.state_file("long_run")


def _sane(body) -> dict:
    """Only what has the right shape: a cache, so anything else reads as missing and
    is asked again, and no reader has to check."""
    out = {"v": VERSION, "symbols": {}, "cov": {}}
    if isinstance(body, dict) and body.get("v") == VERSION:
        for key in ("symbols", "cov"):
            if isinstance(body.get(key), dict):
                out[key] = {k: v for k, v in body[key].items() if isinstance(v, dict)}
    return out


def load() -> dict:
    """The kept results. A cache: anything unreadable reads as empty."""
    with _LOCK:
        if _MEMO["v"] is None:
            try:
                _MEMO["v"] = _sane(json.loads(_path().read_text(encoding="utf-8")))
            except (OSError, ValueError):
                _MEMO["v"] = _sane(None)
        return _MEMO["v"]


def _save(body: dict) -> None:
    with _LOCK:
        _atomic_write(_path(), json.dumps(body, separators=(",", ":")) + "\n")
        _MEMO["v"] = body


def _update(*, symbols: dict | None = None, cov: dict | None = None) -> None:
    """Merge into what is kept now, as one step: a superseded pass still finishing
    must not write back the copy it started from."""
    with _LOCK:
        body = load()
        _save({**body, "symbols": {**body["symbols"], **(symbols or {})},
               "cov": {**body["cov"], **(cov or {})}})  # fmt: skip


def reset_memo() -> None:
    with _LOCK:
        _MEMO["v"] = None


def recovery(symbol: str) -> dict | None:
    """What the hover card shows for a holding, from the kept record."""
    r = (load().get("symbols") or {}).get(symbol)
    if not r or r.get("used", 0) < MIN_DAYS:
        return None  # the ten years' figures stand in
    return {"fit": r.get("fit"), "dips": r.get("dips"), "falls": len(r.get("days") or []),
            "since": r.get("since"),
            "open_days": r.get("open_days"), "open_depth": r.get("open_depth"),
            "open_now": r.get("open_now"), "asof": r.get("asof"),
            "from_here": from_here(r.get("open_now"), r.get("open_days"), r.get("open_depth")),
            **({"proxy": r["proxy"]} if r.get("proxy") else {})}  # fmt: skip


def _month(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


def _record(
    pts: list[tuple[str, float]], month: str, proxy: str | None, own_from: str | None = None
) -> dict:
    """What is kept for a holding, from its cleaned closes. ``n`` is the whole answer's
    length (a later, shorter answer is degraded), ``used`` what the gate left of it.
    ``own_from`` is where a stand-in hands over to the holding's own line: a thin young
    line is the gate's business, not the stand-in's clean years, so a bad sign after it
    leaves the older stretch to be read on its own."""
    ser = pd.Series([v for _, v in pts], index=pd.to_datetime([d for d, _ in pts])).sort_index()
    if len(ser) > 1:
        ser = pd.Series(repair_splits(ser.index, ser.to_numpy()), index=ser.index)
    bad = usable_start(ser.index, ser.to_numpy()) if len(ser) > 1 else None
    older = ser.iloc[:0]
    if bad is not None:
        if own_from and bad >= pd.Timestamp(own_from):
            older = ser[ser.index < pd.Timestamp(own_from)]
            if (early := usable_start(older.index, older.to_numpy(), ser.index[-1]) if len(older) > 1 else None) is not None:  # fmt: skip
                older = older[older.index > early]
        ser = ser[ser.index > bad]
    used = len(older) + len(ser)
    base = {"month": month, "asof": pts[-1][0][:10], "n": len(pts), "used": used}
    if used < MIN_DAYS:
        return base
    eps = episodes(ser.to_numpy()) if len(ser) > 1 else {**episodes(older.to_numpy()), "open_days": None, "open_depth": None, "open_now": None}  # fmt: skip
    dips, running = (
        dip_days(ser.to_numpy()) if len(ser) > 1 else (dip_days(older.to_numpy())[0], None)
    )
    if len(older) > 1 and len(ser) > 1:
        # Finished ones only: what the stretch's end cut short has no answer.
        past = episodes(older.to_numpy())
        eps = {**eps, "days": past["days"] + eps["days"], "depths": past["depths"] + eps["depths"],
               "vols": past["vols"] + eps["vols"], "vol_now": eps["vol_now"] or past["vol_now"]}  # fmt: skip
        dips = dip_days(older.to_numpy())[0] + dips
    vols = eps.pop("vols")
    first = older if len(older) else ser
    return {**base, "since": str(first.index[0])[:10], **eps,
            "fit": recovery_fit(eps["days"], eps["open_days"], vols, eps["vol_now"]),
            "dips": dip_histogram(dips, running, eps["days"]),
            **({"proxy": proxy} if proxy else {})}  # fmt: skip


def due(
    held: list[str], ccy: str, *, month: str | None = None, now: float | None = None
) -> tuple[list[str], bool]:
    """(symbols with no record or an earlier month's, whether the covariance is
    due). Symbols Yahoo just failed to answer for wait ``RETRY_S``."""
    month, now = month or _month(), time.time() if now is None else now
    body, tried = load(), _STATE["tried"]
    syms = body.get("symbols") or {}
    todo = [s for s in held if (syms.get(s) or {}).get("month") != month and now >= tried.get(s, 0)]
    cov = (body.get("cov") or {}).get(ccy) or {}
    cov_due = bool(held) and (
        cov.get("month") != month or not set(held) <= set(cov.get("asked") or ())
    )
    return todo, cov_due and now >= tried.get(f"cov|{ccy}", 0)


# ---------------------------------------------------------------------- pass


def _held_now() -> tuple[list[dict], str | None]:
    from convexity import investments

    last = investments._LAST_PAGE["v"][1] or investments.cached_page(None)
    if not last or last.get("empty"):
        return [], None
    return [p for p in last.get("positions") or [] if not p.get("pending")], last.get("ccy")


def _clean(ser: pd.Series) -> list[tuple[str, float]]:
    """A column of closes without Yahoo's single-day glitches, as (date, close)."""
    from convexity import investments as inv

    ser = ser.dropna()
    return inv._xray_clean(
        [(str(d), float(v)) for d, v in ser.items() if v > 0 and math.isfinite(v)]
    )


def _fill_symbols(todo, held, cancel, month) -> bool:
    """Fetch and record ``todo``, a few at a time. False when the pass gave up."""
    from convexity import investments as inv
    from convexity.analytics import extend_history

    qccy = {p["symbol"]: p.get("quote_ccy") or "USD" for p in held}
    names = {p["symbol"]: p.get("name") for p in held}
    need = (datetime.now(UTC) - timedelta(days=365 * SHORT_YEARS)).strftime("%Y-%m-%d")

    def fetch(syms, period, retry=False):
        # A refresh has taken over: no more requests, however deep in a stand-in search.
        if cancel.is_set():
            return pd.DataFrame()
        return inv._total_closes(syms, period, retry=False)

    failed = 0
    for i in range(0, len(todo), CHUNK):
        if cancel.is_set():
            return True
        if i and cancel.wait(PAUSE_S):
            return True
        chunk = todo[i : i + CHUNK]
        got, proxied = None, {}
        try:
            got = fetch(chunk, "max")
            got, proxied = extend_history(got, chunk, qccy, names, "max", fetch=fetch,
                                          need=dict.fromkeys(chunk, need))  # fmt: skip
        except Exception as exc:
            print(f"[long_run] fetch failed: {type(exc).__name__}")
        if cancel.is_set():
            return True
        cols = getattr(got, "columns", ())
        kept, fresh = load()["symbols"], {}
        for s in chunk:
            if s not in cols:
                _STATE["tried"][s] = time.time() + RETRY_S
                continue
            pts = _clean(got[s])
            if len(pts) < MIN_DAYS or len(pts) < KEEP_FRACTION * (
                (kept.get(s) or {}).get("n") or 0
            ):
                # Too young to read yet, or a shorter answer than the one kept.
                _STATE["tried"][s] = time.time() + RETRY_S
                continue
            fresh[s] = _record(pts, month, *(proxied.get(s) or (None, None)))
        _update(symbols=fresh)
        failed = failed + 1 if not any(s in cols for s in chunk) else 0
        if failed >= MAX_FAILED_CHUNKS:
            return False
    return True


def _fill_cov(held, ccy, cancel, month) -> None:
    """The covariance, in the display currency, from one batched request."""
    from convexity import investments as inv
    from convexity.fx import _apply_fx_to_closes, _fx_usd_series
    from convexity.helpers import major_ccy

    syms = [p["symbol"] for p in held]
    qccy = {p["symbol"]: p.get("quote_ccy") or "USD" for p in held}
    closes = inv._total_closes(syms, COV_SPAN, retry=False)
    if cancel.is_set():
        return
    if closes is None or getattr(closes, "empty", True):
        raise ValueError("no closes")
    # A single bad day would otherwise stand in the matrix for a month.
    closes = pd.DataFrame(
        {
            s: pd.Series({pd.Timestamp(d[:10]): v for d, v in _clean(closes[s])})
            for s in closes.columns
        }
    ).sort_index()
    # A rate Yahoo didn't send leaves a line unconverted: no figure is better.
    for c in {major_ccy(c) for c in qccy.values()} | {ccy}:
        ser = _fx_usd_series(c, COV_SPAN) if c != "USD" else True
        if ser is None or getattr(ser, "empty", False):
            raise ValueError(f"no {c} rates")
    closes = _apply_fx_to_closes(
        closes, {s: {"currency": c} for s, c in qccy.items()}, ccy, COV_SPAN
    )
    res = weekly_cov(closes.sort_index())
    if res is None:
        raise ValueError("no covariance")
    names, m = res
    value = {p["symbol"]: float(p.get("value") or 0) for p in held}
    if sum(value[s] for s in names) < COVERED * sum(value.values()):
        raise ValueError("too little of the book answered")  # degraded: keep the last one
    _update(cov={ccy: {"month": month, "symbols": names, "asked": syms,
                        "cov": [[float(f"{x:.5g}") for x in row] for row in m]}})  # fmt: skip


def run_pass(cancel) -> str:
    """One background pass over what is due. Called only from the job."""
    held, ccy = _held_now()
    if not held or not ccy:
        return "nothing"
    month = _month()
    todo, cov_due = due([p["symbol"] for p in held], ccy, month=month)
    if todo and not _fill_symbols(todo, held, cancel, month):
        _STATE["next"] = time.time() + PASS_RETRY_S
        print("[long_run] Yahoo isn't answering; trying again later")
        return "gave up"
    if cancel.is_set():
        return "cancelled"
    if cov_due:
        try:
            _fill_cov(held, ccy, cancel, month)
        except Exception as exc:
            _STATE["tried"][f"cov|{ccy}"] = time.time() + RETRY_S
            print(f"[long_run] covariance skipped: {exc}")
    return "done"


# ----------------------------------------------------------------- scheduler


def tick(now: float | None = None, *, submit=None):
    """Start a pass if something is due and nothing else is running ("after all
    the other things finish"); a refresh arriving later supersedes it."""
    from convexity import jobs

    now = time.time() if now is None else now
    if now < _STATE["next"]:
        return None
    running = jobs.current()
    if running is not None and not running.is_terminal():
        return None
    held, ccy = _held_now()
    if not held or not ccy:
        return None
    todo, cov_due = due([p["symbol"] for p in held], ccy, now=now)
    if not todo and not cov_due:
        return None
    return (submit or _submit)()


def _submit():
    from convexity import jobs

    return jobs.submit(scope="current", phases=["longrun"], days=7,
                       entries_by_view={jobs.BOOK_VIEW: ""}, context={"background": True})  # fmt: skip


def _loop() -> None:
    stop = _STATE["stop"]
    if stop.wait(FIRST_TICK_S):
        return
    while True:
        try:
            tick()
        except Exception as exc:  # the scheduler outlives any one bad tick
            print(f"[long_run] tick failed: {type(exc).__name__}: {exc}")
        if stop.wait(TICK_S):
            return


def start() -> None:
    """Start the scheduler thread once (server.start_server, both modes)."""
    if os.environ.get("CONVEXITY_LONGRUN") == "0":  # the test suite's servers
        return
    t = _STATE["thread"]
    if t is not None and t.is_alive():
        return
    _STATE["stop"].clear()
    _STATE["thread"] = threading.Thread(target=_loop, name="long-run", daemon=True)
    _STATE["thread"].start()


def stop() -> None:
    _STATE["stop"].set()
