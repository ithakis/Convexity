"""search.py — rule parser, LLM step, executor. Yahoo and NIM are stubbed;
the symbol pack is the synthetic one from test_symbol_db."""

import pytest

from convexity import news_sentiment as ns, search as s
from tests.test_news_sentiment import _HTTPError, _install
from tests.test_symbol_db import install

TEXT = "best tech companies with D/E <.8 and current ratio greater than 1."


@pytest.fixture(autouse=True)
def pack(monkeypatch):
    install()
    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "")
    s._CACHE.clear()
    s._INFO.clear()
    yield


@pytest.fixture()
def yahoo(monkeypatch):
    """Stub Yahoo: yf.screen records its query and returns `quotes`;
    Ticker.info comes from `info`."""
    import yfinance as yf

    st = {"calls": [], "quotes": [], "info": {}}

    def screen(query, size, offset, sortField, sortAsc):
        st["calls"].append({"query": query.to_dict(), "sort": sortField, "asc": sortAsc})
        return {"total": len(st["quotes"]), "quotes": st["quotes"] if offset == 0 else []}

    monkeypatch.setattr(yf, "screen", screen)
    monkeypatch.setattr(
        s, "_yahoo_screen", lambda q, off, sort, asc: yf.screen(q, 250, off, sort, asc)
    )
    monkeypatch.setattr(s, "_info", lambda t: st["info"].get(t, {}))
    return st


def _flat(d):
    """Every [field, value] operand in a screener query dict."""
    op = d["operator"].lower()
    if op in ("and", "or"):
        return [x for o in d["operands"] for x in _flat(o)]
    return [(op, *d["operands"])]


# ------------------------------------------------------------------ rules
def test_the_issue_example_parses_and_sends_yahoo_its_own_units(yahoo):
    q, left = s.parse_rules(TEXT)
    assert q["filters"] == [
        {"field": "de", "op": "lt", "value": 0.8},
        {"field": "current_ratio", "op": "gt", "value": 1.0},
    ]
    assert q["sectors"] == ["Technology"] and left == []
    assert q["rank"] == {"field": "mcap", "dir": "desc", "by": "default"}
    yahoo["quotes"] = [
        {"symbol": t} for t in ("MSF.DE", "SMSI", "TSM", "NVDA", "MSFT", "UNKNOWN.X")
    ]
    yahoo["info"] = {"NVDA": {"debtToEquity": 11.0, "currentRatio": 4.2}}
    out = s.search(TEXT)
    ops = _flat(yahoo["calls"][0]["query"])
    assert ("lt", "totaldebtequity.lasttwelvemonths", 80.0) in ops  # percent on Yahoo's side
    assert ("gt", "currentratio.lasttwelvemonths", 1.0) in ops
    assert ("eq", "sector", "Technology") in ops
    assert yahoo["calls"][0]["sort"] == "intradaymarketcap"
    # home listings once each (MSF.DE -> MSFT, TSM -> 2330.TW), ranked by USD cap
    assert [c["ticker"] for c in out["results"]] == ["NVDA", "MSFT", "2330.TW", "SMSI"]
    assert out["results"][0]["values"] == {"de": 0.11, "current_ratio": 4.2, "perf_52w": None}
    assert any("NVIDIA key" in n for n in out["notes"])


@pytest.mark.parametrize(
    "text, filters",
    [
        ("tech with ROE > 0.2", [{"field": "roe", "op": "gt", "value": 20.0}]),
        ("banks with D/E under 80%", [{"field": "de", "op": "lt", "value": 0.8}]),
        ("market cap over $20B", [{"field": "mcap", "op": "gt", "value": 2e10}]),
        ("P/E at most 15", [{"field": "pe", "op": "lte", "value": 15.0}]),
    ],
)
def test_units_follow_the_field(text, filters):
    assert s.parse_rules(text)[0]["filters"] == filters


def test_an_unsupported_phrase_is_kept_and_warned_about(yahoo):
    out = s.search("tech companies with D/E < 0.8, founded before 1990")
    assert out["query"]["ignored"] == ["founded before 1990"]
    assert {"kind": "ignored", "label": "founded before 1990"} in out["chips"]
    assert any("founded before 1990" in w for w in out["warnings"])


