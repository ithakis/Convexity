"""What-if portfolios (Phase 4): a copy of My Investments to change freely,
compared with the real book, and the trades that would take the book there.

A what-if is a normal portfolio (watchlist, view, weight presets) whose view
carries a ``whatif`` block (persistence.clean_whatif). It belongs to the
My Investments portfolio it was copied from (``book_id``) and shows only while
that one shows; deleting the book detaches it (books.delete). The block's
``origin`` is frozen at the copy (the green markers); everything else here
reads the LIVE book, the page as computed (``investments.page``'s memo: one
computation per currency, never a second fetch of the book).

Four reads, none on ``fetch_one``'s path:
- ``create``: the copy, written in one go (watchlist + view + preset + block).
- ``book_view``: the book's weights by symbol, each what-if name matched to
  the book's line of the same company when the book holds another listing
  ("as APC.DE", the symbol pack's company group), what changed since the copy.
- ``compare``: the actual record as a line on the what-if's chart
  (g(t) = P(d0) TWR(t)/TWR(d0), never backfilled), both sides' statistics on
  one calendar and one risk-free rate, and the book's total row.
- ``rebalance_payload``: the trades (rebalance.py) at one price snapshot, the
  page's own. Read-only: nothing is written, nothing is sent to a broker.
"""

from __future__ import annotations

import math
import re
import threading
import time
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd

from convexity import books, investments, persistence
from convexity import rebalance as rb
from convexity.helpers import major_ccy, price_in_major

BOOK_VIEW = "__book__"
_STATS_KEYS = ("total_return", "ann_return", "ann_vol", "sharpe", "sortino", "max_dd", "calmar",
               "rf_ann")  # fmt: skip
_REL_KEYS = ("beta", "r2", "te", "ir")
_SIGMA_TTL = 600.0
_SIGMA: dict[tuple, tuple[float, np.ndarray, dict]] = {}
_SIGMA_LOCK = threading.Lock()
_MIN_OBS = 60  # daily returns a name needs for its own row of the covariance
_NAME_MAX = 60


