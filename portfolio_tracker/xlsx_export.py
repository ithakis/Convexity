"""Excel export — one sheet per saved portfolio, intended to be the
"everything you can see in the app" artifact you'd hand to an AI/chatbot.

Design contract (matches the inline comment next to the Export button in
dashboard.py — keep them in sync when extending):

  • Every saved portfolio becomes its own sheet (sheet name = portfolio
    name, Excel-sanitized).
  • Each sheet contains, top-to-bottom:
       1. Meta block: portfolio name, constituents string, cache info.
         2. Portfolio analytics summary with static 1Y and 5Y risk/return
             sections (Sharpe / drawdown / beta / ann return / etc. from
             analyze_portfolios_multi). Time-series payloads are deliberately
             excluded from the workbook.
       3. Holdings table: one row per ticker, every field the row payload
          carries, PLUS analyst recommendations fetched at export time.
  • The first sheet ("Overview") lists every portfolio with the headline
    analytics side-by-side, for quick cross-portfolio comparison.

When new columns / analytics / fields land in the dashboard, add them
here so the file stays the comprehensive view. The format is forgiving:
unknown row keys go into the holdings table automatically (see
``HOLDINGS_PRIMARY_COLS`` + the "extras" pass at the end of each row).
"""

from __future__ import annotations

import io
import re
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Iterable, Optional

# openpyxl is an optional dep; the export endpoint will surface a friendly
# error if it's missing.
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import yfinance as yf

# Numeric coercion + dividend-yield normalisation live in helpers.py — the
# canonical copies. `_maybe_num` is an alias for `_safe_num` (identical finite-
# float-or-None semantics) kept only so the many call sites below read the same.
from portfolio_tracker.helpers import _normalize_dividend_yield, _safe_num as _maybe_num


# --------------------------------------------------------------------------
# Holdings table — primary columns in display order. Anything else found on
# the row payload gets appended as an "extras" column so additions to
# fetch_one show up automatically.
# --------------------------------------------------------------------------

HOLDINGS_PRIMARY_COLS: list[tuple[str, str]] = [
    # (row_key, header_label)
    ("symbol", "Ticker"),
    ("name", "Company"),
    ("exchange", "Exchange"),
    ("currency", "Currency"),
    ("price", "Price"),
    ("change_abs_1d", "Change ($)"),
    ("pct_1d", "% 1D"),
    ("pct_1w", "% 1W"),
    ("pct_1m", "% 1M"),
    ("pct_3m", "% 3M"),
    ("pct_6m", "% 6M"),
    ("pct_ytd", "% YTD"),
    ("pct_1y", "% 1Y"),
    ("market_cap", "Market Cap"),
    ("ps_ratio", "P/S"),
    ("pe_ratio", "P/E"),
    ("sector", "Sector"),
    ("industry", "Industry"),
    ("w52_high", "52W High"),
    ("w52_low", "52W Low"),
    ("ath", "2Y High"),
    ("delta_ath", "Δ 2Y High (%)"),
    ("above_sma_20", "Above 20SMA"),
    ("above_sma_50", "Above 50SMA"),
    ("above_sma_200", "Above 200SMA"),
    ("above_1m", "Above 1M"),
    ("volume", "Volume"),
    ("quote_type", "Type"),
    ("website", "Website"),
]

# Analyst columns appended to the right of the holdings table.
ANALYST_COLS: list[tuple[str, str]] = [
    ("recommendation_key",  "Analyst Rating"),
    ("recommendation_mean", "Rec. Mean (1=Buy,5=Sell)"),
    ("num_analysts",        "# Analysts"),
    ("target_mean",         "Target Mean"),
    ("target_high",         "Target High"),
    ("target_low",          "Target Low"),
    ("target_upside_pct",   "Target Upside (%)"),
    ("forward_pe",          "Forward P/E"),
    ("ev_ebitda",           "EV/EBITDA"),
    ("peg",                 "PEG"),
    ("beta_info",           "Beta (info)"),
    ("dividend_yield",      "Dividend Yield"),
]

