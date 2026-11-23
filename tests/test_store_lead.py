"""Store delivery profiles (workers/store_lead.py) and the scenario parser's validation.
Pure — no DB, no model call. Run: python -m pytest tests/test_store_lead.py -q"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "workers"))
import store_lead as L  # noqa: E402

STORES = {1: {"state": "PA", "division": "North"}, 2: {"state": "PA", "division": "North"},
          3: {"state": "CA", "division": "West"}, 4: {"state": "ZZ", "division": None}}


def row(store, week, dl, ship=None):
    return {"store_id": store, "week_number": week, "ship_date": ship, "delivery_date": dl}


def test_week_monday_picks_the_year_nearest_the_delivery():
    assert L.week_monday(47, date(2026, 11, 20)) == date(2026, 11, 16)
    assert L.week_monday(1, date(2027, 1, 8)) == date(2027, 1, 4)
    assert L.week_monday(53, date(2027, 1, 2)) == date(2026, 12, 28)


def test_store_with_history_uses_its_own_median_and_p80():
    rows = [row(1, 47, date(2026, 11, 16 + d), date(2026, 11, 16 + d - 1)) for d in (3, 4, 4, 5, 9)]
    p, s = L.build_profiles(rows, STORES)
    assert p[1]["days"] == 4 and p[1]["basis"] == "store" and p[1]["n"] == 5
    assert p[1]["p80"] == 9 and p[1]["fastest"] == 3 and p[1]["slowest"] == 9
    assert p[1]["transit"] == 1 and p[1]["last_delivery"] == date(2026, 11, 25)
    assert s["stores_with_history"] == 1


def test_fallbacks_state_then_division_then_network():
    rows = [row(1, 47, date(2026, 11, 21)) for _ in range(40)]          # PA / North: day 5
    rows += [row(3, 47, date(2026, 11, 25)) for _ in range(2)]          # CA store: too few of its own
    p, _s = L.build_profiles(rows, STORES)
    assert p[2]["basis"] == "state" and p[2]["days"] == 5 and p[2]["n"] == 0
    assert p[3]["basis"] == "network" and p[3]["n"] == 2                 # CA and West both under 30 trips
    assert p[4]["basis"] == "network" and p[4]["days"] == 5
    many = rows + [row(3, 47, date(2026, 11, 25)) for _ in range(30)]
    assert L.build_profiles(many, STORES)[0][3]["basis"] == "store"


def test_bad_rows_are_dropped():
    rows = [row(1, 47, None), row(1, None, date(2026, 11, 20)), row(1, 47, date(2026, 12, 25)),
            row(1, 47, date(2026, 11, 18), date(2026, 12, 30))]
    obs = L.clean(rows)
    assert len(obs) == 1 and obs[0][1] == 2 and obs[0][2] is None       # impossible transit ignored
    p, _ = L.build_profiles([], STORES)
    assert p[1]["days"] == L.DEFAULT_DAYS and p[1]["basis"] == "network"


# ── scenario parser validation ───────────────────────────────────────────────
from routers import scenarios as SC  # noqa: E402

CAT = {"merchants": ["Paul Haitsch"], "regions": ["Northern", "Southern", "Western"], "states": ["FL", "CA"],
       "velocity_tiers": ["AA", "A"], "families": ["H2O Bowl"], "legacy_tiers": ["Collectors"],
       "skus": [{"sku_id": 1012142347, "description": "PW LEAFJOY H2O BOWL"}]}


def test_parser_is_told_every_type_the_engine_applies():
    assert set(SC.ADJUSTMENT_TYPES) == {"cart_target_override", "demand_multiplier", "exclusion",
                                        "priority_boost", "allocation_floor", "allocation_cap",
                                        "directed_shipment"}
    for t in SC.ADJUSTMENT_TYPES:
        assert t in SC._VOCAB


def test_validate_normalises_store_ids_state_and_drops_unsupported():
    adj = [{"adjustment_type": "cart_target_override", "label": "off", "value_numeric": 0, "scope_store_ids": "608"},
           {"adjustment_type": "demand_multiplier", "label": "more", "value_numeric": 1.25, "scope_state": "fl",
            "scope_family": "H2O Bowl", "scope_store_ids": ["664", 664, "x"], "scope_sku_id": "1012142347"},
           {"adjustment_type": "cart_target_override", "label": "exact", "value_numeric": 3},
           {"adjustment_type": "sku_push", "label": "push", "value_numeric": 0.5},
           "garbage"]
    clean, warns = SC._validate(adj, CAT)
    assert [a["label"] for a in clean] == ["off", "more"]
    assert clean[0]["scope_store_ids"] == [608]
    assert clean[1]["scope_store_ids"] == [664] and clean[1]["scope_state"] == "FL"
    assert clean[1]["scope_sku_id"] == 1012142347
    assert any("not a store number" in w for w in warns) and any("sku_push" in w for w in warns)
    assert any("exact cart count" in w for w in warns)
