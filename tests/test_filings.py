"""filings.py: the E card's SEC filings and the Earnings history (Phase 5).

Synthetic filings in Finnhub's /stock/filings shape for well-known public
tickers (CLAUDE.md §18); Finnhub and Yahoo are stubbed, nothing leaves the
machine. What is pinned: the matching windows (by time, never by Item), the
form each fiscal quarter maps to, pending reports with their due dates, the
no-SEC and no-key answers, a Finnhub miss with EDGAR's own list, a failure
that is never cached and never reads as "none", the URL allow-list and the
US line for a secondary listing."""

from datetime import date, timedelta

import pytest

from convexity import filings
from convexity import finnhub_adapter as fh

CIK = "320193"
ARCH = f"https://www.sec.gov/Archives/edgar/data/{CIK}/000032019326000024/"


def F(form, accepted, name="doc.htm", cik=CIK):
    """One Finnhub filing row; ``accepted`` in US Eastern, as Finnhub sends it."""
    acc = accepted if " " in accepted else accepted + " 16:30:00"
    return {
        "accessNumber": "0000320193-26-000024",
        "symbol": "AAPL",
        "cik": cik,
        "form": form,
        "filedDate": acc[:10] + " 00:00:00",
        "acceptedDate": acc,
        "reportUrl": ARCH + name,
        "filingUrl": ARCH + "0000320193-26-000024-index.html",
    }


def R(day, hhmm="16:00", timing="after", act=2.10, est=1.98):
    """One fetch_events earnings row (Yahoo's calendar), the time in New York."""
    off = "-05:00" if day[5:7] in ("11", "12", "01", "02", "03") else "-04:00"
    return {
        "date": day,
        "react": day,
        "ts": f"{day}T{hhmm}:00{off}" if timing else f"{day}T00:00:00{off}",
        "timing": timing,
        "estimate": est,
        "actual": act,
        "surprise_pct": None if est is None or act is None else (act - est) / abs(est) * 100,
    }


_REAL_FINNHUB_SYMBOL = fh.finnhub_symbol
APPLE_FYE = date(2026, 9, 26)  # a 52/53-week year: late September


# ------------------------------------------------------------ fiscal labels
@pytest.mark.parametrize(
    "report, fye, naming, label",
    [
        ("2026-11-02", date(2026, 9, 26), None, "Q4 FY2026"),  # AAPL, FY ends September
        ("2026-07-30", date(2025, 9, 27), None, "Q3 FY2026"),
        ("2026-10-28", date(2026, 6, 30), None, "Q1 FY2027"),  # MSFT, June
        ("2026-09-10", date(2026, 5, 31), None, "Q1 FY2027"),  # ORCL: "fiscal 2027"
        ("2026-05-14", date(2026, 1, 3), None, "Q1 FY2026"),  # a Jan-3 year end is December's
        ("2026-10-21", date(2025, 12, 31), None, "Q3 FY2026"),  # calendar year
        # January-March year ends: the company's own naming, or no year at all.
        ("2026-08-27", date(2026, 1, 25), "end", "Q2 FY2027"),  # NVDA
        ("2025-08-21", date(2026, 1, 31), "end", "Q2 FY2026"),  # WMT
        ("2025-08-19", date(2026, 2, 1), "start", "Q2 FY2025"),  # HD: fiscal 2025 ends 1 Feb 2026
        ("2025-08-20", date(2026, 1, 31), "start", "Q2 FY2025"),  # TGT
        ("2025-08-19", date(2026, 2, 1), None, None),  # not known: never a guessed year
        ("2025-08-19", date(2026, 2, 1), "", None),  # its 10-Ks couldn't say
        ("2026-08-04", date(2026, 3, 31), None, None),  # TM (March, a 20-F filer)
    ],
)
def test_fiscal_label(report, fye, naming, label):
    assert filings.fiscal(date.fromisoformat(report), fye, naming)["label"] == label


