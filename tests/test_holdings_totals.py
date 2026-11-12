"""The holdings table's Risk columns and total row (Feature 5/6).

One risk function (``mpt.risk_parts``) for My Investments and Portfolios, and one
totals function (``analytics._holdings_totals``) for the table, the panels and the
export. Synthetic tickers only, no network."""

import ast
import inspect
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from convexity import analytics as A
from convexity import attention, fetcher, mpt, persistence
from convexity import investments as inv
from tests.test_finance_math import _analytics

_APP_JS = (Path(__file__).resolve().parent.parent / "src/convexity/static/app.js").read_text()


def _returns(n=260, cols=("AAA", "BBB", "CCC"), seed=7):
    rng = np.random.default_rng(seed)
    common = rng.normal(0, 0.01, n)
    return pd.DataFrame(
        {c: common * (k + 1) * 0.5 + rng.normal(0, 0.01, n) for k, c in enumerate(cols)},
        index=pd.bdate_range("2025-01-01", periods=n),
    )


# ------------------------------------------------------------- risk_parts
def test_risk_shares_add_up_and_the_identities_hold():
    rets = _returns()
    w = {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}
    p = mpt.risk_parts(rets, w)
    h = p["holdings"]
    assert sum(x["share"] for x in h.values()) == pytest.approx(1.0)
    cov = mpt.annualized_cov(rets, "daily", "ledoit").to_numpy()
    wv = np.array(list(w.values()))
    assert p["vol"] == pytest.approx(np.sqrt(wv @ cov @ wv))
    for s, x in h.items():
        assert x["ratio"] == pytest.approx(x["vol"] * x["corr"] / p["vol"])
        assert x["share"] == pytest.approx(w[s] * x["ratio"])
        assert x["vol"] == pytest.approx(rets[s].std() * np.sqrt(252))


def test_a_zero_weight_holding_has_no_share_but_a_ratio():
    p = mpt.risk_parts(_returns(), {"AAA": 0.6, "BBB": 0.4, "CCC": 0.0})["holdings"]
    assert p["CCC"]["share"] == 0.0 and np.isfinite(p["CCC"]["ratio"])


def test_a_young_or_flat_listing_is_left_out():
    rets = _returns()
    rets["NEW"] = np.nan
    rets.iloc[-10:, rets.columns.get_loc("NEW")] = 0.01
    rets["DEAD"] = 0.0
    p = mpt.risk_parts(rets, dict.fromkeys(rets.columns, 0.2))["holdings"]
    assert set(p) == {"AAA", "BBB", "CCC"}
    assert mpt.risk_parts(rets.iloc[:10], {"AAA": 1.0}) == {}


def test_my_investments_reads_the_same_function():
    rets = _returns()
    px = (1 + rets).cumprod() * 100
    syms = list(px.columns)
    vps = {s: np.r_[100.0, px[s].to_numpy()] for s in syms}
    qty = {s: np.full(len(px) + 1, q) for s, q in zip(syms, (10.0, 20.0, 5.0), strict=True)}
    got = inv._risk_share(syms, vps, qty, np.ones(len(px) + 1, dtype=bool), 0)
    values = {s: qty[s][-1] * vps[s][-1] for s in syms}
    days = pd.DataFrame(vps).pct_change(fill_method=None).iloc[1:]
    want = mpt.risk_parts(days, values)["holdings"]
    assert got == pytest.approx({s: x["share"] for s, x in want.items()})


# ------------------------------------------------------------- the payload
ROWS = [
    {"symbol": "AAA", "name": "A", "pe_ratio": 20.0, "ps_ratio": 4.0, "beta": 1.2,
     "pct_1y": 10.0, "model_mu": 0.08, "currency": "USD"},
    {"symbol": "BBB", "name": "B", "pe_ratio": -5.0, "ps_ratio": 2.0, "beta": 0.8,
     "pct_1y": -4.0, "model_mu": 0.06, "currency": "USD"},
    {"symbol": "CCC", "name": "C", "pe_ratio": None, "ps_ratio": None, "beta": 1.0,
     "pct_1y": 3.0, "model_mu": 0.07, "currency": "USD"},
]  # fmt: skip


def test_analytics_carry_risk_and_totals_that_reconcile(monkeypatch):
    a = _analytics(monkeypatch, ROWS, {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2})
    T, h = a["totals"], a["risk"]["holdings"]
    assert sum(x["share"] for x in h.values()) == pytest.approx(1.0)
    assert T["risk"]["v"] == pytest.approx(1.0) and T["risk_w"]["v"] == 1.0
    assert T["vol"]["v"] == pytest.approx(a["risk"]["vol"])
    # Contribution adds up to the period's return, and the total is that return.
    assert sum(x["contribution"] for x in a["contribution"]) == pytest.approx(T["contrib"]["v"])
    assert T["contrib"]["v"] == a["stats"]["total_return"]
    # The multiples and beta are the panels' own numbers.
    assert T["pe_ratio"]["v"] == a["weighted"]["pe"]
    assert T["ps_ratio"]["v"] == a["weighted"]["ps"]
    assert T["beta"]["v"] == a["stats"]["beta"]


