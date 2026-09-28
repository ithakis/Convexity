"""The reference pack's fixed universe (src/convexity/data/sp500.json).

Public index membership only, in the Yahoo ticker format the rest of the app
uses (class shares with '-', not '.'), with its provenance recorded."""

import json
import re
from importlib import resources

_YAHOO = re.compile(r"^[A-Z]{1,5}(-[A-Z])?$")


def _load():
    return json.loads(resources.files("convexity").joinpath("data/sp500.json").read_text("utf-8"))


def test_universe_is_the_sp500_in_yahoo_format():
    d = _load()
    syms = [s["symbol"] for s in d["symbols"]]
    assert 495 <= len(syms) <= 510
    assert len(set(syms)) == len(syms)
    bad = [s for s in syms if not _YAHOO.match(s)]
    assert not bad, bad
    assert "BRK-B" in syms and "BRK.B" not in syms
    assert all(s["name"] for s in d["symbols"])


def test_universe_records_its_source_and_date():
    d = _load()
    assert d["source"].startswith("https://")
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", d["as_of"])
