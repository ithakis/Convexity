"""The Market read — how prices have historically reacted to news like this.

A statistical model trained on FNSPID's 4.3M symbol-tagged headlines
(2009-2023) against what the stocks then did: the vol-standardized,
beta-adjusted abnormal return ("SAR"). Shipped as mlsent-v1.1:

1. Encoder: the v1 per-article LightGBM (hashed TF-IDF + a dense block,
   ml_features.dense_vector) predicts each headline's SAR.
2. Score: the recency x source x novelty x relevance weighted mean of those
   predictions over the ticker's 7-day window (ml_features.weighted_sar over
   relevance.window_sample's capped set — the SAME functions the calibration
   panel in ml/scripts/06 used). Horizon: the next trading day.
3. Placement: z and a percentile against the score's own history, tiers by
   percentile (<=5 very bearish, <=15 bearish, 15-85 "no edge", >=85 bullish,
   >=95 very bullish — only the tails carried signal in the backtest).

Why the percentile is LIVE-anchored: a weak regressor's output LEVEL drifts —
with the booster, the year, and the news source. Knots fixed on 2023 data
missed the +/-3pp tier-mass gate on every later window tried (7.6pp even on
the same booster three months on; 48pp across a year), and the live app reads
Finnhub, not FNSPID. So once the app has scored MIN_LIVE_HISTORY tickers in
the last LIVE_WINDOW_DAYS, a score is ranked against those; until then
against the Jul-Sep 2023 knots shipped in tier_cuts.json. The dict says which
(`anchor`). The v2 ticker-day retrain failed its gates and is not shipped;
the evidence is in docs/ml_sentiment_design.md.

Graceful degradation is the contract (same as finnhub_adapter): a missing
artifact, missing lightgbm/sklearn/scipy, or a schema mismatch make
available() False and every call return None — the News read is unaffected.
Graceful must not mean SILENT: runtime_status() reports the real reason and
the Track record surfaces it.

Artifact bundle (ml/scripts/07 + 09 + 10), <data>/models/<ver>/ (paths.py):
    model.lgbm.txt        encoder Booster
    idf.npy, col_mask.npy train-fitted idf + df-pruning column mask
    feature_schema.json   must equal ml_features.feature_schema()
    tier_cuts.json        bootstrap mu/sigma/knots, tier cuts, expected-SAR table
    meta.json             provenance and gate results
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from convexity import paths

ARTIFACT_VERSION = "mlsent-v1.1"

# Live anchoring: rank against the app's own recent Market read scores once
# there are enough of them (one per ticker per refresh day). 200 gives the 5%
# tails ten observations each; 90 days keeps the reference current.
MIN_LIVE_HISTORY = 200
LIVE_WINDOW_DAYS = 90

_LOCK = threading.Lock()
_STATE: dict = {"loaded": False, "ok": False, "reason": ""}

# Evidence-mass -> confidence: conf = 1 - exp(-wsum / _CONF_SCALE). DISPLAY
# ONLY — never a model input. 3.0 was measured, not guessed: over 124 real
# ticker-days the 7-day weight mass ran p10=1.17 p50=3.34 p90=5.86, which 3.0
# maps to ~32% / 67% / 86% (2.0 and 0.35 were both earlier wrong guesses).
_CONF_SCALE = 3.0

TIERS = ("very_bearish", "bearish", "no_edge", "bullish", "very_bullish")

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone(timedelta(hours=-5))


def model_dir() -> Path:
    """``MLSENT_MODEL_DIR``, else ``<data>/models/<version>`` (paths.py).

    One-release fallback (logged once): the pre-1.14 ``~/.convexity/ml_model``
    location, until migrate.py has moved it. When neither exists the data-dir
    path is returned, so the "missing artifact" reason names the new place."""
    env = os.environ.get("MLSENT_MODEL_DIR")
    if env:
        return Path(env)
    new = paths.models_dir() / ARTIFACT_VERSION
    if new.exists():
        return new
    legacy = paths.legacy_model_root() / ARTIFACT_VERSION
    if legacy.exists():
        paths.note_legacy("ML model dir", legacy)
        return legacy
    return new


def _load() -> dict:
    """Lazy singleton. Never raises — failure marks the model unavailable.

    `loaded` is published LAST, in a finally: the fast path reads it without
    the lock, so publishing it before the ~3 s import would let a concurrent
    caller see {loaded: True, ok: False, reason: ""} and silently skip the
    Market read (which is exactly what once produced "model not loaded" with
    no real failure behind it). A late caller now blocks on _LOCK instead.
    """
    if _STATE["loaded"]:
        return _STATE
    with _LOCK:
        if _STATE["loaded"]:
            return _STATE
        d = model_dir()
        try:
            import numpy as np
            import lightgbm as lgb
            # Imported here so available() is HONEST: scoring needs both
            # (scipy.sparse.hstack; sklearn via ml_features.hash_counts).
            import importlib
            for mod in ("scipy.sparse", "sklearn"):
                importlib.import_module(mod)

            from convexity import ml_features as mf

            if json.loads((d / "feature_schema.json").read_text()) != mf.feature_schema():
                raise RuntimeError("encoder feature schema differs from ml_features.py "
                                   "— retrain or redeploy the bundle")
            _STATE["encoder"] = lgb.Booster(model_file=str(d / "model.lgbm.txt"))
            _STATE["idf"] = np.load(d / "idf.npy")
            _STATE["mask"] = np.load(d / "col_mask.npy")
            cal = json.loads((d / "tier_cuts.json").read_text())
            knots = [float(k) for k in cal["pct_knots"]]
            if len(knots) != 101 or any(b < a for a, b in zip(knots, knots[1:])):
                raise RuntimeError("tier_cuts.json: pct_knots must be 101 non-decreasing values")
            _STATE["cal"] = cal
            _STATE["ok"] = True
            _STATE["reason"] = ""
            print(f"[ml_sentiment] loaded {ARTIFACT_VERSION} from {d}")
        except Exception as e:
            _STATE["ok"] = False
            _STATE["reason"] = f"{type(e).__name__}: {e}"
            print(f"[ml_sentiment] Market read unavailable ({type(e).__name__}: {e})")
        finally:
            # Publish LAST — see the docstring. Never move this above the try.
            _STATE["loaded"] = True
        return _STATE


def available() -> bool:
    return _load()["ok"]


def runtime_status() -> dict:
    """Why the Market read is (not) running. The load is cached for the
    process lifetime, so installing a dependency needs an app restart."""
    st = _load()
    reason = st.get("reason") or ""
    from convexity import model_fetch  # stdlib + paths only; no cycle
    download = model_fetch.status()
    exists = model_dir().exists()
    missing = model_fetch.needed()  # no COMPLETE artifact (a folder alone is not enough)
    if not st["ok"] and missing:
        # The generic "No such file" says nothing useful on a fresh install:
        # name what the first-run download is doing (Track record shows this).
        state = download.get("state")
        if state == "failed":
            reason = f"model download failed: {download.get('error')}"
        elif state in model_fetch.IN_FLIGHT:
            reason = "model is downloading"
        elif state == "disabled":
            reason = "model not installed (automatic download disabled)"
    return {
        "available": bool(st["ok"]),
        "reason": reason,
        "model_dir": str(model_dir()),
        "model_dir_exists": exists,
        "model_missing": missing,
        "version": ARTIFACT_VERSION,
        "download": download,
    }


# ---------------------------------------------------------------- price context
def load_closes(symbols: list[str]):
    """Daily adjusted closes for `symbols` + SPY (6 months, cached by
    analytics). One download serves a whole refresh."""
    from convexity.analytics import _bulk_close

    try:
        df = _bulk_close(sorted(set(symbols) | {"SPY"}), "6M")
    except Exception as exc:
        print(f"[ml_sentiment] price history unavailable: {type(exc).__name__}: {exc}")
        return None
    return df if df is not None and not df.empty else None


def _series(closes, symbol):
    if closes is None or symbol not in getattr(closes, "columns", ()):
        return None
    s = closes[symbol].dropna()
    return s if len(s) else None


def _returns(values):
    return [values[i] / values[i - 1] - 1.0 for i in range(1, len(values))
            if values[i - 1]]


def _std(xs):
    if len(xs) < 2:
        return math.nan
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def price_features(closes, symbol: str, today=None) -> dict:
    """ml_features.PRICE_COLUMNS as of the last COMPLETED session before
    `today` (ET) — the training panel's as-of-D-1 convention (05 stage D)."""
    today = today or datetime.now(_ET).date()
    out = {c: math.nan for c in ("tkr_ret_1d", "tkr_ret_5d", "tkr_ret_20d",
                                 "tkr_vol_20d", "spy_ret_5d", "spy_vol_20d")}
    s, spy = _series(closes, symbol), _series(closes, "SPY")
    if s is not None:
        c = [float(v) for d, v in zip(s.index, s.values) if d.date() < today]
        if len(c) >= 2:
            out["tkr_ret_1d"] = c[-1] / c[-2] - 1.0
        if len(c) >= 6:
            out["tkr_ret_5d"] = c[-1] / c[-6] - 1.0
        if len(c) >= 21:
            out["tkr_ret_20d"] = c[-1] / c[-21] - 1.0
        r = _returns(c[-21:])
        if len(r) >= 15:
            out["tkr_vol_20d"] = _std(r[-20:])
    if spy is not None:
        c = [float(v) for d, v in zip(spy.index, spy.values) if d.date() < today]
        m = _returns(c[-21:])
        if len(m) >= 5:
            out["spy_ret_5d"] = math.prod(1.0 + x for x in m[-5:]) - 1.0
        if len(m) >= 20:
            out["spy_vol_20d"] = _std(m[-20:])
    return out


