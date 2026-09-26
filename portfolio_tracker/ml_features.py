"""Shared article featurizer for the ML sentiment model — train AND serve.

The single implementation both sides use: ml/scripts/06_build_features.py calls
this to build the training matrix, and ml_sentiment.py calls it per live
article. Any change here invalidates the deployed artifact (the artifact's
feature_schema.json records DENSE_COLUMNS + hashing config; ml_sentiment
refuses to score when they disagree).

Text representation: HashingVectorizer (stateless — no vocabulary to ship or
drift) uni+bigrams at 2**18 dims, times a train-fitted idf vector, L2 row
normalization. Dense block: LM lexicon scores, event-keyword flags, relevance,
novelty, session one-hot, calendar, trailing-only market context.

sklearn is imported lazily so the app can import this module (and degrade
gracefully) when sklearn isn't installed.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache

HASH_DIM = 2 ** 18
NGRAM_RANGE = (1, 2)
# Only this many summary chars feed the HASHED text (memory: nnz/row budget on
# an 8 GB training box). The lexicon/dense features still read the full summary.
HASH_SUMMARY_MAX_CHARS = 600

# Event-keyword flags. Deliberately coarse — LightGBM learns interactions; the
# flags exist so the model can split on event type without needing the exact
# vocabulary, and so feature importances are readable.
EVENT_PATTERNS: dict[str, str] = {
    "earnings": r"earnings|\beps\b|quarterly results|reports q[1-4]|profit|net income",
    "guidance": r"guidance|outlook|forecast|raises full.year|lowers full.year",
    "upgrade": r"upgrade[sd]?|initiates.*(buy|outperform|overweight)",
    "downgrade": r"downgrade[sd]?|cuts? rating|initiates.*(sell|underperform|underweight)",
    "price_target": r"price target|\bpt\b raised|\bpt\b lowered",
    "ma": r"merger|acquisition|acquires?|takeover|buyout|to acquire|\bdeal\b",
    "dividend": r"dividend|payout",
    "buyback": r"buyback|repurchase",
    "offering": r"offering|dilut|secondary|private placement|registered direct|atm program",
    "legal": r"lawsuit|litigation|probe|investigation|sec charges|settle|fraud|subpoena",
    "fda": r"\bfda\b|clinical|phase (1|2|3|i{1,3})\b|trial results|approval",
    "bankruptcy": r"bankrupt|chapter 11|delisting|going concern|default",
    "split": r"stock split|reverse split",
    "contract": r"contract|partnership|collaboration|agreement|awarded",
    "insider": r"insider|ceo (buys|sells)|director (buys|sells)|10% owner",
    "index_change": r"joins? s&p|added to (the )?(s&p|nasdaq|russell)|removed from (the )?(s&p|nasdaq|russell)",
}
_EVENT_RE = {k: re.compile(v, re.IGNORECASE) for k, v in EVENT_PATTERNS.items()}

SESSION_CLASSES = ("overnight", "preopen_oc", "intraday_cc", "dateonly_cc")

# Column order is the schema contract — append-only; never reorder.
DENSE_COLUMNS: list[str] = [
    "lm_score", "lm_missing", "uncertainty_ratio",
    *[f"ev_{k}" for k in EVENT_PATTERNS],
    "publisher_tier", "relevance", "log1p_n_duplicates", "log1p_co_mentions",
    *[f"sess_{s}" for s in SESSION_CLASSES],
    "day_of_week", "month",
    "title_n_tokens", "summary_n_tokens",
    "spy_ret_1d", "spy_ret_5d", "spy_vol_20d", "tkr_ret_5d",
]

_TIER1 = ("reuters", "bloomberg", "wall street journal", "wsj",
          "financial times", "associated press", "ap news")
_TIER2 = ("cnbc", "barron", "marketwatch", "yahoo", "forbes",
          "investor's business daily", "business insider", "fortune")
_WIRE = ("pr newswire", "prnewswire", "globenewswire", "business wire",
         "businesswire", "accesswire", "motley fool", "zacks",
         "seekingalpha", "seeking alpha", "benzinga", "investorplace",
         "thefly", "newsfile", "openpr")


@lru_cache(maxsize=4096)
def publisher_tier(source) -> float:
    """Same tiering as news_sentiment._source_weight (kept in sync by
    tests/test_ml_features.py rather than by import, so this module never
    pulls in the news/LLM stack). Accepts anything — pandas hands NaN floats
    for missing publishers."""
    s = source.lower() if isinstance(source, str) else ""
    if any(t in s for t in _TIER1):
        return 1.0
    if any(t in s for t in _TIER2):
        return 0.8
    if any(t in s for t in _WIRE):
        return 0.5
    return 0.7


def event_flags(text: str) -> dict[str, int]:
    return {k: 1 if rx.search(text) else 0 for k, rx in _EVENT_RE.items()}


def dense_vector(article: dict) -> list[float]:
    """One dense feature row. `article` keys (all optional, sane defaults):
    title, summary, publisher, relevance, n_duplicates, co_mention_count,
    session_class, day_of_week, month, spy_ret_1d, spy_ret_5d, spy_vol_20d,
    tkr_ret_5d. Market-context values MUST be trailing-only (as of the window
    start) — passing anything contemporaneous with the label window is leakage.
    """
    from portfolio_tracker.lexicon import lm_score, uncertainty_ratio

    # NaN floats from pandas are truthy — isinstance, not `or`, is the guard.
    title = article.get("title")
    title = title if isinstance(title, str) else ""
    summary = article.get("summary")
    summary = summary if isinstance(summary, str) else ""
    text = f"{title} {summary}".strip()

    lm = lm_score(text)
    unc = uncertainty_ratio(text)
    ev = event_flags(text)
    sess = article.get("session_class") or ""

    row = [
        float(lm) if lm is not None else 0.0,
        1.0 if lm is None else 0.0,
        float(unc) if unc is not None else 0.0,
        *[float(ev[k]) for k in EVENT_PATTERNS],
        float(article.get("publisher_tier") if article.get("publisher_tier") is not None
              else publisher_tier(article.get("publisher"))),
        float(article.get("relevance", 0.5)),
        math.log1p(float(article.get("n_duplicates", 0) or 0)),
        math.log1p(max(0.0, float(article.get("co_mention_count", 1) or 1) - 1.0)),
        *[1.0 if sess == s else 0.0 for s in SESSION_CLASSES],
        float(article.get("day_of_week", 0) or 0),
        float(article.get("month", 0) or 0),
        float(len(title.split())),
        float(len(summary.split())),
        float(article.get("spy_ret_1d", 0.0) or 0.0),
        float(article.get("spy_ret_5d", 0.0) or 0.0),
        float(article.get("spy_vol_20d", 0.0) or 0.0),
        float(article.get("tkr_ret_5d", 0.0) or 0.0),
    ]
    assert len(row) == len(DENSE_COLUMNS)
    return row


# ------------------------------------------------------------------ text side
def _vectorizer():
    from sklearn.feature_extraction.text import HashingVectorizer

    return HashingVectorizer(
        n_features=HASH_DIM, ngram_range=NGRAM_RANGE, alternate_sign=False,
        lowercase=True, norm=None, dtype="float32",
    )


def hash_counts(texts: list[str]):
    """Stateless hashed term counts (CSR float32, n x HASH_DIM)."""
    return _vectorizer().transform(texts)


def fit_idf(count_matrices) -> "object":
    """Smooth idf (sklearn formula) from an iterable of count CSR matrices —
    TRAIN rows only; the fitted vector ships in the artifact."""
    import numpy as np

    df = np.zeros(HASH_DIM, dtype="int64")
    n_docs = 0
    for m in count_matrices:
        m = m.copy()
        m.data = (m.data > 0).astype("float32")
        df += np.asarray(m.sum(axis=0)).ravel().astype("int64")
        n_docs += m.shape[0]
    idf = np.log((1.0 + n_docs) / (1.0 + df)) + 1.0
    return idf.astype("float32")


def apply_idf(counts, idf):
    """counts x idf, L2 row-normalized (the tf-idf transform at serve time)."""
    from sklearn.preprocessing import normalize

    x = counts.multiply(idf).tocsr()
    return normalize(x, norm="l2", copy=False)


def text_for_hashing(title, summary) -> str:
    title = title if isinstance(title, str) else ""
    summary = summary if isinstance(summary, str) else ""
    return f"{title} {summary[:HASH_SUMMARY_MAX_CHARS]}".strip()


def feature_schema() -> dict:
    """Persisted into the artifact; ml_sentiment refuses to score on mismatch."""
    import hashlib

    patterns_blob = "|".join(f"{k}={v}" for k, v in sorted(EVENT_PATTERNS.items()))
    return {
        "hash_dim": HASH_DIM,
        "ngram_range": list(NGRAM_RANGE),
        "hash_summary_max_chars": HASH_SUMMARY_MAX_CHARS,
        "dense_columns": list(DENSE_COLUMNS),
        "event_patterns_md5": hashlib.md5(patterns_blob.encode()).hexdigest(),
        "session_classes": list(SESSION_CLASSES),
    }


# ------------------------------------------------------------------ ticker-day window
# The Market read scores a TICKER on an as-of day from its trailing window —
# the unit the app shows. SERVED (ml_sentiment.market_read, mlsent-v1.1):
# the window constants, article_weight and weighted_sar — the v1.1 score is
# the weighted mean of the encoder's per-article predictions. ml/scripts/06
# computes the calibration panel's `enc_wmean` through the same functions, so
# the tier cuts are fitted on exactly the number the app produces.
#
# window_vector / attention_shock / WINDOW_COLUMNS build the richer panel the
# v2 window model was trained on (ml/scripts/07-08). v2 failed its gates and
# is not served; they stay here only because 06 builds one panel for both
# (docs/ml_sentiment_design.md has the v2 evidence). No raw article count is
# in that panel: live Finnhub volume for a large cap is 10-50x what FNSPID
# tags per ticker; attn_shock is the scale-free version.
RECENCY_TAU_DAYS = 3.0
WINDOW_DAYS = 7            # articles dated D-6..D (ET calendar days)
ATTN_BASE_DAYS = 60        # attention baseline: the 60 days before the window
# The window's article set is capped exactly like the app's retained set
# (relevance.window_sample(articles, WINDOW_CAP, WINDOW_MIN_RECENT)) in both
# training and serving: max/min/share statistics depend on how many articles
# they range over, and live volume for a large cap dwarfs FNSPID's.
WINDOW_CAP = 60
WINDOW_MIN_RECENT = 15
WINDOW_COLUMNS: list[str] = [
    "enc_mean", "enc_max", "enc_min", "enc_wmean",
    "lm_mean", "unc_mean",
    "attn_shock",
    "share_tier1", "mean_log_dup", "share_boiler",
    "fresh_days",
    "tkr_ret_1d", "tkr_ret_5d", "tkr_ret_20d", "tkr_vol_20d",
    "spy_ret_5d", "spy_vol_20d",
]
PRICE_COLUMNS = WINDOW_COLUMNS[-6:]


def _num(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return math.nan
    return v if math.isfinite(v) else math.nan


def article_weight(a: dict, now: float, tau: float = RECENCY_TAU_DAYS) -> float:
    """recency x source x novelty x relevance — the evidence weight of one
    article in a ticker's window. Articles carry either a numeric
    `publisher_tier` (training rows) or a raw `source` string (live)."""
    age_d = max(0.0, (now - float(a.get("datetime") or now)) / 86400.0)
    tier = a.get("publisher_tier")
    w_src = float(tier) if tier is not None else publisher_tier(a.get("source"))
    w_nov = 1.0 / (1.0 + 0.5 * math.log1p(float(a.get("n_duplicates", 0) or 0)))
    rel = a.get("relevance")
    return math.exp(-age_d / tau) * w_src * w_nov * float(0.5 if rel is None else rel)


def weighted_sar(articles: list[dict], now: float, key: str = "sar_pred"):
    """(weighted mean of a[key], total weight) over articles that carry it,
    or (None, 0.0). The v1.1 Market read IS this number."""
    wsum = ssum = 0.0
    for a in articles:
        v = a.get(key)
        if v is None or not math.isfinite(float(v)):
            continue
        w = article_weight(a, now)
        wsum += w
        ssum += w * float(v)
    if wsum <= 0:
        return None, 0.0
    return ssum / wsum, wsum


def attention_shock(rate_window: float, rate_base: float) -> float:
    """log of (7-day article rate) vs (the ticker's prior-60-day rate), both in
    articles/day, add-one smoothed per 7 days so a first-ever article is a
    finite, large shock rather than an infinity."""
    return math.log((7.0 * max(0.0, rate_window) + 1.0) / (7.0 * max(0.0, rate_base) + 1.0))


def window_vector(articles: list[dict], now: float, rate_window: float,
                  rate_base: float, price: dict | None = None) -> list[float]:
    """One v2 window-panel feature row (training only). `articles` are the window's deduped items:
    {sar_pred, lm, unc, publisher_tier|source, n_duplicates, relevance,
    boiler, datetime}. `price` holds PRICE_COLUMNS as of the last completed
    session BEFORE the as-of day. Missing values are NaN (LightGBM routes
    them; never impute here or train and serve drift apart)."""
    preds = [float(a["sar_pred"]) for a in articles
             if a.get("sar_pred") is not None and math.isfinite(float(a["sar_pred"]))]
    lms = [float(a["lm"]) for a in articles
           if a.get("lm") is not None and math.isfinite(float(a["lm"]))]
    n = len(articles)
    wmean, _ = weighted_sar(articles, now)
    ages = [max(0.0, (now - float(a.get("datetime") or now)) / 86400.0) for a in articles]
    tiers = [float(a["publisher_tier"]) if a.get("publisher_tier") is not None
             else publisher_tier(a.get("source")) for a in articles]
    price = price or {}
    row = [
        sum(preds) / len(preds) if preds else math.nan,
        max(preds) if preds else math.nan,
        min(preds) if preds else math.nan,
        wmean if wmean is not None else math.nan,
        sum(lms) / len(lms) if lms else math.nan,
        (sum(_num(a.get("unc")) if math.isfinite(_num(a.get("unc"))) else 0.0
             for a in articles) / n) if n else math.nan,
        attention_shock(rate_window, rate_base),
        (sum(1.0 for t in tiers if t >= 1.0) / n) if n else math.nan,
        (sum(math.log1p(float(a.get("n_duplicates", 0) or 0)) for a in articles) / n)
        if n else math.nan,
        (sum(1.0 for a in articles if a.get("boiler")) / n) if n else math.nan,
        min(ages) if ages else math.nan,
        *[_num(price.get(c)) for c in PRICE_COLUMNS],
    ]
    assert len(row) == len(WINDOW_COLUMNS)
    return row
