"""Past events and the Market Reaction (P6): earnings, dividends and the very
large moves of the held companies, how the market took each one, and why.

What the owner asked for (Feature 1, 2026-11-18): below Upcoming, the recent
events with their cause; a full-screen Events view with a timeline whose
markers are coloured by the event's own outcome and stand as tall as the
market's reaction. The methodology is the quant's (P6 METHOD.md), with one
owner-approved change (the broad index, below). Plain code decides every
number and every label; the model writes one clause, the "why", and only
from a headline the code fetched.

**What is an event**

- E: a reported earnings release (Yahoo's calendar, ``fetcher.fetch_events``);
  its day 0 is ``react``, the first session that can carry it.
- D: an ex-dividend date. The price drop on an ex-date is the dividend
  itself, so a D has no Market Reaction (a dash); its outcome is
  raised / held / cut / special / first against the company's own history
  (``div_outcome``), or "paid" for a fund.
- M, a very large move: ``|z| >= K_MOVE`` (4) on the company's PRIMARY line
  (``capm.primary_line``), z = the day's log return over the standard
  deviation of the 252 sessions before it (at least 60, floored at 0.2%),
  after bad prints are dropped. Not an earnings reaction session (the move
  colours that E instead), not a broad-market day (those become one market
  band), and one per five sessions (the largest stands for a cluster). The
  held listing is the wrong line to detect on: Vienna and Milan lines barely
  trade, and their stale closes jump when they catch up (METHOD §1).
- A market day: the broad index itself moved ``K_MKT`` (3) of its own sigmas
  the same way. Consecutive ones are one band.

**The Market Reaction** (``reaction``): abnormal log returns ar = r - beta m
against the BROAD index, CAR(0,+1) / (sigma_abn sqrt 2), cut at 1 and 2.5
into Big drop / Fell / Shrugged off / Rose / Big rise, sigma_abn and beta
from sessions -257..-6 (before any run-up, so a leak doesn't shrink the
yardstick). A label shows from the close of day +1 and is then final;
before that the event is "Reacting · day 0". Follow-through (kept going /
held / gave back) is a separate tag after day +5.

**The one exception to "one beta" (owner, 2026-11-18).** Everything else in
the app reads ``capm``'s beta against the HOME index. Here the index is the
broad one: S&P 500 for a US line, STOXX 600 for a European one, the capm
home index for anything else, with an OLS beta over the same window as
sigma_abn, shrunk 1 + 0.9 (b - 1) like capm and floored at 0. An index
heavyweight drags its home index: a stock a fifth of the SMI that falls 11%
pulls the SMI down 1.5% with it while the STOXX 600 barely moves. Against its
home index it would have "explained" part of its own fall.

**Causes are never guessed.** A move's cause is the event itself, or a
headline from Finnhub's company news (the company's US line, the free plan's
only answer) for the session and the day before, or "No headline found" with
a sentence on whether the market explains it. The free plan's company news
reaches about a year back, so older moves say "No headline found" and that
is the truth, not a gap to fill from the model's memory. A results-like
headline turns a move into a results day (Yahoo has no report dates for
some European companies; the headline finds them).

**Where it lives.** Market data shared by every portfolio, keyed by the
primary line, never per book: ``state/events_history.json`` (the numbers,
recomputed by the job) and ``state/event_notes.json`` (the costly part: the
headlines and the model's clause, keyed by an inputs hash). A request reads
both and filters by the showing portfolio's holdings; nothing is fetched on
the page's path. A hidden ``events`` job fills them (``run_pass``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import UTC, date, datetime, timedelta

import numpy as np

from convexity import paths
from convexity.persistence import _atomic_write

# Each file's format: bump when a rule below changes and the file is rebuilt.
# Apart, because the notes are the costly part (Finnhub and NIM calls) and are
# keyed by event and inputs, so a new rule for the numbers needn't redo them.
VERSION = 2  # events_history.json (2: dividends in the company's currency, no fund moves, market-day moves)
NOTES_VERSION = 1  # event_notes.json

# --------------------------------------------------------------- the rules
K_MOVE = 4.0  # |z| a day needs to be a big move (METHOD §1: ~0.5 per company-year)
K_MKT = 3.0  # the broad index's own |z| that makes a market day
SIG_N = 252  # sessions behind a day's sigma (the Attention rail's rule)
SIG_MIN = 60
SIG_FLOOR = 0.002  # investments.XRAY_SIGMA_FLOOR
DECLUSTER = 5  # sessions: one big move per cluster
ABN_FROM, ABN_TO = 257, 6  # sigma_abn and beta from sessions i-257..i-6
ABN_MIN = 60
BETA_SHRINK = 0.9  # capm.SHRINK: 1 + 0.9 (b - 1)
CUTS = (1.0, 2.5)  # |CAR(0,+1)| in two-day sigmas
LABELS = {-2: "Big drop", -1: "Fell", 0: "Shrugged off", 1: "Rose", 2: "Big rise"}
FOLLOW_SESSIONS = 5  # follow-through is read at the close of day +5
KEPT_GOING, GAVE_BACK = 1.5, 0.5  # CAR(0,5) / CAR(0,1)
INLINE_PCT = 2.0  # EPS within +-2% of the estimate is in line
DIV_TOL = 0.01  # a dividend change under 1% is "held"
DIV_TOL_FX = 0.02  # ... under 2% when compared after converting the currency
DIV_YEAR_SLACK = 45  # days: "the same payment a year before" is within this of the anniversary
NOT_COMPARABLE_PCT = 50.0  # beyond this the estimate is on another EPS basis
GLITCH = 1.0  # capm.GLITCH: |log return| above this is a bad print
SPIKE = 0.15  # capm.SPIKE: a move the next bar takes back (net < a quarter)
MARKET_EXPLAINS_Z = 1.5  # the index's own |z| that "explains" a move in the sentence

SHOW_DAYS = 740  # the timeline's 2Y, and a little room
UPCOMING_DAYS = 45
RAIL_DAYS = 30
RAIL_TOP = 5
NEWS_DAYS = 365  # Finnhub's free company news reaches about a year back
NEWS_RECHECK_DAYS = 7  # "No headline found" is asked again daily for a week
MAX_HEADLINES = 6
MIN_RELEVANCE = 0.5

# A US line has no suffix; these suffixes are European (capm.HOME + EUROPE).
_EU_SUFFIXES = {
    "PA", "DE", "F", "BE", "DU", "HM", "MU", "SG", "HA", "MC", "MI", "AS", "BR", "HE", "VI",
    "SW", "ST", "CO", "L", "OL", "IR", "LS", "WA", "PR", "BD", "AT", "IC", "RG", "TL", "VS",
    "MT", "IL",
}  # fmt: skip
BROAD_US = ("^GSPC", "S&P 500")
BROAD_EU = ("^STOXX", "STOXX Europe 600")

# ------------------------------------------------------------- the job
PERIOD = "5y"  # yfinance has no 3y; two years shown + a year of sigma before
NUMBERS_TTL_S = 3 * 3600  # a line's numbers are recomputed after this
CHUNK = 6  # symbols per Yahoo request
PAUSE_S = 2.0  # between Yahoo requests
FH_PAUSE_S = 3.0  # between Finnhub calls (the news refresh shares the 60/min)
NIM_PAUSE_S = 12.0  # between NIM calls: a few a minute
NIM_DEADLINE_S = 90.0  # one call, the limiter's waits included
NIM_BACKOFF_S = 600.0  # after a failed NIM call: 10 min, doubling ...
NIM_BACKOFF_MAX_S = 6 * 3600.0  # ... to 6 h; a success resets it
MAX_NIM_PER_PASS = 40
MAX_FH_PER_PASS = 30
FIRST_TICK_S = 240  # after launch: the page, its warm-ups and the owner go first
TICK_S = 300
PASS_RETRY_S = 1800  # a pass that found Yahoo silent
MAX_FAILED_CHUNKS = 2

_LOCK = threading.RLock()
_MEMO: dict = {"history": None, "notes": None}
_STATE: dict = {"thread": None, "stop": threading.Event(), "next": 0.0, "running": False}
# The model's state for the clauses: stopped on a rejected key (401/403) until
# the keys change (reload_keys), else backing off after failures. A rejected
# key used to cost ~11 Finnhub + ~11 NIM calls every 10 minutes.
_NIM: dict = {"stopped": False, "fails": 0, "until": 0.0}


# ======================================================== pure arithmetic


def broad_index(line: str) -> tuple[str, str]:
    """The broad market a line trades in, for the market adjustment and the
    market-day test: S&P 500 for a US line, STOXX 600 for a European one, the
    capm home index for anything else (Tokyo against the S&P on daily data
    reads a beta of 0.25: different sessions)."""
    s = (line or "").upper()
    suf = s.rsplit(".", 1)[1] if "." in s and not s.startswith("^") else ""
    if not suf:
        return BROAD_US
    if suf in _EU_SUFFIXES:
        return BROAD_EU
    try:
        from convexity import capm

        p = capm.home_proxy(s)
        return p["proxy"], p["name"]
    except Exception:
        return BROAD_US


def clean_bars(dates, o, h, lo, c, v) -> dict | None:
    """One line's daily bars, oldest first, with what can't be a move taken
    out: non-positive closes, then one-bar bad prints (capm's rule: a move
    over ``SPIKE`` that the next bar takes back, net under a quarter of it;
    the bad bar is dropped, so the two returns merge into the small real
    one), then ``GLITCH`` returns (Yahoo's x52 days). Returns arrays, or None
    with fewer than two bars."""
    d = [str(x)[:10] for x in dates]
    arr = {
        k: np.asarray(x, dtype=float)
        for k, x in (("o", o), ("h", h), ("l", lo), ("c", c), ("v", v))
    }
    keep = np.isfinite(arr["c"]) & (arr["c"] > 0)
    d = [x for x, k in zip(d, keep, strict=True) if k]
    arr = {k: x[keep] for k, x in arr.items()}
    for _ in range(50):  # a second bad print next to the first shows after one pass
        if len(d) < 3:
            break
        r = np.diff(np.log(arr["c"]))
        a, b = r[:-1], r[1:]
        bad = (np.abs(a) > SPIKE) & (np.sign(a) == -np.sign(b)) & (np.abs(a + b) < 0.25 * np.abs(a))
        drop = set((np.flatnonzero(bad) + 1).tolist())  # the bar the spike went into
        if not drop:
            # A glitch: only the bar it went into (its return out is the same glitch, reversed).
            g = np.flatnonzero(np.abs(r) > GLITCH)
            drop = {int(g[0]) + 1} if len(g) else set()
        if not drop:
            break
        m = np.array([i not in drop for i in range(len(d))])
        d = [x for x, k in zip(d, m, strict=True) if k]
        arr = {k: x[m] for k, x in arr.items()}
    if len(d) < 2:
        return None
    return {"d": d, **arr}


def align(stock: dict, index: dict | None) -> dict:
    """The stock's bars on the sessions both traded, with log returns of
    each (r, m) from one common session to the next: a day the index didn't
    trade merges into the next, for both. Without an index m is NaN."""
    if index is None:
        idx_c = np.full(len(stock["d"]), np.nan)
        keep = np.ones(len(stock["d"]), dtype=bool)
    else:
        pos = {d: i for i, d in enumerate(index["d"])}
        keep = np.array([d in pos for d in stock["d"]])
        idx_c = np.array([index["c"][pos[d]] for d, k in zip(stock["d"], keep, strict=True) if k])
    d = [x for x, k in zip(stock["d"], keep, strict=True) if k]
    out = {k: stock[k][keep] for k in ("o", "h", "l", "c", "v")}
    r = np.concatenate([[np.nan], np.diff(np.log(out["c"]))])
    m = np.concatenate([[np.nan], np.diff(np.log(idx_c))]) if index is not None else idx_c
    return {"d": d, **out, "r": r, "m": m}


def sigma_before(x: np.ndarray, i: int, n: int = SIG_N, min_n: int = SIG_MIN) -> float | None:
    """The standard deviation of ``x`` over the ``n`` sessions before ``i``
    (at least ``min_n`` finite ones), floored: the day's own move is never in
    its yardstick."""
    w = x[max(i - n, 0) : i]
    w = w[np.isfinite(w)]
    if len(w) < min_n:
        return None
    s = float(np.std(w, ddof=1))
    return max(s, SIG_FLOOR) if math.isfinite(s) else None


def z_scores(x: np.ndarray) -> np.ndarray:
    """Each day's return in sigmas of the year before it (NaN when too
    little history)."""
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        if not np.isfinite(x[i]):
            continue
        s = sigma_before(x, i)
        if s:
            out[i] = x[i] / s
    return out


def shrunk_beta(y: np.ndarray, x: np.ndarray) -> float | None:
    """OLS beta of ``y`` on ``x``, shrunk like capm's (1 + 0.9 (b - 1)) and
    floored at 0: a negative beta on a broad index is noise, and it would turn
    a falling market into a "rise" of the stock."""
    ok = np.isfinite(y) & np.isfinite(x)
    y, x = y[ok], x[ok]
    if len(y) < ABN_MIN:
        return None
    xm = x.mean()
    sxx = float(((x - xm) ** 2).sum())
    if sxx <= 0:
        return None
    b = float(((x - xm) * (y - y.mean())).sum() / sxx)
    return max(0.0, 1.0 + BETA_SHRINK * (b - 1.0))


def category(z: float | None) -> int | None:
    """-2..2 from a two-day z: cuts at 1 and 2.5 (with no news the two-day z
    is about N(0,1), so "Shrugged off" holds 68% of ordinary days and a Big
    one happens about one day in 80)."""
    if z is None or not math.isfinite(z):
        return None
    a = abs(z)
    lvl = 0 if a < CUTS[0] else (1 if a < CUTS[1] else 2)
    return int(math.copysign(lvl, z)) if lvl else 0


def follow_through(c1: float | None, c5: float | None, cat: int | None) -> str | None:
    """What sessions +2..+5 did to the move that set the label: "kept going"
    (half as much again), "gave back" (under half left, or reversed), else
    "held". None for "Shrugged off" or before day +5."""
    if not cat or c1 is None or c5 is None or not c1:
        return None
    kept = c5 / c1
    return "gave back" if kept < GAVE_BACK else ("kept going" if kept > KEPT_GOING else "held")


def _pct(v: float | None) -> float | None:
    return None if v is None or not math.isfinite(v) else round(math.expm1(v) * 100, 2)


def _r2(v):
    return None if v is None or not math.isfinite(v) else round(float(v), 2)


def abn_fit(fr: dict, i: int) -> tuple[float, float] | None:
    """(beta, sigma_abn) for session ``i`` of an aligned frame: the broad
    index's shrunk OLS beta and the abnormal returns' standard deviation over
    sessions i-257..i-6 (before any run-up). None with too little history.
    The Market Reaction and the market-day test of a big move share it."""
    r, m = fr["r"], fr["m"]
    lo, hi = max(i - ABN_FROM, 1), max(i - ABN_TO + 1, 1)
    beta = shrunk_beta(r[lo:hi], m[lo:hi])
    if beta is None:
        return None
    ar = r[lo:hi] - beta * np.nan_to_num(m[lo:hi], nan=0.0)
    ar = ar[np.isfinite(ar)]
    if len(ar) < ABN_MIN:
        return None
    return beta, max(float(np.std(ar, ddof=1)), SIG_FLOOR)


def reaction(fr: dict, i: int, fund: bool) -> dict | None:
    """The Market Reaction of session ``i`` of an aligned frame (``align``).
    Abnormal returns against the broad index with its own shrunk beta (a
    fund: raw returns, it IS the market), sigma and beta from sessions
    i-257..i-6. None with too little history before. ``sessions_after`` 0 is
    day 0 only: no label yet."""
    r, m = fr["r"], fr["m"]
    lo, hi = max(i - ABN_FROM, 1), max(i - ABN_TO + 1, 1)
    beta = None
    if not fund:
        beta = shrunk_beta(r[lo:hi], m[lo:hi])
        if beta is None:
            return None
        ar = r - beta * np.nan_to_num(m, nan=0.0)
    else:
        ar = r
    hist = ar[lo:hi]
    hist = hist[np.isfinite(hist)]
    raw = r[lo:hi]
    raw = raw[np.isfinite(raw)]
    if len(hist) < ABN_MIN:
        return None
    sig = max(float(np.std(hist, ddof=1)), SIG_FLOOR)
    sig_raw = max(float(np.std(raw, ddof=1)), SIG_FLOOR)
    n = len(r)
    n_after = n - 1 - i

    def car(x, a, b):
        return float(np.nansum(x[i + a : i + b + 1])) if i + b < n else None

    c0, c1, c5 = car(ar, 0, 0), car(ar, 0, 1), car(ar, 0, 5)
    z0 = c0 / sig
    z1 = c1 / (sig * math.sqrt(2)) if c1 is not None else None
    z5 = c5 / (sig * math.sqrt(6)) if c5 is not None else None
    pre = float(np.nansum(ar[i - 5 : i])) / (sig * math.sqrt(5)) if i >= 6 else None
    cat = category(z1) if z1 is not None else None
    follow = follow_through(c1, c5, cat) if n_after >= FOLLOW_SESSIONS else None
    prev_c = fr["c"][i - 1] if i >= 1 else None
    gap = math.log(fr["o"][i] / prev_c) if prev_c and fr["o"][i] > 0 else None
    v20 = fr["v"][max(i - 20, 0) : i]
    v20 = float(np.nanmean(v20)) if len(v20) else 0.0
    rng = (fr["h"] - fr["l"]) / np.concatenate([[np.nan], fr["c"][:-1]])
    r20 = rng[max(i - 20, 1) : i]
    r20 = float(np.nanmean(r20)) if len(r20) and np.isfinite(r20).any() else 0.0
    return {
        "category": cat,
        "label": LABELS.get(cat) if cat is not None else None,
        "z": _r2(z1 if z1 is not None else z0),
        "z_day": _r2(z0),
        "car_1": _r2(z1),
        "car_5": _r2(z5),
        "pre_5": _r2(pre),
        "gap_z": _r2(gap / sig_raw) if gap is not None else None,
        "vol_ratio": _r2(fr["v"][i] / v20) if v20 > 0 else None,
        "range_ratio": _r2(rng[i] / r20) if r20 > 0 and math.isfinite(rng[i]) else None,
        "beta": _r2(beta),
        "abnormal": not fund,
        "sigma_pct": _r2(math.expm1(sig) * 100),
        "day_pct": _pct(r[i]),
        "pct_0_1": _pct(car(r, 0, 1)),
        "pct_0_5": _pct(car(r, 0, 5)),
        "abn_pct_0_1": _pct(c1) if not fund else None,
        "index_pct": _pct(m[i]) if math.isfinite(m[i]) else None,
        "follow": follow,
        "provisional": n_after < FOLLOW_SESSIONS,
        "sessions_after": int(min(n_after, FOLLOW_SESSIONS)),
    }


def eps_outcome(e: dict) -> tuple[str | None, str | None]:
    """("beat" | "in line" | "miss" | None, a note). In line within +-2%;
    beyond +-50% the estimate is on another EPS basis (a European company's
    Yahoo estimate often is: a French bank "beat" by 11-32% eight quarters
    running), so
    the figures show without a word."""
    if e.get("actual") is None:
        return None, "no reported EPS yet"
    s = e.get("surprise_pct")
    if s is None or not math.isfinite(s):
        return None, "no estimate"
    if abs(s) > NOT_COMPARABLE_PCT:
        return None, "estimate not comparable"
    if s > INLINE_PCT:
        return "beat", None
    if s < -INLINE_PCT:
        return "miss", None
    return "in line", None


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def _dir(ch: float, tol: float) -> int:
    return 1 if ch > tol else (-1 if ch < -tol else 0)


def div_outcome(
    divs: list[dict], k: int, tol: float = DIV_TOL
) -> tuple[str | None, float | None, str]:
    """(raised | held | cut | special | first, the change, its basis) for
    dividend ``k`` of a company's history (oldest first). Each payment is
    compared as ``cmp`` when the job set it (the amount in the company's
    reporting currency: Yahoo converts a USD dividend to pence on London, so
    an unchanged one read "raised 4%" and "cut 5%" with the pound), else as
    ``amount``; a ``cmp`` of None (no FX that day) reads "held", never a guess.

    A quarterly (or more often) payer is "raised" or "cut" only when the
    change against its last payment AND against the same payment a year
    before agree (the last alone when there was none a year before): a
    company whose fourth payment is the big one read "raised 12.5%" then "cut
    5.6%" against the last one alone. The same as the last one is "held";
    the two pointing opposite ways is "held", basis "mixed".
    A yearly or half-yearly payer is compared by the 12 months ending with
    this payment against the 12 months before (the year-ago comparison:
    an interim and a final aren't comparable payments), windows cut 45 days
    off the anniversary so a drifting ex-date isn't counted twice (two French
    banks' new interims in 2025 read as a 46% cut of the next final
    otherwise). Special: a payment far off the schedule (within a third of
    the usual gap). Yahoo tags neither specials nor their absence; the
    timing is all there is. ``tol`` is the change that still reads "held"."""
    d0 = _d(divs[k]["date"])
    prev = [x for x in divs[:k] if _d(x["date"]) > d0 - timedelta(days=760)]
    if not prev:
        return "first", None, "first"
    yr = [x for x in divs[:k] if _d(x["date"]) > d0 - timedelta(days=405)]
    freq = max(len(yr), 1)
    gap = (d0 - _d(divs[k - 1]["date"])).days
    if gap < (365 / freq) / 3 and len(prev) >= 2:
        return "special", None, "timing"

    def amt(x: dict):
        return x["cmp"] if "cmp" in x else x["amount"]

    if freq >= 4:
        cur, last = amt(divs[k]), amt(divs[k - 1])
        anniv = d0 - timedelta(days=365)
        near = [x for x in divs[: k - 1] if abs((_d(x["date"]) - anniv).days) <= DIV_YEAR_SLACK]
        ago = min(near, key=lambda x: abs((_d(x["date"]) - anniv).days)) if near else None
        vals = [cur, last] + ([amt(ago)] if ago else [])
        if any(v is None for v in vals):
            return "held", None, "no_fx"
        if not last:
            return None, None, "last"
        ch = cur / last - 1
        dl = _dir(ch, tol)
        dy = _dir(cur / amt(ago) - 1, tol) if ago and amt(ago) else dl
        if dl == 0:
            return "held", ch, "last"  # the same as the last one, whatever the year before
        if dl != dy:
            return "held", None, "mixed"  # e.g. a big fourth payment, then the usual one
        return {1: "raised", -1: "cut"}[dl], ch, "last"
    a = d0 - timedelta(days=320)
    win = [x for x in divs[: k + 1] if a - timedelta(days=365) < _d(x["date"]) <= d0]
    if any(amt(x) is None for x in win):
        return "held", None, "no_fx"
    ref = sum(amt(x) for x in win if _d(x["date"]) <= a)
    cur = sum(amt(x) for x in win if _d(x["date"]) > a)
    if not ref:
        return "first", None, "first"
    ch = cur / ref - 1
    return {1: "raised", -1: "cut", 0: "held"}[_dir(ch, tol)], ch, "year"


def decluster(cands: list[tuple[int, float]], gap: int = DECLUSTER) -> list[tuple[int, float, int]]:
    """One event per cluster of (session, z): sessions within ``gap`` of the
    previous one, moving the same way, join it; the largest |z| stands for the
    cluster, with the cluster's size. A move the other way starts a cluster of
    its own: a chip maker's -14% day folded into its +31.5% day three sessions
    later and vanished from the timeline (2026-11-23)."""
    out, cur = [], []
    for c in sorted(cands):
        if cur and (c[0] - cur[-1][0] > gap or (c[1] > 0) != (cur[-1][1] > 0)):
            best = max(cur, key=lambda t: abs(t[1]))
            out.append((best[0], best[1], len(cur)))
            cur = []
        cur.append(c)
    if cur:
        best = max(cur, key=lambda t: abs(t[1]))
        out.append((best[0], best[1], len(cur)))
    return out


def abn_z(fr: dict, i: int) -> float | None:
    """Session ``i``'s abnormal return against the broad index in its own
    sigmas (the reaction's beta and sigma convention), None without history."""
    fit = abn_fit(fr, i)
    if fit is None or not np.isfinite(fr["r"][i]):
        return None
    m = fr["m"][i] if np.isfinite(fr["m"][i]) else 0.0
    return (fr["r"][i] - fit[0] * m) / fit[1]


def big_moves(fr: dict, index_z: dict[str, float], earn_sessions: list[int], since: str
              ) -> tuple[list[dict], list[dict]]:  # fmt: skip
    """(kept moves, market-day drops) on an aligned frame from ``since``:
    ``|z| >= K_MOVE`` raw, not on an earnings reaction session or the one
    after it, not on a day the broad index moved ``K_MKT`` its own sigmas
    the same way; declustered. Raw, not abnormal: the shrunk beta is fitted
    in calm years, so on a crash a defensive name that fell less than the
    market reads as a big abnormal RISE (a utility read +4.5 sigma on 3 Apr
    2025, up 2.6% while the CAC fell 3.3%).

    A market day keeps a move that is huge on its own as well: ``|abn_z| >=
    K_MOVE`` the same way as the raw z (the check found a chip maker's +31.5%,
    z 10.4, hidden in the band of a +4.7% S&P day). The defensive name above
    stays out: its raw z is small, or its abnormal z points the other way."""
    z = z_scores(fr["r"])
    kept, drops = [], []
    for i, d in enumerate(fr["d"]):
        if d < since or not np.isfinite(z[i]) or abs(z[i]) < K_MOVE:
            continue
        if any(0 <= i - p <= 1 for p in earn_sessions):
            continue
        mz = index_z.get(d)
        if mz is not None and abs(mz) >= K_MKT and np.sign(mz) == np.sign(z[i]):
            az = abn_z(fr, i)
            if az is None or abs(az) < K_MOVE or np.sign(az) != np.sign(z[i]):
                drops.append({"date": d, "pct": _pct(fr["r"][i]), "z": _r2(z[i])})
                continue
        kept.append((i, float(z[i])))
    out = []
    for i, zz, n in decluster(kept):
        mz = index_z.get(fr["d"][i])
        out.append({"i": i, "date": fr["d"][i], "pct": _pct(fr["r"][i]), "z": _r2(zz),
                    "market_pct": _pct(fr["m"][i]) if math.isfinite(fr["m"][i]) else None,
                    "market_z": _r2(mz) if mz is not None else None, "cluster_days": n})  # fmt: skip
    return out, drops


def merge_bands(days: list[dict]) -> list[dict]:
    """Market days (``{date, index, index_pct, holdings}``) as bands:
    consecutive sessions (one weekday apart, a weekend between is fine) that
    went the SAME way are one episode, labelled by its biggest index move. A
    fall and the rebound the next session are two bands (20 Nov -3.7% and 23
    Nov +4.7% read as one "S&P +4.7%" band before)."""
    out: list[dict] = []
    for m in sorted(days, key=lambda x: x["date"]):
        last = out[-1] if out else None
        prev = last["days"][-1] if last else None
        same = prev is not None and (m.get("index_pct") or 0) * (prev.get("index_pct") or 0) > 0
        if same and int(np.busday_count(last["end"], m["date"])) <= 1:
            last["end"] = m["date"]
            last["days"].append(m)
        else:
            out.append({"start": m["date"], "end": m["date"], "days": [m]})
    for b in out:
        top = max(b["days"], key=lambda x: abs(x.get("index_pct") or 0))
        b["index"], b["index_name"], b["index_pct"] = (
            top.get("index"),
            top.get("index_name"),
            top.get("index_pct"),
        )
        b["holdings"] = sorted({h for x in b["days"] for h in x.get("holdings", [])})
    return out


# ---------------------------------------------------------- the sentences


def _p1(v: float) -> str:
    return f"{abs(v):.1f}%"


def size_sentence(ev: dict, name: str | None = None) -> str:
    """The reaction in words, written by code, the same way every time (the
    model never writes a number the reader acts on: METHOD §4 found it read
    a 4-sigma first day as the label, mixed sigma and %, and once named a
    cause from a headline that said the opposite)."""
    r = ev.get("reaction")
    who = name or ev.get("name") or "It"
    if ev.get("upcoming") or ev.get("type") == "D":
        return ""
    if ev.get("reacting") or not r:
        if ev.get("type") == "E" and ev.get("timing") == "after":
            return (f"{who} reported after the close; the first full session hasn't closed yet, "
                    "so the reaction reads from its close.")  # fmt: skip
        return "The first session after it hasn't closed yet, so the reaction reads from its close."
    if r.get("category") is None:
        return (f"Day 0 only: {abs(r.get('z_day') or 0):.1f}× a normal day so far. "
                "The label is set when day +1 closes.")  # fmt: skip
    two = r.get("pct_0_1")
    if two is None:
        two = r.get("day_pct") or 0.0
    vs = r.get("abn_pct_0_1")
    vs_t = "" if vs is None else f", {_p1(vs)} {'better' if vs >= 0 else 'worse'} than the market"
    scale = ("within a normal two-day move" if r["category"] == 0
             else f"{abs(r.get('z') or 0):.1f}× a normal two-day move")  # fmt: skip
    f = r.get("follow")
    fol = {"gave back": " By day five it had given most of it back.",
           "kept going": " By day five it had kept going.", "held": " By day five it had held."}.get(f, "")  # fmt: skip
    return f"Shares {'rose' if two >= 0 else 'fell'} {_p1(two)} over two days{vs_t}: {scale}.{fol}"


def market_sentence(move: dict | None, index_name: str | None) -> str:
    """Whether the market explains a move, from the broad index's own z that
    day: never a cause, only how much of the move was everyone's."""
    if not move or move.get("market_z") is None:
        return ""
    mz, mp = move["market_z"], move.get("market_pct")
    if abs(mz) < MARKET_EXPLAINS_Z:
        return "The market does not explain it"
    same = (mz > 0) == ((move.get("pct") or 0) > 0)
    tail = (
        f" ({index_name} {'+' if (mp or 0) >= 0 else '−'}{abs(mp or 0):.1f}%)"
        if index_name and mp is not None
        else ""
    )
    return f"The market moved {'the same way' if same else 'the other way'}{tail}"


# ---------------------------------------------------- headlines and the model

_RESULTS_RE = re.compile(
    r"\b(results?|earnings|quarter(ly)?|q[1-4]|half[- ]year|interim|full[- ]year|"
    r"profit warning|trading update|first[- ]half|annual report)\b",
    re.IGNORECASE,
)
# Case-aware on purpose: "may" the verb is lower case mid-clause, while "May"
# the month is capitalised ("results for the quarter to May" was refused).
_HEDGE_RE = re.compile(
    r"\b(?:[Ll]ikely|may|[Mm]ight|[Pp]robably|[Pp]ossibly|[Pp]erhaps|[Cc]ould|[Ss]eems?|[Aa]ppears?|"
    r"[Pp]resumably|[Ss]uggests?)\b"
)
_HEADLINE_REF = re.compile(r"\bheadlines?\s*#?\d+|\(\s*\d+\s*\)|\bitem\s*\d+", re.IGNORECASE)
_UP_WORDS = re.compile(
    r"\b(rose|rises?|rising|jump(s|ed|ing)?|surg(e|es|ed|ing)|soar(s|ed|ing)?|rall(y|ied|ies|ying)|"
    r"gain(s|ed|ing)?|climb(s|ed|ing)?|rebound(s|ed|ing)?)\b",
    re.IGNORECASE,
)
_DOWN_WORDS = re.compile(
    r"\b(fell|falls?|falling|drop(s|ped|ping)?|plung(e|es|ed|ing)|tumbl(e|es|ed|ing)|slump(s|ed|ing)?|"
    r"sank|sinks?|sinking|slid(e|es|ing)?|declin(e|es|ed|ing)|los(t|es|ing)|crash(es|ed|ing)?)\b",
    re.IGNORECASE,
)
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
_STOP = {"the", "and", "for", "with", "from", "that", "this", "its", "after", "over", "into", "stock",
         "shares", "share", "says", "said", "news", "about", "will", "have", "has", "are", "was",
         "were", "than", "more", "less", "inc", "corp", "plc", "company"}  # fmt: skip


def results_like(headline: str | None) -> bool:
    """A headline that reads like a results release (Q2, half-year, earnings
    call): the move was the company's results day."""
    return bool(headline and _RESULTS_RE.search(headline))


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z'-]{3,}", (text or "").lower()) if w not in _STOP}


def _nums(text: str) -> set[str]:
    return {n.replace(",", ".").rstrip("0").rstrip(".") or "0" for n in _NUM_RE.findall(text or "")}


def restates(why: str | None, headline: str | None) -> bool:
    """A clause that only repeats its headline (the 30B model often copies
    it: "The news: ZZ Corp H1 Adj. EPS $0.95 Misses..."): the reader sees the
    headline itself, so the clause adds nothing and is left out."""
    if not why or not headline:
        return False
    w, h = _words(why), _words(headline)
    return bool(h) and len(w & h) / max(len(h), 1) >= 0.7 and len(w - h) <= max(4, len(h) // 2)


def check_why(out, heads: list[dict], direction: int) -> tuple[dict | None, str]:
    """The model's answer, checked by plain code; (clean answer, "") or
    (None, the reason). The pick must exist (0 = none of them explains it);
    the clause must use the picked headline's words, carry no hedge
    ("likely": METHOD §4's one guessed cause said it), no number that isn't
    in the picked headline (its title or lead: the code's own size sentence
    is in the input too, and a clause restating "fell 10.9%" isn't the
    headline's fact), no "Headline N" (the reader can't see the numbers), no
    price verb that contradicts the move (``direction`` -1/0/+1), and a
    headline whose own tone isn't against the move."""
    if not isinstance(out, dict):
        return None, "no answer"
    pick, why, tone = out.get("headline"), out.get("why"), out.get("tone")
    if not isinstance(pick, int) or isinstance(pick, bool) or not 0 <= pick <= len(heads):
        return None, "bad pick"
    if not isinstance(tone, int) or isinstance(tone, bool) or not -2 <= tone <= 2:
        tone = 0
    if pick == 0:
        return {"headline": 0, "why": None, "tone": None}, ""
    if not isinstance(why, str) or not why.strip():
        return None, "empty"
    why = " ".join(why.split()).strip()
    if len(why) > 240:
        return None, "too long"
    if _HEDGE_RE.search(why):
        return None, "hedged"
    if _HEADLINE_REF.search(why):
        return None, "names a headline number"
    h = heads[pick - 1]
    src = _words(f"{h.get('headline', '')} {h.get('summary', '')}")
    used = _words(why) & src
    need = min(2, len(_words(h.get("headline", ""))))
    if len(used) < max(need, 1):
        return None, "not from the headline"
    have = _nums(f"{h.get('headline', '')} {(h.get('summary') or '')[:200]}")  # what the model saw
    if any(n not in have for n in _nums(why)):
        return None, "a number not in the headline"
    if direction < 0 and _UP_WORDS.search(why):
        return None, "direction"
    if direction > 0 and _DOWN_WORDS.search(why):
        return None, "direction"
    if direction and tone * direction < 0:
        # Good news can't explain a fall, nor bad news a rise (a drug maker fell 16%
        # on a trial miss; the model once picked its upbeat sales headline).
        return None, "tone against the move"
    if not why.endswith((".", "!", "?")):
        why += "."
    return {"headline": pick, "why": why[0].upper() + why[1:], "tone": tone}, ""


WHY_SYSTEM = """You explain in one short sentence why a company's shares moved around one event, for a private investor.

You get the event, how the price moved (already measured; do not restate the numbers), a few days of prices around it, and numbered news headlines from the event day and the day before.

Pick the ONE headline that explains the move, by its number, or 0 when none of them does (a headline about other companies, a market round-up, or one that doesn't match the move's direction is not an explanation).

why: one plain sentence, at most 25 words, saying what that headline reports, in its own words. Start with "The news:" or with the company's name. No hedging (never likely, may, might, probably, could). No numbers unless they are in the headline. Never mention a headline's number. Never add facts that are not in the headline.

tone: what the picked headline says about the company's business, -2 (very bad) to +2 (very good); 0 when you pick none."""


def why_schema(n: int) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["headline", "why", "tone"],
        "properties": {
            "headline": {"type": "integer", "minimum": 0, "maximum": n},
            "why": {"type": "string", "maxLength": 220},
            "tone": {"type": "integer", "minimum": -2, "maximum": 2},
        },
    }


def why_input(ev: dict, name: str, heads: list[dict], window: list[str]) -> str:
    """The model's input: the event, the code's own sentence, a few sessions
    of prices (the owner asked for the OHLCV around the event; the window is
    context, the label is already set) and the numbered headlines."""
    kind = {"E": "an earnings report", "M": "a very large one-day move"}.get(ev["type"], "an event")
    lines = [f"Company: {name} ({ev.get('line')}).",
             f"Event: {kind} on {ev.get('session') or ev.get('date')}.",
             f"Measured by code: {size_sentence(ev, name)}"]  # fmt: skip
    rx = ev.get("reaction") or {}
    if rx.get("label"):
        lines.append(f"Reaction label (fixed): {rx['label']}.")
    if window:
        lines.append("Prices (>> marks the event session):\n" + "\n".join(window))
    lines.append("Headlines:\n" + "\n".join(
        f"{k}. {h['headline']} ({h.get('source') or 'news'})" + (f" — {h['summary'][:200]}" if h.get("summary") else "")
        for k, h in enumerate(heads, 1)))  # fmt: skip
    return "\n".join(lines)


def inputs_hash(ev: dict, heads: list[dict], stage: str) -> str:
    """What a note was written from: the stage (day +1, then final), the
    label, the follow-through, the two-day move to 0.1% and the headlines. A
    restated close or a new headline writes it again; nothing else does."""
    rx = ev.get("reaction") or {}
    key = [stage, rx.get("category"), rx.get("follow"),
           None if rx.get("pct_0_1") is None else round(rx["pct_0_1"], 1),
           [h.get("headline") for h in heads]]  # fmt: skip
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def note_stage(ev: dict) -> str | None:
    """When a note may be written: never on day 0 (no label yet), at day +1
    (``d1``), and once more, final, after day +5."""
    rx = ev.get("reaction") or {}
    if ev.get("upcoming") or rx.get("category") is None:
        return None
    return "final" if (rx.get("sessions_after") or 0) >= FOLLOW_SESSIONS else "d1"


def safe_url(u) -> str | None:
    """A headline's link, opened in the owner's browser: plain https, no
    credentials, a sane length. Anything else is dropped (the headline stays)."""
    if (
        not isinstance(u, str)
        or len(u) > 400
        or not u.startswith("https://")
        or "@" in u.split("/")[2]
    ):
        return None
    return u


# ----------------------------------------------------------- the rail


def rail_pick(events: list[dict], weights: dict[str, float], today: date) -> dict:
    """The rail's "Recent events": the last ``RAIL_DAYS`` days, ranked by
    |reaction z| x sqrt(weight) (what mattered to THIS book), the top
    ``RAIL_TOP`` listed newest first, the rest behind "n more". A dividend
    shows only when raised, cut or special (a held one is no news), counted
    as a 1-sigma reaction; one not measured yet counts its day-0 z."""
    since = (today - timedelta(days=RAIL_DAYS)).isoformat()
    t = today.isoformat()
    past = []
    for e in events:
        if e.get("upcoming") or not since < (e.get("session") or e["date"]) <= t:
            continue
        if e["type"] == "D" and (e.get("outcome") not in ("raised", "cut", "special")):
            continue
        rx = e.get("reaction") or {}
        z = rx.get("z") if rx.get("category") is not None else rx.get("z_day")
        z = abs(z) if isinstance(z, int | float) else (1.0 if e["type"] == "D" else 0.5)
        past.append((z * math.sqrt(max(weights.get(e["symbol"], 0.0), 0.0) or 0.01), e))
    ranked = sorted(past, key=lambda t2: -t2[0])
    top = {id(e) for _, e in ranked[:RAIL_TOP]}
    order = sorted((e for _, e in past), key=lambda e: e.get("session") or e["date"], reverse=True)
    return {
        "items": [{**e, "top": id(e) in top} for e in order],
        "top": RAIL_TOP,
        "days": RAIL_DAYS,
    }


# ================================================================ caches


def _hist_path():
    return paths.state_file("events_history")


def _notes_path():
    return paths.state_file("event_notes")


def _read(path, key: str) -> dict:
    """A cache: anything unreadable or from another version reads as empty."""
    ver = VERSION if key == "lines" else NOTES_VERSION
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"v": ver, key: {}}
    if not isinstance(body, dict) or body.get("v") != ver or not isinstance(body.get(key), dict):
        return {"v": ver, key: {}}
    return {"v": ver, key: {k: v for k, v in body[key].items() if isinstance(v, dict)}}


