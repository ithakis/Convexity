"""reference_pack.py — the pack format and its validators (and, from the
app-side fetch on, the download itself against a local HTTP stub)."""

import gzip
import json

import pytest

from convexity import reference_pack as rp

MV = "mlsent-v1.1"


def _history(n=3, **extra):
    recs = [
        {
            "date": "2026-09-2%d" % (i % 9),
            "symbol": "AAPL",
            "market_score": 0.01 * i,
            "market_tier": "no_edge",
            "market_model": MV,
            "fwd_1d": None,
            **extra,
        }
        for i in range(n)
    ]
    return {"schema": rp.SCHEMA_VERSION, "records": recs}


def _anchor(n=3):
    return {
        "schema": rp.SCHEMA_VERSION,
        "model_version": MV,
        "as_of": "2026-09-28",
        "window_days": 90,
        "rows": [["2026-09-28", "MSFT", 0.1 * i] for i in range(n)],
    }


def make_pack(d, history=None, anchor=None, model_version=MV, date="2026-09-28"):
    """Write a valid pack into ``d``; returns the manifest. Used by the
    fetch, anchor and Track record tests too."""
    d.mkdir(parents=True, exist_ok=True)
    blobs = {
        rp.HISTORY: rp.gzip_json(history or _history()),
        rp.ANCHOR: rp.gzip_json(anchor or _anchor()),
    }
    for k, v in blobs.items():
        (d / k).write_bytes(v)
    h = history or _history()
    a = anchor or _anchor()
    m = {
        "schema_version": rp.SCHEMA_VERSION,
        "model_version": model_version,
        "date": date,
        "rows": {"history": len(h["records"]), "anchor": len(a["rows"])},
        "files": {k: {"sha256": rp.sha256(v), "bytes": len(v)} for k, v in blobs.items()},
    }
    (d / rp.MANIFEST).write_text(json.dumps(m))
    return m


def test_a_valid_pack_round_trips(tmp_path):
    make_pack(tmp_path)
    m, h, a = rp.load_dir(tmp_path, MV)
    assert m["date"] == "2026-09-28" and len(h) == 3 and len(a) == 3


def test_gzip_is_deterministic():
    assert rp.gzip_json({"a": [1, 2]}) == rp.gzip_json({"a": [1, 2]})


@pytest.mark.parametrize("field", ["headline", "summary", "url", "text"])
def test_any_text_field_makes_the_history_invalid(field):
    with pytest.raises(rp.PackError, match="unexpected field"):
        rp.validate_history(_history(**{field: "a headline"}), MV)


def test_other_model_version_is_rejected(tmp_path):
    make_pack(tmp_path, model_version="mlsent-v9")
    with pytest.raises(rp.PackError, match="scored by"):
        rp.load_dir(tmp_path, MV)


def test_wrong_schema_is_rejected(tmp_path):
    m = make_pack(tmp_path)
    m["schema_version"] = 99
    (tmp_path / rp.MANIFEST).write_text(json.dumps(m))
    with pytest.raises(rp.PackError, match="schema_version"):
        rp.load_dir(tmp_path, MV)


def test_bad_hash_is_rejected(tmp_path):
    m = make_pack(tmp_path)
    m["files"][rp.ANCHOR]["sha256"] = "0" * 64
    (tmp_path / rp.MANIFEST).write_text(json.dumps(m))
    with pytest.raises(rp.PackError, match="checksum mismatch"):
        rp.load_dir(tmp_path, MV)


def test_truncated_gzip_is_rejected():
    blob = rp.gzip_json(_anchor(200))
    with pytest.raises(rp.PackError, match="truncated|gzip"):
        rp.gunzip_json(blob[: len(blob) // 2])


def test_gzip_bomb_hits_the_cap():
    bomb = gzip.compress(b"[" + b"0," * 500_000 + b"0]")
    assert len(bomb) < 10_000
    with pytest.raises(rp.PackError, match="cap"):
        rp.gunzip_json(bomb, max_bytes=100_000)


def test_non_finite_numbers_are_rejected():
    a = _anchor()
    a["rows"][0][2] = float("nan")
    with pytest.raises(rp.PackError):
        rp.validate_anchor(a, MV)
