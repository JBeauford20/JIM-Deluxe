"""Stop minimum, routing feasibility and release scorecard helpers (workers/stop_rules.py). No DB.
Run: python -m pytest tests/test_stop_rules.py -q"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "workers"))
import stop_rules as SR  # noqa: E402


def test_top_k_progress_matches_single_cart_objective_for_k1():
    assert SR.top_k_progress([0.4, 0.9, 0.2], 1) == 0.9
    assert abs(SR.top_k_progress([0.4, 0.9, 0.2], 2) - 1.3) < 1e-9
    assert SR.top_k_progress([1.4], 2) == 1.0           # a fill is capped at a full cart
    assert SR.top_k_progress([0.5], 0) == 0.0


def test_top_k_full_needs_k_carts_at_the_floor():
    assert SR.top_k_full([0.95, 0.85], 2, 0.8)
    assert not SR.top_k_full([0.95, 0.7], 2, 0.8)
    assert not SR.top_k_full([0.95], 2, 0.8)             # only one cart built
    assert SR.top_k_full([0.95, 0.3], 1, 0.8)
    assert SR.top_k_full([], 0, 0.8)
    assert SR.full_carts([0.8, 0.79, 1.0], 0.8) == 2


def test_routine_target():
    assert SR.routine_target(0, 0) == 1                  # second chance, rule off: one routine cart
    assert SR.routine_target(2, 0) == 2                  # stop minimum
    assert SR.routine_target(2, 1) == 2                  # one directed cart + one routine
    assert SR.routine_target(2, 2) == 3                  # second chance always adds a routine cart
    assert SR.routine_target(2, 1, need_at_least_one=False) == 2


def test_routing_feasibility_overall_and_by_region():
    stops = {1: (1, False), 2: (1, True), 3: (2, False), 4: (3, False), 5: (0, False)}
    region = {1: "West", 2: "West", 3: "East", 4: "East"}
    r = SR.routing_feasibility(stops, region, capacity=4, max_stops=1.5)
    assert r["carts"] == 7 and r["stops"] == 4 and r["trucks"] == 3      # East 2 + West 1
    assert r["trucks_if_pooled"] == 2
    assert r["stops_per_truck"] == 1.33 and not r["pass"]                 # West is over
    assert r["single_cart_stops"] == 2 and r["single_cart_stops_directed_only"] == 1
    assert r["carts_per_stop"] == 1.75
    assert r["by_region"]["East"] == {"carts": 5, "stops": 2, "trucks": 2, "stops_per_truck": 1.0,
                                      "carts_per_stop": 2.5, "single_cart_stops": 0,
                                      "single_cart_stops_directed_only": 0, "pass": True}
    assert r["by_region"]["West"]["trucks"] == 1 and not r["by_region"]["West"]["pass"]
    ok = SR.routing_feasibility({1: (2, False), 2: (2, False)}, {1: "A", 2: "B"}, capacity=45, max_stops=20)
    assert ok["trucks"] == 2 and ok["trucks_if_pooled"] == 1 and ok["pass"]


def test_routing_feasibility_empty():
    r = SR.routing_feasibility({}, {})
    assert r["trucks"] == 0 and r["stops"] == 0 and r["pass"]


def test_cart_physical_counts():
    full = [(1, 11, 6, 36, None, "euro12")]                           # 6 std trays: full shelf
    assert SR.cart_physical(full) == {"shelves_too_wide": 0, "over_height": 0, "over_shelf_cap": 0,
                                      "shelves_not_full": 0, "height_in": 21}
    five = [(L, 11, 6, 36, None, "euro12") for L in range(1, 6)]     # 5 x 21 = 105 in
    assert SR.cart_physical(five)["over_height"] == 1
    wide = [(1, 11, 7, 42, None, "euro12")]                           # 7 std trays > 51 in
    assert SR.cart_physical(wide)["shelves_too_wide"] == 1
    part = [(1, 11, 3, 18, None, "euro12")]
    assert SR.cart_physical(part)["shelves_not_full"] == 1
    assert SR.cart_physical(part, skip_fullness=True)["shelves_not_full"] == 0
    tall17 = [(L, 12, 7, 21, None, "box17") for L in range(1, 6)]     # 17cm: max 4 shelves
    assert SR.cart_physical(tall17)["over_shelf_cap"] == 1


def test_scorecard():
    c1 = SR.check("sellthrough", 0.74, 0.68, ">=", "demand")
    c2 = SR.check("stops_per_truck", 21.5, 20, "<=", "routing")
    c3 = SR.check("lost", None, 0, "==", "demand")
    assert c1["pass"] and not c2["pass"] and not c3["pass"]
    assert c1["target"] == 0.68 and c1["op"] == ">="
    sc = SR.scorecard([c1, c2, c3])
    assert not sc["pass"] and sc["failing"] == ["stops_per_truck", "lost"]
    assert SR.scorecard([c1])["pass"]


def test_informational_check_never_fails():
    c = SR.check("single_cart_stops_not_directed", 12, 0, "==", "routing", informational=True)
    assert c["pass"] and c["informational"] and not c["within_target"]
    assert SR.scorecard([c])["pass"]