def test_without_a_year_the_quarter_is_still_named():
    p = filings.fiscal(date(2025, 8, 19), date(2026, 2, 1))  # HD, naming unknown
    assert p["quarter"] == 2 and p["fiscal_year"] is None
    # A 52/53-week quarter's end is on HD's 13-week grid, not the month end,
    # and not exact until the 10-Q's own file name says so.
    assert p["period_end"] == "2025-08-03" and p["period_exact"] is False
    p = filings.fiscal(date(2026, 8, 4), date(2026, 3, 31))  # a month-end year
    assert p["period_end"] == "2026-06-30" and p["period_exact"] is True


def test_the_quarters_exact_end_comes_from_its_10q_file_name():
    got = {
        "8-K": [F("8-K", "2025-08-19 06:00:00")],
        "10-Q": [F("10-Q", "2025-08-26 06:00:00", "hd-20250803.htm")],
        "10-K": [],
    }
    (r,) = _one([R("2025-08-19", hhmm="06:00", timing="before")], "us", got, fye=date(2026, 2, 1))
    assert r["quarter"] == 2 and r["label"] is None
    assert r["period_end"] == "2025-08-03" and r["period_exact"] is True


def _fin_rows(*rows):
    return {"data": [{"form": f, "year": y, "endDate": e + " 00:00:00"} for f, y, e in rows]}


@pytest.mark.parametrize(
    "answer, naming",
    [
        (
            _fin_rows(
                ("10-K", 2025, "2026-02-01"),
                ("10-K", 2024, "2025-02-02"),
                ("10-K", 2023, "2024-01-28"),
            ),
            "start",
        ),
        (_fin_rows(("10-K", 2026, "2026-01-31"), ("10-K", 2025, "2025-01-31")), "end"),
        (
            _fin_rows(("10-K", 2026, "2026-01-25"), ("10-K", 2024, "2025-01-26")),
            "",
        ),  # they disagree
        ({"data": []}, ""),  # a 20-F filer: no 10-K rows
    ],
)
def test_the_naming_is_what_the_companys_10ks_say(monkeypatch, answer, naming):
    monkeypatch.setattr(fh, "_fh_call", lambda path, params, symbol, **kw: answer)
    assert filings.learn_naming("HD") == naming
    assert filings.fiscal_naming("HD") == naming


@pytest.mark.parametrize("answer", [None, {}, [], {"error": "x"}])
def test_a_failed_naming_call_is_not_an_answer(monkeypatch, answer):
    monkeypatch.setattr(fh, "_fh_call", lambda path, params, symbol, **kw: answer)
    assert filings.learn_naming("HD") is None
    assert filings.fiscal_naming("HD") is None  # nothing kept: asked again next time


def test_a_degraded_info_gives_no_label_never_a_guess():
    ts = 1790380800  # 2026-09-26
    assert filings.fiscal_year_end({"lastFiscalYearEnd": ts}) == date(2026, 9, 26)
    assert filings.fiscal_year_end({"lastFiscalYearEnd": ts, "_fundamentals_missing": True}) is None
    assert filings.fiscal_year_end({}) is None
    rows = filings.label_rows([R("2026-11-02")], None)
    assert rows[0]["label"] is None and rows[0]["period_end"] is None and rows[0]["quarter"] is None


# ------------------------------------------------------------ the 8-K by time
def _one(reports, filer, got, today="2026-11-17", fye=APPLE_FYE):
    return filings.match(reports, filer, got, fye, date.fromisoformat(today), CIK)


def test_the_results_8k_is_the_first_one_accepted_after_the_release():
    got = {
        "8-K": [
            F("8-K", "2026-11-02 16:30:23", "aapl-20261102.htm"),
            F("8-K", "2026-11-02 18:10:00", "aapl-other.htm"),
        ],
        "10-K": [F("10-K", "2026-11-03 06:01:00", "aapl-20260926.htm")],
        "10-Q": [],
    }
    (r,) = _one([R("2026-11-02")], "us", got)
    ef = r["earnings_filing"]
    assert ef["url"].endswith("aapl-20261102.htm")
    assert ef["form"] == "8-K" and ef["days_after"] == 0 and ef["others_that_day"] == 1
    # Structured fields only, the time with its US Eastern offset.
    assert ef["accepted"] == "2026-11-02T16:30:23-05:00"
    assert "label" not in ef
    assert r["label"] == "Q4 FY2026"
    pf = r["periodic_filing"]
    assert pf["kind"] == "periodic" and pf["form"] == "10-K" and pf["filed"] == "2026-11-03"


