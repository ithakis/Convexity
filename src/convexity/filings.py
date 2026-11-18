"""SEC filings for the stock window's earnings reports (Phase 5, Feature 7).

WHY a module of its own
-----------------------
The E card (`openEventCard`) and the Earnings history pop-up link each report
to its SEC filings: the results 8-K (a foreign filer's 6-K) and the quarter's
10-Q / 10-K (20-F / 40-F). That is a concern of its own, with rules of its
own: filings matched by TIME, a URL allow-list, a cache and degraded-answer
policy and a throttle on a quota the news refresh shares. `fetcher` stays
Yahoo; the Finnhub transport and its cache are `finnhub_adapter.sec_filings`
(in `_FH_CACHE`, so `reload_keys()` drops them with the key); this module
composes them. Research and the numbers behind every constant:
`docs/architecture/backend.md` "Earnings filings".

The rules that are load-bearing
-------------------------------
* **By time, not by Item.** Finnhub's filings carry no Items (no "2.02") and
  no exhibit list. Results 8-Ks are accepted minutes after the release
  (AAPL 16:30 for a 16:00 release, P&G 07:02), so the results filing is the
  first 8-K accepted in [report time - 3 h, + 3 days] (a 6-K: + 7 days, SAP
  files its quarterly 6-K 1-6 days late). Same-day ties prefer a file name
  that looks like results; names that plainly aren't (remuneration report,
  AGM, buy-back...) are skipped. 281/282 reports in the research.
* **The periodic form follows the fiscal quarter**: Q1-Q3 the first 10-Q
  filed in [report - 3 d, min(next report, + 75 d)), Q4 the first 10-K in
  [report - 3 d, + 130 d); a foreign filer's Q4 the 20-F (40-F), its
  Q1-Q3 the 6-K itself. A plain "first periodic report after" gave TSM's
  20-F (filed on its Q1 results day) to Q1. Not filed yet and still inside
  its window: `pending`, with a "usually by" date (large accelerated
  filers' deadlines: an estimate, and the page says so).
* **The fiscal label** ("Q3 FY2026") comes from Yahoo's `lastFiscalYearEnd`
  of the primary line: the last quarter end on that grid at least 7 days
  before the report, the year named by the calendar year it ends in. A
  degraded `.info` gives no label, never a guessed one.
* **The SEC line is the company's US line** (`symbol_db.us_line`): Finnhub's
  free plan answers US listings only, and a foreign company files through
  its ADR (SAP.DE -> SAP, APC.DE -> AAPL).
* **A failed call is never "none"** (the 2026-10-30 rule): `unavailable`,
  nothing cached. A genuine "no filings at all" (an unsponsored ADR) is
  cached a day. No key is a flag the page explains.
* **The app never fetches sec.gov** (it answers automated clients 403). It
  only hands the owner's browser links, each checked against the EDGAR
  allow-list first: Finnhub is a third party and these open in a real
  browser.
* **Never on the window's opening path**: the page asks on the first hover
  of an E flag, on opening the card, or on the Earnings history button.
  Cold lookups are single-flight per line and capped per minute.
"""

import contextlib
import re
import threading
import time
from collections import deque
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from convexity import finnhub_adapter as _fh
from convexity.helpers import Deadline

N_REPORTS = 12  # the table's rows, newest first on the page
YEARS = 4  # Yahoo's calendar has holes and stale stubs (TM jumps 2014 -> 2025)
HOT_TTL = 1800.0  # while the latest report is under a week old: its 8-K, then its 10-Q
OK_TTL = 12 * 3600.0
FRESH_DAYS = 7
# Cold lookups (3 Finnhub calls each) per rolling minute: 8 is at most ~32 of
# the free plan's 60, which the news refresh and the quotes build share.
COLD_PER_MIN = 8
THROTTLE_WAIT_S = 12.0
DEADLINE_S = 25.0  # a whole cold lookup, the limiter's waits included