# ------------------------------------------------------------------ offline paths
def test_names_and_categories_never_call_yahoo(yahoo):
    assert s.search("microsft")["results"][0]["ticker"] == "MSFT"
    out = s.search("european banks")
    assert [c["ticker"] for c in out["results"]] == ["HSBA.L", "BNP.PA", "DBK.DE"]
    assert {"kind": "regions", "label": "Region: Europe"} in out["chips"]
    # A company shows its most traded listing (NVO), its home and country
    # kept; NOV.DE trades too little to list. A typed ticker stays as typed.
    nvo = s.search("novo nordisk")["results"][0]
    assert (nvo["ticker"], nvo["exchange_name"], nvo["region"]) == ("NVO", "NYSE", "dk")
    assert [a["ticker"] for a in nvo["alternates"]] == ["NOVO-B.CO"]
    assert s.search("NOVO-B.CO")["results"][0]["ticker"] == "NOVO-B.CO"
    assert yahoo["calls"] == []


def test_regions_apply_to_the_home_listing(yahoo):
    # Yahoo's region filter matches MSF.DE (a German line of Microsoft); the
    # company is American, so a European screen drops it.
    yahoo["quotes"] = [{"symbol": "MSF.DE"}, {"symbol": "DBK.DE"}]
    out = s.search("european companies with P/E under 30")
    assert [c["ticker"] for c in out["results"]] == ["DBK.DE"]


def test_paging_reuses_the_cached_result(yahoo):
    out = s.search("tech companies")
    assert out["total"] >= 5 and len(out["results"]) == 5
    nxt = s.search("tech companies", offset=5)
    assert nxt["offset"] == 5 and {c["ticker"] for c in nxt["results"]}.isdisjoint(
        c["ticker"] for c in out["results"]
    )


# ------------------------------------------------------------------ chip edits
def test_an_edited_query_is_validated_and_runs_without_the_llm(yahoo, monkeypatch):
    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "k")
    monkeypatch.setattr(s, "parse_llm", lambda *a: pytest.fail("LLM called for a chip edit"))
    q = {
        "text": "x",
        "kind": "screen",
        "filters": [
            {"field": "pe", "op": "lt", "value": 30},
            {"field": "founded", "op": "lt", "value": 1990},
            {"field": "pe", "op": "drop table", "value": 1},
            {"field": "pe", "op": "gt", "value": float("nan")},
        ],
        "sectors": ["Technology", "Crypto"],
        "rank": {"field": "roe", "dir": "desc", "by": "ai"},
    }
    out = s.search(query=q)
    assert out["query"]["filters"] == [{"field": "pe", "op": "lt", "value": 30.0}]
    assert out["query"]["sectors"] == ["Technology"] and "Crypto" in out["query"]["ignored"]
    assert len(out["query"]["ignored"]) == 4
    assert yahoo["calls"][0]["sort"] == "returnonequity.lasttwelvemonths"


# ------------------------------------------------------------------ the AI step
def _ai(monkeypatch, reply):
    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "k")
    calls = []

    def fake(system, user, lens, cancel=None, **kw):
        calls.append(kw)
        return reply

    monkeypatch.setattr(ns, "_nvidia_call", fake)
    return calls


def test_rules_first_no_llm_for_a_plain_name_or_a_full_parse(monkeypatch, yahoo):
    calls = _ai(monkeypatch, None)
    s.search("microsoft")
    s.search("european banks")
    assert calls == []


