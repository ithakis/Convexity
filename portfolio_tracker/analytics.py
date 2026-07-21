"""Portfolio analytics — bulk close, analyst blocks, multi-weight analysis."""

from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf

from portfolio_tracker.cache import (
    _BULK_CLOSE_MISS,
    _CACHE_TTL_ANALYTICS,
    _bulk_close_get_cached, _bulk_close_put,
    _cache_get, _cache_put,
)
from portfolio_tracker.fetcher import _SECTOR_ETF
from portfolio_tracker.fx import _apply_fx_to_closes, _norm_ccy_for_fx
from portfolio_tracker.helpers import (
    _safe_num,
    _series_to_points,
)

_PERIOD_YF = {
    "3M": "3mo", "6M": "6mo", "YTD": "ytd",
    "1Y": "1y", "3Y": "3y", "5Y": "5y", "MAX": "max",
}
_PERIOD_DAYS = {
    "3M": 91, "6M": 182, "YTD": None, "1Y": 365,
    "3Y": 365 * 3, "5Y": 365 * 5, "MAX": None,
}


def _normalize_weights(weights_in: dict, symbols: list[str]) -> dict[str, float]:
    raw: dict[str, float] = {}
    for s in symbols:
        v = weights_in.get(s)
        try:
            f = float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            f = 0.0
        raw[s] = max(0.0, f)
    total = sum(raw.values())
    if total <= 0:
        n = len(symbols)
        if not n:
            return {}
        return {s: 1.0 / n for s in symbols}
    return {s: v / total for s, v in raw.items()}


def _mcap_bucket(mcap: float | None) -> str:
    if mcap is None or not math.isfinite(mcap) or mcap <= 0:
        return "Unknown"
    if mcap >= 2e11:
        return "Mega ($200B+)"
    if mcap >= 1e10:
        return "Large ($10B–200B)"
    if mcap >= 2e9:
        return "Mid ($2B–10B)"
    if mcap >= 3e8:
        return "Small ($300M–2B)"
    return "Micro (<$300M)"


def _bulk_close(symbols: list[str], period: str) -> pd.DataFrame:
    """Close prices indexed by date, columns = symbols (those that returned data).

    Caches per-symbol. Retries missing symbols individually with backoff."""
    if not symbols:
        return pd.DataFrame()
    period_yf = _PERIOD_YF.get(period.upper(), "1y")

    out: dict[str, pd.Series] = {}
    to_fetch: list[str] = []
    for s in symbols:
        cached = _bulk_close_get_cached(s, period_yf)
        if cached is _BULK_CLOSE_MISS:
            to_fetch.append(s)
        elif cached is not None and not cached.empty:
            out[s] = cached

    if to_fetch:
        try:
            df = yf.download(
                tickers=to_fetch, period=period_yf, interval="1d",
                auto_adjust=True, group_by="ticker", threads=True, progress=False,
            )
        except Exception:
            df = None
        got: set[str] = set()
        if df is not None and not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                for s in to_fetch:
                    try:
                        col = df[s]["Close"].dropna()
                    except (KeyError, ValueError):
                        continue
                    if not col.empty:
                        out[s] = col
                        _bulk_close_put(s, period_yf, col)
                        got.add(s)
            else:
                try:
                    col = df["Close"].dropna()
                    if not col.empty:
                        out[to_fetch[0]] = col
                        _bulk_close_put(to_fetch[0], period_yf, col)
                        got.add(to_fetch[0])
                except (KeyError, ValueError):
                    pass

        missing = [s for s in to_fetch if s not in got]
        for i, s in enumerate(missing):
            delay = 0.4 + 0.2 * min(i, 6)
            time.sleep(delay)
            ser = None
            for attempt in range(3):
                try:
                    tk = yf.Ticker(s)
                    h = tk.history(period=period_yf, interval="1d", auto_adjust=True)
                    if h is not None and not h.empty:
                        col = h["Close"].dropna()
                        if not col.empty:
                            try:
                                if getattr(col.index, "tz", None) is not None:
                                    col.index = col.index.tz_convert(None)
                            except Exception:
                                try:
                                    col.index = col.index.tz_localize(None)
                                except Exception:
                                    pass
                            ser = col
                            break
                except Exception:
                    pass
                time.sleep(0.6 * (attempt + 1))
            if ser is not None:
                out[s] = ser
                _bulk_close_put(s, period_yf, ser)
            else:
                _bulk_close_put(s, period_yf, pd.Series(dtype=float))

    if not out:
        return pd.DataFrame()
    return pd.concat(out, axis=1).sort_index()


