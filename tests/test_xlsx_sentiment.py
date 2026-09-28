"""The Excel export's News read / Market read columns (xlsx_export.SENTIMENT_COLS)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from convexity import xlsx_export as xe  # noqa: E402

S = {
    "news": {
        "tier": "bullish",
        "score": 0.9,
        "stale": False,
        "lenses": {
            "financials": {"score": 2.0},
            "outlook": {"score": -1.0},
            "competition": None,
            "regulation": None,
            "street": {"score": 1.0},
        },
    },
    "market": {"tier": "no_edge", "z": -0.42},
    "divergence": {
        "kind": "good_news_weak_reaction",
        "text": "Financials +2.0; Market read bearish",
    },
}


def test_every_sentiment_column_resolves():
    got = {label: xe._sentiment_value(key, S) for key, label in xe.SENTIMENT_COLS}
    assert got["News read"] == "bullish"
    assert got["News · Financials (−2..+2)"] == 2.0
    assert got["News · Outlook (−2..+2)"] == -1.0
    assert got["News · Competition (−2..+2)"] is None  # no news, not 0
    assert got["Market read"] == "no edge"
    assert got["Market read z (σ)"] == -0.42
    assert got["Divergence"].startswith("Financials +2.0")


def test_stale_and_missing_reads():
    stale = {"news": dict(S["news"], stale=True)}
    assert xe._sentiment_value("ns:news_tier", stale) == "bullish (stale)"
    assert xe._sentiment_value("ns:market_z", stale) is None
    assert all(xe._sentiment_value(k, None) is None for k, _ in xe.SENTIMENT_COLS)


def test_the_payload_blob_is_not_exported_raw():
    assert "news_sentiment" in xe.HOLDINGS_SKIP_EXTRAS
