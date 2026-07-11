import os
import sys
import time

import pytest


_REPO = os.path.dirname(os.path.dirname(__file__))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from portfolio_tracker import news_sentiment as ns


@pytest.fixture(autouse=True)
def _clean_news_state(monkeypatch, tmp_path):
    ns._NEWS_CACHE.clear()
    ns._SENTIMENT_CACHE.clear()
    with ns._FH_LIMITER._lock:
        ns._FH_LIMITER._calls.clear()
    with ns._NV_LIMITER._lock:
        ns._NV_LIMITER._calls.clear()
    monkeypatch.setattr(ns, "_PERSIST_FILE", tmp_path / ".portfolio_tracker_news.json")
    monkeypatch.setattr(ns, "_HISTORY_FILE", tmp_path / ".portfolio_tracker_sentiment_history.json")
    monkeypatch.setattr(ns, "_schedule_persist", lambda: None)
    monkeypatch.setattr(ns, "_fh_rate_limit_until", 0.0)
    monkeypatch.setattr(ns, "_nv_rate_limit_until", 0.0)
    yield
    ns._NEWS_CACHE.clear()
    ns._SENTIMENT_CACHE.clear()


def test_refresh_symbol_restores_previous_cache_on_transient_failure(monkeypatch):
    prev_news = [{"headline": "Old headline"}]
    prev_sentiment = {
        "tier": "bullish",
        "score": 0.45,
        "summary": "Previous cached sentiment.",
        "assessed_at": "2026-05-23T09:00:00+00:00",
    }
    now = time.time()
    ns._NEWS_CACHE["news|MSFT|7"] = (now, ns._NEWS_TTL, prev_news)
    ns._SENTIMENT_CACHE["sentiment|MSFT|7"] = (now, ns._SENTIMENT_TTL, prev_sentiment)

    monkeypatch.setattr(ns, "get_news_sentiment",
                        lambda symbol, context=None, stage_cb=None: None)

    result = ns._refresh_symbol_sentiment("MSFT")

    assert result == prev_sentiment
    assert ns._cache_get(ns._NEWS_CACHE, "news|MSFT|7") == prev_news
    assert ns._cache_get(ns._SENTIMENT_CACHE, "sentiment|MSFT|7") == prev_sentiment


def test_refresh_sentiment_returns_consistent_status_shape(monkeypatch):
    market = {
        "tier": "neutral",
        "score": 0.0,
        "summary": "Market unchanged.",
        "assessed_at": "2026-05-23T10:00:00+00:00",
    }
    sentiments = {
        "MSFT": {
            "tier": "bullish",
            "score": 0.62,
            "summary": "Positive incremental signal.",
            "assessed_at": "2026-05-23T10:01:00+00:00",
        },
        "TSLA": None,
    }

    monkeypatch.setattr(ns, "FINNHUB_API_KEY", "fh-test")
    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "nv-test")
    monkeypatch.setattr(ns, "_refresh_market_sentiment",
                        lambda days=7, stage_cb=None: market)
    monkeypatch.setattr(ns, "_refresh_symbol_sentiment",
                        lambda symbol, context=None, stage_cb=None: sentiments[symbol])

    result = ns.refresh_sentiment(["MSFT", "TSLA"])
    status = result["status"]

    assert result["market"] == market
    assert result["portfolio"] == sentiments
    assert status["finnhub_key"] is True
    assert status["finnhub_key_set"] is True
    assert status["nvidia_key"] is True
    assert status["nvidia_key_set"] is True
    assert status["market_ok"] is True
    assert status["fetched"] == 1
    assert status["total"] == 2
    assert "finnhub_calls_used" in status
    assert "finnhub_calls_limit" in status
    assert "nvidia_calls_used" in status
    assert "nvidia_calls_limit" in status


def test_status_reports_backoff_windows(monkeypatch):
    monkeypatch.setattr(ns, "FINNHUB_API_KEY", "fh-test")
    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "nv-test")
    monkeypatch.setattr(ns, "_fh_rate_limit_until", time.time() + 9.2)
    monkeypatch.setattr(ns, "_nv_rate_limit_until", time.time() + 4.4)

    status = ns.status()

    assert status["finnhub_backoff_s"] >= 9
    assert status["nvidia_backoff_s"] >= 4
    assert status["finnhub_calls_limit"] == ns._FH_LIMITER.max
    assert status["nvidia_calls_limit"] == ns._NV_LIMITER.max


