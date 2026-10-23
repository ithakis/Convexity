"""Company search: one query object, two parsers, one executor.

    text -> parse_rules() ----------------------------> Query
              (leftover words, a theme, or "best")   ^
              '-> parse_llm()  (NVIDIA NIM) ---------'
    Query -> run() -> {query, results, warnings}

The Query is the audit trail: the frontend shows it as chips before any
result, the user can edit a chip and re-run it (`run(validate(q))`, no LLM),
and every search prints one `[search]` line with the text, the engine and
the query it ran.

  kind      "name" (fuzzy name/ticker), "screen" (criteria and/or sector,
            industry, region), or "theme" (named picks from the LLM)
  filters   [{field, op, value}] — `field` is a FIELDS key, `value` in the
            user's units (D/E 0.8, ROE 15 meaning 15%)
  sectors, industries, regions, types — screener vocabulary
  rank      {field, dir, by} — FIELDS key or "mcap"; `by` is rules/ai/default
  picks     [{ticker, name, why}] — LLM-named companies, each verified
            against the symbol pack before it is shown
  ignored   phrases nothing could use: shown struck through + a warning

Where it runs. Names and pure sector/industry/region screens run offline on
the symbol pack (symbol_db.py). Criteria go to Yahoo's screener (~90 fields,
59 regions), which filters server-side but returns neither the values nor a
comparable market cap (it is in the listing's currency), so: market-cap
filters and ranking are applied here in USD, and the cards' values come
from `Ticker.info` for the five shown. Units differ between the two Yahoo
sources — see FIELDS — and are converted at exactly one place each.
"""

import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import asdict, replace

from convexity import resolver
from convexity import symbol_db as sdb
from convexity.helpers import Deadline

# key: (label, unit, screener field, user -> screener factor, .info key,
#       .info -> user factor, aliases). Units verified against live data
# (2026-10): the screener stores D/E, ROE, margins and growth in percent;
# .info stores D/E and dividend yield in percent but ROE and margins as
# fractions. Growth has no matching .info figure (it is quarterly there), so
# its cards show a tick, not a number from another definition.
# fmt: off
FIELDS: dict[str, tuple] = {
    "pe": ("P/E", "x", "peratio.lasttwelvemonths", 1, "trailingPE", 1, "p/e|pe|pe ratio|price to earnings|price/earnings"),
    "pb": ("P/B", "x", "pricebookratio.quarterly", 1, "priceToBook", 1, "p/b|pb|price to book|price/book"),
    "ps": ("P/S", "x", "lastclosemarketcaptotalrevenue.lasttwelvemonths", 1, "priceToSalesTrailing12Months", 1, "p/s|ps|price to sales|price/sales"),
    "ev_ebitda": ("EV/EBITDA", "x", "lastclosetevebitda.lasttwelvemonths", 1, "enterpriseToEbitda", 1, "ev/ebitda|ev to ebitda"),
    "peg": ("PEG (5y)", "x", "pegratio_5y", 1, None, 1, "peg|peg ratio"),
    "de": ("D/E", "x", "totaldebtequity.lasttwelvemonths", 100, "debtToEquity", 0.01, "d/e|de|debt to equity|debt/equity|debt-to-equity|leverage|debt"),
    "net_debt_ebitda": ("Net debt / EBITDA", "x", "netdebtebitda.lasttwelvemonths", 1, None, 1, "net debt/ebitda|net debt to ebitda"),
    "current_ratio": ("Current ratio", "x", "currentratio.lasttwelvemonths", 1, "currentRatio", 1, "current ratio|cr"),
    "quick_ratio": ("Quick ratio", "x", "quickratio.lasttwelvemonths", 1, "quickRatio", 1, "quick ratio"),
    "roe": ("ROE", "%", "returnonequity.lasttwelvemonths", 1, "returnOnEquity", 100, "roe|return on equity"),
    "roa": ("ROA", "%", "returnonassets.lasttwelvemonths", 1, "returnOnAssets", 100, "roa|return on assets"),
    "gross_margin": ("Gross margin", "%", "grossprofitmargin.lasttwelvemonths", 1, "grossMargins", 100, "gross margin|gross margins|gross profit margin"),
    "ebitda_margin": ("EBITDA margin", "%", "ebitdamargin.lasttwelvemonths", 1, "ebitdaMargins", 100, "ebitda margin"),
    "net_margin": ("Net margin", "%", "netincomemargin.lasttwelvemonths", 1, "profitMargins", 100, "net margin|profit margin|net profit margin"),
    "revenue_growth": ("Revenue growth 1y", "%", "totalrevenues1yrgrowth.lasttwelvemonths", 1, None, 1, "revenue growth|sales growth"),
    "eps_growth": ("EPS growth 1y", "%", "epsgrowth.lasttwelvemonths", 1, None, 1, "eps growth|earnings growth"),
    "dividend_yield": ("Dividend yield", "%", "forward_dividend_yield", 1, "dividendYield", 1, "dividend yield|yield"),
    "perf_52w": ("52-week change", "%", "fiftytwowkpercentchange", 1, "52WeekChange", 100, "52 week change|52-week change|1 year return"),
    "short_float": ("Short % of float", "%", "short_percentage_of_float.value", 1, "shortPercentOfFloat", 100, "short interest|short float"),
    # Market cap: the screener's is in local currency, so it never goes there.
    "mcap": ("Market cap", "$", None, 1, None, 1, "market cap|market capitalization|mcap"),
}
# fmt: on
# No "beta" field (1.29): the screener's and .info's beta is Yahoo's (5Y
# monthly vs the S&P 500 in USD), which the app no longer shows anywhere; its
# own beta (capm.py) exists only for names it has prices for. "Low beta" in a
# request is passed through to `ignored`, so the chips say it was not applied.
OPS = {"lt": "<", "lte": "≤", "gt": ">", "gte": "≥"}
_OP_WORDS = {
    "lt": r"<|under|below|less than|lower than|smaller than",
    "lte": r"<=|≤|at most|no more than|max(?:imum)?",
    "gt": r">|over|above|greater than|more than|higher than|bigger than|exceeding",
    "gte": r">=|≥|at least|no less than|min(?:imum)?",
}
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9,
         "billion": 1e9, "t": 1e12, "tn": 1e12, "trillion": 1e12}  # fmt: skip

# Region words -> screener region codes; groups expand to several.
_EUROPE = "at be ch cz de dk ee es fi fr gb gr hu ie is it lt lv nl no pl pt ro se"
REGIONS = {
    "us": "united states|us|u.s.|usa|american",
    "ca": "canada|canadian",
    "gb": "united kingdom|uk|britain|british|england|english",
    "de": "germany|german",
    "fr": "france|french",
    "it": "italy|italian",
    "es": "spain|spanish",
    "nl": "netherlands|dutch",
    "ch": "switzerland|swiss",
    "se": "sweden|swedish",
    "dk": "denmark|danish",
    "no": "norway|norwegian",
    "fi": "finland|finnish",
    "jp": "japan|japanese",
    "cn": "china|chinese",
    "hk": "hong kong",
    "tw": "taiwan|taiwanese",
    "kr": "korea|south korea|korean",
    "in": "india|indian",
    "au": "australia|australian",
    "br": "brazil|brazilian",
    "mx": "mexico|mexican",
    "sg": "singapore",
    "il": "israel|israeli",
    "za": "south africa|south african",
    _EUROPE: "europe|european",
    "dk fi is no se": "nordic|nordics|scandinavia|scandinavian",
    "cn hk id in jp kr my ph sg th tw vn": "asia|asian",
    "ar br cl co mx pe": "latin america|latam",
    "us ca": "north america|north american",
}
# Exchange name -> (screener codes, words). "nasdaq" is an exchange, not a
# region: Region US would admit NYSE and OTC lines too.
EXCHANGES = {
    "Nasdaq": ("NMS NGM NCM", "nasdaq"),
    "NYSE": ("NYQ", "nyse|new york stock exchange"),
    "NYSE American": ("ASE", "nyse american|amex"),
    "London": ("LSE", "lse|london stock exchange"),
    "Xetra": ("GER", "xetra|frankfurt stock exchange"),
    "Euronext Paris": ("PAR", "euronext paris|paris bourse"),
    "Euronext Amsterdam": ("AMS", "euronext amsterdam"),
    "Milan": ("MIL", "borsa italiana|milan stock exchange"),
    "Madrid": ("MCE", "bme|madrid stock exchange"),
    "SIX Swiss": ("EBS", "six swiss exchange|swiss exchange"),
    "Copenhagen": ("CPH", "nasdaq copenhagen|copenhagen stock exchange"),
    "Stockholm": ("STO", "nasdaq stockholm|stockholm stock exchange"),
    "Tokyo": ("JPX", "tokyo stock exchange|tse"),
    "Hong Kong": ("HKG", "hkex|hong kong stock exchange"),
    "Toronto": ("TOR", "tsx|toronto stock exchange"),
    "Taiwan": ("TAI", "twse|taiwan stock exchange"),
    "Korea": ("KSC", "krx|kospi"),
    "NSE India": ("NSI", "nse|national stock exchange of india"),
    "ASX": ("ASX", "asx|australian securities exchange"),
    "Shanghai": ("SHH", "shanghai stock exchange|sse"),
    "Shenzhen": ("SHZ", "shenzhen stock exchange|szse"),
}
# A listing's exchange code -> the name a card shows.
EXCHANGE_NAMES = {c: name for name, (codes, _w) in EXCHANGES.items() for c in codes.split()}
EXCHANGE_NAMES.update({"PCX": "NYSE Arca", "BTS": "Cboe", "TAI": "Taiwan", "TWO": "Taipei Exchange",
                       "KOE": "KOSDAQ", "BSE": "BSE India", "ENX": "Euronext", "OSL": "Oslo",
                       "HEL": "Helsinki", "VIE": "Vienna", "BRU": "Brussels", "LIS": "Lisbon",
                       "ISE": "Dublin", "SAO": "B3", "BUE": "Buenos Aires", "JNB": "Johannesburg",
                       "SES": "Singapore", "TLV": "Tel Aviv", "CNQ": "CSE", "VAN": "TSX Venture",
                       "PNK": "OTC", "NAS": "Nasdaq", "BUD": "Budapest", "WSE": "Warsaw",
                       "PRA": "Prague", "ATH": "Athens", "IST": "Istanbul", "FRA": "Frankfurt",
                       "STU": "Stuttgart", "MUN": "Munich", "DUS": "Düsseldorf", "BER": "Berlin",
                       "HAM": "Hamburg", "IOB": "LSE intl. order book"})  # fmt: skip