def _article_context(closes, symbol: str, day) -> dict:
    """The encoder's market-context inputs for an article dated `day`, as the
    training enrichment built them (ml/scripts/06 enrich): SPY 1d/5d return
    and 20d vol at D0 (the last session <= day), ticker 5d return at D0."""
    out = {"spy_ret_1d": 0.0, "spy_ret_5d": 0.0, "spy_vol_20d": 0.0, "tkr_ret_5d": 0.0}
    spy = _series(closes, "SPY")
    if spy is not None:
        c = [float(v) for d, v in zip(spy.index, spy.values) if d.date() <= day]
        m = _returns(c[-21:])
        if m:
            out["spy_ret_1d"] = m[-1]
        if len(m) >= 5:
            out["spy_ret_5d"] = math.prod(1.0 + x for x in m[-5:]) - 1.0
        if len(m) >= 20:
            out["spy_vol_20d"] = _std(m[-20:])
    s = _series(closes, symbol)
    if s is not None:
        c = [float(v) for d, v in zip(s.index, s.values) if d.date() <= day]
        if len(c) >= 6 and c[-6]:
            out["tkr_ret_5d"] = c[-1] / c[-6] - 1.0
    return out


# ---------------------------------------------------------------- scoring
def score_articles(articles: list[dict], symbol: str, closes=None) -> list[dict]:
    """Encoder pass over a window's articles -> window items (one predict call).

    Every live article is encoded as `dateonly_cc`: 98.5% of FNSPID rows carry
    a date without a time, so the encoder learned timed-session effects from
    1.5% of its data — feeding real sessions would put every live article in a
    sparsely-trained branch."""
    st = _load()
    if not st["ok"] or not articles:
        return []
    import numpy as np
    import scipy.sparse as sp

    from convexity import ml_features as mf
    from convexity.relevance import is_boilerplate, load_company_names, relevance_score

    name = load_company_names().get((symbol or "").upper())
    ctx_by_day: dict = {}
    dense_rows, texts, items = [], [], []
    for a in articles:
        title, summary = a.get("headline") or "", a.get("summary") or ""
        related = a.get("related") or ""
        n_co = max(1, len([t for t in str(related).split(",") if t.strip()]))
        tier = mf.publisher_tier(a.get("source"))
        rel = relevance_score(title, summary, symbol, name, co_mention_count=n_co,
                              publisher_tier=tier)
        ts = a.get("datetime") or time.time()
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_ET)
        if dt.date() not in ctx_by_day:
            ctx_by_day[dt.date()] = _article_context(closes, symbol, dt.date())
        row = mf.dense_vector({
            "title": title, "summary": summary, "publisher_tier": tier, "relevance": rel,
            "n_duplicates": a.get("n_duplicates", 0), "co_mention_count": n_co,
            "session_class": "dateonly_cc", "day_of_week": dt.isoweekday(),
            "month": dt.month, **ctx_by_day[dt.date()]})
        dense_rows.append(row)
        texts.append(mf.text_for_hashing(title, summary))
        lm_i = mf.DENSE_COLUMNS.index("lm_score")
        miss_i = mf.DENSE_COLUMNS.index("lm_missing")
        items.append({"lm": None if row[miss_i] else row[lm_i],
                      "unc": row[mf.DENSE_COLUMNS.index("uncertainty_ratio")],
                      "source": a.get("source"), "n_duplicates": a.get("n_duplicates", 0),
                      "relevance": rel, "boiler": is_boilerplate(title), "datetime": ts})
    tfidf = mf.apply_idf(mf.hash_counts(texts), st["idf"])[:, st["mask"]]
    X = sp.hstack([tfidf, sp.csr_matrix(np.asarray(dense_rows, dtype="float32"))],
                  format="csr")
    for it, p in zip(items, st["encoder"].predict(X)):
        it["sar_pred"] = float(p)
    return items