# Fields explicitly skipped from the holdings "extras" pass — these are
# either large arrays (charts) or already reported elsewhere in the
# analyst block, so we don't want them appearing twice.
HOLDINGS_SKIP_EXTRAS = {
    "sparkline", "rs_rank", "error",
    # Pass D — these now ride on every row (via fetch_one) but are also
    # rendered in the dedicated ANALYST_COLS block; skip the auto-extras
    # pass so they don't duplicate.
    "forward_pe", "ev_ebitda", "recommendation_mean", "target_mean_price",
}

# Stats keys (under analytics["stats"]) in display order — these are the
# rows of the prominent "Portfolio Metrics" block.
STATS_KEYS: list[tuple[str, str]] = [
    ("total_return", "Total Return (%)"),
    ("ann_return",   "Annualised Return (%)"),
    ("ann_vol",      "Annualised Volatility (%)"),
    ("sharpe",       "Sharpe Ratio"),
    ("sortino",      "Sortino Ratio"),
    ("calmar",       "Calmar Ratio"),
    ("max_dd",       "Max Drawdown (%)"),
    ("beta_spy",     "Beta vs SPY"),
    ("r2_spy",       "R² vs SPY"),
    ("te_spy",       "Tracking Error vs SPY (%)"),
]

EXPORT_STATS_PERIODS: tuple[str, ...] = ("1Y", "5Y")

# Aggregated analyst-coverage keys (under analytics["analyst"]).
ANALYST_AGG_KEYS: list[tuple[str, str]] = [
    ("mean_rating",                "Mean Analyst Rating (1=Buy,5=Sell)"),
    ("rating_coverage_weight",     "Rating Coverage (weight)"),
    ("weighted_target_upside_pct", "Weighted Target Upside (%)"),
    ("target_coverage_weight",     "Target Coverage (weight)"),
    ("n_analysts_total",           "Total Analysts"),
    ("covered_count",              "Holdings Covered"),
    ("active_count",               "Active Holdings"),
]

# Concentration keys (under analytics["concentration"]).
CONCENTRATION_KEYS: list[tuple[str, str]] = [
    ("top5",         "Top-5 Concentration"),
    ("herfindahl",   "Herfindahl Index"),
    ("effective_n",  "Effective N (1/HHI)"),
]


# --------------------------------------------------------------------------
# Analyst-info fetch — lean wrapper that hits only Ticker.info, parallelised
# with a small worker pool and a per-process cache. fetch_detail() would
# also work but pulls full history which we don't need here.
# --------------------------------------------------------------------------

_ANALYST_CACHE: dict[str, dict] = {}
_ANALYST_TTL_S = 60 * 30  # 30 min


def _analyst_info_one(symbol: str) -> dict:
    now = time.time()
    cached = _ANALYST_CACHE.get(symbol)
    if cached and (now - cached.get("_t", 0)) < _ANALYST_TTL_S:
        return cached
    out: dict = {"_t": now}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tk = yf.Ticker(symbol)
            info = tk.info or {}
    except Exception:
        info = {}
    out["recommendation_key"]  = (info.get("recommendationKey") or "").lower() or None
    out["recommendation_mean"] = _maybe_num(info.get("recommendationMean"))
    out["num_analysts"]        = _maybe_num(info.get("numberOfAnalystOpinions"))
    out["target_mean"]         = _maybe_num(info.get("targetMeanPrice"))
    out["target_high"]         = _maybe_num(info.get("targetHighPrice"))
    out["target_low"]          = _maybe_num(info.get("targetLowPrice"))
    out["forward_pe"]          = _maybe_num(info.get("forwardPE"))
    out["ev_ebitda"]           = _maybe_num(info.get("enterpriseToEbitda"))
    out["peg"]                 = _maybe_num(info.get("pegRatio") or info.get("trailingPegRatio"))
    out["beta_info"]           = _maybe_num(info.get("beta"))
    out["dividend_yield"]      = _normalize_dividend_yield(
        info.get("dividendYield"),
        price=info.get("currentPrice") or info.get("regularMarketPrice"),
        dividend_rate=info.get("dividendRate"),
        trailing_yield=info.get("trailingAnnualDividendYield"),
        trailing_rate=info.get("trailingAnnualDividendRate"),
    )
    _ANALYST_CACHE[symbol] = out
    return out