# "low leverage", "high dividend yield": a moderate threshold the chip shows
# (and the user can edit), never a lowest-first ranking — "small leverage"
# ranked by lowest D/E returned companies with no debt at all.
_QUAL = {"de": (0.5, 1.5), "pe": (15, 30), "pb": (1.5, 4),
         "dividend_yield": (1, 3), "roe": (8, 15), "net_margin": (5, 15),
         "gross_margin": (25, 50), "revenue_growth": (5, 15), "eps_growth": (5, 15),
         "short_float": (2, 10), "current_ratio": (1, 2)}  # fmt: skip
_LOW = r"low|small|little|modest|limited|minimal|cheap|conservative"
_HIGH = r"high|strong|solid|healthy|large"
_HEDGE = r"(?:(?:relative|relatively|very|fairly|quite|rather|reasonably|small|low)\s+)*"
# Everyday words for the screener's sectors and industries; every exact
# sector/industry name is matched on top of these.
_I = "industries"
SYNONYMS = {
    "tech|technology|tech companies": ("sectors", ["Technology"]),
    "healthcare|health care": ("sectors", ["Healthcare"]),
    "financials|financial": ("sectors", ["Financial Services"]),
    "energy|oil and gas": ("sectors", ["Energy"]),
    "utilities": ("sectors", ["Utilities"]),
    "real estate|reits|reit": ("sectors", ["Real Estate"]),
    "industrials": ("sectors", ["Industrials"]),
    "materials": ("sectors", ["Basic Materials"]),
    "consumer staples|staples": ("sectors", ["Consumer Defensive"]),
    "consumer discretionary": ("sectors", ["Consumer Cyclical"]),
    "media|communication services|telecoms": ("sectors", ["Communication Services"]),
    "banks|bank|banking": (_I, ["Banks—Regional", "Banks—Diversified"]),
    "insurers|insurance": (_I, ["Insurance—Diversified", "Insurance—Life", "Insurance—Property & Casualty"]),
    "chipmakers|chip makers|chip|chips|semis|semiconductors": (_I, ["Semiconductors", "Semiconductor Equipment & Materials"]),
    "software": (_I, ["Software—Application", "Software—Infrastructure"]),
    "pharma|pharmaceuticals|drug makers|drugmakers": (_I, ["Drug Manufacturers—General", "Drug Manufacturers—Specialty & Generic"]),
    "biotech": (_I, ["Biotechnology"]),
    "automakers|car makers|carmakers": (_I, ["Auto Manufacturers"]),
    "defense|defence": (_I, ["Aerospace & Defense"]),
    "miners|mining": (_I, ["Gold", "Copper", "Other Industrial Metals & Mining", "Other Precious Metals & Mining"]),
}  # fmt: skip
TYPES = {
    "etf|etfs": "etf",
    "mutual funds|mutual fund|funds": "fund",
    "indices|indexes|index": "index",
}
_RANK_WORDS = (
    r"(?:ranked|sorted|ordered)\s+by|(?P<hi>highest|most|best)|(?P<lo>lowest|least|cheapest)"
)
_VAGUE = re.compile(r"\b(best|top|great|good|quality|leading|strongest)\b")
_LARGEST = re.compile(
    r"\b(largest|biggest|big|mega[- ]?caps?|large[- ]?caps?|giants?"
    r"|large(?!\s+(?:dividends?|margins?|growth|yields?|returns?|cash)))\b"
)
_FILLER = set(
    re.findall(
        r"\w+",
        "a an the and or with without of in on at for from by to that which who whose have has having"
        " are is be show me find list give get all any some companies company stocks stock shares"
        " names firms businesses listed based headquartered domiciled ratio than relative relatively"
        " very fairly quite rather reasonably",
    )
)
_TOP_N = 5


def _sectors_industries():
    from yfinance import const

    m = const.EQUITY_SCREENER_EQ_MAP["industry"]
    return (
        sorted(m),
        sorted({i for v in m.values() for i in v}),
        sorted(const.EQUITY_SCREENER_EQ_MAP["region"]),
    )


SECTORS, INDUSTRIES, ALL_REGIONS = _sectors_industries()


# =================================================================== query
def empty(text: str = "") -> dict:
    return {"text": text, "kind": "name", "filters": [], "sectors": [], "industries": [],
            "regions": [], "types": [], "exchanges": [], "rank": None, "picks": [], "ignored": [],
            "notes": [], "engine": "rules"}  # fmt: skip


def validate(q) -> dict:
    """Clean an untrusted query (the LLM's, or a chip edit from the page):
    unknown fields, operators or vocabulary move to `ignored`, never through."""
    if not isinstance(q, dict):
        return empty()
    out = empty(str(q.get("text", ""))[: _LIST_TEXT if q.get("kind") == "list" else 300])
    out["kind"] = q.get("kind") if q.get("kind") in ("name", "screen", "theme", "list") else "name"
    out["engine"] = "ai" if q.get("engine") == "ai" else "rules"
    out["ignored"] = [str(x)[:80] for x in (q.get("ignored") or [])[:10] if str(x).strip()]
    out["notes"] = [str(x)[:160] for x in (q.get("notes") or [])[:10]]
    for f in (q.get("filters") or [])[:40]:
        ok = isinstance(f, dict) and f.get("field") in FIELDS and f.get("op") in OPS
        v = f.get("value") if ok else None
        if len(out["filters"]) >= 20:
            break
        if ok and isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < 1e15:
            out["filters"].append({"field": f["field"], "op": f["op"], "value": float(v)})
        else:
            out["ignored"].append(_chip(f) if ok else f"filter {str(f)[:40]}")
    for key, vocab in (("sectors", SECTORS), ("industries", INDUSTRIES), ("regions", ALL_REGIONS),
                       ("types", sdb.TYPES), ("exchanges", EXCHANGES)):  # fmt: skip
        vals = [v for v in (q.get(key) or [])[:80] if isinstance(v, str)]
        out[key] = sorted(v for v in set(vals) if v in vocab)
        out["ignored"] += [v[:40] for v in vals if v not in vocab]
    r = q.get("rank")
    if isinstance(r, dict) and r.get("field") in FIELDS and r.get("dir") in ("asc", "desc"):
        out["rank"] = {"field": r["field"], "dir": r["dir"], "by": r.get("by") if r.get("by") in ("rules", "ai", "default") else "rules",
                       "why": str(r.get("why") or "")[:80]}  # fmt: skip
    for p in (q.get("picks") or [])[:15]:
        if isinstance(p, dict) and (p.get("ticker") or p.get("name")):
            out["picks"].append({k: str(p.get(k) or "")[:120] for k in ("ticker", "name", "why")})
    return out


def _chip(f) -> str:
    try:
        label, unit = FIELDS[f["field"]][:2]
        return f"{label} {OPS[f['op']]} {_fmt(f['value'], unit)}"
    except (KeyError, TypeError):
        return str(f)[:40]


def _fmt(v, unit: str) -> str:
    if unit == "$":
        for div, s in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
            if abs(v) >= div:
                return f"${v / div:g}{s}"
        return f"${v:g}"
    return f"{v:g}%" if unit == "%" else f"{v:g}"


def _words(spec: str) -> str:
    """'a|b c' -> a whole-word regex alternation, longest first."""
    alts = sorted(spec.split("|"), key=len, reverse=True)
    return (
        r"(?<![\w/])(?:"
        + "|".join(re.escape(a).replace(r"\ ", r"\s+") for a in alts)
        + r")(?![\w/])"
    )