_NY = ZoneInfo("America/New_York")
# File names that are plainly not results (a foreign filer's same-day 6-Ks).
_NOT_RESULTS = re.compile(
    r"(remunerat|agm|annual.?general|proxy|buy.?back|repurchas|own.?shares|transaction|"
    r"director|notice|dividend|bond|notes?due|prospectus|monthly|revenue2|board)",
    re.IGNORECASE,
)
_RESULTS = re.compile(
    r"(q[1-4]|quarter|result|earn|interim|half|h1|fy20|6-?k.*q|financ)", re.IGNORECASE
)

# ------------------------------------------------------------ URL allow-list
_PERIOD_IN_NAME = re.compile(r"-(20\d\d)(\d\d)(\d\d)\.htm$")
_SEC_ARCHIVE = re.compile(
    r"^https://www\.sec\.gov/Archives/edgar/data/[0-9]{1,10}/[0-9A-Za-z._/-]+$"
)
_LIST_FORMS = {"", "8-K", "6-K"}


def sec_url(u) -> str | None:
    """``u`` when it is a plain EDGAR archive link (the primary document or
    the filing index), else None. No query, fragment, port, credentials or
    ``..``: these open in the owner's own browser."""
    if not isinstance(u, str) or len(u) > 300 or ".." in u:
        return None
    return u if _SEC_ARCHIVE.match(u) else None


def edgar_list_url(cik: str, form: str = "", before: str = "") -> str | None:
    """EDGAR's own filing list for the company (one form, up to a day), built
    here from a checked CIK: the fallback when Finnhub lacks a filing (NKE's
    2026-06-30 8-K), where EDGAR shows each 8-K with its Items."""
    if (
        not (isinstance(cik, str) and cik.isdigit() and 0 < len(cik) <= 10)
        or form not in _LIST_FORMS
    ):
        return None
    if before and not re.fullmatch(r"\d{8}", before):
        return None
    return (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={int(cik):010d}&type={form}&dateb={before}&owner=include&count=40"
    )


def website_url(u) -> str | None:
    """The company's website (Yahoo ``website``, never ``irWebsite``: rare,
    http and stale) when it is a plain https link, else None."""
    if (
        not isinstance(u, str)
        or len(u) > 300
        or any(c in u for c in " \"'<>\\`")
        or not u.isprintable()
    ):
        return None
    try:
        p = urlsplit(u)
    except ValueError:
        return None
    if p.scheme != "https" or not p.hostname or p.username or p.password or "." not in p.hostname:
        return None
    return u


# ------------------------------------------------------------ fiscal labels
def fiscal_year_end(info: dict) -> date | None:
    """Yahoo's ``lastFiscalYearEnd`` (epoch seconds) as a date, or None, also
    for a degraded ``.info``: a label is never guessed."""
    if not info or info.get("_fundamentals_missing"):
        return None
    v = info.get("lastFiscalYearEnd")
    try:
        return datetime.fromtimestamp(float(v), UTC).date() if v else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _fy_month(fye: date) -> int:
    """The month a fiscal year ends in. A 52/53-week year ending in a month's
    first week (2026-01-03, HD's 2026-02-01) belongs to the month before."""
    return fye.month if fye.day > 7 else (fye.month - 2) % 12 + 1


def _month_end(fye: date) -> bool:
    return (fye + timedelta(days=1)).day == 1


def _snap(end: date, fye: date) -> tuple[date, bool]:
    """(the quarter's real end, exact?). A month-end year has month-end
    quarters. A 52/53-week year's quarters are 13 weeks from its last year
    end (CSCO's 2026-07-25 -> 2026-10-24, not Oct 31: "usually by" was a week
    late), close but not exact: a 53-week year's 14-week quarter shifts the
    grid a week, so this date only ever feeds the "usually by" estimate."""
    if _month_end(fye):
        return end, True
    cand = fye + timedelta(days=91 * round((end - fye).days / 91))
    return (cand if abs((cand - end).days) <= 10 else end), False