@pytest.mark.parametrize(
    "accepted, hit",
    [
        ("2026-11-02 13:01:00", True),  # 2 h 59 min before the 16:00 release
        ("2026-11-02 12:59:00", False),  # more than 3 h before: another 8-K
        ("2026-11-05 15:59:00", True),  # inside 3 days
        ("2026-11-05 16:01:00", False),  # past them: not this report's
        ("2026-10-30 16:30:00", False),
    ],
)
def test_the_8k_window_is_three_hours_before_to_three_days_after(accepted, hit):
    (r,) = _one([R("2026-11-02")], "us", {"8-K": [F("8-K", accepted)], "10-K": [], "10-Q": []})
    assert (r["earnings_filing"] is not None) is hit


def test_no_time_given_means_midnight_of_the_report_day():
    got = {"8-K": [F("8-K", "2026-11-02 07:02:00")], "10-Q": [], "10-K": []}
    (r,) = _one([R("2026-11-02", timing="")], "us", got)
    assert r["earnings_filing"]["filed"] == "2026-11-02"


def test_a_foreign_filers_6k_comes_up_to_seven_days_late_and_names_decide_ties():
    got = {
        "6-K": [
            F("6-K", "2026-10-26 06:05:00", "remunerationreport_2026.htm"),  # plainly not results
            F("6-K", "2026-10-26 06:10:00", "pressrelease.htm"),
            F(
                "6-K", "2026-10-26 06:20:00", "sapq32026.htm"
            ),  # results-looking: preferred on the day
        ],
        "20-F": [],
    }
    (r,) = _one([R("2026-10-20", hhmm="16:05")], "foreign", got, fye=date(2025, 12, 31))
    ef = r["earnings_filing"]
    assert ef["form"] == "6-K" and ef["url"].endswith("sapq32026.htm") and ef["days_after"] == 6
    assert r["label"] == "Q3 FY2026"
    # Q1-Q3 of a foreign filer: no quarterly report, the 6-K itself.
    assert r["periodic_filing"]["kind"] == "fallback" and r["periodic_filing"]["url"] == ef["url"]
    # Eight days late is no longer this report's.
    late = {"6-K": [F("6-K", "2026-10-28 18:00:00", "sapq32026.htm")], "20-F": []}
    (r,) = _one([R("2026-10-20", hhmm="16:05")], "foreign", late, fye=date(2025, 12, 31))
    assert r["earnings_filing"] is None


# ------------------------------------------------------------ the periodic form
def test_form_follows_the_quarter_10q_for_q1_to_q3_10k_for_q4():
    reports = [R("2026-07-30"), R("2026-11-02")]
    got = {
        "8-K": [F("8-K", "2026-07-30 16:31:00"), F("8-K", "2026-11-02 16:30:00")],
        "10-Q": [F("10-Q", "2026-08-01 06:00:00", "q3.htm")],
        "10-K": [
            F("10-K", "2026-11-03 06:00:00", "k.htm"),
            F("10-K", "2025-11-01 06:00:00", "old.htm"),
        ],
    }
    q3, q4 = _one(reports, "us", got)
    assert q3["label"] == "Q3 FY2026" and q3["periodic_filing"]["form"] == "10-Q"
    assert q4["label"] == "Q4 FY2026" and q4["periodic_filing"]["form"] == "10-K"
    assert q4["periodic_filing"]["url"].endswith("k.htm")


def test_a_10q_belongs_to_the_quarter_before_the_next_report():
    """The 10-Q window closes at the next report: a late one is not stolen."""
    reports = [R("2026-04-30"), R("2026-07-30")]
    got = {"8-K": [], "10-K": [], "10-Q": [F("10-Q", "2026-08-01 06:00:00")]}
    q2, q3 = _one(reports, "us", got, fye=date(2025, 9, 27))
    assert q2["periodic_filing"] is None  # long past its window, nothing to fall back on
    assert q3["periodic_filing"]["kind"] == "periodic"


