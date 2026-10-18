"""persistence.py: column views, app prefs and the last-opened tab.

Column views: the v1.4.5 editable-default-views change. Built-in presets are edited in
place through a per-view override store, and every view carries its own per-column
colour-coding (heat) map.

Prefs (state/prefs.json): the default display currency, My Investments benchmark and the
distribution settings; allow-listed, never a crash on a bad file.

Last view: since 1.18.1 a new tab exists as soon as its constituents are saved, so
set_last_view must accept it."""

import json

import pytest

from convexity import paths, persistence


@pytest.fixture
def cv(tmp_path, monkeypatch):
    """Point the column-views store at an isolated temp file per test."""
    monkeypatch.setattr(persistence, "_COLUMN_VIEWS_FILE", tmp_path / "cv.json")
    return persistence


def test_empty_store_defaults(cv):
    raw = cv.load_column_views()
    assert raw == {"custom_views": {}, "builtin_overrides": {}, "active_view": "Default"}


def test_custom_view_roundtrip_with_heat(cv):
    raw = cv.upsert_column_view(
        "MyVal", ["symbol", "price", "ev_ebitda"], heat={"ev_ebitda": "quantile"}
    )
    entry = raw["custom_views"]["MyVal"]
    assert entry["columns"] == ["symbol", "price", "ev_ebitda"]
    assert entry["heat"] == {"ev_ebitda": "quantile"}
    assert entry["created_at"]  # stamped
    # Not routed into the built-in override store.
    assert raw["builtin_overrides"] == {}


def test_builtin_name_routes_to_override_not_custom(cv):
    """A built-in name is editable in place — it must land in builtin_overrides,
    never as a custom view masquerading under the reserved name."""
    raw = cv.upsert_column_view(
        "Fundamentals", ["symbol", "ev_ebitda"], heat={"ev_ebitda": "minmax"}
    )
    assert "Fundamentals" not in raw["custom_views"]
    ov = raw["builtin_overrides"]["Fundamentals"]
    assert ov["columns"] == ["symbol", "ev_ebitda"]
    assert ov["heat"] == {"ev_ebitda": "minmax"}


def test_removed_percentile_mode_reads_as_quantile(cv):
    raw = cv.upsert_column_view("MyVal", ["symbol", "pe_ratio"], heat={"pe_ratio": "percentile"})
    assert raw["custom_views"]["MyVal"]["heat"] == {"pe_ratio": "quantile"}


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
    cv.upsert_column_view("Fundamentals", ["symbol", "ev_ebitda"], heat={"ev_ebitda": "minmax"})
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
    raw = cv.upsert_column_view(
        "MyVal", ["symbol", "ev_ebitda"], heat={"ev_ebitda": "bogus", "pe_ratio": "quantile"}
    )
    assert raw["custom_views"]["MyVal"]["heat"] == {"pe_ratio": "quantile"}


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
    cv.upsert_column_view("Fundamentals", ["symbol", "ev_ebitda"], heat={"ev_ebitda": "quantile"})
    cv.upsert_column_view("MyVal", ["symbol", "price"], heat={"pe_ratio": "off"})
    on_disk = json.loads((tmp_path / "cv.json").read_text())
    assert on_disk["builtin_overrides"]["Fundamentals"]["heat"] == {"ev_ebitda": "quantile"}
    assert on_disk["custom_views"]["MyVal"]["heat"] == {"pe_ratio": "off"}
    # And a fresh read reconstructs the same structure.
    raw = cv.load_column_views()
    assert raw["builtin_overrides"]["Fundamentals"]["columns"] == ["symbol", "ev_ebitda"]


# --------------------------------------------------------------------------
# v1.11.1 — the `acked` flag behind "Modified · Save · Reset"
#
# Built-ins are edited in place and saved immediately, so the pill is purely
# informational. Save acknowledges the divergence; any FURTHER edit has to
# re-arm it, or a later change would slip in silently. Reverting stays delete.
# --------------------------------------------------------------------------


def test_ack_roundtrips_and_survives_disk(cv, tmp_path):
    cv.upsert_column_view("Momentum", ["symbol", "pct_1d"])
    raw = cv.set_builtin_view_acked("Momentum")
    assert raw["builtin_overrides"]["Momentum"]["acked"] is True
    on_disk = json.loads((tmp_path / "cv.json").read_text())
    assert on_disk["builtin_overrides"]["Momentum"]["acked"] is True
    assert cv.load_column_views()["builtin_overrides"]["Momentum"]["acked"] is True


