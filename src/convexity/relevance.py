"""Deterministic news-relevance heuristic + shared title-dedup primitives.

Shared between the ML training pipeline (ml/scripts/*) and production
inference (ml_sentiment.py) so both sides score relevance and collapse
syndicated duplicates with EXACTLY the same logic — train/serve skew on
these inputs would silently corrupt the model.

Two hard design constraints:
1. NO parameters fitted on FNSPID (or any news data). Every weight below is
   hand-set. The user plans to train an ML relevance model later on the same
   corpus the sentiment model is trained on; that only stays legitimate if
   this heuristic never learned from that corpus.
2. Dependencies limited to stdlib + rapidfuzz (optional) + symbol_db
   (optional) — importable by the app without the ml/ tree or sklearn.

Relevance intuition (Boudoukh et al. 2013: only ~half of firm-tagged news is
firm-relevant; conditioning on relevance ~doubles explained variance):
an article is relevant to a symbol when the company is the SUBJECT — named
early and prominently, not one ticker among twenty in a "stocks to watch"
roundup from a low-tier syndicator.
"""

import re
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz as _fuzz

# Same threshold as news_sentiment._DEDUP_SIMILARITY — keep in sync.
DEDUP_SIMILARITY = 85

# Fuzzy company-name match threshold (partial_ratio on normalized text).
_NAME_MATCH = 88

# Roundup/listicle patterns — articles ABOUT many stocks, not about this one.
_BOILERPLATE_RE = re.compile(
    r"stocks?\s+to\s+(watch|buy|sell)|top\s+\d+|\d+\s+(best|top|worst|cheap)"
    r"|movers|market\s+wrap|weekly\s+(recap|roundup)|roundup|watchlist"
    r"|\bvs\.?\s|earnings\s+calendar|premarket|after.?hours\s+movers",
    re.IGNORECASE,
)

# Legal-suffix noise stripped from company names before matching.
_NAME_SUFFIX_RE = re.compile(
    r"\b(incorporated|inc|corporation|corp|company|co|limited|ltd|plc|holdings?"
    r"|group|nv|sa|ag|se|lp|llc|trust|fund|etf)\b\.?",
    re.IGNORECASE,
)


# ------------------------------------------------------------------ dedup
def norm_title(title: str) -> str:
    """Identical to news_sentiment._norm_title — lowercase, alnum-only, squeezed."""
    return " ".join(
        "".join(c if c.isalnum() or c.isspace() else " " for c in (title or "").lower()).split()
    )


def cluster_titles(titles: list[str]) -> tuple[list[int], list[int]]:
    """Greedy near-duplicate clustering in input order (caller pre-sorts by time).

    Returns (keep_indices, dup_counts): positions of cluster canonicals in the
    input list and, aligned with them, how many duplicates each absorbed.
    Same semantics as news_sentiment._dedup_articles; titles that normalize to
    empty are kept as their own singletons (they carry no dedup evidence).
    """
    keep_idx: list[int] = []
    dup_counts: list[int] = []
    kept_norms: list[str] = []
    for i, t in enumerate(titles):
        norm = norm_title(t)
        if not norm:
            keep_idx.append(i)
            dup_counts.append(0)
            kept_norms.append(f"\x00empty{i}")  # never matches anything real
            continue
        dup_of = None
        for j, kn in enumerate(kept_norms):
            if _fuzz.token_set_ratio(norm, kn) >= DEDUP_SIMILARITY:
                dup_of = j
                break
        if dup_of is not None:
            dup_counts[dup_of] += 1
        else:
            keep_idx.append(i)
            dup_counts.append(0)
            kept_norms.append(norm)
    return keep_idx, dup_counts


def window_sample(articles: list[dict], cap: int, min_recent: int) -> list[dict]:
    """Trim a newest-first article list to ~`cap` while spanning its window.

    Shared by the app (the retained set the timeline, tape, News read and
    Market read all see) and by ml/scripts/06's training panel, which applies
    it to every ticker-day window so the Market read's per-window statistics
    (max/min encoder score, shares) are computed over the same-shaped set in
    training and in serving.

    High-volume tickers publish enough that the newest N cluster within hours,
    so a plain newest-N cut would show only "today". The newest `min_recent`
    are kept verbatim (the News read scores exactly those) and the older
    remainder is TIME-stratified: its span is split into `cap - min_recent`
    equal buckets and the newest article of each non-empty bucket is kept.
    Undated articles (datetime missing/0) carry no time signal, so they are
    excluded from bucketing and merged back in with whatever slots remain.
    Input and output are newest-first; the result may be smaller than `cap`
    when older buckets are empty.
    """
    if len(articles) <= cap:
        return articles
    recent = articles[:min_recent]
    rest = articles[min_recent:]
    slots = cap - min_recent
    if slots <= 0 or not rest:
        return recent
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


