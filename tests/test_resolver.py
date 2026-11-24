"""A broker's exchange codes in the Add box: "IBIS:SIE" used to fall through
to a name search and become a different company than the Xetra line typed.
Plain parsing only, no network (well-known public tickers, CLAUDE.md §18)."""

import pytest

from convexity import resolver as r


@pytest.mark.parametrize(
    "entry, symbol",
    [
        ("SBF:AIR", "AIR.PA"),
        ("AIR:SBF", "AIR.PA"),
        ("AIR SBF", "AIR.PA"),  # as the broker's positions screen shows it
        ("IBIS:SIE", "SIE.DE"),
        ("SIE IBIS", "SIE.DE"),
        ("BM:ITX", "ITX.MC"),
        ("ENI BVME", "ENI.MI"),
        ("ASML AEB", "ASML.AS"),
        ("NVDA NASDAQ.NMS", "NVDA"),  # a code with a dot
        ("NASDAQ.NMS:NVDA", "NVDA"),
        ("XPAR:AIR", "AIR.PA"),  # an ISO MIC
        ("LSE:BP", "BP.L"),
        ("EPA.AIR", "AIR.PA"),  # the existing dotted form still works
    ],
)
def test_broker_codes(entry, symbol):
    mapped = r._broker_pair(entry) or r._normalize_exchange_prefix(r._normalize_google_colon(entry))
    assert mapped is not None and mapped[0] == symbol


@pytest.mark.parametrize(
    "entry", ["Airbus SE", "bank of new york", "Unilever PLC", "ROCHE HOLDING"]
)
def test_names_are_not_read_as_codes(entry):
    assert r._broker_pair(entry) is None  # SE is Stockholm only in the dotted form
