"""News & sentiment — two engines over the same headlines.

News read (this module, LLM): *what the news says.* Every headline in a
ticker's scoring batch is read through five lenses — Financials, Outlook,
Competition, Regulation, Street view — plus `other` (M&A, buybacks, financing,
insiders, management) and `none` (not about the target). The model returns a
FLAT per-headline verdict {id, lens, score -2..+2, fact} through a strict JSON
schema; lens and overall scores are then computed here, deterministically.
The call runs twice (self-consistency) and the pass-to-pass agreement is
reported, not hidden.

Market read (ml_sentiment.py, LightGBM): *how prices react to news like
this.* Computed from the same fetched articles; this module only orchestrates
it and derives the `divergence` between the two engines.

The two are peers. There is no blended score and no primary/challenger.

Caches are disk-backed (`<data>/state/news.json`) and refresh is
user-driven only (the refresh job, jobs.py). Nothing fails silently any more:
the last LLM outcome is tracked (`llm_status()`), rides on /api/health as
`llm_ok`, and a failed refresh keeps the previous News read but marks it
stale instead of passing it off as current.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from convexity import paths
from convexity.helpers import (
    _FH_LIMITER, _MAX_RETRIES, _NV_LIMITER, _RATE_LIMIT_BACKOFF_S, _RETRY_SLEEP_S,
    _YF_LIMITER, _YF_MAX_RETRIES, Cancelled, _is_rate_limited_error,
    _load_local_secret, _notify_rate,
)
from convexity.relevance import (DEDUP_SIMILARITY, norm_title, relevance_score,
                                         window_sample)

# API key loading (env var, then a strictly-local file found by walking
# upward from this module's directory) lives in helpers._load_local_secret —
# shared with finnhub_adapter.py so both modules resolve secrets identically.
FINNHUB_API_KEY = _load_local_secret("FINNHUB_API_KEY", ".finnhub_key")
NVIDIA_API_KEY = _load_local_secret("NVIDIA_API_KEY", ".nvidia_key")
_FINNHUB_BASE = "https://finnhub.io/api/v1/"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Recency decay for item weights: w = exp(-age_days / TAU). TAU=3 days gives a
# ~2.1-day half-life over the default 7-day window.
_RECENCY_TAU_DAYS = 3.0

# User-selectable analysis window (News tab lookback control). The recency
# tau scales proportionally with the window (tau = days * 3/7) so a longer
# lookback actually uses its older articles instead of e^-10'ing them to zero.
_DEFAULT_LOOKBACK_DAYS = 7
_LOOKBACK_CHOICES = (3, 7, 14, 30)

# `_SCORE_BATCH` is how many headlines the News read reads per ticker (the
# most relevant in the window — see _read_batch) and the `min_recent`
# window_sample keeps verbatim. `_ARTICLE_CAP` is the retained total the
# timeline/tape render from and the read batch is chosen from (and, via
# ml_features.WINDOW_CAP, the set the Market read ranges over).
_SCORE_BATCH = 15
_ARTICLE_CAP = 60


def _clamp_lookback(days) -> int:
    try:
        d = int(days)
    except (TypeError, ValueError):
        return _DEFAULT_LOOKBACK_DAYS
    return max(1, min(30, d))


def _tau_for_days(days: int) -> float:
    return max(_RECENCY_TAU_DAYS, days * (_RECENCY_TAU_DAYS / _DEFAULT_LOOKBACK_DAYS))


# Source credibility tiers (Tetlock 2007 / news-analytics practice: agency
# and top-masthead coverage carries more signal than PR wires and
# promotional aggregators). Matched by lowercase substring.
_SOURCE_TIER_1 = ("reuters", "bloomberg", "wall street journal", "wsj",
                  "financial times", "associated press", "ap news")
_SOURCE_TIER_2 = ("cnbc", "barron", "marketwatch", "yahoo", "forbes",
                  "investor's business daily", "business insider", "fortune")
_SOURCE_WIRE = ("pr newswire", "prnewswire", "globenewswire", "business wire",
                "businesswire", "accesswire", "motley fool", "zacks",
                "seekingalpha", "seeking alpha", "benzinga", "investorplace",
                "thefly", "newsfile", "openpr")
_W_SOURCE = {"tier1": 1.0, "tier2": 0.8, "wire": 0.5, "unknown": 0.7}

# ---- lenses: the only taxonomy -------------------------------------------
LENSES = ("financials", "outlook", "competition", "regulation", "street")
LENS_LABELS = {"financials": "Financials", "outlook": "Outlook",
               "competition": "Competition", "regulation": "Regulation",
               "street": "Street view"}
_ITEM_LENSES = (*LENSES, "other", "none")
_MARKET_LENSES = ("other", "none")

# Fixed, semantic tier cuts on the -2..+2 scale. No percentile calibration:
# the integer scale already means something ("+1 positive for the business"),
# and a quantile mapping would force 10% of names into very_bearish whatever
# the news said.
_TIER_CUTS = ((-1.25, "very_bearish"), (-0.4, "bearish"))
_TIER_UPPER = ((0.4, "neutral"), (1.25, "bullish"))

# Evidence-mass saturation for confidence: 1 - exp(-sum(w) / _CONF_SCALE).
_CONF_SCALE = 3.0
# A result built from one surviving pass has no agreement to report (null);
# its confidence is discounted by this factor instead.
_SINGLE_PASS_CONF = 0.75

# ---------------------------------------------------------------------------
# Cache — (timestamp, ttl, value) tuples, same pattern as finnhub_adapter
# ---------------------------------------------------------------------------

_NEWS_CACHE: dict[str, tuple[float, float, Any]] = {}
_SENTIMENT_CACHE: dict[str, tuple[float, float, Any]] = {}
_CACHE_LOCK = threading.Lock()
_MISS = object()

# Effectively-permanent in-memory TTLs so a single long-running session
# never silently re-fetches behind the user's back. Refresh is explicit
# only — see `refresh_sentiment`. Negative cache stays short so transient
# failures don't poison the cache.
_NEWS_TTL = 30 * 86400.0
_SENTIMENT_TTL = 30 * 86400.0
_NEG_TTL = 1800.0


def _cache_get(cache: dict, key: str) -> Any:
    with _CACHE_LOCK:
        hit = cache.get(key)
        if hit is None:
            return None
        ts, ttl, val = hit
        if time.time() - ts > ttl:
            cache.pop(key, None)
            return None
        return val


def _cache_put(cache: dict, key: str, val: Any, ttl: float) -> None:
    with _CACHE_LOCK:
        cache[key] = (time.time(), ttl, val)
    # Persist asynchronously so a burst of puts during refresh coalesces
    # into a single write.
    _schedule_persist()


def _cache_take(cache: dict, key: str) -> tuple[float, float, Any] | None:
    with _CACHE_LOCK:
        return cache.pop(key, None)


def _cache_peek_entry(cache: dict, key: str) -> tuple[float, float, Any] | None:
    with _CACHE_LOCK:
        return cache.get(key)


def _cache_restore(cache: dict, key: str, entry: tuple[float, float, Any] | None) -> None:
    if entry is None:
        return
    _old_ts, ttl, val = entry
    with _CACHE_LOCK:
        cache[key] = (time.time(), ttl, val)
    _schedule_persist()


# ---------------------------------------------------------------------------
# Disk persistence — `<data>/state/news.json` (src/convexity/paths.py)
# ---------------------------------------------------------------------------

_PERSIST_FILE = paths.state_file("news")
_PERSIST_LOCK = threading.Lock()
_persist_timer: threading.Timer | None = None
_PERSIST_DEBOUNCE_S = 1.0


def _schedule_persist() -> None:
    """Debounce disk writes so a burst of cache puts becomes one write."""
    global _persist_timer
    with _PERSIST_LOCK:
        if _persist_timer is not None:
            _persist_timer.cancel()
        _persist_timer = threading.Timer(_PERSIST_DEBOUNCE_S, _save_persisted_caches)
        _persist_timer.daemon = True
        _persist_timer.start()


def _save_persisted_caches() -> None:
    """Serialize positive cache entries to disk. _MISS sentinels are skipped."""
    try:
        with _CACHE_LOCK:
            news_out: dict[str, dict] = {}
            for k, (ts, ttl, val) in _NEWS_CACHE.items():
                if val is _MISS:
                    continue
                news_out[k] = {"saved_at": ts, "ttl": ttl, "value": val}
            sent_out: dict[str, dict] = {}
            for k, (ts, ttl, val) in _SENTIMENT_CACHE.items():
                if val is _MISS:
                    continue
                sent_out[k] = {"saved_at": ts, "ttl": ttl, "value": val}
        body = json.dumps({
            "version": 3,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "news": news_out,
            "sentiment": sent_out,
            "llm_status": dict(_LLM_STATUS),
        }, ensure_ascii=True, indent=2)
        with _PERSIST_LOCK:
            _PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            _PERSIST_FILE.write_text(body + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"[news] failed to persist caches: {exc}", file=sys.stderr)


def _load_persisted_caches() -> None:
    """Rehydrate caches from disk at import time. Resets timestamps to now so
    persisted entries get a fresh in-session lifetime — refresh remains
    user-driven. Sentiment entries from before the two-engine shape (no
    `news` key) are ignored rather than rendered in a schema the UI no longer
    reads; articles are shape-stable and kept."""
    if not _PERSIST_FILE.exists():
        return
    try:
        data = json.loads(_PERSIST_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[news] failed to load persisted caches: {exc}", file=sys.stderr)
        return
    if not isinstance(data, dict):
        return
    now = time.time()
    loaded_news = loaded_sent = 0
    with _CACHE_LOCK:
        for k, entry in (data.get("news") or {}).items():
            if not isinstance(entry, dict) or entry.get("value") is None:
                continue
            _NEWS_CACHE[k] = (now, float(entry.get("ttl") or _NEWS_TTL), entry["value"])
            loaded_news += 1
        for k, entry in (data.get("sentiment") or {}).items():
            val = entry.get("value") if isinstance(entry, dict) else None
            if not isinstance(val, dict) or "news" not in val:
                continue
            _SENTIMENT_CACHE[k] = (now, _SENTIMENT_TTL, val)
            loaded_sent += 1
    st = data.get("llm_status")
    if isinstance(st, dict) and st.get("model") == _MODEL:
        _LLM_STATUS.update({k: st.get(k) for k in ("ok", "error", "at", "permanent")})
    if loaded_news or loaded_sent:
        print(f"[news] rehydrated {loaded_news} news + {loaded_sent} sentiment entries from disk",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# Rate limiting — the limiter class and budgets live in helpers.py (they are
# process-global quotas shared with finnhub_adapter).
# ---------------------------------------------------------------------------

RefreshCancelled = Cancelled


def _ck(cancel) -> None:
    """Raise if the owning job was cancelled. Cheap enough for a hot path."""
    if cancel is not None and cancel.is_set():
        raise RefreshCancelled("refresh cancelled")


_rate_limit_lock = threading.Lock()
_fh_rate_limit_until = 0.0  # last-resort circuit breaker after repeated 429s
_nv_rate_limit_until = 0.0


def _wait_for_circuit_breaker(label: str, until_attr: str, cancel=None) -> None:
    """Sleep through a breaker window instead of returning empty data.

    Rate-limited refreshes wait and retry rather than blanking the News panel.
    The gate can hold for a full 65 s, which is why it takes `cancel`: without
    it, cancelling a refresh would still leave a worker parked here for over a
    minute and the "cancelled" the UI reported would not be true yet.
    """
    warned = False
    started = time.time()
    while True:
        _ck(cancel)
        with _rate_limit_lock:
            until = globals()[until_attr]
            wait = until - time.time()
        if wait <= 0:
            if warned:
                _notify_rate(provider=label, reason="cleared",
                             waited_s=round(time.time() - started, 1))
            return
        if not warned:
            print(f"[news] {label} backoff active — sleeping until the window reopens",
                  file=sys.stderr)
            _notify_rate(provider=label, reason="circuit_breaker",
                         retry_in_s=round(wait, 1), until_ts=until)
            warned = True
        time.sleep(min(_RETRY_SLEEP_S, max(wait, 0.1)))


# ---------------------------------------------------------------------------
# Finnhub news fetching
# ---------------------------------------------------------------------------


def _fh_call(path: str, params: dict[str, Any], cancel=None) -> Any | None:
    global _fh_rate_limit_until
    if not FINNHUB_API_KEY:
        return None
    # Last-resort circuit breaker: if we keep getting 429s even after the
    # rolling limiter, pause everything for 65s.
    _wait_for_circuit_breaker("Finnhub", "_fh_rate_limit_until", cancel=cancel)
    query = dict(params)
    query["token"] = FINNHUB_API_KEY
    url = _FINNHUB_BASE + path + "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={"User-Agent": "Convexity/1.0"})

    for attempt in range(_MAX_RETRIES):
        _FH_LIMITER.acquire(cancel=cancel)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                _FH_LIMITER.penalize()
                print(f"[news] Finnhub 429 (attempt {attempt+1}/{_MAX_RETRIES}) — "
                      f"sleeping {_RETRY_SLEEP_S}s", file=sys.stderr)
                _notify_rate(provider="finnhub", reason="http_429", attempt=attempt + 1,
                             max_attempts=_MAX_RETRIES, retry_in_s=_RETRY_SLEEP_S)
                time.sleep(_RETRY_SLEEP_S)
                if attempt == _MAX_RETRIES - 1:
                    with _rate_limit_lock:
                        _fh_rate_limit_until = time.time() + _RATE_LIMIT_BACKOFF_S
                continue
            return None
        except Exception:
            return None
    return None


# Optional fuzzy title matching for syndication dedup — same optional-dep
# pattern as symbol_db.py. Without rapidfuzz we fall back to exact
# normalised-title matching (catches literal reprints, misses paraphrases).
try:
    from rapidfuzz import fuzz as _fuzz  # type: ignore
except ImportError:
    _fuzz = None


def _dedup_articles(articles: list[dict]) -> list[dict]:
    """Collapse syndicated copies of the same story.

    Markets overreact to reprinted news (Tetlock 2011), so repeats must not
    be scored as independent evidence. The earliest article of a cluster is
    kept as canonical and carries `n_duplicates`; the syndication count is
    itself a salience signal used by the novelty weight.
    """
    ordered = sorted(articles, key=lambda a: a.get("datetime") or 0)
    kept: list[dict] = []
    kept_norms: list[str] = []
    for a in ordered:
        norm = norm_title(a.get("headline", ""))
        if not norm:
            continue
        dup_of = None
        for i, kn in enumerate(kept_norms):
            if _fuzz is not None:
                if _fuzz.token_set_ratio(norm, kn) >= DEDUP_SIMILARITY:
                    dup_of = i
                    break
            elif norm == kn:
                dup_of = i
                break
        if dup_of is not None:
            kept[dup_of]["n_duplicates"] = kept[dup_of].get("n_duplicates", 0) + 1
            continue
        a = dict(a)
        a["n_duplicates"] = 0
        a["aid"] = _article_id(a)
        kept.append(a)
        kept_norms.append(norm)
    kept.sort(key=lambda a: a.get("datetime") or 0, reverse=True)
    return kept


def _article_id(a: dict) -> str:
    """Stable short id for an article — lens rows cite their evidence by it,
    and it survives the per-window caches (url first, normalized title as the
    fallback, the same identity the dedup uses)."""
    key = (a.get("url") or "").strip() or norm_title(a.get("headline", ""))
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:10]


def _parse_yf_epoch(content: dict, item: dict) -> int:
    """Best-effort epoch seconds from a yfinance news payload."""
    ts = content.get("pubDate") or content.get("displayTime")
    if isinstance(ts, str) and ts:
        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    pt = item.get("providerPublishTime")
    if isinstance(pt, (int, float)) and pt > 0:
        return int(pt)
    return 0


def _fetch_yf_news(symbol: str, days: int, cancel=None) -> list[dict]:
    """yfinance news merged as a secondary source — free, keyless, and the
    only coverage for many non-US tickers where Finnhub free returns [].
    Flaky by nature: any failure degrades silently to Finnhub-only.

    Uses get_news(count=N), NOT the `.news` property: `.news` is hard-capped
    at ~10 most-recent items, so on an active ticker it never spans more than
    a day or two regardless of the requested window. A FRESH Ticker is built
    every call on purpose — Ticker caches its news per-instance and would
    otherwise ignore a larger `count` on a reused object. Globally throttled
    via _YF_LIMITER + jittered retry."""
    import yfinance as yf
    count = min(100, max(30, days * 8))
    raw: list = []
    for attempt in range(_YF_MAX_RETRIES):
        _YF_LIMITER.acquire(cancel=cancel)
        try:
            raw = yf.Ticker(symbol).get_news(count=count, tab="news") or []
            break
        except Exception as exc:
            if _is_rate_limited_error(exc) and attempt < _YF_MAX_RETRIES - 1:
                _YF_LIMITER.penalize()
                time.sleep(2.0 + attempt * 2.0 + random.random() * 0.5)
                continue
            return []
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    out: list[dict] = []
    for n in raw:
        if not isinstance(n, dict):
            continue
        content = n.get("content") if isinstance(n.get("content"), dict) else n
        title = content.get("title")
        if not title:
            continue
        prov = content.get("provider")
        source = (prov.get("displayName") if isinstance(prov, dict) else None) \
            or content.get("publisher") or n.get("publisher") or ""
        curl = content.get("canonicalUrl")
        link = (curl.get("url") if isinstance(curl, dict) else None) \
            or content.get("link") or n.get("link") or ""
        ts = _parse_yf_epoch(content, n)
        if ts and ts < cutoff:
            continue
        out.append({
            "headline": title,
            "summary": content.get("summary") or content.get("description") or "",
            "source": source,
            "datetime": ts,
            "url": link,
            "related": symbol,
        })
    return out



def fetch_company_news(symbol: str, days: int = 7, cancel=None) -> list[dict] | None:
    """Fetch recent news for a single ticker from Finnhub + yfinance,
    deduplicated. Returns normalised article list (most recent first).

    Keyed by `days` — a 7d and 30d GET must not collide/shadow each other."""
    cache_key = f"news|{symbol}|{days}"
    cached = _cache_get(_NEWS_CACHE, cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached

    to_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    from_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    raw = _fh_call("company-news", {"symbol": symbol, "from": from_date, "to": to_date},
                   cancel=cancel)
    articles: list[dict] = []
    fh_failed = raw is None  # transient failure vs "returned but empty"
    if isinstance(raw, list):
        # Deep, window-scaled slice, NOT a flat newest-N — Finnhub returns
        # newest-first within the range, so a fixed cap would collapse a wide
        # window to its newest days for a high-volume ticker.
        fh_cap = min(300, max(60, days * 20))
        for a in raw[:fh_cap]:
            articles.append({
                "headline": a.get("headline", ""),
                "summary": a.get("summary", ""),
                "source": a.get("source", ""),
                "datetime": a.get("datetime", 0),
                "url": a.get("url", ""),
                "related": a.get("related", ""),
            })
    articles.extend(_fetch_yf_news(symbol, days, cancel=cancel))
    if not articles:
        if fh_failed:
            return None  # transient — don't negative-cache
        _cache_put(_NEWS_CACHE, cache_key, _MISS, _NEG_TTL)
        return None
    articles = window_sample(_dedup_articles(articles), cap=_ARTICLE_CAP, min_recent=_SCORE_BATCH)
    # A Finnhub outage still lets yfinance-only articles through (better than
    # nothing), but that's a DEGRADED result — cache it briefly, not for the
    # full 30-day _NEWS_TTL, so a real Finnhub recovery isn't masked.
    _cache_put(_NEWS_CACHE, cache_key, articles, _NEG_TTL if fh_failed else _NEWS_TTL)
    return articles


# Benchmark indices whose yfinance news gives the market feed a real window.
# Finnhub's general feed has no date param (latest ~30 only), so windowed
# market coverage comes from these tickers via the same _fetch_yf_news path.
_MARKET_INDEX_TICKERS = ("^GSPC", "^IXIC")


def fetch_market_news(days: int = _DEFAULT_LOOKBACK_DAYS, cancel=None) -> list[dict] | None:
    """General market news: Finnhub `category=general` (latest feed, no
    window) merged with windowed yfinance news for the benchmark indices.
    Keyed by `days` so different windows don't shadow each other in cache."""
    cache_key = f"news|__market__|{days}"
    cached = _cache_get(_NEWS_CACHE, cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached

    raw = _fh_call("news", {"category": "general"}, cancel=cancel)
    fh_failed = raw is None
    articles = []
    if isinstance(raw, list):
        for a in raw[:30]:
            articles.append({
                "headline": a.get("headline", ""),
                "summary": a.get("summary", ""),
                "source": a.get("source", ""),
                "datetime": a.get("datetime", 0),
                "url": a.get("url", ""),
            })
    for idx in _MARKET_INDEX_TICKERS:
        articles.extend(_fetch_yf_news(idx, days, cancel=cancel))
    if not articles:
        if fh_failed:
            return None  # transient Finnhub failure and no yf news — don't negative-cache
        _cache_put(_NEWS_CACHE, cache_key, _MISS, _NEG_TTL)
        return None
    articles = window_sample(_dedup_articles(articles), cap=_ARTICLE_CAP, min_recent=_SCORE_BATCH)
    _cache_put(_NEWS_CACHE, cache_key, articles, _NEG_TTL if fh_failed else _NEWS_TTL)
    return articles


def _market_tape_context() -> tuple[str, list[dict]]:
    """Cross-asset moves for the market card. Returns (tape_string, items):
    the string (SPY/QQQ/10Y 1d moves) is injected into the market prompt so
    the brief can lead with numbers; `items` is [{label, pct}] so the frontend
    can color each return by magnitude. Free via yfinance; any failure returns
    ("", []) and the prompt simply omits the block."""
    try:
        import yfinance as yf
        parts: list[str] = []
        items: list[dict] = []
        labels = {"SPY": "S&P 500 ETF", "QQQ": "Nasdaq-100 ETF", "^TNX": "US 10Y yield (x10 bp)"}
        data = yf.download(list(labels), period="5d", interval="1d",
                           progress=False, auto_adjust=True)["Close"]
        for sym, label in labels.items():
            try:
                s = data[sym].dropna()
                chg = (s.iloc[-1] / s.iloc[-2] - 1.0) * 100.0
                parts.append(f"{label}: {chg:+.2f}% 1d")
                items.append({"label": label, "pct": round(float(chg), 2)})
            except Exception:
                continue
        return "; ".join(parts), items
    except Exception:
        return "", []


# ---------------------------------------------------------------------------
# Deterministic aggregation — the auditable math between per-headline LLM
# verdicts and what the UI shows. Pure functions, unit-tested.
# ---------------------------------------------------------------------------


def _source_weight(source: str) -> float:
    s = (source or "").lower()
    if any(t in s for t in _SOURCE_TIER_1):
        return _W_SOURCE["tier1"]
    if any(t in s for t in _SOURCE_TIER_2):
        return _W_SOURCE["tier2"]
    if any(t in s for t in _SOURCE_WIRE):
        return _W_SOURCE["wire"]
    return _W_SOURCE["unknown"]


def _article_weight(article: dict, now: float | None = None,
                    tau: float | None = None) -> float:
    """w = recency x source x novelty; items the LLM tagged `none` weigh 0.
    `tau` is the recency e-folding in days (scaled with the window)."""
    if article.get("lens") == "none":
        return 0.0
    now = now or time.time()
    tau = tau or _RECENCY_TAU_DAYS
    ts = article.get("datetime") or 0
    age_days = max(0.0, (now - ts) / 86400.0) if ts else tau
    w_recency = math.exp(-age_days / tau)
    w_source = _source_weight(article.get("source", ""))
    n_dup = article.get("n_duplicates", 0) or 0
    w_novelty = 1.0 / (1.0 + 0.5 * math.log1p(n_dup))
    return w_recency * w_source * w_novelty


def tier_for(score: float | None) -> str | None:
    """Fixed semantic tiers on the -2..+2 scale (plan §4.4)."""
    if score is None:
        return None
    for cut, tier in _TIER_CUTS:
        if score <= cut:
            return tier
    for cut, tier in _TIER_UPPER:
        if score < cut:
            return tier
    return "very_bullish"


def aggregate_items(items: list[dict], agreement: float | None,
                    now: float | None = None, tau: float | None = None) -> dict | None:
    """Lens scores + the overall News read from scored headlines.

    `items` are article dicts carrying `lens` / `llm_score` / `fact` / `aid`
    plus the weight fields (datetime, source, n_duplicates). A lens with no
    items is None — "no news", never a fabricated 0. Returns None when no
    headline was scored at all."""
    scored = [a for a in items if a.get("lens") in _ITEM_LENSES
              and isinstance(a.get("llm_score"), (int, float))]
    if not scored:
        return None
    rows: dict[str, list] = {k: [] for k in (*LENSES, "other")}
    for a in scored:
        if a["lens"] in rows:
            rows[a["lens"]].append((_article_weight(a, now, tau), a))
    lenses: dict[str, dict | None] = {}
    for lens in LENSES:
        grp = [(w, a) for w, a in rows[lens] if w > 0]
        wsum = sum(w for w, _ in grp)
        if wsum <= 0:
            lenses[lens] = None
            continue
        s = sum(w * float(a["llm_score"]) for w, a in grp) / wsum
        lead = max(grp, key=lambda t: abs(float(t[1]["llm_score"])) * t[0])[1]
        lenses[lens] = {"score": round(s, 2), "tier": tier_for(s), "n": len(grp),
                        "fact": lead.get("fact") or "", "ids": [a.get("aid") for _, a in grp]}
    about = [(w, a) for lst in rows.values() for w, a in lst if w > 0]
    wsum = sum(w for w, _ in about)
    n_none = sum(1 for a in scored if a["lens"] == "none")
    if wsum <= 0:
        score = None
        conf = 0.0
    else:
        score = sum(w * float(a["llm_score"]) for w, a in about) / wsum
        mass = 1.0 - math.exp(-wsum / _CONF_SCALE)
        conf = mass * (agreement if agreement is not None else _SINGLE_PASS_CONF)
    return {
        "score": (round(score, 2) if score is not None else None),
        "tier": tier_for(score),
        "confidence": round(conf, 3),
        "agreement": (round(agreement, 3) if agreement is not None else None),
        "n_items": len(scored),
        "n_none": n_none,
        "other_n": len([1 for w, _ in rows["other"] if w > 0]),
        "lenses": lenses,
    }


def merge_passes(first: list[dict] | None, second: list[dict] | None
                 ) -> tuple[list[dict], float | None] | None:
    """Combine two validated passes (lists aligned to headline order).

    Score = mean of the two. Lens: pass 1's when both agree or both call the
    item about the target; `none` when either pass says it is not — an item
    only counts when both reads attribute it to the target. Agreement = share
    of items with the same lens in both passes and |score gap| <= 1. One
    surviving pass is used as is, with agreement None."""
    if first is None and second is None:
        return None
    if first is None or second is None:
        return (first or second), None
    merged, same = [], 0
    for a, b in zip(first, second):
        lens = a["lens"] if (a["lens"] == b["lens"]
                             or (a["lens"] != "none" and b["lens"] != "none")) else "none"
        score = 0.0 if lens == "none" else (a["score"] + b["score"]) / 2.0
        if a["lens"] == b["lens"] and abs(a["score"] - b["score"]) <= 1:
            same += 1
        merged.append({"lens": lens, "score": score, "fact": a["fact"] or b["fact"]})
    return merged, (same / len(merged) if merged else None)


def compute_divergence(news: dict | None, market: dict | None,
                       row: dict | None = None, vol_20d: float | None = None) -> dict | None:
    """Flag the two engines disagreeing — one factual sentence, or None.

    good_news_weak_reaction  News bullish+ while the Market read is bearish-
    weak_news_strong_reaction News bearish- while the Market read is bullish+
    sold_the_news            News bullish+ while today's move is <= -2 sigma
                             of the stock's 20-day vol
    """
    if not news or news.get("score") is None:
        return None
    ntier = news.get("tier") or ""
    good = ntier in ("bullish", "very_bullish")
    bad = ntier in ("bearish", "very_bearish")
    mtier = (market or {}).get("tier") or ""
    parts = []
    for lens in sorted(LENSES, key=lambda k: -abs(((news.get("lenses") or {}).get(k)
                                                    or {}).get("score") or 0)):
        ent = (news.get("lenses") or {}).get(lens)
        if ent and abs(ent["score"]) >= 0.4 and len(parts) < 2:
            parts.append(f"{LENS_LABELS[lens]} {ent['score']:+.1f}".replace("-", "−"))
    lead = ", ".join(parts) or f"News {news['score']:+.1f}".replace("-", "−")
    pct_1d = (row or {}).get("pct_1d")
    if good and isinstance(pct_1d, (int, float)) and vol_20d and vol_20d > 0 \
            and pct_1d / 100.0 <= -2.0 * vol_20d:
        return {"kind": "sold_the_news",
                "text": f"{lead}; stock {pct_1d:+.1f}% on the day".replace("-", "−")}
    z = (market or {}).get("z")
    zs = f" ({z:+.1f}σ)" if isinstance(z, (int, float)) else ""
    if good and mtier in ("bearish", "very_bearish"):
        return {"kind": "good_news_weak_reaction",
                "text": f"{lead}; Market read {mtier.replace('_', ' ')}{zs}".replace("-", "−")}
    if bad and mtier in ("bullish", "very_bullish"):
        return {"kind": "weak_news_strong_reaction",
                "text": f"{lead}; Market read {mtier.replace('_', ' ')}{zs}".replace("-", "−")}
    return None


# ---------------------------------------------------------------------------
# Sentiment history — append-only daily records powering the Track record.
# One record per symbol per refresh; same-day re-refresh overwrites.
# ---------------------------------------------------------------------------

_HISTORY_FILE = paths.state_file("sentiment_history")
_HISTORY_LOCK = threading.Lock()
_HISTORY_MAX_DAYS = 400


def _history_load() -> list[dict]:
    try:
        with _HISTORY_LOCK:
            data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
        recs = data.get("records")
        return recs if isinstance(recs, list) else []
    except Exception:
        return []


def _history_append(record: dict) -> None:
    try:
        with _HISTORY_LOCK:
            try:
                data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
                records = data.get("records") or []
            except Exception:
                records = []
            key = (record.get("date"), record.get("symbol"))
            records = [r for r in records
                       if (r.get("date"), r.get("symbol")) != key]
            records.append(record)
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=_HISTORY_MAX_DAYS)).strftime("%Y-%m-%d")
            records = [r for r in records if (r.get("date") or "") >= cutoff]
            _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            _HISTORY_FILE.write_text(
                json.dumps({"version": 2, "records": records}) + "\n",
                encoding="utf-8")
    except Exception as exc:
        print(f"[news] failed to append sentiment history: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# News read engine — NVIDIA NIM
# ---------------------------------------------------------------------------

# The only two NIM models that answered on 2026-09-24 were this one (6-12 s
# per 15-headline ticker with thinking off) and openai/gpt-oss-20b (20-40 s,
# timeouts). nemotron-nano-9b-v2, the previous model, was retired on
# 2026-08-26 and answers every call with HTTP 410 — which the old code logged
# and swallowed, so refreshes silently kept August's sentiment. One model, no
# fallback chain: an unavailable model is SURFACED (llm_status) instead.
_MODEL = "nvidia/nemotron-3-super-120b-a12b"
_NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
_NV_TIMEOUT_S = 60.0
_MAX_TOKENS = 1800

# Last LLM outcome, persisted with the caches so a retired model is still
# reported after a restart. ok: None (never called) | True | False.
_LLM_STATUS: dict[str, Any] = {"ok": None, "error": None, "at": None, "model": _MODEL,
                               "permanent": False}
_LLM_STATUS_LOCK = threading.Lock()

_nvidia_client: Any = None
_client_lock = threading.Lock()


def _llm_record(ok: bool, error: str | None = None, permanent: bool = False) -> None:
    """`permanent` marks failures a retry cannot fix (no key, retired model,
    rejected key): the second pass and re-asks are skipped for those."""
    with _LLM_STATUS_LOCK:
        _LLM_STATUS.update({"ok": ok, "error": error, "model": _MODEL,
                            "permanent": bool(permanent) and not ok,
                            "at": datetime.now(timezone.utc).isoformat()})


def llm_status() -> dict:
    """{model, ok, error, at} — `ok` is False with a reason when the News read
    cannot run (no key, retired model, auth failure), None before any call."""
    with _LLM_STATUS_LOCK:
        st = dict(_LLM_STATUS)
    if not NVIDIA_API_KEY:
        st.update({"ok": False, "permanent": True,
                   "error": "NVIDIA key missing — add it in Settings → API keys"})
    return st


def _llm_blocked() -> bool:
    st = llm_status()
    return st.get("ok") is False and bool(st.get("permanent"))


def llm_unblock() -> None:
    """Forget a recorded permanent failure so the next call really tries.
    Called at the start of every refresh: within a refresh, one 410 spares
    every later ticker the call; across refreshes, a fixed key or a restored
    model must get its chance (the status is persisted across restarts)."""
    with _LLM_STATUS_LOCK:
        _LLM_STATUS["permanent"] = False


def reload_keys() -> None:
    """Re-read both keys (Settings -> API keys saved or cleared one; they used
    to be read once at import, so a new key needed a restart).

    The cached OpenAI client captured the old NVIDIA key at construction, so it
    is dropped and rebuilt lazily. A changed NVIDIA key also clears the
    recorded LLM outcome: a persisted "key rejected" is permanent and would
    otherwise keep short-circuiting every call (and the banner) until the next
    refresh. A retired model simply re-records itself on the next call."""
    global FINNHUB_API_KEY, NVIDIA_API_KEY, _nvidia_client
    fh = _load_local_secret("FINNHUB_API_KEY", ".finnhub_key")
    nv = _load_local_secret("NVIDIA_API_KEY", ".nvidia_key")
    FINNHUB_API_KEY = fh
    with _client_lock:
        changed = nv != NVIDIA_API_KEY
        NVIDIA_API_KEY = nv
        _nvidia_client = None
    if changed:
        with _LLM_STATUS_LOCK:
            _LLM_STATUS.update({"ok": None, "error": None, "permanent": False, "at": None})
        _schedule_persist()  # or a restart would reload the old "key rejected"


def _get_client() -> Any:
    """Lazy OpenAI-compatible client. The `openai` import is deferred (~1.2 s)
    so it never sits on the desktop app's startup path."""
    global _nvidia_client
    if _nvidia_client is not None:
        return _nvidia_client
    try:
        from openai import OpenAI as _OpenAI
    except ImportError:
        return None
    with _client_lock:
        if _nvidia_client is None:
            # timeout caps a hung request (SDK default is 600 s); max_retries=0
            # so our own loop is the only retry layer.
            _nvidia_client = _OpenAI(
                base_url=_NVIDIA_BASE, api_key=NVIDIA_API_KEY,
                timeout=_NV_TIMEOUT_S, max_retries=0,
            )
        return _nvidia_client


def _http_status(exc: Exception) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def _json_schema(lens_enum: tuple[str, ...]) -> dict:
    """Flat per-headline schema. A nested per-lens schema written by the model
    came back malformed on 2 of 4 calls; this one was 5/5 valid, so lens
    verdicts are computed in Python instead (plan §2.3)."""
    return {
        "type": "object", "additionalProperties": False, "required": ["items", "brief"],
        "properties": {
            "items": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["id", "lens", "score", "fact"],
                "properties": {
                    "id": {"type": "integer"},
                    "lens": {"type": "string", "enum": list(lens_enum)},
                    "score": {"type": "integer", "minimum": -2, "maximum": 2},
                    "fact": {"type": "string"},
                }}},
            "brief": {"type": "string"},
        },
    }


