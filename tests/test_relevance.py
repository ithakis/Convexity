"""Unit tests for convexity.relevance — the deterministic relevance
heuristic and the shared title-dedup primitives (train/serve contract)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time

from convexity.relevance import (
    cluster_titles,
    is_boilerplate,
    is_near_duplicate,
    norm_title,
    relevance_score,
    window_sample,
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


def test_is_boilerplate_flags_roundups_only():
    assert is_boilerplate("Top 10 stocks to watch this week")
    assert is_boilerplate("Market wrap: Dow slips")
    assert not is_boilerplate("Acme raises full-year guidance")
    assert not is_boilerplate(None)


# window_sample is shared by the app's retained article set and the Market
# read's training panel (ml/scripts/06), so its shape is a train/serve contract.
def test_window_sample_spans_window_instead_of_collapsing_to_newest():
    now = time.time()
    arts = [{"headline": f"h{i}", "datetime": now - i * (7 * 86400 / 40), "url": f"u{i}"}
            for i in range(40)]
    out = window_sample(arts, cap=25, min_recent=10)
    span_days = (out[0]["datetime"] - out[-1]["datetime"]) / 86400.0
    assert span_days > 5.0  # a plain newest-25 cut would only span ~4.4 days
    assert out[:10] == arts[:10]  # newest `min_recent` kept verbatim, in order


def test_window_sample_zero_timestamp_does_not_collapse_the_window():
    # A single datetime==0 article (unparsed yfinance pubDate) used to make
    # tmin=0, so every dated article landed in the last bucket.
    now = time.time()
    dated = [{"headline": f"h{i}", "datetime": now - i * 3600, "url": f"u{i}"}
             for i in range(40)]
    out = window_sample(dated + [{"headline": "unparsed", "datetime": 0, "url": "uz"}],
                        cap=30, min_recent=10)
    assert len(out) > 20


def test_window_sample_all_zero_timestamps_does_not_crash():
    arts = [{"headline": f"z{i}", "datetime": 0, "url": f"u{i}"} for i in range(20)]
    assert len(window_sample(arts, cap=15, min_recent=5)) == 15


def test_window_sample_is_a_no_op_below_the_cap():
    arts = [{"headline": "a", "datetime": 1}, {"headline": "b", "datetime": 0}]
    assert window_sample(arts, cap=60, min_recent=15) is arts