def gather_analyst_info(symbols: Iterable[str], *, max_workers: int = 5) -> dict[str, dict]:
    """Dedup, fetch info in parallel, return ``{symbol: analyst_dict}``."""
    uniq = sorted({s for s in symbols if s})
    if not uniq:
        return {}
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_analyst_info_one, s): s for s in uniq}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                out[s] = fut.result()
            except Exception:
                out[s] = {}
    return out


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_INVALID_SHEET_CHARS = re.compile(r"[\\/?*\[\]:]")


def _sanitize_sheet_name(name: str, used: set[str]) -> str:
    """Excel: 31-char max, no `\\ / ? * [ ] :`, must be unique."""
    base = _INVALID_SHEET_CHARS.sub("_", (name or "Portfolio").strip())[:31] or "Portfolio"
    cand = base
    n = 2
    while cand.lower() in used:
        suffix = f"-{n}"
        cand = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(cand.lower())
    return cand


def _flatten_value(v):
    """Coerce a row-payload value to something openpyxl will accept."""
    if v is None:
        return None
    if isinstance(v, (int, float, str, bool)):
        return v
    if isinstance(v, (list, tuple)):
        # Compress lists into a count so the sheet stays readable.
        return f"[{len(v)} items]"
    if isinstance(v, dict):
        return f"[{len(v)} keys]"
    return str(v)


# --------------------------------------------------------------------------
# Sheet writers
# --------------------------------------------------------------------------

_HEADER_FILL = PatternFill("solid", fgColor="1F2937")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_SECTION_FONT = Font(bold=True, size=12)
_TITLE_FONT = Font(bold=True, size=14)


def _analytics_result_for_period(result: dict, set_name: str = "cap") -> dict:
    """Pull the single weight-set we asked for out of analyze_portfolios_multi's
    output. `set_name` is whatever key we passed in weight_sets (the active
    preset name, else "cap"), so the export reflects the portfolio's real
    weighting rather than always assuming cap-weighted."""
    if not isinstance(result, dict):
        return {"error": "invalid analytics payload"}
    if "results" in result:
        return result["results"].get(set_name) or {}
    if "error" in result:
        return {"error": result["error"]}
    return result.get(set_name) or result


def _first_available_analytics(analytics_by_period: dict[str, dict], periods: list[str]) -> dict:
    for period in periods:
        analytics = analytics_by_period.get(period) or {}
        if analytics and not analytics.get("error"):
            return analytics
    return {}


def _excess_vs_spy(analytics: dict) -> Optional[float]:
    if not isinstance(analytics, dict):
        return None
    port_tr = _maybe_num((analytics.get("stats") or {}).get("total_return"))
    spy_tr = _maybe_num((analytics.get("spy_stats") or {}).get("total_return"))
    if port_tr is None or spy_tr is None:
        return None
    return port_tr - spy_tr


def _metric_periods(primary_period: str) -> list[str]:
    periods: list[str] = []
    for candidate in (primary_period, *EXPORT_STATS_PERIODS):
        period = str(candidate or "").upper()
        if period and period not in periods:
            periods.append(period)
    return periods or list(EXPORT_STATS_PERIODS)


def _write_overview(wb: Workbook, summaries: list[dict], metric_periods: list[str]) -> None:
    ws = wb.active
    ws.title = "Overview"
    ws["A1"] = "Portfolio _App — Export"
    ws["A1"].font = _TITLE_FONT
    ws["A2"] = f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    ws["A2"].font = Font(italic=True, color="6B7280")

    headers = ["Portfolio", "Rows", "Cached At"]
    for period in metric_periods:
        headers.extend([
            f"{period} Ann Return %",
            f"{period} Ann Vol %",
            f"{period} Sharpe",
            f"{period} Max DD %",
            f"{period} Excess vs SPY %",
        ])
    row = 4
    _write_header_cells(ws, row, headers)
    for s in summaries:
        row += 1
        ws.cell(row=row, column=1, value=s.get("name"))
        ws.cell(row=row, column=2, value=s.get("rows"))
        ws.cell(row=row, column=3, value=s.get("saved_at"))
        col = 4
        analytics_by_period = s.get("analytics_by_period") or {}
        for period in metric_periods:
            analytics = analytics_by_period.get(period) or {}
            stats = analytics.get("stats") or {}
            ws.cell(row=row, column=col, value=_maybe_num(stats.get("ann_return"))); col += 1
            ws.cell(row=row, column=col, value=_maybe_num(stats.get("ann_vol"))); col += 1
            ws.cell(row=row, column=col, value=_maybe_num(stats.get("sharpe"))); col += 1
            ws.cell(row=row, column=col, value=_maybe_num(stats.get("max_dd"))); col += 1
            ws.cell(row=row, column=col, value=_excess_vs_spy(analytics)); col += 1

    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 18
    ws.column_dimensions["A"].width = 28