_NUM = r"\$?\s*(-?\d+(?:\.\d+)?|-?\.\d+)\s*(%|x|k|mn|m|bn|b|tn|t|thousand|million|billion|trillion)?(?![\w])"
_FILTER_RES = [
    (
        key,
        op,
        re.compile(
            _words(spec[6]) + r"\s*(?:of|is|ratio|ratio of)?\s*(?:" + words + r")\s*" + _NUM
        ),
    )
    for key, spec in FIELDS.items()
    for op, words in _OP_WORDS.items()
]


def _value(key: str, num: str, suffix: str | None, notes: list) -> float:
    v, unit, suffix = float(num), FIELDS[key][1], (suffix or "").lower()
    if unit == "$":
        return v * _MULT.get(suffix, 1e9 if v < 10_000 else 1)
    # "ROE > 0.15" means 15%; "ROE > 1" means 1%. Yields and short interest
    # are routinely below 1%, so a fraction there is taken as written.
    if (
        unit == "%"
        and suffix != "%"
        and 0 < abs(v) < 1
        and key not in ("dividend_yield", "short_float")
    ):
        notes.append(f"Read {FIELDS[key][0]} {num} as {v * 100:g}%.")
        return v * 100
    if unit == "x" and suffix == "%":
        return v / 100  # "D/E under 80%" means 0.8
    return v


def parse_rules(text: str) -> tuple[dict, list[str]]:
    """Deterministic parse. Returns (query, leftover words nothing used)."""
    q = empty(text.strip()[:300])
    s = " " + " ".join(q["text"].lower().split()) + " "

    def take(rx, repl=" "):
        nonlocal s
        hits = list(re.finditer(rx, s))
        s = re.sub(rx, repl, s)
        return hits

    for key, (lo, hi) in _QUAL.items():
        for words, op, v in ((_LOW, "lt", lo), (_HIGH, "gt", hi)):
            if take(
                r"(?<![\w/])(?:"
                + words
                + r")\s+"
                + _HEDGE
                + _words(FIELDS[key][6])[len(r"(?<![\w/])") :]
            ):
                q["filters"].append({"field": key, "op": op, "value": float(v)})
    for key, op, rx in _FILTER_RES:
        for m in take(rx):
            q["filters"].append(
                {"field": key, "op": op, "value": _value(key, m.group(1), m.group(2), q["notes"])}
            )
    for key, spec in FIELDS.items():
        for m in take(r"(?:" + _RANK_WORDS + r")\s*" + _words(spec[6])):
            q["rank"] = {"field": key, "dir": "asc" if m.group("lo") else "desc", "by": "rules"}
    if take(_LARGEST.pattern):
        q["rank"] = q["rank"] or {"field": "mcap", "dir": "desc", "by": "rules"}
    # Exact sector/industry names first, with a generic word after them, so
    # "uranium miners" is Uranium and not every mining industry too.
    for key, vocab in (("sectors", SECTORS), ("industries", INDUSTRIES)):
        for v in vocab:
            if take(
                _words(_fold(v)) + r"(?:\s+(?:miners|mining|producers|makers|companies|stocks))?"
            ):
                q[key].append(v)
    for spec, (key, vals) in SYNONYMS.items():
        if take(_words(spec)):
            q[key] += [v for v in vals if v not in q[key]]
    for name, (_codes, spec) in EXCHANGES.items():
        if take(_words(spec)):
            q["exchanges"].append(name)
    for codes, spec in REGIONS.items():
        if take(_words(spec)):
            q["regions"] += [c for c in codes.split() if c not in q["regions"]]
    for spec, typ in TYPES.items():
        if take(_words(spec)):
            q["types"].append(typ)
    vague = bool(_VAGUE.search(s))
    s = _VAGUE.sub(" ", s)
    left = [w for w in re.findall(r"[\w\-&.'/]+", s) if w not in _FILLER and re.search(r"\w", w)]
    structured = (q["filters"] or q["sectors"] or q["industries"] or q["regions"] or q["types"]
                  or q["exchanges"])  # fmt: skip
    if structured or q["rank"]:
        q["kind"] = "screen"
        if vague and not q["rank"]:
            q["rank"] = {"field": "mcap", "dir": "desc", "by": "default"}
    return q, (left if structured or q["rank"] else [])


def _fold(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9&]+", " ", s.lower().replace("—", " ")).split())


# =================================================================== the LLM
_SYSTEM = """You turn a request for companies into a JSON query for a stock screener.
Use only the enums in the schema. Values are in the user's units: D/E 0.8 means
0.8x, margins/ROE/growth/yield in percent (15 means 15%), market cap in USD.

How to read a request:
- filters are constraints; rank is what the user wants most of. A field the
  user only constrains is not the ranking ("low debt" is D/E < 0.5, not
  "lowest D/E first"). Rank by a filtered field only when the user says
  lowest/highest/most/least of it.
- Size words (big, large, largest, giant, mega cap) mean rank by market cap,
  highest first. Small caps mean a market-cap filter (< 2e9), not a rank.
- Vague amounts get moderate thresholds, never extremes: low/small D/E < 0.5,
  high D/E > 1.5, cheap P/E < 15, high dividend yield > 3, high ROE > 15,
  high margin > 15, high growth > 15. There is no beta field.
- "best"/"top"/"quality" without a metric: rank by a quality metric (ROE or a
  margin) that the user does not already filter on; why in rank.why.
- Exchanges (Nasdaq, NYSE, London...) go in "exchanges", not "regions".
- No ranking wanted: rank.field "none".
- kind "theme": a theme no sector or industry captures (a drug class, a
  technology, a supply chain). List up to 12 real, listed companies in "picks"
  (ticker as on Yahoo Finance, name, why in under 8 words), most relevant
  first. Pick only companies whose own business is the theme — for a drug or
  technology, an approved product or a late-stage program, not a partnership
  or an early trial; fewer picks beat weak ones. Any sectors/industries you set must contain every pick.
- kind "name": the user typed a company name; put it in picks.
- Anything you cannot express goes, verbatim, into "ignored".

Examples (request -> query):
- big tech in nasdaq with small leverage -> screen, sectors [Technology],
  exchanges [Nasdaq], filters [de lt 0.5], rank mcap desc
- cheap european banks -> screen, industries [Banks—Regional, Banks—Diversified],
  regions [Europe codes], filters [pe lt 15], rank none
- best dividend payers in utilities -> screen, sectors [Utilities],
  filters [dividend_yield gt 3], rank dividend_yield desc (the user asked for
  the most of it)
- profitable small caps with strong growth -> screen, filters [mcap lt 2e9,
  net_margin gt 0, revenue_growth gt 15], rank revenue_growth desc
- GLP-1 drug makers -> theme, picks [LLY, NVO, ...]
Never invent fields. Never return prose outside the JSON."""


def _llm_schema() -> dict:
    # Free strings and lists are capped: NIM's 30B model can loop, repeating
    # one phrase until max_tokens (news_sentiment._json_schema), which here
    # meant the 20 s timeout and the rule parser's reading.
    s = {"type": "string", "maxLength": 160}
    arr = lambda items, n=40: {"type": "array", "items": items, "maxItems": n}  # noqa: E731
    obj = lambda props: {"type": "object", "additionalProperties": False,  # noqa: E731
                         "required": list(props), "properties": props}  # fmt: skip
    keys = list(FIELDS)
    return obj({
        "kind": {"type": "string", "enum": ["name", "screen", "theme"]},
        "filters": arr(obj({"field": {"type": "string", "enum": keys},
                            "op": {"type": "string", "enum": list(OPS)}, "value": {"type": "number"}})),
        "sectors": arr({"type": "string", "enum": SECTORS}),
        "industries": arr({"type": "string", "enum": INDUSTRIES}),
        "regions": arr({"type": "string", "enum": ALL_REGIONS}),
        "types": arr({"type": "string", "enum": list(sdb.TYPES)}),
        "exchanges": arr({"type": "string", "enum": list(EXCHANGES)}),
        "rank": obj({"field": {"type": "string", "enum": [*keys, "none"]}, "dir": {"type": "string", "enum": ["asc", "desc"]}, "why": s}),
        "picks": arr(obj({"ticker": {"type": "string", "maxLength": 24}, "name": s, "why": s}), 30),
        "ignored": arr(s),
    })  # fmt: skip


def ai_available() -> bool:
    from convexity import news_sentiment as ns

    return bool(ns.NVIDIA_API_KEY)


_AI_DEADLINE_S = 25.0
# Hedged request (2026-10-22). NIM's free models split speed and judgement:
# nemotron-3.5-lightning answers in ~2 s but misread 3-4 of the 22 eval cases
# ("best" with nothing to rank by, "european" dropped, an empty theme);
# nemotron-3-ultra read all of them but took 3-33 s. Both are asked at once:
# the careful answer is used when it lands within _CAREFUL_WAIT_S, else the
# fast one. Search is user-triggered, so the second call costs little quota.
_CAREFUL_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
_CAREFUL_WAIT_S = 18.0