class WhatIfError(Exception):
    """A request that can't be answered as asked: ``status`` for the route,
    ``code`` for the page, ``message`` in the owner's words."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ------------------------------------------------------------- the book


def _expect(book_id, *, writes: bool) -> str:
    """books.expect, worded for a read: BookMoved's own message says nothing
    was saved, true of a write and confusing on a comparison."""
    try:
        return books.expect(book_id)
    except books.BookMoved:
        if writes:
            raise
        raise WhatIfError(409, "BookMoved",
                          "Another My Investments portfolio is showing now: ask again.") from None  # fmt: skip


def _page(book_id: str, ccy) -> dict:
    """The book's page as computed: the memo or the last page of this rev and
    day, else ``investments.page`` (which waits on a computation already
    running rather than starting a second). A disk copy (``stale``) is never
    used: its prices may be hours old."""
    with books.pin(book_id):
        out = investments.computed_page(ccy)
        if out is None or out.get("stale"):
            out = investments.page(ccy)
    return out


def _status(page: dict) -> str:
    if page.get("error") or page.get("broken"):
        return "broken"
    if page.get("empty") or not page.get("positions"):
        return "empty"
    if page.get("waiting") or page.get("splits_pending"):
        return "waiting"
    return "ok"


def _ready(page: dict) -> None:
    """Refuse a copy or a rebalance on a book that isn't whole: a holding left
    out for want of its split history would be sold, or missing, in arithmetic
    that looks complete."""
    st = _status(page)
    if st == "broken":
        raise WhatIfError(422, "BookBroken",
                          "My Investments can't be computed right now: fix it on its own page first.")  # fmt: skip
    if st == "empty":
        raise WhatIfError(422, "BookEmpty", "My Investments holds nothing yet.")
    if st == "waiting":
        raise WhatIfError(409, "BookWaiting",
                          "Waiting for Yahoo's split history for a holding. Try again in a minute.")  # fmt: skip


def _held(page: dict) -> list[dict]:
    return [p for p in page.get("positions") or []
            if not p.get("pending") and isinstance(p.get("value"), int | float) and p["value"] > 0]  # fmt: skip


def book_weights(page: dict) -> dict[str, float]:
    """Each holding's weight, cash left out: the red tab's Book mode."""
    held = _held(page)
    total = sum(p["value"] for p in held)
    return {p["symbol"]: p["value"] / total for p in held} if total > 0 else {}


def _day_label(d: date | None = None) -> str:
    d = d or date.today()
    return f"{d.day} {d:%b}"


def _taken() -> set[str]:
    return set(persistence.load_watchlists()) | set(persistence.list_views().get("views") or {})


def next_name() -> str:
    taken, n = _taken(), 1
    while f"What-if {n}" in taken:
        n += 1
    return f"What-if {n}"


def _clean_name(name) -> str | None:
    if name is None or (isinstance(name, str) and not name.strip()):
        return None
    if not isinstance(name, str) or not name.strip().isprintable() or len(name.strip()) > _NAME_MAX:
        raise WhatIfError(400, "BadName", "Give the copy a shorter name, letters and digits.")
    name = name.strip()
    if name.startswith("__"):
        raise WhatIfError(400, "BadName", "That name is reserved.")
    return name


# ------------------------------------------------------------- the copy


def create(book_id, name=None, ccy=None) -> dict:
    """Copy the showing book into a new portfolio at today's weights, with the
    active preset "My Investments · 13 Nov" and the ``whatif`` block. Undo is
    the ordinary delete (DELETE /api/watchlists?name=…), which removes the
    watchlist, the view, its presets and its runs."""
    book_id = _expect(book_id, writes=True)
    page = _page(book_id, ccy)
    _ready(page)
    weights = book_weights(page)
    if not weights:
        raise WhatIfError(422, "BookEmpty", "My Investments holds nothing yet.")
    name = _clean_name(name) or next_name()
    if name in _taken():
        raise WhatIfError(409, "NameTaken", f"There is already a portfolio called {name}.")
    syms = sorted(weights, key=lambda s: -weights[s])
    saved = (persistence.load_view(BOOK_VIEW) or {}).get("rows") or []
    rows = [r for r in saved if isinstance(r, dict) and r.get("symbol") in weights]
    block = {
        "book_id": book_id,
        "copied_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rev": page.get("rev"),
        "ccy": page.get("ccy"),
        "origin": {s: round(weights[s], 8) for s in syms},
        "entry_ids": [e["id"] for e in page.get("entries") or [] if isinstance(e.get("id"), str)],
    }
    preset = f"My Investments · {_day_label()}"
    try:
        view = persistence.create_whatif_view(
            name, ", ".join(syms), rows, block, preset, stale=len(rows) < len(syms)
        )
    except ValueError as exc:
        raise WhatIfError(409, "NameTaken", str(exc)) from exc
    print(f"[whatif] {name}: a copy of {len(syms)} holdings ({book_id})")
    return {
        "name": name,
        "entries": view["entries"],
        "whatif": _public(view["whatif"]),
        "presets": view["weight_presets"],
        "active": preset,
        "n_holdings": len(syms),
        "rows_complete": len(rows) == len(syms),
    }


def _public(block: dict) -> dict:
    return {k: v for k, v in block.items() if k != "entry_ids"}


# ------------------------------------------------------ book weights + match


def match(symbols: list[str], weights: dict[str, float]) -> tuple[dict, list[str]]:
    """Each portfolio name's book weight: the same ticker, else the book's
    line of the same company (symbol pack group; shares only, never a fund
    for a share) when no portfolio name holds that line itself. Returns
    ({symbol: {weight, via}}, book symbols matched by nobody)."""
    from convexity import symbol_db as sdb

    syms = list(dict.fromkeys(s for s in symbols if isinstance(s, str)))
    own = set(syms)
    used: set[str] = set()
    out: dict[str, dict] = {}

    def hit(t):
        try:
            h = sdb.get(t)
        except Exception:
            return None
        return (
            h if h is not None and h.group != sdb.YAHOO_GROUP and not sdb.not_the_share(t) else None
        )

    by_group: dict[int, list] = {}
    for b in weights:
        if b not in own and (h := hit(b)) is not None:
            by_group.setdefault(h.group, []).append((b, h))
    for s in syms:
        if s in weights:
            out[s] = {"weight": weights[s], "via": None}
            used.add(s)
            continue
        h = hit(s)
        cands = [b for b, hb in by_group.get(h.group, []) if b not in used and sdb.may_stand_in(h, hb)] if h else []  # fmt: skip
        if cands:
            b = max(cands, key=lambda x: weights[x])
            out[s] = {"weight": weights[b], "via": b}
            used.add(b)
        else:
            out[s] = {"weight": 0.0, "via": None}
    return out, [b for b in sorted(weights, key=lambda x: -weights[x]) if b not in used]


def _changes(block: dict, page: dict) -> dict:
    """The ledger entries added since the copy (newest first) and how many
    of the copy's are gone: "2 trades on Nov 14"."""
    seen = set(block.get("entry_ids") or [])
    now = [e for e in page.get("entries") or [] if isinstance(e, dict)]
    added = [{k: e.get(k) for k in ("id", "date", "type", "symbol", "name", "qty")}
             for e in now if e.get("id") not in seen]  # fmt: skip
    gone = len(seen - {e.get("id") for e in now})
    return {"added": added[:20], "n_added": len(added), "n_removed": gone}