def test_a_foreign_q4_is_the_20f_and_tsms_q1_day_20f_stays_with_q4():
    """TSM files its 20-F on its Q1 results day: a plain "first periodic
    report after" gave it to Q1."""
    reports = [R("2026-01-15", hhmm="01:30"), R("2026-04-16", hhmm="01:30")]
    got = {
        "6-K": [
            F("6-K", "2026-01-15 02:00:00", "q4.htm"),
            F("6-K", "2026-04-16 02:00:00", "q1.htm"),
        ],
        "20-F": [F("20-F", "2026-04-16 05:00:00", "annual.htm")],
    }
    q4, q1 = _one(reports, "foreign", got, fye=date(2025, 12, 31))
    assert q4["label"] == "Q4 FY2025" and q4["periodic_filing"]["form"] == "20-F"
    assert q1["label"] == "Q1 FY2026" and q1["periodic_filing"]["kind"] == "fallback"


def test_a_40f_filer_gets_its_40f():
    got = {
        "6-K": [F("6-K", "2026-03-05 07:00:00")],
        "20-F": [],
        "40-F": [F("40-F", "2026-03-27 07:00:00")],
    }
    (r,) = _one(
        [R("2026-03-05", hhmm="06:30", timing="before")], "foreign", got, fye=date(2025, 12, 31)
    )
    assert r["periodic_filing"]["form"] == "40-F" and r["periodic_filing"]["kind"] == "periodic"


@pytest.mark.parametrize(
    "report, fye, form, due",
    [
        # From the real 52/53-week period end, not the month end (a week late):
        ("2026-11-12", date(2026, 7, 25), "10-Q", "2026-12-03"),  # CSCO Q1 FY2027: 24 Oct + 40 d
        ("2026-11-12", date(2025, 10, 26), "10-K", "2026-12-24"),  # AMAT Q4: 25 Oct + 60 d
    ],
)
def test_pending_until_filed_with_its_usual_due_date(report, fye, form, due):
    got = {
        "8-K": [F("8-K", report + " 16:04:00")],
        "10-Q": [F("10-Q", "2026-08-20 06:00:00")],
        "10-K": [],
    }
    (r,) = _one([R(report)], "us", got, fye=fye)
    pf = r["periodic_filing"]
    assert pf == {"kind": "pending", "form": form, "url": None, "due": due}
    assert r["earnings_filing"]["form"] == "8-K"


def test_pending_ends_with_its_window():
    """Past the 10-Q's 75 days it is no longer "pending": the results 8-K."""
    got = {"8-K": [F("8-K", "2026-04-30 16:30:00")], "10-Q": [], "10-K": []}
    (r,) = _one([R("2026-04-30")], "us", got, fye=date(2025, 9, 27))
    assert r["periodic_filing"]["kind"] == "fallback" and r["periodic_filing"]["form"] == "8-K"


def test_a_finnhub_miss_links_edgars_own_list_up_to_that_week():
    """NKE's 2026-06-30 8-K is missing from Finnhub: EDGAR has it."""
    got = {"8-K": [], "10-Q": [], "10-K": [F("10-K", "2026-07-15 16:00:00", "nke-10k.htm")]}
    (r,) = _one([R("2026-06-30")], "us", got, fye=date(2026, 5, 31))
    assert r["earnings_filing"] is None
    assert r["periodic_filing"]["form"] == "10-K"
    assert r["edgar_list"] == (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        "&CIK=0000320193&type=8-K&dateb=20260708&owner=include&count=40"
    )


def test_amendments_never_stand_for_the_report():
    raw = [F("8-K/A", "2026-11-02 16:31:00"), F("8-K", "2026-11-02 16:40:00", "real.htm")]

    def call(path, params, symbol, **kw):
        return [f for f in raw if f["form"].startswith(params["form"])]

    import convexity.finnhub_adapter as m

    old = m._fh_call
    m._fh_call = call
    try:
        assert [f["reportUrl"][-8:] for f in m._filings_one("AAPL", "8-K", "a", "b", None)] == [
            "real.htm"
        ]
    finally:
        m._fh_call = old