def _write_kv(ws, r: int, label: str, value, label_bold: bool = True) -> int:
    """Single key/value row helper. Returns the next row index."""
    a = ws.cell(row=r, column=1, value=label)
    if label_bold:
        a.font = Font(bold=True)
    ws.cell(row=r, column=2, value=value)
    return r + 1


def _write_section(ws, r: int, title: str) -> int:
    c = ws.cell(row=r, column=1, value=title)
    c.font = _SECTION_FONT
    return r + 1


def _write_header_cells(ws, r: int, labels: Iterable[str]) -> None:
    """Write one styled table-header row (bold white on dark, centered) from
    column 1. Shared by the overview, per-period metrics, and holdings tables
    so the three headers stay visually identical without repeating the
    fill/font/alignment triple at each call site."""
    for col_idx, label in enumerate(labels, 1):
        c = ws.cell(row=r, column=col_idx, value=label)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
        c.alignment = Alignment(horizontal="center")


def _per_symbol_analyst(analytics: dict) -> dict[str, dict]:
    """Pull the per-holding analyst rows out of analytics["analyst"]["holdings"]
    and key them by ticker, so the holdings table can look them up cheaply."""
    out: dict[str, dict] = {}
    if not isinstance(analytics, dict):
        return out
    ana = analytics.get("analyst") or {}
    for h in (ana.get("holdings") or []):
        if isinstance(h, dict) and h.get("symbol"):
            out[h["symbol"]] = h
    return out