def _view_symbols(view: str | None) -> list[str]:
    entry = persistence.load_view(view) if view else {}
    rows = entry.get("rows") or []
    syms = [r["symbol"] for r in rows if isinstance(r, dict) and isinstance(r.get("symbol"), str)]
    if syms:
        return syms
    text = entry.get("entries") or persistence.load_watchlists().get(view or "", "")
    return [t.strip().upper() for t in re.split(r"[,\n]", text or "") if t.strip()]


def book_view(view: str | None, symbols: list[str] | None, ccy=None) -> dict:
    """GET /api/whatif/book: the showing book's weights for the Weight
    Center and the weights row (any portfolio, not only a what-if), and for a
    what-if its copy facts and what changed since."""
    book_id = books.showing()
    block = (
        persistence.clean_whatif((persistence.load_view(view) or {}).get("whatif"))
        if view
        else None
    )
    if block and block["book_id"] != book_id:
        # Its book isn't showing: the tab is hidden, and the page in hand is
        # another portfolio's, so no numbers.
        return {"status": "other_book", "book_id": book_id, "whatif": _public(block)}
    page = _page(book_id, ccy)
    st = _status(page)
    out: dict = {"status": st, "book_id": book_id, "rev": page.get("rev"), "ccy": page.get("ccy")}
    if block:
        out["whatif"] = {**_public(block), "changed": block["rev"] != page.get("rev"),
                         "changes": _changes(block, page)}  # fmt: skip
    if st != "ok":
        return out
    weights = book_weights(page)
    syms = symbols or _view_symbols(view)
    by_sym, only = match(syms, weights)
    names = {p["symbol"]: p.get("name") for p in _held(page)}
    out.update(
        weights=weights,
        names=names,
        match=by_sym,
        book_only=[{"symbol": b, "name": names.get(b), "weight": weights[b]} for b in only],
        cash_weight=(page.get("headline") or {}).get("cash_weight"),
    )
    return out


# ------------------------------------------------------- the comparison


def _points_series(points) -> pd.Series:
    if not points:
        return pd.Series(dtype=float)
    arr = np.asarray(points, dtype=float)
    idx = pd.to_datetime(arr[:, 0].astype("int64"), unit="ms").normalize()
    return pd.Series(arr[:, 1], index=idx)


def _pick(stats: dict, keys) -> dict:
    return {k: stats.get(k) for k in keys}


def _block(val: pd.Series, rf, bench_ret: pd.Series | None) -> dict | None:
    from convexity.analytics import _relative, _stats

    if val is None or len(val) < 3:
        return None
    ret = val.pct_change().dropna()
    out = _pick(_stats(ret, val, rf), _STATS_KEYS)
    out["rel"] = _pick(_relative(ret, bench_ret), _REL_KEYS) if bench_ret is not None else {}
    return out