# ------------------------------------------------------------ URL allow-list
@pytest.mark.parametrize(
    "u, ok",
    [
        (ARCH + "aapl-20261102.htm", True),
        (ARCH + "0000320193-26-000024-index.html", True),
        ("http://www.sec.gov/Archives/edgar/data/320193/x.htm", False),
        ("https://www.sec.gov.evil.example/Archives/edgar/data/320193/x.htm", False),
        ("https://www.sec.gov/Archives/edgar/data/320193/x.htm?a=1", False),
        ("https://www.sec.gov/Archives/edgar/data/320193/x.htm#f", False),
        ("https://www.sec.gov/Archives/edgar/data/320193/../../x.htm", False),
        ("https://user@www.sec.gov/Archives/edgar/data/320193/x.htm", False),
        ("https://www.sec.gov:8443/Archives/edgar/data/320193/x.htm", False),
        ("https://www.sec.gov/ix?doc=/Archives/edgar/data/320193/x.htm", False),
        ("javascript:alert(1)", False),
        ('https://www.sec.gov/Archives/edgar/data/320193/x.htm"onmouseover=1', False),
        (None, False),
    ],
)
def test_only_plain_edgar_archive_links_reach_the_page(u, ok):
    assert filings.sec_url(u) == (u if ok else None)


def test_a_filing_with_a_bad_link_is_dropped():
    got = {"8-K": [{**F("8-K", "2026-11-02 16:30:00"), "reportUrl": "https://evil.example/x.htm"}]}
    (r,) = _one([R("2026-11-02")], "us", {**got, "10-K": [], "10-Q": []})
    assert r["earnings_filing"] is None


@pytest.mark.parametrize(
    "u, ok",
    [
        ("https://www.rheinmetall.com", True),
        ("https://www.apple.com/investor/", True),
        ("http://www.asml.com/asml/show.do?ctx=32918", False),  # irWebsite-style, http
        ("javascript:alert(1)", False),
        ("https://user:pw@example.com", False),
        ("https://exa mple.com", False),
        ("https://localhost", False),
        (None, False),
    ],
)
def test_the_website_is_https_only(u, ok):
    assert filings.website_url(u) == (u if ok else None)


def test_edgar_list_is_built_from_a_checked_cik_only():
    assert filings.edgar_list_url("320193") == (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        "&CIK=0000320193&type=&dateb=&owner=include&count=40"
    )
    assert filings.edgar_list_url("32x") is None
    assert filings.edgar_list_url("320193", "10-K") is None
    assert filings.edgar_list_url("320193", "8-K", "2026-11-10") is None


# ------------------------------------------------------------ the route
def _days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


@pytest.fixture()
def stub(monkeypatch):
    """history() with Yahoo and Finnhub stubbed. ``s.answers[form]`` is what
    Finnhub sends for that form (a list, or None for a failure); every
    Finnhub call is recorded in ``s.calls``."""

    class S:
        reports = [R(_days_ago(100)), R(_days_ago(9))]
        info = {
            "lastFiscalYearEnd": 1790380800,
            "website": "https://www.apple.com",
            "longName": "Apple Inc.",
        }
        answers: dict = {}
        calls: list = []

    s = S()
    s.calls = []
    monkeypatch.setattr(fh, "FINNHUB_API_KEY", "test-key")
    fh._FH_CACHE.clear()
    filings._COLD.clear()

    from convexity import fetcher

    monkeypatch.setattr(
        fetcher, "fetch_events", lambda sym: {"symbol": sym, "earnings": s.reports, "dividends": []}
    )
    monkeypatch.setattr(fetcher, "_research_info", lambda line: s.info)
    monkeypatch.setattr(filings, "_primary", lambda sym: sym)
    monkeypatch.setattr(
        fh, "finnhub_symbol", lambda sym: {"RHM.DE": "RNMBY", "ENI.MI": None}.get(sym, sym)
    )

    def call(path, params, symbol, **kw):
        assert path == "stock/filings"
        s.calls.append((params["symbol"], params["form"]))
        return s.answers.get(params["form"], [])

    monkeypatch.setattr(fh, "_fh_call", call)
    # RNMBY is an OTC line (an unsponsored ADR); everything else here trades on
    # a US exchange (the pack decides in the app: test_exchange_lines_*).
    monkeypatch.setattr(filings, "_on_us_exchange", lambda line: line != "RNMBY")
    yield s
    fh._FH_CACHE.clear()