def parse_llm(text: str, hint: dict) -> dict | None:
    """The NIM model's reading of `text`, validated; None when it failed, timed
    out, or NIM is rate-limited right now (the news refresh owns the quota)."""
    from convexity import news_sentiment as ns
    from convexity.helpers import Cancelled

    if ns._nv_rate_limit_until > time.time():
        return None
    # The rules' placeholder rank (market cap for "best") is not a reading:
    # shown to the model, it was copied back instead of choosing a metric.
    brief = {k: v for k, v in _brief(hint).items() if k != "rank" or v.get("by") != "default"}
    user = f"Request: {text}\nA rule parser already read: {json.dumps(brief)}"
    raw = _hedged_call(ns, Cancelled, user)
    if not isinstance(raw, dict):
        return None
    # The schema makes the model always send a rank; only a screen uses one.
    rank = {**(raw.get("rank") or {}), "by": "ai"} if raw.get("kind") == "screen" else None
    return _sane_rank(validate({**raw, "text": text, "engine": "ai", "rank": rank}))


def _hedged_call(ns, cancelled, user: str) -> dict | None:
    """The careful model's reading if it is back within _CAREFUL_WAIT_S,
    else the fast model's (the News read chain's head), else whichever of
    them answers before the deadline. One call when they are the same model."""
    t0 = time.monotonic()
    deadline = Deadline(_AI_DEADLINE_S)
    fast = ns.active_model()

    def ask(model: str, timeout: float):
        try:
            return ns._nvidia_call(_SYSTEM, user, (), deadline, schema=_llm_schema(),
                                   name="company_search", record=False, tag="search",
                                   timeout=timeout, model=model)  # fmt: skip
        except cancelled:
            return None

    if fast == _CAREFUL_MODEL:
        return ask(fast, 20.0)
    pool = ThreadPoolExecutor(max_workers=2)
    careful_f = pool.submit(ask, _CAREFUL_MODEL, _AI_DEADLINE_S - 1)
    fast_f = pool.submit(ask, fast, 20.0)
    pool.shutdown(wait=False)  # never join: a late answer is simply dropped
    wait([careful_f], timeout=_CAREFUL_WAIT_S)
    if careful_f.done() and isinstance(careful_f.result(), dict):
        return careful_f.result()
    left = lambda: max(0.0, _AI_DEADLINE_S - (time.monotonic() - t0))  # noqa: E731
    wait([fast_f], timeout=left())
    if fast_f.done() and isinstance(fast_f.result(), dict):
        print("[search] careful model slow; using the fast model's reading", flush=True)
        return fast_f.result()
    wait([careful_f], timeout=left())
    if careful_f.done() and isinstance(careful_f.result(), dict):
        return careful_f.result()
    print("[search] AI step timed out; using the rule parser's reading", flush=True)
    return None


_EXTREME = re.compile(
    r"\b(lowest|highest|least|most|cheapest|smallest|ranked|sorted|order(?:ed)?)\b"
)


def _sane_rank(q: dict) -> dict:
    """Guard the model's ranking with the two rules it broke live: a size word
    ranks by market cap, and a field the user only constrains ("small
    leverage") is not ranked lowest-first unless they asked for an extreme."""
    text = q["text"].lower()
    r = q["rank"]
    if (
        q["kind"] == "screen"
        and _LARGEST.search(text)
        and not any(f["field"] == "mcap" for f in q["filters"])
    ):
        q["rank"] = {"field": "mcap", "dir": "desc", "by": "rules", "why": "size"}
    elif r and r["field"] in {f["field"] for f in q["filters"]}:
        # "best dividend payers" may rank by the most of it; never lowest-first
        # by a constraint the user only stated ("small leverage").
        asked = _EXTREME.search(text) or (r["dir"] == "desc" and re.search(r"\b(best|top)\b", text))
        if not asked:
            q["rank"] = None
    return q


_TICKERISH = re.compile(r"[A-Z0-9][A-Z0-9.\-^=]{0,11}")


def _ticker_run(text: str) -> list[str] | None:
    """`AAPL MSFT NVDA`: tickers separated by spaces only. Every token must be
    upper-case as typed AND an exact listing in the symbol pack, so a
    description ("US REITS", "AI chip makers") never reads as a list."""
    toks = text.split()
    if len(toks) < 2 or re.search(r"[,;\n]", text):
        return None
    if not all(_TICKERISH.fullmatch(t) and re.search(r"[A-Z]", t) for t in toks):
        return None
    return toks if all(sdb.get(t) for t in toks) else None


# Pasted lists ------------------------------------------------------------
_LIST_MAX = 50  # parts searched; the rest is reported, not silently dropped
_LIST_TEXT = 4000  # characters of a paste kept in the query
# A trailing weight or amount is not part of a name ("Apple 25%", "SAP
# €1,200.50"); a bare whole number may be ("S&P 500", "FTSE 100"), so it is
# dropped only after a ticker ("AAPL 100").
_AMOUNT = re.compile(r"\s+(?:[$€£]\s?\d[\d.,]*|\d[\d.,]*\s?%|\d+\.\d+)$")
_NUMBER = re.compile(r"[$€£]?\s?[\d.,]*\d[\d.,]*\s?%?")
# A spreadsheet's header row ("Ticker, Name, Weight") is not three companies:
# as names they found Ticketplus, Nameson and an equal-weight ETF.
_HEADER = re.compile(
    r"(ticker|symbol|name|company|security|holding|instrument|isin|weight|shares|qty|quantity"
    r"|amount|value|price|allocation|position|market value)s?(\s*\(?%\)?)?",
    re.IGNORECASE,
)


def _legal_form(p: str, prev: str) -> bool:
    """A part that is only a legal form or share class belongs to the name
    before it: "Nestle, S.A.", "Eni, S.p.A.", "Alphabet Inc., Class A".
    symbol_db's own list, also read without spaces ("L.P." is "lp"). An
    upper-case word after a ticker is a ticker: "NVDA, SE" is Sea Ltd."""
    f = sdb._fold(p)
    if not f or all(sdb._SUFFIX.sub(" ", x).strip() for x in (f, f.replace(" ", ""))):
        return False
    return not (p.isalpha() and p.isupper() and _TICKERISH.fullmatch(prev))


def _parts(text: str) -> list[str]:
    """One name or ticker per comma, semicolon, line or tab (an Excel row);
    weights and amounts stripped, number-only cells and header words dropped
    (a CSV row "Apple Inc.,AAPL,100" is a name and its ticker)."""
    run = _ticker_run(text)
    if run:
        return run
    out: list[str] = []
    for p in re.split(r"[,;\n\t]+", text):
        p = _AMOUNT.sub("", p.strip())
        if m := re.fullmatch(r"(\S+)\s+\d+", p):
            p = m[1] if _TICKERISH.fullmatch(m[1]) and sdb.get(m[1]) else p
        if not p or (_NUMBER.fullmatch(p) and not sdb.get(p)) or _HEADER.fullmatch(p):
            continue
        if out and _legal_form(p, out[-1]):
            out[-1] += ", " + p
        else:
            out.append(p)
    return out


def _all_categories(parts: list[str]) -> bool:
    """Every part a category and nothing else ("semiconductors, banks",
    "large caps, Europe"): a screen, not a list of companies."""
    for p in parts:
        pq, left = parse_rules(p)
        if left or not (
            pq["rank"]
            or any(pq[k] for k in ("sectors", "industries", "regions", "types", "exchanges"))
        ):
            return False
    return True


def _mostly_names(parts: list[str]) -> bool:
    """At least half the parts name a company for sure (pack only, no
    network). "AI, robotics, automation" is a theme with commas: as a list
    it showed C3.ai and two random matches."""
    need, sure = (len(parts) + 1) // 2, 0
    for i, p in enumerate(parts):
        sure += bool(_ISIN.fullmatch(p)) or _pack_match(p)[0] != "guess"
        if sure >= need or sure + len(parts) - i - 1 < need:
            break
    return sure >= need


def _brief(q: dict) -> dict:
    return {
        k: q[k]
        for k in ("filters", "sectors", "industries", "regions", "exchanges", "types", "rank")
        if q[k]
    }


def parse(text: str) -> dict:
    """Rules first; the LLM only when a phrase is left over, the query reads
    as a theme, or "best" needs a metric — and only with a key."""
    q, left = parse_rules(text)
    # A pasted list ("AAPL, microsft, Novo"): one company per part, no AI.
    # Any length of name ("HSBC S&P 500 Dist UCITS ETF"); criteria with an
    # operator, or parts that are all categories, stay a screen. Not cut to
    # the screen's 300 characters: a long paste keeps every part. A list
    # that is mostly not companies is a theme with commas ("AI, robotics").
    parts = _parts(text)
    if (
        len(parts) >= 2
        and not q["filters"]
        and not re.search(r"[<>=]", text)
        and not _all_categories(parts)
        and _mostly_names(parts[:_LIST_MAX])
    ):
        return _list_query(text, parts)
    if left and not q["filters"]:
        # "deutsche bank", "bank of america", "american express": category
        # words inside a company name. A near-exact name wins over the parse.
        top = sdb.lookup(q["text"], limit=1)
        if top and top[0].score >= _SURE:
            return empty(q["text"])
    needs_ai = bool(left) or (q["rank"] or {}).get("by") == "default"
    if q["kind"] == "name":
        # Two words are enough to be a description: "cybersecurity companies"
        # best-matched The TJX Companies at 79.5 and was shown as a name. A
        # real name or a typo of one scores 85+ ("tjx companies" 100, "novo
        # nordsk" 89.7, "microsft" 88.1); single words stay name lookups.
        top = sdb.lookup(q["text"], limit=1)
        needs_ai = len(q["text"].split()) >= 2 and (not top or top[0].score < _SURE)
        if not needs_ai:
            return q
    if needs_ai and ai_available():
        ai = parse_llm(q["text"], q)
        if ai is not None:
            return ai
        q["notes"].append("The AI step failed, so this is the rule parser's reading.")
    elif needs_ai and q["kind"] == "name":
        q["notes"].append("This reads like a description: theme search needs an NVIDIA key.")
    if left:
        q["ignored"].append(" ".join(left)[:80])
    if (q["rank"] or {}).get("by") == "default":
        hint = "" if ai_available() else " With an NVIDIA key the AI picks the ranking."
        q["notes"].append('"best" is not a metric, so this is ranked by market cap.' + hint)
    return q


