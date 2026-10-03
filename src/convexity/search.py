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
from dataclasses import asdict

from convexity import symbol_db as sdb

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
    "de": ("D/E", "x", "totaldebtequity.lasttwelvemonths", 100, "debtToEquity", 0.01, "d/e|de|debt to equity|debt/equity|debt-to-equity"),
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
_LARGEST = re.compile(r"\b(largest|biggest|mega[- ]?caps?|large[- ]?caps?)\b")
_FILLER = set(
    re.findall(
        r"\w+",
        "a an the and or with without of in on at for from by to that which who whose have has having"
        " are is be show me find list give get all any some companies company stocks stock shares"
        " names firms businesses listed based headquartered domiciled ratio than",
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
            "regions": [], "types": [], "rank": None, "picks": [], "ignored": [],
            "notes": [], "engine": "rules"}  # fmt: skip


def validate(q) -> dict:
    """Clean an untrusted query (the LLM's, or a chip edit from the page):
    unknown fields, operators or vocabulary move to `ignored`, never through."""
    out = empty(str((q or {}).get("text", ""))[:300])
    if not isinstance(q, dict):
        return out
    out["kind"] = q.get("kind") if q.get("kind") in ("name", "screen", "theme") else "name"
    out["engine"] = "ai" if q.get("engine") == "ai" else "rules"
    out["ignored"] = [str(x)[:80] for x in q.get("ignored") or [] if str(x).strip()][:10]
    out["notes"] = [str(x)[:160] for x in q.get("notes") or []][:10]
    for f in q.get("filters") or []:
        ok = isinstance(f, dict) and f.get("field") in FIELDS and f.get("op") in OPS
        v = f.get("value") if ok else None
        if ok and isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
            out["filters"].append({"field": f["field"], "op": f["op"], "value": float(v)})
        else:
            out["ignored"].append(_chip(f) if ok else f"filter {str(f)[:40]}")
    for key, vocab in (("sectors", SECTORS), ("industries", INDUSTRIES), ("regions", ALL_REGIONS),
                       ("types", sdb.TYPES)):  # fmt: skip
        vals = [v for v in q.get(key) or [] if isinstance(v, str)]
        out[key] = sorted(v for v in set(vals) if v in vocab)
        out["ignored"] += [v[:40] for v in vals if v not in vocab]
    r = q.get("rank")
    if isinstance(r, dict) and r.get("field") in FIELDS and r.get("dir") in ("asc", "desc"):
        out["rank"] = {"field": r["field"], "dir": r["dir"], "by": r.get("by") if r.get("by") in ("rules", "ai", "default") else "rules"}  # fmt: skip
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
    if unit == "%" and suffix != "%" and 0 < abs(v) <= 1:
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
    for spec, (key, vals) in SYNONYMS.items():
        if take(_words(spec)):
            q[key] += [v for v in vals if v not in q[key]]
    for key, vocab in (("sectors", SECTORS), ("industries", INDUSTRIES)):
        for v in vocab:
            if take(_words(_fold(v))):
                q[key].append(v)
    for codes, spec in REGIONS.items():
        if take(_words(spec)):
            q["regions"] += [c for c in codes.split() if c not in q["regions"]]
    for spec, typ in TYPES.items():
        if take(_words(spec)):
            q["types"].append(typ)
    vague = bool(_VAGUE.search(s))
    s = _VAGUE.sub(" ", s)
    left = [w for w in re.findall(r"[\w\-&.'/]+", s) if w not in _FILLER and re.search(r"\w", w)]
    structured = q["filters"] or q["sectors"] or q["industries"] or q["regions"] or q["types"]
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
- kind "screen": filters/sectors/industries/regions narrow a screener.
- kind "theme": the request names a theme no sector or industry captures
  (a drug class, a technology, a supply chain). Then list up to 12 real, listed
  companies in "picks" (ticker as on Yahoo Finance, company name, why in under
  8 words), most relevant first, plus any filters the user gave.
- kind "name": the user typed a company name; put it in picks.
- rank: when the user asks for "best"/"top" without a metric, choose the metric
  that best fits the request and say why in rank.why (under 10 words).
- Anything you cannot express with the schema goes, verbatim, into "ignored".
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
        "rank": obj({"field": {"type": "string", "enum": keys}, "dir": {"type": "string", "enum": ["asc", "desc"]}, "why": s}),
        "picks": arr(obj({"ticker": s, "name": s, "why": s})),
        "ignored": arr(s),
    })  # fmt: skip