def test_history_us_filer(stub):
    stub.answers = {
        "10-Q": [F("10-Q", _days_ago(97) + " 06:00:00")],
        "8-K": [F("8-K", _days_ago(100) + " 16:30:00"), F("8-K", _days_ago(9) + " 16:30:00")],
        "10-K": [],
    }
    p = filings.history("AAPL")
    assert p["sec"]["status"] == "ok" and p["sec"]["filer"] == "us" and p["sec"]["cik"] == CIK
    assert p["sec"]["edgar_url"].startswith("https://www.sec.gov/cgi-bin/browse-edgar?")
    # The 10-Q decides the filer; the 8-K and 10-K then go out together.
    assert stub.calls[0][1] == "10-Q" and sorted(c[1] for c in stub.calls[1:]) == ["10-K", "8-K"]
    assert all(c[0] == "AAPL" for c in stub.calls)
    assert len(p["reports"]) == 2 and all(r["earnings_filing"] for r in p["reports"])
    assert p["website"] == "https://www.apple.com"
    # Warm: the cache, no call.
    filings.history("AAPL")
    assert len(stub.calls) == 3


def test_the_hot_ttl_while_the_latest_report_is_fresh(stub, monkeypatch):
    stub.reports = [R(_days_ago(2))]
    stub.answers = {"10-Q": [F("10-Q", _days_ago(80) + " 06:00:00")], "8-K": [], "10-K": []}
    filings.history("AAPL")
    ts, ttl, _ = next(v for k, v in fh._FH_CACHE.items() if k.startswith("filings|AAPL|"))
    assert ttl == filings.HOT_TTL
    assert p_age_status(stub) == "ok"


def p_age_status(stub):
    p = filings.history("AAPL")
    (r,) = p["reports"]
    assert r["age_days"] == 2 and r["earnings_filing"] is None  # the page says "yet"
    return p["sec"]["status"]


def test_no_sec_filings_is_none_cached_a_day_and_offers_the_website(stub):
    stub.info = {**stub.info, "website": "https://www.rheinmetall.com"}
    p = filings.history("RHM.DE")
    assert p["sec"]["status"] == "none" and p["sec"]["line"] == "RNMBY"
    assert [c[1] for c in stub.calls] == ["10-Q", "6-K", "10-K"]
    assert p["website"] == "https://www.rheinmetall.com"
    assert all(r["earnings_filing"] is None and r["edgar_list"] is None for r in p["reports"])
    assert p["reports"][-1]["label"] is not None  # the facts stay
    (entry,) = [v for k, v in fh._FH_CACHE.items() if k.startswith("filings|RNMBY|")]
    assert entry[1] == fh.FILINGS_NONE_TTL


def test_no_us_line_at_all(stub):
    p = filings.history("ENI.MI")
    assert p["sec"]["status"] == "no_line" and not stub.calls


def test_no_key_is_a_flag_and_spends_nothing(stub, monkeypatch):
    monkeypatch.setattr(fh, "FINNHUB_API_KEY", "")
    p = filings.history("AAPL")
    assert p["sec"]["status"] == "no_key" and not stub.calls
    assert len(p["reports"]) == 2  # Yahoo's facts are complete without it


def test_a_failed_call_is_unavailable_never_none_and_never_cached(stub):
    stub.answers = {"10-Q": [F("10-Q", _days_ago(97) + " 06:00:00")], "8-K": None}
    p = filings.history("AAPL")
    assert p["sec"]["status"] == "unavailable" and p["sec"]["reason"] == "no_answer"
    assert not any(k.startswith("filings|") for k in fh._FH_CACHE)
    # A failure on the very first form is not "no 10-Q, so foreign" either.
    stub.answers = {"10-Q": None}
    assert filings.history("AAPL")["sec"]["status"] == "unavailable"
    # The next ask tries again, and an answer is then kept.
    stub.answers = {"10-Q": [F("10-Q", _days_ago(97) + " 06:00:00")], "8-K": [], "10-K": []}
    n = len(stub.calls)
    assert filings.history("AAPL")["sec"]["status"] == "ok"
    assert len(stub.calls) == n + 3


def test_a_403_is_a_failure_too(stub, monkeypatch):
    """_fh_call turns a 403 into {}: not a list, so not an answer."""
    stub.answers = {"10-Q": {}}
    assert filings.history("AAPL")["sec"]["status"] == "unavailable"


