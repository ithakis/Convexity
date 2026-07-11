"""News & sentiment adapter — Finnhub news + NVIDIA NIM AI analysis.

Fetches per-ticker and market-wide news from Finnhub's free API, then
scores each batch through a NVIDIA NIM model (integrate.api.nvidia.com)
to produce a 5-tier sentiment signal (very_bullish → very_bearish).

Caches are disk-backed (`.portfolio_tracker_news.json`) so news/sentiment
survives app restarts. Refresh is user-driven only — the on-disk cache is
treated as effectively permanent (30-day TTL) and only `bust_cache()` or
`refresh_sentiment()` re-fetches.

Both Finnhub and NVIDIA NIM calls flow through a token-bucket rate
limiter (rolling 60s window). On 429 the call sleeps and retries up to
3 times instead of returning None silently.

Degrades cleanly:
- No Finnhub key → no news fetched, all functions return None.
- No NVIDIA key → news fetched but sentiment is None (raw articles
  still available for the News tab).
- Transient API failures → not cached, retried next request.
"""

from __future__ import annotations

import json
import math
import random
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from portfolio_tracker.helpers import _is_rate_limited_error, _load_local_secret, _repo_root
from portfolio_tracker import lexicon as _lex

# API key loading (env var, then a strictly-local file found by walking
# upward from this module's directory) lives in helpers._load_local_secret —
# shared with finnhub_adapter.py so both modules resolve secrets identically.
FINNHUB_API_KEY = _load_local_secret("FINNHUB_API_KEY", ".finnhub_key")
NVIDIA_API_KEY = _load_local_secret("NVIDIA_API_KEY", ".nvidia_key")
_FINNHUB_BASE = "https://finnhub.io/api/v1/"

# ---------------------------------------------------------------------------
# Scoring configuration — every tunable of the sentiment engine in one place
# ---------------------------------------------------------------------------

# Systematic tilt strength: s_total = clip(KAPPA * beta * s_mkt + s_idio).
# Set by an empirical sensitivity study over the S&P 100 (2026-07-08, n=99,
# scripts/kappa_sensitivity.py + scripts/kappa_sensitivity_results.json).
# The binding constraint is that per-article LLM idio scores are COMPRESSED
# (cross-sectional std ~0.12), so the beta*s_mkt term reaches one full
# cross-sectional sigma at kappa=0.5 on an ordinary bearish day (s_mkt
# -0.24) — flipping the sign of 19% of names, migrating 26% of tiers, and
# in a -0.6 stress collapsing the rank correlation vs s_idio to 0.45 (the
# panel would show the market, not the stocks). kappa=0.2 keeps the tilt
# at ~0.4 sigma on typical days (8-11% sign flips, rank corr >= 0.92) while
# still mattering in stress (|s_mkt|=0.6: ~16% flips, rank corr 0.75-0.82).
# Note betas shipped by the frontend are Yahoo's (mean ~1.0, higher than
# the study's 1y-weekly betas, mean 0.67), which makes the effective tilt
# ~1.5x the study's — a further argument for the conservative value.
KAPPA = 0.2

# Score each ticker's article batch twice (temperature 0.1) and average —
# cheap insurance against small-model flakiness (Lopez-Lira & Tang 2023:
# small LLMs are noisy scorers). Doubles NIM calls per refresh; the token-
# bucket limiter absorbs it, staged refresh spreads it.
SELF_CONSISTENCY = True

# Recency decay for article weights: w = exp(-age_days / TAU). TAU=3 days
# gives a ~2.1-day half-life over the default 7-day fetch window.
_RECENCY_TAU_DAYS = 3.0

# User-selectable analysis window (News tab lookback control). The recency
# tau scales proportionally with the window (tau = days * 3/7) so a longer
# lookback actually uses its older articles instead of e^-10'ing them to
# zero — at the default 7 days the math is bit-identical to the validated
# tau=3.0 behaviour.
_DEFAULT_LOOKBACK_DAYS = 7
_LOOKBACK_CHOICES = (3, 7, 14, 30)

# Load-bearing pair: `_SCORE_BATCH` is both "how many articles get AI-scored"
# (get_news_sentiment/get_market_sentiment slice articles[:_SCORE_BATCH]) AND
# the `min_recent` passed to _window_sample — the two MUST match, or scoring
# would silently include time-bucket-sampled older articles instead of
# genuinely-most-recent ones, breaking the recency-weighted aggregation's
# assumption. `_ARTICLE_CAP` is the retained total (scored + time-sampled)
# that the timeline/tape render from.
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

# Relevance weights. "low" is deliberately tiny (not 0) so an article where the
# target is not the real subject can only nudge, never dominate — the guardrail
# against a competitor's/partner's news bleeding onto the target when the model
# mis-tags relevance (see the GOOGL/Cognizant miss the target-attribution prompt
# rules address). The prompt already forces low-relevance scores toward 0.0;
# this is the belt-and-braces on the aggregation side.
_W_RELEVANCE = {"high": 1.0, "med": 0.6, "low": 0.1}

_VALID_EVENTS = {"earnings", "guidance", "ma", "analyst", "legal_regulatory",
                 "product", "insider", "macro", "other"}

# Title-similarity threshold for syndication dedup (rapidfuzz token_set_ratio).
_DEDUP_SIMILARITY = 85

# Tier calibration: percentiles of the trailing pooled score distribution
# (10/20/40/20/10 by construction — guarantees full range usage). Until
# _MIN_CALIBRATION_N observations exist, fixed thresholds apply.
_TIER_QUANTILES = ((0.10, "very_bearish"), (0.30, "bearish"),
                   (0.70, "neutral"), (0.90, "bullish"))
_FIXED_TIER_THRESHOLDS = ((-0.5, "very_bearish"), (-0.15, "bearish"),
                          (0.15, "neutral"), (0.5, "bullish"))
_MIN_CALIBRATION_N = 100
_CALIBRATION_WINDOW_DAYS = 90

# LLM-vs-lexicon disagreement gate (see aggregate + serving logic): strong
# opposite-sign disagreement flags the ticker and caps confidence.
_DISAGREEMENT_GAP = 0.6
_DISAGREEMENT_MIN_ABS = 0.2

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
# Disk persistence — `.portfolio_tracker_news.json`
# ---------------------------------------------------------------------------

_PERSIST_FILE = _repo_root() / ".portfolio_tracker_news.json"
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
            "version": 2,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "news": news_out,
            "sentiment": sent_out,
        }, ensure_ascii=True, indent=2)
        with _PERSIST_LOCK:
            _PERSIST_FILE.write_text(body + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"[news] failed to persist caches: {exc}", file=sys.stderr)


def _load_persisted_caches() -> None:
    """Rehydrate caches from disk at import time. Resets timestamps to now so
    persisted entries get a fresh in-session lifetime — refresh remains
    user-driven."""
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
    loaded_news = 0
    loaded_sent = 0
    version = data.get("version") or 1
    with _CACHE_LOCK:
        for k, entry in (data.get("news") or {}).items():
            if not isinstance(entry, dict):
                continue
            val = entry.get("value")
            if val is None:
                continue
            _NEWS_CACHE[k] = (now, _NEWS_TTL, val)
            loaded_news += 1
        for k, entry in (data.get("sentiment") or {}).items():
            if not isinstance(entry, dict):
                continue
            val = entry.get("value")
            if val is None:
                continue
            # v1 sentiment entries predate the decomposed shape (no s_total/
            # confidence) — drop them so the UI never renders a stale schema.
            # News entries are shape-stable and kept either way.
            if version < 2 and isinstance(val, dict) and "s_total" not in val:
                continue
            _SENTIMENT_CACHE[k] = (now, _SENTIMENT_TTL, val)
            loaded_sent += 1
    if loaded_news or loaded_sent:
        print(f"[news] rehydrated {loaded_news} news + {loaded_sent} sentiment entries from disk",
              file=sys.stderr)


