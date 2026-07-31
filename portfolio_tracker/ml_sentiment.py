"""Production inference for the ML news-sentiment model (MLNews pipeline).

Loads the trained LightGBM artifact and scores live Finnhub/yfinance articles
into predicted SAR (vol-standardized, beta-adjusted abnormal return), then
aggregates per-ticker into ml_* fields that ride alongside the existing LLM
fields in the sentiment dict.

Graceful degradation is the contract (same as finnhub_adapter): missing
artifact, missing lightgbm/sklearn, or a feature-schema mismatch all make
available() False and every scoring call return None — the LLM path is never
affected. The artifact lives OUTSIDE the repo at ~/.portfolio_tracker/ml_model/
(override with MLSENT_MODEL_DIR); deploy = copy the mlsent-v1 bundle there.

Graceful must not mean SILENT, which is what it was: the desktop app's `pt`
conda env shipped without lightgbm/scikit-learn (environment.yml listed neither),
so the model was dead in every installed copy while the UI merely showed the LLM
fallback. runtime_status() now reports the real reason and the News tab surfaces
it. Both packages are required at load time so available() cannot be True while
every scoring call returns None.

Artifact bundle (produced by ml/scripts/07+09+10):
    model.lgbm.txt        LightGBM Booster
    idf.npy               float32[2**18] idf vector (train-fitted)
    feature_schema.json   must match ml_features.feature_schema() exactly
    tier_cuts.json        score thresholds for the 5 tiers
    meta.json             provenance (git sha, metrics, train window)
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ARTIFACT_VERSION = "mlsent-v1"

_LOCK = threading.Lock()
_STATE: dict = {"loaded": False, "ok": False, "reason": ""}

# Recency half-life for aggregation (matches the app's default tau).
_TAU_DAYS = 3.0

# Evidence-mass -> confidence decay: conf = 1 - exp(-wsum / _CONF_SCALE).
# DISPLAY ONLY — ml_confidence is never a model input and never touches the
# trained artifact, so retuning this does not invalidate mlsent-v1.
# History: was 2.0, calibrated from a guess ("a realistic 3-5 article ticker
# has wsum ~0.2-0.5") that turned out to be off by an order of magnitude — a
# real ticker rendered as "Confidence 6%". A same-day fix dropped it to 0.35,
# but that guess was ALSO wrong in the other direction: measured directly
# against 124 real ticker-days in this app's own history (7-day lookback,
# recency/source/novelty/relevance weights as actually computed), wsum's
# real distribution is p10=1.17 p50=3.34 p90=5.86 (median ~20 articles/week
# for an actively-covered name) — under 0.35, 118/124 (95%) records pinned to
# 90-100% confidence, including the thinnest-coverage ticker in the set.
# 3.0 was chosen by sweeping candidate scales against that same measured
# wsum distribution and picking the one with a real spread instead of a
# ceiling: p10->=32%, p50->=67%, p90->=86%. Re-derive this the same way
# (recompute wsum per real ticker-day, sweep SCALE, pick for spread) if the
# news volume/mix in the cache changes meaningfully — don't re-guess it.
_CONF_SCALE = 3.0

# US/Eastern offset approximation for session classification. DST-correct
# conversion needs zoneinfo — used when available, fixed -5 fallback otherwise.
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone(timedelta(hours=-5))


def model_dir() -> Path:
    env = os.environ.get("MLSENT_MODEL_DIR")
    if env:
        return Path(env)
    return Path.home() / ".portfolio_tracker" / "ml_model" / ARTIFACT_VERSION


def _load() -> dict:
    """Lazy singleton. Never raises — failure marks the model unavailable.

    `loaded` is published LAST, in a finally. It used to be set before the ~3 s
    lightgbm/scipy/sklearn import, while the fast path below reads it without
    the lock — so any thread arriving mid-load (the refresh pool's 2 workers vs.
    /api/runtime-status, say) saw {loaded: True, ok: False, reason: ""} and quietly
    fell back to the LLM. That published-but-uninitialized window is what produced
    "model not loaded" in the log (news_sentiment's fallback string, not a real
    failure — a real one populates `reason`) and ML coverage of N-1 out of N.
    Publishing last means a late caller blocks on _LOCK until the load finishes
    and then reads the true result.
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
            # scipy + sklearn are imported here purely so available() is HONEST.
            # score_article needs both (scipy.sparse.hstack; sklearn via
            # ml_features.hash_counts/apply_idf) but used to import them lazily
            # at call time — so an env with lightgbm but no sklearn reported
            # available() == True and then returned None for every article.
            import scipy.sparse  # noqa: F401
            import sklearn  # noqa: F401

            from portfolio_tracker import ml_features as mf

            schema_disk = json.loads((d / "feature_schema.json").read_text())
            if schema_disk != mf.feature_schema():
                raise RuntimeError(
                    "feature schema mismatch between artifact and ml_features.py "
                    "— retrain or update the deployed bundle")
            _STATE["booster"] = lgb.Booster(model_file=str(d / "model.lgbm.txt"))
            _STATE["idf"] = np.load(d / "idf.npy")
            # Optional df-pruning column mask (top-k hash buckets, train-fitted).
            # Must be applied identically to training or predictions are garbage.
            mask_p = d / "col_mask.npy"
            _STATE["mask"] = np.load(mask_p) if mask_p.exists() else None
            _STATE["cuts"] = json.loads((d / "tier_cuts.json").read_text())
            _STATE["ok"] = True
            _STATE["reason"] = ""
            print(f"[ml_sentiment] loaded {ARTIFACT_VERSION} from {d}")
        except Exception as e:
            _STATE["ok"] = False
            _STATE["reason"] = f"{type(e).__name__}: {e}"
            print(f"[ml_sentiment] model unavailable ({type(e).__name__}: {e}) "
                  f"— ml_* fields disabled")
        finally:
            # Publish LAST — see the docstring. Never move this above the try.
            _STATE["loaded"] = True
        return _STATE


