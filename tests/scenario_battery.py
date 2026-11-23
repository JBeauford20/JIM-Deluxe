"""Scenario battery — what order writers type, and what the parser must make of it.

Runs every phrase through the REAL path (routers.scenarios.interpret: model + validation +
store checks) against a READ-ONLY database connection. Nothing is saved; no scenario is
created. It costs one model call per phrase, so it is not part of the unit-test run.

  python tests/scenario_battery.py            # all cases
  python tests/scenario_battery.py florida    # only cases whose text contains "florida"

Needs SUPABASE_DB_URL and ANTHROPIC_API_KEY in the environment (both are in jim.env).
Exit code 1 when any case fails. Add a case whenever an order writer's phrase surprises you.

A case: (text, expectation). Expectation keys:
  type    adjustment type every returned adjustment must have (or a set of allowed types)
  n       exact number of adjustments (default: at least 1)
  value   value_numeric every adjustment must carry (within 0.01)
  scope   {field: value} every adjustment must match; value may be a set (any of), a list
          for store ids (exact), or True (field just has to be set)
  any     {field: {values}} at least one adjustment per listed value (e.g. one exclusion each
          for Northern and Western)
  none    True = must return NO adjustment and say why (a refusal with a warning)
  warn    substring that must appear in the warnings
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

BOWL = "H2O Bowl"
CASES = [
    # ── more / less of a product ──
    ("Send 25% more H2O bowls to Florida stores this week", dict(type="demand_multiplier", value=1.25, scope={"scope_state": "FL", "scope_family": BOWL})),
    ("bump bowls 10% in the south", dict(type="demand_multiplier", value=1.10, scope={"scope_region": "Southern", "scope_family": BOWL})),
    ("Cut 17cm by a third in California", dict(type="demand_multiplier", value=0.67, scope={"scope_state": "CA", "scope_family": "17cm"})),
    ("double the littles for AA stores", dict(type="demand_multiplier", value=2.0, scope={"scope_velocity_tier": "AA", "scope_family": "9cm Littles"})),
    ("half as many terrariums everywhere", dict(type="demand_multiplier", value=0.5, any={"scope_family": {"Lg Terrarium", "Med Terrarium", "Sm Terrarium"}})),
    ("Texas gets 20% less climb-itt", dict(type="demand_multiplier", value=0.8, scope={"scope_state": "TX", "scope_family": "Climb-itt"})),
    ("push collectors 50% harder in the West", dict(type="demand_multiplier", value=1.5, scope={"scope_region": "Western", "scope_legacy_tier": "Collectors"})),
    # ── shelves: at least / at most ──
    ("send 2 shelves of collectors to Western stores", dict(type="directed_shipment", value=2, scope={"scope_region": "Western", "scope_legacy_tier": "Collectors"})),
    ("ship 1 shelf of the holiday gnome 9cm to stores 901 and 902", dict(type="directed_shipment", value=1, scope={"scope_store_ids": [901, 902], "scope_sku_id": 1014628066})),
    ("put 3 shelves of littles in every AA store", dict(type="directed_shipment", value=3, scope={"scope_velocity_tier": "AA", "scope_family": "9cm Littles"})),
    ("send collectors to store 121", dict(type="directed_shipment", value=1, scope={"scope_store_ids": [121], "scope_legacy_tier": "Collectors"})),
    ("send 40 shelves of bowls to store 121", dict(none=True)),
    ("send 2 shelves to store 121", dict(none=True)),
    ("Give stores 121 and 608 at least 1 shelf of H2O bowls", dict(type="allocation_floor", value=1, scope={"scope_store_ids": [121, 608], "scope_family": BOWL})),
    ("no more than 1 shelf of dish gardens per store in New York", dict(type="allocation_cap", value=1, scope={"scope_state": "NY", "scope_family": "Dish Garden"})),
    ("cap rare collectors at 2 shelves for D stores", dict(type="allocation_cap", value=2, scope={"scope_velocity_tier": "D", "scope_legacy_tier": "Rare Collectors"})),
    ("make sure every A store gets a shelf of venti", dict(type={"allocation_floor", "directed_shipment"}, value=1, scope={"scope_velocity_tier": "A", "scope_family": "H2O Venti"})),
    # ── block a product ──
    ("No collectors to the West this week", dict(type="exclusion", scope={"scope_region": "Western", "scope_legacy_tier": "Collectors"})),
    ("don't ship dish gardens to Florida", dict(type="exclusion", scope={"scope_state": "FL", "scope_family": "Dish Garden"})),
    ("Black Friday Bowls going to the South", dict(type="exclusion", n=2, scope={"scope_family": BOWL}, any={"scope_region": {"Northern", "Western"}})),
    ("only the north gets 17cm this week", dict(type="exclusion", n=2, scope={"scope_family": "17cm"}, any={"scope_region": {"Southern", "Western"}})),
    ("hold all climb-itt", dict(type="exclusion", scope={"scope_family": "Climb-itt"})),
    ("skip SKU 1012138479 for store 121", dict(type="exclusion", scope={"scope_store_ids": [121], "scope_sku_id": 1012138479})),
    # ── remove stores ──
    ("Take store 608 off this week", dict(type="cart_target_override", value=0, scope={"scope_store_ids": [608]})),
    ("remove stores 121, 608 and 664 from this run", dict(type="cart_target_override", value=0, scope={"scope_store_ids": [121, 608, 664]})),
    ("Paul Haitsch removed from this week's consideration", dict(type="cart_target_override", value=0, scope={"scope_merchant": "Paul Haitsch"})),
    ("no orders for Washington state", dict(type="cart_target_override", value=0, scope={"scope_state": "WA"})),
    ("pull all the D stores this week", dict(type="cart_target_override", value=0, scope={"scope_velocity_tier": "D"})),
    ("skip the west", dict(type="cart_target_override", value=0, scope={"scope_region": "Western"})),
    # ── priority ──
    ("AA stores get first pick of the 12cm", dict(type="priority_boost", scope={"scope_velocity_tier": "AA", "scope_family": "12cm"})),
    ("prioritize Georgia if we run short", dict(type="priority_boost", scope={"scope_state": "GA"})),
    # ── sloppy typing, abbreviations ──
    ("fl stores +25% h2o bowl", dict(type="demand_multiplier", value=1.25, scope={"scope_state": "FL", "scope_family": BOWL})),
    ("pls take 608 off", dict(type="cart_target_override", value=0, scope={"scope_store_ids": [608]})),
    ("store #121 - xtra shelf littles", dict(type={"allocation_floor", "demand_multiplier", "directed_shipment"}, scope={"scope_store_ids": [121], "scope_family": "9cm Littles"})),
    ("NO BOWLS IN THE NORTH!!", dict(type="exclusion", scope={"scope_region": "Northern", "scope_family": BOWL})),
    # ── two instructions in one ──
    ("Take store 608 off and send 25% more bowls to Florida", dict(type={"cart_target_override", "demand_multiplier"}, n=2, any={"adjustment_type": {"cart_target_override", "demand_multiplier"}})),
    # ── must refuse, and say why ──
    ("set every A store to exactly 3 carts", dict(none=True)),
    ("order more plants from the greenhouse", dict(none=True)),
    ("make it a good week", dict(none=True)),
    ("ignore the do not ship list for store 664", dict(none=True)),
    ("change the price of collectors to $20", dict(none=True)),
    ("send bowls to store 99999", dict(none=True, warn="99999")),
    ("delete last week's run", dict(none=True)),
    # ── store-rule awareness ──
    ("give store 664 two shelves of littles", dict(type={"allocation_floor", "directed_shipment"}, scope={"scope_store_ids": [664]}, warn="do not ship")),
    ("25% more 17cm for store 608", dict(type="demand_multiplier", scope={"scope_store_ids": [608]}, warn="restriction")),
]


def check(res, exp):
    adj, warns = res["adjustments"], " | ".join(res["warnings"]).lower()
    if exp.get("none"):
        if adj:
            return f"expected a refusal, got {[a['adjustment_type'] for a in adj]}"
        if not res["warnings"]:
            return "refused without saying why"
    else:
        if not adj:
            return "no adjustment returned: " + (res["warnings"][0][:120] if res["warnings"] else "no reason")
        if "n" in exp and len(adj) != exp["n"]:
            return f"expected {exp['n']} adjustments, got {len(adj)}"
        types = exp.get("type")
        types = {types} if isinstance(types, str) else types
        for a in adj:
            if types and a["adjustment_type"] not in types:
                return f"type {a['adjustment_type']}, expected {sorted(types)}"
            if "value" in exp and abs(float(a.get("value_numeric") or 0) - exp["value"]) > 0.011:
                return f"value {a.get('value_numeric')}, expected {exp['value']}"
            for f, want in (exp.get("scope") or {}).items():
                got = a.get(f)
                ok = (bool(got) if want is True else got in want if isinstance(want, set)
                      else sorted(got or []) == sorted(want) if isinstance(want, list) else got == want)
                if not ok:
                    return f"{f} = {got!r}, expected {want!r}"
        for f, wants in (exp.get("any") or {}).items():
            have = {a.get(f) for a in adj}
            if not wants <= have:
                return f"{f}: missing {sorted(wants - have)} (got {sorted(x for x in have if x)})"
    if exp.get("warn") and exp["warn"].lower() not in warns:
        return f"expected a note mentioning '{exp['warn']}'"
    return None


def main():
    import psycopg2
    import psycopg2.extras
    from routers import scenarios as SC
    only = " ".join(sys.argv[1:]).lower()
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=25)
    conn.set_session(readonly=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cases = [c for c in CASES if only in c[0].lower()]
    fails, t0 = [], time.time()
    for text, exp in cases:
        try:
            res = SC.interpret(cur, text)
            err = check(res, exp)
        except Exception as e:
            res, err = None, f"crashed: {type(e).__name__}: {str(e)[:120]}"
            conn.rollback()
        print(("PASS  " if not err else "FAIL  ") + text + ("" if not err else f"\n        -> {err}"), flush=True)
        if err:
            fails.append(text)
            if res:
                for a in res["adjustments"]:
                    print("           got:", a["adjustment_type"], a.get("value_numeric"),
                          {k: v for k, v in a.items() if k.startswith("scope_") and v})
    print(f"\n{len(cases) - len(fails)} of {len(cases)} passed in {time.time() - t0:.0f}s")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
