"""Column-view persistence: custom views, built-in overrides, per-view color.

Covers the v1.4.5 "editable default views" change — built-in presets
(Default/Fundamentals/Momentum) are now editable in place via a per-view
override store, and every view (built-in override or custom) carries its own
per-column color-coding ("heat") map. These are pure-JSON CRUD functions, so
they're cheap to pin down here without a running server.
"""

import json

import pytest

from portfolio_tracker import persistence


@pytest.fixture
def cv(tmp_path, monkeypatch):
    """Point the column-views store at an isolated temp file per test."""
    monkeypatch.setattr(persistence, "_COLUMN_VIEWS_FILE", tmp_path / "cv.json")
    return persistence


def test_empty_store_defaults(cv):
    raw = cv.load_column_views()
    assert raw == {"custom_views": {}, "builtin_overrides": {}, "active_view": "Default"}


def test_custom_view_roundtrip_with_heat(cv):
    raw = cv.upsert_column_view("MyVal", ["symbol", "price", "ev_ebitda"],
                                heat={"ev_ebitda": "percentile"})
    entry = raw["custom_views"]["MyVal"]
    assert entry["columns"] == ["symbol", "price", "ev_ebitda"]
    assert entry["heat"] == {"ev_ebitda": "percentile"}
    assert entry["created_at"]  # stamped
    # Not routed into the built-in override store.
    assert raw["builtin_overrides"] == {}


def test_builtin_name_routes_to_override_not_custom(cv):
    """A built-in name is editable in place — it must land in builtin_overrides,
    never as a custom view masquerading under the reserved name."""
    raw = cv.upsert_column_view("Fundamentals", ["symbol", "ev_ebitda"],
                                heat={"ev_ebitda": "minmax"})
    assert "Fundamentals" not in raw["custom_views"]
    ov = raw["builtin_overrides"]["Fundamentals"]
    assert ov["columns"] == ["symbol", "ev_ebitda"]
    assert ov["heat"] == {"ev_ebitda": "minmax"}


def test_builtin_alias_normalizes(cv):
    """Legacy alias names collapse onto their canonical built-in."""
    raw = cv.upsert_column_view("IB View", ["symbol", "price"])
    assert "Fundamentals" in raw["builtin_overrides"]
    assert "IB View" not in raw["builtin_overrides"]


def test_set_builtin_view_heat_preserves_columns(cv):
    """A heat-only toggle must not freeze/overwrite an existing column order."""
    cv.upsert_column_view("Momentum", ["symbol", "pct_1w", "beta"])
    raw = cv.set_builtin_view_heat("Momentum", "beta", "off")
    ov = raw["builtin_overrides"]["Momentum"]
    assert ov["columns"] == ["symbol", "pct_1w", "beta"]  # untouched
    assert ov["heat"] == {"beta": "off"}


def test_set_builtin_view_heat_on_pristine_view(cv):
    """Toggling color on a never-edited built-in creates a heat-only override
    (no columns key — so the factory column list still applies)."""
    raw = cv.set_builtin_view_heat("Fundamentals", "ev_ebitda", "minmax")
    ov = raw["builtin_overrides"]["Fundamentals"]
    assert ov == {"heat": {"ev_ebitda": "minmax"}}
    assert "columns" not in ov


def test_delete_builtin_resets_to_factory(cv):
    """Deleting a built-in name drops its override (reset), never erroring."""
    cv.upsert_column_view("Fundamentals", ["symbol", "ev_ebitda"],
                          heat={"ev_ebitda": "minmax"})
    raw = cv.delete_column_view("Fundamentals")
    assert raw["builtin_overrides"] == {}
    # Active view untouched by a built-in reset.
    assert raw["active_view"] == "Default"


def test_delete_custom_view_and_active_fallback(cv):
    cv.upsert_column_view("MyVal", ["symbol", "price"])
    cv.set_active_column_view("MyVal")
    raw = cv.delete_column_view("MyVal")
    assert "MyVal" not in raw["custom_views"]
    assert raw["active_view"] == "Default"  # falls back when active view deleted


def test_invalid_heat_modes_are_dropped(cv):
    raw = cv.upsert_column_view("MyVal", ["symbol", "ev_ebitda"],
                                heat={"ev_ebitda": "bogus", "pe_ratio": "percentile"})
    assert raw["custom_views"]["MyVal"]["heat"] == {"pe_ratio": "percentile"}


def test_set_builtin_view_heat_rejects_non_builtin(cv):
    with pytest.raises(ValueError):
        cv.set_builtin_view_heat("MyVal", "ev_ebitda", "minmax")


def test_set_builtin_view_heat_rejects_bad_mode(cv):
    with pytest.raises(ValueError):
        cv.set_builtin_view_heat("Fundamentals", "ev_ebitda", "rainbow")


def test_upsert_requires_nonempty_columns(cv):
    with pytest.raises(ValueError):
        cv.upsert_column_view("MyVal", [])


def test_overrides_persist_to_disk(cv, tmp_path):
    """State must round-trip through the on-disk JSON — verify the raw file, so
    a fresh process (which reads from disk on every call) sees the overrides."""
    cv.upsert_column_view("Fundamentals", ["symbol", "ev_ebitda"],
                          heat={"ev_ebitda": "percentile"})
    cv.upsert_column_view("MyVal", ["symbol", "price"], heat={"pe_ratio": "off"})
    on_disk = json.loads((tmp_path / "cv.json").read_text())
    assert on_disk["builtin_overrides"]["Fundamentals"]["heat"] == {"ev_ebitda": "percentile"}
    assert on_disk["custom_views"]["MyVal"]["heat"] == {"pe_ratio": "off"}
    # And a fresh read reconstructs the same structure.
    raw = cv.load_column_views()
    assert raw["builtin_overrides"]["Fundamentals"]["columns"] == ["symbol", "ev_ebitda"]