def available() -> bool:
    return _load()["ok"]


def runtime_status() -> dict:
    """Why ML is (not) running — surfaced in the News tab's Model Diagnostics.

    Without this the UI could only guess, and it guessed wrong: the old copy
    blamed a missing artifact when the real cause was a conda env with no
    lightgbm. Note the load is cached for the process lifetime (see _load), so
    installing the dependency requires an app restart before this flips.
    """
    st = _load()
    return {
        "available": bool(st["ok"]),
        "reason": st.get("reason") or "",
        "model_dir": str(model_dir()),
        "model_dir_exists": model_dir().exists(),
        "version": ARTIFACT_VERSION,
    }


def _session_class(epoch: int | None, now: float | None = None) -> str:
    """Approximate live session classification (no exchange calendar in-app:
    holidays land in a same-shape window; the feature is context, not label)."""
    if not epoch:
        return "dateonly_cc"
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(_ET)
    if dt.weekday() >= 5:
        return "overnight"
    mins = dt.hour * 60 + dt.minute
    if mins >= 16 * 60:
        return "overnight"
    if mins < 9 * 60 + 30:
        return "preopen_oc"
    return "intraday_cc"


def score_article(article: dict, symbol: str, context: dict | None = None) -> dict | None:
    """Predicted SAR + relevance for one live article (~1-3 ms).

    `article` uses the app's normalized fields: headline, summary, source,
    datetime (epoch s), n_duplicates, related. `context` may carry trailing
    market features {spy_ret_1d, spy_ret_5d, spy_vol_20d, tkr_ret_5d} — zeros
    (feature-neutral) when absent.
    """
    st = _load()
    if not st["ok"]:
        return None
    try:
        import numpy as np
        import scipy.sparse as sp

        from portfolio_tracker import ml_features as mf
        from portfolio_tracker.relevance import load_company_names, relevance_score

        ctx = context or {}
        title = article.get("headline") or ""
        summary = article.get("summary") or ""
        related = article.get("related") or ""
        n_co = max(1, len([s for s in str(related).split(",") if s.strip()])) if related else 1
        pub_tier = mf.publisher_tier(article.get("source"))
        rel = relevance_score(title, summary, symbol,
                              load_company_names().get((symbol or "").upper()),
                              co_mention_count=n_co, publisher_tier=pub_tier)
        ts = article.get("datetime") or 0
        dt = (datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_ET)
              if ts else datetime.now(_ET))
        art = {
            "title": title, "summary": summary,
            "publisher_tier": pub_tier, "relevance": rel,
            "n_duplicates": article.get("n_duplicates", 0),
            "co_mention_count": n_co,
            "session_class": _session_class(ts),
            "day_of_week": dt.isoweekday(),
            "month": dt.month,
            "spy_ret_1d": ctx.get("spy_ret_1d", 0.0),
            "spy_ret_5d": ctx.get("spy_ret_5d", 0.0),
            "spy_vol_20d": ctx.get("spy_vol_20d", 0.0),
            "tkr_ret_5d": ctx.get("tkr_ret_5d", 0.0),
        }
        counts = mf.hash_counts([mf.text_for_hashing(title, summary)])
        dense = np.asarray([mf.dense_vector(art)], dtype="float32")
        tfidf = mf.apply_idf(counts, st["idf"])
        if st.get("mask") is not None:
            tfidf = tfidf[:, st["mask"]]
        X = sp.hstack([tfidf, sp.csr_matrix(dense)], format="csr")
        sar = float(st["booster"].predict(X)[0])
        return {"sar_pred": round(sar, 4), "relevance": rel}
    except Exception as e:
        print(f"[ml_sentiment] score_article failed: {type(e).__name__}: {e}")
        return None