def ai_available() -> bool:
    from convexity import news_sentiment as ns

    return bool(ns.NVIDIA_API_KEY)


def parse_llm(text: str, hint: dict) -> dict | None:
    """The NIM model's reading of `text`, validated; None when it failed."""
    from convexity import news_sentiment as ns

    user = f"Request: {text}\nA rule parser already read: {json.dumps(_brief(hint))}"
    raw = ns._nvidia_call(_SYSTEM, user, (), schema=_llm_schema(), name="company_search",
                          record=False, tag="search")  # fmt: skip
    if not isinstance(raw, dict):
        return None
    why = (raw.get("rank") or {}).get("why")
    q = validate(
        {**raw, "text": text, "engine": "ai", "rank": {**(raw.get("rank") or {}), "by": "ai"}}
    )
    if q["rank"] and why:
        q["notes"].append(f"Ranked by {FIELDS[q['rank']['field']][0]}: {str(why)[:80]}")
    return q


def _brief(q: dict) -> dict:
    return {
        k: q[k] for k in ("filters", "sectors", "industries", "regions", "types", "rank") if q[k]
    }


def parse(text: str) -> dict:
    """Rules first; the LLM only when a phrase is left over, the query reads
    as a theme, or "best" needs a metric — and only with a key."""
    q, left = parse_rules(text)
    needs_ai = bool(left) or (q["rank"] or {}).get("by") == "default"
    if q["kind"] == "name":
        top = sdb.lookup(q["text"], limit=1)
        needs_ai = len(q["text"].split()) >= 3 and (not top or top[0].score < 85)
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
        q["kind"] = q["kind"] if q["kind"] != "name" else "theme"
    if (q["rank"] or {}).get("by") == "default" and not ai_available():
        q["notes"].append(
            '"best" is not a metric, so this is ranked by market cap. With an NVIDIA key the AI picks the ranking.'
        )
    return q


# =================================================================== executor
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_INFO: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()
_TTL_S = 600.0


def run(q: dict, offset: int = 0) -> dict:
    """Execute a validated query: the next five results + warnings."""
    t0 = time.time()
    # The text only matters to a name search; a screen is its parsed query.
    skip = ("notes",) if q["kind"] == "name" else ("notes", "text")
    key = json.dumps({k: v for k, v in q.items() if k not in skip}, sort_keys=True)
    with _LOCK:
        hit = _CACHE.get(key)
    base = hit[1] if hit and time.time() - hit[0] < _TTL_S else None
    warnings = [f'Ignored "{x}": not something Convexity can search on.' for x in q["ignored"]]
    if base is None:
        if q["kind"] == "name" and not q["picks"]:
            base = [_card(h) for h in sdb.lookup(q["text"], limit=25)]
        elif q["kind"] in ("name", "theme"):
            base, dropped = _verify_picks(q["picks"])
            if q["regions"]:  # "... in Europe": the company's home, not the AI's say-so
                base = [c for c in base if c["region"] in q["regions"]]
            if q["filters"]:  # the AI's picks still have to pass the user's criteria
                _fill_values(base, q)
                base = [c for c in base if _passes(c, q["filters"], c["values"])]
            if dropped:
                warnings.append(
                    f"{dropped} AI-named compan{'y' if dropped == 1 else 'ies'} not found, dropped."
                )
            if not q["picks"]:
                warnings.append("Theme search needs an NVIDIA key (Settings → API keys).")
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
            base = [_card(h) for h in sdb.category(sectors=q["sectors"], industries=q["industries"],
                    regions=q["regions"], types=q["types"] or ("stock",), limit=250)]  # fmt: skip
            base = [c for c in base if _passes(c, q["filters"], {})]
        with _LOCK:
            _CACHE[key] = (time.time(), base)
    page = base[offset : offset + _TOP_N]
    _fill_values(page, q)
    if not base:
        warnings.append("No matches." if sdb.status()["installed"] else
                        "The symbol list is still downloading; try again in a minute.")  # fmt: skip
    print(f"[search] {q['text']!r} engine={q['engine']} kind={q['kind']} query={json.dumps(_brief(q))}"
          f" results={len(base)} {time.time() - t0:.1f}s", flush=True)  # fmt: skip
    return {"query": q, "chips": _chips(q), "results": page, "total": len(base),
            "offset": offset, "warnings": warnings, "notes": q["notes"], "fields": _LABELS}  # fmt: skip


