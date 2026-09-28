"""`convexity build-reference-pack` — build the daily reference pack.

Runs in `.github/workflows/reference-pack.yml` from the same commit as the app
(`uv sync --locked`), so the featurizer, the relevance heuristic, the article
window and the model are the app's own by construction. Per run:

1. The universe (src/convexity/data/sp500.json, `--limit N` for a quick run).
2. News per ticker through `news_sentiment.collect_company_news` — Finnhub +
   yfinance, deduplicated and window-sampled exactly as the app retains them —
   under the app's own rate limiters (Finnhub 55/min, yfinance news 40/min).
3. The Market read ONLY (`ml_sentiment.market_read`, the local LightGBM model
   fetched and SHA-256 checked by model_fetch). No LLM. Its percentile is
   anchored on the PREVIOUS pack's last 90 days — only scores from earlier
   runs, never this run's — so a published tier never used later data.
4. Forward idiosyncratic returns (`news_diagnostics.forward_idio`, the Track
   record's own definition) joined onto earlier records once the closes exist.
5. anchor.json.gz, history.json.gz and manifest.json into `--out` (format and
   validators: reference_pack.py). No headline, summary or URL is written.

The Finnhub key comes from the FINNHUB_API_KEY environment variable ONLY —
never config.json or a legacy key file — so running this from a checkout that
holds the owner's key files cannot spend their quota by accident; in CI it is
the Actions secret. `--returns-only` fetches just the universe's prices (and
probes yfinance news on a few names) to check that Yahoo answers from the
runner's IP; it writes nothing.

A degraded run is never published: fewer than half the names scored, or
Finnhub failing for most of them, exits non-zero with nothing written.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

from convexity import reference_pack as rp

_WORKERS = 4  # enough to keep both rate limiters busy; they set the pace
_MIN_SCORED_SHARE = 0.5
_BETA_DAYS = 252
_BETA_MIN_OBS = 60


def universe() -> dict:
    """sp500.json: {source, as_of, symbols: [{symbol, name, sector}]}."""
    return json.loads(resources.files("convexity").joinpath("data/sp500.json").read_text("utf-8"))


def finnhub_symbol(symbol: str) -> str:
    """Finnhub spells class shares with a dot (BRK.B); Yahoo with a dash."""
    return symbol.replace("-", ".")


def _r(x, nd: int):
    return round(float(x), nd) if isinstance(x, (int, float)) and math.isfinite(x) else None


def _beta(s, spy) -> float | None:
    """1-year daily OLS beta vs SPY (the local history stores the quote's
    beta; CI has no quote, so it is estimated from the same closes)."""
    import pandas as pd

    if s is None or spy is None:
        return None
    df = pd.concat([s.pct_change(), spy.pct_change()], axis=1).dropna().tail(_BETA_DAYS)
    if len(df) < _BETA_MIN_OBS:
        return None
    var = float(df.iloc[:, 1].var())
    return float(df.iloc[:, 0].cov(df.iloc[:, 1]) / var) if var > 0 else None


def _last_close(s, day: str) -> float | None:
    import pandas as pd

    if s is None:
        return None
    sub = s[s.index <= pd.Timestamp(day)]
    return float(sub.iloc[-1]) if len(sub) else None


def _configure_finnhub(ns) -> str | None:
    """Point news_sentiment at the env key (and, for the stubbed verification
    run only, a loopback base URL). Returns an error message or None."""
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        return "set FINNHUB_API_KEY (the builder reads its key from the environment only)"
    ns.FINNHUB_API_KEY = key
    base = os.environ.get("CONVEXITY_FINNHUB_BASE", "").strip()
    if base:
        from convexity import model_fetch

        try:
            model_fetch.check_url(base)
        except model_fetch.FetchError as e:
            return str(e)
        if not base.startswith("http://"):
            return "CONVEXITY_FINNHUB_BASE is for a local stub only (http://127.0.0.1:…)"
        ns._FINNHUB_BASE = base.rstrip("/") + "/"
    return None


def _ensure_model():
    from convexity import ml_sentiment as ml
    from convexity import model_fetch

    if model_fetch.needed():
        model_fetch.start(force=True)
        model_fetch.wait()
        ml.reload()
    return ml if ml.available() else None


def _load_previous(prev: Path | None, model_version: str) -> list[dict]:
    if prev is None or not (prev / rp.MANIFEST).exists():
        print("[reference] no previous pack — starting a fresh history")
        return []
    try:
        manifest, history, _ = rp.load_dir(prev, model_version)
    except rp.PackError as e:
        if "scored by" in str(e):
            print(f"[reference] previous pack not reusable ({e}) — starting a fresh history")
            return []
        raise
    print(f"[reference] previous pack {manifest['date']}: {len(history)} records")
    return history


def build(
    out: Path,
    *,
    limit: int | None = None,
    previous: Path | None = None,
    today: str | None = None,
) -> dict:
    """Build the pack into `out`. Returns the manifest; raises SystemExit
    with a message on a run that must not be published."""
    from convexity import analytics
    from convexity import news_diagnostics as nd
    from convexity import news_sentiment as ns

    err = _configure_finnhub(ns)
    if err:
        raise SystemExit(err)
    ml = _ensure_model()
    if ml is None:
        from convexity import ml_sentiment

        raise SystemExit(f"Market read unavailable: {ml_sentiment.runtime_status().get('reason')}")
    model_version = ml.ARTIFACT_VERSION

    uni = universe()
    names = uni["symbols"][: limit or None]
    symbols = [n["symbol"] for n in names]
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    t0 = time.time()

    prev = [r for r in _load_previous(previous, model_version) if r.get("date") != today]
    first_anchor = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=rp.ANCHOR_DAYS)).strftime(
        "%Y-%m-%d"
    )
    # The percentile reference: earlier runs only (the rerun-safe filter
    # above dropped any rows of today's date), so no tier sees this run.
    reference = [
        r["market_score"]
        for r in prev
        if r.get("date", "") >= first_anchor and isinstance(r.get("market_score"), (int, float))
    ]

    closes = analytics._bulk_close(
        sorted(set(symbols) | {r["symbol"] for r in prev} | {"SPY"}), "1Y"
    )
    if closes is None or closes.empty or "SPY" not in closes.columns:
        raise SystemExit("price history unavailable (yfinance returned nothing for SPY)")
    spy = closes["SPY"].dropna()
    series = {c: closes[c].dropna() for c in closes.columns}
    print(f"[reference] closes: {len(series) - 1}/{len(symbols)} names, {len(spy)} SPY days")

    def fetch(n):
        try:
            return ns.collect_company_news(
                n["symbol"], 7, finnhub_symbol=finnhub_symbol(n["symbol"])
            )
        except Exception as e:  # one bad ticker never sinks the run
            print(f"[reference] {n['symbol']}: news fetch failed ({type(e).__name__}: {e})")
            return [], False, 0

    counts = {"finnhub_ok": 0, "yf_nonempty": 0, "with_news": 0, "scored": 0, "failed": 0}
    new: list[dict] = []
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for i, (n, (arts, fh_ok, n_yf)) in enumerate(zip(names, pool.map(fetch, names)), 1):
            sym = n["symbol"]
            counts["finnhub_ok"] += bool(fh_ok)
            counts["yf_nonempty"] += bool(n_yf)
            if arts:
                counts["with_news"] += 1
                market, _ = ml.market_read(
                    sym, arts, closes, history=reference, company_name=n.get("name")
                )
            else:
                market = None
            if market is None:
                counts["failed"] += 1
            else:
                counts["scored"] += 1
                new.append(
                    {
                        "date": today,
                        "symbol": sym,
                        "market_score": _r(market.get("score"), 6),
                        "market_sar": _r(market.get("sar"), 4),
                        "market_z": _r(market.get("z"), 3),
                        "market_pct": _r(market.get("pct"), 2),
                        "market_tier": market.get("tier"),
                        "market_model": model_version,
                        "n_articles": market.get("n_articles"),
                        "price": _r(_last_close(series.get(sym), today), 4),
                        "beta": _r(_beta(series.get(sym), spy), 3),
                        "fwd_1d": None,
                        "fwd_5d": None,
                    }
                )
            if i % 50 == 0 or i == len(names):
                print(f"[reference] {i}/{len(names)} names, {counts['scored']} scored")

    n = len(names)
    if counts["scored"] < _MIN_SCORED_SHARE * n or counts["finnhub_ok"] < _MIN_SCORED_SHARE * n:
        raise SystemExit(
            f"degraded run, not written: {counts['scored']}/{n} scored, "
            f"Finnhub answered for {counts['finnhub_ok']}/{n}"
        )

    first_hist = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=rp.HISTORY_DAYS)).strftime(
        "%Y-%m-%d"
    )
    merged = {(r["date"], r["symbol"]): r for r in prev if r.get("date", "") >= first_hist}
    merged.update({(r["date"], r["symbol"]): r for r in new})
    records = [merged[k] for k in sorted(merged)]
    filled = 0
    for r in records:
        for h in (1, 5):
            k = f"fwd_{h}d"
            if r.get(k) is None:
                v = nd.forward_idio(series.get(r["symbol"]), spy, r["date"], r.get("beta"), h)
                if v is not None:
                    r[k] = _r(v, 6)
                    filled += 1
    anchor_rows = [
        [r["date"], r["symbol"], r["market_score"]]
        for r in records
        if r["date"] >= first_anchor and r.get("market_score") is not None
    ]

    history_obj = {"schema": rp.SCHEMA_VERSION, "records": records}
    anchor_obj = {
        "schema": rp.SCHEMA_VERSION,
        "model_version": model_version,
        "as_of": today,
        "window_days": rp.ANCHOR_DAYS,
        "rows": anchor_rows,
    }
    # Validate what we are about to publish with the SAME code the app runs.
    rp.validate_history(history_obj, model_version)
    rp.validate_anchor(anchor_obj, model_version)
    blobs = {rp.HISTORY: rp.gzip_json(history_obj), rp.ANCHOR: rp.gzip_json(anchor_obj)}
    manifest = {
        "schema_version": rp.SCHEMA_VERSION,
        "model_version": model_version,
        "date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "universe": {"source": "sp500.json", "as_of": uni.get("as_of"), "n": n},
        "rows": {"history": len(records), "anchor": len(anchor_rows)},
        "sources": counts,
        "anchor_basis": len(reference),
        "files": {k: {"sha256": rp.sha256(v), "bytes": len(v)} for k, v in blobs.items()},
    }
    rp.validate_manifest(manifest, model_version)
    rp.verify_files(manifest, blobs, model_version)

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for name, data in blobs.items():
        (out / name).write_bytes(data)
    (out / rp.MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(
        f"[reference] wrote {out}: {len(records)} history records ({len(new)} new, "
        f"{filled} forward returns filled), {len(anchor_rows)} anchor rows, "
        f"{time.time() - t0:.0f}s — sources {counts}"
    )
    return manifest


def returns_only(limit: int | None = None) -> int:
    """The Yahoo check: can this machine fetch the universe's closes (and
    yfinance news)? Cloud IPs are sometimes blocked by Yahoo; this runs
    first, from a workflow_dispatch, before anything depends on it."""
    from convexity import analytics
    from convexity import news_sentiment as ns

    syms = [n["symbol"] for n in universe()["symbols"][: limit or None]]
    t0 = time.time()
    closes = analytics._bulk_close(syms + ["SPY"], "1Y")
    have = [s for s in syms if s in closes.columns and closes[s].dropna().size >= 200]
    spy_ok = "SPY" in closes.columns and closes["SPY"].dropna().size >= 200
    cov = len(have) / max(1, len(syms))
    print(
        f"[reference] closes: {len(have)}/{len(syms)} names with >= 200 days "
        f"({cov:.1%}), SPY {'ok' if spy_ok else 'MISSING'}, {time.time() - t0:.0f}s"
    )
    missing = sorted(set(syms) - set(have))
    if missing:
        print(f"[reference] no usable closes: {' '.join(missing[:40])}")
    probe = syms[:20]
    got = sum(1 for s in probe if ns._fetch_yf_news(s, 7))
    print(f"[reference] yfinance news: {got}/{len(probe)} probed names returned articles")
    return 0 if spy_ok and cov >= 0.9 else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="convexity build-reference-pack",
        description="Build the reference pack (Market read of the S&P 500) into a directory.",
    )
    ap.add_argument("--out", help="Output directory for manifest.json + the two .json.gz files.")
    ap.add_argument("--limit", type=int, default=None, help="Only the first N names (testing).")
    ap.add_argument(
        "--previous",
        default=None,
        help="Directory holding the previous pack; its history is carried forward.",
    )
    ap.add_argument(
        "--returns-only",
        action="store_true",
        help="Only check that the universe's prices can be fetched; write nothing.",
    )
    args = ap.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        sys.stderr.write("error: --limit must be >= 1\n")
        return 2
    if args.returns_only:
        return returns_only(args.limit)
    if not args.out:
        sys.stderr.write("error: --out is required\n")
        return 2
    if not os.environ.get("FINNHUB_API_KEY", "").strip():
        sys.stderr.write(
            "error: set FINNHUB_API_KEY (the builder reads its key from the environment only)\n"
        )
        return 2
    try:
        build(
            Path(args.out),
            limit=args.limit,
            previous=Path(args.previous) if args.previous else None,
        )
    except rp.PackError as e:
        sys.stderr.write(f"error: previous pack rejected: {e}\n")
        return 1
    except SystemExit as e:
        if isinstance(e.code, str):
            sys.stderr.write(f"error: {e.code}\n")
            return 1
        raise
    return 0