def compare(book_id, rows, weights, period="1Y", ccy="USD", rebalance=None, tc_bps=None) -> dict:
    """POST /api/whatif/compare. The what-if's own analytics come from
    ``analyze_portfolios_multi`` with the weights the page shows (its 30-min
    cache: the page asked for them a moment ago), so the green line sits on
    the what-if's calendar and both sides read one risk-free rate: ^IRX in
    USD, 0 otherwise (analytics._daily_rf, the rule investments._risk uses).

    ``columns`` compares the whole window when the book covers it; when the
    window starts before the book, the book's column is None there and
    ``shared`` repeats the comparison on the dates both have."""
    from convexity.analytics import (
        _PERIOD_YF,
        _RF_TICKER,
        _WARMUP_YF,
        _bulk_close,
        _daily_rf,
        _relative,
        analyze_portfolios_multi,
    )
    from convexity.persistence import clean_trading, trading_prefs

    t0 = time.perf_counter()
    book_id = _expect(book_id, writes=False)
    period_u = str(period or "1Y").upper()
    period_u = period_u if period_u in _PERIOD_YF else "1Y"
    disp = major_ccy(ccy or "USD")
    tp = trading_prefs()
    reb, tc = clean_trading(tp["rebalance"] if rebalance is None else rebalance,
                            tp["tc_bps"] if tc_bps is None else tc_bps)  # fmt: skip
    rows = [r for r in rows or [] if isinstance(r, dict) and r.get("symbol")]
    if not rows or not isinstance(weights, dict):
        raise WhatIfError(400, "BadRequest", "rows[] and weights{} required")
    res = analyze_portfolios_multi(rows, {"whatif": weights}, period_u, disp, reb, tc * 1e4)
    a = res.get("whatif") if isinstance(res, dict) else None
    if not isinstance(a, dict) or a.get("error") or "error" in res:
        raise WhatIfError(422, "NoAnalytics", (a or res or {}).get("error") or "no analytics")
    page = _page(book_id, disp)
    st = _status(page)
    out: dict = {"status": st, "book_id": book_id, "rev": page.get("rev"), "period": period_u,
                 "ccy": disp, "rf_source": (a.get("stats") or {}).get("rf_source")}  # fmt: skip
    if st != "ok" or not (page.get("series") or {}).get("twr_index"):
        out["ms"] = round((time.perf_counter() - t0) * 1000)
        return out

    P = _points_series((a.get("series") or {}).get("portfolio"))
    ser = page["series"]
    twr = pd.Series(ser["twr_index"], index=pd.DatetimeIndex(ser["dates"]))
    on = twr.reindex(twr.index.union(P.index)).ffill().reindex(P.index)
    have = on.notna() & (on > 0)
    rf_wide = _bulk_close([_RF_TICKER], _WARMUP_YF[period_u]) if disp == "USD" else pd.DataFrame()
    spy = _points_series(((a.get("benchmarks") or {}).get("SPY") or {}).get("series"))
    spy = spy.reindex(spy.index.union(P.index)).ffill().reindex(P.index).dropna()
    if not have.any() or len(P) < 3:
        out.update(line=None, columns=None, shared=None)
        return out
    d0 = P.index[have.to_numpy().argmax()]
    covers = d0 == P.index[0]
    g = P.loc[d0] * on.loc[d0:] / on.loc[d0]
    out["line"] = {
        "points": [[int(ts.timestamp() * 1000), round(float(v), 6)] for ts, v in g.items()],
        "starts": str(d0.date()),
        "mid_chart": not covers,
        "book_first": ser["dates"][0],
    }

    def side(index_from):
        idx = P.loc[index_from:].index
        rf = _daily_rf(rf_wide, idx, disp)
        s_spy = spy.loc[index_from:]
        spy_ret = s_spy.pct_change().dropna() if len(s_spy) > 2 else None
        wi, bk = P.loc[index_from:], g.loc[index_from:] if index_from >= d0 else None
        blk = {"since": str(idx[0].date()), "whatif": _block(wi, rf, spy_ret),
               "book": _block(bk, rf, spy_ret) if bk is not None else None,
               "spy": _block(s_spy, rf, None) if len(s_spy) > 2 else None}  # fmt: skip
        if bk is not None and len(bk) > 2:
            # How far apart the two lines moved: the realised TE between them.
            blk["between"] = _pick(_relative(bk.pct_change().dropna(), wi.pct_change().dropna()),
                                   _REL_KEYS)  # fmt: skip
        return blk

    out["columns"] = side(P.index[0])
    out["shared"] = None if covers else side(d0)
    out["trading"] = a.get("trading")
    out["book_total"] = _book_total(page, rows, period_u, disp, reb, tc)
    out["ms"] = round((time.perf_counter() - t0) * 1000)
    return out