def fiscal(report_day: date, fye: date | None, naming: str | None = None) -> dict:
    """{quarter, fiscal_year, label, period_end, period_exact} of a report.

    The quarter is the last quarter end on the fiscal-year grid at least 7
    days before the report. The fiscal YEAR's name is the company's own
    convention, which has no universal rule for years ending in January to
    March: WMT and NVDA name theirs by the end (FY2026 ends Jan 2026), HD,
    TGT and LOW by the start (HD's fiscal 2025 ends 1 Feb 2026). For those,
    ``naming`` is what the company's own 10-Ks say ("end" / "start",
    `fiscal_naming`); unknown, there is no fiscal year and no label, and the
    page names the quarter by its end instead: never a guessed year. Any
    other year end is named by the calendar year it ends in (Apple's Sep,
    MSFT's Jun, Oracle's "fiscal 2027" for Jun 2026 - May 2027): 115 of 116
    US company-quarters agreed with Finnhub's XBRL labels."""
    out = {
        "quarter": None,
        "fiscal_year": None,
        "label": None,
        "period_end": None,
        "period_exact": False,
    }
    if fye is None:
        return out
    m0 = _fy_month(fye)
    y, m = report_day.year, report_day.month
    for _ in range(15):
        end = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        if (m - m0) % 3 == 0 and end <= report_day - timedelta(days=7):
            q = ((m - m0 - 1) % 12) // 3 + 1
            fy: int | None = y + (1 if m > m0 else 0)
            if m0 in AMBIGUOUS_FY_MONTHS:
                fy = fy if naming == "end" else fy - 1 if naming == "start" else None
            real, exact = _snap(end, fye)
            out.update(quarter=q, fiscal_year=fy, period_end=real.isoformat(), period_exact=exact)
            out["label"] = f"Q{q} FY{fy}" if fy else None
            return out
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def label_rows(rows: list[dict], fye: date | None, naming: str | None = None) -> list[dict]:
    """Earnings rows (``fetch_events``) with their `fiscal` fields."""
    out = []
    for e in rows:
        try:
            parts = fiscal(date.fromisoformat(e["date"]), fye, naming)
        except (KeyError, TypeError, ValueError):
            parts = fiscal(date.today(), None)
        out.append({**e, **parts})
    return out


# ------------------------------------------------------------ fiscal naming
AMBIGUOUS_FY_MONTHS = (1, 2, 3)
NAMING_TTL = 90 * 86400.0  # a company doesn't rename its years
NAMING_MISS_TTL = 7 * 86400.0  # no 10-K rows (a 20-F filer) or 10-Ks that disagree
_NAMING_LOCK = threading.Lock()


def _naming_path():
    from convexity import paths

    return paths.state_file("fiscal_naming")


