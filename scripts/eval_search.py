"""Live check of how company search *reads* requests (search.parse: rules,
then the NVIDIA model when needed). Each case states what a sensible reading
must satisfy; the script prints pass/fail per case and the query it got.

Needs NVIDIA_API_KEY in the environment (CLAUDE.md §3: pass it from the real
config.json, never print it) and a temp CONVEXITY_HOME with the symbol pack
installed. Re-run after any change to the prompt, the rules or the model:

    CONVEXITY_HOME=<temp home with pack> NVIDIA_API_KEY=... uv run python scripts/eval_search.py
"""

import sys

from convexity import search as s

EU = set(s._EUROPE.split())


def f(q, field, op=None):
    return [x for x in q["filters"] if x["field"] == field and (op is None or x["op"] == op)]


def rank(q):
    return (q["rank"] or {}).get("field"), (q["rank"] or {}).get("dir")


CASES = [
    ("big tech in nasdaq with small relative small leverage",
     lambda q: q["sectors"] == ["Technology"] and q["exchanges"] == ["Nasdaq"]
     and f(q, "de", "lt") and f(q, "de")[0]["value"] >= 0.3 and rank(q) == ("mcap", "desc")),
    ("best tech companies with D/E <.8 and current ratio greater than 1",
     lambda q: f(q, "de", "lt") and f(q, "current_ratio", "gt")
     and rank(q)[0] not in ("de", "current_ratio", "mcap", None)),
    ("tech with small leverage", lambda q: f(q, "de", "lt") and rank(q)[0] != "de"),
    ("cheap european banks",
     lambda q: set(q["regions"]) >= EU and any("Banks" in i for i in q["industries"])
     and (f(q, "pe", "lt") or rank(q) == ("pe", "asc"))),
    ("the cheapest european banks by P/E", lambda q: rank(q) == ("pe", "asc")),
    ("best dividend payers in utilities",
     lambda q: q["sectors"] == ["Utilities"] and rank(q) == ("dividend_yield", "desc")),
    ("profitable small caps with strong revenue growth",
     lambda q: f(q, "mcap", "lt") and f(q, "revenue_growth", "gt")),
    ("low debt japanese industrials",
     lambda q: q["regions"] == ["jp"] and q["sectors"] == ["Industrials"] and f(q, "de", "lt")),
    ("software companies with the highest ROE",
     lambda q: any("Software" in i for i in q["industries"]) and rank(q) == ("roe", "desc")),
    ("largest semiconductor companies",
     lambda q: any("Semiconductor" in i for i in q["industries"]) and rank(q) == ("mcap", "desc")),
    ("undervalued healthcare with low P/E",
     lambda q: q["sectors"] == ["Healthcare"] and f(q, "pe", "lt") and f(q, "pe")[0]["value"] >= 10),
    ("high growth US biotech",
     lambda q: q["regions"] == ["us"] and "Biotechnology" in q["industries"]
     and (f(q, "revenue_growth", "gt") or rank(q)[0] == "revenue_growth")),
    ("stable low beta consumer staples",
     lambda q: q["sectors"] == ["Consumer Defensive"] and f(q, "beta", "lt")),
    ("companies with net margin above 20% and revenue growth over 10%",
     lambda q: f(q, "net_margin", "gt") and f(q, "net_margin")[0]["value"] == 20
     and f(q, "revenue_growth", "gt") and f(q, "revenue_growth")[0]["value"] == 10),
    ("nyse energy giants",
     lambda q: q["exchanges"] == ["NYSE"] and q["sectors"] == ["Energy"] and rank(q) == ("mcap", "desc")),
    ("GLP-1 drug makers",
     lambda q: q["kind"] == "theme" and {p["ticker"] for p in q["picks"]} & {"LLY", "NVO", "NOVO-B.CO"}),
    ("uranium miners",
     lambda q: q["kind"] in ("theme", "screen") and (q["picks"] or q["industries"])),
    ("companies making AI chips",
     lambda q: q["picks"] or any("Semiconductor" in i for i in q["industries"])),
    ("high dividend european telecoms",
     lambda q: set(q["regions"]) >= EU and f(q, "dividend_yield", "gt")),
    ("microsoft", lambda q: q["kind"] == "name"),
    # Two words, no company called that: a theme, not The TJX Companies.
    ("cybersecurity companies",
     lambda q: q["kind"] in ("theme", "screen")
     and ({p["ticker"] for p in q["picks"]} & {"CRWD", "PANW", "FTNT", "ZS", "S", "OKTA", "CHKP"}
          or any("Software" in i for i in q["industries"]))),
    ("AAPL MSFT NVDA ASML.AS", lambda q: q["kind"] == "list"),
]  # fmt: skip


def main() -> int:
    if not s.ai_available():
        print("NVIDIA_API_KEY is not set: only the rule parser would run.")
        return 2
    passed = 0
    for text, ok in CASES:
        q = s.parse(text)
        good = bool(ok(q))
        passed += good
        print(f"{'PASS' if good else 'FAIL'} [{q['engine']:5}] {text}\n       {s._brief(q) or q['kind']}"
              f"{' picks=' + ','.join(p['ticker'] for p in q['picks']) if q['picks'] else ''}")  # fmt: skip
    print(f"\n{passed}/{len(CASES)} passed")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