def _tier(sar: float, cuts: list[float], tiers: list[str]) -> str:
    i = 0
    for c in cuts:
        if sar > c:
            i += 1
    return tiers[i]


# Display anchors for display_score(). These are EXACTLY the thresholds
# app.js's nsTierFromScore() uses (-0.5 / -0.15 / +0.15 / +0.5), which is the
# whole point: pinning cuts[i] -> _DISP_ANCHORS[i] makes the client's tier
# bucketing of the displayed number agree with the server's ml_tier by
# construction. Before this they could not agree — the client applied +/-0.15
# to a number the trained cuts confined to about +/-0.008.
_DISP_ANCHORS = (-0.5, -0.15, 0.15, 0.5)


def display_score(sar: float | None, cuts: list[float] | None = None) -> float | None:
    """Map a raw SAR prediction onto (-1, +1) through the TRAINED tier cuts.

    Why this exists: the shipped cuts are [-0.0203, -0.0007, +0.0051, +0.0150],
    so `tanh(sar/2)` — the old display transform — squeezed the model's whole
    5th-to-95th-percentile range into under 0.02 of display width, and the
    entire neutral band (45% of the mass by construction) rendered as a literal
    "-0.00"/"0.00" while the tier label said something else entirely. This maps
    the same value through the cuts themselves, so the number and the tier
    label can never disagree and the full range is visible at 2 decimals.

    Piecewise-linear between the cuts (cuts[i] lands exactly on
    _DISP_ANCHORS[i]); outside them a saturating tail
    `anchor +/- 0.5*tanh(slope*dist/0.5)` reusing the adjacent segment's slope,
    which is C1-continuous at the boundary (tanh'(0) == 1) and monotone. The
    tail approaches +/-1 asymptotically rather than clipping at a hard edge; it
    does reach +/-1.0 exactly in float64 past roughly |SAR| > 0.29, about 20x
    the 95th-percentile cut and so unreachable in practice.

    DISPLAY ONLY. `ml_sar` and `ml_score` keep their raw values everywhere —
    the sentiment history, and therefore compute_diagnostics' IC, calibration
    curve and agreement grid, are untouched by this.

    Approximation worth knowing: the cuts were fitted on GROUP-MEAN predictions
    (ml/scripts/09_tier_cuts.py's group_frame averages `pred` per
    symbol x d1 x session_class), which is exactly what `ml_sar` is. Passing a
    single article's `sar_pred` through them — as news_sentiment does for the
    per-headline tape/brief colors — reuses a group-calibrated scale on a more
    dispersed quantity, so single-headline tiers skew more extreme than the
    ticker-level tier. Acceptable for coloring headlines; a properly
    article-level cut set would need a re-run of script 09.
    """
    if sar is None:
        return None
    try:
        sar = float(sar)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(sar):
        return None
    if cuts is None:
        st = _load()
        if not st["ok"]:
            return None
        cuts = st["cuts"]["cuts"]
    if not cuts or len(cuts) != len(_DISP_ANCHORS):
        return None
    c = [float(x) for x in cuts]
    if any(c[i] >= c[i + 1] for i in range(len(c) - 1)):
        return None  # non-monotone cuts — refuse rather than invert the scale

    # Interior: linear interpolation between adjacent (cut, anchor) pairs.
    for i in range(len(c) - 1):
        if c[i] <= sar <= c[i + 1]:
            span = c[i + 1] - c[i]
            frac = (sar - c[i]) / span
            v = _DISP_ANCHORS[i] + frac * (_DISP_ANCHORS[i + 1] - _DISP_ANCHORS[i])
            return round(v, 4)

    # Tails: extend with the adjacent segment's slope, softened by tanh so the
    # curve stays monotone and never reaches the +/-1 rail.
    if sar < c[0]:
        slope = (_DISP_ANCHORS[1] - _DISP_ANCHORS[0]) / (c[1] - c[0])
        v = _DISP_ANCHORS[0] - 0.5 * math.tanh(slope * (c[0] - sar) / 0.5)
    else:
        n = len(c) - 1
        slope = (_DISP_ANCHORS[n] - _DISP_ANCHORS[n - 1]) / (c[n] - c[n - 1])
        v = _DISP_ANCHORS[n] + 0.5 * math.tanh(slope * (sar - c[n]) / 0.5)
    return round(v, 4)