def _payload(weights):
    return {"weights_applied": weights, "stats": {}, "weighted": {}, "analyst": {}}


def test_the_harmonic_total_is_w_harmonic_and_a_loss_is_a_gap():
    w = {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}
    T = A._holdings_totals(ROWS, _payload(w))
    vals = {r["symbol"]: r["pe_ratio"] for r in ROWS}
    assert T["pe_ratio"]["v"] == pytest.approx(A._w_harmonic(vals, w))
    assert T["pe_ratio"]["cov"] == pytest.approx(0.5) and T["pe_ratio"]["n"] == 1
    assert T["pe_ratio"]["out"] == [["BBB", 0.3, "loss"], ["CCC", 0.2, "none"]]
    assert T["beta"]["v"] == pytest.approx(0.5 * 1.2 + 0.3 * 0.8 + 0.2 * 1.0)


def test_under_40_percent_covered_the_total_stays_empty():
    T = A._holdings_totals(ROWS, _payload({"AAA": 0.35, "BBB": 0.35, "CCC": 0.3}))
    assert T["pe_ratio"]["cov"] == pytest.approx(0.35) and T["pe_ratio"]["v"] is None
    assert T["ps_ratio"]["cov"] == pytest.approx(0.70) and T["ps_ratio"]["v"] is not None


def test_a_holding_at_zero_weight_never_counts():
    T = A._holdings_totals(ROWS, _payload({"AAA": 1.0, "BBB": 0.0, "CCC": 0.0}))
    assert T["pe_ratio"]["cov"] == 1.0 and T["pe_ratio"]["out"] == []
    assert T["pe_ratio"]["v"] == pytest.approx(20.0)


def test_a_panel_figure_wins_over_the_rows():
    a = {**_payload({"AAA": 0.5, "BBB": 0.5}), "weighted": {"pe": 33.0, "coverage": {"pe": 1.0}},
         "stats": {"beta": 1.11, "beta_coverage": 1.0}}  # fmt: skip
    T = A._holdings_totals(ROWS[:2], a)
    assert T["pe_ratio"]["v"] == 33.0 and T["pe_ratio"]["out"] == []
    assert T["beta"]["v"] == 1.11


def test_the_red_tab_reads_closes_as_traded(monkeypatch):
    adjusted = (1 + _returns()).cumprod() * 100
    traded = adjusted * 0.9 + np.linspace(0, 5, len(adjusted))[:, None]
    monkeypatch.setattr(A, "_bulk_close", lambda syms, period, adjusted=True: traded[syms])
    monkeypatch.setattr(A, "extend_history", lambda df, *a, **k: (df, {}))
    monkeypatch.setattr(A, "_apply_fx_to_closes", lambda df, *a, **k: df)
    syms = list(adjusted.columns)
    rows = [{"symbol": s} for s in syms]

    def rets(as_traded):
        return A._risk_returns(adjusted, rows, syms, syms, {}, "2y", "USD", as_traded)

    start = A._period_start("1Y", adjusted.index[-1])
    want = traded.loc[start:].pct_change(fill_method=None).iloc[1:]
    pd.testing.assert_frame_equal(rets(True), want)
    pd.testing.assert_frame_equal(
        rets(False), adjusted.loc[start:].pct_change(fill_method=None).iloc[1:]
    )


def test_the_export_writes_a_total_and_a_coverage_row():
    from openpyxl import Workbook

    from convexity import xlsx_export as xe

    w = {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}
    a = {
        **_payload(w),
        "risk": {"holdings": {"AAA": {"share": 0.6, "ratio": 1.2, "vol": 0.3, "corr": 0.9}}},
    }
    a["totals"] = A._holdings_totals(ROWS, a)
    ws = Workbook().active
    xe._write_holdings(ws, 1, ROWS, w, a, a, 1)
    head = [c.value for c in ws[1]]
    total, cover = ws[len(ROWS) + 2], ws[len(ROWS) + 3]
    assert total[0].value == "Total" and cover[0].value == "Coverage"
    pe = head.index("P/E")
    assert total[pe].value == pytest.approx(a["totals"]["pe_ratio"]["v"]) and cover[pe].value == 0.5
    assert ws.cell(row=2, column=head.index("Risk") + 1).value == 0.6