def test_nvidia_call_retries_after_non_json(monkeypatch):
    class _Message:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _Message(content)

    class _Response:
        def __init__(self, content):
            self.choices = [_Choice(content)]

    class _Completions:
        def __init__(self, payloads):
            self.payloads = list(payloads)

        def create(self, **_kwargs):
            return _Response(self.payloads.pop(0))

    class _Chat:
        def __init__(self, payloads):
            self.completions = _Completions(payloads)

    class _Client:
        def __init__(self, payloads):
            self.chat = _Chat(payloads)

    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "nv-test")
    monkeypatch.setattr(ns, "_get_client", lambda: _Client([
        "not-json",
        '{"tier":"bullish","score":0.5,"summary":"Recovered on retry."}',
    ]))
    monkeypatch.setattr(ns.time, "sleep", lambda *_args, **_kwargs: None)

    result = ns._nvidia_call("system", "user")

    assert result == {
        "tier": "bullish",
        "score": 0.5,
        "summary": "Recovered on retry.",
    }

# ---------------------------------------------------------------------------
# Math core — aggregation / decomposition / calibration / lexicon / dedup
# ---------------------------------------------------------------------------

import time as _time

from portfolio_tracker import lexicon as lex


def _art(score, days_ago=0.0, source="Reuters", n_dup=0, relevance="high"):
    return {
        "score": score,
        "datetime": _time.time() - days_ago * 86400,
        "source": source,
        "n_duplicates": n_dup,
        "relevance": relevance,
    }


def test_aggregate_scores_weighted_mean_and_bounds():
    agg = ns.aggregate_scores([_art(0.8), _art(0.4)])
    assert agg is not None
    assert 0.4 <= agg["s_idio"] <= 0.8
    assert 0.0 <= agg["confidence"] <= 1.0
    assert agg["n_articles"] == 2


def test_aggregate_scores_recency_decay_downweights_old_news():
    fresh_neg = _art(-1.0, days_ago=0.0)
    stale_pos = _art(+1.0, days_ago=7.0)
    agg = ns.aggregate_scores([fresh_neg, stale_pos])
    assert agg["s_idio"] < 0  # fresh article dominates


def test_aggregate_scores_source_tier_downweights_wires():
    tier1_neg = _art(-1.0, source="Reuters")
    wire_pos = _art(+1.0, source="PR Newswire")
    agg = ns.aggregate_scores([tier1_neg, wire_pos])
    assert agg["s_idio"] < 0


def test_aggregate_scores_skips_unscored_articles():
    assert ns.aggregate_scores([{"datetime": _time.time()}]) is None
    agg = ns.aggregate_scores([_art(0.5), {"datetime": _time.time()}])
    assert agg["n_articles"] == 1


def test_dispersion_lowers_confidence():
    uniform = ns.aggregate_scores([_art(0.5), _art(0.5), _art(0.5)])
    mixed = ns.aggregate_scores([_art(0.9), _art(-0.9), _art(0.5)])
    assert mixed["confidence"] < uniform["confidence"]


def test_combine_signal_decomposition_and_clip():
    # beta=1.5, s_mkt=-0.4: systematic drag of KAPPA*1.5*(-0.4)
    total = ns.combine_signal(0.3, 1.5, -0.4)
    assert total == pytest.approx(0.3 + ns.KAPPA * 1.5 * -0.4)
    # missing beta/market -> pure idio
    assert ns.combine_signal(0.3, None, -0.4) == 0.3
    assert ns.combine_signal(0.3, 1.5, None) == 0.3
    # clipping
    assert ns.combine_signal(0.9, 2.0, 1.0) == 1.0
    assert ns.combine_signal(-0.9, 2.0, -1.0) == -1.0


def test_calibrate_tier_fixed_thresholds_without_history():
    assert ns.calibrate_tier(-0.8) == "very_bearish"
    assert ns.calibrate_tier(-0.3) == "bearish"
    assert ns.calibrate_tier(0.0) == "neutral"
    assert ns.calibrate_tier(0.3) == "bullish"
    assert ns.calibrate_tier(0.8) == "very_bullish"


def test_calibrate_tier_quantiles_guarantee_full_range():
    # History strongly clustered positive: quantile calibration must still
    # label the bottom decile very_bearish even though scores are all > 0.
    hist = [0.1 + 0.8 * i / 199 for i in range(200)]
    assert ns.calibrate_tier(0.05, hist) == "very_bearish"
    assert ns.calibrate_tier(0.95, hist) == "very_bullish"
    mid = sorted(hist)[100]
    assert ns.calibrate_tier(mid, hist) == "neutral"