def test_a_theme_goes_to_the_llm_and_its_picks_are_verified(monkeypatch, yahoo):
    reply = {
        "kind": "theme",
        "filters": [],
        "sectors": [],
        "industries": ["Drug Manufacturers—General"],
        "regions": [],
        "types": [],
        "rank": {"field": "mcap", "dir": "desc", "why": ""},
        "picks": [
            {"ticker": "LLY", "name": "Eli Lilly", "why": "Tirzepatide"},
            {"ticker": "NVO", "name": "Novo Nordisk", "why": "Semaglutide"},
            {"ticker": "ZZZZ", "name": "Made Up Pharma", "why": "hallucinated"},
            {"ticker": "MSFT", "name": "Microsoft", "why": "off theme"},  # Software
        ],
        "ignored": [],
    }
    calls = _ai(monkeypatch, reply)
    out = s.search("GLP-1 drug makers")
    eu = s.search("", query={**out["query"], "regions": ["dk"]})  # a chip edit: home region
    assert [c["ticker"] for c in eu["results"]] == ["NVO"]  # a Danish company, its US line
    assert calls and calls[0]["record"] is False and calls[0]["tag"] == "search"
    assert "de" in str(calls[0]["schema"])  # the field enum is the registry
    assert out["query"]["engine"] == "ai" and out["query"]["kind"] == "theme"
    assert [(c["ticker"], c["why"]) for c in out["results"]] == [
        ("LLY", "Tirzepatide"),
        ("NVO", "Semaglutide"),  # shown as its most traded listing
    ]
    assert any("1 AI-named company not found" in w for w in out["warnings"])
    assert any("outside the theme's sectors and industries: MSFT" in w for w in out["warnings"])


def test_best_lets_the_llm_choose_the_ranking(monkeypatch, yahoo):
    q, _ = s.parse_rules(TEXT)
    reply = {
        **q,
        "rank": {"field": "roe", "dir": "desc", "why": "profitability"},
        "picks": [],
        "kind": "screen",
    }
    _ai(monkeypatch, reply)
    out = s.search(TEXT)
    assert out["query"]["rank"] == {
        "field": "roe",
        "dir": "desc",
        "by": "ai",
        "why": "profitability",
    }
    rank = next(c for c in out["chips"] if c["kind"] == "rank")
    assert rank["label"] == "Rank: ROE" and rank["ai"] and rank["title"].startswith("profitability")
    assert not any("Ranked" in n for n in out["notes"])  # the chip says it
    assert yahoo["calls"][0]["sort"] == "returnonequity.lasttwelvemonths"


def test_a_failed_llm_falls_back_to_the_rules(monkeypatch, yahoo):
    _ai(monkeypatch, None)
    out = s.search("tech companies with D/E < 0.8, founded before 1990")
    assert out["query"]["engine"] == "rules" and "founded before 1990" in out["query"]["ignored"]
    assert any("AI step failed" in n for n in out["notes"])


def test_search_calls_never_touch_the_news_llm_banner(monkeypatch):
    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "k")
    _install(monkeypatch, [_HTTPError(401)])
    before = ns.llm_status()
    assert (
        ns._nvidia_call("s", "u", (), schema={"type": "object"}, record=False, tag="search") is None
    )
    assert ns.llm_status() == before


