"""News read engine (LLM, five lenses) — the deterministic math and the
failure handling. The model itself is mocked; the prompt is certified on the
gold set by scripts/benchmark_news_read.py, not here."""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

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
    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "nv-test")
    # The limiter's penalty (on 429/503) backfills the rolling window so the
    # next acquire waits ~60 s; with sleeps stubbed that would spin forever.
    monkeypatch.setattr(ns.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(ns._NV_LIMITER, "acquire", lambda cancel=None: None)
    monkeypatch.setattr(ns._NV_LIMITER, "penalize", lambda: None)
    monkeypatch.setattr(ns, "_LLM_STATUS", {"ok": None, "error": None, "at": None,
                                            "model": ns._MODEL, "permanent": False})
    yield
    ns._NEWS_CACHE.clear()
    ns._SENTIMENT_CACHE.clear()


# ---------------------------------------------------------------- fake NIM
class _HTTPError(Exception):
    def __init__(self, code):
        super().__init__(f"Error code: {code}")
        self.status_code = code


def _fake_client(outcomes, calls):
    """outcomes: list of payload strings/dicts or exceptions, consumed per call."""
    class _Msg:
        def __init__(self, c):
            self.content = c

    class _Resp:
        def __init__(self, c):
            self.choices = [type("C", (), {"message": _Msg(c)})()]

    class _Completions:
        def create(self, **kw):
            calls.append(kw)
            nxt = outcomes.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return _Resp(nxt if isinstance(nxt, str) else json.dumps(nxt))

    return type("Client", (), {"chat": type("Chat", (), {"completions": _Completions()})()})()


def _install(monkeypatch, outcomes):
    """Install ONE fake client (shared by every call) and return its call log."""
    calls = []
    client = _fake_client(list(outcomes), calls)
    monkeypatch.setattr(ns, "_get_client", lambda: client)
    return calls


def _payload(lenses_scores, brief="Acme beat estimates."):
    return {"items": [{"id": i, "lens": ln, "score": sc, "fact": f"fact {i}"}
                      for i, (ln, sc) in enumerate(lenses_scores, 1)], "brief": brief}


# ---------------------------------------------------------------- tiers
@pytest.mark.parametrize("score,tier", [
    (-2.0, "very_bearish"), (-1.25, "very_bearish"), (-1.24, "bearish"),
    (-0.4, "bearish"), (-0.39, "neutral"), (0.0, "neutral"), (0.39, "neutral"),
    (0.4, "bullish"), (1.24, "bullish"), (1.25, "very_bullish"), (2.0, "very_bullish"),
])
def test_tier_cut_edges(score, tier):
    assert ns.tier_for(score) == tier


def test_tier_of_nothing_is_nothing():
    assert ns.tier_for(None) is None


# ---------------------------------------------------------------- aggregation
def _item(lens, score, days_ago=0.0, source="Reuters", n_dup=0, fact="", aid=None):
    return {"lens": lens, "llm_score": score, "fact": fact or f"{lens} {score}",
            "datetime": time.time() - days_ago * 86400, "source": source,
            "n_duplicates": n_dup, "aid": aid or f"{lens}{score}{days_ago}"}


def test_lens_scores_are_weighted_means_with_the_leading_fact():
    now = time.time()
    items = [_item("financials", 2, fact="Q2 revenue beat", aid="a1"),
             _item("financials", 1, days_ago=3, aid="a2"),
             _item("outlook", -1, fact="Guidance cut", aid="a3")]
    out = ns.aggregate_items(items, agreement=1.0, now=now)
    fin = out["lenses"]["financials"]
    assert 1.0 < fin["score"] < 2.0                  # recency pulls toward the fresh +2
    assert fin["n"] == 2 and set(fin["ids"]) == {"a1", "a2"}
    assert fin["fact"] == "Q2 revenue beat"           # highest |score| x weight
    assert out["lenses"]["outlook"] == {"score": -1.0, "tier": "bearish", "n": 1,
                                        "fact": "Guidance cut", "ids": ["a3"]}


def test_empty_lens_is_null_not_neutral():
    out = ns.aggregate_items([_item("financials", 0)], agreement=1.0)
    assert out["lenses"]["financials"]["score"] == 0.0
    assert out["lenses"]["financials"]["tier"] == "neutral"
    for lens in ("outlook", "competition", "regulation", "street"):
        assert out["lenses"][lens] is None


def test_none_items_are_ignored_and_other_counts_overall():
    items = [_item("none", 0), _item("none", 0), _item("other", 2), _item("street", -1)]
    out = ns.aggregate_items(items, agreement=0.9)
    assert out["n_items"] == 4 and out["n_none"] == 2 and out["other_n"] == 1
    assert "other" not in out["lenses"]
    assert out["score"] == pytest.approx(0.5)        # (+2 - 1) / 2, same weights
    assert out["tier"] == "bullish"


def test_all_none_gives_no_overall_score():
    out = ns.aggregate_items([_item("none", 0)], agreement=1.0)
    assert out["score"] is None and out["tier"] is None and out["confidence"] == 0.0


def test_confidence_is_mass_times_agreement_and_single_pass_is_discounted():
    items = [_item("financials", 1) for _ in range(5)]
    both = ns.aggregate_items(items, agreement=1.0)["confidence"]
    half = ns.aggregate_items(items, agreement=0.5)["confidence"]
    single = ns.aggregate_items(items, agreement=None)
    assert half == pytest.approx(both / 2, abs=1e-3)
    assert single["agreement"] is None
    assert single["confidence"] == pytest.approx(both * ns._SINGLE_PASS_CONF, abs=1e-3)


def test_unscored_articles_are_skipped():
    assert ns.aggregate_items([{"headline": "x", "datetime": time.time()}], 1.0) is None


# ---------------------------------------------------------------- two passes
def test_merge_averages_scores_and_measures_agreement():
    p1 = [{"lens": "financials", "score": 2, "fact": "a"},
          {"lens": "outlook", "score": -1, "fact": "b"},
          {"lens": "street", "score": 1, "fact": "c"},
          {"lens": "none", "score": 0, "fact": "d"}]
    p2 = [{"lens": "financials", "score": 1, "fact": "a2"},   # same lens, gap 1: agrees
          {"lens": "outlook", "score": 1, "fact": "b2"},      # gap 2: disagrees
          {"lens": "none", "score": 0, "fact": "c2"},         # one says none -> none
          {"lens": "none", "score": 0, "fact": "d2"}]
    merged, agreement = ns.merge_passes(p1, p2)
    assert [m["lens"] for m in merged] == ["financials", "outlook", "none", "none"]
    assert [m["score"] for m in merged] == [1.5, 0.0, 0.0, 0.0]
    assert agreement == pytest.approx(2 / 4)


def test_merge_keeps_pass_one_lens_when_both_see_the_target():
    p1 = [{"lens": "outlook", "score": 1, "fact": "a"}]
    p2 = [{"lens": "financials", "score": 1, "fact": "b"}]
    merged, agreement = ns.merge_passes(p1, p2)
    assert merged[0]["lens"] == "outlook" and agreement == 0.0


def test_single_surviving_pass_has_null_agreement():
    p = [{"lens": "street", "score": -1, "fact": "a"}]
    assert ns.merge_passes(p, None) == (p, None)
    assert ns.merge_passes(None, p) == (p, None)
    assert ns.merge_passes(None, None) is None


# ---------------------------------------------------------------- validation
def test_validation_accepts_a_clean_pass_and_zeroes_none_scores():
    raw = _payload([("financials", 2), ("none", 1), ("other", -1)])
    items, brief = ns.validate_items(raw, 3)
    assert [i["lens"] for i in items] == ["financials", "none", "other"]
    assert [i["score"] for i in items] == [2, 0, -1]
    assert brief == "Acme beat estimates."


@pytest.mark.parametrize("mutate", [
    lambda it: it[0].update(id=0),                 # id below range
    lambda it: it[0].update(id=4),                 # id above range
    lambda it: it[1].update(id=1),                 # duplicate id
    lambda it: it.pop(),                           # missing id
    lambda it: it[0].update(score=3),              # score out of range
    lambda it: it[0].update(score=-3),
    lambda it: it[0].update(score=1.5),            # not an integer
    lambda it: it[0].update(score=float("nan")),
    lambda it: it[0].update(id=True),              # bool is not an id
    lambda it: it[0].update(lens="earnings"),      # not in the enum
])
def test_validation_rejects_bad_ids_and_ranges(mutate):
    raw = _payload([("financials", 1), ("outlook", 0), ("street", -1)])
    mutate(raw["items"])
    assert ns.validate_items(raw, 3) is None


def test_validation_rejects_non_objects():
    assert ns.validate_items(None, 2) is None
    assert ns.validate_items({"brief": "no items"}, 2) is None
    assert ns.validate_items({"items": ["x"], "brief": ""}, 1) is None


# ---------------------------------------------------------------- transport
def test_request_uses_schema_and_thinking_off(monkeypatch):
    calls = _install(monkeypatch, [_payload([("financials", 1)])])
    out = ns._nvidia_call("sys", "user", ns._ITEM_LENSES)
    assert out["items"][0]["lens"] == "financials"
    kw = calls[0]
    assert kw["model"] == ns._MODEL
    assert kw["response_format"]["type"] == "json_schema"
    assert kw["response_format"]["json_schema"]["strict"] is True
    assert kw["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert kw["messages"][0]["content"].startswith("/no_think\n")
    assert ns.llm_status()["ok"] is True


def test_410_is_model_retired_and_never_retried(monkeypatch):
    calls = _install(monkeypatch, [_HTTPError(410)] * 5)
    res = ns.read_headlines("sys", "user", 1)
    assert res is None
    assert len(calls) == 2                           # one per concurrent pass, no retries
    st = ns.llm_status()
    assert st["ok"] is False and "410 model retired" in st["error"] and st["permanent"]
    # later tickers in the same refresh are spared the call...
    assert ns.read_headlines("sys", "user", 1) is None and len(calls) == 2
    # ...but the next refresh tries again (a fixed key / restored model)
    ns.llm_unblock()
    ns.read_headlines("sys", "user", 1)
    assert len(calls) == 4


def test_404_is_treated_like_410(monkeypatch):
    calls = _install(monkeypatch, [_HTTPError(404)])
    assert ns._nvidia_call("sys", "user", ns._ITEM_LENSES) is None
    assert "404" in ns.llm_status()["error"] and len(calls) == 1


def test_503_is_retried_then_succeeds(monkeypatch):
    good = _payload([("street", 1)])
    calls = _install(monkeypatch, [_HTTPError(503), _HTTPError(503), good])
    out = ns._nvidia_call("sys", "user", ns._ITEM_LENSES)
    assert out == good and len(calls) == 3 and ns.llm_status()["ok"] is True


def test_503_storm_fails_without_tripping_the_permanent_block(monkeypatch):
    calls = _install(monkeypatch, [_HTTPError(503)] * 20)
    assert ns.read_headlines("sys", "user", 1) is None
    st = ns.llm_status()
    assert st["ok"] is False and "503" in st["error"] and not st["permanent"]
    # both passes were attempted (transient), each with its retries
    assert len(calls) == 2 * 2 * ns._MAX_RETRIES


def test_malformed_json_is_retried(monkeypatch):
    good = _payload([("outlook", -1)])
    _install(monkeypatch, ["{not json", good])
    assert ns._nvidia_call("sys", "user", ns._ITEM_LENSES) == good


def test_missing_key_is_reported(monkeypatch):
    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "")
    assert ns._nvidia_call("sys", "user", ns._ITEM_LENSES) is None
    st = ns.llm_status()
    assert st["ok"] is False and "key missing" in st["error"]


def test_two_passes_report_agreement(monkeypatch):
    p1 = _payload([("financials", 2), ("outlook", -1)])
    p2 = _payload([("financials", 2), ("outlook", 1)])
    calls = _install(monkeypatch, [p1, p2])
    items, brief, agreement = ns.read_headlines("sys", "user", 2)
    assert len(calls) == 2
    assert agreement == 0.5 and [i["score"] for i in items] == [2.0, 0.0]


# ---------------------------------------------------------------- per-ticker flow
def _arts(n=3):
    now = time.time()
    return [{"headline": f"Acme headline {i}", "summary": "", "source": "Reuters",
             "datetime": now - i * 3600, "url": f"https://x/{i}", "n_duplicates": 0,
             "aid": f"id{i}"} for i in range(n)]


def test_read_batch_prefers_headlines_about_the_company(monkeypatch):
    monkeypatch.setattr("portfolio_tracker.relevance.load_company_names",
                        lambda: {"NVDA": "NVIDIA CORP"})
    now = time.time()
    noise = [{"headline": f"Is stock {i} a buy before October?", "summary": "",
              "datetime": now - i * 60} for i in range(20)]
    named = [{"headline": f"Nvidia signs supply deal {i}", "summary": "",
              "datetime": now - 86400 * (2 + i)} for i in range(4)]
    batch = ns._read_batch(noise + named, "NVDA")
    assert len(batch) == ns._SCORE_BATCH
    assert all(a in batch for a in named)                      # older, but about NVDA
    assert batch == sorted(batch, key=lambda a: -a["datetime"])  # presented newest first
    assert noise[0] in batch and noise[-1] not in batch         # ties go to the newest


def test_assess_writes_items_both_engines_and_history(monkeypatch):
    arts = _arts(3)
    monkeypatch.setattr(ns, "fetch_company_news", lambda s, days=7, cancel=None: arts)
    monkeypatch.setattr(ns, "_market_read", lambda s, ctx, cancel=None: (
        {"sar": -0.1, "z": -1.9, "pct": 3.0, "tier": "very_bearish", "score": -0.004,
         "horizon_days": 1, "confidence": 0.6, "model_version": "mlsent-v1.1"}, 0.02))
    p = _payload([("financials", 2), ("outlook", 1), ("none", 0)])
    _install(monkeypatch, [p, p])
    res, outcome = ns._assess_symbol("ACME", {"row": {"price": 10.0}})
    assert outcome == "ok"
    news = res["news"]
    assert news["tier"] in ("bullish", "very_bullish") and news["agreement"] == 1.0
    assert news["n_none"] == 1 and news["lenses"]["financials"]["ids"] == ["id0"]
    assert arts[0]["lens"] == "financials" and arts[0]["llm_score"] == 2.0
    assert res["divergence"]["kind"] == "good_news_weak_reaction"
    rec = ns._history_load()[-1]
    assert rec["symbol"] == "ACME" and rec["news_tier"] == news["tier"]
    assert rec["market_tier"] == "very_bearish" and rec["lens"]["financials"] == 2.0
    assert rec["market_score"] == -0.004 and rec["market_model"] == "mlsent-v1.1"
    assert "s_idio" not in rec and "ml_sar" not in rec


def test_market_history_is_the_running_models_recent_scores(monkeypatch):
    from portfolio_tracker import ml_sentiment as _ml

    today = datetime.now(timezone.utc).date()
    day = lambda n: (today - timedelta(days=n)).isoformat()  # noqa: E731
    recs = [
        {"date": day(1), "symbol": "A", "market_score": 0.1, "market_model": "m1"},
        {"date": day(0), "symbol": "B", "market_score": 0.2, "market_model": "m1"},
        {"date": day(0), "symbol": "ACME", "market_score": 0.3, "market_model": "m1"},
        {"date": day(2), "symbol": "C", "market_score": 0.4, "market_model": "m0"},
        {"date": day(_ml.LIVE_WINDOW_DAYS + 5), "symbol": "D", "market_score": 0.5,
         "market_model": "m1"},
        {"date": day(1), "symbol": "E", "ml_sar": 0.6},
    ]
    monkeypatch.setattr(ns, "_history_load", lambda: recs)
    assert sorted(ns._market_history("m1", (day(0), "ACME"))) == [0.1, 0.2]


def test_failed_refresh_keeps_the_headlines_the_stale_read_was_made_from(monkeypatch):
    read = _arts(2)
    for a in read:
        a.update(lens="financials", llm_score=1.0, fact="f")
    ns._cache_put(ns._NEWS_CACHE, "news|ACME|7", read, ns._NEWS_TTL)
    ns._SENTIMENT_CACHE["sentiment|ACME|7"] = (time.time(), ns._SENTIMENT_TTL, {
        "news": {"tier": "bullish", "score": 1.0, "lenses": {}, "brief": "old",
                 "assessed_at": "2026-08-07T09:00:00+00:00"}, "market": None})
    fresh = _arts(3)                                     # re-fetched, not yet read

    def fetch(s, days=7, cancel=None):
        ns._cache_put(ns._NEWS_CACHE, f"news|{s}|{days}", fresh, ns._NEWS_TTL)
        return fresh
    monkeypatch.setattr(ns, "fetch_company_news", fetch)
    monkeypatch.setattr(ns, "_market_read", lambda s, ctx, cancel=None: (None, None))
    _install(monkeypatch, [_HTTPError(410), _HTTPError(410)])
    res, outcome = ns._refresh_symbol_sentiment("ACME", context={})
    assert outcome == "failed" and res["news"]["stale"] is True
    cached = ns._cache_get(ns._NEWS_CACHE, "news|ACME|7")
    assert cached is read and all(a["lens"] == "financials" for a in cached)


def test_failed_read_keeps_previous_news_marked_stale(monkeypatch):
    arts = _arts(2)
    prev = {"news": {"tier": "bullish", "score": 1.0, "lenses": {}, "brief": "old",
                     "assessed_at": "2026-08-07T09:00:00+00:00"},
            "market": None, "divergence": None, "article_count": 2,
            "assessed_at": "2026-08-07T09:00:00+00:00"}
    ns._SENTIMENT_CACHE["sentiment|ACME|7"] = (time.time(), ns._SENTIMENT_TTL, prev)
    monkeypatch.setattr(ns, "fetch_company_news", lambda s, days=7, cancel=None: arts)
    monkeypatch.setattr(ns, "_market_read", lambda s, ctx, cancel=None: (None, None))
    _install(monkeypatch, [_HTTPError(410), _HTTPError(410)])      # one per pass
    res, outcome = ns._assess_symbol("ACME", {})
    assert outcome == "failed"
    assert res["news"]["stale"] is True and "410" in res["news"]["stale_reason"]
    assert res["news"]["assessed_at"] == "2026-08-07T09:00:00+00:00"   # never re-dated
    assert res["divergence"] is None
    assert ns._history_load() == []                  # nothing new to record


def test_refresh_reports_scored_failed_and_empty(monkeypatch):
    outcomes = {"A": ({"news": {}}, "ok"), "B": ({"news": None}, "failed"),
                "C": (None, "empty")}
    monkeypatch.setattr(ns, "_refresh_market_sentiment",
                        lambda days=7, stage_cb=None, cancel=None: ({"news": {}}, "ok"))
    monkeypatch.setattr(ns, "_refresh_symbol_sentiment",
                        lambda s, context=None, stage_cb=None, cancel=None: outcomes[s])
    from portfolio_tracker import ml_sentiment as _ml
    monkeypatch.setattr(_ml, "load_closes", lambda symbols: None)     # no network
    events = []
    out = ns.refresh_sentiment(["A", "B", "C"], progress_cb=lambda k, b: events.append((k, b)))
    st = out["status"]
    assert (st["scored"], st["failed"], st["empty"], st["total"]) == (1, 1, 1, 3)
    assert st["market_ok"] is True
    done = {b["symbol"]: b["outcome"] for k, b in events if k == "symbol"}
    assert done == {"A": "ok", "B": "failed", "C": "empty"}


def test_status_carries_the_llm_state(monkeypatch):
    _install(monkeypatch, [_HTTPError(410)])
    ns._nvidia_call("sys", "user", ns._ITEM_LENSES)
    st = ns.status()
    assert st["llm_model"] == ns._MODEL and st["llm_ok"] is False
    assert "retired" in st["llm_error"]


def test_status_reports_backoff_windows(monkeypatch):
    monkeypatch.setattr(ns, "_fh_rate_limit_until", time.time() + 9.2)
    monkeypatch.setattr(ns, "_nv_rate_limit_until", time.time() + 4.4)
    status = ns.status()
    assert status["finnhub_backoff_s"] >= 9
    assert status["nvidia_backoff_s"] >= 4
    assert status["finnhub_calls_limit"] == ns._FH_LIMITER.max
    assert status["nvidia_calls_limit"] == ns._NV_LIMITER.max


# ---------------------------------------------------------------- divergence
def _news(tier, lenses=None, score=1.0):
    return {"tier": tier, "score": score, "lenses": lenses or {}}


def test_divergence_good_news_weak_reaction():
    news = _news("bullish", {"financials": {"score": 2.0}, "outlook": {"score": -1.0}})
    d = ns.compute_divergence(news, {"tier": "bearish", "z": -1.2})
    assert d["kind"] == "good_news_weak_reaction"
    assert d["text"].startswith("Financials +2.0, Outlook −1.0")


def test_divergence_reverse():
    d = ns.compute_divergence(_news("very_bearish", score=-1.5),
                              {"tier": "very_bullish", "z": 2.0})
    assert d["kind"] == "weak_news_strong_reaction"


def test_divergence_sold_the_news():
    d = ns.compute_divergence(_news("bullish", {"financials": {"score": 2.0}}),
                              {"tier": "no_edge"}, {"pct_1d": -19.0}, vol_20d=0.03)
    assert d["kind"] == "sold_the_news"
    assert d["text"] == "Financials +2.0; stock −19.0% on the day"


def test_no_divergence_when_engines_agree_or_move_is_normal():
    assert ns.compute_divergence(_news("bullish"), {"tier": "bullish"}) is None
    assert ns.compute_divergence(_news("bullish"), {"tier": "no_edge"},
                                 {"pct_1d": -3.0}, vol_20d=0.03) is None
    assert ns.compute_divergence(None, {"tier": "bearish"}) is None


# ---------------------------------------------------------------- persistence / dedup
def test_persisted_old_shape_sentiment_is_ignored(tmp_path, monkeypatch):
    f = tmp_path / "news.json"
    f.write_text(json.dumps({"version": 2, "news": {}, "sentiment": {
        "sentiment|OLD|7": {"value": {"tier": "bearish", "s_total": -0.2}},
        "sentiment|NEW|7": {"value": {"news": {"tier": "bullish"}, "market": None}}}}))
    monkeypatch.setattr(ns, "_PERSIST_FILE", f)
    ns._load_persisted_caches()
    assert "sentiment|OLD|7" not in ns._SENTIMENT_CACHE
    assert "sentiment|NEW|7" in ns._SENTIMENT_CACHE


def test_llm_status_survives_a_restart(tmp_path, monkeypatch):
    _install(monkeypatch, [_HTTPError(410)])
    ns._nvidia_call("sys", "user", ns._ITEM_LENSES)
    f = tmp_path / "news.json"
    monkeypatch.setattr(ns, "_PERSIST_FILE", f)
    ns._save_persisted_caches()
    ns._LLM_STATUS.update({"ok": None, "error": None})
    ns._load_persisted_caches()
    assert ns.llm_status()["ok"] is False and "410" in ns.llm_status()["error"]


def test_dedup_collapses_syndicated_copies_and_ids_articles():
    now = time.time()
    arts = [
        {"headline": "Acme Corp raises full-year guidance", "datetime": now - 100},
        {"headline": "Acme Corp Raises Full-Year Guidance", "datetime": now - 50},
        {"headline": "Completely different story about Acme lawsuit", "datetime": now},
    ]
    out = ns._dedup_articles(arts)
    assert len(out) == 2
    guid = [a for a in out if "guidance" in a["headline"].lower()][0]
    assert guid["n_duplicates"] == 1
    assert guid["datetime"] == now - 100          # earliest copy kept as canonical
    assert len({a["aid"] for a in out}) == 2


def test_get_cached_articles_spans_window_across_many_symbols():
    # A multi-symbol portfolio publishes hundreds of articles a day, so a flat
    # newest-N truncation of the merged feed collapsed the timeline into the
    # last few hours though each symbol's cache spanned the whole week.
    now = time.time()
    syms = [f"S{i}" for i in range(15)]
    for s in syms:
        arts = [{"headline": f"{s}-{j}", "url": f"{s}/{j}",
                 "datetime": now - j * (7 * 86400 / 40)} for j in range(40)]
        ns._NEWS_CACHE[f"news|{s}|7"] = (now, ns._NEWS_TTL, arts)
    out = ns.get_cached_articles(syms, limit=250)
    assert len(out) <= 250
    span_days = (out[0]["datetime"] - out[-1]["datetime"]) / 86400.0
    assert span_days > 5.0, f"timeline collapsed to {span_days:.1f}d instead of spanning the week"
    assert out[0]["datetime"] >= out[-1]["datetime"]


def test_cached_reads_never_fetch(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("network touched")
    monkeypatch.setattr(ns, "_fh_call", _boom)
    monkeypatch.setattr(ns, "_nvidia_call", _boom)
    val = {"news": {"tier": "bullish"}, "market": None}
    ns._SENTIMENT_CACHE["sentiment|ACME|14"] = (time.time(), ns._SENTIMENT_TTL, val)
    assert ns.get_cached_sentiments(["ACME", "NONE"], days=14) == {"ACME": val, "NONE": None}
    assert ns.get_cached_sentiment("ACME") == val     # sweeps other windows
    assert ns.get_cached_market() is None