def test_ack_can_be_cleared(cv):
    cv.upsert_column_view("Momentum", ["symbol", "pct_1d"])
    cv.set_builtin_view_acked("Momentum")
    raw = cv.set_builtin_view_acked("Momentum", False)
    assert "acked" not in raw["builtin_overrides"]["Momentum"]
    # The override itself is untouched — un-acking is not a revert.
    assert raw["builtin_overrides"]["Momentum"]["columns"] == ["symbol", "pct_1d"]


def test_column_edit_after_ack_rearms_the_pill(cv):
    cv.upsert_column_view("Default", ["symbol", "price"])
    cv.set_builtin_view_acked("Default")
    raw = cv.upsert_column_view("Default", ["symbol", "price", "volume"])
    assert "acked" not in raw["builtin_overrides"]["Default"]


def test_heat_change_after_ack_rearms_the_pill(cv):
    """The regression this guards: set_builtin_view_heat mutates an EXISTING
    entry rather than rebuilding it, so it has to drop `acked` by hand."""
    cv.upsert_column_view("Default", ["symbol", "price"])
    cv.set_builtin_view_acked("Default")
    raw = cv.set_builtin_view_heat("Default", "price", "quantile")
    assert "acked" not in raw["builtin_overrides"]["Default"]


def test_ack_without_an_override_is_a_noop(cv):
    raw = cv.set_builtin_view_acked("Fundamentals")
    assert raw["builtin_overrides"] == {}


def test_lone_acked_entry_does_not_survive_a_read(cv, tmp_path):
    """An `acked` with no columns/heat means nothing — the empty-override-
    disappears invariant must still hold, however the file got that way."""
    (tmp_path / "cv.json").write_text(
        json.dumps(
            {
                "custom_views": {},
                "active_view": "Default",
                "builtin_overrides": {"Default": {"acked": True}},
            }
        )
    )
    assert cv.load_column_views()["builtin_overrides"] == {}


def test_revert_drops_the_ack_too(cv):
    cv.upsert_column_view("Momentum", ["symbol", "pct_1d"])
    cv.set_builtin_view_acked("Momentum")
    raw = cv.delete_column_view("Momentum")
    assert "Momentum" not in raw["builtin_overrides"]


def test_ack_rejects_non_builtin(cv):
    cv.upsert_column_view("MyVal", ["symbol", "price"])
    with pytest.raises(ValueError):
        cv.set_builtin_view_acked("MyVal")


def test_prefs_round_trip_and_null_removes():
    assert persistence.load_prefs() == {}
    assert persistence.save_prefs({"default_ccy": "EUR"}) == {"default_ccy": "EUR"}
    persistence.save_prefs({"inv_benchmark": "WORLD"})
    assert persistence.load_prefs() == {"default_ccy": "EUR", "inv_benchmark": "WORLD"}
    persistence.save_prefs({"default_ccy": None})
    assert persistence.load_prefs() == {"inv_benchmark": "WORLD"}


def test_prefs_drop_unknown_keys_and_values():
    saved = persistence.save_prefs(
        {"default_ccy": "XYZ", "inv_benchmark": "^GSPC", "evil": "x", "theme": 3}
    )
    assert saved == {}


def test_a_malformed_prefs_file_reads_as_empty():
    path = paths.state_file("prefs")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert persistence.load_prefs() == {}
    path.write_text(json.dumps(["EUR"]), encoding="utf-8")
    assert persistence.load_prefs() == {}
    path.write_text(json.dumps({"default_ccy": "GBP", "x": 1}), encoding="utf-8")
    assert persistence.load_prefs() == {"default_ccy": "GBP"}


def test_distribution_prefs_are_bounded():
    saved = persistence.save_prefs(
        {"dist_erp": 0.06, "dist_kappa_stock": 0.9, "dist_kappa_index": 3.0, "dist_measure": "Q",
         "dist_haircut": True}
    )  # fmt: skip
    assert saved == {
        "dist_erp": 0.06,
        "dist_kappa_stock": 0.9,
        "dist_measure": "Q",
        "dist_haircut": True,
    }
    assert (
        persistence.save_prefs({"dist_erp": "0.05", "dist_measure": "R", "dist_haircut": 1})
        == saved
    )
    assert persistence.save_prefs({"dist_erp": True})["dist_erp"] == 0.06


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