def _load(which: str) -> dict:
    """Memoised by the file's mtime: the page reads these on every answer."""
    path, key = (_hist_path(), "lines") if which == "history" else (_notes_path(), "notes")
    try:
        mt = path.stat().st_mtime_ns
    except OSError:
        mt = None
    with _LOCK:
        memo = _MEMO.get(which)
        if memo and memo[0] == (str(path), mt):
            return memo[1]
        body = _read(path, key)
        _MEMO[which] = ((str(path), mt), body)
        return body


def load_history() -> dict:
    return _load("history")


def load_notes() -> dict:
    return _load("notes")


def _merge(which: str, items: dict) -> None:
    """Merge into what is kept now, as one step: a superseded pass still
    finishing must not write back the copy it started from."""
    path, key = (_hist_path(), "lines") if which == "history" else (_notes_path(), "notes")
    with _LOCK:
        body = _read(path, key)
        body[key].update(items)
        _atomic_write(path, json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n")
        _MEMO[which] = None


def reset_memo() -> None:
    with _LOCK:
        _MEMO.update(history=None, notes=None)


# ========================================================= building a line


def ohlcv_window(fr: dict, i: int, k: int = 5) -> list[list]:
    """The sessions from ``k`` before to ``k`` after ``i`` as [date, open,
    high, low, close, volume, return %, index %]: the model's context (the
    owner asked for the week either side), never shown."""
    out = []
    for j in range(max(i - k, 0), min(i + k + 1, len(fr["d"]))):
        rr, mm = fr["r"][j], fr["m"][j]
        out.append([fr["d"][j], *(float(f"{fr[c][j]:.6g}") for c in ("o", "h", "l", "c")), int(fr["v"][j] or 0),
                    _r2(math.expm1(rr) * 100) if math.isfinite(rr) else None,
                    _r2(math.expm1(mm) * 100) if math.isfinite(mm) else None])  # fmt: skip
    return out


def _session_of(dates: list[str], day: str) -> int | None:
    """The first session on or after ``day`` (an ex-date or a report on a
    holiday reacts on the next one)."""
    import bisect

    j = bisect.bisect_left(dates, day[:10])
    return j if j < len(dates) else None


def build_line(line: str, fund: bool, bars: dict, index: dict | None, index_z: dict[str, float],
               earnings: list[dict], dividends: list[dict], today: date,
               held: dict[str, dict] | None = None, index_name: str | None = None,
               div_compared_in: str | None = None) -> dict:  # fmt: skip
    """Every event of one primary line over the show window, in the record
    shape the response serves. Pure: the bars and Yahoo's rows come in.

    ``div_compared_in``: the company's reporting currency when its dividends
    were converted into it (each dividend then carries ``cmp``, see
    ``div_outcome``); a change under 2% is "held" then, the FX's noise. A
    fund has no big moves: its move is its market's (an emerging-markets fund
    read "Big move · up 7.1% · No headline found")."""
    fr = align(bars, index)
    since = (today - timedelta(days=SHOW_DAYS)).isoformat()
    t = today.isoformat()
    last = fr["d"][-1] if fr["d"] else ""
    events: dict[str, dict] = {}
    idx_sym = (index or {}).get("symbol")

    def add(ev: dict) -> None:
        events[ev["id"]] = ev

    # ---- earnings
    earn_pos = []
    for x in earnings or []:
        rd, d = x.get("react") or x.get("date"), x.get("date")
        if not d or not rd:
            continue
        if d > t or (d == t and x.get("actual") is None and rd > t):
            continue  # upcoming: the Upcoming list carries those
        if d < since:
            continue
        i = _session_of(fr["d"], rd)
        oc, note = eps_outcome(x)
        base = {"id": f"E|{line}|{d}", "type": "E", "line": line, "date": d, "timing": x.get("timing") or None,
                "outcome": oc, "outcome_note": note,
                "eps": {"estimate": x.get("estimate"), "actual": x.get("actual"),
                        "surprise_pct": _r2(x["surprise_pct"]) if x.get("surprise_pct") is not None else None}}  # fmt: skip
        if i is None or rd > last:
            add({**base, "session": rd, "reacting": True, "reaction": None})
            continue
        earn_pos.append(i)
        if x.get("actual") is None and (today - _d(fr["d"][i])).days > 7:
            continue  # a stale stub in Yahoo's calendar, not a report (it still guards the moves)
        add(
            {
                **base,
                "session": fr["d"][i],
                "reaction": reaction(fr, i, fund),
                "window": ohlcv_window(fr, i),
            }
        )
    # ---- dividends (the company's own, in its own currency)
    divs = sorted(
        (x for x in dividends or [] if x.get("date") and x.get("amount")), key=lambda x: x["date"]
    )
    for k, x in enumerate(divs):
        if not since <= x["date"] <= t:
            continue
        i = _session_of(fr["d"], x["date"])
        tol = DIV_TOL_FX if div_compared_in else DIV_TOL
        oc, ch, basis = ("paid", None, "fund") if fund else div_outcome(divs, k, tol)
        add({"id": f"D|{line}|{x['date']}", "type": "D", "line": line, "date": x["date"],
             "session": fr["d"][i] if i is not None else x["date"], "outcome": oc,
             "dividend": {"amount": x["amount"], "change_pct": _r2(ch * 100) if ch is not None else None,
                          "basis": basis, "compared_in": None if fund else div_compared_in},
             "reaction": None})  # fmt: skip
    # ---- very large moves (a fund's market days still count towards the bands)
    moves, drops = big_moves(fr, index_z, earn_pos, since)
    if fund:
        moves = []
    for mv in moves:
        i = mv.pop("i")
        add({"id": f"M|{line}|{mv['date']}", "type": "M", "line": line, "date": mv["date"],
             "session": mv["date"], "outcome": "up" if (mv["pct"] or 0) > 0 else "down",
             "move": {**mv, "index": idx_sym, "index_name": index_name},
             "reaction": reaction(fr, i, fund), "window": ohlcv_window(fr, i)})  # fmt: skip
    for dr in drops:
        dr.update(index=idx_sym, index_name=index_name, index_pct=None)
        j = _session_of(fr["d"], dr["date"])
        if j is not None and math.isfinite(fr["m"][j]):
            dr["index_pct"] = _pct(fr["m"][j])
            dr["index_z"] = _r2(index_z.get(dr["date"]))
    # ---- the held listings' own % on each event session (a Vienna line's
    # close is the owner's, the primary line's the company's)
    held_out = {}
    for sym, hb in (held or {}).items():
        dd = hb["d"]
        lr = np.concatenate([[np.nan], np.diff(np.log(hb["c"]))])
        pos = {d: j for j, d in enumerate(dd)}
        got = {}
        for ev in events.values():
            j = pos.get(ev.get("session"))
            if ev["type"] in ("M", "E") and j is not None and math.isfinite(lr[j]):
                got[ev["session"]] = _pct(lr[j])
        held_out[sym] = got
    return {"asof": last, "computed_at": time.time(), "fund": fund, "index": idx_sym, "index_name": index_name,
            "events": events, "drops": drops, "held": held_out}  # fmt: skip


# ======================================================== the response


def _weights(positions: list[dict]) -> dict[str, float]:
    return {p["symbol"]: float(p.get("weight") or 0.0) for p in positions if p.get("symbol")}


def _is_fund(symbol: str, name: str | None = None) -> bool:
    with contextlib.suppress(Exception):
        from convexity import capm

        if capm.is_fund(symbol):
            return True
    with contextlib.suppress(Exception):
        from convexity import investments

        return bool(investments._pack_kind(symbol, name)["fund"])
    return False


def _primary(symbol: str) -> str:
    try:
        from convexity import capm

        return capm.primary_line(symbol)
    except Exception:
        return symbol.upper()


def _keys() -> tuple[bool, bool]:
    try:
        from convexity import news_sentiment as ns

        return bool(ns.FINNHUB_API_KEY), bool(ns.NVIDIA_API_KEY)
    except Exception:
        return False, False


def reload_keys() -> None:
    """Hooked into ``keys.reload_all()``: a changed key gets its chance at
    once. A rejected key (401/403) stopped the model's calls for good, and a
    backoff would make a fixed key wait hours."""
    with _LOCK:
        _NIM.update(stopped=False, fails=0, until=0.0)
    _STATE["next"] = 0.0


def nim_ready(now: float | None = None) -> bool:
    """Whether the job may call the model now: not stopped by a rejected
    key, not inside a backoff."""
    now = time.time() if now is None else now
    with _LOCK:
        return not _NIM["stopped"] and now >= _NIM["until"]


def _nim_result(ok: bool, code: int | None = None, now: float | None = None) -> None:
    """Record one NIM call's outcome. A rejected key (401/403) stops the
    calls until the keys change (``reload_keys``): asking again can't fix
    it, and it cost ~11 calls every 10 minutes. Any other failure backs off,
    10 minutes doubling to 6 hours; an answer resets it."""
    now = time.time() if now is None else now
    with _LOCK:
        if ok:
            _NIM.update(fails=0, until=0.0)
            return
        if code in (401, 403):
            if not _NIM["stopped"]:
                print(
                    f"[events] NVIDIA key rejected ({code}): the clauses wait until the keys change"
                )
            _NIM["stopped"] = True
            return
        _NIM["fails"] += 1
        wait = min(NIM_BACKOFF_S * 2 ** (_NIM["fails"] - 1), NIM_BACKOFF_MAX_S)
        _NIM["until"] = now + wait
    print(
        f"[events] NIM failed{f' ({code})' if code else ''}: the clauses wait {wait / 60:.0f} min"
    )


def _cause(ev: dict, note: dict | None, today: date, keys_ok: bool) -> dict:
    """What the reader sees as the cause: the note's headline when the model
    picked one, else "No headline found" with why (older than the news
    source's year, no US line, nothing found), or "pending" ("Looking for the
    news") while the job hasn't looked yet, day 0 included: "No headline
    found" before anyone looked would be a guess too."""
    mv = ev.get("move") or {}
    market = market_sentence(mv, mv.get("index_name")) if ev["type"] == "M" else ""
    base = {"market": market}
    n = note or {}
    c = n.get("cause") or {}
    if c.get("kind") in ("news", "results"):
        return {**base, **{k: c.get(k) for k in ("kind", "headline", "source", "url", "tone")}}
    if c.get("kind") == "none":
        return {**base, "kind": "none", "reason": c.get("reason")}
    if ev["type"] != "M":
        return {**base, "kind": "event"}
    age = (today - _d(ev["session"])).days
    if age > NEWS_DAYS:
        return {**base, "kind": "none", "reason": "older"}
    if not keys_ok:
        return {**base, "kind": "none", "reason": "no_key"}
    return {**base, "kind": "pending"}


def for_book(
    positions: list[dict], today: date | None = None, *, upcoming_dates: dict | None = None
) -> dict:
    """The Events payload for one portfolio's holdings, from the two caches
    only (no fetch, no model): ``{events, market_days, bands, upcoming, asof,
    pending, rail}``. A company held through two listings shows once, under
    the bigger one; a holding whose line the job hasn't computed yet is
    counted in ``pending``."""
    today = today or date.today()
    hist = load_history()["lines"]
    notes = load_notes()["notes"]
    fh_key, nv_key = _keys()
    held = [p for p in positions if p.get("symbol") and not p.get("pending")]
    held.sort(key=lambda p: -(p.get("weight") or 0))
    w = _weights(held)
    since = (today - timedelta(days=SHOW_DAYS)).isoformat()
    t = today.isoformat()
    events, seen_lines, missing, asof = [], set(), [], []
    days: dict[str, dict] = {}
    writing = 0
    for p in held:
        sym = p["symbol"]
        line = _primary(sym)
        if line in seen_lines:
            continue
        seen_lines.add(line)
        rec = hist.get(line)
        if not rec:
            missing.append(sym)
            continue
        asof.append(rec.get("asof") or "")
        hp = (rec.get("held") or {}).get(sym) or {}
        for ev in (rec.get("events") or {}).values():
            if (ev.get("session") or ev["date"]) < since:
                continue
            out = {**{k: v for k, v in ev.items() if k != "window"}, "symbol": sym, "name": p.get("name") or sym,
                   "fund": rec.get("fund", False), "ccy": rec.get("ccy"), "index_name": rec.get("index_name"),
                   "upcoming": False, "held_pct": hp.get(ev.get("session")) if sym != line else None}  # fmt: skip
            note = notes.get(ev["id"])
            out["cause"] = _cause(ev, note, today, fh_key and nv_key)
            why = (note or {}).get("why")
            out["why"] = None if restates(why, out["cause"].get("headline")) else why
            out["model"] = (note or {}).get("model")
            if out["cause"].get("kind") == "results" and ev["type"] == "M":
                out["results_day"] = True
            if (
                ev["type"] in ("E", "M")
                and note_stage(ev)
                and out["cause"].get("kind") == "pending"
            ):
                writing += 1
            out["say"] = size_sentence(out, p.get("name") or sym)
            events.append(out)
        for dr in rec.get("drops") or []:
            if dr["date"] < since:
                continue
            m = days.setdefault(dr["date"], {"date": dr["date"], "index": dr.get("index"),
                                             "index_name": dr.get("index_name"), "index_pct": dr.get("index_pct"),
                                             "holdings": []})  # fmt: skip
            m["holdings"].append(sym)
            # A US day reads in the S&P, a European one in the STOXX; the bigger move names it.
            if abs(dr.get("index_pct") or 0) > abs(m.get("index_pct") or 0):
                m.update(
                    index=dr.get("index"),
                    index_name=dr.get("index_name"),
                    index_pct=dr.get("index_pct"),
                )
    events.sort(key=lambda e: (e.get("session") or e["date"], e["symbol"]), reverse=True)
    up = upcoming(held, today, upcoming_dates, hist)
    market_days = sorted(days.values(), key=lambda m: m["date"])
    return {
        "events": events,
        "market_days": market_days,
        "bands": merge_bands(market_days),
        "upcoming": up,
        "asof": min(asof) if asof else None,
        "today": t,
        "pending": {"holdings": missing, "notes": writing},
        "rail": rail_pick(events, w, today),
        "rules": {
            "k_move": K_MOVE,
            "k_market": K_MKT,
            "cuts": list(CUTS),
            "news_days": NEWS_DAYS,
            "rail_move_z": _rail_move_z(),
        },  # fmt: skip
    }


def _rail_move_z() -> float:
    """The Attention rail's own big-move limit (3 by default): the history
    keeps 4 and up, and "How to read it" says so."""
    try:
        from convexity import attention

        return float(attention.load_state()["rules"]["move"]["value"])
    except Exception:
        return 3.0


def upcoming(held: list[dict], today: date, dates: dict | None, hist: dict) -> list[dict]:
    """The next 45 days' earnings and ex-dividend dates of the holdings, from
    the Attention rail's events cache (``state/events.json``), with the
    report's time and the analysts' estimate when Yahoo's calendar has them."""
    if dates is None:
        try:
            from convexity import attention

            dates = attention.load_events()
        except Exception:
            dates = {}
    until = (today + timedelta(days=UPCOMING_DAYS)).isoformat()
    t = today.isoformat()
    out = []
    for p in held:
        ev = dates.get(p["symbol"]) or {}
        for field, typ in (("earnings", "E"), ("ex_div", "D")):
            d = ev.get(field)
            if isinstance(d, str) and t <= d[:10] <= until:
                out.append({"id": f"{typ}|{p['symbol']}|{d[:10]}|up", "type": typ, "symbol": p["symbol"],
                            "name": p.get("name") or p["symbol"], "date": d[:10], "upcoming": True})  # fmt: skip
    return sorted(out, key=lambda u: (u["date"], u["name"]))


def rail_block(positions: list[dict], today: date | None = None) -> dict | None:
    """What the page carries for the rail's Recent events: the rail's items
    only (the full payload is the Events view's own request). From the
    caches, never a fetch; None when nothing is computed yet."""
    try:
        full = for_book(positions, today)
    except Exception as exc:  # extra, like the Attention rail: never lose the page over it
        print(f"[events] rail failed: {type(exc).__name__}")
        return None
    slim = ("id", "type", "symbol", "name", "date", "session", "timing", "outcome", "outcome_note", "eps",
            "dividend", "move", "reaction", "cause", "reacting", "results_day", "top", "line", "fund")  # fmt: skip
    items = [{k: e.get(k) for k in slim if k in e} for e in full["rail"]["items"]]
    return {"items": items, "top": full["rail"]["top"], "days": full["rail"]["days"],
            "pending": bool(full["pending"]["holdings"]), "computed": bool(full["asof"])}  # fmt: skip


# =============================================================== the job


def _closed(line: str, day: str, now: datetime) -> bool:
    """Whether a bar dated ``day`` is a finished session: one dated today is
    live until its market closes (a US line at 21:15 UTC, a European one at
    16:45 UTC, anything else at 09:00 UTC; generous, never early)."""
    if day < now.date().isoformat():
        return True
    if day > now.date().isoformat():
        return False
    s = line.upper()
    suf = s.rsplit(".", 1)[1] if "." in s and not s.startswith("^") else ""
    hm = (
        (21, 15)
        if not suf or s in ("^GSPC", "^IXIC", "^DJI")
        else (16, 45)
        if suf in _EU_SUFFIXES or s == "^STOXX"
        else (9, 0)
    )
    return (now.hour, now.minute) >= hm


def _bars_from_frame(frame, line: str, now: datetime) -> dict | None:
    """yfinance's OHLCV frame for one symbol as ``clean_bars``' arrays,
    today's live bar left out until its market closes."""
    try:
        f = frame[["Open", "High", "Low", "Close", "Volume"]].astype(float).dropna(subset=["Close"])
    except (KeyError, ValueError, TypeError):
        return None
    if f.empty:
        return None
    idx = f.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    days = [d.strftime("%Y-%m-%d") for d in idx]
    keep = [i for i, d in enumerate(days) if _closed(line, d, now)]
    f = f.iloc[keep]
    days = [days[i] for i in keep]
    out = clean_bars(days, f["Open"].values, f["High"].values, f["Low"].values, f["Close"].values,
                     f["Volume"].fillna(0).values)  # fmt: skip
    if out is not None:
        out["symbol"] = line
    return out


def _download(symbols: list[str], now: datetime, cancel) -> dict[str, dict]:
    """Five years of adjusted daily OHLCV, a few symbols a request (dividends
    adjusted in, so an ex-date drop is not a move). A failed chunk is simply
    missing: never "no events"."""
    import yfinance as yf

    out: dict[str, dict] = {}
    failed = 0
    for k in range(0, len(symbols), CHUNK):
        if cancel.is_set():
            break
        if k and cancel.wait(PAUSE_S):
            break
        chunk = symbols[k : k + CHUNK]
        try:
            df = yf.download(tickers=chunk, period=PERIOD, interval="1d", auto_adjust=True,
                             group_by="ticker", threads=True, progress=False)  # fmt: skip
        except Exception as exc:
            print(f"[events] Yahoo failed: {type(exc).__name__}")
            df = None
        got = 0
        if df is not None and not df.empty:
            import pandas as pd

            multi = isinstance(df.columns, pd.MultiIndex)
            for s in chunk if multi else chunk[:1]:
                with contextlib.suppress(KeyError):
                    b = _bars_from_frame(df[s] if multi else df, s, now)
                    if b is not None and len(b["d"]) > SIG_MIN:
                        out[s] = b
                        got += 1
        failed = failed + 1 if not got else 0
        if failed >= MAX_FAILED_CHUNKS:
            break
    return out


def line_ccy(line: str, held: list[dict] | None = None) -> str | None:
    """The unit a line's dividends are in: the held position's quote
    currency when the line is held itself, else its market's (pence on
    London, like Yahoo's per-share amounts)."""
    for p in held or []:
        if p.get("symbol") == line and p.get("quote_ccy"):
            return p["quote_ccy"]
    s = (line or "").upper()
    suf = s.rsplit(".", 1)[1] if "." in s else ""
    if not suf:
        return "USD"
    if suf in ("L", "IL"):
        return "GBp"
    try:
        from convexity import capm

        return capm.home_proxy(s)["ccy"]
    except Exception:
        return None


def reporting_ccy(line: str, held: list[dict]) -> str | None:
    """The company's reporting currency (Yahoo's ``financialCurrency``): the
    saved rows' (``fetch_one`` keeps it from the ``.info`` it already read),
    else the line's own ``.info`` (cached; the job may fetch, the page never
    calls this)."""
    from convexity.helpers import major_ccy

    with contextlib.suppress(Exception):
        from convexity import persistence

        rows = (persistence.load_view("__book__") or {}).get("rows") or []
        syms = {line, *(p["symbol"] for p in held)}
        for r in rows:
            if isinstance(r, dict) and r.get("symbol") in syms and r.get("financial_currency"):
                return major_ccy(r["financial_currency"])
    with contextlib.suppress(Exception):
        from convexity import fetcher

        fin = fetcher._research_info(line).get("financialCurrency")
        if fin:
            return major_ccy(fin)
    return None


def _match_us(divs: list[dict], us_divs: list[dict], days: int = 7) -> list[float | None]:
    """Each payment's US-line amount: the US payment within ``days`` of its
    ex-date (the two venues' ex-dates differ by a day or two), else None."""
    us = sorted((_d(x["date"]), x["amount"]) for x in us_divs if x.get("date") and x.get("amount"))
    out = []
    for x in divs:
        d0 = _d(x["date"])
        near = [(abs((d - d0).days), a) for d, a in us if abs((d - d0).days) <= days]
        out.append(min(near)[1] if near else None)
    return out


CLEAN_SHARE = 0.5  # payments that repeat an earlier one: the series is the declared one


def _per_year(divs: list[dict]) -> int:
    """Payments in the 405 days up to the latest one (4 for a quarterly payer)."""
    if not divs:
        return 0
    last = _d(divs[-1]["date"])
    return sum(1 for x in divs if (last - _d(x["date"])).days <= 405)


def _clean_share(series: list[tuple[str, float | None]], since: str | None) -> float:
    """The share of payments (since ``since``) equal, to 0.1%, to one paid in
    the 400 days before: a declared dividend repeats (the last one, or the
    same payment a year before), a converted one almost never does."""
    pts = [(_d(d), a) for d, a in series if a]
    hits = n = 0
    for k, (d, a) in enumerate(pts):
        if since and d.isoformat() < since:
            continue
        before = [b for dd, b in pts[:k] if 0 < (d - dd).days <= 400]
        if before:
            n += 1
            hits += any(abs(a / b - 1) < 1e-3 for b in before)
    return hits / n if n else 0.0


def div_in_reporting_ccy(divs: list[dict], div_ccy: str | None, rep_ccy: str | None, rates=None,
                         us_divs: list[dict] | None = None, since: str | None = None
                         ) -> tuple[list[dict], str | None]:  # fmt: skip
    """The dividends with ``cmp``, each one in the reporting currency, when
    that isn't the currency Yahoo gives them in (a USD dividend of a London
    line arrives in pence). (divs, the currency compared in), or (divs, None)
    when there is nothing to convert.

    The cleaner source first. A declared dividend repeats: the same amount
    as the last payment or the year-before one (``_clean_share``), while a
    converted one drifts with the rate every time. So the line's own amounts
    stay as they are when they repeat at least as often as the US line's (a
    French company reporting in USD declares in euros: its US line's are the
    converted ones, and read "raised 20%"); else ``us_divs``, the company's
    US line's dividends (an ADR's are the declared USD amount times its
    ratio, which cancels in a change), when they match at least 80% of the
    payments since ``since`` and repeat; a payment with no match gets ``cmp``
    None, never the converted figure (the ratio would mix in). Else, with no
    evidence either way, each payment at its ex-date's rate (``rates``, tests: date
    -> reporting units per major unit; else ``fx.fx_multipliers``, Yahoo's
    history, a job-side fetch). That is only close: the company fixes the
    pence when it announces, so the ex-date's rate is up to ~2% off (checked
    on a USD-declaring London company, 2025-26), hence the 2% "held" band.
    A date with no rate gets ``cmp`` None, which ``div_outcome`` reads as
    "held"."""
    from convexity.helpers import major_ccy, price_in_major

    if not divs or not div_ccy or not rep_ccy or major_ccy(div_ccy) == major_ccy(rep_ccy):
        return divs, None
    # A yearly or half-yearly payer can't show a repeat (each payment is the
    # first of its kind in 400 days), so neither series can prove it is the
    # declared one; its own line's amounts are far more often the declared
    # ones than not (a Swiss company that reports in USD declares in francs:
    # converted at the ex-date's rate its +5.7% read +19.6%, 2026-11-23).
    if _per_year(divs) <= 2:
        return divs, None
    own = _clean_share([(x["date"], x["amount"]) for x in divs], since)
    got, us = None, -1.0
    if us_divs:
        got = _match_us(divs, us_divs)
        recent = [g for x, g in zip(divs, got, strict=True) if since is None or x["date"] >= since]
        if recent and sum(g is not None for g in recent) >= 0.8 * len(recent):
            us = _clean_share([(x["date"], g) for x, g in zip(divs, got, strict=True) if g], since)
    if own >= CLEAN_SHARE and own >= us:
        return divs, None  # declared in the line's own currency: compared as they are
    if got is not None and us >= CLEAN_SHARE / 2:
        return [{**x, "cmp": g} for x, g in zip(divs, got, strict=True)], major_ccy(rep_ccy)
    dates = [x["date"] for x in divs]
    if rates is None:
        mult = None
        with contextlib.suppress(Exception):
            from convexity import fx

            mult = fx.fx_multipliers([major_ccy(div_ccy)], major_ccy(rep_ccy), dates).get(
                major_ccy(div_ccy)
            )
        rates = dict(zip(dates, [float(v) for v in mult], strict=True)) if mult is not None else {}
    out = []
    for x in divs:
        r = rates.get(x["date"])
        ok = r is not None and math.isfinite(r) and r > 0
        out.append({**x, "cmp": price_in_major(x["amount"], div_ccy) * r if ok else None})
    return out, major_ccy(rep_ccy)


def _us_divs(line: str, ccy: str | None, rep: str | None) -> list[dict] | None:
    """The US line's dividends, for a USD reporter whose line pays in
    another currency (job-side: ``fetch_events`` is a Yahoo call, cached)."""
    from convexity.helpers import major_ccy

    if rep != "USD" or major_ccy(ccy or "USD") == "USD":
        return None
    with contextlib.suppress(Exception):
        from convexity import fetcher, symbol_db

        us = symbol_db.us_line(line)
        if us and us != line:
            return fetcher.fetch_events(us).get("dividends") or None
    return None


def _held_now() -> list[dict]:
    """The showing (pinned) portfolio's holdings: the computed page's
    positions when there is one (names, weights), else the ledger's symbols."""
    from convexity import investments

    page = investments.computed_page() or investments.last_page() or {}
    if not page.get("positions"):
        # A fresh process: the last good page on disk, in whatever currency it was saved.
        with contextlib.suppress(Exception):
            from convexity import books

            saved = json.loads(books.book_file("book_cache").read_text(encoding="utf-8"))
            page = investments.cached_page(saved.get("ccy")) or {}
    pos = [p for p in page.get("positions") or [] if p.get("symbol") and not p.get("pending")]
    if pos:
        return pos
    return [{"symbol": s, "name": s, "weight": 0.0} for s in investments.held_symbols()]


def lines_due(held: list[dict], now: float | None = None) -> list[str]:
    """Held primary lines whose numbers are missing or older than ``NUMBERS_TTL_S``."""
    now = time.time() if now is None else now
    hist = load_history()["lines"]
    out = []
    for p in held:
        line = _primary(p["symbol"])
        rec = hist.get(line) or {}
        if now - float(rec.get("computed_at") or 0) >= NUMBERS_TTL_S and line not in out:
            out.append(line)
    return out


def _reusable_heads(n: dict, stage: str | None, age: int, now: float) -> list[dict] | None:
    """A note's kept headlines when they still stand: fetched (successfully:
    a failed call keeps nothing) at this stage, and not a "No headline found"
    whose daily re-ask is due. None means Finnhub must be asked. Notes
    written before ``headlines_at`` existed count from ``checked_at``."""
    at = n.get("headlines_at") or (n.get("checked_at") if "headlines" in n else None)
    if not at or (n.get("headlines_stage") or n.get("stage")) != stage:
        return None
    stale = now - float(at) >= 86400
    if (n.get("cause") or {}).get("kind") == "none" and age <= NEWS_RECHECK_DAYS and stale:
        return None
    return list(n.get("headlines") or [])


def notes_due(held: list[dict], today: date | None = None) -> list[dict]:
    """Events whose note the job should write now (keys set): E and M events
    with a label whose note is missing, from an earlier stage, or a "No
    headline found" still inside its week of daily re-asks. While the model
    is stopped or backing off (``nim_ready``), only moves whose headlines
    must still be fetched are due: the rest would only wait for it, and a
    pass every 10 minutes that can't call it is noise."""
    today = today or date.today()
    fh_key, nv_key = _keys()
    if not nv_key:
        return []  # the numbers need no key; the texts need the model
    payload = for_book(held, today)
    notes = load_notes()["notes"]
    ready, now = nim_ready(), time.time()
    out = []
    for ev in payload["events"]:
        if ev["type"] not in ("E", "M") or not note_stage(ev) or (ev["type"] == "M" and not fh_key):
            continue
        n = notes.get(ev["id"]) or {}
        age = (today - _d(ev["session"])).days
        if ev["type"] == "M" and age > NEWS_DAYS:
            if not n:
                out.append(ev)  # recorded once, no call: "No headline found" is the truth there
            continue
        if not ready and (
            ev["type"] == "E" or _reusable_heads(n, note_stage(ev), age, now) is not None
        ):
            continue
        if not n or not n.get("inputs"):
            out.append(ev)  # never written, or a rewrite was asked for
            continue
        empty = (n.get("cause") or {}).get("kind") in ("none", "event")
        if note_stage(ev) != n.get("stage"):
            out.append(ev)
        elif (
            empty
            and age <= NEWS_RECHECK_DAYS
            and time.time() - float(n.get("checked_at") or 0) >= 86400
        ):
            out.append(ev)  # news arrives late: asked again daily for a week
    return out


def company_name(symbol: str, given: str | None = None) -> str | None:
    """The company's name for the relevance scorer: the page's, else the
    symbol pack's (a name that is only the ticker would match nothing: the
    first backfill found no headline for a Madrid line that way)."""
    if given and given.upper() != (symbol or "").upper():
        return given
    with contextlib.suppress(Exception):
        from convexity import symbol_db

        for s in (symbol, _primary(symbol)):
            h = symbol_db.get(s)
            if h and h.name:
                return h.name
    return None


def _finnhub_line(symbol: str) -> str | None:
    """The company's US line for Finnhub's free plan, else the primary line
    when that is a US line (a US company's Milan line: the pack has no US
    line for it, capm learned it by name)."""
    with contextlib.suppress(Exception):
        from convexity import finnhub_adapter, symbol_db

        symbol_db.discover_us_line(symbol)
        us = finnhub_adapter.finnhub_symbol(symbol)
        if us:
            return us
    prim = _primary(symbol)
    return prim.replace("-", ".") if prim and "." not in prim and not prim.startswith("^") else None


def _headlines_finnhub(ev: dict, name: str, cancel) -> list[dict] | None:
    """Finnhub company news for the session and the day before, kept when
    the app's relevance scorer finds the company in the title or the lead;
    None when the call failed (never stored as "none")."""
    from convexity import news_sentiment as ns
    from convexity import relevance

    us = _finnhub_line(ev["symbol"])
    if not us:
        return []
    day = _d(ev["session"])
    raw = ns._fh_call("company-news", {"symbol": us, "from": (day - timedelta(days=1)).isoformat(),
                                       "to": day.isoformat()}, cancel=cancel)  # fmt: skip
    if not isinstance(raw, list):
        return None  # no answer, or an error body: a failed call, never "none"
    return _keep_heads(raw, us, name, day, relevance)


def _keep_heads(arts: list[dict], sym: str, name: str, day: date, relevance) -> list[dict]:
    lo = datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() - 86400 - 6 * 3600
    hi = lo + 2 * 86400 + 6 * 3600 + 23 * 3600
    seen, out = set(), []
    for a in arts:
        h = (a.get("headline") or "").strip()
        ts = a.get("datetime") or 0
        if not h or not (lo <= ts <= hi):
            continue
        key = relevance.norm_title(h)
        if key in seen or relevance.is_boilerplate(h):
            continue
        seen.add(key)
        rel = relevance.relevance_score(h, a.get("summary"), sym, name)
        if rel >= MIN_RELEVANCE:
            out.append({"headline": h[:200], "summary": (a.get("summary") or "")[:300], "source": a.get("source") or "",
                        "url": safe_url(a.get("url")), "datetime": ts, "rel": round(rel, 2)})  # fmt: skip
    out.sort(key=lambda x: (-x["rel"], x["datetime"]))
    return out[:MAX_HEADLINES]


def _headlines_cached(ev: dict, name: str) -> list[dict]:
    """The app's own cached news for the event (free: the News read keeps a
    month), for an earnings report too."""
    try:
        from convexity import news_sentiment as ns
        from convexity import relevance

        arts = ns.get_cached_articles([ev["symbol"]])
    except Exception:
        return []
    return _keep_heads(arts, ev["symbol"], name, _d(ev["session"]), relevance)


class _Either:
    """A cancel token that fires when either does: the job's cancel (a
    refresh took over) or a deadline (one NIM call must not hold the pass)."""

    def __init__(self, a, b):
        self.a, self.b = a, b

    def is_set(self) -> bool:
        return self.a.is_set() or self.b.is_set()


def _window(ev: dict) -> list[str]:
    """The kept OHLCV week either side, one line a session, the event's marked."""
    out = []
    for d, o, h, lo, c, v, r, m in ev.get("window") or []:
        mark = ">>" if d == ev.get("session") else "  "
        ret = "" if r is None else f" | {r:+.2f}%"
        idx = "" if m is None else f" | market {m:+.2f}%"
        out.append(f"{mark} {d} O {o:g} H {h:g} L {lo:g} C {c:g} V {v:,}{ret}{idx}")
    return out


def _ask_model(
    ev: dict, name: str, heads: list[dict], cancel, budget: dict
) -> tuple[dict | None, str]:
    """One "why" call and, if the answer fails the checks, one more; then
    "No headline found". (None, "failed") when NIM didn't answer at all: a
    failed call is never stored, and it stops or backs off the model
    (``_nim_result``). Each CALL comes off ``budget["nim"]``."""
    from convexity import news_sentiment as ns
    from convexity.helpers import Cancelled, Deadline

    text = why_input(ev, name, heads, _window(ev))
    rx = ev.get("reaction") or {}
    direction = (1 if (ev.get("move") or {}).get("pct", 0) > 0 else -1) if ev["type"] == "M" else int(
        math.copysign(1, rx["category"]) if rx.get("category") else 0)  # fmt: skip
    reason = "failed"
    for _ in range(2):
        if budget["nim"] <= 0:
            return None, "budget"
        budget["nim"] -= 1
        fail: dict = {}
        try:
            out = ns._nvidia_call(WHY_SYSTEM, text, (), _Either(cancel, Deadline(NIM_DEADLINE_S)),
                                  schema=why_schema(len(heads)), name="event_why", record=False,
                                  tag="events", timeout=45, max_tokens=300, failure=fail)  # fmt: skip
        except Cancelled:
            if cancel.is_set():
                raise
            out = None
        _STATE["nim_calls"] = _STATE.get("nim_calls", 0) + 1
        _nim_result(out is not None, fail.get("code"))
        if out is None:
            return None, "failed"
        got, reason = check_why(out, heads, direction)
        if got is not None:
            return got, ""
    return {"headline": 0, "why": None, "tone": None, "rejected": reason}, reason


def _write_note(ev: dict, name: str, cancel, budget: dict) -> bool | None:
    """The note for one event: headlines, then the model's clause. True when
    settled; False when a call failed or the pass's budget ran out (tried
    again next pass); None when the clause waits for the model (stopped or
    backing off). Headlines that came back are kept either way, with their
    fetch time (``headlines_at``), so a model that fails doesn't cost a
    Finnhub call per move per pass; a failed call keeps nothing."""
    stage = note_stage(ev)
    old = load_notes()["notes"].get(ev["id"]) or {}
    today = date.today()
    age = (today - _d(ev["session"])).days
    now = time.time()
    if ev["type"] == "M" and age > NEWS_DAYS:
        _merge("notes", {ev["id"]: {"stage": "final", "inputs": "older", "checked_at": now,
                                    "cause": {"kind": "none", "reason": "older"}}})  # fmt: skip
        return True
    kept: dict = {}  # the headlines just fetched, stored whatever the model does
    if ev["type"] == "M":
        heads = _reusable_heads(old, stage, age, now)
        if heads is None:
            if not _keys()[0]:
                return True  # no Finnhub key: nothing to look at, nothing stored
            if budget["fh"] <= 0:
                return False
            budget["fh"] -= 1
            got = _headlines_finnhub(ev, name, cancel)
            _STATE["fh_calls"] = _STATE.get("fh_calls", 0) + 1
            cancel.wait(FH_PAUSE_S)
            if got is None:
                return False  # Finnhub didn't answer: never "none"
            # the app's cached news may have more for a fresh move
            more = [
                h
                for h in _headlines_cached(ev, name)
                if h["headline"] not in {x["headline"] for x in got}
            ]
            heads = (got + more)[:MAX_HEADLINES]
            kept = {"headlines": heads, "headlines_at": now, "headlines_stage": stage}
    else:
        heads = _headlines_cached(ev, name)

    def wait_for_model() -> None:
        # The old cause stays; no inputs, so the note is due again once the model is.
        if kept:
            _merge("notes", {ev["id"]: {**old, **kept, "inputs": None}})

    if not heads:
        # An E with no headline stays templated (no call); a move has "No headline found".
        cause = (
            {"kind": "event"} if ev["type"] == "E" else {"kind": "none", "reason": "nothing_found"}
        )
        _merge("notes", {ev["id"]: {"stage": stage, "inputs": inputs_hash(ev, [], stage), "checked_at": now,
                                    "headlines": [], "cause": cause, **kept}})  # fmt: skip
        return True
    h = inputs_hash(ev, heads, stage)
    if old.get("inputs") == h and (old.get("cause") or {}).get("kind") not in (None, "pending"):
        if kept:  # a re-ask found the same headlines: checked today, nothing new
            _merge("notes", {ev["id"]: {**old, **kept, "checked_at": now}})
        return True
    if not _keys()[1]:
        wait_for_model()
        return True  # headlines but no NIM key: the cause waits; nothing guessed
    if not nim_ready():
        wait_for_model()
        return None
    if budget["nim"] < 2:  # one unit can take two calls (the retry)
        wait_for_model()
        return False
    got, why = _ask_model(ev, name, heads, cancel, budget)
    cancel.wait(NIM_PAUSE_S)
    if got is None:
        wait_for_model()
        return False
    pick = got["headline"]
    if pick:
        hd = heads[pick - 1]
        kind = "results" if ev["type"] == "M" and results_like(hd["headline"]) else "news"
        cause = {"kind": kind, "headline": hd["headline"], "source": hd.get("source"), "url": hd.get("url"),
                 "tone": got.get("tone")}  # fmt: skip
    else:
        cause = {"kind": "none", "reason": "rejected" if got.get("rejected") else "none_fit"}
    from convexity import news_sentiment as ns

    fetched = {"headlines_at": kept.get("headlines_at") or old.get("headlines_at") or now,
               "headlines_stage": stage} if ev["type"] == "M" else {}  # fmt: skip
    _merge("notes", {ev["id"]: {"stage": stage, "inputs": h, "checked_at": now, "headlines": heads, **fetched,
                                "cause": cause, "why": got.get("why"), "model": ns.active_model(),
                                "at": datetime.now(UTC).isoformat(timespec="seconds")}})  # fmt: skip
    return True


def compute_lines(held: list[dict], lines: list[str], cancel, now: datetime | None = None) -> int:
    """Download and rebuild the numbers of ``lines`` (with the indexes and
    the held listings they need); returns how many were kept."""
    from convexity import fetcher

    now = now or datetime.now(UTC)
    today = date.today()
    by_line: dict[str, list[dict]] = {}
    for p in held:
        by_line.setdefault(_primary(p["symbol"]), []).append(p)
    lines = [ln for ln in lines if ln in by_line]
    if not lines:
        return 0
    idx = {ln: broad_index(ln) for ln in lines}
    heldsyms = sorted({p["symbol"] for ln in lines for p in by_line[ln] if p["symbol"] != ln})
    want = list(dict.fromkeys([*sorted({i for i, _ in idx.values()}), *lines, *heldsyms]))
    bars = _download(want, now, cancel)
    if cancel.is_set():
        return 0
    iz = {}
    for i, _n in set(idx.values()):
        b = bars.get(i)
        if b is not None:
            r = np.concatenate([[np.nan], np.diff(np.log(b["c"]))])
            iz[i] = {
                d: float(z) for d, z in zip(b["d"], z_scores(r), strict=True) if np.isfinite(z)
            }
    kept = {}
    for ln in lines:
        if cancel.is_set():
            break
        b = bars.get(ln)
        i_sym, i_name = idx[ln]
        if b is None or bars.get(i_sym) is None:
            continue  # no answer: the old record stays, asked again next pass
        fund = all(_is_fund(p["symbol"], p.get("name")) for p in by_line[ln])
        try:
            evs = fetcher.fetch_events(ln)
        except Exception as exc:
            print(f"[events] Yahoo events {ln} failed: {type(exc).__name__}")
            continue
        earnings = evs.get("earnings") or []
        divs = evs.get("dividends") or []
        if fund:
            # A fund's own distributions, on the listing held
            hs = by_line[ln][0]["symbol"]
            if hs != ln:
                with contextlib.suppress(Exception):
                    divs = fetcher.fetch_events(hs).get("dividends") or divs
        held_b = {
            p["symbol"]: bars[p["symbol"]]
            for p in by_line[ln]
            if p["symbol"] != ln and p["symbol"] in bars
        }
        ccy = line_ccy(ln, by_line[ln])
        cmp_in = None
        if not fund and divs:
            rep = reporting_ccy(ln, by_line[ln])
            since = (
                today - timedelta(days=SHOW_DAYS + 410)
            ).isoformat()  # the shown ones and their references
            divs, cmp_in = div_in_reporting_ccy(
                divs, ccy, rep, us_divs=_us_divs(ln, ccy, rep), since=since
            )
        try:
            rec = build_line(ln, fund, b, {**bars[i_sym], "symbol": i_sym}, iz.get(i_sym, {}), earnings, divs,
                             today, held_b, i_name, cmp_in)  # fmt: skip
        except Exception as exc:  # one bad line never ends the pass
            print(f"[events] {ln} skipped: {type(exc).__name__}: {exc}")
            continue
        rec["ccy"] = ccy
        kept[ln] = rec
    if kept:
        _merge("history", kept)
    return len(kept)


def run_pass(cancel) -> str:
    """One background pass: the numbers of the lines that are due, then the
    notes, a few at a time. Called only from the job."""
    held = _held_now()
    if not held:
        return "nothing"
    t0 = time.time()
    # The primary lines first: capm learns a company's home line by name or
    # country (a London company's Amsterdam line -> its London line, not the NYSE
    # receipt the pack groups it with; a US company's Milan line -> its US line), in the background, kept a week on disk.
    with contextlib.suppress(Exception):
        from convexity import capm

        capm.ensure(
            [p["symbol"] for p in held],
            names={p["symbol"]: p.get("name") for p in held if p.get("name")},
        )
    if cancel.is_set():
        return "cancelled"
    due = lines_due(held)
    n = compute_lines(held, due, cancel) if due else 0
    if due and not n and not cancel.is_set():
        _STATE["next"] = time.time() + PASS_RETRY_S
        print("[events] Yahoo isn't answering; trying again later")
    if cancel.is_set():
        return "cancelled"
    budget = {"nim": MAX_NIM_PER_PASS, "fh": MAX_FH_PER_PASS}
    names = {p["symbol"]: company_name(p["symbol"], p.get("name")) or p["symbol"] for p in held}
    todo = notes_due(held)
    wrote = failed = waiting = 0
    for ev in todo:
        if cancel.is_set():
            return "cancelled"
        try:
            ok = _write_note(ev, names.get(ev["symbol"], ev["symbol"]), cancel, budget)
        except Exception as exc:
            from convexity.helpers import Cancelled

            if isinstance(exc, Cancelled) and cancel.is_set():
                return "cancelled"
            print(f"[events] note {ev['id']} failed: {type(exc).__name__}")
            ok = False
        wrote += ok is True
        failed += ok is False
        waiting += ok is None
        if budget["nim"] < 2 or budget["fh"] <= 0:
            break
    if failed:
        # A Finnhub failure or the budget: again in 10 minutes. A NIM failure
        # also backs the model off (nim_ready), and notes_due leaves out what
        # only waits for it, so the retry spends no call on those.
        _STATE["next"] = time.time() + PASS_RETRY_S / 3
    print(f"[events] {n} line(s) computed, {wrote} note(s) checked, {failed} failed, "
          f"{waiting} waiting for the model, {time.time() - t0:.0f}s")  # fmt: skip
    try:
        from convexity import investments

        investments.forget_page()  # the page carries the rail's Recent events
    except Exception:
        pass
    return "done"


# ---------------------------------------------------------------- scheduler


def due(now: float | None = None) -> bool:
    held = _held_now()
    if not held:
        return False
    return bool(lines_due(held, now)) or bool(notes_due(held))


def tick(now: float | None = None, *, submit=None):
    """Start a pass if something is due and nothing else is running; a
    refresh the owner asks for supersedes it."""
    from convexity import jobs

    now = time.time() if now is None else now
    if now < _STATE["next"]:
        return None
    running = jobs.current()
    if running is not None and not running.is_terminal():
        return None
    if not due(now):
        return None
    return (submit or _submit)()


def _submit():
    from convexity import jobs

    return jobs.submit(scope="current", phases=["events"], days=7,
                       entries_by_view={jobs.BOOK_VIEW: ""}, context={"background": True})  # fmt: skip


def _loop() -> None:
    stop = _STATE["stop"]
    if stop.wait(FIRST_TICK_S):
        return
    while True:
        try:
            tick()
        except Exception as exc:  # the scheduler outlives any one bad tick
            print(f"[events] tick failed: {type(exc).__name__}: {exc}")
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
    _STATE["thread"] = threading.Thread(target=_loop, name="event-study", daemon=True)
    _STATE["thread"].start()


def stop() -> None:
    _STATE["stop"].set()