def _analyst_for(symbol: str, row: dict | None = None) -> dict:
    """Analyst block for one symbol, reusing the fields `fetch_one` already put
    on the row instead of re-fetching them.

    The streaming row already carries recommendation_mean / target_mean_price /
    rec_key / n_analysts / rating_dist / dividend_yield / ev_ebitda / price /
    currency per symbol, so re-hitting Ticker.info + tk.recommendations for those
    is pure waste. Only the target low/high/median trio is absent from the row,
    so that trio is the *sole* reason to touch Ticker.info here — and
    tk.recommendations is dropped entirely (dist comes from row["rating_dist"]).
    Net effect: cold analytics drops from ~2 yfinance calls/active symbol
    (tk.info via _safe_info + tk.recommendations) to at most 1 (a bare tk.info
    for the target trio), and to 0 when the row already supplies that trio.
    Output shape is byte-for-byte identical to before (same 12 keys)."""
    row = row or {}
    price = _safe_num(row.get("price"))
    out = {
        "mean_rating": _safe_num(row.get("recommendation_mean")),
        "rec_key": (row.get("rec_key") or "").strip().lower() or None,
        "n_analysts": _safe_num(row.get("n_analysts")),
        "target_mean": _safe_num(row.get("target_mean_price")),
        # Row's dividend_yield is already normalized to a fraction at ingestion
        # (fetcher._normalize_dividend_yield) — reuse it, don't re-derive.
        "div_yield": _safe_num(row.get("dividend_yield")),
        "ev_ebitda": _safe_num(row.get("ev_ebitda")),
        "price": price,
        "currency": (row.get("currency") or "").upper() or None,
        "dist": row.get("rating_dist") if isinstance(row.get("rating_dist"), dict) else None,
    }

    # Prefer any target trio the row supplies (future-proofing); only the gaps
    # justify a network call. A single bare tk.info covers all three — cheaper
    # than _safe_info (which additionally pings fast_info we don't need here).
    trio = {
        "target_low": _safe_num(row.get("target_low")),
        "target_high": _safe_num(row.get("target_high")),
        "target_median": _safe_num(row.get("target_median")),
    }
    if any(v is None for v in trio.values()):
        cache_key = f"analyst_targets|{symbol}"
        hit = _cache_get(cache_key)
        if hit is not None:
            for k in trio:
                if trio[k] is None:
                    trio[k] = hit.get(k)
        else:
            info: dict = {}
            try:
                info = yf.Ticker(symbol).info or {}
            except Exception:
                info = {}
            fetched = {
                "target_low": _safe_num(info.get("targetLowPrice")),
                "target_high": _safe_num(info.get("targetHighPrice")),
                "target_median": _safe_num(info.get("targetMedianPrice")),
            }
            for k in trio:
                if trio[k] is None:
                    trio[k] = fetched[k]
            thin = all(v is None for v in fetched.values())
            _cache_put(cache_key, fetched, ttl=300.0 if thin else _CACHE_TTL_ANALYTICS)
    out.update(trio)
    return out


def analyze_portfolio(rows: list[dict], weights_in: dict, period: str, display_ccy: str = "USD") -> dict:
    out = analyze_portfolios_multi(rows, {"__single__": weights_in or {}}, period, display_ccy=display_ccy)
    if isinstance(out, dict) and "error" in out:
        return out
    return (out or {}).get("__single__", {"error": "no result"})


