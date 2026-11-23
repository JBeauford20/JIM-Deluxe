"""Editable engine policies — the catalog behind the Policies screen.

Every entry is one ipr_settings key the engine reads (workers/ipr_engine.py), with a plain
label, what it does, its type and safe bounds. `default` mirrors the engine's own fallback, so
a key with no database row shows the value the engine is actually using. Keys not listed
here stay read-only on the screen (structural settings: scoring weights, lot-age curve,
per-SKU freshness, store rule definitions).

Pure: validate() has no DB access and is unit-tested. Writes happen in routers/ipr.py
(manager / admin only, every change logged with who, when, old value, new value and why).
"""
from __future__ import annotations

GRADES = ["AA", "A", "B", "C+", "C", "C-", "D"]

# kind: int | number | money | pct | bool | choice | grade_int | grade_pct | map_int | map_pct | list
CATALOG = [
    # ── Minimum order and cart economics ────────────────────────────────────
    dict(key="min_order_full_carts", group="Minimum order and carts", label="Minimum full carts per order",
         kind="int", default=1, min=0, max=3,
         help="A store's order must fill at least this many full carts or the whole order waits a "
              "week. 0 turns the minimum off (every store with need ships)."),
    dict(key="min_cart_fill_pct", group="Minimum order and carts", label="How full a cart must be to count as full",
         kind="pct", default=0.9, min=0.3, max=1.0,
         help="Share of the cart's tray space that must be filled. Lowering it lets more stores "
              "ship, with lighter carts."),
    dict(key="min_cart_review_after_pushes", group="Minimum order and carts",
         label="Pushes in a row before a store is flagged for review", kind="int", default=4, min=1, max=12,
         help="A store pushed this many due cycles in a row under the cart minimum is flagged as "
              "an effective service stop for a DSM decision."),
    dict(key="cart_delivered_cost", group="Minimum order and carts", label="Delivered cost per cart",
         kind="money", default=240, min=50, max=1000,
         help="Freight assumption used for the freight % and the low-value cart test "
              "(leadership range $180 to $300)."),
    dict(key="cart_wholesale_low", group="Minimum order and carts", label="Low-value cart threshold (wholesale)",
         kind="money", default=1700, min=100, max=10000,
         help="A cart below this wholesale value is flagged low-value (leadership range $1,700 to $3,000)."),
    dict(key="cart_consolidate", group="Minimum order and carts", label="Pack each store's shelves into the fewest carts",
         kind="bool", default=True,
         help="On: shelves are packed best-fit so a store uses as few carts as physically possible. "
              "Never changes what the store receives."),
    # ── Delivery timing ─────────────────────────────────────────────────────
    dict(key="delivery_cadence_weeks", group="Delivery timing", label="Weeks between deliveries, by store grade",
         kind="grade_int", default={}, min=1, max=12, allow_blank=True,
         help="How often a store of each grade is due a delivery. Leave a grade blank to use the "
              "built-in seasonal schedule (in season AA/A/B 3, C 4, D 5; off season AA/A 4, B 5, C/D 6)."),
    dict(key="arrival_from_store_profile", group="Delivery timing",
         label="Use each store's usual delivery day", kind="bool", default=False,
         help="Off: every store is assumed to receive its order 2 days after the Monday of the ship "
              "week. On: each store's own usual day from routed history is used (network 7 days; "
              "North 6, South 7, West 8), so the order covers the sales made before the truck lands. "
              "Turning it on raises order sizes."),
    dict(key="cadence_buffer_weeks", group="Delivery timing", label="Extra weeks of cover on top of the cadence",
         kind="number", default=1, min=0, max=4,
         help="Each order covers the weeks until the next delivery plus this buffer."),
    dict(key="max_cover_weeks_after_delivery", group="Delivery timing",
         label="Most weeks of stock a store may hold after a delivery", kind="number",
         default=round(4 / 0.6, 2), min=1, max=26,
         help="Limits how far coverage can stretch to fill a cart. 6.67 = four weeks at the 60% "
              "sell-through target."),
    # ── Sell-through gates ──────────────────────────────────────────────────
    dict(key="conversion_hard_block", group="Sell-through gates", label="13-week sell-through stop line",
         kind="pct", default=0.6, min=0, max=1,
         help="A store and product family that sold less than this share of its mature deliveries "
              "is stopped for the week."),
    dict(key="conversion_thresholds", group="Sell-through gates", label="13-week sell-through target, by grade",
         kind="grade_pct", default={"AA": .67, "A": .67, "B": .7, "C+": .7, "C": .75, "C-": .8, "D": .8},
         min=0, max=1.5,
         help="Between the stop line and this target the family ships a reduced quantity; at or "
              "above it ships in full."),
    dict(key="conversion_delivery_lag_weeks", group="Sell-through gates",
         label="Weeks before a delivery counts toward sell-through", kind="int", default=2, min=0, max=8,
         help="Deliveries newer than this are too fresh to judge and are left out of the 13-week test."),
    dict(key="h2o_terrarium_window_weeks", group="Sell-through gates",
         label="H2O and Terrarium: most weeks of demand to ship", kind="number", default=13, min=1, max=52,
         help="H2O and Terrarium trays never exceed this many weeks of the store's demand."),
    # ── Demand ──────────────────────────────────────────────────────────────
    dict(key="weekly_objective_retail", group="Demand and goal", label="Weekly sales goal",
         kind="money", default=900000, min=0, max=10000000,
         help="The goal the This Week readout measures against (wholesale dollars). Reporting only; "
              "it never adds product to orders."),
    dict(key="corrective_test_13wk", group="Sell-through gates",
         label="Ship a capped test in place of a 13-week stop where the cause is explainable",
         kind="bool", default=True,
         help="AA and A families below the stop line always ship a capped test and are flagged for a "
              "look. Other grades do so only when they missed by a few points and recent sales were not "
              "a fair read (likely stocked out, or a sales-reporting gap). Off = every result below the "
              "stop line is stopped."),
    dict(key="corrective_test_share", group="Sell-through gates", label="Corrective test size, share of normal",
         kind="pct", default=0.5, min=0.1, max=1.0,
         help="A corrective test ships this share of the normal quantity, at least one tray."),
    dict(key="corrective_test_near_miss_points", group="Sell-through gates",
         label="How close to the stop line counts as a near miss",
         kind="pct", default=0.10, min=0.01, max=0.30,
         help="0.10 = within ten points of the stop line (50% to 60% when the stop line is 60%)."),
    dict(key="gap_fill_test_items", group="Minimum order and carts",
         label="Let test items from comparable stores help complete a cart",
         kind="bool", default=True,
         help="When a store's own products cannot finish a cart, add up to three products it has not "
              "sold, one tray each, chosen from what stores of the same grade and region sell. AA and A "
              "up to 20% of a cart, B 10%, C and D none."),
    dict(key="cart_completion", group="Minimum order and carts", label="Complete a short cart with qualified gap-fill",
         kind="bool", default=True,
         help="When a due store's order does not fill one cart, add trays of products that sell at that "
              "store, best first, until one cart is full. Off = a short order is always pushed."),
    dict(key="gap_fill_weeks_supply_by_grade", group="Minimum order and carts",
         label="Gap-fill ceiling: weeks of supply after delivery, by grade", kind="grade_int", min=1, max=12, allow_blank=True,
         default={"AA": 10, "A": 10, "B": 8, "C+": 8, "C": 7, "C-": 6, "D": 6},
         help="Gap-fill never takes a product past this many weeks of that store's sales (stock on hand "
              "plus this delivery). Higher = more stores complete a cart, with more inventory risk. "
              "Never past the freshness window or the 13-week H2O/Terrarium limit."),
    dict(key="weekly_growth_objective", group="Demand and goal", label="Weekly growth objective",
         kind="money", default=1200000, min=100000, max=5000000,
         help="The weekly sales the business is growing toward. The weekly minimum is the recovery "
              "floor; reports show the gap to both. Reporting only."),
    dict(key="plan_sell_through_pct", group="Demand and goal", label="Share of a shipment we expect to sell",
         kind="pct", default=0.6, min=0.1, max=1.0,
         help="Used for 'Added by this plan' on This Week: order value x this share, spread over the "
              "weeks below. Reporting only; it never changes what is ordered."),
    dict(key="plan_sell_through_weeks", group="Demand and goal", label="Weeks it takes to sell that share",
         kind="int", default=4, min=1, max=13,
         help="4 = the 60%-in-28-days target, so one week adds a quarter of it. Set to 1 to count the "
              "whole share in the first week. Reporting only."),
    dict(key="min_weekly_rate_units", group="Demand and goal", label="Slowest seller the engine will plan (units per week)",
         kind="number", default=0.25, min=0, max=10,
         help="A store and SKU selling slower than this is left out of orders."),
    dict(key="forecast_weights", group="Demand and goal", label="Forecast blend (4, 10 and 13 week sales)",
         kind="map_pct", default={"w4": .5, "w10": .3, "w13": .2}, min=0, max=1, sum_to=1.0,
         key_labels={"w4": "Last 4 weeks", "w10": "Last 10 weeks", "w13": "Last 13 weeks"},
         help="How much each sales window counts in the weekly forecast. Must total 100%."),
    dict(key="peak_floor_share", group="Demand and goal", label="Peak-season floor",
         kind="pct", default=0.5, min=0.05, max=1,
         help="Forecast is never below this share of the store's Apr-Jul rate (seasonally adjusted), "
              "so lines starved of supply do not vanish."),
    dict(key="peak_floor_require_starvation", group="Demand and goal",
         label="Peak floor only where the store was short of product", kind="bool", default=False,
         help="On: the floor applies only when recent deliveries could not have covered that demand."),
    dict(key="excluded_families", group="Demand and goal", label="Product families left out of routine orders",
         kind="list", default=[],
         help="Comma separated. These families never ship through the routine engine "
              "(seasonal product ships through Promotions)."),
    # ── Inventory ───────────────────────────────────────────────────────────
    dict(key="freshness_days_by_family", group="Inventory in stores", label="Days product stays sellable, by family",
         kind="map_int", default={"_default": 90}, min=14, max=400,
         key_labels={"_default": "Any other family"},
         help="Stock older than this is assumed dumped and no longer counts as on hand."),
    dict(key="availability_grade_first", group="Inventory in stores",
         label="Availability grade to ship first", kind="int", default=3, min=0, max=9,
         help="Plants of this grade are picked before any other grade of the same product. Grade 3 = "
              "more mature plants and contract-grower stock. It changes which plants are picked, "
              "never how much a store is sent. 0 = no preference."),
    dict(key="in_transit_low_factor", group="Inventory in stores", label="How much to trust estimated in-transit units",
         kind="pct", default=0.5, min=0, max=1,
         help="In-transit units whose store was estimated (not confirmed) count at this share."),
    dict(key="kit_split_ceramic_share", group="Inventory in stores", label="Ceramic share of 12cm and 17cm kits",
         kind="pct", default=0.5, min=0, max=1,
         help="When a plant can be kitted either way, this share goes ceramic and the rest jute."),
    # ── Learning and scenarios ──────────────────────────────────────────────
    dict(key="post_delivery_feedback", group="Learning and scenarios", label="Use measured shipment response",
         kind="choice", default="observe", choices=["off", "observe", "apply"],
         choice_labels={"off": "Off", "observe": "Measure and report only",
                        "apply": "Apply: shift a store's cadence by up to one week"},
         help="How the 7 to 28 day sales response after each delivery is used."),
    dict(key="scenario_max_multiplier", group="Learning and scenarios", label="Largest demand multiplier a scenario may apply",
         kind="number", default=3, min=1, max=10,
         help="An approved scenario can never multiply a store's demand by more than this."),
    dict(key="scenario_priority_boost_cap", group="Learning and scenarios", label="Largest priority boost a scenario may apply",
         kind="number", default=0.2, min=0, max=1,
         help="Caps how far a scenario can move a store up the allocation order (0 to 1 scale)."),
]
BY_KEY = {e["key"]: e for e in CATALOG}