# ------------------------------------------------------------- earnings fill
def test_earnings_fill_is_small_quiet_and_never_caches_an_empty_answer(monkeypatch):
    started = []
    monkeypatch.setattr(
        attention, "_JobRunning", type("J", (), {"is_set": staticmethod(lambda: False)})
    )
    monkeypatch.setattr(
        attention.threading,
        "Thread",
        lambda target, args, **k: type("T", (), {"start": lambda s: started.append(args)})(),
    )
    syms = [f"S{i:02d}" for i in range(30)]
    assert attention.fill_events(syms) == 30
    assert len(started[0][0]) == attention.EVENTS_BATCH
    monkeypatch.setattr(attention, "fetch_events", lambda s: {"earnings": None, "ex_div": None})
    assert attention._dated_only("S00") is None
    attention.refresh_events(["S00"], fetch=attention._dated_only)
    assert "S00" not in attention.load_events()


def test_no_fill_while_a_job_runs(monkeypatch):
    monkeypatch.setattr(
        attention, "_JobRunning", type("J", (), {"is_set": staticmethod(lambda: True)})
    )
    monkeypatch.setattr(attention.threading, "Thread", lambda *a, **k: pytest.fail("started"))
    assert attention.fill_events(["AAA"]) == 1


# ------------------------------------------------------------- frontend and fetch_one
def test_manager_is_a_builtin_everywhere():
    assert "Manager" in persistence._BUILTIN_COLUMN_VIEW_NAMES
    views = re.search(r"const BUILTIN_VIEWS = \{(.*?)\n\};", _APP_JS, re.DOTALL).group(1)
    assert re.search(
        r'"Manager":\s*\["logo","symbol","name","weight","risk","risk_w","contrib"', views
    )
    assert 'const BUILTIN_ORDER = ["Default", "Fundamentals", "Momentum", "Manager"]' in _APP_JS


def test_coverage_display_rules():
    assert "const TOTAL_QUIET = 95;" in _APP_JS
    assert "Math.floor(e.cov * 100 + 1e-9)" in _APP_JS and "e.cov >= 1 - 1e-9" in _APP_JS
    assert A._TOTAL_COV_FLOOR == 0.40


def test_an_old_cached_payload_is_backfilled():
    i = _APP_JS.index("const backfill = ")
    assert "!cached.risk || !cached.totals" in _APP_JS[i : _APP_JS.index(";", i)]


def test_fetch_one_does_none_of_this():
    names = {n.id if isinstance(n, ast.Name) else n.attr
             for n in ast.walk(ast.parse(inspect.getsource(fetcher.fetch_one)))
             if isinstance(n, ast.Name | ast.Attribute)}  # fmt: skip
    banned = {"risk_parts", "fill_events", "fetch_events", "next_earnings", "_holdings_totals",
              "annualized_cov", "refresh_events"}  # fmt: skip
    assert not names & banned


def test_an_empty_calendar_waits_hours_and_a_failure_minutes(monkeypatch):
    monkeypatch.setattr(attention, "_EVENTS_EMPTY", {})
    monkeypatch.setattr(attention, "_EVENTS_TRIED", {})
    answers = {"EMPTY": {"earnings": None, "ex_div": None}, "FAIL": None}
    monkeypatch.setattr(attention, "fetch_events", lambda s: answers[s])
    attention.refresh_events(["EMPTY", "FAIL"], fetch=attention._dated_only)
    assert attention.load_events() == {}
    assert attention._due(["EMPTY", "FAIL"], {}, False) == []
    later = attention.time.monotonic() + attention.EVENTS_RETRY + 1
    monkeypatch.setattr(attention.time, "monotonic", lambda: later)
    assert attention._due(["EMPTY", "FAIL"], {}, False) == ["FAIL"]


def test_a_payload_without_model_figures_is_asked_again():
    i = _APP_JS.index("const backfill = ")
    assert (
        "!cached.totals.model_mu?.cov && DATA.some(r => r.model_mu != null)" in _APP_JS[i : i + 900]
    )
    assert "if (hit && TBL.a && !TBL.a.totals?.model_mu?.cov) requestAnalytics();" in _APP_JS


def test_a_resize_rechecks_the_sticky_state_with_fit_off():
    assert "if (STATE.fitColumns) render(); else applyTableZoom();" in _APP_JS


def test_the_total_says_all_only_when_it_is():
    rows = [ROWS[0], {**ROWS[1], "beta": None}]
    T = A._holdings_totals(rows, {**_payload({"AAA": 0.9996, "BBB": 0.0004}),
                                  "stats": {"beta": 1.2, "beta_coverage": 0.9996}})  # fmt: skip
    assert T["beta"]["cov"] == 0.9996 and T["beta"]["out"] == [["BBB", 0.0004, "none"]]