def _book_total(
    page: dict, rows: list[dict], period_u: str, disp: str, reb: str, tc: float
) -> dict:
    """The red tab's Book-mode total row (analytics._holdings_totals with the
    live book weights, as traded): the second total row on a what-if tab.
    Rows come from the request (fresh) and the red tab's saved rows for the
    book names the what-if no longer has; a book name with neither is left
    out and named."""
    from convexity.analytics import analyze_portfolios_multi

    weights = book_weights(page)
    by_sym = {r["symbol"]: r for r in rows if r.get("symbol") in weights}
    saved = (persistence.load_view(BOOK_VIEW) or {}).get("rows") or []
    for r in saved:
        if isinstance(r, dict) and r.get("symbol") in weights and r["symbol"] not in by_sym:
            by_sym[r["symbol"]] = r
    missing = [s for s in weights if s not in by_sym]
    if not by_sym:
        return {"totals": None, "missing": missing}
    res = analyze_portfolios_multi(list(by_sym.values()), {"book": weights}, period_u, disp, reb,
                                   tc * 1e4, as_traded=True)  # fmt: skip
    b = res.get("book") if isinstance(res, dict) else None
    if not isinstance(b, dict) or b.get("error"):
        return {"totals": None, "missing": missing, "error": (b or {}).get("error")}
    return {
        "totals": b.get("totals"),
        "stats": _pick(b.get("stats") or {}, _STATS_KEYS),
        "weights": weights,
        "n_holdings": len(weights),
        "missing": missing,
    }


# ------------------------------------------------------- the rebalance


def _sigma(symbols: list[str], qccy: dict, names: dict, disp: str, span: str):
    """Ledoit-Wolf on the last year of daily returns in the display currency,
    as traded, annualised: the recipe of the book's own Risk (analytics.
    _risk_returns: short listings extended by the same company elsewhere). A
    name with under 60 returns gets its own volatility (from 20 returns) or
    the median, and the average correlation: a young listing must not cut
    every other name's window to its few days. Cached ten minutes."""
    from convexity import mpt
    from convexity.analytics import _bulk_close, _period_start, extend_history
    from convexity.fx import _apply_fx_to_closes

    key = (tuple(symbols), disp, span, date.today().isoformat())
    with _SIGMA_LOCK:
        hit = _SIGMA.get(key)
        if hit and time.monotonic() - hit[0] < _SIGMA_TTL:
            return hit[1], hit[2]
    m = len(symbols)
    raw = _bulk_close(symbols, span, adjusted=False)
    info = {"n_obs": 0, "short": list(symbols), "stand_ins": {}}
    rets = pd.DataFrame()
    if raw is not None and not raw.empty:
        raw, done = extend_history(raw, symbols, qccy, names, span)
        info["stand_ins"] = {s: p for s, (p, _) in done.items()}
        raw = _apply_fx_to_closes(raw, {s: {"currency": qccy.get(s)} for s in symbols}, disp, span)
        cols = [s for s in symbols if s in raw.columns]
        frame = raw[cols].dropna(how="all").ffill()
        if len(frame):
            rets = (
                frame.loc[_period_start("1Y", frame.index[-1]) :]
                .pct_change(fill_method=None)
                .iloc[1:]
            )
    n_row = len(rets)
    obs = {s: int(rets[s].notna().sum()) if s in rets.columns else 0 for s in symbols}
    good = [s for s in symbols if obs[s] >= max(_MIN_OBS, int(0.8 * n_row))
            and float(rets[s].abs().sum()) > 0]  # fmt: skip
    r = rets[good].dropna(how="any") if good else pd.DataFrame()
    S = np.zeros((m, m))
    if len(good) >= 2 and len(r) >= _MIN_OBS:
        cov = mpt.annualized_cov(r, "daily", "ledoit").to_numpy()
        sd = np.sqrt(np.diag(cov))
        corr = cov / np.outer(sd, sd)
        rbar = float(corr[~np.eye(len(good), dtype=bool)].mean())
        med = float(np.median(sd))
    else:  # no usable history at all: a flat 25% / 0.5 model, said in ``info``
        cov, sd, rbar, med = None, None, 0.5, 0.25
    pos = {s: k for k, s in enumerate(good)} if cov is not None else {}
    vol = np.empty(m)
    for i, s in enumerate(symbols):
        if s in pos:
            vol[i] = sd[pos[s]]
        elif obs[s] >= 20:
            vol[i] = float(rets[s].std() * math.sqrt(252))
        else:
            vol[i] = med
    for i, a_ in enumerate(symbols):
        for j, b_ in enumerate(symbols):
            if a_ in pos and b_ in pos:
                S[i, j] = cov[pos[a_], pos[b_]]
            else:
                S[i, j] = vol[i] * vol[j] * (1.0 if i == j else rbar)
    info.update(n_obs=len(r), short=[s for s in symbols if s not in pos])
    with _SIGMA_LOCK:
        _SIGMA[key] = (time.monotonic(), S, info)
    return S, info