def test_is_disagreement_requires_opposite_signs_and_magnitude():
    assert ns.is_disagreement(0.5, -0.5) is True
    assert ns.is_disagreement(0.5, 0.1) is False       # same-ish direction
    assert ns.is_disagreement(0.1, -0.1, ) is False    # too small
    assert ns.is_disagreement(0.5, None) is False      # no lexicon signal


def test_lexicon_scores_polarity_and_negation():
    pos = lex.lm_score("Company achieved record profitability and strong growth")
    neg = lex.lm_score("Company faces litigation, losses and bankruptcy risk")
    assert pos is not None and pos > 0
    assert neg is not None and neg < 0
    assert lex.lm_score("The quarterly report was published on Tuesday") is None
    flipped = lex.lm_score("The company is not profitable")
    assert flipped is not None and flipped < 0


def test_dedup_collapses_syndicated_copies():
    now = _time.time()
    arts = [
        {"headline": "Acme Corp raises full-year guidance", "datetime": now - 100},
        {"headline": "Acme Corp Raises Full-Year Guidance", "datetime": now - 50},
        {"headline": "Completely different story about Acme lawsuit", "datetime": now},
    ]
    out = ns._dedup_articles(arts)
    assert len(out) == 2
    guid = [a for a in out if "guidance" in a["headline"].lower()][0]
    assert guid["n_duplicates"] == 1
    assert guid["datetime"] == now - 100  # earliest copy kept as canonical


def test_window_sample_spans_window_instead_of_collapsing_to_newest():
    now = _time.time()
    # 40 articles evenly spread across 7 days, newest-first (matches
    # _dedup_articles' output order).
    arts = [{"headline": f"h{i}", "datetime": now - i * (7 * 86400 / 40), "url": f"u{i}"}
            for i in range(40)]
    out = ns._window_sample(arts, cap=25, min_recent=10)
    span_days = (out[0]["datetime"] - out[-1]["datetime"]) / 86400.0
    assert span_days > 5.0  # a plain newest-25 cut would only span ~4.4 days
    assert out[:10] == arts[:10]  # newest `min_recent` kept verbatim, in order


def test_window_sample_zero_timestamp_does_not_collapse_the_window():
    # Regression: a single datetime==0 article (unparsed yfinance pubDate)
    # used to make tmin=0, blowing up the bucket span so every dated article
    # landed in the same last bucket and the retained set collapsed to ~2.
    now = _time.time()
    dated = [{"headline": f"h{i}", "datetime": now - i * 3600, "url": f"u{i}"}
              for i in range(40)]
    zero_ts = [{"headline": "unparsed-date", "datetime": 0, "url": "uz"}]
    out = ns._window_sample(dated + zero_ts, cap=30, min_recent=10)
    assert len(out) > 20  # not collapsed to ~2


def test_window_sample_all_zero_timestamps_does_not_crash():
    arts = [{"headline": f"z{i}", "datetime": 0, "url": f"u{i}"} for i in range(20)]
    out = ns._window_sample(arts, cap=15, min_recent=5)
    assert len(out) == 15


def test_validate_article_scores_strictness():
    ok = ns._validate_article_scores({
        "articles": [
            {"id": 1, "score": 0.4, "event": "earnings", "relevance": "high"},
            {"id": 2, "score": -2.5, "event": "bogus", "relevance": "nope"},
        ],
        "brief": "Acme rose 4% after beating estimates.",
    }, 2)
    assert ok is not None
    scores, brief = ok
    assert scores[0]["score"] == 0.4
    assert scores[1]["score"] == -1.0          # clipped
    assert scores[1]["event"] == "other"       # normalised
    assert scores[1]["relevance"] == "med"     # normalised
    assert brief.startswith("Acme")
    # fragmentary coverage (< half the ids) is a failed call
    assert ns._validate_article_scores(
        {"articles": [{"id": 1, "score": 0.1}], "brief": "x"}, 4) is None
    assert ns._validate_article_scores({"brief": "no articles"}, 3) is None


def test_history_append_and_calibration_scores(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "_HISTORY_FILE", tmp_path / "hist.json")
    rec = {"date": "2026-07-08", "symbol": "MSFT", "s_total": 0.42, "s_idio": 0.4}
    ns._history_append(rec)
    ns._history_append(dict(rec, s_total=0.5))  # same-day overwrite
    ns._history_append({"date": "2026-07-08", "symbol": "__market__", "s_total": 0.1})
    recs = ns._history_load()
    assert len(recs) == 2
    scores = ns._calibration_scores()
    assert scores == [0.5]  # market excluded, overwrite applied
