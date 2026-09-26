"""Loughran-McDonald dictionary sentiment — the free local cross-check.

Why this exists: the LLM sentiment score (news_sentiment.py) is a black box.
Loughran & McDonald (2011, J. Finance) showed generic sentiment lexicons
misclassify financial text ("liability", "tax" read as negative by Harvard
GI but are neutral in filings) and published finance-specific word lists
that became the standard dictionary baseline. Scoring an article batch
through these lists costs microseconds and zero API calls, giving an
independent polarity signal to sanity-check the LLM against: when the two
disagree strongly with opposite signs, the ticker gets a `disagreement`
flag and its confidence is capped (see news_sentiment.aggregate_scores).

Word lists are vendored in data/lm_lexicon.json (2024 vintage, positive /
negative / uncertainty; license note inside the file). Pure stdlib.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_DATA_FILE = Path(__file__).resolve().parent / "data" / "lm_lexicon.json"

_POSITIVE: frozenset[str] = frozenset()
_NEGATIVE: frozenset[str] = frozenset()
_UNCERTAINTY: frozenset[str] = frozenset()
_LOADED = False

# Simple negation cues: a polarity word within NEGATION_WINDOW tokens after
# one of these flips sign ("not profitable" is negative). LM's own guidance
# is that negation handling matters most for the (small) positive list.
_NEGATORS = frozenset({"not", "no", "never", "without", "neither", "nor"})
_NEGATION_WINDOW = 3

_TOKEN_RE = re.compile(r"[a-z']+")


def _load() -> bool:
    """Lazy-load word lists once. Missing/corrupt data file degrades to
    'lexicon unavailable' (lm_score returns None) instead of crashing —
    same optional-dependency philosophy as symbol_db."""
    global _POSITIVE, _NEGATIVE, _UNCERTAINTY, _LOADED
    if _LOADED:
        return bool(_POSITIVE)
    _LOADED = True
    try:
        data = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        _POSITIVE = frozenset(data["positive"])
        _NEGATIVE = frozenset(data["negative"])
        _UNCERTAINTY = frozenset(data.get("uncertainty", []))
    except Exception:
        return False
    return True


def lm_score(text: str) -> float | None:
    """Polarity of `text` in [-1, +1]: (pos - neg) / (pos + neg).

    Returns None when the lexicon is unavailable or the text contains no
    polarity words at all (no signal is not the same as neutral signal —
    callers should skip None articles when aggregating).
    """
    if not text or not _load():
        return None
    tokens = _TOKEN_RE.findall(text.lower())
    pos = neg = 0
    for i, tok in enumerate(tokens):
        hit_pos = tok in _POSITIVE
        hit_neg = tok in _NEGATIVE
        if not (hit_pos or hit_neg):
            continue
        lo = max(0, i - _NEGATION_WINDOW)
        negated = any(t in _NEGATORS for t in tokens[lo:i])
        if hit_pos:
            neg += 1 if negated else 0
            pos += 0 if negated else 1
        elif hit_neg:
            # Negated negatives ("no losses") count as weakly positive.
            pos += 1 if negated else 0
            neg += 0 if negated else 1
    total = pos + neg
    if total == 0:
        return None
    return (pos - neg) / total


def uncertainty_ratio(text: str) -> float | None:
    """Fraction of tokens on the LM uncertainty list — a cheap 'how hedged
    is this coverage' signal (not wired into scoring yet; exposed for the
    diagnostics panel / future use)."""
    if not text or not _load():
        return None
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return None
    return sum(1 for t in tokens if t in _UNCERTAINTY) / len(tokens)