def _nvidia_call(system_prompt: str, user_content: str, lens_enum: tuple[str, ...],
                 cancel=None) -> dict | None:
    """One schema-constrained NIM completion, parsed.

    Thinking is off twice over (chat_template_kwargs AND a leading /no_think):
    with it on, this hybrid model spends its budget on a hidden reasoning pass.
    Output goes through response_format json_schema (strict) — 5/5 valid in
    probing vs 2/4 without; `nvext.guided_json` is rejected (HTTP 400) here.
    Retries: 429 (the limiter is penalised and the call sleeps 5 s), 503
    ("Service temporarily overloaded" is routine on NIM: a short jittered
    sleep, NO limiter penalty — the penalty backfills the whole rolling minute,
    and on the gold benchmark one 503 turned a 10 s ticker into 64 s),
    timeouts and bad JSON. 404/410 mean the model is gone and 401/403 a bad
    key: no retry, recorded in llm_status for the banner."""
    global _nv_rate_limit_until
    if not NVIDIA_API_KEY:
        _llm_record(False, "NVIDIA key missing — add it in Settings → API keys", permanent=True)
        return None
    _ck(cancel)
    _wait_for_circuit_breaker("NVIDIA NIM", "_nv_rate_limit_until", cancel=cancel)
    client = _get_client()
    if client is None:
        _llm_record(False, "openai package not installed", permanent=True)
        return None
    messages = [
        {"role": "system", "content": "/no_think\n" + system_prompt},
        {"role": "user", "content": user_content},
    ]
    last_err = "no response"
    for attempt in range(_MAX_RETRIES):
        _ck(cancel)
        _NV_LIMITER.acquire(cancel=cancel)
        try:
            response = client.chat.completions.create(
                model=_MODEL, messages=messages, temperature=0.1, max_tokens=_MAX_TOKENS,
                response_format={"type": "json_schema", "json_schema": {
                    "name": "news_read", "schema": _json_schema(lens_enum), "strict": True}},
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            text = (response.choices[0].message.content or "").strip()
            if not text:
                raise json.JSONDecodeError("empty content", "", 0)
            data = json.loads(text)
            _llm_record(True)
            return data
        except json.JSONDecodeError:
            last_err = "malformed JSON"
            print(f"[news] NIM returned unusable JSON (attempt {attempt+1})", file=sys.stderr)
            time.sleep(1.0)
            continue
        except Cancelled:
            raise
        except Exception as exc:
            code = _http_status(exc)
            if code in (404, 410):
                reason = f"{code} model retired ({_MODEL})"
                print(f"[news] NIM {reason}", file=sys.stderr)
                _llm_record(False, reason, permanent=True)
                return None
            if code in (401, 403):
                reason = f"{code} NVIDIA key rejected"
                print(f"[news] NIM {reason}", file=sys.stderr)
                _llm_record(False, reason, permanent=True)
                return None
            if code == 503:
                last_err = "503 service overloaded"
                wait = 1.5 + attempt * 1.5 + random.random()
                print(f"[news] NIM {last_err} (attempt {attempt+1}/{_MAX_RETRIES}) — "
                      f"retrying in {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if code == 429 or "rate limit" in str(exc).lower():
                _NV_LIMITER.penalize()
                last_err = "429 rate limited"
                print(f"[news] NIM {last_err} (attempt {attempt+1}/{_MAX_RETRIES}) — "
                      f"sleeping {_RETRY_SLEEP_S}s", file=sys.stderr)
                _notify_rate(provider="nvidia", reason="http_429", attempt=attempt + 1,
                             max_attempts=_MAX_RETRIES, retry_in_s=_RETRY_SLEEP_S)
                time.sleep(_RETRY_SLEEP_S)
                if attempt == _MAX_RETRIES - 1:
                    with _rate_limit_lock:
                        _nv_rate_limit_until = time.time() + _RATE_LIMIT_BACKOFF_S
                continue
            last_err = f"{type(exc).__name__}: {str(exc)[:160]}"
            print(f"[news] NIM call failed: {last_err} (attempt {attempt+1})", file=sys.stderr)
            continue
    _llm_record(False, last_err)
    return None


def validate_items(raw: dict | None, n: int, lens_enum: tuple[str, ...] = _ITEM_LENSES
                   ) -> tuple[list[dict], str] | None:
    """Strict check of one pass. Returns (items aligned to headline order,
    brief) or None when the pass is unusable: every id 1..n must appear
    exactly once with a lens from the enum and an integer score in -2..2.
    `none` items are forced to score 0 (the prompt's rule, enforced)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        return None
    by_id: dict[int, dict] = {}
    for it in raw["items"]:
        if not isinstance(it, dict):
            return None
        idx, lens, score = it.get("id"), it.get("lens"), it.get("score")
        if isinstance(idx, bool) or not isinstance(idx, int) or not 1 <= idx <= n \
                or idx in by_id:
            return None
        if lens not in lens_enum:
            return None
        if isinstance(score, bool) or not isinstance(score, (int, float)) \
                or not math.isfinite(score) or float(score) != int(score) \
                or not -2 <= score <= 2:
            return None
        by_id[idx] = {"lens": lens, "score": 0 if lens == "none" else int(score),
                      "fact": str(it.get("fact") or "").strip()[:160]}
    if len(by_id) != n:
        return None
    return [by_id[i + 1] for i in range(n)], str(raw.get("brief") or "").strip()[:500]


def _read_pass(system_prompt: str, user_prompt: str, n: int, lens_enum,
               cancel=None) -> tuple[list[dict], str] | None:
    """One pass = one call; a response that fails validation is re-asked once."""
    for _ in range(2):
        out = validate_items(_nvidia_call(system_prompt, user_prompt, lens_enum, cancel=cancel),
                             n, lens_enum)
        if out is not None:
            return out
        if _llm_blocked():
            return None
    return None


def read_headlines(system_prompt: str, user_prompt: str, n: int, lens_enum=_ITEM_LENSES,
                   stage_cb=None, cancel=None) -> tuple[list[dict], str, float | None] | None:
    """The two-pass News read: (items, brief, agreement) or None if both fail.

    The passes are independent samples of the same prompt, so they run
    concurrently: a ticker costs one call's latency, not two (p95 is a gate).
    A permanent failure (retired model, no key) is known after the first
    response; the concurrent pass then fails fast on the same status."""
    if _llm_blocked():
        return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(_read_pass, system_prompt, user_prompt, n, lens_enum, cancel)
                for _ in range(2)]
        done = []
        for fut in as_completed(futs):
            done.append(fut)
            if stage_cb:
                stage_cb("pass1" if len(done) == 1 else "pass2",
                         0.5 if len(done) == 1 else 0.75)
        first, second = (f.result() for f in futs)
    _ck(cancel)
    merged = merge_passes(first[0] if first else None, second[0] if second else None)
    if merged is None:
        return None
    items, agreement = merged
    brief = (first or second)[1]
    return items, brief, agreement


_NEWS_READ_PROMPT = """You are an equity analyst. For each numbered headline, decide which LENS it bears on for the TARGET company and score what it says about the TARGET's business.

LENSES (exactly one per headline):
- financials: reported or realized results — revenue, profit, EPS, margins, losses, cash flow, impairments — including how they compare with expectations, even when framed as a trend.
- outlook: forward-looking statements and demand signals — guidance and forecasts, company targets, demand, pricing, orders and backlog totals, capex plans.
- competition: the target's position against rivals and with customers — product launches, customer wins or losses, contracts, partnerships, market share, and rival moves that DIRECTLY affect the target.
- regulation: lawsuits, fines, investigations, antitrust, export controls, tariffs, government policy or political pressure.
- street: sell-side analyst actions — rating changes, price-target changes, estimate revisions, coverage initiations.
- other: capital and corporate actions — M&A, buybacks, dividends, debt or equity financing, credit ratings, insider trades, management changes.
- none: not about the target's business — listicles and "stocks to buy/watch" roundups, "X vs. Y: which is the better buy" pieces, valuation or should-you-buy opinion pieces, market wraps, share-price moves reported without business news, fund holdings, and other companies' news.

SCORE (integer, for the TARGET's business): -2 clearly negative, -1 negative, 0 neutral or mixed, +1 positive, +2 clearly positive.
- Judge the CONTENT, not the share-price reaction: "revenue grew 36%, stock fell 16%" is financials +1.
- A price-target raise is street +1 and a cut is street -1 even when the rating is unchanged; an upgrade or downgrade is +1/-1, or +2/-2 when emphatic.
- A new customer, contract, partnership or product launch is competition +1 unless the headline says otherwise; losing a customer or share is -1.
- Insider selling is other -1 and insider buying other +1; a credit-rating downgrade is other -1; a dividend increase or buyback is other +1.
- TARGET ATTRIBUTION: a rival's, supplier's, customer's or partner's own news is none unless the headline states a concrete effect on the target (a rival launching a chip is none; a customer cutting orders from the target is outlook -1).
- If a headline is not really about the target, label it none — never a real lens with score 0. A none item must have score 0.
- fact: at most 14 words, a plain factual restatement of what happened for the target. No adjectives, no opinion.
- Include every id exactly once, in order.

BRIEF (Bloomberg Way, 2-4 sentences, at most 60 words):
- Sentence 1: what happened + magnitude + why, company first, active voice. Use the QUANT CONTEXT numbers when given.
- Sentence 2: the numbers versus expectations (consensus, guidance, targets).
- Sentence 3 (optional): what it means for the holding (valuation, next catalyst).
- Every claim must trace to a headline or the quant context. No opinion words (banned: exciting, concerning, impressive, notably, significantly, "continues to"), no hedging, no exclamation marks. If coverage is thin or immaterial, say so in one sentence."""

_MARKET_READ_PROMPT = """You are a macro strategist. For each numbered headline, decide whether it bears on aggregate US equity market risk appetite and score it.

LENS: other = a market-relevant story (monetary policy, economic data versus consensus, credit, geopolitics, market breadth, sector-wide moves); none = single-stock, human-interest or promotional items.
SCORE (integer, for US equity market risk appetite): -2 clearly negative, -1 negative, 0 neutral or already priced, +1 positive, +2 clearly positive. A none item must have score 0.
fact: at most 14 words, plain and factual, no adjectives.
Include every id exactly once, in order.

BRIEF (Bloomberg Way, 1-2 sentences, at most 40 words): the systematic-risk picture — what the market did or faces and why, with numbers from the CROSS-ASSET TAPE when given — ending with the risk-appetite read (e.g. "a headwind for high-beta holdings"). No opinion adjectives, no hedging."""


def _format_quant_context(symbol: str, row_ctx: dict | None) -> str:
    """Row-payload numbers injected into the prompt so briefs lead with
    magnitudes (the Bloomberg Way is 50% quantitative context)."""
    if not row_ctx:
        return ""
    parts = []
    fmt = {
        "pct_1d": "1-day move {:+.1f}%", "pct_1w": "1-week move {:+.1f}%",
        "pct_ytd": "YTD {:+.1f}%", "delta_ath": "vs all-time high {:+.1f}%",
        "target_upside": "analyst target upside {:+.1f}%",
    }
    for key, template in fmt.items():
        v = row_ctx.get(key)
        if isinstance(v, (int, float)):
            parts.append(template.format(v))
    ne = row_ctx.get("next_earnings")
    if ne:
        parts.append(f"next earnings {ne}")
    if not parts:
        return ""
    return f"\nQUANT CONTEXT for {symbol}: " + "; ".join(parts) + "\n"


def _read_batch(articles: list[dict], symbol: str) -> list[dict]:
    """The headlines the News read reads for one ticker: the _SCORE_BATCH most
    relevant to it in the window (relevance.relevance_score — the company
    named in the title beats a tagged-only mention; newer first within a
    tie), presented newest first. Newest-N is wrong for a heavily covered
    name: Finnhub tags a mega-cap onto every listicle that mentions it, and
    on 2026-09-25 NVDA's newest 15 were all "not about NVDA" while four
    headlines that named it sat just outside the cut. Recency still weights
    the aggregate (aggregate_items)."""
    ranked = sorted(articles, key=lambda a: (
        -relevance_score(a.get("headline", ""), a.get("summary"), symbol),
        -(a.get("datetime") or 0)))
    return sorted(ranked[:_SCORE_BATCH], key=lambda a: -(a.get("datetime") or 0))


def _build_articles_prompt(batch: list[dict], symbol: str,
                           row_ctx: dict | None = None) -> str:
    """Numbered headlines (+ a 300-char summary) for one ticker's batch."""
    name = (row_ctx or {}).get("name")
    target = f"{symbol} ({name})" if name else symbol
    lines = [f"TARGET COMPANY: {target}.", _format_quant_context(symbol, row_ctx)]
    for i, a in enumerate(batch, 1):
        ts = a.get("datetime", 0)
        date = (datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                if isinstance(ts, (int, float)) and ts > 0 else "?")
        dup = a.get("n_duplicates", 0)
        dup_note = f" (+{dup} syndicated copies)" if dup else ""
        lines.append(f"[{i}] {date} | {a.get('source', '')}{dup_note} | {a.get('headline', '')}")
        summary = (a.get("summary") or "")[:300]
        if summary:
            lines.append(f"    {summary}")
    return "\n".join(lines)


def _build_market_prompt(articles: list[dict], tape: str = "") -> str:
    lines = ["Market headlines:"]
    if tape:
        lines.append(f"\nCROSS-ASSET TAPE (1-day moves): {tape}\n")
    for i, a in enumerate(articles[:_SCORE_BATCH], 1):
        lines.append(f"[{i}] {a.get('source', '')}: {a.get('headline', '')}")
    return "\n".join(lines)


def _stamp_items(batch: list[dict], items: list[dict]) -> None:
    """Write the merged per-headline verdicts onto the cached article dicts
    (the same objects the tape/timeline read). Replaces, never merges, so a
    headline cannot carry a stale lens from an older read."""
    for a, it in zip(batch, items):
        a["lens"] = it["lens"]
        a["llm_score"] = round(float(it["score"]), 2)
        a["fact"] = it["fact"]
        a.setdefault("aid", _article_id(a))


# ML scoring is per-symbol, so an unavailable model would log once per ticker
# per refresh. One line per process is enough to diagnose it and keeps the
# Settings -> Logs console readable.
_ML_WARNED = False


def _warn_ml_once(reason: str) -> None:
    global _ML_WARNED
    if _ML_WARNED:
        return
    _ML_WARNED = True
    print(f"[news_sentiment] Market read unavailable ({reason}) — market fields "
          f"will be null", file=sys.stderr)


# ---------------------------------------------------------------------------
# Per-ticker assessment
# ---------------------------------------------------------------------------


def _market_history(model_version: str, exclude: tuple[str, str]) -> list[float]:
    """Raw Market read scores from the last LIVE_WINDOW_DAYS of history — the
    reference the live-anchored percentile ranks against. Only the running
    model's scores (another model's are on another scale), and not the
    (date, symbol) being re-scored, which would rank a ticker against itself."""
    from convexity import ml_sentiment as _ml

    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=_ml.LIVE_WINDOW_DAYS)).strftime("%Y-%m-%d")
    return [r["market_score"] for r in _history_load()
            if r.get("market_model") == model_version
            and isinstance(r.get("market_score"), (int, float))
            and (r.get("date") or "") >= cutoff
            and (r.get("date"), r.get("symbol")) != exclude]


def _market_read(symbol: str, ctx: dict, cancel=None) -> tuple[dict | None, float | None]:
    """(Market read dict, stock 20d vol) — ml_sentiment on the 7-day window.

    The Market read is always the 7-day read whatever the News window: that
    is the unit the model was calibrated on."""
    try:
        from convexity import ml_sentiment as _ml

        if not _ml.available():
            _warn_ml_once(_ml.runtime_status().get("reason") or "model not loaded")
            return None, None
        arts7 = fetch_company_news(symbol, days=7, cancel=cancel) or []
        closes = ctx.get("closes")
        if closes is None:
            closes = _ml.load_closes([symbol])
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        history = _market_history(_ml.ARTIFACT_VERSION, (today, symbol))
        return _ml.market_read(symbol, arts7, closes, history=history)
    except Cancelled:
        raise
    except Exception as exc:
        # Never swallow this silently: this path used to be a bare except
        # that left the model dead for weeks without a single log line.
        _warn_ml_once(f"{type(exc).__name__}: {exc}")
        return None, None


def _assess_symbol(symbol: str, ctx: dict, stage_cb=None,
                   cancel=None) -> tuple[dict | None, str]:
    """Run both engines for one ticker. Returns (result, outcome) where
    outcome is "ok" (fresh News read), "failed" (the LLM could not produce
    one — the previous read is kept, marked stale) or "empty" (no news).

    Result shape (persisted, served, rendered):
      {news: {tier, score, confidence, agreement, n_items, n_none, brief,
              lenses: {lens: {score, tier, n, fact, ids} | None}, other_n,
              assessed_at, lookback_days, stale?, stale_reason?} | None,
       market: {sar, z, pct, tier, anchor, n_history, score, horizon_days,
                confidence, n_articles, model_version, assessed_at} | None,
       divergence: {kind, text} | None, article_count, assessed_at}
    """
    days = _clamp_lookback(ctx.get("lookback_days") or _DEFAULT_LOOKBACK_DAYS)
    key = f"sentiment|{symbol}|{days}"
    prev = _cache_get(_SENTIMENT_CACHE, key)
    prev = prev if isinstance(prev, dict) else None
    tau = _tau_for_days(days)
    articles = fetch_company_news(symbol, days=days, cancel=cancel)
    if stage_cb:
        stage_cb("fetch", 0.25)
    _ck(cancel)
    if not articles:
        return None, "empty"

    row_ctx = ctx.get("row")
    batch = _read_batch(articles, symbol)
    now_iso = datetime.now(timezone.utc).isoformat()
    read = read_headlines(_NEWS_READ_PROMPT, _build_articles_prompt(batch, symbol, row_ctx),
                          len(batch), stage_cb=stage_cb, cancel=cancel)
    outcome = "ok"
    news = None
    if read is not None:
        items, brief, agreement = read
        _stamp_items(batch, items)
        _cache_put(_NEWS_CACHE, f"news|{symbol}|{days}", articles, _NEWS_TTL)
        agg = aggregate_items(batch, agreement, tau=tau)
        if agg is not None:
            news = {**agg, "brief": brief or "No summary available.",
                    "assessed_at": now_iso, "lookback_days": days}
    if news is None:
        outcome = "failed"
        old = (prev or {}).get("news")
        if old:
            news = {**old, "stale": True,
                    "stale_reason": llm_status().get("error") or "News read failed"}

    if stage_cb:
        stage_cb("market", 0.85)
    market, vol_20d = _market_read(symbol, ctx, cancel=cancel)
    if market is None and prev and prev.get("market"):
        market = {**prev["market"], "stale": True}

    if stage_cb:
        stage_cb("aggregate", 0.95)
    result = {
        "news": news,
        "market": market,
        "divergence": compute_divergence(news if outcome == "ok" else None, market,
                                         row_ctx, vol_20d),
        "article_count": len(articles),
        "assessed_at": now_iso,
    }
    _cache_put(_SENTIMENT_CACHE, key, result, _SENTIMENT_TTL)
    if outcome == "ok" or market is not None:
        lens_scores = {k: ((news or {}).get("lenses") or {}).get(k, {}) or {}
                       for k in LENSES}
        _history_append({
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "symbol": symbol,
            "news_score": news.get("score") if (news and outcome == "ok") else None,
            "news_tier": news.get("tier") if (news and outcome == "ok") else None,
            "lens": {k: v.get("score") for k, v in lens_scores.items()}
            if outcome == "ok" else None,
            "agreement": news.get("agreement") if (news and outcome == "ok") else None,
            **({f"market_{k}": market.get(k) for k in ("score", "sar", "z", "pct", "tier")}
               if market and not market.get("stale") else {}),
            "market_model": market.get("model_version")
            if market and not market.get("stale") else None,
            "price": (row_ctx or {}).get("price"),
            "beta": ctx.get("beta"),
        })
    return result, outcome


def get_news_sentiment(symbol: str, context: dict | None = None,
                       stage_cb=None, cancel=None) -> dict | None:
    """Per-ticker two-engine read. Cache-first, then fetch + assess.

    `context` (optional): {row: {...}, beta, lookback_days, closes} — the row
    payload feeds quant context into the brief; closes (a shared price frame)
    spare the Market read a per-symbol download during a refresh."""
    ctx = context or {}
    days = _clamp_lookback(ctx.get("lookback_days") or _DEFAULT_LOOKBACK_DAYS)
    cached = _cache_get(_SENTIMENT_CACHE, f"sentiment|{symbol}|{days}")
    if isinstance(cached, dict):
        return cached
    return _assess_symbol(symbol, ctx, stage_cb=stage_cb, cancel=cancel)[0]


def get_cached_sentiment(symbol: str, days: int | None = None) -> dict | None:
    """Cache-only read — safe to call from fetch_one's hot path.

    Reads the requested window first, then sweeps every lookback bucket
    (default-first): the main table's NS dots aren't tied to the News tab's
    window, and the refresh that last scored a row may have used another."""
    order = ([_clamp_lookback(days)] if days else []) + [_DEFAULT_LOOKBACK_DAYS,
                                                         *_LOOKBACK_CHOICES]
    for d in order:
        cached = _cache_get(_SENTIMENT_CACHE, f"sentiment|{symbol}|{d}")
        if isinstance(cached, dict):
            return cached
    return None


def get_cached_sentiments(symbols: list[str], days: int | None = None) -> dict:
    """{symbol: cached two-engine read | None} — never fetches or scores."""
    return {s: get_cached_sentiment(s, days) for s in symbols}


def get_cached_market(days: int | None = None) -> dict | None:
    """The market-wide News read from cache (requested window first)."""
    order = ([_clamp_lookback(days)] if days else []) + [_DEFAULT_LOOKBACK_DAYS,
                                                         *_LOOKBACK_CHOICES]
    for d in order:
        cached = _cache_get(_SENTIMENT_CACHE, f"sentiment|__market__|{d}")
        if isinstance(cached, dict):
            return cached
    return None


def get_cached_market_articles(days: int | None = None) -> list[dict]:
    """The market feed's cached headlines (requested window first)."""
    order = ([_clamp_lookback(days)] if days else []) + [_DEFAULT_LOOKBACK_DAYS,
                                                         *_LOOKBACK_CHOICES]
    for d in order:
        arts = _cache_get(_NEWS_CACHE, f"news|__market__|{d}")
        if arts and arts is not _MISS:
            return arts
    return []


def _assess_market(days: int, stage_cb=None, cancel=None) -> tuple[dict | None, str]:
    """The market-wide News read (same engine, market-risk scale, lens
    restricted to other/none). Shape: {news: {...}, tape, tape_items,
    article_count, assessed_at}."""
    key = f"sentiment|__market__|{days}"
    prev = _cache_get(_SENTIMENT_CACHE, key)
    prev = prev if isinstance(prev, dict) else None
    articles = fetch_market_news(days=days, cancel=cancel)
    if stage_cb:
        stage_cb("fetch", 0.25)
    _ck(cancel)
    if not articles:
        return None, "empty"
    tape, tape_items = _market_tape_context()
    batch = articles[:_SCORE_BATCH]
    read = read_headlines(_MARKET_READ_PROMPT, _build_market_prompt(articles, tape),
                          len(batch), lens_enum=_MARKET_LENSES, stage_cb=stage_cb,
                          cancel=cancel)
    now_iso = datetime.now(timezone.utc).isoformat()
    outcome, news = "ok", None
    if read is not None:
        items, brief, agreement = read
        _stamp_items(batch, items)
        _cache_put(_NEWS_CACHE, f"news|__market__|{days}", articles, _NEWS_TTL)
        agg = aggregate_items(batch, agreement, tau=_tau_for_days(days))
        if agg is not None:
            news = {k: agg[k] for k in ("score", "tier", "confidence", "agreement",
                                        "n_items", "n_none")}
            news.update({"brief": brief or "No summary available.",
                         "assessed_at": now_iso, "lookback_days": days})
    if news is None:
        outcome = "failed"
        old = (prev or {}).get("news")
        if old:
            news = {**old, "stale": True,
                    "stale_reason": llm_status().get("error") or "News read failed"}
    if stage_cb:
        stage_cb("aggregate", 0.95)
    result = {"news": news, "tape": tape, "tape_items": tape_items,
              "article_count": len(articles), "assessed_at": now_iso}
    _cache_put(_SENTIMENT_CACHE, key, result, _SENTIMENT_TTL)
    if outcome == "ok":
        _history_append({"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                         "symbol": "__market__", "news_score": news.get("score"),
                         "news_tier": news.get("tier"), "agreement": news.get("agreement")})
    return result, outcome


def _refresh_symbol_sentiment(symbol: str, context: dict | None = None,
                              stage_cb=None, cancel=None) -> tuple[dict | None, str]:
    """Force-refresh one ticker. The article cache is taken out so the fetch
    is real, and restored if the refresh produced nothing (a transient
    failure must not leave the tape empty) or if the News read failed: the
    previous read is kept (stale), so the headlines it read — and their lens
    tags, which the tape, timeline and lens links show — are kept with it.
    The Market read has already run on the fresh fetch by then."""
    if stage_cb:
        stage_cb("start", 0.05)
    ctx = context or {}
    days = _clamp_lookback(ctx.get("lookback_days") or _DEFAULT_LOOKBACK_DAYS)
    news_key = f"news|{symbol}|{days}"
    prev_news = _cache_take(_NEWS_CACHE, news_key)
    prev_week = _cache_take(_NEWS_CACHE, f"news|{symbol}|7") if days != 7 else None
    try:
        result, outcome = _assess_symbol(symbol, ctx, stage_cb=stage_cb, cancel=cancel)
        if outcome == "failed" and prev_news is not None:
            _cache_restore(_NEWS_CACHE, news_key, prev_news)
        return result, outcome
    finally:
        if _cache_peek_entry(_NEWS_CACHE, news_key) is None:
            _cache_restore(_NEWS_CACHE, news_key, prev_news)
        if prev_week is not None and _cache_peek_entry(_NEWS_CACHE, f"news|{symbol}|7") is None:
            _cache_restore(_NEWS_CACHE, f"news|{symbol}|7", prev_week)


def _refresh_market_sentiment(days: int = _DEFAULT_LOOKBACK_DAYS, stage_cb=None,
                              cancel=None) -> tuple[dict | None, str]:
    news_key = f"news|__market__|{days}"
    prev_news = _cache_take(_NEWS_CACHE, news_key)
    try:
        result, outcome = _assess_market(days, stage_cb=stage_cb, cancel=cancel)
        if outcome == "failed" and prev_news is not None:
            _cache_restore(_NEWS_CACHE, news_key, prev_news)   # same reason as above
        return result, outcome
    finally:
        if _cache_peek_entry(_NEWS_CACHE, news_key) is None:
            _cache_restore(_NEWS_CACHE, news_key, prev_news)


def _symbol_context(symbol: str, context: dict | None, closes=None) -> dict:
    """Per-symbol slice of the request-level context payload
    ({rows, betas, weights, lookback_days} keyed by symbol)."""
    context = context or {}
    return {
        "row": (context.get("rows") or {}).get(symbol),
        "beta": (context.get("betas") or {}).get(symbol),
        "lookback_days": context.get("lookback_days"),
        "closes": closes,
    }


def refresh_sentiment(symbols: list[str], context: dict | None = None,
                      progress_cb=None, *, cancel=None, refresh_market=True,
                      workers: int = 2) -> dict:
    """Force-refresh, staged: the market read first, then constituents sorted
    by portfolio weight descending so the biggest positions land first.

    `progress_cb(kind, payload)` receives: `plan` (all jobs, up front),
    `market_stage` / `symbol_stage` ({stage, frac}; stage is one of
    start|fetch|pass1|pass2|market|aggregate) and the completions `market` /
    `symbol` ({symbol, sentiment, outcome}).

    `cancel` (any object with .is_set()) stops the run at the next checkpoint;
    queued symbols are de-queued outright. Duck-typed so this module never
    imports jobs.py. `refresh_market=False` reuses the cached market read
    across a multi-portfolio job.

    Returns {market, portfolio, status, cancelled} where status carries
    {scored, failed, empty, total, market_ok} — the refresh chip reports
    "12/15 scored" from it and never "done" when nothing was scored.
    """
    ctx = context or {}
    days = _clamp_lookback(ctx.get("lookback_days") or _DEFAULT_LOOKBACK_DAYS)
    weights = ctx.get("weights") or {}
    ordered = sorted(symbols, key=lambda s: -(weights.get(s) or 0.0))
    llm_unblock()
    if progress_cb:
        progress_cb("plan", {"market": refresh_market, "symbols": ordered, "days": days})

    def _market_stage(stage, frac):
        if progress_cb:
            progress_cb("market_stage", {"stage": stage, "frac": frac})

    market, market_outcome = None, "skipped"
    if refresh_market:
        _market_stage("start", 0.05)
        try:
            market, market_outcome = _refresh_market_sentiment(
                days, stage_cb=_market_stage, cancel=cancel)
        except RefreshCancelled:
            if progress_cb:
                progress_cb("market", {"sentiment": None, "outcome": "cancelled"})
            return {"market": None, "portfolio": {}, "status": status(), "cancelled": True}
        if progress_cb:
            progress_cb("market", {"sentiment": market, "outcome": market_outcome})
    else:
        market = get_cached_market(days)

    closes = None
    try:
        from convexity import ml_sentiment as _ml
        if _ml.available() and ordered:
            closes = _ml.load_closes(ordered)
    except Exception as exc:
        _warn_ml_once(f"{type(exc).__name__}: {exc}")

    def _mk_stage_cb(sym):
        if not progress_cb:
            return None

        def cb(stage, frac):
            progress_cb("symbol_stage", {"symbol": sym, "stage": stage, "frac": frac})
        return cb

    portfolio: dict[str, dict | None] = {}
    outcomes: dict[str, str] = {}
    cancelled = cancel is not None and cancel.is_set()
    # NOT a `with` block: ThreadPoolExecutor.__exit__ joins every queued
    # future, so a cancel would wait out the whole portfolio.
    # shutdown(wait=False, cancel_futures=True) de-queues untouched symbols.
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = {}
        for sym in ordered:
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            futures[pool.submit(_refresh_symbol_sentiment, sym,
                                _symbol_context(sym, context, closes),
                                _mk_stage_cb(sym), cancel)] = sym
        for future in as_completed(futures):
            sym = futures[future]
            try:
                portfolio[sym], outcomes[sym] = future.result()
            except RefreshCancelled:
                portfolio[sym], outcomes[sym] = None, "cancelled"
                cancelled = True
            except Exception as exc:
                print(f"[news] {sym}: refresh failed: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                portfolio[sym], outcomes[sym] = None, "failed"
            if progress_cb:
                progress_cb("symbol", {"symbol": sym, "sentiment": portfolio[sym],
                                       "outcome": outcomes[sym]})
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    diag = status()
    diag.update({
        "market_ok": market_outcome in ("ok", "skipped") and market is not None,
        "scored": sum(1 for o in outcomes.values() if o == "ok"),
        "failed": sum(1 for o in outcomes.values() if o == "failed"),
        "empty": sum(1 for o in outcomes.values() if o == "empty"),
        "total": len(symbols),
    })
    return {"market": market, "portfolio": portfolio, "status": diag,
            "cancelled": cancelled}


# ---------------------------------------------------------------------------
# Cache-only surfaces: window re-aggregation, the merged tape
# ---------------------------------------------------------------------------


def _cached_articles_union(symbol: str) -> tuple[list[dict], list[int]]:
    """Every cached article for `symbol`, across ALL lookback windows.

    Returns (articles, windows) where `windows` lists the lookback windows that
    actually contributed — the caller needs it to tell "this window was really
    fetched" from "we're re-weighting a narrower fetch". Deduped on the article
    id; when the same headline is cached in two windows, the copy the News
    read scored wins (only _SCORE_BATCH of each window are read).
    """
    seen: dict[str, dict] = {}
    windows: list[int] = []
    for days in _LOOKBACK_CHOICES:
        arts = _cache_get(_NEWS_CACHE, f"news|{symbol}|{days}")
        if not arts or arts is _MISS:
            continue
        windows.append(days)
        for a in arts:
            key = a.get("aid") or _article_id(a)
            prev = seen.get(key)
            if prev is None or (a.get("lens") and not prev.get("lens")):
                seen[key] = a
    return list(seen.values()), windows


def rescore_window(symbols: list[str], days: int) -> dict:
    """Re-aggregate cached, already-read headlines onto a different window.

    ZERO network, zero LLM, zero ML inference — changing the News window
    (3/7/14/30D) repaints instantly instead of costing a full refresh. Only
    the deterministic lens math re-runs, under the new window's recency tau.
    The Market read is carried unchanged: it is defined on the 7-day window
    it was trained on, whatever the News window.

    THE HONEST LIMITATION, and why `coverage` exists: widening the window
    cannot conjure headlines that were never fetched or read. Narrowing
    (30D -> 7D) is lossless; widening (7D -> 30D) re-weights the same evidence
    under a longer tau. `truncated_by_fetch` says so.

    Returns {days, sentiment: {sym: dict|None}, market: dict|None,
             coverage: {sym: {...}}}.
    """
    days = _clamp_lookback(days)
    tau = _tau_for_days(days)
    now = time.time()
    cutoff = now - days * 86400
    out: dict[str, dict | None] = {}
    coverage: dict[str, dict] = {}
    for sym in symbols:
        union, from_windows = _cached_articles_union(sym)
        windowed = [a for a in union if (a.get("datetime") or 0) >= cutoff]
        batch = [a for a in windowed if a.get("lens")]
        oldest = min((a.get("datetime") or 0) for a in windowed) if windowed else None
        coverage[sym] = {
            "n_scored": len(batch),
            "oldest_ts": oldest,
            "from_windows": from_windows,
            # Derived from WHICH WINDOWS WERE FETCHED, not from how old the
            # newest article happens to be: a quiet ticker with no week-old
            # news is not the same thing as a narrower fetch.
            "truncated_by_fetch": bool(from_windows) and max(from_windows) < days,
        }
        prev = get_cached_sentiment(sym, days)
        if not batch:
            out[sym] = None
            continue
        prev_news = (prev or {}).get("news") or {}
        agreement = prev_news.get("agreement")
        agg = aggregate_items(batch, agreement, now=now, tau=tau)
        if agg is None:
            out[sym] = None
            continue
        news = {**agg,
                # The brief was written for a DIFFERENT headline set and can't
                # be regenerated without a NIM call. Carried forward, flagged.
                "brief": prev_news.get("brief") or "No summary available.",
                "brief_stale": True,
                "brief_window": prev_news.get("lookback_days"),
                "assessed_at": prev_news.get("assessed_at"),
                "lookback_days": days,
                "rescored": True}
        market = (prev or {}).get("market")
        result = {"news": news, "market": market,
                  "divergence": compute_divergence(news, market),
                  "article_count": len(windowed),
                  "assessed_at": (prev or {}).get("assessed_at")}
        # Safe to cache: _refresh_symbol_sentiment computes afresh, so a later
        # real Refresh at this window is never short-circuited by this entry.
        _cache_put(_SENTIMENT_CACHE, f"sentiment|{sym}|{days}", result, _SENTIMENT_TTL)
        out[sym] = result
    # Deliberately NO _history_append: a rescore re-projects evidence already
    # recorded, and writing it would double-count the day in the Track record.
    return {"days": days, "sentiment": out, "market": get_cached_market(days),
            "coverage": coverage}


def get_cached_articles(symbols: list[str], limit: int = 800) -> list[dict]:
    """Merged article feed for the flash tape / timeline — cache-only, O(n),
    never triggers a fetch. Articles carry lens / llm_score / fact when the
    News read has scored them.

    Sweeps the widest cached window per symbol (a 30D cache is a superset of
    what 7D would have fetched). Truncation is time-stratified, NOT a flat
    newest-N cut: a multi-symbol portfolio publishes hundreds of articles a
    day, so newest-250 collapsed the Timeline into the last ~19 hours even
    though every symbol's cache spans the window. window_sample keeps the
    newest `min_recent` intact (the tape renders its freshest ~120) and
    spreads the rest across the window's time buckets."""
    out: list[dict] = []
    for sym in symbols:
        arts = None
        for days in sorted(_LOOKBACK_CHOICES, reverse=True):
            cand = _cache_get(_NEWS_CACHE, f"news|{sym}|{days}")
            if cand and cand is not _MISS:
                arts = cand
                break
        if arts:
            for a in arts:
                b = dict(a)
                b["symbol"] = sym
                out.append(b)
    out.sort(key=lambda a: a.get("datetime") or 0, reverse=True)
    if len(out) > limit:
        out = window_sample(out, cap=limit, min_recent=120)
    return out


def status() -> dict:
    """Diagnostic: keys, quotas, and whether the News read can run."""
    fh = _FH_LIMITER.snapshot()
    nv = _NV_LIMITER.snapshot()
    with _rate_limit_lock:
        fh_backoff = max(0.0, _fh_rate_limit_until - time.time())
        nv_backoff = max(0.0, _nv_rate_limit_until - time.time())
    llm = llm_status()
    return {
        "finnhub_key_set": bool(FINNHUB_API_KEY),
        "nvidia_key_set": bool(NVIDIA_API_KEY),
        "finnhub_calls_used": fh["used"],
        "finnhub_calls_limit": fh["limit"],
        "nvidia_calls_used": nv["used"],
        "nvidia_calls_limit": nv["limit"],
        "finnhub_backoff_s": int(fh_backoff + 0.999) if fh_backoff > 0 else 0,
        "nvidia_backoff_s": int(nv_backoff + 0.999) if nv_backoff > 0 else 0,
        "news_cache_size": len(_NEWS_CACHE),
        "sentiment_cache_size": len(_SENTIMENT_CACHE),
        "llm_model": _MODEL,
        "llm_ok": llm.get("ok"),
        "llm_error": llm.get("error"),
    }


# ---------------------------------------------------------------------------
# Module import — load persisted caches once
# ---------------------------------------------------------------------------

_load_persisted_caches()