def test_a_burst_is_throttled_not_spent(stub, monkeypatch):
    monkeypatch.setattr(filings, "THROTTLE_WAIT_S", 0.0)
    monkeypatch.setattr(filings, "_cold_slot", lambda wait_s=0.0: False)
    p = filings.history("AAPL")
    assert p["sec"]["status"] == "unavailable" and p["sec"]["reason"] == "busy"
    assert not stub.calls


def test_cold_slots_cap_the_minute(monkeypatch):
    filings._COLD.clear()
    assert all(filings._cold_slot(0.0) for _ in range(filings.COLD_PER_MIN))
    assert filings._cold_slot(0.0) is False
    filings._COLD.clear()


def test_no_reports_asks_nothing(stub):
    stub.reports = [R(_days_ago(-30), act=None)]  # only the next date
    p = filings.history("AAPL")
    assert p["sec"]["status"] == "no_reports" and not stub.calls and p["reports"] == []
    assert p["next"] == _days_ago(-30)


def test_reports_kept_are_the_last_twelve_of_four_years():
    rows = [R(_days_ago(30 + 91 * k)) for k in range(20)][::-1]
    rows.append(R(date.today().isoformat(), act=None))  # due today, not out: next
    kept = filings.past_reports(rows, date.today())
    assert len(kept) == 12 and kept[-1]["date"] == _days_ago(30)
    old = [R(_days_ago(5 * 365))]
    assert filings.past_reports(old, date.today()) == []


def test_reload_keys_drops_the_filings_cache(monkeypatch):
    fh._FH_CACHE["filings|AAPL|2022-10-01"] = (0.0, 1e9, ("us", {}))
    monkeypatch.setattr(fh, "FINNHUB_API_KEY", "old")
    monkeypatch.setattr(fh, "_load_local_secret", lambda name: "new")
    fh.reload_keys()
    assert not fh._FH_CACHE


# ------------------------------------------------------------ the US line
def test_a_secondary_listing_asks_finnhub_for_its_us_line(monkeypatch):
    """MSF.DE (Xetra) files nothing: Microsoft files as MSFT. Synthetic pack."""
    from convexity import symbol_db as sdb
    from tests.test_symbol_db import install

    install()
    try:
        assert fh.finnhub_symbol("MSF.DE") == "MSFT"
        seen = []
        monkeypatch.setattr(fh, "FINNHUB_API_KEY", "test-key")
        monkeypatch.setattr(
            fh, "_fh_call", lambda path, params, symbol, **kw: seen.append(params["symbol"]) or []
        )
        from convexity import fetcher

        monkeypatch.setattr(
            fetcher,
            "fetch_events",
            lambda sym: {"earnings": [R(_days_ago(20))], "earnings_via": "MSFT"},
        )
        monkeypatch.setattr(fetcher, "_research_info", lambda line: {})
        fh._FH_CACHE.clear()
        p = filings.history("MSF.DE")
        assert set(seen) == {"MSFT"} and p["sec"]["line"] == "MSFT"
        assert p["sec"]["name"] == "Microsoft Corporation" and p["dates_via"] == "MSFT"
    finally:
        fh._FH_CACHE.clear()
        sdb._invalidate()


# ------------------------------------------------------------ class shares, doubtful "none"
def test_class_shares_ask_filings_by_the_dash_and_keep_the_dot_elsewhere(stub, monkeypatch):
    """/stock/filings answers BRK-B (6 rows) and nothing for BRK.B, while the
    other Finnhub endpoints take BRK.B: the filings only use the dash."""
    from convexity import symbol_db

    monkeypatch.setattr(symbol_db, "us_line", lambda sym: "BRK-B")
    monkeypatch.setattr(fh, "finnhub_symbol", _REAL_FINNHUB_SYMBOL)  # the stub's is a dict
    assert fh.finnhub_symbol("BRK-B") == "BRK.B"
    assert filings._sec_line("BRK-B") == "BRK-B"
    stub.answers = {"10-Q": [F("10-Q", _days_ago(97) + " 06:00:00")], "8-K": [], "10-K": []}
    p = filings.history("BRK-B")
    assert {c[0] for c in stub.calls} == {"BRK-B"} and p["sec"]["status"] == "ok"
    assert p["sec"]["line"] == "BRK-B"