def _write_portfolio_sheet(
    wb: Workbook,
    sheet_name: str,
    portfolio_name: str,
    view: dict,
    analytics_by_period: dict[str, dict],
    metric_periods: list[str],
    analyst_fallback: dict[str, dict],
    weighting_label: str = "cap-weighted",
) -> None:
    ws = wb.create_sheet(title=sheet_name)
    rows = view.get("rows") or []
    entries = view.get("entries") or ""
    saved_at = view.get("saved_at") or ""
    analytics_by_period = analytics_by_period or {}
    primary_analytics = _first_available_analytics(analytics_by_period, metric_periods)
    analyst_block = primary_analytics.get("analyst") or {}
    analyst_by_sym = _per_symbol_analyst(primary_analytics)
    exposure = primary_analytics.get("exposure") or {}
    concentration = primary_analytics.get("concentration") or {}
    has_analytics = any(a and not a.get("error") for a in analytics_by_period.values())

    # 1) Meta block
    r = 1
    c = ws.cell(row=r, column=1, value=portfolio_name)
    c.font = _TITLE_FONT
    r += 1
    r = _write_kv(ws, r, "Constituents:", entries)
    ws.cell(row=r - 1, column=2).alignment = Alignment(wrap_text=True)
    r = _write_kv(ws, r, "Cached at:", saved_at)
    r = _write_kv(ws, r, "Holdings:", len(rows))
    r += 1

    # 2) Portfolio metrics + benchmark comparison side-by-side.
    if not has_analytics:
        errors = [a.get("error") for a in analytics_by_period.values() if isinstance(a, dict) and a.get("error")]
        msg = f"(analytics unavailable: {errors[0]})" if errors else "(no analytics computed for this export)"
        ws.cell(row=r, column=1, value=msg).font = Font(italic=True, color="9CA3AF")
        r += 2
    else:
        for period in metric_periods:
            analytics = analytics_by_period.get(period) or {}
            r = _write_section(ws, r, f"Portfolio Metrics ({weighting_label}, {period})")
            if analytics.get("error"):
                ws.cell(row=r, column=1, value=f"(analytics unavailable: {analytics['error']})").font = Font(italic=True, color="9CA3AF")
                r += 2
                continue
            stats = analytics.get("stats") or {}
            spy_stats = analytics.get("spy_stats") or {}
            ndx_stats = analytics.get("nasdaq_stats") or {}
            _write_header_cells(ws, r, ["Metric", "Portfolio", "SPY", "NASDAQ"])
            r += 1
            for key, label in STATS_KEYS:
                ws.cell(row=r, column=1, value=label).font = Font(bold=True)
                ws.cell(row=r, column=2, value=_maybe_num(stats.get(key)))
                ws.cell(row=r, column=3, value=_maybe_num(spy_stats.get(key)))
                ws.cell(row=r, column=4, value=_maybe_num(ndx_stats.get(key)))
                r += 1
            r += 1

        # 3) Aggregated analyst coverage
        if analyst_block:
            r = _write_section(ws, r, "Analyst Coverage (portfolio-weighted)")
            for key, label in ANALYST_AGG_KEYS:
                v = analyst_block.get(key)
                if v is None:
                    continue
                r = _write_kv(ws, r, label, _maybe_num(v))
            # Rating distribution
            dist = analyst_block.get("distribution_pct") or {}
            if dist:
                ws.cell(row=r, column=1, value="Rating Distribution (%)").font = Font(bold=True)
                r += 1
                for k in ("strongBuy", "buy", "hold", "sell", "strongSell"):
                    if k in dist:
                        ws.cell(row=r, column=1, value=f"  {k}")
                        ws.cell(row=r, column=2, value=_maybe_num(dist.get(k)))
                        r += 1
            r += 1

        # 4) Sector / industry exposure
        sec = exposure.get("by_sector") if isinstance(exposure, dict) else None
        if sec:
            r = _write_section(ws, r, "Sector Exposure (weight)")
            for sname, w in sorted(sec.items(), key=lambda kv: kv[1], reverse=True):
                ws.cell(row=r, column=1, value=sname)
                ws.cell(row=r, column=2, value=_maybe_num(w))
                r += 1
            r += 1

        # 5) Concentration
        if concentration:
            r = _write_section(ws, r, "Concentration")
            for key, label in CONCENTRATION_KEYS:
                v = concentration.get(key)
                if v is None:
                    continue
                r = _write_kv(ws, r, label, _maybe_num(v))
            r += 1

    # 6) Holdings table — every row field + per-symbol analyst data
    r = _write_section(ws, r, "Holdings")

    # Discover any extra row-payload keys not in HOLDINGS_PRIMARY_COLS, so
    # future fetch_one extensions show up automatically.
    primary_keys = {k for k, _ in HOLDINGS_PRIMARY_COLS}
    extra_keys_all = set()
    for row in rows:
        if isinstance(row, dict):
            extra_keys_all.update(row.keys())
    extra_keys = sorted(extra_keys_all - primary_keys - HOLDINGS_SKIP_EXTRAS)
    extra_cols = [(k, k) for k in extra_keys]

    headers = HOLDINGS_PRIMARY_COLS + extra_cols + ANALYST_COLS
    _write_header_cells(ws, r, [label for _key, label in headers])
    header_row = r

    analyst_col_keys = {k for k, _ in ANALYST_COLS}

    for row_data in rows:
        if not isinstance(row_data, dict):
            continue
        r += 1
        sym = row_data.get("symbol")
        # Prefer analytics-engine per-symbol data (already fetched, richer);
        # fall back to the parallel info pull only if analytics wasn't run.
        ana_row = analyst_by_sym.get(sym) or {}
        fallback_row = analyst_fallback.get(sym, {}) if not ana_row else {}
        for col_idx, (key, _label) in enumerate(headers, 1):
            if key in analyst_col_keys:
                # Map our column key to whatever the source row uses.
                v = None
                if key == "target_upside_pct":
                    v = ana_row.get("upside_pct")
                    if v is None and fallback_row:
                        price = _maybe_num(row_data.get("price"))
                        tgt = _maybe_num(fallback_row.get("target_mean"))
                        if price and tgt and price > 0:
                            v = (tgt / price - 1.0) * 100.0
                elif key == "recommendation_key":
                    v = ana_row.get("rec_key") or fallback_row.get("recommendation_key")
                elif key == "recommendation_mean":
                    v = ana_row.get("mean_rating") or fallback_row.get("recommendation_mean")
                elif key == "num_analysts":
                    v = ana_row.get("n_analysts") or fallback_row.get("num_analysts")
                elif key in {"target_mean", "target_high", "target_low"}:
                    v = ana_row.get(key) or fallback_row.get(key)
                else:
                    # forward_pe, ev_ebitda, peg, beta_info, dividend_yield —
                    # not in the per-portfolio analyst block; rely on fallback.
                    v = fallback_row.get(key)
            else:
                v = row_data.get(key)
            ws.cell(row=r, column=col_idx, value=_flatten_value(v))

    # Approximate column widths.
    for col_idx, (_key, label) in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = max(12, len(str(label)) + 2)
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 32
    # Freeze the header row + the ticker column.
    ws.freeze_panes = ws.cell(row=header_row + 1, column=2)


