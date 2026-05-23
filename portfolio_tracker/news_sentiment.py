"""News & sentiment adapter — Finnhub news + OpenRouter AI analysis.

Fetches per-ticker and market-wide news from Finnhub's free API, then
scores each batch through the nvidia/nemotron model on OpenRouter to
produce a 5-tier sentiment signal (very_bullish → very_bearish).

Caches are disk-backed (`.portfolio_tracker_news.json`) so news/sentiment
survives app restarts. Refresh is user-driven only — the on-disk cache is
treated as effectively permanent (30-day TTL) and only `bust_cache()` or
`refresh_sentiment()` re-fetches.

Both Finnhub and OpenRouter calls flow through a token-bucket rate
limiter (rolling 60s window). On 429 the call sleeps and retries up to
3 times instead of returning None silently.

Degrades cleanly:
- No Finnhub key → no news fetched, all functions return None.
- No OpenRouter key → news fetched but sentiment is None (raw articles
  still available for the News tab).
- Transient API failures → not cached, retried next request.
"""

from __future__ import annotations

import json
import os
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from portfolio_tracker.helpers import _repo_root

# ---------------------------------------------------------------------------
# API key loading — mirrors finnhub_adapter._load_api_key()
# ---------------------------------------------------------------------------

def _load_key(env_var: str, filename: str) -> str:
    env = os.environ.get(env_var, "").strip()
    if env:
        return env
    for p in [Path(__file__).resolve().parent / filename,
              Path(__file__).resolve().parent.parent / filename]:
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


FINNHUB_API_KEY = _load_key("FINNHUB_API_KEY", ".finnhub_key")
OPENROUTER_API_KEY = _load_key("OPENROUTER_API_KEY", ".openrouter_key")
_FINNHUB_BASE = "https://finnhub.io/api/v1/"

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
            "version": 1,
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


_FH_LIMITER = _RateLimiter(max_per_min=55, name="finnhub")
_OR_LIMITER = _RateLimiter(max_per_min=15, name="openrouter")


# ---------------------------------------------------------------------------
# Finnhub news fetching
# ---------------------------------------------------------------------------

_RATE_LIMIT_BACKOFF_S = 65
_RETRY_SLEEP_S = 5.0
_MAX_RETRIES = 3

_rate_limit_lock = threading.Lock()
_fh_rate_limit_until = 0.0  # last-resort circuit breaker after repeated 429s


def _fh_call(path: str, params: dict[str, Any]) -> Any | None:
    global _fh_rate_limit_until
    if not FINNHUB_API_KEY:
        return None
    # Last-resort circuit breaker: if we keep getting 429s even after the
    # rolling limiter, pause everything for 65s.
    with _rate_limit_lock:
        if time.time() < _fh_rate_limit_until:
            return None
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