def aggregate(scored: list[dict], now: float | None = None) -> dict | None:
    """Relevance/recency/source-weighted aggregate of per-article scores.

    `scored` items: {sar_pred, relevance, datetime?, source?, n_duplicates?}.
    Returns {ml_sar, ml_score, ml_tier, ml_confidence, ml_n} or None.

    `now` pins the recency reference epoch. Live scoring leaves it None (=
    wall clock); the history backfill passes the record's own date so an old
    record is weighted as it would have been on the day it was written.
    """
    st = _load()
    if not st["ok"] or not scored:
        return None
    try:
        from portfolio_tracker import ml_features as mf

        now = float(now) if now is not None else time.time()
        wsum = ssum = 0.0
        vals = []
        for a in scored:
            if a is None or a.get("sar_pred") is None:
                continue
            age_d = max(0.0, (now - float(a.get("datetime") or now)) / 86400.0)
            w_rec = math.exp(-age_d / _TAU_DAYS)
            w_src = mf.publisher_tier(a.get("source"))
            w_nov = 1.0 / (1.0 + 0.5 * math.log1p(float(a.get("n_duplicates", 0) or 0)))
            w = w_rec * w_src * w_nov * float(a.get("relevance", 0.5))
            wsum += w
            ssum += w * float(a["sar_pred"])
            vals.append(float(a["sar_pred"]))
        if wsum <= 0 or not vals:
            return None
        ml_sar = ssum / wsum
        cuts_j = st["cuts"]
        tier = _tier(ml_sar, cuts_j["cuts"], cuts_j["tiers"])
        conf = 1.0 - math.exp(-wsum / _CONF_SCALE)
        return {
            "ml_sar": round(ml_sar, 4),
            # Raw tanh transform — kept for the history record and the
            # diagnostics that were built on it. NOT what the UI shows.
            "ml_score": round(math.tanh(ml_sar / 2.0), 4),
            # What the UI shows. See display_score() for why tanh isn't legible.
            "ml_score_disp": display_score(ml_sar, cuts_j["cuts"]),
            "ml_tier": tier,
            "ml_confidence": round(conf, 3),
            "ml_n": len(vals),
        }
    except Exception as e:
        print(f"[ml_sentiment] aggregate failed: {type(e).__name__}: {e}")
        return None


def reset_for_tests() -> None:
    """Test hook: forget the loaded model so a new MLSENT_MODEL_DIR applies."""
    with _LOCK:
        _STATE.clear()
        _STATE.update({"loaded": False, "ok": False, "reason": ""})
