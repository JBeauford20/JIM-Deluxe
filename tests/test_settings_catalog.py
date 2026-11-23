"""Policies screen: catalog validation (services/settings_catalog.py). Pure — no DB.
Run: python -m pytest tests/test_settings_catalog.py -q"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from services import settings_catalog as C  # noqa: E402

ENGINE = open(os.path.join(os.path.dirname(__file__), "..", "backend", "workers", "ipr_engine.py"),
              encoding="utf-8").read()


def test_every_catalog_key_is_read_by_the_engine():
    read = set(re.findall(r'S(?:\.get\(|\[)"([a-z0-9_]+)"', ENGINE))
    # reporting-only settings are read by services/sales_outlook.py, not the engine
    outlook = open(os.path.join(os.path.dirname(__file__), "..", "backend", "services", "sales_outlook.py"),
                   encoding="utf-8").read()
    read |= set(re.findall(r'_setting\(cur, "([a-z0-9_]+)"', outlook))
    assert {e["key"] for e in C.CATALOG} <= read


def test_every_default_passes_its_own_validation():
    for e in C.CATALOG:
        assert C.validate(e["key"], e["default"]) == e["default"], e["key"]


def test_numbers_are_bounded_and_typed():
    assert C.validate("min_cart_fill_pct", 0.75) == 0.75
    assert C.validate("min_order_full_carts", 0) == 0
    for key, bad in (("min_cart_fill_pct", 0.1), ("min_cart_fill_pct", 1.5), ("min_order_full_carts", 1.5),
                     ("min_order_full_carts", 9), ("conversion_hard_block", "abc"),
                     ("conversion_hard_block", None), ("cart_delivered_cost", True),
                     ("cart_delivered_cost", float("nan"))):
        with pytest.raises(ValueError):
            C.validate(key, bad)


def test_values_the_engine_would_silently_replace_are_refused():
    # the engine reads these with `or <default>`: a 0 would be ignored, so 0 is not offered
    for key in ("peak_floor_share", "cart_wholesale_low", "min_cart_fill_pct",
                "scenario_max_multiplier", "max_cover_weeks_after_delivery"):
        with pytest.raises(ValueError):
            C.validate(key, 0)


def test_bool_choice_and_list():
    assert C.validate("cart_consolidate", False) is False
    with pytest.raises(ValueError):
        C.validate("cart_consolidate", "false")
    assert C.validate("post_delivery_feedback", "apply") == "apply"
    with pytest.raises(ValueError):
        C.validate("post_delivery_feedback", "always")
    assert C.validate("excluded_families", [" Seasonal/Holiday ", "", "Seasonal/Holiday"]) == ["Seasonal/Holiday"]
    with pytest.raises(ValueError):
        C.validate("excluded_families", "Seasonal/Holiday")


def test_grade_maps():
    full = {"AA": .67, "A": .67, "B": .7, "C+": .7, "C": .75, "C-": .8, "D": .8}
    assert C.validate("conversion_thresholds", full) == full
    with pytest.raises(ValueError):                      # every grade required
        C.validate("conversion_thresholds", {"AA": .67})
    with pytest.raises(ValueError):
        C.validate("conversion_thresholds", {**full, "Z": .5})
    # cadence: blanks mean "use the seasonal schedule" and are dropped
    assert C.validate("delivery_cadence_weeks", {"AA": 2, "A": None, "B": ""}) == {"AA": 2}
    assert C.validate("delivery_cadence_weeks", {}) == {}
    for bad in ({"AA": 0}, {"AA": 2.5}, {"AA": 40}):
        with pytest.raises(ValueError):
            C.validate("delivery_cadence_weeks", bad)


def test_forecast_weights_must_total_100():
    assert C.validate("forecast_weights", {"w4": .6, "w10": .2, "w13": .2}) == {"w4": .6, "w10": .2, "w13": .2}
    with pytest.raises(ValueError):
        C.validate("forecast_weights", {"w4": .6, "w10": .3, "w13": .2})
    with pytest.raises(ValueError):
        C.validate("forecast_weights", {"w4": .5, "w10": .5})


def test_freshness_map_needs_default_and_bounds():
    assert C.validate("freshness_days_by_family", {"_default": 90, "Terrarium": 275})["Terrarium"] == 275
    with pytest.raises(ValueError):
        C.validate("freshness_days_by_family", {"Terrarium": 275})
    with pytest.raises(ValueError):
        C.validate("freshness_days_by_family", {"_default": 3})


def test_unlisted_keys_cannot_be_edited():
    for key in ("store_priority_weights", "store_sku_rules", "sellable_share_by_age_days", "nope"):
        with pytest.raises(ValueError):
            C.validate(key, 1)


def test_describe_merges_rows_defaults_and_advanced():
    d = C.describe({"min_cart_fill_pct": {"key": "min_cart_fill_pct", "value": 0.75, "status": "x"},
                    "store_priority_weights": {"key": "store_priority_weights", "value": {"a": 1}}})
    flat = {s["key"]: s for g in d["groups"] for s in g["settings"]}
    assert flat["min_cart_fill_pct"]["value"] == 0.75 and not flat["min_cart_fill_pct"]["is_default"]
    assert flat["min_order_full_carts"]["value"] == 1 and flat["min_order_full_carts"]["is_default"]
    assert [r["key"] for r in d["advanced"]] == ["store_priority_weights"]