def _naming_load() -> dict:
    import json

    try:
        d = json.loads(_naming_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def fiscal_naming(line: str | None) -> str | None:
    """How the company names its fiscal years ("end" / "start"), as its own
    10-Ks said, from ``state/fiscal_naming.json``; "" when they couldn't say
    (no 10-K rows, or rows that disagree); None when not asked yet. Disk only:
    the window's events read it, never ask."""
    if not line:
        return None
    hit = _naming_load().get(line)
    if not (isinstance(hit, list) and len(hit) == 2 and hit[1] in ("end", "start", "")):
        return None
    ttl = NAMING_TTL if hit[1] else NAMING_MISS_TTL
    return hit[1] if time.time() - float(hit[0]) < ttl else None


def learn_naming(line: str) -> str | None:
    """Ask the company's 10-Ks how it names its fiscal years: Finnhub's
    reported annual financials carry each 10-K's fiscal year (the filer's
    own, HD 2025 for the year to 2026-02-01, WMT 2026 for 2026-01-31) and
    its end date. One call, 0.5-0.9 s and ~200 KB (`from` is ignored),
    asked only for years ending in January to March, beside the filings
    lookup (never on the window's path), kept 90 days. The last three 10-Ks
    must agree. A failed call is not an answer and is not kept."""
    raw = _fh._fh_call(
        "stock/financials-reported",
        {"symbol": line, "freq": "annual"},
        line,
        timeout=15.0,
        cancel=Deadline(DEADLINE_S),
    )
    if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
        return None
    rows = []
    for d in raw["data"]:
        with contextlib.suppress(KeyError, TypeError, ValueError):
            if d.get("form") == "10-K" and isinstance(d.get("year"), int):
                rows.append((date.fromisoformat(str(d["endDate"])[:10]), d["year"]))
    votes = set()
    for end, year in sorted(rows, reverse=True)[:3]:
        ref = end - timedelta(days=7) if end.day <= 7 else end
        votes.add("end" if year == ref.year else "start" if year == ref.year - 1 else "?")
    val = votes.pop() if len(votes) == 1 and "?" not in votes else ""
    from convexity.persistence import _atomic_write

    with _NAMING_LOCK:
        store = _naming_load()
        store[line] = [time.time(), val]
        try:
            path = _naming_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            import json

            _atomic_write(path, json.dumps(store, indent=1, sort_keys=True))
        except OSError as e:
            print(f"[filings] {line}: fiscal naming not saved ({type(e).__name__})")
    print(f"[filings] {line}: fiscal years named by their {val or 'unknown'}")
    return val


def _sec_line(symbol: str) -> str | None:
    """The company's SEC line in the pack's spelling (BRK-B): Finnhub's
    /stock/filings answers class shares only by the dash (BRK.B: 0 rows,
    BRK-B: 6), unlike its other endpoints (`finnhub_symbol`'s BRK.B)."""
    line = _fh.finnhub_symbol(symbol)
    return line.replace(".", "-") if line else None


def _on_us_exchange(line: str) -> bool:
    """Whether the pack has ``line`` on a US exchange (NYSE, Nasdaq...), or
    doesn't know it. Every exchange-listed company files with the SEC, so an
    empty answer for one is Finnhub's gap or spelling, never "not filed";
    only an OTC line (an unsponsored ADR: RNMBY) can truly have nothing."""
    with contextlib.suppress(Exception):
        from convexity import symbol_db

        hit = symbol_db.get(line)
        if hit is not None:
            return hit.exchange in symbol_db._US_EXCHANGES
    return True


def _is_fund(symbol: str) -> bool:
    with contextlib.suppress(Exception):
        from convexity import symbol_db

        hit = symbol_db.get(symbol)
        return bool(hit) and hit.type != "stock"
    return False


def _primary(symbol: str) -> str:
    with contextlib.suppress(Exception):
        from convexity import capm

        return capm.primary_line(symbol)
    return symbol


def events(symbol: str) -> dict:
    """``fetch_events`` (the window's E/D flags) with each report's fiscal
    label. The labels are added on a copy at response time, so a degraded
    ``.info`` never sits in the 6-hour events cache as "no label". The
    primary line's ``.info`` (cached, `fetcher._research_info`) is asked in
    parallel with Yahoo's calendar: no Finnhub call, no extra wait."""
    from convexity.fetcher import _research_info, fetch_events

    primary = _primary(symbol)
    box: dict = {}
    t = None
    if not _is_fund(symbol):
        t = threading.Thread(
            target=lambda: box.setdefault("info", _research_info(primary)), daemon=True
        )
        t.start()
    ev = fetch_events(symbol)
    if t is not None:
        t.join(timeout=15)
    fye = fiscal_year_end(box.get("info") or {})
    naming = (
        fiscal_naming(_sec_line(symbol)) if fye and _fy_month(fye) in AMBIGUOUS_FY_MONTHS else None
    )
    out = dict(ev)
    out["earnings"] = label_rows(ev.get("earnings") or [], fye, naming)
    out["fiscal_year_end"] = fye.isoformat() if fye else None
    return out


# ------------------------------------------------------------ the reports
def past_reports(rows: list[dict], today: date) -> list[dict]:
    """The reports the table shows, oldest first: out (before today, or today
    once Yahoo has the figure; a report due today is "next"), from the last
    YEARS years, at most N_REPORTS. app.js `ehRows` applies the same rule."""
    t = today.isoformat()
    cut = (today - timedelta(days=int(YEARS * 365))).isoformat()
    past = [
        e
        for e in rows
        if isinstance(e.get("date"), str)
        and (e["date"] < t or (e["date"] == t and e.get("actual") is not None))
        and e["date"] >= cut
    ]
    return past[-N_REPORTS:]


def _acc(f: dict) -> datetime:
    return datetime.fromisoformat(f["acceptedDate"])


def _report_time(e: dict, day: date) -> datetime:
    """The report's New York wall time; midnight when Yahoo gave no time."""
    if e.get("timing") and e.get("ts"):
        with contextlib.suppress(ValueError, TypeError):
            return datetime.fromisoformat(e["ts"]).astimezone(_NY).replace(tzinfo=None)
    return datetime.combine(day, datetime.min.time())


def _usable(f: dict) -> bool:
    try:
        _acc(f)
        date.fromisoformat(f["filedDate"][:10])
    except (KeyError, TypeError, ValueError):
        return False
    return bool(sec_url(f.get("reportUrl")))


def _filing(f: dict) -> dict:
    """The structured fields the page reads; all copy is the page's."""
    acc = _acc(f).replace(tzinfo=_NY)
    return {
        "form": f["form"],
        "filed": f["filedDate"][:10],
        "accepted": acc.isoformat(),
        "url": sec_url(f.get("reportUrl")),
        "index_url": sec_url(f.get("filingUrl")),
    }


_DUE_DAYS = {"10-Q": 40, "10-K": 60, "20-F": 120, "40-F": 90}


def match(
    reports: list[dict],
    filer: str,
    got: dict,
    fye: date | None,
    today: date,
    cik: str | None,
    naming: str | None = None,
) -> list[dict]:
    """Each report with its filings (oldest first, as given). Pure: the
    rules are in the module docstring; tests/test_filings.py pins them."""
    got = {k: [f for f in v if _usable(f)] for k, v in got.items()}
    out = []
    for i, e in enumerate(reports):
        rd = date.fromisoformat(e["date"])
        ts = _report_time(e, rd)
        nxt = reports[i + 1]["date"] if i + 1 < len(reports) else "9999-12-31"
        parts = fiscal(rd, fye, naming)
        q, pend = parts["quarter"], parts["period_end"]
        ef = pf = None
        if filer in ("us", "foreign"):
            form = "8-K" if filer == "us" else "6-K"
            span = timedelta(days=3 if filer == "us" else 7)
            c = sorted(
                (
                    f
                    for f in got.get(form, [])
                    if ts - timedelta(hours=3) <= _acc(f) <= ts + span
                    and not _NOT_RESULTS.search(f["reportUrl"].rsplit("/", 1)[-1])
                ),
                key=_acc,
            )
            if c:
                day0 = _acc(c[0]).date()
                same = [f for f in c if _acc(f).date() == day0]
                pick = next((f for f in same if _RESULTS.search(f["reportUrl"])), same[0])
                ef = {
                    **_filing(pick),
                    "days_after": (_acc(pick).date() - rd).days,
                    "others_that_day": len(same) - 1,
                }
            if q == 4 or q is None:
                per_forms = ("10-K",) if filer == "us" else ("20-F", "40-F")
                hi_days = 130
            else:
                per_forms = ("10-Q",) if filer == "us" else ()
                hi_days = 75
            if q is None and filer == "us":
                per_forms = ("10-Q", "10-K")
            lo = (rd - timedelta(days=3)).isoformat()
            hi = (rd + timedelta(days=hi_days)).isoformat()
            if q != 4:
                hi = min(hi, nxt)
            per = sorted(
                (f for k in per_forms for f in got.get(k, []) if lo <= f["filedDate"][:10] < hi),
                key=lambda f: f["filedDate"],
            )
            if per:
                pf = {"kind": "periodic", **_filing(per[0])}
                # The report's own file name carries its period (hd-20250803.htm,
                # EDGAR's inline-XBRL naming): the exact end of a 52/53-week quarter.
                dm = _PERIOD_IN_NAME.search(per[0]["reportUrl"])
                with contextlib.suppress(ValueError):
                    exact = date(int(dm[1]), int(dm[2]), int(dm[3])) if dm else None
                    if exact and pend and abs((exact - date.fromisoformat(pend)).days) <= 14:
                        parts.update(period_end=exact.isoformat(), period_exact=True)
            elif per_forms and q is not None and pend and today.isoformat() < hi:
                # Still inside its window: not filed yet. A 40-F filer is
                # one whose annual report came as a 40-F (CNQ).
                due_form = per_forms[0]
                if filer == "foreign" and not got.get("20-F") and got.get("40-F"):
                    due_form = "40-F"
                due = date.fromisoformat(pend) + timedelta(days=_DUE_DAYS[due_form])
                pf = {"kind": "pending", "form": due_form, "url": None, "due": due.isoformat()}
            elif ef:
                # The owner's rule: otherwise the results filing (a foreign
                # filer's Q1-Q3 by design: it has no quarterly report).
                pf = {
                    "kind": "fallback",
                    **{k: ef[k] for k in ("form", "filed", "accepted", "url", "index_url")},
                }
        out.append(
            {
                "date": e["date"],
                "react": e.get("react"),
                "timing": e.get("timing") or "",
                "ts": e.get("ts"),
                **parts,
                "age_days": (today - rd).days,
                "eps": {
                    "est": e.get("estimate"),
                    "act": e.get("actual"),
                    "surprise_pct": e.get("surprise_pct"),
                },
                "earnings_filing": ef,
                "periodic_filing": pf,
                "edgar_list": edgar_list_url(
                    cik or "",
                    "8-K" if filer == "us" else "6-K",
                    (rd + timedelta(days=8)).strftime("%Y%m%d"),
                )
                if cik and filer in ("us", "foreign")
                else None,
            }
        )
    return out


# ------------------------------------------------------------ throttle
_LOCK = threading.Lock()
_COLD: deque[float] = deque()
_INFLIGHT: dict[str, threading.Event] = {}


def _cold_slot(wait_s: float = THROTTLE_WAIT_S) -> bool:
    """A cold lookup's place in the rolling minute (COLD_PER_MIN), waiting up
    to ``wait_s``. A burst of windows opened with E-hovers can't eat the
    Finnhub minute the news refresh shares; past the wait, the page says so
    and offers Try again."""
    stop = time.time() + wait_s
    while True:
        with _LOCK:
            now = time.time()
            while _COLD and now - _COLD[0] > 60.0:
                _COLD.popleft()
            if len(_COLD) < COLD_PER_MIN:
                _COLD.append(now)
                return True
            wait = 60.0 - (now - _COLD[0]) + 0.05
        if now + wait > stop:
            return False
        time.sleep(min(wait, 1.0))


_RESULT: dict[str, str] = {}  # a cold lookup's outcome, for those who waited on it


def _lookup(line: str, frm: str, max_age: float) -> tuple[str, tuple | None]:
    """("ok", (filer, got)) or ("unavailable" | "busy", None). Warm: the
    cache, no limiter. Cold: one lookup per line at a time (a hover and the
    card's click right after it wait for the same answer)."""
    hit = _fh.sec_filings_cached(line, frm, max_age)
    if hit is not None:
        return "ok", hit
    key = f"{line}|{frm}"
    with _LOCK:
        ev = _INFLIGHT.get(key)
        owner = ev is None
        if owner:
            ev = _INFLIGHT[key] = threading.Event()
    if not owner:
        ev.wait(DEADLINE_S + THROTTLE_WAIT_S)
        hit = _fh.sec_filings_cached(line, frm, max_age)
        return ("ok", hit) if hit is not None else (_RESULT.get(key, "unavailable"), None)
    status = "unavailable"
    try:
        if not _cold_slot():
            print(f"[filings] {line}: over {COLD_PER_MIN} lookups this minute, asked to try again")
            status = "busy"
            return status, None
        t0 = time.time()
        res = _fh.sec_filings(
            line,
            frm,
            date.today().isoformat(),
            ttl=max_age,
            none_ttl=_fh.FILINGS_DOUBT_TTL if _on_us_exchange(line) else _fh.FILINGS_NONE_TTL,
            cancel=Deadline(DEADLINE_S),
        )
        ms = int((time.time() - t0) * 1000)
        if res is None:
            print(f"[filings] {line}: Finnhub didn't answer ({ms} ms); nothing cached")
            return "unavailable", None
        filer, got, calls = res
        print(f"[filings] {line}: {filer} filer, {calls} Finnhub calls, {ms} ms")
        status = "ok"
        return status, (filer, got)
    finally:
        with _LOCK:
            _RESULT[key] = status
            _INFLIGHT.pop(key, None)
        ev.set()


# ------------------------------------------------------------ the route
def _name(line: str | None) -> str | None:
    if not line:
        return None
    with contextlib.suppress(Exception):
        from convexity import symbol_db

        hit = symbol_db.get(line.replace(".", "-")) or symbol_db.get(line)
        return hit.name if hit and hit.name else None
    return None


def history(symbol: str) -> dict:
    """GET /api/earnings-history: the window's reports with their filings
    (DATA_SPEC §6 shape). A detail path, never `fetch_one`'s: composes
    the cached events and `.info` with `finnhub_adapter.sec_filings`. No
    payload-level cache: the parts are cached, so the E card and the table
    read the same ones, and a status can't outlive its cause."""
    from convexity.fetcher import _research_info

    s = (symbol or "").upper()
    today = date.today()
    ev = events(s)
    primary = _primary(s)
    info = _research_info(primary) if not _is_fund(s) else {}
    fye = fiscal_year_end(info)
    rows = ev.get("earnings") or []
    reports = past_reports(rows, today)
    t = today.isoformat()
    nxt = next(
        (
            e["date"]
            for e in rows
            if e.get("date", "") > t or (e.get("date") == t and e.get("actual") is None)
        ),
        None,
    )
    line = _sec_line(s)
    sec: dict = {
        "status": "ok",
        "line": line,
        "cik": None,
        "filer": None,
        "name": _name(line),
        "edgar_url": None,
    }
    filer, got = None, {}
    if not reports:
        sec["status"] = "no_reports"
    elif not line:
        sec["status"] = "no_line"
    elif not _fh.FINNHUB_API_KEY:
        sec["status"] = "no_key"
    else:
        # From the oldest kept report - 10 days, on the first of its month:
        # a stable cache key while the table's oldest row stays the same.
        d0 = date.fromisoformat(reports[0]["date"]) - timedelta(days=10)
        frm = d0.replace(day=1).isoformat()
        fresh = (today - date.fromisoformat(reports[-1]["date"])).days < FRESH_DAYS
        # A year ending in January-March: how the company names it, asked of
        # its 10-Ks beside the filings (one call, kept 90 days).
        learner = None
        if fye and _fy_month(fye) in AMBIGUOUS_FY_MONTHS and fiscal_naming(line) is None:
            learner = threading.Thread(target=learn_naming, args=(line,), daemon=True)
            learner.start()
        status, res = _lookup(line, frm, HOT_TTL if fresh else OK_TTL)
        if learner is not None:
            learner.join(timeout=DEADLINE_S)
        if status != "ok" or res is None:
            sec["status"] = "unavailable"
            sec["reason"] = "busy" if status == "busy" else "no_answer"
        else:
            filer, got = res
            if filer == "none" and _on_us_exchange(line):
                # An exchange-listed company files with the SEC: an empty
                # answer is Finnhub's gap, kept a minute only, never "none".
                sec["status"], sec["reason"], filer, got = "unavailable", "empty", None, {}
            elif filer == "none":
                sec["status"] = "none"
            sec["filer"] = filer
            cik = next((f.get("cik") for fl in got.values() for f in fl if f.get("cik")), None)
            cik = cik if isinstance(cik, str) and cik.isdigit() else None
            sec["cik"] = cik
            sec["edgar_url"] = edgar_list_url(cik) if cik else None
    naming = fiscal_naming(line) if fye and _fy_month(fye) in AMBIGUOUS_FY_MONTHS else None
    matched = match(reports, filer or "", got, fye, today, sec["cik"], naming)
    return {
        "symbol": s,
        "primary": primary,
        "dates_via": ev.get("earnings_via"),
        "sec": sec,
        "website": website_url(info.get("website")),
        "fiscal_year_end": fye.isoformat() if fye else None,
        "reports": matched,
        "next": nxt,
        "asof": datetime.now(UTC).isoformat(timespec="seconds"),
    }