_LABELS = {k: v[:2] for k, v in FIELDS.items()}  # the page formats values with these


def _card(h: sdb.Hit, why: str = "") -> dict:
    alts = sdb.alternates(h.group, exclude=h.ticker) if h.type == "stock" else []
    return {
        **{k: v for k, v in asdict(h).items() if k != "score"},
        "why": why,
        "alternates": alts,
        "values": {},
    }


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
    for col, vals in (("sector", q["sectors"]), ("industry", q["industries"])):
        if vals:
            parts.append(Q("is-in", [col, *vals]) if len(vals) > 1 else Q("eq", [col, vals[0]]))
    for f in q["filters"]:
        _label, _u, field, factor = FIELDS[f["field"]][:4]
        if field:
            parts.append(Q(f["op"], [field, f["value"] * factor]))
    rank = q["rank"] or {"field": "mcap", "dir": "desc"}
    sort = FIELDS[rank["field"]][2] or "intradaymarketcap"
    quotes: list[dict] = []
    for page in range(2 if rank["field"] == "mcap" else 1):
        r = yf.screen(
            Q("and", parts),
            size=250,
            offset=page * 250,
            sortField=sort,
            sortAsc=rank["dir"] == "asc",
        )
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
        cards.append(_card(h))
    cards = [c for c in cards if _passes(c, [f for f in q["filters"] if f["field"] == "mcap"], {})]
    if rank["field"] == "mcap":
        cards.sort(key=lambda c: (c["mcap_usd"] or 0) * (1 if rank["dir"] == "asc" else -1))
    return cards


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
        _INFO[ticker] = (time.time(), info)
    return info


def _fill_values(cards: list[dict], q: dict) -> None:
    """The card values for every criterion and the rank field, in user units
    (None where .info has no matching figure: the card shows a tick)."""
    keys = list(
        dict.fromkeys(
            [f["field"] for f in q["filters"]] + ([q["rank"]["field"]] if q["rank"] else [])
        )
    )
    keys = [k for k in keys if k != "mcap" and FIELDS[k][4]]
    if not keys or not cards:
        return
    with ThreadPoolExecutor(max_workers=_TOP_N) as pool:
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
    chips += [
        {"kind": "type", "label": t.upper() if t == "etf" else t.title() + "s"} for t in q["types"]
    ]
    chips += [{"kind": "filter", "label": _chip(f), "i": i} for i, f in enumerate(q["filters"])]
    if q["rank"]:
        r = q["rank"]
        chips.append({"kind": "rank", "ai": r["by"] == "ai",
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


def search(text: str = "", query: dict | None = None, offset: int = 0) -> dict:
    """The /api/search entry: free text (parsed) or an edited query (not)."""
    q = validate(query) if query is not None else parse(text)
    return run(q, max(0, int(offset)))