def _num(v, e, what=""):
    if isinstance(v, bool) or v is None or (isinstance(v, str) and not v.strip()):
        raise ValueError(f"{what or e['label']}: enter a number")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what or e['label']}: '{v}' is not a number")
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError(f"{what or e['label']}: not a valid number")
    lo, hi = e.get("min"), e.get("max")
    if lo is not None and f < lo or hi is not None and f > hi:
        pct = e["kind"].endswith("pct")
        show = (lambda x: f"{x * 100:g}%") if pct else (lambda x: f"{x:g}")
        raise ValueError(f"{what or e['label']}: must be between {show(lo)} and {show(hi)}")
    return f


def validate(key: str, value):
    """Normalised value for `key`, or ValueError with a message a person can act on."""
    e = BY_KEY.get(key)
    if not e:
        raise ValueError(f"'{key}' cannot be edited from the Policies screen")
    k = e["kind"]
    if k == "int":
        f = _num(value, e)
        if not float(f).is_integer():
            raise ValueError(f"{e['label']}: must be a whole number")
        return int(f)
    if k in ("number", "money", "pct"):
        f = _num(value, e)
        return int(f) if k == "money" and float(f).is_integer() else round(f, 4)
    if k == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{e['label']}: must be on or off")
        return value
    if k == "choice":
        if value not in e["choices"]:
            raise ValueError(f"{e['label']}: must be one of {', '.join(e['choices'])}")
        return value
    if k == "list":
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            raise ValueError(f"{e['label']}: must be a list of names")
        out = []
        for x in value:
            x = x.strip()
            if x and x not in out:
                out.append(x)
        if any(len(x) > 80 for x in out) or len(out) > 60:
            raise ValueError(f"{e['label']}: too long")
        return out
    if k in ("grade_int", "grade_pct", "map_int", "map_pct"):
        if not isinstance(value, dict):
            raise ValueError(f"{e['label']}: must be a set of values")
        grade = k.startswith("grade")
        whole = k.endswith("int")
        out = {}
        for name, v in value.items():
            name = str(name).strip()
            if not name or len(name) > 60:
                raise ValueError(f"{e['label']}: bad name '{name}'")
            if grade and name not in GRADES:
                raise ValueError(f"{e['label']}: '{name}' is not a store grade")
            if v is None or (isinstance(v, str) and not v.strip()):
                if e.get("allow_blank"):
                    continue
                raise ValueError(f"{e['label']}: {name} is blank")
            f = _num(v, e, f"{e['label']} ({name})")
            if whole and not float(f).is_integer():
                raise ValueError(f"{e['label']} ({name}): must be a whole number")
            out[name] = int(f) if whole else round(f, 4)
        if grade and not e.get("allow_blank") and set(out) != set(GRADES):
            raise ValueError(f"{e['label']}: every grade needs a value ({', '.join(GRADES)})")
        if not grade and not e.get("allow_blank"):
            need = set(e["default"]) if k == "map_pct" else {"_default"} & set(e["default"])
            missing = need - set(out)
            if missing:
                raise ValueError(f"{e['label']}: missing {', '.join(sorted(missing))}")
        if e.get("sum_to") is not None and abs(sum(out.values()) - e["sum_to"]) > 0.011:
            raise ValueError(f"{e['label']}: the parts must total {e['sum_to'] * 100:g}% "
                             f"(they total {sum(out.values()) * 100:g}%)")
        return out
    raise ValueError(f"{e['label']}: unsupported setting type")


def describe(rows: dict, history_by_key: dict | None = None) -> dict:
    """Catalog merged with the database rows ({key: row}). Returns {groups, advanced}."""
    history_by_key = history_by_key or {}
    groups, order = {}, []
    for e in CATALOG:
        row = rows.get(e["key"])
        item = {k: e[k] for k in ("key", "label", "help", "kind", "default") if k in e}
        for k in ("min", "max", "choices", "choice_labels", "key_labels", "allow_blank", "sum_to"):
            if k in e:
                item[k] = e[k]
        item["value"] = row["value"] if row else e["default"]
        item["is_default"] = row is None
        item["status"] = (row or {}).get("status")
        item["spec_ref"] = (row or {}).get("spec_ref")
        item["updated_at"] = (row or {}).get("updated_at")
        item["last_change"] = history_by_key.get(e["key"])
        if e["group"] not in groups:
            groups[e["group"]] = []
            order.append(e["group"])
        groups[e["group"]].append(item)
    advanced = [dict(r) for k, r in sorted(rows.items()) if k not in BY_KEY]
    return {"groups": [{"name": g, "settings": groups[g]} for g in order], "advanced": advanced,
            "grades": GRADES}