def calibrate(score: float, cal: dict, history: list[float] | None = None) -> dict:
    """Place a raw score on its own history: z, percentile, tier and the
    expected SAR (the realized mean SAR of that percentile band in the
    calibration window, isotonic).

    `history` — recent live scores. With at least MIN_LIVE_HISTORY of them the
    percentile is the score's mid-rank among them and z uses their mean/sd
    (anchor "live"); otherwise the artifact's knots (anchor "training").
    ml/scripts/09 verifies the training-anchored path through this function."""
    import numpy as np

    hist = [float(h) for h in (history or []) if h is not None and math.isfinite(float(h))]
    if len(hist) >= MIN_LIVE_HISTORY:
        arr = np.asarray(hist)
        sd = float(arr.std())
        z = (float(score) - float(arr.mean())) / sd if sd > 0 else 0.0
        below = float((arr < score).sum()) + 0.5 * float((arr == score).sum())
        pct = 100.0 * below / len(arr)
        anchor = "live"
    else:
        z = (float(score) - cal["mu"]) / cal["sigma"]
        knots = np.asarray(cal["pct_knots"], dtype="float64")
        # np.interp needs strictly increasing x; flat runs (ties) collapse
        xs, idx = np.unique(knots, return_index=True)
        pct = float(np.interp(z, xs, np.arange(101, dtype="float64")[idx]))
        anchor = "training"
    lo, lo2, hi2, hi = cal["tier_pct"]
    tier = ("very_bearish" if pct <= lo else "bearish" if pct <= lo2
            else "no_edge" if pct < hi2 else "bullish" if pct < hi else "very_bullish")
    edges, sars = cal["exp_sar"]["pct_edges"], cal["exp_sar"]["sar"]
    k = min(len(sars) - 1, max(0, int(np.searchsorted(edges, pct, side="right")) - 1))
    return {"z": round(z, 2), "pct": round(pct, 1), "tier": tier,
            "sar": round(float(sars[k]), 3), "anchor": anchor, "n_history": len(hist)}