# =================================================================== executor
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_INFO: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()
_TTL_S = 600.0


def run(q: dict, offset: int = 0, limit: int = _TOP_N) -> dict:
    """Execute a validated query: the next `limit` results (5 inline, pages of
    25 in the full-screen table) + warnings."""
    t0 = time.time()
    if q["kind"] == "list":
        return _run_list(q, t0)
    # The text only matters to a name search; a screen is its parsed query.
    skip = ("notes",) if q["kind"] == "name" else ("notes", "text")
    key = json.dumps({k: v for k, v in q.items() if k not in skip}, sort_keys=True)
    with _LOCK:
        hit = _CACHE.get(key)
    base = hit[1] if hit and time.time() - hit[0] < _TTL_S else None
    warnings = [f'Ignored "{x}": not something Convexity can search on.' for x in q["ignored"]]
    if base is None:
        if q["kind"] == "name" and not q["picks"]:
            hits = sdb.lookup(q["text"], limit=25)
            # Near-misses far below the best match are noise ("microsoft" ->
            # Smith Micro Software); keep the ones within 15 points.
            typed = q["text"].strip().upper()  # a typed ticker keeps that listing
            base = [_card(h, keep=h.ticker == typed) for h in hits if h.score >= hits[0].score - 15]
            if not hits or hits[0].score < _SURE:  # a name the pack lacks (Gedeon Richter)
                live = _yahoo_cards(q["text"], {c["ticker"] for c in base})
                # A company whose name has the typed word in it ("richter")
                # beats the pack's partial matches (Richtech Robotics).
                exact = [c for c in live if _has_words(q["text"], c["name"])]
                base = exact + base + [c for c in live if c not in exact]
        elif q["kind"] in ("name", "theme"):
            base, dropped = _verify_picks(q["picks"])
            if q["regions"]:  # "... in Europe": the company's home, not the AI's say-so
                base = [c for c in base if c["region"] in q["regions"]]
            if q["exchanges"]:
                base = [c for c in base if sdb.on_exchanges(c["group"], _codes(q))]
            if q["sectors"] or q["industries"]:  # a pick outside its own theme's categories
                off = [c["ticker"] for c in base if (c["sector"] or c["industry"])
                       and c["sector"] not in q["sectors"] and c["industry"] not in q["industries"]]  # fmt: skip
                base = [c for c in base if c["ticker"] not in off]
                if off:
                    warnings.append(
                        f"Dropped AI picks outside the theme's sectors and industries: {', '.join(off)}."
                    )
            if q["filters"]:  # the AI's picks still have to pass the user's criteria
                _fill_values(base, q)
                base = [c for c in base if _passes(c, q["filters"], c["values"])]
            if dropped:
                warnings.append(
                    f"{dropped} AI-named compan{'y' if dropped == 1 else 'ies'} not found, dropped."
                )
            if not q["picks"]:
                warnings.append(
                    "The AI named no companies for this theme."
                    if ai_available()
                    else "Theme search needs an NVIDIA key (Settings → API keys)."
                )
        elif (
            q["filters"]
            and any(f["field"] != "mcap" for f in q["filters"])
            or (q["rank"] and q["rank"]["field"] != "mcap")
        ):
            try:
                base = _screen(q)
            except Exception as e:  # Yahoo down or a refused query: say so, cache nothing
                print(f"[search] screener failed: {type(e).__name__}: {e}", flush=True)
                warnings.append("Yahoo's screener did not answer. Try again in a moment.")
                return {"query": q, "chips": _chips(q), "results": [], "total": 0,
                        "offset": offset, "warnings": warnings, "notes": q["notes"]}  # fmt: skip
        else:
            base = [_card(h, exchanges=_codes(q)) for h in sdb.category(sectors=q["sectors"], industries=q["industries"],
                    regions=q["regions"], types=q["types"] or ("stock",), exchanges=_codes(q), limit=250)]  # fmt: skip
            base = [c for c in base if _passes(c, q["filters"], {})]
        with _LOCK:
            _evict(_CACHE, 50)
            _CACHE[key] = (time.time(), base)
    page = base[offset : offset + max(1, min(limit, 50))]
    t1 = time.time()
    _fill_values(page, q)
    if not base:
        warnings.append("No matches." if sdb.status()["installed"] else
                        "The symbol list is still downloading; try again in a minute.")  # fmt: skip
    print(f"[search] {q['text']!r} engine={q['engine']} kind={q['kind']} query={json.dumps(_brief(q))}"
          f" results={len(base)} {time.time() - t0:.1f}s (values {time.time() - t1:.1f}s)", flush=True)  # fmt: skip
    return {"query": q, "chips": _chips(q), "results": page, "total": len(base),
            "offset": offset, "warnings": warnings, "notes": q["notes"], "fields": _LABELS}  # fmt: skip


_LABELS = {k: v[:2] for k, v in FIELDS.items()}  # the page formats values with these
_SURE = 85.0  # a name match this good is the company; below it, a guess
_NEAR = 6.0  # name matches this close to the best are weighed by _pick...
_FUND_NEAR = 12.0  # ...funds wider: thin lines named exactly beat CSPX.L by 7, Amundi by 11
_THIN = 0.05  # a fund line trading < 5% of the liquid one is not the one meant
_QUAL_COST = 4.0  # per fund-changing word nobody typed
_GIANT = 20  # a short name of a company 20x its namesake's size is that company
_GIANT_USD = 1e10  # initials (TSMC) or a tied word (Samsung) are trusted this big only
_TIE = 1.0  # another company's name match this close makes the best one a guess
_LIST_WORKERS = 6
_LIST_DEADLINE_S = 20.0  # one whole list; a Yahoo fallback alone can take 8 s
_YAHOO_TIMEOUT_S = 8.0
_EURO = (".L", ".DE", ".AS", ".SW", ".PA", ".MI")  # where a bare ETF ticker (VWCE) trades
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}\d")
_SHORT_TICKER = re.compile(r"(?=.*[A-Z])[A-Z0-9]{2,6}")
_CLASS_PART = re.compile(r"class\s+[a-c]\b", re.IGNORECASE)  # "Alphabet Class C" wants GOOG
_CLASS_TICKER = re.compile(r"([A-Z]{1,5})[./ ]([A-C])")  # BRK.B, BF/B: Yahoo writes BRK-B
_PAREN = re.compile(r"(.*?)\s*\(([A-Z0-9][A-Z0-9.\-]{0,11})\)")  # Apple Inc. (AAPL)
_BBG = re.compile(r"([A-Z0-9][A-Z0-9/.\-]{0,11})\s+([A-Z]{2})\s+(?i:equity)")  # VOD LN Equity
# Bloomberg's exchange codes -> Yahoo's suffix: US composites and the main venues.
_BBG_SUFFIX = {"US": "", "UN": "", "UW": "", "UQ": "", "LN": ".L", "GY": ".DE", "GR": ".DE",
               "FP": ".PA", "NA": ".AS", "BB": ".BR", "PL": ".LS", "IM": ".MI", "SM": ".MC",
               "SW": ".SW", "VX": ".SW", "SS": ".ST", "DC": ".CO", "FH": ".HE", "NO": ".OL",
               "AV": ".VI", "ID": ".IR", "JP": ".T", "JT": ".T", "HK": ".HK", "CN": ".TO",
               "CT": ".TO", "AU": ".AX"}  # fmt: skip
# Share classes whose tickers carry no class letter (GOOG is Alphabet's class C).
_CLASS_LINES = {("GOOGL", "c"): "GOOG", ("GOOG", "a"): "GOOGL"}
# Words that make a fund a different fund; a candidate carrying one the user
# did not type is a worse match ("Swap", "ESG", "EUR Hdg" next to the plain one).
_QUALIFIERS = {"swap", "esg", "sri", "climate", "screened", "pab", "hedged", "hdg", "leveraged", "short", "inverse", "2x", "3x",
               "income", "ex", "equal", "covered", "buffer", "daily", "ultra"}  # fmt: skip
