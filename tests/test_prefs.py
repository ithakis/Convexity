"""App preferences (state/prefs.json): the default display currency and My
Investments' benchmark. Allow-listed, never a crash on a bad file."""

import json

from convexity import paths, persistence


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
