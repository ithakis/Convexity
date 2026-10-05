"""`convexity build-reference-pack` (reference_build.py).

No network: news, prices and the key are stubbed; the Market read is the
real code path on the tiny test artifact from test_ml_sentiment."""

import gzip
import json
import time
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from convexity import cli
from convexity import ml_sentiment as ml
from convexity import news_sentiment as ns
from convexity import reference_build as rb
from convexity import reference_pack as rp
from tests.test_ml_sentiment import build_tiny_artifact

MARK = "ZQXJ-PRIVATE-HEADLINE"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    d = build_tiny_artifact(tmp_path / "mlsent-v1.1")[0]
    monkeypatch.setenv("MLSENT_MODEL_DIR", str(d))
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key-not-real")
    monkeypatch.delenv("CONVEXITY_FINNHUB_BASE", raising=False)
    ml.reset_for_tests()
    syms = [n["symbol"] for n in rb.universe()["symbols"][:6]] + ["SPY"]
    # End the prices on build()'s own "today", the UTC date. The local date
    # differs for part of every day (00:00-02:00 in UTC+2), and a close dated
    # after the record would give it a forward return it cannot have yet.
    today = pd.Timestamp(datetime.now(UTC).strftime("%Y-%m-%d"))
    idx = pd.bdate_range(end=today, periods=300)
    rng = np.random.default_rng(1)
    closes = pd.DataFrame(
        {s: 100 * np.cumprod(1 + rng.normal(0, 0.01, len(idx))) for s in syms}, index=idx
    )
    from convexity import analytics

    monkeypatch.setattr(
        analytics,
        "_bulk_close",
        lambda symbols, period: closes[[s for s in symbols if s in closes.columns]],
    )
    calls = []

    def news(symbol, days=7, cancel=None, finnhub_symbol=None):
        calls.append((symbol, finnhub_symbol))
        now = time.time()
        arts = [
            {
                "headline": f"{MARK} {symbol} {'beats' if i % 2 else 'misses'} earnings {i}",
                "summary": f"{MARK} summary text",
                "source": "Reuters",
                "datetime": int(now - i * 3600 * 20),
                "url": f"https://example.invalid/{MARK}/{i}",
                "related": symbol,
                "n_duplicates": 0,
            }
            for i in range(6)
        ]
        return arts, True, 3

    monkeypatch.setattr(ns, "collect_company_news", news)
    yield {"closes": closes, "calls": calls, "idx": idx}
    ml.reset_for_tests()


def _all_text(out):
    blob = (out / rp.MANIFEST).read_text()
    for name in rp.DATA_FILES:
        blob += gzip.decompress((out / name).read_bytes()).decode()
    return blob


def test_builds_a_valid_pack_without_any_article_text(env, tmp_path):
    out = tmp_path / "pack"
    m = rb.build(out, limit=6)
    manifest, history, anchor = rp.load_dir(out, ml.ARTIFACT_VERSION)
    assert manifest == m
    assert m["rows"] == {"history": 6, "anchor": 6}
    assert m["sources"]["scored"] == 6 and m["universe"]["n"] == 6
    assert {r["symbol"] for r in history} == {s for s, _ in env["calls"]}
    assert all(r["market_tier"] in rp.TIERS for r in history)
    # Today's forward returns cannot be known yet.
    assert all(r["fwd_1d"] is None and r["fwd_5d"] is None for r in history)
    text = _all_text(out)
    assert MARK not in text and "example.invalid" not in text
    assert "summary" not in text and "headline" not in text


def test_finnhub_gets_the_dot_spelling_of_class_shares(env, monkeypatch, tmp_path):
    monkeypatch.setattr(
        rb, "universe", lambda: {"as_of": "x", "symbols": [{"symbol": "BRK-B", "name": "B"}]}
    )
    env["closes"]["BRK-B"] = env["closes"]["SPY"]
    rb.build(tmp_path / "p", limit=1)
    assert env["calls"] == [("BRK-B", "BRK.B")]


def test_previous_history_carries_forward_and_forward_returns_fill(env, monkeypatch, tmp_path):
    past = env["idx"][-12].strftime("%Y-%m-%d")
    rb.build(tmp_path / "prev", limit=6, today=past)
    prev_scores = [
        r["market_score"] for r in rp.load_dir(tmp_path / "prev", ml.ARTIFACT_VERSION)[1]
    ]
    seen = []
    real = ml.market_read

    def spy_read(*a, **kw):
        seen.append(list(kw["history"]))
        return real(*a, **kw)

    monkeypatch.setattr(ml, "market_read", spy_read)
    m = rb.build(tmp_path / "now", limit=6, previous=tmp_path / "prev")
    # The percentile reference is the previous pack only — never this run.
    assert all(sorted(h) == sorted(prev_scores) for h in seen)
    assert m["anchor_basis"] == 6
    _, history, anchor = rp.load_dir(tmp_path / "now", ml.ARTIFACT_VERSION)
    old = [r for r in history if r["date"] == past]
    assert len(old) == 6 and len(history) == 12 and len(anchor) == 12
    assert all(isinstance(r["fwd_1d"], float) and isinstance(r["fwd_5d"], float) for r in old)