def market_read(symbol: str, articles: list[dict], closes=None, now: float | None = None,
                history: list[float] | None = None) -> tuple[dict | None, float | None]:
    """The Market read for one ticker from its 7-day articles.

    Returns (market dict, the stock's 20d vol) — the vol feeds the "sold the
    news" divergence check. market dict:
      {sar, z, pct, tier, anchor, n_history, score, horizon_days, confidence,
       n_articles, model_version, assessed_at}
    `score` is the raw weighted SAR — the history the next reads rank against.
    """
    st = _load()
    if not st["ok"] or not articles:
        return None, None
    try:
        from convexity import ml_features as mf
        from convexity.relevance import window_sample

        now = float(now) if now is not None else time.time()
        # The calibration panel's window is articles dated D-6..D (ET days),
        # capped exactly like the app's retained set.
        first = (datetime.fromtimestamp(now, tz=timezone.utc).astimezone(_ET).date()
                 - timedelta(days=mf.WINDOW_DAYS - 1))
        recent = [a for a in sorted(articles, key=lambda a: a.get("datetime") or 0,
                                    reverse=True)
                  if a.get("datetime") and datetime.fromtimestamp(
                      a["datetime"], tz=timezone.utc).astimezone(_ET).date() >= first]
        recent = window_sample(recent, mf.WINDOW_CAP, mf.WINDOW_MIN_RECENT)
        items = score_articles(recent, symbol, closes)
        score, wsum = mf.weighted_sar(items, now)
        if score is None:
            return None, None
        cal = calibrate(score, st["cal"], history)
        vol = price_features(closes, symbol).get("tkr_vol_20d")
        return {
            **cal,
            "score": round(score, 6),
            "horizon_days": int(st["cal"]["horizon_days"]),
            "confidence": round(1.0 - math.exp(-wsum / _CONF_SCALE), 3),
            "n_articles": len(items),
            "model_version": ARTIFACT_VERSION,
            "assessed_at": datetime.now(timezone.utc).isoformat(),
        }, (vol if isinstance(vol, float) and math.isfinite(vol) else None)
    except Exception as e:
        print(f"[ml_sentiment] market_read failed for {symbol}: {type(e).__name__}: {e}")
        return None, None


def reload() -> dict:
    """Forget the cached load and load again. model_fetch calls this once a
    downloaded artifact is in place — otherwise the "missing artifact" failure
    cached at boot would hide the new model until a restart.

    A model that already loaded is left alone: scorers hold references into
    _STATE, and clearing it under them would turn a no-op into a KeyError."""
    # Under _LOCK: a load already in progress (the boot warm-up) finishes
    # first, and if it found the new files there is nothing to redo.
    with _LOCK:
        if _STATE.get("loaded") and _STATE.get("ok"):
            return _STATE
        _STATE.clear()
        _STATE.update({"loaded": False, "ok": False, "reason": ""})
    return _load()


def reset_for_tests() -> None:
    """Forget the loaded model so a new MLSENT_MODEL_DIR (or a freshly
    downloaded artifact) applies on the next _load()."""
    with _LOCK:
        _STATE.clear()
        _STATE.update({"loaded": False, "ok": False, "reason": ""})