def bust_cache() -> None:
    """Clear all news and sentiment caches (manual refresh)."""
    with _CACHE_LOCK:
        _NEWS_CACHE.clear()
        _SENTIMENT_CACHE.clear()
    try:
        with _PERSIST_LOCK:
            if _PERSIST_FILE.exists():
                _PERSIST_FILE.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Token-bucket rate limiter — rolling 60s window
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Sleeps (not fails) when at the per-minute budget. On 429 the caller
    invokes `penalize()` to backfill the window so subsequent calls wait."""

    def __init__(self, max_per_min: int, name: str):
        self.max = max_per_min
        self.name = name
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.time()
                while self._calls and now - self._calls[0] > 60.0:
                    self._calls.popleft()
                if len(self._calls) < self.max:
                    self._calls.append(now)
                    return
                wait = 60.0 - (now - self._calls[0]) + 0.05
            time.sleep(min(max(wait, 0.05), 5.0))

    def penalize(self) -> None:
        """Backfill the rolling window so further acquires block ~60s."""
        with self._lock:
            t = time.time()
            slots = self.max - len(self._calls)
            for _ in range(max(slots, 0)):
                self._calls.append(t)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            now = time.time()
            while self._calls and now - self._calls[0] > 60.0:
                self._calls.popleft()
            return {"used": len(self._calls), "limit": self.max}


_FH_LIMITER = _RateLimiter(max_per_min=55, name="finnhub")
_NV_LIMITER = _RateLimiter(max_per_min=60, name="nvidia")
# yfinance has no official rate limit and no app-level throttle anywhere else
# in the codebase (price/analytics paths burst at 5-8 workers). Windowed news
# now fetches deeper per ticker AND for benchmark indices, so a full-portfolio
# refresh could realistically burst Yahoo into a 429. This limiter caps just
# the news calls; kept conservative because it shares Yahoo's backend with the
# unthrottled price/analytics traffic.
_YF_LIMITER = _RateLimiter(max_per_min=40, name="yfinance-news")
_YF_MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Finnhub news fetching
# ---------------------------------------------------------------------------

_RATE_LIMIT_BACKOFF_S = 65
_RETRY_SLEEP_S = 5.0
_MAX_RETRIES = 3

_rate_limit_lock = threading.Lock()
_fh_rate_limit_until = 0.0  # last-resort circuit breaker after repeated 429s


def _wait_for_circuit_breaker(label: str, until_attr: str) -> None:
    """Sleep through a breaker window instead of returning empty data.

    The user explicitly asked for rate-limited refreshes to wait/retry rather
    than blanking the News panel. We therefore treat the breaker as a sleep
    gate, polling at the same 5s cadence used for HTTP 429 retries.
    """
    warned = False
    while True:
        with _rate_limit_lock:
            wait = globals()[until_attr] - time.time()
        if wait <= 0:
            return
        if not warned:
            print(f"[news] {label} backoff active — sleeping until the window reopens",
                  file=sys.stderr)
            warned = True
        time.sleep(min(_RETRY_SLEEP_S, max(wait, 0.1)))


def _fh_call(path: str, params: dict[str, Any]) -> Any | None:
    global _fh_rate_limit_until
    if not FINNHUB_API_KEY:
        return None
    # Last-resort circuit breaker: if we keep getting 429s even after the
    # rolling limiter, pause everything for 65s.
    _wait_for_circuit_breaker("Finnhub", "_fh_rate_limit_until")
    query = dict(params)
    query["token"] = FINNHUB_API_KEY
    url = _FINNHUB_BASE + path + "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={"User-Agent": "PortfolioTracker/1.0"})

    for attempt in range(_MAX_RETRIES):
        _FH_LIMITER.acquire()
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


def _norm_title(title: str) -> str:
    return " ".join("".join(c if c.isalnum() or c.isspace() else " "
                            for c in (title or "").lower()).split())


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
        norm = _norm_title(a.get("headline", ""))
        if not norm:
            continue
        dup_of = None
        for i, kn in enumerate(kept_norms):
            if _fuzz is not None:
                if _fuzz.token_set_ratio(norm, kn) >= _DEDUP_SIMILARITY:
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
        kept.append(a)
        kept_norms.append(norm)
    kept.sort(key=lambda a: a.get("datetime") or 0, reverse=True)
    return kept


def _window_sample(articles: list[dict], cap: int, min_recent: int) -> list[dict]:
    """Trim to ~`cap` while spanning the lookback window instead of collapsing
    to the newest `cap` articles.

    High-volume tickers (NVDA, MSFT) publish enough that the newest N cluster
    within hours, so a plain newest-N cut makes the timeline show only "today"
    even though older articles were fetched. We keep the newest `min_recent`
    verbatim (AI scoring reads articles[:min_recent], recency-weighted, and
    must stay genuinely most-recent) then TIME-stratify the older remainder:
    split its time span into `slots` equal buckets and keep the newest article
    from each non-empty bucket. Stratifying by time (not by index) is the point
    — index striding over-samples the dense recent cluster and still drops the
    older days. Input and output are newest-first; the result may be smaller
    than `cap` when older buckets are empty (coverage beats raw count here).
    """
    if len(articles) <= cap:
        return articles
    recent = articles[:min_recent]
    rest = articles[min_recent:]
    slots = cap - min_recent
    if slots <= 0 or not rest:
        return recent
    # Undated articles (datetime missing/0 — e.g. an unparsed yfinance pubDate)
    # have no time signal to stratify on. A single one used to collapse tmin
    # to 0 and blow up the whole span, so they're excluded from bucketing and
    # merged back in afterward, capped to whatever slot budget remains.
    dated = [a for a in rest if a.get("datetime")]
    undated = [a for a in rest if not a.get("datetime")]
    if not dated:
        return recent + undated[:slots]
    times = [a["datetime"] for a in dated]
    tmin, tmax = min(times), max(times)
    if tmax <= tmin:
        sampled = dated[:slots]
    else:
        span = tmax - tmin
        buckets: dict[int, dict] = {}
        for a in dated:
            t = a["datetime"]
            idx = min(slots - 1, int((t - tmin) / span * slots))
            if idx not in buckets or t > buckets[idx]["datetime"]:
                buckets[idx] = a  # newest per time-bucket
        sampled = list(buckets.values())
    leftover = max(0, slots - len(sampled))
    result = recent + sampled + undated[:leftover]
    return sorted(result, key=lambda a: a.get("datetime") or 0, reverse=True)


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


def _fetch_yf_news(symbol: str, days: int) -> list[dict]:
    """yfinance news merged as a secondary source — free, keyless, and the
    only coverage for many non-US tickers where Finnhub free returns [].
    Flaky by nature: any failure degrades silently to Finnhub-only.

    Uses get_news(count=N), NOT the `.news` property: `.news` is hard-capped
    at ~10 most-recent items, so on an active ticker it never spans more than
    a day or two regardless of the requested window. `count` scales with the
    window (capped at 100 to bound cost) and the results are then filtered to
    the window client-side below. A FRESH Ticker is built every call on
    purpose — Ticker caches its news per-instance and would otherwise ignore
    a larger `count` on a reused object. Globally throttled via _YF_LIMITER +
    jittered retry so a full-portfolio windowed refresh can't burst Yahoo
    into a 429 (mirrors fetcher.fetch_one's 429 handling)."""
    import yfinance as yf  # hoisted out of the loop — re-importing per attempt
                            # wasted a rate-limit slot before the module-cache
                            # hit even resolved on the very first (cold) call
    count = min(100, max(30, days * 8))
    raw: list = []
    for attempt in range(_YF_MAX_RETRIES):
        _YF_LIMITER.acquire()
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


def fetch_company_news(symbol: str, days: int = 7) -> list[dict] | None:
    """Fetch recent news for a single ticker from Finnhub + yfinance,
    deduplicated. Returns normalised article list (most recent first).

    Keyed by `days` — mirrors fetch_market_news: a 7d and 30d GET must not
    collide/shadow each other, otherwise switching the News-tab window
    silently keeps serving whichever window was cached first."""
    cache_key = f"news|{symbol}|{days}"
    cached = _cache_get(_NEWS_CACHE, cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached

    to_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    from_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    raw = _fh_call("company-news", {"symbol": symbol, "from": from_date, "to": to_date})
    articles: list[dict] = []
    fh_failed = raw is None  # transient failure vs "returned but empty"
    if isinstance(raw, list):
        # Deep, window-scaled slice, NOT a flat newest-30/150 — Finnhub
        # returns newest-first within the from/to range, so a fixed cap
        # still collapses a wide window to its newest days for a high-volume
        # ticker. Scales like the yfinance count below; _window_sample then
        # spreads the retained set across the window.
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
    articles.extend(_fetch_yf_news(symbol, days))
    if not articles:
        if fh_failed:
            return None  # transient — don't negative-cache
        _cache_put(_NEWS_CACHE, cache_key, _MISS, _NEG_TTL)
        return None
    # Span the window (timeline/tape) rather than keeping only the newest N.
    # AI scoring still reads the newest 15 (min_recent) in get_news_sentiment;
    # recency-weighted aggregation keeps that split sound.
    articles = _window_sample(_dedup_articles(articles), cap=_ARTICLE_CAP, min_recent=_SCORE_BATCH)
    # A Finnhub outage still lets yfinance-only articles through above (better
    # than nothing), but that's a DEGRADED result — cache it briefly, not for
    # the full 30-day _NEWS_TTL, so a real Finnhub recovery isn't masked for
    # a month. (Mirrors the "transient failure never long-caches" rule below.)
    _cache_put(_NEWS_CACHE, cache_key, articles, _NEG_TTL if fh_failed else _NEWS_TTL)
    return articles


# Benchmark indices whose yfinance news gives the market feed a real window.
# Finnhub's general feed has no date param (latest ~30 only), so windowed
# market coverage comes from these tickers via the same _fetch_yf_news path.
_MARKET_INDEX_TICKERS = ("^GSPC", "^IXIC")


def fetch_market_news(days: int = _DEFAULT_LOOKBACK_DAYS) -> list[dict] | None:
    """Fetch general market news (not ticker-specific).

    Finnhub `category=general` supplies the latest general feed (no window),
    merged with windowed yfinance news for the benchmark indices so the market
    timeline spans the selected window instead of showing only the latest.
    Keyed by `days` so different windows don't shadow each other in cache."""
    cache_key = f"news|__market__|{days}"
    cached = _cache_get(_NEWS_CACHE, cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached

    raw = _fh_call("news", {"category": "general"})
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
    # Windowed index news (S&P 500, Nasdaq) — merged so the market feed honours
    # the lookback window. Each is individually best-effort inside _fetch_yf_news.
    for idx in _MARKET_INDEX_TICKERS:
        articles.extend(_fetch_yf_news(idx, days))
    if not articles:
        if fh_failed:
            return None  # transient Finnhub failure and no yf news — don't negative-cache
        _cache_put(_NEWS_CACHE, cache_key, _MISS, _NEG_TTL)
        return None
    articles = _window_sample(_dedup_articles(articles), cap=_ARTICLE_CAP, min_recent=_SCORE_BATCH)
    # Same degraded-result rule as fetch_company_news: a Finnhub outage still
    # lets an index-only (yfinance) feed through, but it must not lock in for
    # the full 30-day _NEWS_TTL — that would silently mask a Finnhub recovery.
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
# Deterministic aggregation, decomposition, calibration — the auditable math
# between per-article LLM scores and the signal the UI shows. Pure functions,
# unit-tested in tests/test_news_sentiment.py.
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
    """w = recency x source x novelty x relevance (plan §2.3). `tau` is the
    recency e-folding in days — defaults to the validated 3.0; the lookback
    control scales it with the window via _tau_for_days()."""
    now = now or time.time()
    tau = tau or _RECENCY_TAU_DAYS
    ts = article.get("datetime") or 0
    age_days = max(0.0, (now - ts) / 86400.0) if ts else tau
    w_recency = math.exp(-age_days / tau)
    w_source = _source_weight(article.get("source", ""))
    n_dup = article.get("n_duplicates", 0) or 0
    w_novelty = 1.0 / (1.0 + 0.5 * math.log1p(n_dup))
    w_rel = _W_RELEVANCE.get(article.get("relevance", "med"), 0.6)
    return w_recency * w_source * w_novelty * w_rel


def aggregate_scores(articles: list[dict], now: float | None = None,
                     tau: float | None = None) -> dict | None:
    """Weighted mean of per-article scores -> {s_idio, confidence, dispersion,
    n_articles}. Articles need `score` plus the weight fields (datetime,
    source, n_duplicates, relevance). Articles without a numeric score are
    skipped; returns None when nothing is scorable."""
    scored = [a for a in articles if isinstance(a.get("score"), (int, float))]
    if not scored:
        return None
    weights = [_article_weight(a, now, tau) for a in scored]
    total_w = sum(weights)
    if total_w <= 0:
        return None
    s = sum(w * float(a["score"]) for w, a in zip(weights, scored)) / total_w
    s = max(-1.0, min(1.0, s))
    # Weighted dispersion — high disagreement between articles lowers
    # confidence (mixed news is genuinely lower-conviction than uniform news).
    var = sum(w * (float(a["score"]) - s) ** 2 for w, a in zip(weights, scored)) / total_w
    dispersion = math.sqrt(var)
    # Confidence saturates with effective evidence mass and is discounted by
    # dispersion. Bounds: one stale wire article ~0.15; five fresh tier-1
    # articles agreeing ~0.9.
    confidence = (1.0 - math.exp(-total_w / 2.0)) * (1.0 - min(dispersion, 1.0) * 0.5)
    return {
        "s_idio": round(s, 4),
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "dispersion": round(dispersion, 4),
        "n_articles": len(scored),
    }


def lm_aggregate(articles: list[dict], now: float | None = None,
                 tau: float | None = None) -> float | None:
    """Loughran-McDonald lexicon score aggregated with the same weights —
    the free cross-check signal (plan §2.5)."""
    pairs = []
    for a in articles:
        s = _lex.lm_score(f"{a.get('headline', '')} {a.get('summary', '')}")
        if s is not None:
            pairs.append((_article_weight(a, now, tau), s))
    if not pairs:
        return None
    total_w = sum(w for w, _ in pairs)
    if total_w <= 0:
        return None
    return round(sum(w * s for w, s in pairs) / total_w, 4)


def is_disagreement(s_idio: float, s_lm: float | None) -> bool:
    """Strong opposite-sign LLM-vs-lexicon disagreement — the guardrail
    against hallucinated polarity. Flags the ticker and caps confidence."""
    if s_lm is None:
        return False
    return (abs(s_idio - s_lm) > _DISAGREEMENT_GAP
            and abs(s_idio) > _DISAGREEMENT_MIN_ABS
            and abs(s_lm) > _DISAGREEMENT_MIN_ABS
            and (s_idio > 0) != (s_lm > 0))


def combine_signal(s_idio: float, beta: float | None, s_mkt: float | None,
                   kappa: float = None) -> float:
    """s_total = clip(kappa * beta * s_mkt + s_idio). The systematic term
    tilts the stock signal by market sentiment scaled by its market exposure
    (user requirement: high-beta holdings inherit S&P mood even when their
    own news is fine). Missing beta or market sentiment -> pure s_idio."""
    if beta is None or s_mkt is None:
        return max(-1.0, min(1.0, s_idio))
    k = KAPPA if kappa is None else kappa
    return max(-1.0, min(1.0, k * float(beta) * float(s_mkt) + s_idio))


def calibrate_tier(s_total: float, history_scores: list[float] | None = None) -> str:
    """Map a raw score to a 5-tier label by percentile rank against the
    trailing pooled score distribution (10/20/40/20/10 by construction —
    full range usage is guaranteed structurally, not hoped for). Falls back
    to fixed thresholds until enough history exists."""
    hist = [s for s in (history_scores or []) if isinstance(s, (int, float))]
    if len(hist) >= _MIN_CALIBRATION_N:
        rank = sum(1 for h in hist if h < s_total) / len(hist)
        for q, tier in _TIER_QUANTILES:
            if rank <= q + 1e-12:
                return tier
        return "very_bullish"
    for thresh, tier in _FIXED_TIER_THRESHOLDS:
        if s_total <= thresh:
            return tier
    return "very_bullish"


# ---------------------------------------------------------------------------
# Sentiment history — append-only daily records powering tier calibration and
# the diagnostics panel (IC vs forward returns). One record per symbol per
# refresh, same-day re-refresh overwrites. Capped at _HISTORY_MAX_DAYS.
# ---------------------------------------------------------------------------

_HISTORY_FILE = _repo_root() / ".portfolio_tracker_sentiment_history.json"
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
            _HISTORY_FILE.write_text(
                json.dumps({"version": 1, "records": records}) + "\n",
                encoding="utf-8")
    except Exception as exc:
        print(f"[news] failed to append sentiment history: {exc}", file=sys.stderr)


def _calibration_scores() -> list[float]:
    """Pooled s_total scores from the trailing calibration window."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=_CALIBRATION_WINDOW_DAYS)).strftime("%Y-%m-%d")
    return [r["s_total"] for r in _history_load()
            if (r.get("date") or "") >= cutoff
            and isinstance(r.get("s_total"), (int, float))
            and r.get("symbol") != "__market__"]


# ---------------------------------------------------------------------------
# AI sentiment engine — OpenRouter + nvidia/nemotron
# ---------------------------------------------------------------------------

_SENTIMENT_SYSTEM_PROMPT = """You are a senior quantitative equity analyst scoring news sentiment.

FRAMEWORK — Structural Asset Pricing Decomposition:
  Returns(company) = Returns(market) × Beta + Returns(idiosyncratic) + Noise
News primarily drives the idiosyncratic component. Your job is to assess the INCREMENTAL information content of the news relative to market expectations.

SCORING PROTOCOL:
1. SEPARATE market-wide signals (macro, fed policy, index moves) from company-specific signals (earnings, management, regulation, competitive dynamics, contracts).
2. ASSESS CASH FLOW MATERIALITY: Does this news materially affect expected future cash flows (revenue growth, margins, capex needs) or the discount rate (risk premium shift, credit spread)?
3. GAUGE VS EXPECTATIONS: A strong earnings report that matches consensus is NEUTRAL, not bullish. Only SURPRISES relative to the market's prior beliefs move the score.
4. WEIGHT BY RECENCY AND SOURCE CREDIBILITY: Recent articles from established financial outlets carry more weight than older or less credible sources.

OUTPUT FORMAT — respond with ONLY a JSON object, no markdown, no explanation:
{"tier": "<tier>", "score": <float>, "summary": "<1-2 sentence Bloomberg-style summary>"}

TIERS (mutually exclusive):
- "very_bullish"  (score +0.7 to +1.0): Material positive surprise — earnings beat, transformative deal, major regulatory tailwind
- "bullish"       (score +0.3 to +0.7): Moderately positive — upgraded guidance, favorable sector rotation, positive sentiment shift
- "neutral"       (score -0.3 to +0.3): No material new signal — routine coverage, mixed signals, already priced in
- "bearish"       (score -0.7 to -0.3): Moderately negative — downgraded guidance, competitive threat, margin pressure
- "very_bearish"  (score -1.0 to -0.7): Material negative surprise — earnings miss, regulatory crackdown, management crisis, fraud

RULES:
- Default to "neutral" when articles are generic, promotional, or lack substance.
- The summary must read like a Bloomberg terminal flash — factual, terse, no opinion words like "exciting" or "concerning."
- Score must be consistent with the tier boundaries above.
- If no articles are provided, return {"tier": "neutral", "score": 0.0, "summary": "No recent news coverage."}"""

_MARKET_SENTIMENT_SYSTEM_PROMPT = """You score market news sentiment. Respond with ONLY a JSON object, no markdown:
{"tier": "<tier>", "score": <float>, "summary": "<1 sentence Bloomberg-style summary>"}

Tiers: very_bullish (+0.7 to +1.0), bullish (+0.3 to +0.7), neutral (-0.3 to +0.3), bearish (-0.7 to -0.3), very_bearish (-1.0 to -0.7).
Focus on monetary policy surprises, economic data vs consensus, geopolitical risk, and market breadth. Default neutral when signals are mixed."""

# Per-article scoring prompts (the primary path; the blob prompts above are
# the fallback when array output fails validation twice). The editorial
# rules codify "The Bloomberg Way" (Winkler): headline-first discipline, a
# compressed four-paragraph lead (theme with magnitude -> details vs
# expectations -> what's at stake), prefer the short/familiar/specific,
# show-don't-tell, no opinion adjectives.

_ARRAY_SYSTEM_PROMPT = """You are a senior quantitative equity analyst. Score each news article INDIVIDUALLY for its impact on THE TARGET COMPANY ONLY, then write one brief.

TARGET ATTRIBUTION (most important rule — read first):
- Score ONLY the incremental impact on the target company named in the prompt. A headline often names several companies; identify who the news is actually ABOUT and who merely gets a mention.
- Negative (or positive) news whose SUBJECT is a competitor, customer, supplier, or partner is NOT the target's sentiment. Unless the article states a direct effect on the target, its score for the target is 0.0 with relevance "low" — even if the headline's overall tone is strongly negative.
- A rival's bad news can be neutral or mildly positive for the target (share gain); do not copy the rival's negative tone onto the target. Judge from the target's point of view like a human analyst would, not from the headline's surface tone.

SCORING each article (idiosyncratic sentiment for the target only):
- score: float in [-1,+1]. Target-specific incremental information relative to market expectations. Strip the market-wide component: macro/index/fed stories are NOT company signal (relevance "low", score 0.0 unless the article states a target-specific impact).
- News that merely matches consensus is 0.0. Only surprises move the score. Promotional or generic coverage is 0.0.
- event: one of "earnings","guidance","ma","analyst","legal_regulatory","product","insider","macro","other".
- relevance: "high" only if the target is the PRIMARY subject; "med" if the target is one of several roughly co-equal subjects; "low" if the target is mentioned in passing, is not the story's subject, or the story is macro. When relevance is "low" the score MUST be 0.0 unless the article names a concrete effect on the target.

BRIEF (Bloomberg Way, 2-4 sentences, max 60 words):
- Sentence 1 (theme): what happened + magnitude + why, ticker/company first, past or present simple, active voice. Use the QUANT CONTEXT numbers when given.
- Sentence 2 (details): the numbers versus expectations (consensus, guidance, targets).
- Sentence 3 (stake, optional): what it means for the holding (valuation, upcoming catalyst).
- Every claim must trace to a provided article or the quant context. No opinion words (banned: exciting, concerning, impressive, notably, significantly, "continues to"), no hedging, no exclamation marks. If coverage is thin or immaterial, say so in one sentence.

OUTPUT — ONLY this JSON object, no markdown, no explanation:
{"articles":[{"id":1,"score":0.0,"event":"other","relevance":"med"}, ...],"brief":"<the brief>"}
Include every article id exactly once.

Example output for 3 articles about a company that raised guidance while a macro story tagged along:
{"articles":[{"id":1,"score":0.55,"event":"guidance","relevance":"high"},{"id":2,"score":0.1,"event":"analyst","relevance":"med"},{"id":3,"score":0.0,"event":"macro","relevance":"low"}],"brief":"Acme raised full-year revenue guidance to $4.2 billion, above the $4.0 billion consensus, and shares rose 3.1% this week. Two analysts lifted targets following the update. The stock trades 12% below its high with earnings due March 4."}"""

_MARKET_ARRAY_SYSTEM_PROMPT = """You are a senior macro strategist. Score each market news article INDIVIDUALLY, then write one brief.

SCORING each article:
- score: float in [-1,+1] for aggregate US equity market risk appetite. Focus on monetary policy surprises, economic data versus consensus, geopolitical risk, credit conditions, market breadth. Routine or already-priced stories are 0.0.
- event: one of "macro","earnings","legal_regulatory","ma","other".
- relevance: "high" for market-moving macro/policy stories, "med" for sector-level, "low" for single-stock or human-interest stories.

BRIEF (Bloomberg Way, 1-2 sentences, max 40 words): the systematic-risk picture — what the market did / faces and why, with numbers from the CROSS-ASSET TAPE when given. It ends by characterising risk appetite (e.g. "a headwind for high-beta holdings"). No opinion adjectives, no hedging.

OUTPUT — ONLY this JSON object, no markdown:
{"articles":[{"id":1,"score":0.0,"event":"macro","relevance":"high"}, ...],"brief":"<the brief>"}
Include every article id exactly once."""

# nvidia/llama-3.1-nemotron-nano-8b-v1 (the original choice) is still listed in
# the NIM catalog but no longer actually served — chat completions against it
# hang indefinitely (verified: no response in 90s, vs 0.79s for the model
# below). nemotron-nano-9b-v2 is its current successor and returns structured
# JSON reliably. If NS ever goes dark again, re-check the served model list at
# integrate.api.nvidia.com/v1/models before assuming a code bug.
_MODEL = "nvidia/nvidia-nemotron-nano-9b-v2"
_NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
# Hard ceiling on any single NIM request. Without it the OpenAI SDK default is
# 600s, so one hung/deprecated model would block a warm-up worker for ten
# minutes and NS dots would never fill. 30s is generous for an 8-9B model.
_NV_TIMEOUT_S = 30.0

# Last-resort circuit breaker after repeated 429s
_nv_rate_limit_until = 0.0

# Lazy-initialized OpenAI-compatible client (reused across calls). The `openai`
# import itself is deferred into _get_client() — importing it eagerly costs
# ~1.2s (it pulls in the whole openai.types.* tree), which would otherwise sit
# on the desktop app's startup path even though AI sentiment scoring only ever
# runs on-demand (user-triggered news refresh / background warm-up).
_nvidia_client: Any = None
_client_lock = threading.Lock()


def _get_client() -> Any:
    global _nvidia_client
    if _nvidia_client is not None:
        return _nvidia_client
    try:
        from openai import OpenAI as _OpenAI
    except ImportError:
        return None
    with _client_lock:
        if _nvidia_client is None:
            # timeout caps a hung request at _NV_TIMEOUT_S (SDK default is 600s);
            # max_retries=0 so our own _MAX_RETRIES loop is the only retry layer.
            _nvidia_client = _OpenAI(
                base_url=_NVIDIA_BASE, api_key=NVIDIA_API_KEY,
                timeout=_NV_TIMEOUT_S, max_retries=0,
            )
        return _nvidia_client


def _nvidia_call(system_prompt: str, user_content: str,
                 max_tokens: int = 512) -> dict | None:
    """Call NVIDIA NIM with rolling-window rate limiting + 429 retry."""
    global _nv_rate_limit_until
    if not NVIDIA_API_KEY:
        return None
    _wait_for_circuit_breaker("NVIDIA NIM", "_nv_rate_limit_until")
    client = _get_client()
    if client is None:
        return None
    # nemotron-nano is a hybrid reasoning model: on a complex prompt it burns
    # the entire max_tokens budget on an internal <think> pass (returned in a
    # separate reasoning_content field) and emits content=None — so the JSON
    # we need never arrives. The "/no_think" control token disables that pass,
    # giving a direct JSON answer in ~1.7s instead of a truncated non-answer.
    # (Verified: "detailed thinking off" and chat_template_kwargs do NOT work
    # for this model; only the leading control token does.)
    messages = [
        {"role": "system", "content": "/no_think\n" + system_prompt},
        {"role": "user", "content": user_content},
    ]

    for attempt in range(_MAX_RETRIES):
        _NV_LIMITER.acquire()
        try:
            response = client.chat.completions.create(
                model=_MODEL,
                messages=messages,
                temperature=0.1,
                max_tokens=max_tokens,
            )
            text = response.choices[0].message.content
            if text is None or not text.strip():
                # /no_think is a mitigation, not a guarantee — the model can
                # still emit an empty content=None on an off attempt. Retry
                # like the JSONDecodeError/429 branches below instead of
                # giving up on the first empty response.
                print(f"[news] NVIDIA NIM returned empty content (attempt {attempt+1})",
                      file=sys.stderr)
                if attempt == _MAX_RETRIES - 1:
                    return None
                time.sleep(_RETRY_SLEEP_S)
                continue
            text = text.strip()
            # Belt-and-suspenders: strip a leading <think>…</think> block if the
            # model emits one despite /no_think (empty or otherwise).
            if text.startswith("<think>"):
                end = text.find("</think>")
                text = (text[end + len("</think>"):] if end != -1 else "").strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            return json.loads(text)
        except json.JSONDecodeError:
            print(f"[news] NVIDIA NIM returned non-JSON (attempt {attempt+1})",
                  file=sys.stderr)
            if attempt == _MAX_RETRIES - 1:
                return None
            time.sleep(_RETRY_SLEEP_S)
            continue
        except Exception as exc:
            exc_str = str(exc)
            if "429" in exc_str or "rate limit" in exc_str.lower():
                _NV_LIMITER.penalize()
                print(f"[news] NVIDIA NIM 429 (attempt {attempt+1}/{_MAX_RETRIES}) — "
                      f"sleeping {_RETRY_SLEEP_S}s", file=sys.stderr)
                time.sleep(_RETRY_SLEEP_S)
                if attempt == _MAX_RETRIES - 1:
                    with _rate_limit_lock:
                        _nv_rate_limit_until = time.time() + _RATE_LIMIT_BACKOFF_S
                continue
            print(f"[news] NVIDIA NIM call failed: {exc}", file=sys.stderr)
            return None
    return None


_VALID_TIERS = {"very_bullish", "bullish", "neutral", "bearish", "very_bearish"}

_TIER_SCORE_RANGES = {
    "very_bullish": (0.7, 1.0),
    "bullish": (0.3, 0.7),
    "neutral": (-0.3, 0.3),
    "bearish": (-0.7, -0.3),
    "very_bearish": (-1.0, -0.7),
}


def _validate_sentiment(raw: dict | None) -> dict | None:
    """Validate and normalise AI output into a clean sentiment dict."""
    if raw is None:
        return None
    tier = raw.get("tier", "").lower().strip()
    if tier not in _VALID_TIERS:
        return None
    score = raw.get("score")
    if not isinstance(score, (int, float)):
        return None
    score = max(-1.0, min(1.0, float(score)))
    lo, hi = _TIER_SCORE_RANGES[tier]
    if score < lo or score > hi:
        score = (lo + hi) / 2
    summary = str(raw.get("summary", "")).strip()
    if not summary:
        summary = "No summary available."
    return {
        "tier": tier,
        "score": score,
        "summary": summary,
    }


def _validate_article_scores(raw: dict | None, n_articles: int) -> tuple[list[dict], str] | None:
    """Strict validation of the array-prompt output. Returns (scores, brief)
    where scores is a list aligned to article index (0-based), or None when
    the payload is unusable (caller retries, then falls back to blob)."""
    if not isinstance(raw, dict):
        return None
    items = raw.get("articles")
    if not isinstance(items, list) or not items:
        return None
    by_id: dict[int, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        if not (1 <= idx <= n_articles) or idx in by_id:
            continue
        score = it.get("score")
        if not isinstance(score, (int, float)):
            continue
        event = str(it.get("event", "other")).lower().strip()
        rel = str(it.get("relevance", "med")).lower().strip()
        by_id[idx] = {
            "score": max(-1.0, min(1.0, float(score))),
            "event": event if event in _VALID_EVENTS else "other",
            "relevance": rel if rel in _W_RELEVANCE else "med",
        }
    # Require coverage of at least half the articles — a fragmentary reply
    # is treated as a failed call rather than silently thin evidence.
    if len(by_id) < max(1, n_articles // 2):
        return None
    brief = str(raw.get("brief", "")).strip()[:400]
    scores = [by_id.get(i + 1) for i in range(n_articles)]
    return scores, brief


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


def _build_articles_prompt(articles: list[dict], symbol: str,
                           row_ctx: dict | None = None) -> str:
    """Format articles into a concise prompt for the AI."""
    name = (row_ctx or {}).get("name")
    target = f"{symbol} ({name})" if name else symbol
    lines = [f"TARGET COMPANY: {target}. Score every article's sentiment for {target} "
             f"ONLY — attribute news to its true subject; a rival's or partner's news "
             f"is not {symbol}'s sentiment unless it directly affects {symbol}.",
             _format_quant_context(symbol, row_ctx)]
    for i, a in enumerate(articles[:_SCORE_BATCH], 1):
        ts = a.get("datetime", 0)
        if isinstance(ts, (int, float)) and ts > 0:
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            date = "?"
        headline = a.get("headline", "")
        summary = a.get("summary", "")[:300]
        source = a.get("source", "")
        dup = a.get("n_duplicates", 0)
        dup_note = f" (+{dup} syndicated copies)" if dup else ""
        lines.append(f"[{i}] {date} | {source}{dup_note} | {headline}")
        if summary:
            lines.append(f"    {summary}")
    return "\n".join(lines)


def _build_market_prompt(articles: list[dict], tape: str = "") -> str:
    """Format market articles into a concise headlines-only prompt."""
    lines = ["Score market sentiment from these articles:"]
    if tape:
        lines.append(f"\nCROSS-ASSET TAPE (1-day moves): {tape}\n")
    for i, a in enumerate(articles[:_SCORE_BATCH], 1):
        headline = a.get("headline", "")
        source = a.get("source", "")
        lines.append(f"[{i}] {source}: {headline}")
    return "\n".join(lines)


def _score_articles_once(system_prompt: str, user_prompt: str,
                         n_articles: int) -> tuple[list[dict], str] | None:
    """One array-scoring call + validation. ~35 output tokens per article
    plus the brief, so the token budget scales with batch size."""
    max_tok = min(1600, 200 + 45 * n_articles)
    raw = _nvidia_call(system_prompt, user_prompt, max_tokens=max_tok)
    return _validate_article_scores(raw, n_articles)


def _score_articles(system_prompt: str, user_prompt: str,
                    n_articles: int, stage_cb=None) -> tuple[list[dict], str, float] | None:
    """Per-article scoring with optional 2-pass self-consistency.

    Returns (scores, brief, agreement) where scores aligns to article order
    (None entries = article not scored) and agreement in [0,1] discounts
    confidence when the two passes disagree. One usable pass is accepted;
    both failing returns None (caller falls back to blob scoring).

    `stage_cb(stage, frac)` (optional) fires after each AI pass so the refresh
    progress modal can fill a per-job bar through the two passes."""
    first = _score_articles_once(system_prompt, user_prompt, n_articles)
    if stage_cb:
        stage_cb("score1", 0.6)
    if not SELF_CONSISTENCY:
        if first is None:
            return None
        return first[0], first[1], 1.0
    second = _score_articles_once(system_prompt, user_prompt, n_articles)
    if stage_cb:
        stage_cb("score2", 0.85)
    if first is None and second is None:
        return None
    if first is None or second is None:
        scores, brief = first or second
        # Only one pass usable — keep it, but with a mild agreement discount
        # since the other pass failed outright.
        return scores, brief, 0.8
    merged: list[dict | None] = []
    gaps: list[float] = []
    for a, b in zip(first[0], second[0]):
        if a is None and b is None:
            merged.append(None)
        elif a is None or b is None:
            merged.append(a or b)
        else:
            gap = abs(a["score"] - b["score"])
            gaps.append(gap)
            merged.append({
                "score": (a["score"] + b["score"]) / 2.0,
                "event": a["event"],
                "relevance": a["relevance"] if a["relevance"] == b["relevance"]
                             else ("med" if "med" in (a["relevance"], b["relevance"])
                                   else "low"),
            })
    agreement = 1.0 - (sum(gaps) / len(gaps)) / 2.0 if gaps else 0.8
    if gaps and max(gaps) > 0.5:
        agreement = min(agreement, 0.5)
    return merged, first[1], max(0.0, min(1.0, agreement))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_news_sentiment(symbol: str, context: dict | None = None,
                       stage_cb=None) -> dict | None:
    """Per-ticker sentiment. Cache-first, then fetch+analyze if stale.

    `context` (optional): {"beta": float, "s_mkt": float, "row": {...}} —
    beta and market sentiment feed the systematic decomposition; the row
    payload feeds quant context into the Bloomberg brief. Missing context
    degrades to pure idiosyncratic scoring.

    `stage_cb(stage, frac)` (optional) fires at fetch/score1/score2/aggregate
    so the refresh progress modal can fill a per-job bar.

    Returns the enriched sentiment dict (legacy keys tier/score/summary are
    always populated; score == s_total, summary == brief) or None.
    """
    ctx = context or {}
    days = _clamp_lookback(ctx.get("lookback_days") or _DEFAULT_LOOKBACK_DAYS)
    cache_key = f"sentiment|{symbol}|{days}"
    cached = _cache_get(_SENTIMENT_CACHE, cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached

    tau = _tau_for_days(days)
    articles = fetch_company_news(symbol, days=days)
    if stage_cb:
        stage_cb("fetch", 0.35)
    if not articles:
        _cache_put(_SENTIMENT_CACHE, cache_key, _MISS, _NEG_TTL)
        return None

    row_ctx = ctx.get("row")
    prompt = _build_articles_prompt(articles, symbol, row_ctx)
    batch = articles[:_SCORE_BATCH]
    scored = _score_articles(_ARRAY_SYSTEM_PROMPT, prompt, len(batch), stage_cb=stage_cb)

    fallback = False
    if scored is not None:
        per_article, brief, agreement = scored
        # Write per-article scores back onto the cached articles so the
        # flash tape / articles route can color and tag each headline.
        for a, sc in zip(batch, per_article):
            if sc is not None:
                a.update(sc)
        _cache_put(_NEWS_CACHE, f"news|{symbol}|{days}", articles, _NEWS_TTL)
        agg = aggregate_scores(batch, tau=tau)
        if agg is None:
            return None
        s_idio = agg["s_idio"]
        confidence = round(agg["confidence"] * agreement, 3)
        dispersion = agg["dispersion"]
        n_scored = agg["n_articles"]
        events: dict[str, int] = {}
        for a in batch:
            ev = a.get("event")
            if ev and a.get("relevance") in ("high", "med"):
                events[ev] = events.get(ev, 0) + 1
    else:
        # Blob fallback — the old single-call path. Never returns less than
        # the previous version of this module did.
        raw = _nvidia_call(_SENTIMENT_SYSTEM_PROMPT, prompt)
        validated = _validate_sentiment(raw)
        if validated is None:
            return None
        s_idio = validated["score"]
        brief = validated["summary"]
        confidence = 0.3
        dispersion = None
        n_scored = len(batch)
        events = {}
        fallback = True

    if stage_cb:
        stage_cb("aggregate", 0.95)
    s_lm = lm_aggregate(batch, tau=tau)
    disagreement = is_disagreement(s_idio, s_lm)
    if disagreement:
        confidence = min(confidence, 0.5)

    beta = ctx.get("beta")
    s_mkt = ctx.get("s_mkt")
    if s_mkt is None:
        # Best-effort fallback for callers with no explicit s_mkt (e.g. ad-hoc
        # get_news_sentiment(symbol) with no context) — look up the market
        # entry for THIS call's own window so it doesn't cross-contaminate
        # with a market score computed for a different lookback.
        mkt_cached = _cache_get(_SENTIMENT_CACHE, f"sentiment|__market__|{days}")
        if mkt_cached is not None and mkt_cached is not _MISS:
            s_mkt = mkt_cached.get("score")
    s_total = combine_signal(s_idio, beta, s_mkt)
    s_sys = round(s_total - max(-1.0, min(1.0, s_idio)), 4)
    tier = calibrate_tier(s_total, _calibration_scores())

    if not brief:
        brief = "No summary available."
    result = {
        # Legacy keys — NS dot, xlsx export, old consumers keep working.
        "tier": tier,
        "score": round(s_total, 4),
        "summary": brief,
        # Enriched signal decomposition.
        "s_idio": s_idio,
        "s_lm": s_lm,
        "s_sys": s_sys,
        "s_total": round(s_total, 4),
        "beta_used": beta,
        "s_mkt_used": s_mkt,
        "confidence": confidence,
        "dispersion": dispersion,
        "disagreement": disagreement,
        "events": events,
        "fallback": fallback,
        "article_count": len(articles),
        "lookback_days": days,
        "recency_tau_days": round(tau, 2),
        "assessed_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache_put(_SENTIMENT_CACHE, cache_key, result, _SENTIMENT_TTL)
    _history_append({
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "symbol": symbol,
        "s_idio": s_idio,
        "s_lm": s_lm,
        "s_mkt": s_mkt,
        "s_total": round(s_total, 4),
        "tier": tier,
        "confidence": confidence,
        "n_articles": n_scored,
        "price": (row_ctx or {}).get("price"),
        "beta": beta,
    })
    return result


def get_cached_sentiment(symbol: str) -> dict | None:
    """Cache-only read — safe to call from fetch_one hot path.

    The NS dot in the main table isn't tied to the News tab's selected
    window, so this sweeps every lookback bucket (default-first) rather
    than reading a single key — sentiment is now cached per-window
    (see get_news_sentiment), and the row that triggered the last
    refresh may not have used the default window."""
    for days in (_DEFAULT_LOOKBACK_DAYS, *_LOOKBACK_CHOICES):
        cached = _cache_get(_SENTIMENT_CACHE, f"sentiment|{symbol}|{days}")
        if cached is not None and cached is not _MISS:
            return cached
    return None


def get_market_sentiment(days: int = _DEFAULT_LOOKBACK_DAYS,
                         stage_cb=None) -> dict | None:
    """Market-wide sentiment (s_mkt) — per-article scored, aggregated with
    the same weighting math as stocks. Tier uses fixed thresholds (market
    history is one observation per refresh — too sparse for quantiles).

    `days` windows the benchmark-index news the market feed is built from —
    keyed into the cache so a 7d and a 30d refresh don't collide/shadow each
    other (a GET-only read after a window change would otherwise silently
    serve whichever window was cached last, mislabeled).
    `stage_cb(stage, frac)` (optional) fills the market job's progress bar."""
    cache_key = f"sentiment|__market__|{days}"
    cached = _cache_get(_SENTIMENT_CACHE, cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached

    articles = fetch_market_news(days=days)
    if stage_cb:
        stage_cb("fetch", 0.35)
    if not articles:
        _cache_put(_SENTIMENT_CACHE, cache_key, _MISS, _NEG_TTL)
        return None

    tape, tape_items = _market_tape_context()
    prompt = _build_market_prompt(articles, tape)
    batch = articles[:_SCORE_BATCH]
    scored = _score_articles(_MARKET_ARRAY_SYSTEM_PROMPT, prompt, len(batch), stage_cb=stage_cb)

    fallback = False
    if scored is not None:
        per_article, brief, agreement = scored
        for a, sc in zip(batch, per_article):
            if sc is not None:
                a.update(sc)
        _cache_put(_NEWS_CACHE, f"news|__market__|{days}", articles, _NEWS_TTL)
        agg = aggregate_scores(batch)
        if agg is None:
            return None
        s_mkt = agg["s_idio"]
        confidence = round(agg["confidence"] * agreement, 3)
    else:
        raw = _nvidia_call(_MARKET_SENTIMENT_SYSTEM_PROMPT, prompt)
        validated = _validate_sentiment(raw)
        if validated is None:
            return None
        s_mkt = validated["score"]
        brief = validated["summary"]
        confidence = 0.3
        fallback = True

    if stage_cb:
        stage_cb("aggregate", 0.95)
    tier = calibrate_tier(s_mkt, None)  # fixed thresholds by design
    result = {
        "tier": tier,
        "score": round(s_mkt, 4),
        "summary": brief or "No summary available.",
        "s_mkt": round(s_mkt, 4),
        "confidence": confidence,
        "tape": tape,            # legacy pre-formatted string (back-compat)
        "tape_items": tape_items,  # structured [{label, pct}] for per-item coloring
        "fallback": fallback,
        "article_count": len(articles),
        "assessed_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache_put(_SENTIMENT_CACHE, cache_key, result, _SENTIMENT_TTL)
    _history_append({
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "symbol": "__market__",
        "s_idio": None, "s_lm": None,
        "s_mkt": round(s_mkt, 4),
        "s_total": round(s_mkt, 4),
        "tier": tier,
        "confidence": confidence,
        "n_articles": len(batch),
        "price": None, "beta": None,
    })
    return result


def get_portfolio_sentiment(symbols: list[str]) -> dict:
    """Batch sentiment for all portfolio tickers. Thread-pooled (2 workers).

    Returns {symbol: sentiment_dict_or_None}.
    """
    results: dict[str, dict | None] = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(get_news_sentiment, sym): sym for sym in symbols}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                results[sym] = future.result()
            except Exception:
                results[sym] = None
    return results


def _restore_cache_if_refresh_failed(news_key: str, sentiment_key: str,
                                     prev_news: tuple[float, float, Any] | None,
                                     prev_sent: tuple[float, float, Any] | None) -> None:
    if _cache_peek_entry(_NEWS_CACHE, news_key) is None:
        _cache_restore(_NEWS_CACHE, news_key, prev_news)
    if _cache_peek_entry(_SENTIMENT_CACHE, sentiment_key) is None:
        _cache_restore(_SENTIMENT_CACHE, sentiment_key, prev_sent)


def _refresh_symbol_sentiment(symbol: str, context: dict | None = None,
                              stage_cb=None) -> dict | None:
    if stage_cb:
        stage_cb("start", 0.05)
    ctx = context or {}
    days = _clamp_lookback(ctx.get("lookback_days") or _DEFAULT_LOOKBACK_DAYS)
    news_key = f"news|{symbol}|{days}"
    sentiment_key = f"sentiment|{symbol}|{days}"
    prev_news = _cache_take(_NEWS_CACHE, news_key)
    prev_sent = _cache_take(_SENTIMENT_CACHE, sentiment_key)
    try:
        get_news_sentiment(symbol, context, stage_cb=stage_cb)
    finally:
        _restore_cache_if_refresh_failed(news_key, sentiment_key, prev_news, prev_sent)
    cached = _cache_get(_SENTIMENT_CACHE, sentiment_key)
    if cached is _MISS or cached is None:
        return None
    return cached


def _refresh_market_sentiment(days: int = _DEFAULT_LOOKBACK_DAYS,
                              stage_cb=None) -> dict | None:
    news_key = f"news|__market__|{days}"
    sentiment_key = f"sentiment|__market__|{days}"
    prev_news = _cache_take(_NEWS_CACHE, news_key)
    prev_sent = _cache_take(_SENTIMENT_CACHE, sentiment_key)
    try:
        get_market_sentiment(days, stage_cb=stage_cb)
    finally:
        _restore_cache_if_refresh_failed(news_key, sentiment_key, prev_news, prev_sent)
    cached = _cache_get(_SENTIMENT_CACHE, sentiment_key)
    if cached is _MISS or cached is None:
        return None
    return cached


def _symbol_context(symbol: str, context: dict | None,
                    s_mkt: float | None) -> dict | None:
    """Assemble the per-symbol scoring context from the request-level
    context payload ({betas, rows, weights, lookback_days} keyed by symbol)."""
    if not context:
        return {"s_mkt": s_mkt} if s_mkt is not None else None
    return {
        "beta": (context.get("betas") or {}).get(symbol),
        "s_mkt": s_mkt,
        "row": (context.get("rows") or {}).get(symbol),
        "lookback_days": context.get("lookback_days"),
    }


def refresh_sentiment(symbols: list[str], context: dict | None = None,
                      progress_cb=None) -> dict:
    """Force-refresh, staged: market first (its s_mkt feeds every stock's
    systematic term), then constituents sorted by portfolio weight
    descending so the biggest positions land first in the UI.

    `progress_cb(kind, payload)` (optional) is invoked per completion —
    the NDJSON streaming route uses it for progressive rendering.

    Returns {market, portfolio, status} where status surfaces enough
    diagnostic info for the UI to show a precise message instead of a
    generic 'check API keys' fallback.

    Progress events emitted via `progress_cb` (in addition to the completion
    events consumed today): `plan` (up front, lists all jobs so the modal can
    render every row as queued), `market_stage`/`symbol_stage`
    ({stage: start|fetch|score1|score2|aggregate, frac}) so each job's bar
    fills as its two AI passes run.
    """
    ctx = context or {}
    days = _clamp_lookback(ctx.get("lookback_days") or _DEFAULT_LOOKBACK_DAYS)
    weights = ctx.get("weights") or {}
    ordered = sorted(symbols, key=lambda s: -(weights.get(s) or 0.0))
    if progress_cb:
        # Emit the full job list up front so the modal shows every row as
        # "queued" immediately, before any work lands.
        progress_cb("plan", {"market": True, "symbols": ordered, "days": days})

    def _market_stage(stage, frac):
        if progress_cb:
            progress_cb("market_stage", {"stage": stage, "frac": frac})
    _market_stage("start", 0.05)
    market = _refresh_market_sentiment(days, stage_cb=_market_stage)
    if progress_cb:
        progress_cb("market", {"sentiment": market})
    s_mkt = market.get("score") if market else None

    def _mk_stage_cb(sym):
        if not progress_cb:
            return None
        def cb(stage, frac):
            # _emit is thread-safe (write_lock in the route); safe to call
            # from the 2 worker threads concurrently.
            progress_cb("symbol_stage", {"symbol": sym, "stage": stage, "frac": frac})
        return cb

    portfolio: dict[str, dict | None] = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(_refresh_symbol_sentiment, sym,
                        _symbol_context(sym, context, s_mkt),
                        _mk_stage_cb(sym)): sym
            for sym in ordered
        }
        for future in as_completed(futures):
            sym = futures[future]
            try:
                portfolio[sym] = future.result()
            except Exception:
                portfolio[sym] = None
            if progress_cb:
                progress_cb("symbol", {"symbol": sym, "sentiment": portfolio[sym]})
    fetched = sum(1 for v in portfolio.values() if v)
    diag = status()
    diag.update({
        "market_ok": market is not None,
        "fetched": fetched,
        "total": len(symbols),
    })
    return {
        "market": market,
        "portfolio": portfolio,
        "status": diag,
    }


def get_cached_articles(symbols: list[str], limit: int = 800) -> list[dict]:
    """Merged article feed for the flash tape / timeline — cache-only, O(n),
    never triggers a fetch. Articles carry per-article score/event/relevance
    when the symbol has been scored via the array path.

    News is cached per lookback window (see fetch_company_news), and this
    feed isn't tied to any one window — it backs the Timeline, which has its
    own independent period selector. Sweep the widest-to-narrowest cached
    bucket per symbol so a 30D cache (a superset of what 7D would have
    fetched) is preferred when present, instead of reading one fixed key
    that nothing writes to once the window scoping changes.

    Truncation is time-stratified, NOT a flat newest-N cut. A multi-symbol
    portfolio publishes hundreds of articles/day collectively, so a plain
    `out[:limit]` of the merged newest-first feed collapsed the whole
    Timeline into the last ~19 hours even though every symbol's cache spans
    the full window (verified: 15 hyperscalers cached 696 articles across a
    week, but newest-250 kept only Jul 10-11). `_window_sample` keeps the
    newest `min_recent` intact — the flash tape only ever renders its
    freshest ~120 — and spreads the remaining budget across the window's
    time buckets so every day of the Timeline stays populated. Below `limit`
    it's a no-op, so normal-size portfolios pass through at full density."""
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
        out = _window_sample(out, cap=limit, min_recent=120)
    return out


def compute_diagnostics() -> dict:
    """Validation panel data: rank IC of s_idio vs forward idiosyncratic
    returns, per-tier forward-return monotonicity, and the s_total
    distribution. Uses the sentiment history file + the same close cache
    analytics uses — no extra network calls beyond a possible SPY fetch.

    This is the 'sentiment is measured, not vibes' contract: IC (Spearman)
    against r_i - beta_i * r_SPY follows the standard validation design in
    Lopez-Lira & Tang (2023); Tetlock et al. (2008) motivates using the
    idiosyncratic component.
    """
    import numpy as np
    from portfolio_tracker.analytics import _bulk_close  # lazy: pandas-heavy

    records = [r for r in _history_load()
               if r.get("symbol") != "__market__"
               and isinstance(r.get("s_idio"), (int, float))]
    out: dict[str, Any] = {"n_records": len(records)}

    # Score distribution (last calibration window) — range-usage evidence.
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=_CALIBRATION_WINDOW_DAYS)).strftime("%Y-%m-%d")
    scores = [r["s_total"] for r in _history_load()
              if (r.get("date") or "") >= cutoff
              and isinstance(r.get("s_total"), (int, float))
              and r.get("symbol") != "__market__"]
    edges = [round(-1.0 + i * 0.1, 1) for i in range(21)]
    counts = [0] * 20
    for s in scores:
        idx = min(19, max(0, int((s + 1.0) / 0.1)))
        counts[idx] += 1
    out["histogram"] = {"edges": edges, "counts": counts, "n": len(scores)}
    out["calibration_n"] = len(scores)
    out["calibration_active"] = len(scores) >= _MIN_CALIBRATION_N

    if not records:
        out["ic"] = None
        out["tiers"] = {}
        return out

    symbols = sorted({r["symbol"] for r in records})
    try:
        closes = _bulk_close(symbols + ["SPY"], "1Y")
    except Exception:
        closes = None
    if closes is None or closes.empty or "SPY" not in closes.columns:
        out["ic"] = None
        out["tiers"] = {}
        out["note"] = "price history unavailable"
        return out

    import pandas as pd
    spy = closes["SPY"].dropna()

    def _fwd_idio(rec: dict, horizon: int) -> float | None:
        sym = rec["symbol"]
        if sym not in closes.columns:
            return None
        s = closes[sym].dropna()
        if len(s) < horizon + 2:
            return None
        ts = pd.Timestamp(rec["date"])
        pos = s.index.searchsorted(ts, side="right") - 1  # last close <= date
        if pos < 0 or pos + horizon >= len(s):
            return None
        r = float(s.iloc[pos + horizon] / s.iloc[pos] - 1.0)
        spos = spy.index.searchsorted(ts, side="right") - 1
        if spos < 0 or spos + horizon >= len(spy):
            return None
        r_spy = float(spy.iloc[spos + horizon] / spy.iloc[spos] - 1.0)
        beta = rec.get("beta")
        beta = float(beta) if isinstance(beta, (int, float)) else 1.0
        return r - beta * r_spy

    def _spearman(x: list[float], y: list[float]) -> float | None:
        if len(x) < 5:
            return None
        rx = np.argsort(np.argsort(x)).astype(float)
        ry = np.argsort(np.argsort(y)).astype(float)
        if rx.std() == 0 or ry.std() == 0:
            return None
        return float(np.corrcoef(rx, ry)[0, 1])

    ic_out: dict[str, Any] = {}
    tier_stats: dict[str, dict] = {}
    for horizon, label in ((1, "1d"), (5, "5d")):
        xs, ys = [], []
        for rec in records:
            fr = _fwd_idio(rec, horizon)
            if fr is None:
                continue
            xs.append(float(rec["s_idio"]))
            ys.append(fr)
            if horizon == 1:
                t = rec.get("tier") or "neutral"
                st = tier_stats.setdefault(t, {"n": 0, "sum": 0.0})
                st["n"] += 1
                st["sum"] += fr
        ic = _spearman(xs, ys)
        t_stat = None
        if ic is not None and len(xs) > 2 and abs(ic) < 1.0:
            t_stat = round(ic * math.sqrt((len(xs) - 2) / (1.0 - ic * ic)), 2)
        ic_out[label] = {"ic": (round(ic, 4) if ic is not None else None),
                         "n": len(xs), "t_stat": t_stat}
    out["ic"] = ic_out
    out["tiers"] = {
        t: {"n": st["n"], "mean_fwd_1d_pct": round(st["sum"] / st["n"] * 100.0, 3)}
        for t, st in tier_stats.items() if st["n"] > 0
    }
    return out


def status() -> dict:
    """Diagnostic: which keys are configured."""
    fh = _FH_LIMITER.snapshot()
    nv = _NV_LIMITER.snapshot()
    with _rate_limit_lock:
        fh_backoff = max(0.0, _fh_rate_limit_until - time.time())
        nv_backoff = max(0.0, _nv_rate_limit_until - time.time())
    return {
        "finnhub_key": bool(FINNHUB_API_KEY),
        "finnhub_key_set": bool(FINNHUB_API_KEY),
        "nvidia_key": bool(NVIDIA_API_KEY),
        "nvidia_key_set": bool(NVIDIA_API_KEY),
        "finnhub_calls_used": fh["used"],
        "finnhub_calls_limit": fh["limit"],
        "nvidia_calls_used": nv["used"],
        "nvidia_calls_limit": nv["limit"],
        "finnhub_backoff_s": int(fh_backoff + 0.999) if fh_backoff > 0 else 0,
        "nvidia_backoff_s": int(nv_backoff + 0.999) if nv_backoff > 0 else 0,
        "news_cache_size": len(_NEWS_CACHE),
        "sentiment_cache_size": len(_SENTIMENT_CACHE),
        "kappa": KAPPA,
        "self_consistency": SELF_CONSISTENCY,
        "lexicon_available": _lex._load(),
    }


# ---------------------------------------------------------------------------
# Module import — load persisted caches once
# ---------------------------------------------------------------------------

_load_persisted_caches()