def test_a_rerun_on_the_same_day_does_not_rank_against_itself(env, monkeypatch, tmp_path):
    rb.build(tmp_path / "a", limit=6)
    seen = []
    real = ml.market_read
    monkeypatch.setattr(
        ml, "market_read", lambda *a, **kw: (seen.append(kw["history"]), real(*a, **kw))[1]
    )
    m = rb.build(tmp_path / "b", limit=6, previous=tmp_path / "a")
    assert all(h == [] for h in seen) and m["rows"]["history"] == 6


def test_degraded_run_writes_nothing(env, monkeypatch, tmp_path):
    monkeypatch.setattr(ns, "collect_company_news", lambda *a, **k: ([], False, 0))
    out = tmp_path / "pack"
    assert cli.main(["build-reference-pack", "--out", str(out), "--limit", "4"]) == 1
    assert not out.exists()


def test_missing_key_is_a_usage_error(env, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("FINNHUB_API_KEY")
    out = tmp_path / "pack"
    assert cli.main(["build-reference-pack", "--out", str(out)]) == 2
    assert "FINNHUB_API_KEY" in capsys.readouterr().err
    assert not out.exists()


def test_tampered_previous_pack_is_refused(env, tmp_path):
    # The history itself damaged: nothing to salvage, so the run fails
    # (loudly, nothing written) rather than publish a truncated history.
    rb.build(tmp_path / "prev", limit=6, today="2026-01-02")
    p = tmp_path / "prev" / rp.HISTORY
    b = bytearray(p.read_bytes())
    b[len(b) // 2] ^= 0xFF
    p.write_bytes(bytes(b))
    out = tmp_path / "now"
    args = ["build-reference-pack", "--out", str(out), "--limit", "6"]
    assert cli.main([*args, "--previous", str(tmp_path / "prev")]) == 1
    assert not out.exists()


def test_a_half_published_previous_pack_still_carries_its_history(env, tmp_path, capsys):
    """A publish that broke off after the data files: new data, old manifest.
    The history is carried forward on its own rather than failing every later
    run (the release would never change again) or restarting from scratch."""
    rb.build(tmp_path / "old", limit=6, today="2026-01-02")
    rb.build(tmp_path / "prev", limit=6, today="2026-01-05", previous=tmp_path / "old")
    (tmp_path / "prev" / rp.MANIFEST).write_bytes((tmp_path / "old" / rp.MANIFEST).read_bytes())
    with pytest.raises(rp.PackError):
        rp.load_dir(tmp_path / "prev", ml.ARTIFACT_VERSION)
    m = rb.build(tmp_path / "now", limit=6, today="2026-01-06", previous=tmp_path / "prev")
    assert "carried its history.json.gz forward" in capsys.readouterr().out
    dates = {r["date"] for r in rp.load_dir(tmp_path / "now", ml.ARTIFACT_VERSION)[1]}
    assert dates == {"2026-01-02", "2026-01-05", "2026-01-06"}
    assert m["rows"]["history"] == 18


def test_stub_base_url_must_be_loopback_http(env, monkeypatch, tmp_path):
    monkeypatch.setenv("CONVEXITY_FINNHUB_BASE", "http://example.com/api/v1/")
    with pytest.raises(SystemExit):
        rb.build(tmp_path / "p", limit=1)
    monkeypatch.setenv("CONVEXITY_FINNHUB_BASE", "http://127.0.0.1:9/api/v1")
    monkeypatch.setattr(ns, "_FINNHUB_BASE", ns._FINNHUB_BASE)
    rb.build(tmp_path / "p", limit=1)
    assert ns._FINNHUB_BASE == "http://127.0.0.1:9/api/v1/"


def test_returns_only_writes_nothing(env, monkeypatch, tmp_path, capsys):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(ns, "_fetch_yf_news", lambda s, d, cancel=None: [{"x": 1}])
    assert cli.main(["build-reference-pack", "--returns-only", "--limit", "6"]) == 0
    assert "6/6 names" in capsys.readouterr().out
    assert list(cwd.iterdir()) == []


def test_returns_only_fails_without_spy(env, monkeypatch):
    from convexity import analytics

    monkeypatch.setattr(analytics, "_bulk_close", lambda s, p: env["closes"].drop(columns="SPY"))
    monkeypatch.setattr(ns, "_fetch_yf_news", lambda s, d, cancel=None: [])
    assert rb.returns_only(6) == 1


def test_help_lists_the_command(capsys):
    assert cli.main(["--help"]) == 0
    assert "build-reference-pack" in capsys.readouterr().out


def test_manifest_date_is_utc_today(env, tmp_path):
    m = rb.build(tmp_path / "p", limit=6)
    assert m["date"] == datetime.now(UTC).strftime("%Y-%m-%d")
    assert json.loads((tmp_path / "p" / rp.MANIFEST).read_text())["files"].keys() == set(
        rp.DATA_FILES
    )
    assert (
        timedelta(0)
        <= datetime.now(UTC) - datetime.fromisoformat(m["generated_at"])
        < timedelta(minutes=5)
    )