def _target(p: dict, view: str | None) -> tuple[dict, str]:
    """The weights to rebalance to: sent, else a preset of the view by name,
    else its active preset."""
    w = p.get("weights")
    if isinstance(w, dict) and w:
        return w, "weights"
    entry = persistence.load_view(view) if view else {}
    presets = [x for x in entry.get("weight_presets") or [] if isinstance(x, dict)]
    want = p.get("preset") or entry.get("active_weight_preset")
    for x in presets:
        if x.get("name") == want and isinstance(x.get("weights"), dict):
            return x["weights"], f"preset:{want}"
    raise WhatIfError(400, "NoTarget", "Choose the weights to rebalance to.")


def _num(v, default: float, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return min(max(f, lo), hi) if math.isfinite(f) else default


def rebalance_payload(p: dict) -> dict:
    """POST /api/whatif/rebalance: the trades from the showing book to the
    target. Both sides valued at the page's closes (one snapshot): held names
    at the page's price and the rate implied by its own value; new names at
    their last close as traded, at the rate a held name of the same currency
    implies (else today's). Read-only."""
    from convexity.analytics import _bulk_close
    from convexity.fx import convert_amount
    from convexity.persistence import trading_prefs

    t0 = time.perf_counter()
    book_id = _expect(p.get("book_id"), writes=False)
    view = p.get("view") if isinstance(p.get("view"), str) else None
    page = _page(book_id, p.get("ccy") if isinstance(p.get("ccy"), str) else None)
    _ready(page)
    disp = page["ccy"]
    target_raw, target_src = _target(p, view)
    target = {}
    for s, w in target_raw.items():
        f = _num(w, 0.0, 0.0, 1e9)
        if isinstance(s, str) and s.strip():
            target[s.strip().upper()] = target.get(s.strip().upper(), 0.0) + f
    if sum(target.values()) <= 0:
        raise WhatIfError(400, "NoTarget", "The target has no weight.")
    held = {x["symbol"]: x for x in _held(page)}
    rows = {r["symbol"]: r for r in (persistence.load_view(view) or {}).get("rows") or []
            if isinstance(r, dict) and isinstance(r.get("symbol"), str)} if view else {}  # fmt: skip
    new = [s for s, w in target.items() if w > 0 and s not in held]
    dates = (page.get("series") or {}).get("dates") or [date.today().isoformat()]
    span = investments._span(dates[1] if len(dates) > 1 else dates[0])
    qccy = {s: x.get("quote_ccy") or "USD" for s, x in held.items()}
    for s in new:
        qccy[s] = (rows.get(s) or {}).get("currency") or investments._QCCY.get(s) \
            or investments._fetch_quote_ccy(s) or "USD"  # fmt: skip
    # The rate each currency's held names imply (value / shares x major
    # price): exactly the page's, so a new name in a held currency is valued
    # on the same footing as the rest.
    implied: dict[str, float] = {}
    lines: list[rb.Line] = []
    for s, x in held.items():
        major = price_in_major(x.get("price"), x.get("quote_ccy"))
        fx = x["value"] / (x["shares"] * major) if major and x.get("shares") else None
        if fx and not x.get("note"):
            implied.setdefault(major_ccy(x.get("quote_ccy")), fx)
        lines.append(rb.Line(s, float(x.get("shares") or 0.0), None if x.get("note") else x.get("price"),
                             x.get("quote_ccy") or "USD", fx, x.get("price_date"),
                             x.get("profit_held"), x["value"], x.get("name")))  # fmt: skip
    if new:
        closes = _bulk_close(new, span, adjusted=False)
        for s in new:
            col = (
                closes[s].dropna()
                if closes is not None and s in getattr(closes, "columns", ())
                else None
            )
            last = float(col.iloc[-1]) if col is not None and len(col) else None
            when = str(col.index[-1])[:10] if col is not None and len(col) else None
            mc = major_ccy(qccy[s])
            fx = implied.get(mc) or (1.0 if mc == disp else convert_amount(1.0, mc, disp))
            name = (rows.get(s) or {}).get("name") or s
            lines.append(rb.Line(s, 0.0, last, qccy[s], fx, when, None, None, name))
    syms = [ln.symbol for ln in lines]
    names = {ln.symbol: ln.name for ln in lines}
    S, info = _sigma(syms, qccy, names, disp, span)
    mode = p.get("mode") if p.get("mode") in rb.MODES else "shares"
    opts = rb.Opts(
        mode=mode,
        rounding=p.get("rounding") if p.get("rounding") in rb.ROUNDINGS else "shares",
        step=_num(p.get("step"), 100.0, 0.01, 1e9),
        min_trade=_num(p.get("min_trade"), 100.0, 0.0, 1e9),
        cash="invest" if p.get("cash") == "invest" else "keep",
        reserve=_num(p.get("reserve"), 0.0, 0.0, 1e12),
        tc_bps=trading_prefs()["tc_bps"],
        te_tol=_num(p.get("te_tol"), 0.005, 0.0, 1.0),
        n_trades=int(p["n_trades"])
        if isinstance(p.get("n_trades"), int) and not isinstance(p.get("n_trades"), bool)
        else None,  # fmt: skip
    )
    try:
        res = rb.rebalance(
            lines, target, float((page.get("headline") or {}).get("cash") or 0.0), S, opts
        )
    except rb.RebalanceError as exc:
        raise WhatIfError(400, "BadRequest", str(exc)) from exc
    warnings = list(res.warnings)
    gone = [s for s in held if target.get(s, 0.0) <= 0]
    locked = {x["symbol"] for x in res.locked}
    if short := [s for s in info["short"] if s not in locked]:
        warnings.append("Short price history for " + ", ".join(short[:6])
                        + ": their tracking error is estimated from the others' average.")  # fmt: skip
    as_of = max((ln.price_date for ln in lines if ln.price_date), default=None)
    return {
        "book_id": book_id,
        "rev": page.get("rev"),
        "ccy": disp,
        "computed_at": page.get("computed_at"),
        "prices_as_of": as_of,
        "target": target_src,
        "mode": mode,
        "options": {
            "step": opts.step,
            "min_trade": opts.min_trade,
            "cash": opts.cash,
            "reserve": opts.reserve,
            "tc_bps": opts.tc_bps,
            "te_tol": opts.te_tol,
            "rounding": opts.rounding,
            "n_trades": opts.n_trades,
        },  # fmt: skip
        "trades": res.trades,
        "summary": res.summary,
        "path": res.path,
        "warnings": warnings,
        "locked": res.locked,
        "not_in_target": gone,
        "risk_model": {
            "n_obs": info["n_obs"],
            "short": info["short"],
            "stand_ins": info["stand_ins"],
        },
        "ms": round((time.perf_counter() - t0) * 1000),
    }


# ------------------------------------------- the optimizer plug's weight set


def save_optimized(p: dict) -> dict:
    """POST /api/weight-presets/optimized: a point of a SAVED run as a weight
    set ("Optimize and save as a set" in the Weight Center). The point comes
    from the server's copy of the run, never the request: ``rule`` same_risk
    or min_cvar picks it (frontier.pick_points), selected takes ``index``.
    Names the run left out keep their current weight (``held_out``), the set
    is cleaned and named by frontier.set_from_point / set_name, and the
    provenance goes in its allow-listed ``source`` block. Nothing is saved
    when anything is missing."""
    from convexity import frontier

    view = str(p.get("view") or "").strip()
    rule = p.get("rule")
    if not view or rule not in ("same_risk", "min_cvar", "selected"):
        raise WhatIfError(
            400, "BadRequest", "view and rule (same_risk, min_cvar, selected) required"
        )
    run = next((r for r in persistence.get_mpt_runs(view) if r.get("id") == p.get("run_id")), None)
    if run is None:
        raise WhatIfError(
            404, "RunGone", "That optimizer run isn't saved any more. Nothing was saved."
        )
    fr = [x for x in run.get("frontier") or [] if isinstance(x, dict)]
    current = (run.get("anchors") or {}).get("current") or {}
    warnings = []
    if rule == "selected":
        idx = p.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(fr):
            raise WhatIfError(400, "BadRequest", "index must be a point of the run")
    else:
        picks = run.get("picks") if isinstance(run.get("picks"), dict) else None
        picks = picks or frontier.pick_points(fr, current)
        pick = picks.get(rule) if isinstance(picks.get(rule), dict) else None
        idx = pick.get("index") if pick else None
        if not isinstance(idx, int) or not 0 <= idx < len(fr):
            raise WhatIfError(422, "NoPoint", "The run has no such point. Nothing was saved.")
        if isinstance(pick.get("note"), str):
            warnings.append(pick["note"])
        elif pick.get("fallback"):  # a run saved before the note existed
            warnings.append(frontier.SAME_RISK_FALLBACK_NOTE)
    point = fr[idx]
    held = run.get("held_out")
    if not isinstance(held, list):  # a run saved before Phase 4
        syms = list(run.get("symbols") or [])
        held = frontier._held_out(
            p.get("current_weights"), syms + list(run.get("missing") or []), syms
        )
    try:
        weights = frontier.set_from_point(point.get("weights"), held)
    except ValueError as exc:
        raise WhatIfError(422, "NoPoint", f"{exc}. Nothing was saved.") from exc
    if int((run.get("meta") or {}).get("n_nonconv") or 0) > 0 or any(
        "iteration cap" in str(w) for w in (run.get("meta") or {}).get("warnings") or []
    ):
        warnings.append("Some frontier points hit the solver's iteration cap: "
                        "the figures may be slightly off.")  # fmt: skip
    taken = [x.get("name") for x in persistence.list_weight_presets(view)["presets"]]
    name = frontier.set_name(rule, taken)
    source = {
        "kind": "optimizer", "rule": rule, "index": idx, "n_points": len(fr),
        "run_id": run.get("id"), "params": run.get("params") or {},
        "point": point, "current": current, "held_out": held,
        "computed_at": run.get("saved_at"), "warnings": warnings,
    }  # fmt: skip
    try:
        out = persistence.upsert_weight_preset(view, name, weights, set_active=p.get("set_active", True) is not False,
                                               source=source)  # fmt: skip
    except ValueError as exc:
        raise WhatIfError(404, "NoPortfolio", f"{exc}. Nothing was saved.") from exc
    return {
        **out,
        "name": name,
        "weights": weights,
        "held_out": held,
        "index": idx,
        "warnings": warnings,
    }
