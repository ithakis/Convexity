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
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict

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
    "beta": ("Beta", "x", "beta", 1, "beta", 1, "beta"),
    "perf_52w": ("52-week change", "%", "fiftytwowkpercentchange", 1, "52WeekChange", 100, "52 week change|52-week change|1 year return"),
    "short_float": ("Short % of float", "%", "short_percentage_of_float.value", 1, "shortPercentOfFloat", 100, "short interest|short float"),
    # Market cap: the screener's is in local currency, so it never goes there.
    "mcap": ("Market cap", "$", None, 1, None, 1, "market cap|market capitalization|mcap"),
}
# fmt: on
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
                       "PNK": "OTC", "NAS": "Nasdaq"})  # fmt: skip
# "low leverage", "high dividend yield": a moderate threshold the chip shows
# (and the user can edit), never a lowest-first ranking — "small leverage"
# ranked by lowest D/E returned companies with no debt at all.
_QUAL = {"de": (0.5, 1.5), "pe": (15, 30), "pb": (1.5, 4), "beta": (0.8, 1.3),
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
    out = empty(str((q or {}).get("text", ""))[:300])
    if not isinstance(q, dict):
        return out
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
  high margin > 15, high growth > 15, low beta < 0.8.
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
    s = {"type": "string"}
    arr = lambda items: {"type": "array", "items": items}  # noqa: E731
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
        "picks": arr(obj({"ticker": s, "name": s, "why": s})),
        "ignored": arr(s),
    })  # fmt: skip


def ai_available() -> bool:
    from convexity import news_sentiment as ns

    return bool(ns.NVIDIA_API_KEY)


_AI_DEADLINE_S = 25.0


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
    try:
        raw = ns._nvidia_call(_SYSTEM, user, (), Deadline(_AI_DEADLINE_S), schema=_llm_schema(),
                              name="company_search", record=False, tag="search", timeout=20.0)  # fmt: skip
    except Cancelled:
        print("[search] AI step timed out; using the rule parser's reading", flush=True)
        return None
    if not isinstance(raw, dict):
        return None
    # The schema makes the model always send a rank; only a screen uses one.
    rank = {**(raw.get("rank") or {}), "by": "ai"} if raw.get("kind") == "screen" else None
    return _sane_rank(validate({**raw, "text": text, "engine": "ai", "rank": rank}))


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


def _parts(text: str) -> list[str]:
    return _ticker_run(text) or [p.strip() for p in re.split(r"[,;\n]+", text) if p.strip()]


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
    parts = _parts(text)
    if len(parts) >= 2 and not q["filters"] and all(len(p.split()) <= 5 for p in parts):
        return {**empty(q["text"]), "kind": "list"}
    if left and not q["filters"]:
        # "deutsche bank", "bank of america", "american express": category
        # words inside a company name. A near-exact name wins over the parse.
        top = sdb.lookup(q["text"], limit=1)
        if top and top[0].score >= 85:
            return empty(q["text"])
    needs_ai = bool(left) or (q["rank"] or {}).get("by") == "default"
    if q["kind"] == "name":
        # Two words are enough to be a description: "cybersecurity companies"
        # best-matched The TJX Companies at 79.5 and was shown as a name. A
        # real name or a typo of one scores 85+ ("tjx companies" 100, "novo
        # nordsk" 89.7, "microsft" 88.1); single words stay name lookups.
        top = sdb.lookup(q["text"], limit=1)
        needs_ai = len(q["text"].split()) >= 2 and (not top or top[0].score < 85)
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
_LIST_MAX = 50


def _run_list(q: dict, t0: float) -> dict:
    """A pasted list: each part as typed (an exact ticker stays that listing),
    else its best name match; all on one page. Not cached: lookups are."""
    cards, seen, missing = [], set(), []
    for part in _parts(q["text"])[:_LIST_MAX]:
        h = sdb.get(part) or next(iter(sdb.lookup(part, limit=1)), None)
        if h is None:
            missing.append(part)
        elif h.ticker not in seen:
            seen.add(h.ticker)
            cards.append(_card(h, keep=h.ticker == part.upper()))
    warnings = [f"Not found: {', '.join(missing)}."] if missing else []
    if not cards and not sdb.status()["installed"]:
        warnings = ["The symbol list is still downloading; try again in a minute."]
    print(f"[search] list of {len(_parts(q['text']))} engine=rules kind=list"
          f" results={len(cards)} missing={len(missing)} {time.time() - t0:.1f}s", flush=True)  # fmt: skip
    return {"query": q, "chips": [], "results": cards, "total": len(cards), "offset": 0,
            "warnings": warnings, "notes": [], "fields": _LABELS}  # fmt: skip


def _card(h: sdb.Hit, why: str = "", keep: bool = False, exchanges=()) -> dict:
    """One result. A company shows (and Add adds) its most traded listing —
    NVO for Novo Nordisk — unless the user typed a ticker (`keep`); region,
    sector and size stay the company's own."""
    lst = h if keep or h.type != "stock" else (sdb.top(h.group, exchanges) or h)
    alts = sdb.alternates(h.group, exclude=lst.ticker) if h.type == "stock" else []
    return {
        **{k: v for k, v in asdict(h).items() if k != "score"},
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
        if h is None:
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
