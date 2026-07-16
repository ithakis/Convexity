"""Unit tests for portfolio_tracker.relevance — the deterministic relevance
heuristic and the shared title-dedup primitives (train/serve contract)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_tracker.relevance import (
    cluster_titles,
    is_near_duplicate,
    norm_title,
    relevance_score,
)


def test_norm_title_matches_app_convention():
    assert norm_title("Apple, Inc. -- BEATS!  Q3") == "apple inc beats q3"
    assert norm_title("") == ""
    assert norm_title(None) == ""


def test_cluster_titles_collapses_near_duplicates():
    keep, dups = cluster_titles([
        "Apple beats earnings estimates",
        "Apple Beats Earnings Estimates!",
        "Microsoft launches new product",
    ])
    assert keep == [0, 2]
    assert dups == [1, 0]


def test_cluster_titles_keeps_empty_titles_as_singletons():
    keep, dups = cluster_titles(["", "Real headline", ""])
    assert keep == [0, 1, 2]
    assert dups == [0, 0, 0]


def test_is_near_duplicate_symmetric():
    a, b = "Tesla raises guidance for 2024", "Tesla Raises 2024 Guidance"
    assert is_near_duplicate(a, b) == is_near_duplicate(b, a)


def test_relevance_ordering_title_beats_lead_beats_tagged_only():
    title_hit = relevance_score("Apple announces record sales", "", "AAPL", "Apple Inc.")
    lead_hit = relevance_score("Tech earnings preview", "Apple reports this week...",
                               "AAPL", "Apple Inc.")
    tagged = relevance_score("Markets rally on Fed decision", "The S&P rose.",
                             "AAPL", "Apple Inc.")
    assert title_hit > lead_hit > tagged


def test_relevance_boilerplate_penalty():
    normal = relevance_score("Apple announces record sales", "", "AAPL", "Apple Inc.")
    listicle = relevance_score("Top 10 stocks to watch: Apple leads", "", "AAPL",
                               "Apple Inc.")
    assert listicle < normal


def test_relevance_co_mention_penalty_monotonic():
    scores = [relevance_score("Apple announces record sales", "", "AAPL",
                              "Apple Inc.", co_mention_count=n) for n in (1, 3, 10)]
    assert scores[0] > scores[1] > scores[2]


def test_relevance_short_ticker_needs_explicit_form():
    # "A" (Agilent) must not fire on the word "All"; cashtag form must.
    loose = relevance_score("All eyes on the Fed", "", "A", None)
    explicit = relevance_score("Agilent (A) beats estimates", "", "A", None)
    assert explicit > loose


def test_relevance_bounds_and_determinism():
    args = ("Apple announces record sales", "Apple Inc. said...", "AAPL", "Apple Inc.")
    s1, s2 = relevance_score(*args), relevance_score(*args)
    assert s1 == s2
    assert 0.05 <= s1 <= 1.0