def fetch_company_news(symbol: str, days: int = 7) -> list[dict] | None:
    """Fetch recent news for a single ticker. Returns normalised article list."""
    cache_key = f"news|{symbol}"
    cached = _cache_get(_NEWS_CACHE, cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached

    to_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    from_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    raw = _fh_call("company-news", {"symbol": symbol, "from": from_date, "to": to_date})
    if raw is None:
        return None
    if not isinstance(raw, list):
        _cache_put(_NEWS_CACHE, cache_key, _MISS, _NEG_TTL)
        return None
    articles = []
    for a in raw[:20]:
        articles.append({
            "headline": a.get("headline", ""),
            "summary": a.get("summary", ""),
            "source": a.get("source", ""),
            "datetime": a.get("datetime", 0),
            "url": a.get("url", ""),
            "related": a.get("related", ""),
        })
    if not articles:
        _cache_put(_NEWS_CACHE, cache_key, _MISS, _NEG_TTL)
        return None
    _cache_put(_NEWS_CACHE, cache_key, articles, _NEWS_TTL)
    return articles


def fetch_market_news() -> list[dict] | None:
    """Fetch general market news (not ticker-specific)."""
    cache_key = "news|__market__"
    cached = _cache_get(_NEWS_CACHE, cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached

    raw = _fh_call("news", {"category": "general"})
    if raw is None:
        return None
    if not isinstance(raw, list):
        _cache_put(_NEWS_CACHE, cache_key, _MISS, _NEG_TTL)
        return None
    articles = []
    for a in raw[:25]:
        articles.append({
            "headline": a.get("headline", ""),
            "summary": a.get("summary", ""),
            "source": a.get("source", ""),
            "datetime": a.get("datetime", 0),
            "url": a.get("url", ""),
        })
    if not articles:
        _cache_put(_NEWS_CACHE, cache_key, _MISS, _NEG_TTL)
        return None
    _cache_put(_NEWS_CACHE, cache_key, articles, _NEWS_TTL)
    return articles


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

_MODEL = "nvidia/nemotron-3-nano-30b-a3b:free"
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Last-resort circuit breaker after repeated 429s
_or_rate_limit_until = 0.0

# Lazy-initialized OpenAI client (reused across calls)
try:
    from openai import OpenAI as _OpenAI
except ImportError:
    _OpenAI = None  # type: ignore[assignment,misc]
_openrouter_client: Any = None
_client_lock = threading.Lock()


def _get_client() -> Any:
    global _openrouter_client
    if _OpenAI is None:
        return None
    if _openrouter_client is not None:
        return _openrouter_client
    with _client_lock:
        if _openrouter_client is None:
            _openrouter_client = _OpenAI(base_url=_OPENROUTER_BASE, api_key=OPENROUTER_API_KEY)
        return _openrouter_client


def _openrouter_call(system_prompt: str, user_content: str) -> dict | None:
    """Call OpenRouter with rolling-window rate limiting + 429 retry.

    Tries with reasoning enabled first; falls back to reasoning disabled
    if the model returns empty content (happens with longer prompts on
    the free tier).
    """
    global _or_rate_limit_until
    if not OPENROUTER_API_KEY:
        return None
    with _rate_limit_lock:
        if time.time() < _or_rate_limit_until:
            return None
    client = _get_client()
    if client is None:
        return None
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    # Two "shapes" — first with reasoning, then without if the first
    # returns empty content. Each shape gets up to _MAX_RETRIES on 429.
    shapes = [
        {"extra_body": {"reasoning": {"enabled": True}}},
        {},
    ]
    for shape_idx, extra_kwargs in enumerate(shapes):
        for attempt in range(_MAX_RETRIES):
            _OR_LIMITER.acquire()
            try:
                response = client.chat.completions.create(
                    model=_MODEL,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=2048,
                    **extra_kwargs,
                )
                text = response.choices[0].message.content
                if text is None or not text.strip():
                    break  # try next shape
                text = text.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[-1]
                    if text.endswith("```"):
                        text = text[:-3]
                    text = text.strip()
                return json.loads(text)
            except json.JSONDecodeError:
                print(f"[news] AI returned non-JSON response (shape {shape_idx+1}, attempt {attempt+1})",
                      file=sys.stderr)
                break
            except Exception as exc:
                exc_str = str(exc)
                if "429" in exc_str or "rate limit" in exc_str.lower():
                    _OR_LIMITER.penalize()
                    print(f"[news] OpenRouter 429 (shape {shape_idx+1}, attempt {attempt+1}/{_MAX_RETRIES}) — "
                          f"sleeping {_RETRY_SLEEP_S}s", file=sys.stderr)
                    time.sleep(_RETRY_SLEEP_S)
                    if attempt == _MAX_RETRIES - 1 and shape_idx == len(shapes) - 1:
                        with _rate_limit_lock:
                            _or_rate_limit_until = time.time() + _RATE_LIMIT_BACKOFF_S
                    continue
                print(f"[news] OpenRouter call failed: {exc}", file=sys.stderr)
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


def _build_articles_prompt(articles: list[dict], symbol: str) -> str:
    """Format articles into a concise prompt for the AI."""
    lines = [f"Assess sentiment for {symbol} based on these recent news articles:\n"]
    for i, a in enumerate(articles[:15], 1):
        ts = a.get("datetime", 0)
        if isinstance(ts, (int, float)) and ts > 0:
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            date = "?"
        headline = a.get("headline", "")
        summary = a.get("summary", "")[:300]
        source = a.get("source", "")
        lines.append(f"[{i}] {date} | {source} | {headline}")
        if summary:
            lines.append(f"    {summary}")
    return "\n".join(lines)


def _build_market_prompt(articles: list[dict]) -> str:
    """Format market articles into a concise headlines-only prompt."""
    lines = ["Score market sentiment from these headlines:\n"]
    for i, a in enumerate(articles[:10], 1):
        headline = a.get("headline", "")
        source = a.get("source", "")
        lines.append(f"[{i}] {source}: {headline}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_news_sentiment(symbol: str) -> dict | None:
    """Per-ticker sentiment. Cache-first, then fetch+analyze if stale.

    Returns: {"tier", "score", "summary", "article_count", "assessed_at"} or None.
    """
    cache_key = f"sentiment|{symbol}"
    cached = _cache_get(_SENTIMENT_CACHE, cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached

    articles = fetch_company_news(symbol)
    if not articles:
        _cache_put(_SENTIMENT_CACHE, cache_key, _MISS, _NEG_TTL)
        return None

    prompt = _build_articles_prompt(articles, symbol)
    raw = _openrouter_call(_SENTIMENT_SYSTEM_PROMPT, prompt)
    validated = _validate_sentiment(raw)
    if validated is None:
        return None

    validated["article_count"] = len(articles)
    validated["assessed_at"] = datetime.now(timezone.utc).isoformat()
    _cache_put(_SENTIMENT_CACHE, cache_key, validated, _SENTIMENT_TTL)
    return validated


def get_cached_sentiment(symbol: str) -> dict | None:
    """O(1) cache-only read — safe to call from fetch_one hot path."""
    cached = _cache_get(_SENTIMENT_CACHE, f"sentiment|{symbol}")
    if cached is _MISS or cached is None:
        return None
    return cached


def get_market_sentiment() -> dict | None:
    """Market-wide sentiment."""
    cache_key = "sentiment|__market__"
    cached = _cache_get(_SENTIMENT_CACHE, cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached

    articles = fetch_market_news()
    if not articles:
        _cache_put(_SENTIMENT_CACHE, cache_key, _MISS, _NEG_TTL)
        return None

    prompt = _build_market_prompt(articles)
    raw = _openrouter_call(_MARKET_SENTIMENT_SYSTEM_PROMPT, prompt)
    validated = _validate_sentiment(raw)
    if validated is None:
        return None

    validated["article_count"] = len(articles)
    validated["assessed_at"] = datetime.now(timezone.utc).isoformat()
    _cache_put(_SENTIMENT_CACHE, cache_key, validated, _SENTIMENT_TTL)
    return validated


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


def refresh_sentiment(symbols: list[str]) -> dict:
    """Force-refresh: bust caches, re-fetch news, re-analyze all.

    Returns {market, portfolio, status} where status surfaces enough
    diagnostic info for the UI to show a precise message instead of a
    generic 'check API keys' fallback.
    """
    bust_cache()
    market = get_market_sentiment()
    portfolio = get_portfolio_sentiment(symbols)
    fetched = sum(1 for v in portfolio.values() if v)
    return {
        "market": market,
        "portfolio": portfolio,
        "status": {
            "finnhub_key": bool(FINNHUB_API_KEY),
            "openrouter_key": bool(OPENROUTER_API_KEY),
            "market_ok": market is not None,
            "fetched": fetched,
            "total": len(symbols),
        },
    }


def status() -> dict:
    """Diagnostic: which keys are configured."""
    return {
        "finnhub_key_set": bool(FINNHUB_API_KEY),
        "openrouter_key_set": bool(OPENROUTER_API_KEY),
        "news_cache_size": len(_NEWS_CACHE),
        "sentiment_cache_size": len(_SENTIMENT_CACHE),
    }


# ---------------------------------------------------------------------------
# Module import — load persisted caches once
# ---------------------------------------------------------------------------

_load_persisted_caches()
