"""The tab a reload reopens (persistence.set_last_view).

Since 1.18.1 a new tab exists as soon as its constituents are saved (a
watchlist) and gets its view only at the first Refresh. set_last_view used to
accept view names only, so a new tab was never remembered and a reload opened
an empty "Untitled N" instead. Found by the 1.19.0 verification pass.
"""

from convexity import persistence


def _last():
    return persistence.list_views()["last_view"]


def test_last_view_accepts_a_tab_that_only_has_constituents():
    persistence.upsert_watchlist("Untitled 1", "AAPL, MSFT")
    persistence.set_last_view("Untitled 1")
    assert _last() == "Untitled 1"


def test_last_view_still_ignores_unknown_names():
    persistence.upsert_watchlist("Kept", "AAPL")
    persistence.set_last_view("Kept")
    persistence.set_last_view("Nope")
    assert _last() == "Kept"
    persistence.set_last_view("")
    assert _last() is None