# A currency in a fund's name ("2C EUR", "3CCHF") marks a hedged or currency
# share class: a different line from the plain one unless typed.
_CCY_CLASS = re.compile(r"(eur|chf|gbp|jpy|cad|aud|sek|nok|hkd)$")
_ACC = {"acc", "accumulating", "accumulation"}
_DIST = {"dist", "distributing", "distribution"}
# Share-class words lower a name's score ("Vanguard FTSE All-World Dist":
# 82.5 with them, 92.5 without), so the pack is asked with and without them
# and _pick reads them as hints. Yahoo's name search is asked without the
# wrapper words too.
_CLASS_WORDS = rf"{'|'.join(_ACC | _DIST)}"
_FUND_WORDS = rf"ucits|etf|class\s+[a-c]|{_CLASS_WORDS}"


def _without(words: str, part: str) -> str:
    return re.sub(rf"\b({words})\b", " ", part, flags=re.IGNORECASE).strip() or part


_MATCH_RANK = {"exact": 0, "hit": 1, "guess": 2}


def _has_words(typed: str, name: str) -> bool:
    """Every word typed is a word of the name, legal forms aside: how a Yahoo
    answer ("richter" is Gedeon Richter, not Richtech) and a capitalised part
    (NVIDIA, not a fund with those initials) are checked."""
    words = set(sdb.name_key(typed).split())
    return bool(words) and words <= set(sdb.name_key(name).split())


def _pick(part: str, hits: list) -> sdb.Hit | None:
    """The best of the near-equal name matches, read with what the typed name
    says beyond the company: UCITS wants a non-US line, Acc/Dist the matching
    share class, a qualifier nobody typed costs. Among funds a thin line is
    dropped and liquidity breaks ties ("iShares Core S&P 500 UCITS ETF" is
    CSPX.L, not the Colombian line named exactly that). Then a short name of
    a giant is the giant: "Taiwan Semiconductor" is TSMC, not the $0.8B
    company with that exact name (whole words: "Ali" is not Alibaba)."""
    if not hits:
        return None
    typed = set(sdb.name_key(part).split())
    acc, dist = typed & _ACC, typed & _DIST
    if acc or dist:  # only UCITS funds come in Acc / Dist share classes
        typed.add("ucits")
    floor = hits[0].score - (_NEAR if hits[0].type == "stock" else _FUND_NEAR)
    near = [h for h in hits if h.score >= floor]
    # The liquid line is measured among the lines that fit what was typed: a
    # US fund (IVV) must not make every UCITS line look thin.
    liquid = max((h.adv_usd or 0 for h in near if h.type != "stock" and not ("ucits" in typed and h.region == "us")),
                 default=0)  # fmt: skip
    near = [h for h in near if h.type == "stock" or (h.adv_usd or 0) >= _THIN * liquid]
    # UCITS nobody typed counts against a line only when the US fund of that
    # name is in the running ("SPDR S&P 500 ETF" is SPY).
    us_alt = any(h.region == "us" for h in near)

    def adj(h) -> float:
        n = set(sdb.name_key(h.name).split())
        extra = n - typed
        a = h.score - _QUAL_COST * (
            len(extra & _QUALIFIERS) + sum(bool(_CCY_CLASS.search(w)) for w in extra)
        )
        if us_alt and "ucits" in extra:
            a -= 4
        if "ucits" in typed:
            a += 3 if h.region != "us" else -5
        if acc:  # an unmarked UCITS line is usually the distributing class
            a += 3 if n & _ACC else -6 if n & (_DIST | {"inc"}) else -3
        if dist:
            a += 3 if n & (_DIST | {"inc"}) else -6 if n & _ACC else 0
        if h.type != "stock" and h.adv_usd:  # 0 for the most liquid, down to -1.3
            a += math.log10(h.adv_usd / liquid)
        return a

    best = max(near, key=adj)
    if best.type == "stock" and best.mcap_usd:
        key = sdb.name_key(part) + " "
        giants = [h for h in hits if h.type == "stock" and h.score >= 90 and h.mcap_usd
                  and h.mcap_usd >= _GIANT * best.mcap_usd and (sdb.name_key(h.name) + " ").startswith(key)]  # fmt: skip
        if giants:
            best = max(giants, key=lambda h: h.mcap_usd)
    return best


def _class_line(h: sdb.Hit, cls: str) -> sdb.Hit:
    """The typed share class's own line: BRK-A for "Berkshire Hathaway Class
    A", GOOG for "Alphabet Class C"."""
    want = _CLASS_LINES.get((h.ticker, cls), f"-{cls.upper()}")
    for x in (h, *sdb.listings(h.group)):
        if x.ticker == want or (x.ticker.endswith(want) and x.region == h.region):
            return replace(x, score=h.score)
    return h


def _dash(t: str) -> str:
    m = _CLASS_TICKER.fullmatch(t.upper())  # "brk.b" too: it found a BRK.B option fund
    return f"{m[1]}-{m[2]}" if m else t


def _pack_match(part: str) -> tuple[str, sdb.Hit | None, bool]:
    """One pasted part read offline: (match, hit, keep). "exact": a ticker in
    any form a broker, Bloomberg or a spreadsheet writes it (LSE:HSPX, GLE
    SBF, AAPL US Equity, BRK.B, Apple Inc. (AAPL)). "hit": a sure name match,
    or a bare European ETF ticker (VWCE is VWCE.DE). "guess": the closest
    name, shown as such. `keep`: show that listing, not the company's most
    traded one."""
    if _ISIN.fullmatch(part):  # only Yahoo knows ISINs
        return "guess", None, False
    m = _PAREN.fullmatch(part)
    if m and (h := sdb.get(m[2])) and (not m[1] or sdb.same_company(m[1], h.name)
                                       or any(x.group == h.group for x in sdb.lookup(m[1], limit=3, min_score=_SURE))):  # fmt: skip
        return "exact", h, True
    pair = resolver._broker_pair(part) or resolver._normalize_exchange_prefix(
        resolver._normalize_google_colon(part)
    )
    forms = [part, _dash(part), pair[0] if pair else ""]
    if (m := _BBG.fullmatch(part)) and (sfx := _BBG_SUFFIX.get(m[2])) is not None:
        t = _dash(m[1].replace("/", "-"))
        forms.append((t.zfill(4) if sfx == ".HK" else t) + sfx)
    for t in forms:
        if t and (h := sdb.get(t)):
            return "exact", h, True
    if _SHORT_TICKER.fullmatch(part):
        lines = [h for sfx in _EURO if (h := sdb.get(part + sfx))]
        if lines:
            return "hit", max(lines, key=lambda h: h.adv_usd or 0), True
    plain = _without(_CLASS_WORDS, part)
    hits = {x.ticker: x for x in sdb.lookup(plain, limit=10)} if plain != part else {}
    for x in sdb.lookup(part, limit=10):
        if x.score >= getattr(hits.get(x.ticker), "score", -1):
            hits[x.ticker] = x
    h = _pick(part, sorted(hits.values(), key=lambda x: -x.score))
    if h is None:
        return "guess", None, False
    # A word several companies tie on is the biggest of them ("Honeywell":
    # HON, not the HONA spin-off 0.2 points ahead), and a guess unless it
    # starts a giant's name or initials (Samsung, TSMC): "robotics" ties five small companies,
    # "automation" Rockwell Automation and three others.
    tied = [
        x
        for x in hits.values()
        if x.type == "stock" and x.group != h.group and x.score >= h.score - _TIE
    ]
    if tied and h.type == "stock":
        h = max([h, *tied], key=lambda x: x.mcap_usd or 0)
    giant = h.type == "stock" and (h.mcap_usd or 0) >= _GIANT_USD
    starts = (sdb.name_key(h.name) + " ").startswith(sdb.name_key(part) + " ") or (
        bool(_TICKERISH.fullmatch(part)) and sdb.acronym(h.name).startswith(part.lower()))  # fmt: skip
    cls = re.search(r"class\s+([a-c])\b", part, re.IGNORECASE)
    if cls and h.type == "stock":
        h = _class_line(h, cls[1].lower())
    # A capitalised part matched by initials or a typo is a guess too (VWCE
    # hit a Virtus fund's initials at 90), unless a giant's (TSMC).
    if tied:
        sure = giant and starts
    else:
        sure = giant or not _TICKERISH.fullmatch(part) or _has_words(part, h.name)
    sure = sure and h.score >= _SURE
    return ("hit" if sure else "guess"), h, bool(cls)


def _isin_symbols(isin: str) -> list[str]:
    """Yahoo's search knows ISINs (its Lookup and the pack don't)."""
    import yfinance as yf

    try:
        found = yf.Search(isin, max_results=3, news_count=0, lists_count=0,
                          timeout=_YAHOO_TIMEOUT_S, raise_errors=False).quotes  # fmt: skip
    except Exception as e:
        print(f"[search] ISIN lookup for {isin} failed: {type(e).__name__}", flush=True)
        return []
    return [str(q.get("symbol") or "") for q in found or [] if isinstance(q, dict)]


def _yahoo_card(part: str) -> dict | None:
    """A company Yahoo names with every typed word in it (share-class words
    aside), or the typed ticker; an ISIN's listing, the fund's most traded
    line. A weak pack match ("richter" -> Richtech Robotics) loses to it."""
    if _ISIN.fullmatch(part):
        h = next(filter(None, map(sdb.get, _isin_symbols(part))), None)
        return _card(sdb.top(h.group) or h, keep=True) if h else None
    plain = _without(_FUND_WORDS, part)
    tk = part.upper() if _TICKERISH.fullmatch(part) else ""  # a name is no ticker
    return next(
        (
            c
            for c in _yahoo_cards(plain, (), tk)
            if c["ticker"] == tk or _has_words(plain, c["name"])
        ),
        None,
    )


