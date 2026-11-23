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


def test_directed_shipment_needs_stores_product_and_a_sane_count():
    base = {"adjustment_type": "directed_shipment", "label": "d", "value_numeric": 2}
    ok, _w = SC._validate([{**base, "scope_store_ids": [121], "scope_family": "H2O Bowl"}], CAT)
    assert len(ok) == 1
    # an unrecognised product name is cleared, and the directive must then be dropped
    bad, w = SC._validate([{**base, "scope_store_ids": [121], "scope_family": "12cm Collector"}], CAT)
    assert bad == [] and any("both the stores and the product" in x for x in w)
    for extra in ({"scope_family": "H2O Bowl"}, {"scope_store_ids": [121]},
                  {"scope_store_ids": [121], "scope_family": "H2O Bowl", "value_numeric": 40},
                  {"scope_store_ids": [121], "scope_family": "H2O Bowl", "value_numeric": 1.5}):
        assert SC._validate([{**base, **extra}], CAT)[0] == [], extra


def test_combined_product_names_resolve_to_family_plus_tier():
    cat = {**CAT, "families": ["12cm", "17cm", "H2O Bowl"], "legacy_tiers": ["Collectors", "Rare Collectors"],
           "product_groups": {"12cm Collector": {"scope_family": "12cm", "scope_legacy_tier": "Collectors"},
                              "17cm Rare Collector": {"scope_family": "17cm", "scope_legacy_tier": "Rare Collectors"}}}
    for said in ("12cm Collectors", "12cm collector", "12 CM Collectors"):
        ok, _w = SC._validate([{"adjustment_type": "directed_shipment", "label": "d", "value_numeric": 2,
                                "scope_store_ids": [627], "scope_family": said}], cat)
        assert len(ok) == 1 and ok[0]["scope_family"] == "12cm" and ok[0]["scope_legacy_tier"] == "Collectors", said
    ok, _w = SC._validate([{"adjustment_type": "exclusion", "label": "x", "scope_region": "Northern",
                            "scope_legacy_tier": "17cm Rare Collectors"}], cat)
    assert ok[0]["scope_family"] == "17cm" and ok[0]["scope_legacy_tier"] == "Rare Collectors"
    # a real family or tier is never rewritten
    ok, _w = SC._validate([{"adjustment_type": "exclusion", "label": "x", "scope_family": "12cm"}], cat)
    assert ok[0]["scope_family"] == "12cm" and not ok[0].get("scope_legacy_tier")


def test_pasted_store_list_is_lifted_out_exactly():
    ids = [101 + i for i in range(114)]
    text = "Send 3 shelves (21 units each, 63 units) of Monstera Bulbasaur 17cm to stores " + ", ".join(map(str, ids))
    out, found = SC._extract_store_list(text)
    assert found == ids and "[STORE LIST]" in out
    assert "17cm" in out and "63 units" in out and "3 shelves" in out          # sizes and counts are not stores
    assert not any(str(i) in out for i in ids[:5])
    # one per line, as pasted from a spreadsheet, with a repeat
    nl = chr(10)
    out, found = SC._extract_store_list("stores:" + nl + nl.join(map(str, ids + [101])) + nl + "get 3 shelves of littles")
    assert found == ids
    # a short list is left for the model to read
    assert SC._extract_store_list("take stores 121, 608 and 664 off") == ("take stores 121, 608 and 664 off", [])
    # a SKU number is not a store
    assert SC._extract_store_list("skip SKU 1012138479 for store 121")[1] == []