# ------------------------------------------------------------------ the route
def test_api_search_round_trip(yahoo):
    import http.server
    import json
    import threading
    import urllib.request

    from convexity import server

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/search"

    def post(body):
        req = urllib.request.Request(
            url, json.dumps(body).encode(), {"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    try:
        assert post({"q": "microsft"})["results"][0]["ticker"] == "MSFT"
        edited = post({"q": "european banks"})["query"]
        edited["regions"] = ["fr"]
        assert [c["ticker"] for c in post({"query": edited})["results"]] == ["BNP.PA"]
    finally:
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------------------ review fixes
@pytest.mark.parametrize("text", ["deutsche bank", "jp morgan chase"])
def test_a_company_name_with_category_words_is_a_name_search(yahoo, text):
    out = s.search(text)
    assert out["query"]["kind"] == "name" and yahoo["calls"] == []
    assert out["results"][0]["ticker"] in ("DBK.DE", "JPM")


@pytest.mark.parametrize(
    "text, value",
    [
        ("tech with ROE > 1", 1.0),
        ("tech with ROE > 0.15", 15.0),
        ("banks with dividend yield > 0.5", 0.5),
    ],
)
def test_fractions_become_percent_only_where_that_is_meant(text, value):
    assert s.parse_rules(text)[0]["filters"][0]["value"] == pytest.approx(value)


def test_ai_is_skipped_while_nim_is_rate_limited(monkeypatch, yahoo):
    import time

    calls = _ai(monkeypatch, {"kind": "screen"})
    monkeypatch.setattr(ns, "_nv_rate_limit_until", time.time() + 60)
    out = s.search("tech companies founded before 1990")
    assert calls == [] and "founded before 1990" in out["query"]["ignored"]


def test_an_ai_theme_carries_no_forced_rank(monkeypatch, yahoo):
    reply = {"kind": "theme", "filters": [], "sectors": [], "industries": [], "regions": [],
             "types": [], "rank": {"field": "roe", "dir": "desc", "why": "x"},
             "picks": [{"ticker": "LLY", "name": "Eli Lilly", "why": "GLP-1"}], "ignored": []}  # fmt: skip
    _ai(monkeypatch, reply)
    out = s.search("GLP-1 drug makers")
    assert out["query"]["rank"] is None and not any(c["kind"] == "rank" for c in out["chips"])


def test_an_oversized_or_non_finite_edit_is_capped():
    q = s.validate({"filters": [{"field": "pe", "op": "lt", "value": float("inf")}] * 100})
    assert q["filters"] == [] and len(q["ignored"]) <= 50


def test_name_results_drop_far_weaker_matches(yahoo):
    assert [c["ticker"] for c in s.search("microsoft")["results"]] == ["MSFT"]


# ------------------------------------------------------------------ pasted lists
def test_a_pasted_list_resolves_each_part_and_names_the_misses(yahoo):
    out = s.search("NVO, microsft\nZZZZ")
    assert out["query"]["kind"] == "list" and out["query"]["engine"] == "rules"
    # An exact ticker keeps that listing (NVO, not the home NOVO-B.CO).
    assert [c["ticker"] for c in out["results"]] == ["NVO", "MSFT"]
    assert out["warnings"] == ["Not found: ZZZZ."]
    assert yahoo["calls"] == []


def test_criteria_with_commas_are_not_a_list(yahoo):
    assert s.parse("tech companies with D/E < 0.8, current ratio > 1")["kind"] == "screen"


# ------------------------------------------------------------------ screener session
def test_the_own_session_remints_a_stale_crumb(monkeypatch):
    """A 401 on the search's own session mints a new cookie + crumb and retries."""
    from yfinance import EquityQuery as Q, _http

    class Resp:
        def __init__(self, code, text="", data=None):
            self.status_code, self.text, self._data = code, text, data

        def json(self):
            return self._data

        def raise_for_status(self):
            assert self.status_code == 200

    log = []

    class Sess:
        def get(self, url, **kw):
            log.append(url)
            return Resp(200, f"crumb{len(log)}")

        def post(self, url, params, data, headers, timeout):
            log.append(params["crumb"])
            assert "Banks—Regional".encode() in data  # raw UTF-8, never \\u2014
            if params["crumb"] == "stale":
                return Resp(401)
            return Resp(200, data={"finance": {"result": [{"total": 1, "quotes": []}]}})

    monkeypatch.setattr(_http, "new_session", Sess)
    monkeypatch.setattr(s.time, "sleep", lambda _s: None)
    monkeypatch.setitem(s._SESSION, "s", Sess())
    monkeypatch.setitem(s._SESSION, "crumb", "stale")
    out = s._yahoo_screen(Q("eq", ["industry", "Banks—Regional"]), 0, "intradaymarketcap", False)
    assert out["total"] == 1 and log[0] == "stale" and "getcrumb" in log[2]
    assert s._SESSION["crumb"] not in (None, "stale")


def test_a_refused_own_session_falls_back_to_yfinance(yahoo, monkeypatch):
    def refused(*a):
        raise RuntimeError("no crumb")

    monkeypatch.setattr(s, "_yahoo_screen", refused)
    yahoo["quotes"] = [{"symbol": "MSFT"}]
    out = s.search("tech companies with D/E < 0.8")
    assert [c["ticker"] for c in out["results"]] == ["MSFT"] and not out["warnings"]


def test_a_ratio_ranking_skips_microcaps_unless_asked(yahoo):
    # Yahoo sorts by ROE: the microcap first. Without a cap filter of the
    # user's own, the ranking is among companies above $1B (pack USD caps).
    yahoo["quotes"] = [{"symbol": t} for t in ("SMSI", "MSFT", "NVDA", "AAPL", "GOOGL", "2330.TW")]
    q = {**s.empty("x"), "kind": "screen", "sectors": ["Technology"],
         "filters": [{"field": "de", "op": "lt", "value": 1.0}],
         "rank": {"field": "roe", "dir": "desc", "by": "ai"}}  # fmt: skip
    out = s.search("", query=q)
    assert [c["ticker"] for c in out["results"]] == ["MSFT", "NVDA", "AAPL", "GOOGL", "2330.TW"]
    tip = lambda o: next(c for c in o["chips"] if c["kind"] == "rank")["title"]  # noqa: E731
    assert "above $1B" in tip(out)
    q["filters"].append({"field": "mcap", "op": "gt", "value": 1e6})
    assert "above $1B" not in tip(s.search("", query=q))


def test_a_lowest_first_ranking_excludes_negative_values(yahoo):
    q = {**s.empty("x"), "kind": "screen", "sectors": ["Technology"],
         "rank": {"field": "de", "dir": "asc", "by": "ai"}}  # fmt: skip
    s.search("", query=q)
    ops = _flat(yahoo["calls"][0]["query"])
    assert ("gte", "totaldebtequity.lasttwelvemonths", 0) in ops


# ------------------------------------------------------------------ smarter reading
def test_size_exchange_and_vague_amounts_parse_without_the_ai(monkeypatch, yahoo):
    """The query that went wrong live: "big" is a market-cap ranking, Nasdaq an
    exchange (not Region US), and "small leverage" a moderate filter."""
    calls = _ai(monkeypatch, None)
    q, left = s.parse_rules("big tech in nasdaq with small relative small leverage")
    assert left == [] and q["sectors"] == ["Technology"] and q["exchanges"] == ["Nasdaq"]
    assert q["filters"] == [{"field": "de", "op": "lt", "value": 0.5}]
    assert q["rank"] == {"field": "mcap", "dir": "desc", "by": "rules"} and not q["regions"]
    out = s.search("big tech in nasdaq with small relative small leverage")
    assert calls == [] and {"kind": "exchange", "label": "Exchange: Nasdaq"} in out["chips"]
    ops = _flat(yahoo["calls"][0]["query"])
    assert ("eq", "exchange", "NMS") in ops or any(o[1:] == ("exchange", "NMS") for o in ops)


@pytest.mark.parametrize(
    "text, rank",
    [
        ("tech with small leverage", None),  # a constraint, not the ranking
        ("tech with the lowest leverage", "de"),  # an extreme was asked for
        ("large tech with small leverage", "mcap"),  # size wins
    ],
)
def test_the_ai_ranking_is_kept_sane(monkeypatch, yahoo, text, rank):
    reply = {**s.empty(), "kind": "screen", "sectors": ["Technology"],
             "filters": [{"field": "de", "op": "lt", "value": 0.5}],
             "rank": {"field": "de", "dir": "asc", "why": "low leverage"}}  # fmt: skip
    _ai(monkeypatch, reply)
    q = s.parse_llm(text, s.empty(text))
    assert (q["rank"] or {}).get("field") == rank


@pytest.mark.parametrize(
    "text, industries",
    [
        ("uranium miners", ["Uranium"]),
        (
            "european banks and semiconductors",
            ["Banks—Diversified", "Banks—Regional", "Semiconductors"],
        ),
    ],
)
def test_an_exact_industry_is_not_widened_by_a_generic_word(text, industries):
    assert sorted(s.parse_rules(text)[0]["industries"]) == industries


def test_a_return_ranking_needs_a_profit(yahoo):
    q = {**s.empty("x"), "kind": "screen", "sectors": ["Technology"],
         "rank": {"field": "roe", "dir": "desc", "by": "ai"}}  # fmt: skip
    s.search("", query=q)
    assert ("gt", "netincomemargin.lasttwelvemonths", 0) in _flat(yahoo["calls"][0]["query"])