# ------------------------------------------------------------------ company names
def _find_symbol_db() -> Path | None:
    # The symbol pack's file in the data folder (symbol_db.py). Since 2.0 its
    # names come from Yahoo, not NASDAQ/SEC lists; _clean_name drops the legal
    # forms where the two differ, so a company keeps the same cleaned name.
    from convexity import symbol_db

    p = symbol_db.db_path()
    return p if p.exists() else None


class _CompanyNames:
    """{ticker: company name}, read through from symbol_db.sqlite one ticker
    at a time and memoised. The symbol pack holds every Yahoo listing (~470k):
    as a dict that was ~100 MB for lookups that need a few hundred names. Only
    `.get()` is offered — the one call every caller (app and ml/) makes — and
    the names are the same rows a full read returned."""

    def __init__(self, db: Path | None):
        self._db, self._memo = db, {}

    def get(self, symbol, default=None):
        key = (symbol or "").upper()
        if key not in self._memo:
            self._memo[key] = self._fetch(key)
        return default if self._memo[key] is None else self._memo[key]

    def _fetch(self, key: str) -> str | None:
        if self._db is None or not key:
            return None
        try:
            uri = self._db.as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as con:
                row = con.execute("SELECT name FROM symbols WHERE ticker = ?", (key,)).fetchone()
        except Exception:
            return None
        return row[0] if row and row[0] else None


@lru_cache(maxsize=1)
def load_company_names() -> _CompanyNames:
    """{ticker: company name} from symbol_db.sqlite (`.get()` only); empty
    when unavailable. symbol_db clears the cache when a new pack lands."""
    return _CompanyNames(_find_symbol_db())


def _clean_name(name: str) -> str:
    return norm_title(_NAME_SUFFIX_RE.sub(" ", name or ""))


def _name_hits(clean_name: str, text_norm: str) -> bool:
    if len(clean_name) < 3 or not text_norm:
        return False
    if clean_name in text_norm:
        return True
    return _fuzz.partial_ratio(clean_name, text_norm) >= _NAME_MATCH


def _ticker_hits(symbol: str, raw_text: str) -> bool:
    """Word-boundary ticker match. 1-2 char tickers (A, IT, ALL...) collide with
    English words, so those require an explicit cashtag/parenthesis form."""
    if not raw_text:
        return False
    sym = re.escape(symbol.upper())
    if len(symbol) <= 2:
        return (
            re.search(
                rf"\${sym}\b|\({sym}\)|NYSE:\s*{sym}\b|NASDAQ:\s*{sym}\b", raw_text, re.IGNORECASE
            )
            is not None
        )
    return re.search(rf"\b{sym}\b", raw_text) is not None


# ------------------------------------------------------------------ relevance
def is_boilerplate(title) -> bool:
    """Roundup/listicle headline — an article ABOUT many stocks, not this one.
    Exposed on its own because the Market read counts the share of such items
    in a ticker's window (ml_features.window_vector), train and serve alike."""
    return bool(_BOILERPLATE_RE.search(title if isinstance(title, str) else ""))


def relevance_score(
    title: str,
    summary: str | None,
    symbol: str,
    company_name: str | None = None,
    co_mention_count: int = 1,
    publisher_tier: float = 0.7,
) -> float:
    """Relevance of one article to one symbol, in [0.05, 1.0]. Deterministic.

    Multiplicative combination of:
      mention position  — company named in title (1.0) > lead 150 chars (0.6)
                          > summary body (0.35) > tagged-only (0.2)
      co-mention penalty— 1/(1 + 0.4*(n-1)): a 5-ticker roundup is worth ~0.4
      boilerplate       — x0.45 when the title matches listicle/roundup patterns
      headline length   — x0.85 beyond 120 chars (roundups run long)
      publisher tier    — softened to 0.75..1.0 so source quality tilts but
                          never dominates the text evidence
    """
    # NaN floats from pandas are truthy — isinstance, not `or`, is the guard.
    title = title if isinstance(title, str) else ""
    summary = summary if isinstance(summary, str) else ""
    if company_name is not None and not isinstance(company_name, str):
        company_name = None
    if company_name is None:
        company_name = load_company_names().get((symbol or "").upper())
    clean = _clean_name(company_name) if company_name else ""

    title_norm = norm_title(title)
    lead_norm = norm_title(summary[:150])
    body_norm = norm_title(summary[150:1000])

    if _ticker_hits(symbol, title) or _name_hits(clean, title_norm):
        mention = 1.0
    elif _ticker_hits(symbol, summary[:150]) or _name_hits(clean, lead_norm):
        mention = 0.6
    elif _ticker_hits(symbol, summary[150:1000]) or _name_hits(clean, body_norm):
        mention = 0.35
    else:
        mention = 0.2

    n_co = max(1, int(co_mention_count))
    p_co = 1.0 / (1.0 + 0.4 * (n_co - 1))
    p_boiler = 0.45 if is_boilerplate(title) else 1.0
    p_len = 0.85 if len(title) > 120 else 1.0
    p_pub = 0.75 + 0.25 * max(0.0, min(1.0, float(publisher_tier)))

    score = mention * p_co * p_boiler * p_len * p_pub
    return round(max(0.05, min(1.0, score)), 4)