# --------------------------------------------------------------------------
# Weighting resolution
# --------------------------------------------------------------------------

def _resolve_active_weights(view: dict, rows: list[dict]) -> tuple[dict[str, float], str, str]:
    """Resolve the weighting the export should use for one portfolio.

    Mirrors the frontend (``weightsForMode`` in app.js): the view's active
    weight preset — the ``weight_presets`` entry whose name == the view's
    ``active_weight_preset`` — projected onto the current row symbols, with
    symbols absent from the preset defaulting to 0 and negatives clamped to 0,
    then renormalized to sum 1.0. Falls back to market-cap weighting when there
    is no active preset, the named preset is missing, or the projection sums to
    <= 0 (e.g. the preset predates every current holding) — same cap fallback
    the frontend uses.

    Returns ``(weights, set_name, label)``: ``set_name`` is the key handed to
    analyze_portfolios_multi (so ``_analytics_result_for_period`` can pull the
    matching result back out), and ``label`` describes the weighting in the
    sheet's "Portfolio Metrics" header.
    """
    active_name = str((view or {}).get("active_weight_preset") or "").strip()
    if active_name:
        presets = view.get("weight_presets")
        preset = None
        if isinstance(presets, list):
            for p in presets:
                if isinstance(p, dict) and str(p.get("name") or "").strip() == active_name:
                    preset = p
                    break
        if isinstance(preset, dict):
            # Preset weight keys are stored upper-cased (persistence.
            # _normalize_preset_weights); match row symbols the same way.
            raw = preset.get("weights") if isinstance(preset.get("weights"), dict) else {}
            projected: dict[str, float] = {}
            total = 0.0
            for r in rows:
                sym = r.get("symbol") if isinstance(r, dict) else None
                if not sym:
                    continue
                w = _maybe_num(raw.get(str(sym).strip().upper()))
                w = w if (w is not None and w > 0) else 0.0
                projected[sym] = w
                total += w
            if total > 0:
                return ({s: w / total for s, w in projected.items()}, active_name, active_name)
    # No usable preset → market-cap weighting (matches the frontend fallback).
    return (_cap_weights(rows), "cap", "cap-weighted")


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def build_workbook(
    views_map: dict[str, dict],
    *,
    analytics_runner=None,
    period: str = "1Y",
    display_ccy: str = "USD",
) -> bytes:
    """Build an .xlsx blob covering every saved portfolio.

    Parameters
    ----------
    views_map
        ``{portfolio_name: view_dict}`` — view_dict has keys ``entries``,
        ``rows``, ``saved_at``, ``stale``.
    analytics_runner
        Optional callable ``(rows, weight_sets, period, display_ccy) -> dict``
        used to compute the analytics block per portfolio. Pass
        ``dashboard.analyze_portfolios_multi`` here. When omitted, sheets
        still build but the analytics block notes that nothing was run.
    period, display_ccy
        Forwarded to ``analytics_runner``.
    """
    wb = Workbook()
    used_names: set[str] = set()
    metric_periods = _metric_periods(period)

    # Pre-pass: collect valid views + every unique symbol across portfolios.
    valid_views: list[tuple[str, dict]] = []
    all_symbols: set[str] = set()
    for name, view in views_map.items():
        if not isinstance(view, dict):
            continue
        rows = view.get("rows") or []
        if not rows:
            continue
        valid_views.append((name, view))
        for r in rows:
            if isinstance(r, dict) and r.get("symbol"):
                all_symbols.add(str(r["symbol"]))

    # Run analytics per portfolio. Track which symbols got per-row analyst
    # data from the engine (the rich `analyst.holdings` block) so we only
    # do the redundant info-only fetch for the leftovers.
    summaries: list[dict] = []
    # (name, view, analytics_by_period, weighting_label)
    portfolio_analytics: list[tuple[str, dict, dict[str, dict], str]] = []
    covered_symbols: set[str] = set()
    for name, view in valid_views:
        rows = view.get("rows") or []
        # Use the portfolio's own saved active weighting, not a blanket cap
        # assumption, so the exported metrics match what the user sees.
        weights, set_name, weighting_label = _resolve_active_weights(view, rows)
        analytics_by_period: dict[str, dict] = {}
        if analytics_runner is not None:
            for analytics_period in metric_periods:
                try:
                    result = analytics_runner(rows, {set_name: weights}, analytics_period, display_ccy)
                    analytics_by_period[analytics_period] = _analytics_result_for_period(result, set_name)
                except Exception as exc:
                    analytics_by_period[analytics_period] = {"error": f"runner failed: {exc}"}
        portfolio_analytics.append((name, view, analytics_by_period, weighting_label))
        # Symbols already covered by analytics["analyst"]["holdings"].
        for analytics in analytics_by_period.values():
            for h in ((analytics.get("analyst") or {}).get("holdings") or []):
                if isinstance(h, dict) and h.get("symbol"):
                    covered_symbols.add(h["symbol"])

    # Fallback info fetch ONLY for symbols not covered by the engine's
    # per-holding analyst block. Avoids the slow redundant info call when
    # analytics succeeded for every symbol (the common case).
    leftovers = all_symbols - covered_symbols
    analyst_fallback = gather_analyst_info(leftovers) if leftovers else {}

    # Write per-portfolio sheets + summaries.
    for name, view, analytics_by_period, weighting_label in portfolio_analytics:
        sheet_name = _sanitize_sheet_name(name, used_names)
        _write_portfolio_sheet(wb, sheet_name, name, view, analytics_by_period,
                               metric_periods, analyst_fallback, weighting_label)
        summaries.append({
            "name": name,
            "rows": len(view.get("rows") or []),
            "saved_at": view.get("saved_at"),
            "analytics_by_period": analytics_by_period,
        })

    _write_overview(wb, summaries, metric_periods)
    # Move Overview to the front (openpyxl appends it after creation).
    wb.move_sheet("Overview", offset=-len(wb.sheetnames) + 1)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _cap_weights(rows: list[dict]) -> dict[str, float]:
    """Market-cap weighting from row payloads. Symbols without a usable
    market cap get an equal share of the residual after the cap-weighted
    symbols sum, so the result is always normalised to 1.0."""
    caps: dict[str, float] = {}
    no_cap: list[str] = []
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        mc = _maybe_num(r.get("market_cap"))
        if mc and mc > 0:
            caps[sym] = mc
        else:
            no_cap.append(sym)
    total_cap = sum(caps.values())
    if total_cap <= 0:
        # All symbols capless → equal weights.
        n = max(1, len(rows))
        return {r["symbol"]: 1.0 / n for r in rows if r.get("symbol")}
    weights: dict[str, float] = {s: c / total_cap for s, c in caps.items()}
    if no_cap:
        # Reserve 5% of the book to share equally among capless tickers,
        # rescale the rest. Keeps capless rows from being silently dropped.
        share = 0.05 / len(no_cap)
        for s in no_cap:
            weights[s] = share
        scale = 0.95
        for s in caps:
            weights[s] *= scale
    return weights