def _resolve_part(part: str, dl: Deadline) -> dict | None:
    """One pasted part as a card, with `match` and `query` (the part as
    typed), or None. The pack first; Yahoo only for what the pack is unsure
    of, and only while the list's deadline allows."""
    match, h, keep = _pack_match(part)
    card = _yahoo_card(part) if match == "guess" and not dl.is_set() else None
    if card is not None:
        return {**card, "match": "hit", "query": part}
    return {**_card(h, keep=keep), "match": match, "query": part} if h else None


def _list_query(text: str, parts: list[str]) -> dict:
    """A list query: the paste as typed, or, past 50 names or 4,000
    characters, its first whole names and a warning that says so."""
    kept, size = [], 0
    for p in parts[:_LIST_MAX]:
        size += len(p) + 1
        if size > _LIST_TEXT:
            break
        kept.append(p)
    q = {**empty(text.strip()), "kind": "list"}
    if len(kept) < len(parts) or len(q["text"]) > _LIST_TEXT:
        q["text"] = "\n".join(kept)
    if len(kept) < len(parts):
        q["notes"] = [f"Searched the first {len(kept)} of {len(parts)} names."]
    return q


def _run_list(q: dict, t0: float) -> dict:
    """A pasted list: each part as typed (an exact ticker stays that listing),
    else its best name match; all on one page, in the order pasted. Parts
    resolve in parallel within one deadline. A guess says so on its card
    (`match`), and two parts landing on one card are told, unless one is
    the other's ticker (a CSV row) or a repeat. Not cached: lookups are."""
    parts = _parts(q["text"])[:_LIST_MAX]
    dl = Deadline(_LIST_DEADLINE_S)
    pool = ThreadPoolExecutor(max_workers=_LIST_WORKERS)
    futs = [pool.submit(_resolve_part, p, dl) for p in parts]
    wait(futs, timeout=_LIST_DEADLINE_S)
    pool.shutdown(wait=False, cancel_futures=True)  # a late Yahoo call ends on its own
    got = [f.result() if f.done() and not f.cancelled() else "late" for f in futs]
    # A spreadsheet row names a company twice ("NOV IBIS" and "Novo Nordisk"):
    # the listing typed as a ticker is the one meant, not the most traded.
    typed = {c.get("group") for c in got if isinstance(c, dict) and c["match"] == "exact"} - {None}
    cards, at, missing, late, warnings = [], {}, [], [], list(q["notes"])
    for part, card in zip(parts, got, strict=True):
        if card is None or card == "late":
            (missing if card is None else late).append(part)
            continue
        if card["match"] != "exact" and card.get("group") in typed and not _CLASS_PART.search(part):
            continue
        i = at.setdefault(card["ticker"], len(cards))
        if i == len(cards):
            cards.append(card)
            continue
        old = cards[i]
        if _MATCH_RANK[card["match"]] < _MATCH_RANK[old["match"]]:
            cards[i] = card
        if "exact" not in (old["match"], card["match"]) and old["query"].lower() != part.lower():
            warnings.append(f'"{old["query"]}" and "{part}" are both {card["ticker"]}: shown once.')
    if missing:
        warnings.append(f"Not found: {', '.join(missing)}.")
    if late:
        warnings.append(f"Out of time before: {', '.join(late)}. Search them again.")
    if not cards and not sdb.status()["installed"]:
        warnings = ["The symbol list is still downloading; try again in a minute."]
    print(f"[search] list of {len(parts)} engine=rules kind=list results={len(cards)}"
          f" missing={len(missing)} late={len(late)} {time.time() - t0:.1f}s", flush=True)  # fmt: skip
    return {"query": q, "chips": [], "results": cards, "total": len(cards), "offset": 0,
            "warnings": warnings, "notes": [], "fields": _LABELS}  # fmt: skip


def _hit_fields(h: sdb.Hit) -> dict:
    return {k: v for k, v in asdict(h).items() if k not in ("score", "adv_usd")}


def _card(h: sdb.Hit, why: str = "", keep: bool = False, exchanges=()) -> dict:
    """One result. A company shows (and Add adds) its most traded listing —
    NVO for Novo Nordisk — unless the user typed a ticker (`keep`); region,
    sector and size stay the company's own."""
    lst = h if keep or h.type != "stock" else (sdb.top(h.group, exchanges) or h)
    alts = sdb.alternates(h.group, exclude=lst.ticker) if h.type == "stock" else []
    return {
        **_hit_fields(h),
        "ticker": lst.ticker,
        "exchange": lst.exchange,
        "exchange_name": EXCHANGE_NAMES.get(lst.exchange or "", lst.exchange),
        "why": why,
        "alternates": [
            {**a, "exchange_name": EXCHANGE_NAMES.get(a["exchange"] or "", a["exchange"])}
            for a in alts
        ],  # fmt: skip
        "values": {},
    }


def _yahoo_cards(text: str, have=(), ticker: str = "") -> list[dict]:
    """Companies Yahoo's live Lookup knows and the pack lacks, one card per
    company: its first listing (a home venue first), the others as
    alternates. ``ticker`` (an AI pick's) puts that listing first."""
    want = (ticker or "").strip().upper()
    found = sdb.yahoo_lookup(text, limit=12)
    if want and want.split(".")[0] != text.strip().upper():
        found += sdb.yahoo_lookup(
            want.split(".")[0], limit=12
        )  # Budapest's RICHT.BD is found by "RICHT"
    words = [w for w in sdb.name_key(text).split() if len(w) >= 4]
    if found and len(words) > 1 and all(sdb.relisting(h.ticker, h.exchange) for h in found):
        # Only secondary lines for the full name ("gedeon richter"): a word
        # on its own can find the home line (RICHT.BD for "richter").
        for w in words:
            found += [
                h for h in sdb.yahoo_lookup(w, limit=12) if sdb.same_company(h.name, found[0].name)
            ]
    hits = list({h.ticker: h for h in found if h.ticker not in have}.values())
    if not hits:
        return []
    hits.sort(key=lambda h: (h.ticker != want, sdb.relisting(h.ticker, h.exchange)))
    by_co: dict[str, list] = {}
    for h in hits:
        by_co.setdefault(sdb.name_key(h.name)[:12], []).append(h)
    cards = []
    for group in by_co.values():
        h, rest = group[0], group[1:]
        cards.append({
            **_hit_fields(h),
            "exchange_name": EXCHANGE_NAMES.get(h.exchange or "", h.exchange),
            "why": "From Yahoo Finance: not in the offline symbol list.",
            "alternates": [{"ticker": a.ticker, "exchange": a.exchange, "region": a.region,
                            "exchange_name": EXCHANGE_NAMES.get(a.exchange or "", a.exchange)}
                           for a in rest[:6]],
            "values": {},
        })  # fmt: skip
    return cards


def _codes(q: dict) -> list[str]:
    return [c for name in q["exchanges"] for c in EXCHANGES[name][0].split()]


def _verify_picks(picks: list[dict]) -> tuple[list[dict], int]:
    """Each AI pick must exist in the symbol pack (exact ticker, else a close
    name match), and shows as its home listing."""
    out, seen = [], set()
    for p in picks:
        h = sdb.get(p.get("ticker", "")) or next(
            iter(sdb.lookup(p.get("name", ""), limit=1, min_score=88)), None
        )
        if h is None:  # not in the pack: Yahoo must still know it, live
            # Verified like a pack pick: the AI's ticker exactly, or the
            # company it named, never Lookup's first fuzzy hit.
            want = (p.get("ticker") or "").strip().upper()
            card = next((c for c in _yahoo_cards(p.get("name", ""), seen, want)
                         if c["ticker"] == want or sdb.same_company(c["name"], p.get("name"))), None)  # fmt: skip
            if card is not None:
                seen.add(card["ticker"])
                out.append({**card, "why": p.get("why", "")})
            continue
        h = sdb.home(h.group) or h
        if h.ticker not in seen:
            seen.add(h.ticker)
            out.append(_card(h, p.get("why", "")))
    return out, len(picks) - len(out)