def analyze_portfolios_multi(
    rows: list[dict],
    weight_sets: dict[str, dict],
    period: str,
    display_ccy: str = "USD",
) -> dict:
    period_u = (period or "1Y").upper()
    if period_u not in _PERIOD_YF:
        period_u = "1Y"

    display_ccy = _norm_ccy_for_fx(display_ccy or "USD")

    rows = [r for r in (rows or []) if r and r.get("symbol")]
    symbols = [str(r["symbol"]) for r in rows]
    if not symbols:
        return {"error": "no symbols"}

    if not isinstance(weight_sets, dict) or not weight_sets:
        weight_sets = {"__single__": {}}

    by_sym = {r["symbol"]: r for r in rows}

    normalized_sets: dict[str, dict[str, float]] = {
        name: _normalize_weights(w_in or {}, symbols) for name, w_in in weight_sets.items()
    }

    per_set_cache_keys: dict[str, str] = {}
    cached_results: dict[str, dict] = {}
    for name, weights in normalized_sets.items():
        key = "pf|" + "|".join(f"{s}:{weights[s]:.6f}" for s in sorted(symbols)) + f"|{period_u}|{display_ccy}"
        per_set_cache_keys[name] = key
        hit = _cache_get(key)
        if hit is not None:
            cached_results[name] = hit
    if len(cached_results) == len(normalized_sets):
        return cached_results

    closes = _bulk_close(symbols + ["SPY", "QQQ"], period_u)
    warnings: list[str] = []
    missing = [s for s in symbols if s not in closes.columns]
    if missing:
        warnings.append(f"No price history for: {', '.join(missing)}")
    spy = closes["SPY"].dropna() if "SPY" in closes.columns else None
    ndx = closes["QQQ"].dropna() if "QQQ" in closes.columns else None
    sym_closes = closes[[s for s in symbols if s in closes.columns]].dropna(how="all")
    if sym_closes.empty:
        return {"error": "no price history for portfolio", "warnings": warnings}

    active = [s for s in symbols if s in sym_closes.columns]
    if not active:
        return {"error": "no price history for portfolio", "warnings": warnings}

    period_yf = _PERIOD_YF.get(period_u, "1y")
    if display_ccy != "USD":
        combined = sym_closes.copy()
        if spy is not None and not spy.empty:
            combined = combined.join(spy.rename("__SPY__"), how="outer")
        if ndx is not None and not ndx.empty:
            combined = combined.join(ndx.rename("__QQQ__"), how="outer")
        combined_by_sym = dict(by_sym)
        combined_by_sym["__SPY__"] = {"currency": "USD"}
        combined_by_sym["__QQQ__"] = {"currency": "USD"}
        converted = _apply_fx_to_closes(combined, combined_by_sym, display_ccy, period_yf)
        sym_closes = converted[[c for c in converted.columns if c not in ("__SPY__", "__QQQ__")]]
        if "__SPY__" in converted.columns:
            spy = converted["__SPY__"].dropna()
        if "__QQQ__" in converted.columns:
            ndx = converted["__QQQ__"].dropna()

    sym_closes = sym_closes.ffill().dropna(how="any")
    if sym_closes.empty or len(sym_closes) < 3:
        return {"error": "insufficient overlapping history", "warnings": warnings}

    daily_ret = sym_closes.pct_change().dropna(how="all").fillna(0.0)
    common_index = sym_closes.index

    spy_aligned_raw = None
    spy_ret_full = None
    if spy is not None and not spy.empty:
        spy_aligned_raw = spy.reindex(common_index).ffill().dropna()
        if len(spy_aligned_raw) >= 2:
            spy_ret_full = spy_aligned_raw.pct_change().dropna()
        else:
            spy_aligned_raw = None

    ndx_aligned_raw = None
    ndx_ret_full = None
    if ndx is not None and not ndx.empty:
        ndx_aligned_raw = ndx.reindex(common_index).ffill().dropna()
        if len(ndx_aligned_raw) >= 2:
            ndx_ret_full = ndx_aligned_raw.pct_change().dropna()
        else:
            ndx_aligned_raw = None

    all_sectors = {(by_sym.get(s, {}).get("sector") or "").strip() for s in active}
    all_sectors.discard("")
    sec_etfs = list({_SECTOR_ETF.get(sec) for sec in all_sectors if _SECTOR_ETF.get(sec)})
    sec_ret_df = None
    if sec_etfs:
        sec_closes_df = _bulk_close(sec_etfs, period_u)
        if not sec_closes_df.empty:
            sec_closes_df = sec_closes_df.reindex(common_index).ffill().dropna(how="any")
            if not sec_closes_df.empty:
                sec_ret_df = sec_closes_df.pct_change().fillna(0.0)

    analyst_blocks: dict[str, dict] = {}
    if active:
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(active)))) as pool:
            futs = {pool.submit(_analyst_for, s, by_sym.get(s, {})): s for s in active}
            for fut in as_completed(futs):
                try:
                    analyst_blocks[futs[fut]] = fut.result()
                except Exception:
                    analyst_blocks[futs[fut]] = {}

    pe_vals = {s: _safe_num(by_sym.get(s, {}).get("pe_ratio")) for s in active}
    ps_vals = {s: _safe_num(by_sym.get(s, {}).get("ps_ratio")) for s in active}
    ev_vals = {s: analyst_blocks.get(s, {}).get("ev_ebitda") for s in active}
    div_vals = {s: analyst_blocks.get(s, {}).get("div_yield") for s in active}
    mcap_vals = {s: _safe_num(by_sym.get(s, {}).get("market_cap")) for s in active}

    period_returns: dict[str, float] = {}
    for s in active:
        col = sym_closes[s]
        period_returns[s] = float(col.iloc[-1] / col.iloc[0] - 1.0) * 100.0 if len(col) >= 2 else 0.0

    def _stats(ret: pd.Series, val: pd.Series) -> dict:
        if ret.empty or val.empty:
            return {}
        n_days = (val.index[-1] - val.index[0]).days or 1
        years = max(n_days / 365.25, 1e-6)
        total_return = float(val.iloc[-1] / val.iloc[0] - 1.0) * 100.0
        ann_return = float((val.iloc[-1] / val.iloc[0]) ** (1.0 / years) - 1.0) * 100.0
        ann_vol = float(ret.std() * math.sqrt(252)) * 100.0
        sharpe = float((ret.mean() * 252) / (ret.std() * math.sqrt(252))) if ret.std() else None
        downside = math.sqrt((ret.clip(upper=0) ** 2).mean())
        sortino = float((ret.mean() * 252) / (downside * math.sqrt(252))) if downside > 0 else None
        run_mx = val.cummax()
        max_dd = float((val / run_mx - 1.0).min() * 100.0)
        calmar = (ann_return / abs(max_dd)) if max_dd < 0 else None
        return {
            "total_return": total_return, "ann_return": ann_return,
            "ann_vol": ann_vol, "sharpe": sharpe, "sortino": sortino,
            "max_dd": max_dd, "calmar": calmar,
        }

    spy_stats_shared = {}
    spy_aligned_rebased = None
    if spy_aligned_raw is not None:
        spy_aligned_rebased = 100.0 * spy_aligned_raw / spy_aligned_raw.iloc[0]
        spy_stats_shared = _stats(spy_ret_full, spy_aligned_rebased)
        spy_stats_shared.setdefault("beta_spy", 1.0)
        spy_stats_shared.setdefault("r2_spy", 1.0)
        spy_stats_shared.setdefault("te_spy", 0.0)
    spy_points_shared = _series_to_points(spy_aligned_rebased) if spy_aligned_rebased is not None else []
    spy_var_full = float(spy_ret_full.var()) if (spy_ret_full is not None and len(spy_ret_full) > 30) else None

    ndx_stats_shared = {}
    ndx_aligned_rebased = None
    if ndx_aligned_raw is not None:
        ndx_aligned_rebased = 100.0 * ndx_aligned_raw / ndx_aligned_raw.iloc[0]
        ndx_stats_shared = _stats(ndx_ret_full, ndx_aligned_rebased)
        if spy_ret_full is not None and spy_var_full and len(spy_ret_full) > 30:
            common_n = ndx_ret_full.index.intersection(spy_ret_full.index)
            if len(common_n) >= 30:
                rp = ndx_ret_full.loc[common_n]
                rb = spy_ret_full.loc[common_n]
                cov = float(rp.cov(rb))
                var_b = float(rb.var())
                if var_b:
                    ndx_stats_shared["beta_spy"] = cov / var_b
                corr = rp.corr(rb)
                if pd.notna(corr):
                    ndx_stats_shared["r2_spy"] = float(corr * corr)
                te = (rp - rb).std()
                if te and pd.notna(te):
                    ndx_stats_shared["te_spy"] = float(te * math.sqrt(252) * 100.0)
    ndx_points_shared = _series_to_points(ndx_aligned_rebased) if ndx_aligned_rebased is not None else []

    daily_ret_active = daily_ret[active]

    results: dict[str, dict] = dict(cached_results)
    for name, weights in normalized_sets.items():
        if name in results:
            continue

        if len(active) != len(symbols):
            weights = _normalize_weights({s: weights.get(s, 0.0) for s in active}, active)

        w_vec = pd.Series([weights[s] for s in active], index=active)
        port_ret = (daily_ret_active * w_vec).sum(axis=1)
        if not port_ret.empty:
            port_growth = (1.0 + port_ret).cumprod()
            port_val = pd.concat([
                pd.Series([1.0], index=[common_index[0]]),
                port_growth,
            ]) * 100.0
        else:
            port_val = pd.Series(dtype=float)
        drawdown = (port_val / port_val.cummax() - 1.0) * 100.0

        sec_blend = None
        if sec_ret_df is not None:
            sector_alloc: dict[str, float] = {}
            for s in active:
                sec = (by_sym.get(s, {}).get("sector") or "").strip()
                if sec:
                    sector_alloc[sec] = sector_alloc.get(sec, 0.0) + weights[s]
            if sector_alloc:
                pairs = [(sec, _SECTOR_ETF.get(sec), w) for sec, w in sector_alloc.items() if _SECTOR_ETF.get(sec)]
                sec_w_total = sum(w for _, _, w in pairs)
                parts = [sec_ret_df[etf] * (w / sec_w_total)
                         for _, etf, w in pairs
                         if sec_w_total > 0 and etf in sec_ret_df.columns]
                if parts:
                    sec_blend_ret = sum(parts)
                    sec_blend_growth = (1.0 + sec_blend_ret).cumprod()
                    sec_blend = pd.concat([
                        pd.Series([1.0], index=[common_index[0]]),
                        sec_blend_growth,
                    ]) * 100.0

        pf_stats = _stats(port_ret, port_val)

        beta_spy = r2_spy = te_spy = None
        if spy_ret_full is not None and spy_var_full and len(spy_ret_full) > 30:
            common = port_ret.index.intersection(spy_ret_full.index)
            if len(common) >= 30:
                rp, rb = port_ret.loc[common], spy_ret_full.loc[common]
                var_b = float(rb.var())
                cov = float(rp.cov(rb))
                beta_spy = cov / var_b if var_b else None
                corr = rp.corr(rb)
                r2_spy = float(corr * corr) if pd.notna(corr) else None
                te = (rp - rb).std()
                te_spy = float(te * math.sqrt(252) * 100.0) if te and pd.notna(te) else None

        def _w_avg(values, _w=weights):
            num = 0.0
            denom = 0.0
            for s, v in values.items():
                if v is None or not math.isfinite(float(v)):
                    continue
                num += float(v) * _w.get(s, 0.0)
                denom += _w.get(s, 0.0)
            return num / denom if denom > 0 else None

        weighted = {
            "pe": _w_avg(pe_vals), "ps": _w_avg(ps_vals),
            "ev_ebitda": _w_avg(ev_vals), "div_yield": _w_avg(div_vals),
            "market_cap": _w_avg(mcap_vals),
        }

        rating_num = rating_w = upside_num = upside_w = 0.0
        n_analysts_total = 0
        dist_sum = {"strongBuy": 0.0, "buy": 0.0, "hold": 0.0, "sell": 0.0, "strongSell": 0.0}
        dist_w = 0.0
        holdings_out: list[dict] = []
        not_covered: list[dict] = []
        for s in active:
            blk = analyst_blocks.get(s, {})
            row = by_sym.get(s, {})
            w = weights.get(s, 0.0)
            mr = blk.get("mean_rating")
            tgt = blk.get("target_mean")
            tgt_med = blk.get("target_median")
            tgt_lo = blk.get("target_low")
            tgt_hi = blk.get("target_high")
            px = _safe_num(row.get("price")) or blk.get("price")
            na = blk.get("n_analysts")
            dist = blk.get("dist")
            upside = None
            if tgt and px and px > 0:
                upside = (float(tgt) / float(px) - 1.0) * 100.0
                upside_num += upside * w
                upside_w += w
            if mr is not None and math.isfinite(float(mr)):
                rating_num += float(mr) * w
                rating_w += w
            if na is not None and math.isfinite(float(na)):
                n_analysts_total += int(na)
            if dist and isinstance(dist, dict):
                tot_votes = sum(int(v or 0) for v in dist.values())
                if tot_votes > 0:
                    for k in dist_sum:
                        dist_sum[k] += float(dist.get(k, 0) or 0) * w
                    dist_w += w
            has_coverage = bool((na and na > 0) or mr is not None or tgt or dist)
            if has_coverage:
                holdings_out.append({
                    "symbol": s, "name": row.get("name") or s,
                    "currency": row.get("currency") or blk.get("currency") or "USD",
                    "weight": w, "price": px,
                    "target_mean": tgt, "target_median": tgt_med,
                    "target_low": tgt_lo, "target_high": tgt_hi,
                    "upside_pct": upside, "mean_rating": mr,
                    "rec_key": blk.get("rec_key"),
                    "n_analysts": int(na) if (na is not None and math.isfinite(float(na))) else None,
                    "dist": dist,
                })
            else:
                not_covered.append({"symbol": s, "name": row.get("name") or s, "weight": w})
        holdings_out.sort(key=lambda h: (-(h.get("weight") or 0), h["symbol"]))
        dist_norm = None
        if dist_w > 0:
            dist_total_w = sum(dist_sum.values())
            if dist_total_w > 0:
                dist_norm = {k: (v / dist_total_w) * 100.0 for k, v in dist_sum.items()}
        analyst = {
            "mean_rating": (rating_num / rating_w) if rating_w > 0 else None,
            "rating_coverage_weight": rating_w,
            "weighted_target_upside_pct": (upside_num / upside_w) if upside_w > 0 else None,
            "target_coverage_weight": upside_w,
            "n_analysts_total": n_analysts_total,
            "distribution_pct": dist_norm, "distribution_weight": dist_w,
            "holdings": holdings_out, "not_covered": not_covered,
            "covered_count": len(holdings_out), "active_count": len(active),
        }

        by_sector: dict[str, float] = {}
        by_industry: dict[str, float] = {}
        by_bucket: dict[str, float] = {}
        by_country: dict[str, float] = {}
        for s in active:
            r = by_sym.get(s, {})
            w = weights.get(s, 0.0)
            sec = (r.get("sector") or "Unknown").strip() or "Unknown"
            ind = (r.get("industry") or "Unknown").strip() or "Unknown"
            country = (r.get("country") or "Unknown").strip() or "Unknown"
            bucket = _mcap_bucket(_safe_num(r.get("market_cap")))
            by_sector[sec] = by_sector.get(sec, 0.0) + w
            by_industry[ind] = by_industry.get(ind, 0.0) + w
            by_country[country] = by_country.get(country, 0.0) + w
            by_bucket[bucket] = by_bucket.get(bucket, 0.0) + w

        weights_sorted = sorted(weights.values(), reverse=True)
        top5 = float(sum(weights_sorted[:5])) if weights_sorted else 0.0
        herfindahl = float(sum(w * w for w in weights.values()))
        effective_n = (1.0 / herfindahl) if herfindahl > 0 else 0.0

        contribution = []
        for s in active:
            w = weights.get(s, 0.0)
            pr = period_returns.get(s, 0.0)
            contribution.append({
                "symbol": s, "name": by_sym.get(s, {}).get("name") or s,
                "weight": w, "period_return": pr, "contribution": w * pr,
                "sector": by_sym.get(s, {}).get("sector") or "",
            })
        contribution.sort(key=lambda x: x["contribution"], reverse=True)

        out_one = {
            "period": period_u, "display_ccy": display_ccy,
            "weights_applied": weights, "active_symbols": active,
            "missing_symbols": missing,
            "series": {
                "portfolio": _series_to_points(port_val),
                "spy": spy_points_shared, "nasdaq": ndx_points_shared,
                "sector_mix": _series_to_points(sec_blend) if sec_blend is not None else [],
                "drawdown": _series_to_points(drawdown),
            },
            "stats": {**pf_stats, "beta_spy": beta_spy, "r2_spy": r2_spy, "te_spy": te_spy},
            "spy_stats": spy_stats_shared, "nasdaq_stats": ndx_stats_shared,
            "weighted": weighted, "analyst": analyst,
            "exposure": {
                "by_sector": by_sector, "by_industry": by_industry,
                "by_bucket": by_bucket, "by_country": by_country,
            },
            "concentration": {"top5": top5, "herfindahl": herfindahl, "effective_n": effective_n},
            "contribution": contribution, "warnings": warnings,
        }
        _cache_put(per_set_cache_keys[name], out_one, ttl=_CACHE_TTL_ANALYTICS)
        results[name] = out_one

    return results
