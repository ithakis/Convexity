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
_STATE: dict = {"loaded": False, "ok": False}

# Recency half-life for aggregation (matches the app's default tau).
_TAU_DAYS = 3.0

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
    """Lazy singleton. Never raises — failure marks the model unavailable."""
    if _STATE["loaded"]:
        return _STATE
    with _LOCK:
        if _STATE["loaded"]:
            return _STATE
        _STATE["loaded"] = True
        d = model_dir()
        try:
            import numpy as np
            import lightgbm as lgb

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
            print(f"[ml_sentiment] loaded {ARTIFACT_VERSION} from {d}")
        except Exception as e:
            _STATE["ok"] = False
            print(f"[ml_sentiment] model unavailable ({type(e).__name__}: {e}) "
                  f"— ml_* fields disabled")
        return _STATE


def available() -> bool:
    return _load()["ok"]


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


def aggregate(scored: list[dict]) -> dict | None:
    """Relevance/recency/source-weighted aggregate of per-article scores.

    `scored` items: {sar_pred, relevance, datetime?, source?, n_duplicates?}.
    Returns {ml_sar, ml_score, ml_tier, ml_confidence, ml_n} or None.
    """
    st = _load()
    if not st["ok"] or not scored:
        return None
    try:
        from portfolio_tracker import ml_features as mf

        now = time.time()
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
        conf = 1.0 - math.exp(-wsum / 2.0)
        return {
            "ml_sar": round(ml_sar, 4),
            "ml_score": round(math.tanh(ml_sar / 2.0), 4),
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
        _STATE.update({"loaded": False, "ok": False})