def _screen(q: dict) -> list[dict]:
    """Yahoo's screener for the criteria, then home listings, the requested
    regions (of the home listing: LLY.DE is not a European company), USD
    market-cap filters, and the market-cap ranking — all here."""
    import yfinance as yf
    from yfinance import EquityQuery as Q

    parts = [Q("is-in", ["region", *(q["regions"] or ALL_REGIONS)])]
    for col, vals in (
        ("sector", q["sectors"]),
        ("industry", q["industries"]),
        ("exchange", _codes(q)),
    ):
        if vals:
            parts.append(Q("is-in", [col, *vals]) if len(vals) > 1 else Q("eq", [col, vals[0]]))
    for f in q["filters"]:
        _label, _u, field, factor = FIELDS[f["field"]][:4]
        if field:
            parts.append(Q(f["op"], [field, f["value"] * factor]))
    rank = q["rank"] or {"field": "mcap", "dir": "desc"}
    sort = FIELDS[rank["field"]][2] or "intradaymarketcap"
    if rank["field"] in ("roe", "roa") and rank["dir"] == "desc":
        # A loss over negative equity is a "high" ROE on Yahoo (Z.AI, 2026-10):
        # only profitable companies can lead a return ranking.
        parts.append(Q("gt", ["netincomemargin.lasttwelvemonths", 0]))
    if rank["dir"] == "asc" and FIELDS[rank["field"]][2]:
        # Lowest first: a negative D/E or P/E is negative equity or a loss, not
        # the best of the list — Yahoo would sort those to the top.
        parts.append(Q("gte", [sort, 0]))
    quotes: list[dict] = []
    for page in range(2 if rank["field"] == "mcap" else 4 if _size_floor(q) else 1):
        args = (Q("and", parts), page * 250, sort, rank["dir"] == "asc")
        try:
            r = _yahoo_screen(*args)
        except Exception as e:  # our session refused: yfinance's shared one
            print(
                f"[search] own screener session failed ({type(e).__name__}: {e}); using yfinance's",
                flush=True,
            )
            r = yf.screen(args[0], size=250, offset=args[1], sortField=sort, sortAsc=args[3])
        quotes += r.get("quotes") or []
        if len(quotes) >= (r.get("total") or 0):
            break
    cards, groups = [], set()
    for qt in quotes:
        h = sdb.get(qt.get("symbol", ""))
        if h is None:
            continue  # not in the pack: cannot dedupe or place it — skip
        h = sdb.home(h.group) or h
        if h.group in groups or (q["regions"] and h.region not in q["regions"]):
            continue
        groups.add(h.group)
        cards.append(_card(h, exchanges=_codes(q)))
    cards = [c for c in cards if _passes(c, [f for f in q["filters"] if f["field"] == "mcap"], {})]
    if _size_floor(q):
        big = [c for c in cards if (c["mcap_usd"] or 0) >= _FLOOR_USD]
        cards = big if len(big) >= _TOP_N else cards
    if rank["field"] == "mcap":
        cards.sort(key=lambda c: (c["mcap_usd"] or 0) * (1 if rank["dir"] == "asc" else -1))
    return cards


_SCREENER = "https://query1.finance.yahoo.com/v1/finance/screener"
_SESSION: dict = {"s": None, "crumb": None}
_SESSION_LOCK = threading.Lock()


def _yahoo_screen(query, offset: int, sort: str, asc: bool) -> dict:
    """One screener page through the search's OWN Yahoo session.

    yfinance keeps one cookie + crumb for the whole process, and every 4xx
    anywhere (a delisted ticker's 404 during the startup warm-up, which fetches
    hundreds of symbols) flips its cookie strategy and wipes the cookie. For as
    long as such a burst lasts (~35 s seen) every screen sent through it gets a
    401, retries included. This session is used by nothing else, so nothing
    resets it: cookie from fc.yahoo.com, crumb from getcrumb (yfinance's own
    "basic" flow, same hosts), minted once and re-minted on a 401/403.
    Serialized: searches are rare and the session is not thread-safe."""
    from yfinance._http import new_session

    body = {"offset": offset, "size": 250, "sortField": sort, "sortType": "ASC" if asc else "DESC",
            "quoteType": "EQUITY", "query": query.to_dict(), "userId": "", "userIdType": "guid"}  # fmt: skip
    data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    params = {
        "corsDomain": "finance.yahoo.com",
        "formatted": "false",
        "lang": "en-US",
        "region": "US",
    }
    with _SESSION_LOCK:
        for attempt in range(3):
            if _SESSION["crumb"] is None:
                sess = new_session()
                with suppress(Exception):  # sets the cookie though the page is a 404
                    sess.get("https://fc.yahoo.com", timeout=10, allow_redirects=True)
                crumb = sess.get(
                    "https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10
                ).text
                if not crumb or "<" in crumb or "Too Many" in crumb:
                    raise RuntimeError("no crumb")
                _SESSION.update(s=sess, crumb=crumb)
            # Raw UTF-8, as yfinance sends it: Yahoo does not decode \u escapes,
            # so "Banks\u2014Regional" (json= default) matched nothing.
            r = _SESSION["s"].post(_SCREENER, params={**params, "crumb": _SESSION["crumb"]},
                                   data=data, headers={"Content-Type": "application/json"},
                                   timeout=20)  # fmt: skip
            if r.status_code in (401, 403):
                _SESSION["crumb"] = None  # stale: mint a new one
                time.sleep(0.5 * attempt)
                continue
            r.raise_for_status()
            return r.json()["finance"]["result"][0]
    raise RuntimeError(f"HTTP {r.status_code} after a fresh crumb")


_FLOOR_USD = 1e9


def _size_floor(q: dict) -> bool:
    """A ratio ranking over every listing is led by microcaps (a 400% ROE on a
    tiny equity base). Without a market-cap filter of the user's own, rank
    among companies above $1B — in USD from the pack: the screener's own cap is
    in local currency."""
    return (
        bool(q["rank"])
        and q["rank"]["field"] != "mcap"
        and all(f["field"] != "mcap" for f in q["filters"])
    )


def _passes(card: dict, filters: list[dict], values: dict) -> bool:
    for f in filters:
        v = card["mcap_usd"] if f["field"] == "mcap" else values.get(f["field"])
        if v is None:
            continue
        lim = f["value"]
        if not {"lt": v < lim, "lte": v <= lim, "gt": v > lim, "gte": v >= lim}[f["op"]]:
            return False
    return True


def _info(ticker: str) -> dict:
    with _LOCK:
        hit = _INFO.get(ticker)
    if hit and time.time() - hit[0] < _TTL_S * 3:
        return hit[1]
    try:
        import yfinance as yf

        info = yf.Ticker(ticker).info or {}
    except Exception:
        info = {}
    with _LOCK:
        _evict(_INFO, 500)
        _INFO[ticker] = (time.time(), info)
    return info


def _evict(cache: dict, cap: int) -> None:
    """Keep a process-global cache bounded: drop the oldest entries."""
    if len(cache) >= cap:
        for k in sorted(cache, key=lambda k: cache[k][0])[: len(cache) - cap + 1]:
            del cache[k]


def _fill_values(cards: list[dict], q: dict) -> None:
    """The card values for every criterion and the rank field, in user units
    (None where .info has no matching figure: the card shows a tick)."""
    keys = list(
        dict.fromkeys(
            [f["field"] for f in q["filters"]] + ([q["rank"]["field"]] if q["rank"] else [])
        )
    )
    # The 1-year return is on every card (with the pack's market cap).
    keys = [k for k in dict.fromkeys([*keys, "perf_52w"]) if k != "mcap" and FIELDS[k][4]]
    if not cards:
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        infos = list(pool.map(_info, [c["ticker"] for c in cards]))
    for c, info in zip(cards, infos, strict=True):
        for k in keys:
            v = info.get(FIELDS[k][4])
            c["values"][k] = round(v * FIELDS[k][5], 4) if isinstance(v, (int, float)) else None


def _chips(q: dict) -> list[dict]:
    """The query as the chips the page shows, in reading order."""
    chips = [{"kind": "sector", "label": v} for v in q["sectors"]]
    chips += [{"kind": "industry", "label": v} for v in q["industries"]]
    if q["regions"]:
        chips.append({"kind": "regions", "label": "Region: " + _region_label(q["regions"])})
    chips += [{"kind": "exchange", "label": "Exchange: " + x} for x in q["exchanges"]]
    chips += [
        {"kind": "type", "label": t.upper() if t == "etf" else t.title() + "s"} for t in q["types"]
    ]
    chips += [{"kind": "filter", "label": _chip(f), "i": i} for i, f in enumerate(q["filters"])]
    if q["rank"]:
        r = q["rank"]
        tip = [r.get("why") or ""] + (["among companies above $1B"] if _size_floor(q) else [])
        chips.append({"kind": "rank", "ai": r["by"] == "ai", "title": " · ".join(t for t in tip if t),
                      "label": f"Rank: {FIELDS[r['field']][0]}{' ↑' if r['dir'] == 'asc' else ''}"})  # fmt: skip
    if q["picks"]:
        chips.append({"kind": "picks", "ai": True, "label": "AI picks"})
    chips += [{"kind": "ignored", "label": x} for x in q["ignored"]]
    return chips


def _region_label(codes: list[str]) -> str:
    """'Europe' for the European codes, 'US, Europe' for both, else the codes."""
    rest, names = set(codes), []
    for group, spec in sorted(REGIONS.items(), key=lambda kv: -len(kv[0])):
        g = set(group.split())
        if len(g) > 1 and g <= rest:
            names.append(spec.split("|")[0].title())
            rest -= g
    return ", ".join([c.upper() for c in sorted(rest)] + names)


def search(text: str = "", query: dict | None = None, offset: int = 0, limit: int = _TOP_N) -> dict:
    """The /api/search entry: free text (parsed) or an edited query (not)."""
    q = validate(query) if query is not None else parse(text)
    return run(q, max(0, int(offset)), int(limit))