def test_other_endpoints_keep_finnhubs_dot_spelling(monkeypatch):
    from convexity import symbol_db

    monkeypatch.setattr(symbol_db, "us_line", lambda sym: "BRK-B")
    monkeypatch.setattr(fh, "FINNHUB_API_KEY", "test-key")
    fh._FH_CACHE.clear()
    seen = []
    monkeypatch.setattr(
        fh, "_fh_call", lambda path, params, symbol, **kw: seen.append(params["symbol"]) or []
    )
    fh.get_recommendation_trend("BRK-B")
    assert seen == ["BRK.B"]
    fh._FH_CACHE.clear()


def test_nothing_at_all_for_an_exchange_listed_company_is_never_none(stub):
    """Every exchange-listed company files with the SEC: an empty answer is
    Finnhub's gap or spelling (BRK.B), kept a minute, not 24 h of "Not filed"."""
    p = filings.history("AAPL")
    assert p["sec"]["status"] == "unavailable" and p["sec"]["reason"] == "empty"
    assert all(r["earnings_filing"] is None for r in p["reports"])
    (entry,) = [v for k, v in fh._FH_CACHE.items() if k.startswith("filings|AAPL|")]
    assert entry[1] == fh.FILINGS_DOUBT_TTL
    assert filings.history("AAPL")["sec"]["status"] == "unavailable"  # warm: still not "none"


def test_exchange_lines_are_read_from_the_pack():
    from convexity import symbol_db as sdb
    from tests.test_symbol_db import ROWS, install

    otc = [
        "RNMBY",
        "Rheinmetall AG",
        "stock",
        "PNK",
        "us",
        "Industrials",
        "Aerospace",
        3e10,
        1e5,
        77,
        0,
    ]
    install(ROWS + [otc])
    try:
        assert filings._on_us_exchange("MSFT") is True  # NMS
        assert filings._on_us_exchange("RNMBY") is False  # OTC: may truly file nothing
        assert filings._on_us_exchange("ZZZZ") is True  # unknown: never trusted as "none"
    finally:
        sdb._invalidate()


def test_a_follower_reports_the_owners_reason(monkeypatch):
    """A request that waited on another's lookup says why it failed: busy
    stays busy (the page then says so), not "Finnhub didn't answer"."""
    import threading

    key = "AAPL|2022-10-01"
    ev = threading.Event()
    ev.set()
    filings._INFLIGHT[key] = ev
    filings._RESULT[key] = "busy"
    try:
        assert filings._lookup("AAPL", "2022-10-01", 60.0) == ("busy", None)
    finally:
        filings._INFLIGHT.pop(key, None)
        filings._RESULT.pop(key, None)


def test_a_jan_to_mar_year_learns_its_naming_beside_the_filings(stub, monkeypatch):
    """HD: the label waits for its 10-Ks' word; asked once, then kept."""
    stub.info = {**stub.info, "lastFiscalYearEnd": 1769904000}  # 2026-02-01
    stub.reports = [R(_days_ago(100))]
    stub.answers = {"10-Q": [F("10-Q", _days_ago(97) + " 06:00:00")], "8-K": [], "10-K": []}
    asked = []

    def call(path, params, symbol, **kw):
        if path == "stock/financials-reported":
            asked.append(symbol)
            return _fin_rows(("10-K", 2025, "2026-02-01"), ("10-K", 2024, "2025-02-02"))
        stub.calls.append((params["symbol"], params["form"]))
        return stub.answers.get(params["form"], [])

    monkeypatch.setattr(fh, "_fh_call", call)
    ev = filings.events("HD")
    assert ev["earnings"][0]["label"] is None and ev["earnings"][0]["quarter"]  # not known yet
    p = filings.history("HD")
    assert asked == ["HD"] and p["reports"][0]["label"].endswith(
        str(p["reports"][0]["fiscal_year"])
    )
    assert filings.events("HD")["earnings"][0]["label"] == p["reports"][0]["label"]
    filings.history("HD")
    assert asked == ["HD"]  # kept: not asked again
