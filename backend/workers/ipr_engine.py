"""
IPR Engine v2 — demand-first replenishment, built to the AI Replenishment master
specification of 2026-11-17 (the "Bible": JIM Deluxe/AI replenishment tool nov 17th.docx).
§ references below point into that document.

Decision sequence (§32), in code order:
  1. Validate data (§5)                — stale sales / missing mappings / EDI gaps; never zero-fill
  2. What can each store sell? (§7)    — multi-window store x SKU velocity, units first
  3. What remains at arrival? (§8–9)   — confidence-adjusted on-hand + inbound − sales before arrival
  4. Does the store need product? (§10)— unconstrained need, computed BEFORE availability
  5. Gates (§11, §24)                  — family grades, mature-delivery conversion, hard blocks
  6. Allocate availability (§15)       — supply is a constraint, never a demand factor
  7. Trays -> cart-loading plan (§17–19) — physical fit only; never adds or forces trays
  8. Review + $900K readout (§2, §25)

Deliberately ABSENT (spec + Josh 2026-11-17): 90% clearance floor, zero-SKU rescue, cart
minimum fill, cart-config caps/consolidation, cart-count caps, truck building (routing is
out of scope — the Routing Exchange owns trucks), availability weighting in any score.

Writes the same tables as the legacy engine (recommendation_runs/orders/order_lines/
order_carts/cart_shelves/order_line_allocations/cart_config_templates*) so the Routing
Exchange, pack plans, Aster export, Cart Configs and editor all work unchanged.
"""
import json
import math
import uuid
from collections import Counter, defaultdict
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

import store_rules
from db_conn import get_conn
# Proven pieces carried over from the legacy engine (physical/packing truths, not policy)
from recommendation_engine import (CART_SHELVES, CART_CEILING_IN, MIN_SHELF_CLEAR_IN,
                                   PROFILE_HEIGHT_RANK,
                                   _distribute_units_across_lines)

ENGINE_VERSION = "v2"
ARRIVAL_OFFSET_DAYS = 2          # shipping week Monday -> typical in-store arrival
STALE_SALES_BLOCK_DAYS = 21      # sales older than this = Incomplete Data (§5)
STALE_SALES_WARN_DAYS = 9
H2O_TERR_FAMILIES = {"H2O", "Terrarium"}
GRADE_ORDER = ["AA", "A", "B", "C+", "C", "C-", "D"]
GRADE_SCORE = {"AA": 1.0, "A": .85, "B": .7, "C+": .55, "C": .4, "C-": .25, "D": .1}
# §11.3 reduce-quantity band (conversion between the 60% floor and the grade threshold):
# share of need trays shipped, by store-family grade. Round down, minimum 1 tray.
REVIEW_BAND_SHIP_PCT = {"AA": 1.0, "A": 1.0, "B": .75, "C+": .75, "C": .5, "C-": .5, "D": .5}
RISK_SCORE = {"Critical": 1.0, "High": .75, "Moderate": .4, "Low": .1, "Unknown": .3}
BLOCK_STATUSES = ("do_not_ship", "paused", "inactive", "hold")
# Duplicate-run control (§1, §24, §28): run statuses that no longer represent live orders,
# and statuses/pipeline stages meaning the week's orders have already left the building.
CANCELLED_RUN_STATUSES = ("cancelled", "canceled", "failed", "rejected", "superseded", "deleted")
RELEASED_RUN_STATUSES = ("exported", "released", "shipped")
RELEASED_PIPELINE = ("Aster Exported", "Released", "Shipped", "Received or Estimated Received",
                     "Performance Matured", "Sent to Pin Routes", "Routing Response Received")


# Delivery cadence by store grade (Josh 2026-11-19; §10 coverage varies by delivery frequency,
# §19.1 release on service urgency). Defaults; ipr_settings rows override.
DEFAULT_CADENCE_WEEKS = {"AA": 1, "A": 1, "B": 2, "C+": 3, "C": 3, "C-": 4, "D": 4}
# One-full-CC-cart minimum (leadership 2026-11-20) + calibration (2026-11-20): a full cart is
# ~240 units = 3.6 wks of a median AA store's in-season sales, 8 wks of a D store's. Cadence
# by season so each due store's order can fill a cart. Off season = Sep-Feb (sales ~0.65x Apr-Jul).
SEASONAL_CADENCE_WEEKS = {
    "in":  {"AA": 3, "A": 3, "B": 3, "C+": 4, "C": 4, "C-": 5, "D": 5},
    "off": {"AA": 4, "A": 4, "B": 5, "C+": 6, "C": 6, "C-": 6, "D": 6},
}
OFF_SEASON_MONTHS = {9, 10, 11, 12, 1, 2}
DEFAULT_CADENCE_BUFFER_WEEKS = 1
SETTINGS_OVERRIDE = {}           # preview-only what-if overrides of ipr_settings (scenario analysis)
STORE_OVERRIDES = {}             # preview-only {store_id: {col: value}} (e.g. a store master not yet loaded)
# Weekly DTS feeds repeat delivery rows from other weeks (25.5K duplicate units found 2026-11-20;
# the loader now skips them, but the table has no unique key). Read deliveries through this
# drop-in dedupe: first feed to report a (store, sku, delivery_date) wins. Rows up to 2026-10-01
# are the single-feed ledger era and pass through untouched.
DEDUP_DELIVERIES = """(
    SELECT store_id, sku_id, delivery_date, units_delivered FROM deliveries
    WHERE delivery_date <= DATE '2026-10-01'
    UNION ALL
    SELECT store_id, sku_id, delivery_date, units_delivered FROM (
        SELECT store_id, sku_id, delivery_date, units_delivered, feed_source,
               MIN(feed_source) OVER (PARTITION BY store_id, sku_id, delivery_date) AS _kf
        FROM deliveries WHERE delivery_date > DATE '2026-10-01') z
    WHERE feed_source = _kf)"""
DUE_SLACK_DAYS = 3


def cadence_grade(volume_code, best_family_grade, cadence):
    """Store cadence grade: store-master volume grade, else best family grade, else 'C'."""
    if volume_code in cadence:
        return volume_code
    if best_family_grade in cadence:
        return best_family_grade
    return "C"


def due_status(last_delivery, arrival, cadence_weeks, any_critical=False):
    """(is_due, next_due_date). Due when there is no prior delivery, when the gap from the last
    delivery to this arrival is >= cadence x 7 - 3 days, or when any line is Critical risk."""
    if last_delivery is None:
        return True, arrival
    next_due = last_delivery + timedelta(days=cadence_weeks * 7 - DUE_SLACK_DAYS)
    return (any_critical or arrival >= next_due), next_due


def coverage_weeks(cadence_weeks, buffer_weeks, fresh_days):
    """Coverage for a due store's lines = cadence + buffer, never past the freshness window."""
    return min(float(cadence_weeks + buffer_weeks), fresh_days / 7.0)


def prior_economic_deferrals(history):
    """Consecutive economic deferrals in the store's most recent DUE cycles.
    history: decision-kind sets for this store, one per earlier v2 run (latest run per ship
    week), newest first. A not-due week is not a due cycle and is skipped; any other due cycle
    (shipped, released, absent) ends the streak."""
    n = 0
    for kinds in history:
        if "deferred_economics" in kinds:
            n += 1
        elif "not_due" in kinds:
            continue
        else:
            break
    return n


def shrink_rate(delivered, sold, onhand_change=None, efficiency=None, cap=0.6):
    """§8 expected shrink for a family (network): 1 − (sold + Δ on-hand) ÷ delivered over the
    shrink window, clamped 0..cap. Without on-hand history: 1 − shipping efficiency.
    Returns (rate, source) or (None, 'no data')."""
    if delivered and onhand_change is not None:
        raw, src = 1.0 - (sold + onhand_change) / delivered, "sold + Δ DTS on-hand vs delivered"
    elif efficiency is not None:
        raw, src = 1.0 - efficiency, "fallback 1 − shipping efficiency (no on-hand history)"
    else:
        return None, "no data"
    return round(min(cap, max(0.0, raw)), 4), src


def is_overdue(last_delivery, arrival, cadence_weeks):
    """Week-1 deferral seed: a store not served within cadence + 1 weeks (or never served) has
    already waited — it is never economically deferred."""
    return last_delivery is None or (arrival - last_delivery).days > (cadence_weeks + 1) * 7


def economic_action(wholesale, carts, lost_one_week, cart_cost, wholesale_low, any_critical,
                    prior_deferrals, max_deferrals, overdue=False):
    """§19.1 economic test on a due store's whole order (Josh option B, 2026-11-19).
    Returns 'pass' (not low value), 'release' (passes on lost sales / Critical), 'defer'
    (first time), or 'release_after_deferral' (already deferred max_deferrals due cycles)."""
    if carts and wholesale / carts >= wholesale_low:
        return "pass"
    if lost_one_week >= carts * cart_cost or any_critical:
        return "release"
    if overdue:
        return "release_overdue"
    if prior_deferrals >= max_deferrals:
        return "release_after_deferral"
    return "defer"


def shipping_efficiency(sold, delivered):
    """Josh's KPI: 13-wk units sold / 13-wk units delivered (same window, no lag)."""
    return round(sold / delivered, 4) if delivered else None


def run(batch_id, shipping_week=None, preview=False, verbose=True, what_if_unlimited=False):
    """what_if_unlimited (preview only): every mapped, packable SKU treated as available in
    unlimited quantity — the spec's unconstrained-need view, for analysis. Never writes."""
    if what_if_unlimited and not preview:
        raise ValueError("what_if_unlimited is preview-only")
    conn = get_conn()
    try:
        return _run(conn, batch_id, shipping_week, preview, verbose, what_if_unlimited)
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def _norm(vals):
    """Map a dict of values to 0..1 by max (robust to all-zero)."""
    mx = max(vals.values(), default=0) or 0
    return {k: (v / mx if mx else 0.0) for k, v in vals.items()}


def _run(conn, batch_id, shipping_week, preview, verbose, what_if_unlimited=False):
    log = print if verbose else (lambda *a, **k: None)
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    today = date.today()
    if shipping_week is None:
        shipping_week = today + timedelta(days=((7 - today.weekday()) % 7 or 7))
    decisions = []          # (decision_type, store_id, sku_id, reason, data)

    def decide(kind, reason, store_id=None, sku_id=None, **data):
        decisions.append((kind, store_id, sku_id, reason, data))

    # ── settings (every tunable is a DB row flagged with its spec status) ──
    cur.execute("SELECT key, value FROM ipr_settings")
    S = {r["key"]: r["value"] for r in cur.fetchall()}
    if preview and SETTINGS_OVERRIDE:          # what-if scenarios (preview only, never written)
        S.update(SETTINGS_OVERRIDE)
    cover_wk = float(S["coverage_weeks"])        # legacy flat coverage; cadence coverage below
    _season = "off" if (shipping_week or date.today()).month in OFF_SEASON_MONTHS else "in"
    _base_cad = (SEASONAL_CADENCE_WEEKS[_season] if int(S.get("min_order_full_carts", 1) or 0) > 0
                 else DEFAULT_CADENCE_WEEKS)
    cadence = {**_base_cad, **(S.get("delivery_cadence_weeks") or {})}
    _cb = S.get("cadence_buffer_weeks")
    cad_buffer = float(DEFAULT_CADENCE_BUFFER_WEEKS if _cb is None else _cb)
    econ_defer_due = str(S.get("economic_deferral_for_due_stores", False)).lower() in ("true", "1", "yes")
    # Josh option B (2026-11-19): a due store failing the §19.1 test is deferred at most this many
    # consecutive due cycles, then ships regardless. economic_deferral_for_due_stores=true means
    # no limit (always defer when the test fails).
    _md = S.get("max_economic_deferrals")
    max_econ_def = 10 ** 6 if econ_defer_due else int(1 if _md is None else _md)
    # §29 post-delivery feedback (leadership 11/20: 7–28 day shipment response, measured by
    # services/post_delivery.py each week). observe = report only (default). apply = a store
    # whose measured response is clearly strong/weak gets its cadence shifted ONE week,
    # bounded and logged — the training loop: measure → aggregate → adjust → measure again.
    pd_mode = str(S.get("post_delivery_feedback", "observe") or "observe").lower()
    pd_response, pd_adj = {}, {}
    if pd_mode in ("observe", "apply"):
        cur.execute("SELECT to_regclass('public.store_family_response') IS NOT NULL AS ok")
        if cur.fetchone()["ok"]:
            cur.execute("""SELECT store_id, ipr_family, cohorts, avg_lift, avg_sell28,
                                  response_class
                           FROM store_family_response WHERE cohorts >= 2""")
            pd_response = {(r["store_id"], r["ipr_family"]): dict(r) for r in cur.fetchall()}
    if pd_mode == "apply" and pd_response:
        _t = defaultdict(lambda: [0, 0, 0])            # strong, weak, total cohorts per store
        for (st_, _f), r in pd_response.items():
            _t[st_][2] += r["cohorts"]
            if r["response_class"] == "strong":
                _t[st_][0] += r["cohorts"]
            elif r["response_class"] == "weak":
                _t[st_][1] += r["cohorts"]
        for st_, (s_, w_, n_) in _t.items():
            if n_ >= 3 and s_ / n_ >= 0.5:
                pd_adj[st_] = -1                        # proven responder: one week sooner
            elif n_ >= 3 and w_ / n_ >= 0.5:
                pd_adj[st_] = 1                         # proven weak: one week later
    fw = S["forecast_weights"]
    min_rate = float(S["min_weekly_rate_units"])
    conv_thr = S["conversion_thresholds"]
    hard_block = float(S["conversion_hard_block"])
    lag_days = int(S["conversion_delivery_lag_weeks"]) * 7
    conf_factor = S["onhand_confidence_factor"]
    sp_w = S["store_priority_weights"]
    sf_w = S["sku_fit_weights"]
    cutoffs = S["grade_cutoffs_pct"]
    objective = float(S["weekly_objective_retail"])
    fresh_wk = int(S.get("freshness_weeks", 8))
    fresh_days = fresh_wk * 7

    log(f"\n{'=' * 62}\nIPR Engine v2 — batch {batch_id} — ship week {shipping_week}\n{'=' * 62}")

    # ══ 1. DATA VALIDATION (§5) ══════════════════════════════════════════
    cur.execute("SELECT MAX(sale_date) AS mx FROM sales WHERE sale_date <= CURRENT_DATE")
    anchor = cur.fetchone()["mx"]
    if anchor is None:
        raise ValueError("Incomplete Data: no sales history")
    stale = (today - anchor).days
    if stale > STALE_SALES_BLOCK_DAYS:
        raise ValueError(f"Incomplete Data: newest sales are {stale} days old (> {STALE_SALES_BLOCK_DAYS}) — "
                         f"run the Monday ingest before planning (§5)")
    if stale > STALE_SALES_WARN_DAYS:
        decide("validation", f"Sales data is {stale} days old", severity="warning")
    arrival = shipping_week + timedelta(days=ARRIVAL_OFFSET_DAYS)
    days_to_arrival = max(0, (arrival - anchor).days)
    log(f"Sales anchor {anchor} ({stale}d old) · arrival {arrival} · {days_to_arrival}d of sales before arrival")

    # Duplicate-run control (§1 "may not override duplicate-order controls", §24 "block when the
    # order duplicates covered inbound supply"). A released/exported run for this ship week
    # refuses outright; earlier live runs for the same week are counted as inbound below so a
    # rerun ships only the residual need, never the same order twice.
    # Live = v2 runs (any non-cancelled status) plus exported/released runs of any engine;
    # leftover v1 drafts are not live orders and are ignored.
    cur.execute("""SELECT id, status, pipeline_status, engine_version FROM recommendation_runs
                   WHERE shipping_week = %s
                     AND COALESCE(status, '') NOT IN %s""",
                (shipping_week, CANCELLED_RUN_STATUSES))
    prior_runs = [r for r in cur.fetchall()
                  if r["engine_version"] == ENGINE_VERSION or r["status"] in RELEASED_RUN_STATUSES
                  or (r["pipeline_status"] or "") in RELEASED_PIPELINE]
    released = [r for r in prior_runs
                if r["status"] in RELEASED_RUN_STATUSES or (r["pipeline_status"] or "") in RELEASED_PIPELINE]
    if released:
        raise ValueError(
            f"Duplicate run refused: ship week {shipping_week} already has a released/exported run "
            f"({', '.join(str(r['id']) + ' ' + str(r['status']) for r in released)}). Cancel that run "
            f"before planning this week again, or plan the next ship week.")
    same_week_prior = {}
    if prior_runs:
        cur.execute("""
            SELECT o.store_id, ol.sku_id,
                   SUM(COALESCE(ol.approved_qty, ol.draft_qty, ol.recommended_qty)) AS qty
            FROM orders o JOIN order_lines ol ON ol.order_id = o.id
            WHERE o.run_id = ANY(%s::uuid[])
            GROUP BY o.store_id, ol.sku_id
        """, ([str(r["id"]) for r in prior_runs],))
        same_week_prior = {(r["store_id"], r["sku_id"]): int(r["qty"] or 0) for r in cur.fetchall()}
        decide("duplicate_run", f"{len(prior_runs)} earlier run(s) exist for ship week {shipping_week} — "
                                f"their {sum(same_week_prior.values()):,} units are counted as inbound; "
                                f"this run plans only the residual need", severity="warning",
               prior_runs=[{"id": str(r["id"]), "status": r["status"]} for r in prior_runs],
               prior_units=sum(same_week_prior.values()))
        log(f"Duplicate-run control: {len(prior_runs)} earlier run(s) for {shipping_week} — "
            f"{sum(same_week_prior.values()):,}u counted as inbound")

    cur.execute("""SELECT al.id, al.sku_id, al.units_available, al.code_level, al.hard_good_type,
                          al.plant_id
                   FROM availability_lines al WHERE al.batch_id = %s ORDER BY al.sku_id, al.id""",
                (batch_id,))
    avail_rows = cur.fetchall()
    if not avail_rows:
        raise ValueError("Incomplete Data: availability batch has no lines")
    # Plant genus per availability line (store master variety restrictions, e.g. "No Hedera").
    # HD SKUs are assortments; the variety — and so the genus — lives on the Aster plant code.
    _pids = sorted({str(r["plant_id"]) for r in avail_rows if r["plant_id"]})
    plant_genus, plant_name = {}, {}
    if _pids:
        cur.execute("""SELECT aster_code::text AS c, parsed_genus, product_name
                       FROM variety_parsed_reference WHERE aster_code::text = ANY(%s)""", (_pids,))
        for r in cur.fetchall():
            plant_genus[r["c"]] = (r["parsed_genus"] or "").strip().lower()
            plant_name[r["c"]] = r["product_name"] or ""
        cur.execute("""SELECT aster_code::text AS c, aster_name FROM aster_product_mapping
                       WHERE aster_code::text = ANY(%s)""", (_pids,))
        for r in cur.fetchall():
            plant_name.setdefault(r["c"], r["aster_name"] or "")
    supply = defaultdict(int)
    avail_meta, aster_lines = {}, defaultdict(list)
    line_genus = {}
    for r in avail_rows:
        supply[r["sku_id"]] += int(r["units_available"] or 0)
        avail_meta.setdefault(r["sku_id"], {"code_level": r["code_level"],
                                            "hard_good_type": r["hard_good_type"]})
        aster_lines[r["sku_id"]].append([str(r["id"]), int(r["units_available"] or 0)])
        line_genus[str(r["id"])] = plant_genus.get(str(r["plant_id"] or ""), "")
    for s in aster_lines:
        aster_lines[s].sort(key=lambda x: (-x[1], x[0]))
    total_supply = sum(supply.values())

    # SKU master: family, prices, packing (units per tray, shelf capacity, height).
    # k.family / k.legacy_product_group are the v1 naming the scenario scopes match on.
    cur.execute("""
        SELECT k.sku_id, k.description, k.wholesale_price_whl, k.is_holiday,
               k.family, k.legacy_product_group,
               fm.ipr_family, fm.approved, pb.retail_price
        FROM skus k
        LEFT JOIN ipr_family_map fm ON fm.sku_id = k.sku_id
        LEFT JOIN pricebook_hd pb ON pb.sku_id = k.sku_id
    """)
    sku_info = {r["sku_id"]: dict(r) for r in cur.fetchall()}
    cur.execute("""
        SELECT spp.sku_id, spp.profile_id, pp.units_per_tray, pp.clear_height_in,
               sc.trays_per_shelf, sc.max_shelves
        FROM sku_packing_profiles spp
        JOIN packing_profiles pp ON pp.id = spp.profile_id
        LEFT JOIN shelf_configurations sc ON sc.profile_id = spp.profile_id
             AND sc.cart_type_id = 'cc' AND sc.approved = true
        ORDER BY spp.sku_id, spp.is_default DESC, spp.profile_id
    """)
    pack = {}
    for r in cur.fetchall():
        pack.setdefault(r["sku_id"], dict(r))

    if what_if_unlimited:
        for sku_id in sku_info:
            if pack.get(sku_id, {}).get("trays_per_shelf"):
                supply[sku_id] = 10 ** 7
                avail_meta.setdefault(sku_id, {"code_level": "3M", "hard_good_type": None})
        total_supply = sum(supply.values())
        log("WHAT-IF: every mapped, packable SKU treated as available (preview only)")

    excluded_fams = set(S.get("excluded_families") or [])   # e.g. Seasonal/Holiday: not routine replenishment
    plannable = set()
    for sku_id in supply:
        info, pk = sku_info.get(sku_id), pack.get(sku_id)
        if not info:
            decide("validation", "Availability SKU not in SKU master — excluded", sku_id=sku_id, severity="block")
        elif (info["ipr_family"] or "Unmapped") in excluded_fams:
            decide("validation", f"{info['ipr_family']} is excluded from routine replenishment (seasonal "
                                 f"programs ship through Promotions)", sku_id=sku_id, severity="info")
        elif not pk or not pk["units_per_tray"] or not pk["trays_per_shelf"]:
            decide("validation", "No approved packing/shelf configuration — cannot be loaded", sku_id=sku_id,
                   severity="block", profile=(pk or {}).get("profile_id"))
        elif (info["ipr_family"] or "Unmapped") == "Unmapped":
            decide("validation", "SKU has no product-family mapping (§4.5) — excluded", sku_id=sku_id,
                   severity="block")
        else:
            plannable.add(sku_id)
            if not info["approved"]:
                decide("validation", "Family mapping not yet business-approved (§4.5)", sku_id=sku_id,
                       severity="warning", family=info["ipr_family"])
    unapproved_map = sum(1 for s in plannable if not sku_info[s]["approved"])

    def usd(l):
        """Price for $ totals: a missing price contributes nothing (never guessed)."""
        return l["price"] if l["price"] is not None else 0.0

    def price(sku_id, realized=None):
        # Leadership goal ($900K/wk, 2026-11-20 meeting) is WHOLESALE at SKU wholesale price:
        # every $ figure prices at skus.wholesale_price_whl first; realized DTS $/unit (also
        # wholesale) next; pricebook retail only as a last resort (flagged by price_src).
        info = sku_info.get(sku_id) or {}
        if info.get("wholesale_price_whl"):
            return float(info["wholesale_price_whl"]), "wholesale"
        if realized:
            return realized, "realized"
        if info.get("retail_price"):
            return float(info["retail_price"]), "pricebook"
        # No retail price: never guessed (wholesale x2 removed). The line still plans on units;
        # it is excluded from every $ total and counted in summary.incomplete_price_units.
        return None, "missing"

    # ── SCENARIOS (approved plain-English directives; checks-and-balances chain of
    # 2026-11-20: an LLM parses the English, a DIFFERENT person approves, the engine applies
    # DETERMINISTICALLY and BOUNDED here, and the independent scenario audit verifies the
    # result after the run). Only approved + active scenarios reach this query.
    scn_max_mult = float(S.get("scenario_max_multiplier", 3.0) or 3.0)
    scn_boost_cap = float(S.get("scenario_priority_boost_cap", 0.2) or 0.2)
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'planning_scenarios' AND column_name = 'status'""")
    _scn_status = bool(cur.fetchone())
    cur.execute(f"""
        SELECT wa.id::text AS adj_id, wa.scenario_id::text AS scenario_id, wa.label,
               wa.adjustment_type, wa.value_numeric,
               wa.scope_merchant, wa.scope_region, wa.scope_state, wa.scope_velocity_tier,
               wa.scope_store_ids, wa.scope_sku_id, wa.scope_family, wa.scope_legacy_tier,
               ps.name AS scenario_name, ps.created_by AS requested_by
               {", ps.approved_by" if _scn_status else ", NULL AS approved_by"}
        FROM weekly_adjustments wa
        LEFT JOIN planning_scenarios ps ON ps.id = wa.scenario_id
        WHERE wa.shipping_week = %s AND wa.active
          AND (wa.scenario_id IS NULL OR
               (COALESCE(ps.is_active, true) {"AND ps.status = 'approved'" if _scn_status else ""}))
        ORDER BY wa.created_at
    """, (shipping_week,))
    scn_all = [dict(r) for r in cur.fetchall()]
    scn_mult = [a for a in scn_all if a["adjustment_type"] == "demand_multiplier"]
    scn_excl = ([a for a in scn_all if a["adjustment_type"] == "exclusion"]
                + [a for a in scn_all if a["adjustment_type"] == "cart_target_override"
                   and not float(a["value_numeric"] or 0)])
    scn_floor = [a for a in scn_all if a["adjustment_type"] == "allocation_floor"]
    scn_cap = [a for a in scn_all if a["adjustment_type"] == "allocation_cap"]
    scn_boost = [a for a in scn_all if a["adjustment_type"] == "priority_boost"]
    scn_unsupported = [a for a in scn_all
                       if a["adjustment_type"] == "sku_push"
                       or (a["adjustment_type"] == "cart_target_override" and float(a["value_numeric"] or 0))]
    scn_fx = {a["adj_id"]: {"label": a["label"], "scenario_id": a["scenario_id"],
                            "type": a["adjustment_type"], "lines": 0, "stores": set(),
                            "trays_requested": 0, "trays_granted": 0, "trays_trimmed": 0}
              for a in scn_all}
    if scn_all:
        log(f"Scenarios: {len(scn_all)} approved directive(s) for {shipping_week} "
            f"({len(scn_unsupported)} unsupported in v2)")

    # Stores. §1/§4.1 hard-block fields (store capacity, approved assortment, receiving
    # restrictions, DSM assignment) ship in the dsm_portal_2026_11_20 migration; read them
    # when present so the engine works against a database that predates it.
    cur.execute("""SELECT column_name::text AS c FROM information_schema.columns
                   WHERE table_name = 'stores' AND column_name IN
                         ('capacity_carts', 'approved_assortment', 'receiving_restrictions',
                          'dsm', 'dsm_email', 'sku_rule', 'sku_restriction', 'cross_dock',
                          'master_status')""")
    _hb_cols = sorted({r["c"] for r in cur.fetchall()})
    cur.execute(f"""SELECT store_id, store_name, active, store_status, dynamic_velocity_tier,
                           region, merchant, state, volume_code{''.join(', ' + c for c in _hb_cols)}
                    FROM stores ORDER BY store_id""")
    stores = {r["store_id"]: dict(r) for r in cur.fetchall()}
    if preview and STORE_OVERRIDES:            # what-if store attributes (preview only, never written)
        for _sid, _patch in STORE_OVERRIDES.items():
            if _sid in stores:
                stores[_sid].update(_patch)
        _hb_cols = sorted(set(_hb_cols) | {k for _p in STORE_OVERRIDES.values() for k in _p})
    hard_block_data = {
        "columns_present": _hb_cols,
        "capacity_carts_set": sum(1 for s in stores.values() if s.get("capacity_carts")),
        "approved_assortment_set": sum(1 for s in stores.values() if s.get("approved_assortment")),
        "receiving_restrictions_set": sum(1 for s in stores.values() if s.get("receiving_restrictions")),
        "note": "§1/§4.1 hard blocks are inert for stores where the field is NULL",
    }

    # STORE MASTER HANDLING RULES (Home Depot Store List, first sheet; workers/store_rules.py).
    # Hard blocks the agent may never override (§1): do-not-ship is stores.store_status; the
    # SKU / variety restrictions are stores.sku_rule. A rule code with no definition blocks the
    # store's routine replenishment (fail closed) — a restriction is never silently ignored.
    rules = store_rules.merged_rules(S.get("store_sku_rules"))
    store_rule = {sid: store_rules.rule_for(st_, rules) for sid, st_ in stores.items()
                  if (st_.get("sku_rule") or "").strip()}
    store_deny = {sid: store_rules.denied_genera(r) for sid, r in store_rule.items()
                  if store_rules.denied_genera(r)}
    _sensitive = set().union(*store_deny.values()) if store_deny else set()
    # A line whose plant code has no variety reference cannot be genus-checked by lookup; fall
    # back to the Aster product name so a denied genus named in it is still caught.
    for r in avail_rows:
        lid = str(r["id"])
        if not line_genus.get(lid) and _sensitive:
            nm = (plant_name.get(str(r["plant_id"] or ""), "") or "").lower()
            hit = next((g for g in sorted(_sensitive) if g and g in nm.split()), None)
            if hit:
                line_genus[lid] = hit
    pools = store_rules.VarietyPools(_sensitive)
    for r in avail_rows:
        pools.add(r["sku_id"], line_genus.get(str(r["id"]), ""), int(r["units_available"] or 0))
    if what_if_unlimited:
        for sku_id in supply:
            if supply[sku_id] >= 10 ** 7:
                pools.set_unlimited(sku_id, supply[sku_id])
    NO_DENY = frozenset()
    _unref = [r for r in avail_rows if r["plant_id"] and str(r["plant_id"]) not in plant_genus
              and str(r["plant_id"]) not in plant_name]
    if _sensitive and _unref:
        decide("validation",
               f"{len(_unref)} availability line(s) have a plant code with no variety reference — "
               f"they cannot be checked against the variety restriction(s) "
               f"({', '.join(sorted(g.title() for g in _sensitive))}) and are treated as permitted",
               severity="warning", plant_ids=sorted({str(r['plant_id']) for r in _unref})[:40],
               units=sum(int(r["units_available"] or 0) for r in _unref))
    _unknown_rules = sorted({r["code"] for r in store_rule.values() if r.get("unknown")})
    if _unknown_rules:
        decide("validation",
               f"Store rule code(s) with no definition: {', '.join(_unknown_rules)} — those stores "
               f"are blocked from routine replenishment until the rule is defined",
               severity="block", rules=_unknown_rules,
               stores=sum(1 for r in store_rule.values() if r.get("unknown")))
    hard_block_data["store_master"] = {
        "columns_present": "sku_rule" in _hb_cols,
        "stores_with_rule": len(store_rule),
        "by_rule": dict(Counter(r["code"] for r in store_rule.values())),
        "cross_dock_stores": sum(1 for s in stores.values() if s.get("cross_dock")),
        "denied_genus_units_in_batch": {g: sum(pools.total[k].get(g, 0) for k in pools.total)
                                        for g in sorted(_sensitive)},
    }

    def _scn_store(a, store_id):
        st_ = stores.get(store_id) or {}
        if a["scope_store_ids"] and store_id not in a["scope_store_ids"]:
            return False
        if a["scope_merchant"] and st_.get("merchant") != a["scope_merchant"]:
            return False
        if a["scope_region"] and st_.get("region") != a["scope_region"]:
            return False
        if a["scope_state"] and st_.get("state") != a["scope_state"]:
            return False
        if a["scope_velocity_tier"] and (st_.get("dynamic_velocity_tier") or "").strip() != a["scope_velocity_tier"]:
            return False
        return True

    def _scn_sku(a, sku_id):
        info = sku_info.get(sku_id) or {}
        if a["scope_sku_id"] and sku_id != a["scope_sku_id"]:
            return False
        if a["scope_family"] and (info.get("family") or "") != a["scope_family"]:
            return False
        if a["scope_legacy_tier"] and (info.get("legacy_product_group") or "") != a["scope_legacy_tier"]:
            return False
        return True

    # Freshness window by family (§4.5, Josh's dump cycle 2026-11-19): ipr_settings
    # freshness_days_by_family {family: days, "_default": 90} + freshness_days_by_sku overrides.
    # A family missing from the map gets the default and a validation flag. Without the map
    # the legacy single freshness_weeks applies to every SKU.
    fmap = S.get("freshness_days_by_family") or {}
    fsku = {int(k): int(v) for k, v in (S.get("freshness_days_by_sku") or {}).items()}
    fdefault = int(fmap.get("_default", 90)) if fmap else fresh_days
    fresh_of, fresh_unmapped = {}, defaultdict(int)
    for sku_id, info in sku_info.items():
        fam = info["ipr_family"] or "Unmapped"
        if sku_id in fsku:
            fresh_of[sku_id] = fsku[sku_id]
        elif fam in fmap:
            fresh_of[sku_id] = int(fmap[fam])
        else:
            fresh_of[sku_id] = fdefault
            if fmap:
                fresh_unmapped[fam] += 1
    for fam, n in sorted(fresh_unmapped.items()):
        decide("validation", f"No freshness window for family '{fam}' — using {fdefault} days",
               severity="warning", family=fam, skus=n)
    fresh_max = max([fresh_days, *fresh_of.values()])
    fresh_arr = (list(fresh_of), [fresh_of[k] for k in fresh_of])

    # ══ 2. WHAT CAN THE STORE SELL? (§7) ═════════════════════════════════
    d = lambda n: anchor - timedelta(days=n)
    # Historical store peak (Frank Paul, 2026-11-20; calibrated): Apr-Jul healthy-season rate per
    # store x SKU. Forecast floor = 0.5 x peak rate x seasonal index (Oct-Nov 0.65) — restores
    # demand on lines starved by short supply (back-test: -44% -> -4% on well-stocked lines where
    # it binds). Months without a calibrated index get no floor.
    pk_win = S.get("peak_window") or ["2026-04-01", "2026-07-31"]
    pk_idx = {int(k): float(v) for k, v in (S.get("peak_seasonal_index") or {"10": 0.65, "11": 0.65}).items()}
    pk_share = float(S.get("peak_floor_share", 0.5) or 0.5)
    pk_starve = str(S.get("peak_floor_require_starvation", False)).lower() in ("true", "1", "yes")
    pk_month = (shipping_week or anchor).month
    peak_rate = {}
    if pk_month in pk_idx:
        cur.execute("""SELECT store_id, sku_id, SUM(units_sold)::float AS u FROM sales
                       WHERE sale_date BETWEEN %s AND %s GROUP BY 1, 2""", tuple(pk_win))
        _wk = ((date.fromisoformat(pk_win[1]) - date.fromisoformat(pk_win[0])).days + 1) / 7.0
        peak_rate = {(r["store_id"], r["sku_id"]): r["u"] / _wk for r in cur.fetchall() if r["u"]}
    peak_mult = pk_share * pk_idx.get(pk_month, 0.0)
    cur.execute("""
        WITH fw AS (SELECT * FROM unnest(%s::bigint[], %s::int[]) AS t(sku_id, days))
        SELECT s.store_id, s.sku_id,
               SUM(units_sold) FILTER (WHERE sale_date > %s)  AS w2,
               SUM(units_sold) FILTER (WHERE sale_date > %s)  AS w4,
               SUM(units_sold) FILTER (WHERE sale_date > %s::date - COALESCE(fw.days, %s)) AS w8,
               SUM(units_sold) FILTER (WHERE sale_date > %s)  AS w10,
               SUM(units_sold) FILTER (WHERE sale_date > %s)  AS w13,
               SUM(dollars)    FILTER (WHERE sale_date > %s)  AS d13,
               SUM(units_sold) FILTER (WHERE sale_date > %s AND dollars IS NOT NULL) AS w13d
        FROM sales s LEFT JOIN fw ON fw.sku_id = s.sku_id
        WHERE sale_date > %s AND sale_date <= %s
        GROUP BY s.store_id, s.sku_id
    """, (*fresh_arr, d(14), d(28), anchor, fresh_days, d(70), d(91), d(91), d(91),
          d(max(91, fresh_max)), anchor))
    sales = {}
    store_recent, store_older = defaultdict(int), defaultdict(int)
    for r in cur.fetchall():
        k = (r["store_id"], r["sku_id"])
        sales[k] = {c: float(r[c] or 0) for c in ("w2", "w4", "w8", "w10", "w13", "d13", "w13d")}
        store_recent[r["store_id"]] += sales[k]["w2"]
        store_older[r["store_id"]] += sales[k]["w13"] - sales[k]["w2"]

    # EDI-gap detection: store sold in weeks 3-13 but reported nothing for 14 days.
    # Missing EDI is NOT zero demand (§3, §5) — demand is held at the 13-wk baseline.
    edi_gap = {s for s in store_older if store_older[s] >= 10 and store_recent.get(s, 0) == 0}
    for s in edi_gap:
        decide("validation", "No sales reported for 14 days at a selling store — suspected EDI gap; "
                             "demand held at 13-wk baseline, not zeroed", store_id=s, severity="warning")

    # Deliveries: last delivery, avg size, conversion cohort, on-hand estimate inputs
    cur.execute("""
        WITH fw AS (SELECT * FROM unnest(%s::bigint[], %s::int[]) AS t(sku_id, days))
        SELECT dl.store_id, dl.sku_id,
               MAX(delivery_date) FILTER (WHERE delivery_date > %s) AS last_del,
               SUM(units_delivered) FILTER (WHERE delivery_date > %s AND delivery_date <= %s) AS cohort,
               SUM(units_delivered) FILTER (WHERE delivery_date > %s::date - COALESCE(fw.days, %s)) AS recent8,
               SUM(units_delivered) FILTER (WHERE delivery_date > %s) AS d26,
               COUNT(DISTINCT delivery_date) FILTER (WHERE delivery_date > %s) AS n_del,
               SUM(units_delivered) FILTER (WHERE delivery_date > %s AND delivery_date <= %s) AS d13
        FROM {dd} dl LEFT JOIN fw ON fw.sku_id = dl.sku_id
        WHERE delivery_date > %s
        GROUP BY dl.store_id, dl.sku_id
    """.format(dd=DEDUP_DELIVERIES), (*fresh_arr, d(182), d(91 + lag_days), d(lag_days), anchor, fresh_days, d(182), d(182),
          d(91), anchor, d(max(182, fresh_max))))
    dels = {(r["store_id"], r["sku_id"]): dict(r) for r in cur.fetchall()}
    # Lot-age sellable credit (calibration 2026-11-20, 4,474 clean delivery cohorts, Apr-Jul 2026
    # + 2025 replicate): only ~60% of delivered units ever sell, and the sellable share left in a
    # lot falls with its age. Each delivery lot counts units x share(age at the sales anchor).
    lot_curve = [tuple(x) for x in (S.get("sellable_share_by_age_days")
                                    or [[0, 0.60], [28, 0.25], [42, 0.14], [56, 0.07], [63, 0.05], [70, 0.0]])]

    def sellable_share(age):
        if age <= lot_curve[0][0]:
            return lot_curve[0][1]
        for (a0, v0), (a1, v1) in zip(lot_curve, lot_curve[1:]):
            if age <= a1:
                return v0 + (v1 - v0) * (age - a0) / (a1 - a0)
        return 0.0
    cur.execute("""SELECT store_id, sku_id, delivery_date, SUM(units_delivered) AS u FROM {dd} dl
                   WHERE delivery_date > %s AND delivery_date <= %s GROUP BY 1, 2, 3""".format(dd=DEDUP_DELIVERIES),
                (d(lot_curve[-1][0]), anchor))
    lot_credit = defaultdict(float)
    for r in cur.fetchall():
        lot_credit[(r["store_id"], r["sku_id"])] += float(r["u"] or 0) * sellable_share((anchor - r["delivery_date"]).days)
    # store-level delivery count (26 wk) for the manifest fallback: a store's average truck carries
    # d26 / store deliveries of a SKU — NOT the SKU's own average drop (which assumed every SKU the
    # store ever received is on every truck and overstated inbound ~5x on the week-48 test).
    cur.execute("""SELECT store_id, COUNT(DISTINCT delivery_date) AS n FROM deliveries
                   WHERE delivery_date > %s GROUP BY store_id""", (d(182),))
    store_ndel = {r["store_id"]: int(r["n"] or 0) for r in cur.fetchall()}

    # Shipping efficiency (Josh's KPI): 13-wk units sold / 13-wk units delivered, same window,
    # no lag, per store x family and network. Reported only; the §11 conversion gate is unchanged.
    eff_sold, eff_del = defaultdict(float), defaultdict(float)
    for (st_id, sk), sv in sales.items():
        if sk in sku_info:
            eff_sold[(st_id, sku_info[sk]["ipr_family"] or "Unmapped")] += sv["w13"]
    for (st_id, sk), dv in dels.items():
        if sk in sku_info:
            eff_del[(st_id, sku_info[sk]["ipr_family"] or "Unmapped")] += float(dv.get("d13") or 0)
    eff = {k: shipping_efficiency(eff_sold.get(k, 0.0), eff_del.get(k, 0.0))
           for k in set(eff_sold) | set(eff_del)}
    store_eff_s, store_eff_d = defaultdict(float), defaultdict(float)
    for (st_id, _f), v in eff_sold.items():
        store_eff_s[st_id] += v
    for (st_id, _f), v in eff_del.items():
        store_eff_d[st_id] += v

    # Last delivery per store = latest of recorded deliveries and loaded in-transit arrivals.
    store_last_del = {}
    for (st_id, _sk), dv in dels.items():
        if dv.get("last_del") and (store_last_del.get(st_id) is None or dv["last_del"] > store_last_del[st_id]):
            store_last_del[st_id] = dv["last_del"]
    cur.execute("SELECT to_regclass('public.in_transit_lines') IS NOT NULL AS ok")
    if cur.fetchone()["ok"]:
        cur.execute("SELECT store_id, MAX(delivery_date) AS mx FROM in_transit_lines GROUP BY store_id")
        for r in cur.fetchall():
            if r["mx"] and (store_last_del.get(r["store_id"]) is None or r["mx"] > store_last_del[r["store_id"]]):
                store_last_del[r["store_id"]] = r["mx"]
    store_best_grade = {}                       # filled after family grades (fallback cadence grade)

    # §8 expected shrink and adjustments, per family at network level over shrink_window_weeks:
    # 1 − (units sold + change in DTS on-hand) ÷ units delivered, clamped 0–0.6. Needs DTS
    # snapshots within 14 days of both window ends; otherwise 1 − shipping efficiency (13 wk).
    # shrink_rate_override {family: rate} wins. Applied to the freshness-window on-hand estimate.
    shrink_wk = int(S.get("shrink_window_weeks") or 26)
    shrink_over = {k: float(v) for k, v in (S.get("shrink_rate_override") or {}).items()}
    w0 = anchor - timedelta(weeks=shrink_wk)
    cur.execute("""
        SELECT COALESCE(fm.ipr_family, 'Unmapped') AS fam,
               (SELECT SUM(units_delivered) FROM {dd} dl WHERE dl.sku_id = ANY(array_agg(k.sku_id))
                  AND dl.delivery_date > %s AND dl.delivery_date <= %s) AS delivered,
               (SELECT SUM(units_sold) FROM sales sa WHERE sa.sku_id = ANY(array_agg(k.sku_id))
                  AND sa.sale_date > %s AND sa.sale_date <= %s) AS sold
        FROM skus k LEFT JOIN ipr_family_map fm ON fm.sku_id = k.sku_id
        GROUP BY 1
    """.format(dd=DEDUP_DELIVERIES), (w0, anchor, w0, anchor))
    shrink_in = {r["fam"]: (float(r["delivered"] or 0), float(r["sold"] or 0)) for r in cur.fetchall()}
    cur.execute("""SELECT MIN(period_end) FILTER (WHERE period_end BETWEEN %s AND %s) AS p0,
                          MAX(period_end) FILTER (WHERE period_end BETWEEN %s AND %s) AS p1
                   FROM dts_weekly_snapshot""",
                (w0 - timedelta(days=14), w0 + timedelta(days=14), anchor - timedelta(days=14), anchor))
    p = cur.fetchone()
    oh_change = {}
    if p["p0"] and p["p1"] and p["p0"] < p["p1"]:
        cur.execute("""SELECT COALESCE(fm.ipr_family, 'Unmapped') AS fam,
                              SUM(ds.onhand_qty) FILTER (WHERE ds.period_end = %s) AS oh0,
                              SUM(ds.onhand_qty) FILTER (WHERE ds.period_end = %s) AS oh1
                       FROM dts_weekly_snapshot ds LEFT JOIN ipr_family_map fm ON fm.sku_id = ds.sku_id
                       WHERE ds.period_end IN (%s, %s) GROUP BY 1""", (p["p0"], p["p1"], p["p0"], p["p1"]))
        oh_change = {r["fam"]: float(r["oh1"] or 0) - float(r["oh0"] or 0) for r in cur.fetchall()}
    fam_eff_s, fam_eff_d = defaultdict(float), defaultdict(float)
    for (_st, f), v in eff_sold.items():
        fam_eff_s[f] += v
    for (_st, f), v in eff_del.items():
        fam_eff_d[f] += v
    shrink, shrink_src = {}, {}
    for f in set(shrink_in) | set(fam_eff_d):
        if f in shrink_over:
            shrink[f], shrink_src[f] = shrink_over[f], "shrink_rate_override"
            continue
        dlv, sold = shrink_in.get(f, (0.0, 0.0))
        rate, src = shrink_rate(dlv, sold, oh_change.get(f) if f in oh_change else None,
                                shipping_efficiency(fam_eff_s.get(f, 0), fam_eff_d.get(f, 0)))
        shrink[f], shrink_src[f] = (rate or 0.0), src
    shrink_summary = {
        "window_weeks": shrink_wk,
        "onhand_snapshots": [str(p["p0"]), str(p["p1"])] if oh_change else None,
        "by_family": {f: {"rate": shrink[f], "source": shrink_src[f],
                          "delivered": round(shrink_in.get(f, (0, 0))[0]),
                          "sold": round(shrink_in.get(f, (0, 0))[1])}
                      for f in sorted(shrink)},
    }
    log(f"Shrink ({shrink_wk} wk, by family): " + ", ".join(f"{f} {shrink[f]:.0%}" for f in sorted(shrink)
                                                         if shrink_in.get(f, (0, 0))[0]))

    # On-hand: DTS snapshot (MEDIUM) when fresh, else deliveries−sales estimate (LOW)
    cur.execute("""
        SELECT DISTINCT ON (store_id, sku_id) store_id, sku_id, onhand_qty, period_end
        FROM dts_weekly_snapshot
        WHERE period_end > %s
        ORDER BY store_id, sku_id, period_end DESC
    """, (d(14),))
    snap = {(r["store_id"], r["sku_id"]): r for r in cur.fetchall()}

    # Inbound (§8: in-transit is supply, not a block), in order of trust:
    #   1. loaded in-transit lines (backend/ingest/load_in_transit_lines.py: what actually
    #      shipped, store x SKU) arriving after the sales anchor;
    #   2. exported prior-run quantities (only for ship weeks with no loaded lines);
    #   3. manifest fallback — store has a manifest delivery after the anchor but no loaded
    #      lines: avg delivery size, flagged LOW.
    cur.execute("SELECT DISTINCT store_id FROM delivery_manifest WHERE delivery_date > %s", (anchor,))
    in_transit_stores = {r["store_id"] for r in cur.fetchall()}
    it_lines, it_weeks = IN_TRANSIT_SOURCE(cur, anchor)   # (store, sku) -> (medium u, low u, methods)
    it_stores = {s for s, _ in it_lines}
    it_low = float(S.get("in_transit_low_factor", 0.5))      # LOW lines (pooled guesses) count at 50%
    cur.execute("""
        SELECT o.store_id, ol.sku_id,
               SUM(COALESCE(ol.approved_qty, ol.draft_qty, ol.recommended_qty)) AS qty
        FROM recommendation_runs r
        JOIN orders o ON o.run_id = r.id
        JOIN order_lines ol ON ol.order_id = o.id
        WHERE r.status IN ('exported','finalized','approved')
          AND r.shipping_week > %s AND r.shipping_week < %s
          AND NOT (r.shipping_week = ANY(%s::date[]))
        GROUP BY o.store_id, ol.sku_id
    """, (anchor - timedelta(days=ARRIVAL_OFFSET_DAYS), shipping_week, sorted(it_weeks)))
    # ^ a run counts as inbound only while its arrival (ship week + 2 days) is after the
    #   sales anchor. The old 7-day window double-counted a run that had already arrived
    #   and entered the delivery/on-hand data (audit 2 N2 — understated need).
    inbound_known = {(r["store_id"], r["sku_id"]): int(r["qty"] or 0) for r in cur.fetchall()}
    manifest_est_stores = set()
    log(f"Inbound after {anchor}: loaded in-transit {sum(m + lo for m, lo, _ in it_lines.values()):,.0f}u "
        f"({sum(lo for _, lo, _ in it_lines.values()):,.0f}u LOW at {it_low:.0%}) "
        f"at {len(it_stores)} stores (weeks {', '.join(str(w) for w in sorted(it_weeks)) or 'none'}) · "
        f"manifest stores without loaded lines: {len(in_transit_stores - it_stores)}")

    # Consecutive minimum-cart pushes per store (latest earlier v2 run per ship week, newest
    # first; a week where the store shipped or was not due ends/skips the streak).
    min_full_carts = int(S.get("min_order_full_carts", 1) or 0)
    min_cart_fill = float(S.get("min_cart_fill_pct", 0.9) or 0.9)
    # Sell-through cover limit: TPC target is 60% sold within 28 days of delivery (Frank Paul,
    # 2026-11-20), so stock after a delivery should not exceed 4 wks / 0.6 = 6.7 wks of forecast.
    max_cover_wk = float(S.get("max_cover_weeks_after_delivery", 4 / 0.6) or 4 / 0.6)
    cur.execute("""
        SELECT DISTINCT ON (shipping_week) id, shipping_week FROM recommendation_runs
        WHERE engine_version = %s AND shipping_week < %s AND COALESCE(status, '') NOT IN %s
        ORDER BY shipping_week DESC, run_at DESC
    """, (ENGINE_VERSION, shipping_week, CANCELLED_RUN_STATUSES))
    _pr = [str(r["id"]) for r in sorted(cur.fetchall(), key=lambda r: r["shipping_week"], reverse=True)[:8]]
    push_streak = {}
    if _pr:
        cur.execute("""SELECT run_id::text AS run_id, store_id, decision_type FROM run_decisions
                       WHERE run_id = ANY(%s::uuid[]) AND store_id IS NOT NULL
                         AND decision_type IN ('pushed_min_cart', 'not_due')""", (_pr,))
        _ph = defaultdict(lambda: [set() for _ in _pr])
        for r in cur.fetchall():
            _ph[r["store_id"]][_pr.index(r["run_id"])].add(r["decision_type"])
        for st_id, weeks in _ph.items():
            n = 0
            for kinds in weeks:
                if "pushed_min_cart" in kinds:
                    n += 1
                elif "not_due" in kinds:
                    continue
                else:
                    break
            if n:
                push_streak[st_id] = n

    # ══ 3+4. PROJECTED INVENTORY AT ARRIVAL + UNCONSTRAINED NEED (§8–10) ════
    def store_cadence(store_id):
        g = cadence_grade((stores.get(store_id) or {}).get("volume_code"),
                          store_best_grade.get(store_id), cadence)
        # measured-response shift (§29, post_delivery_feedback=apply): at most ±1 week, floor 1
        return g, max(1, int(cadence[g]) + pd_adj.get(store_id, 0))

    def size_line(l):
        """§10 need for one line: coverage = store cadence + buffer weeks (capped by the family
        freshness window), then the §17 H2O/Terrarium cap and the §5 unreliable-inventory cap."""
        cg, cw = store_cadence(l["store_id"])
        # Minimum-order floor (meeting 2026-11-20): a store pushed for not filling one full CC
        # cart waits longer, so its coverage grows one week per consecutive push (still capped
        # by the freshness window) — need accumulates to a full cart from forecast, never padded.
        cov = coverage_weeks(cw + push_streak.get(l["store_id"], 0), cad_buffer,
                             fresh_of.get(l["sku_id"], fresh_days))
        if l.get("cov_extra"):
            # fill-to-cart extension: forecast demand over a longer window, capped by freshness
            # and by the sell-through cover limit (stock after delivery <= max_cover_weeks of fc)
            cov = min(cov + l["cov_extra"], max(cov, max_cover_wk),
                      fresh_of.get(l["sku_id"], fresh_days) / 7.0)
        need = max(0.0, l["fc"] * cov - l["projected"])
        cap_note = None
        if not l["plannable"]:
            trays = 0                                                     # objective-only line
        elif l["family"] in H2O_TERR_FAMILIES:                           # §17 tray control
            win = float(S["h2o_terrarium_window_weeks"])
            cap = max(0.0, l["fc"] * win - l["oh_adj"] - l["inbound"])
            if need > cap:
                need, cap_note = cap, f"capped at {int(win)}-wk demand ({cap:.0f}u)"
            trays = int(need // l["upt"])                                 # never exceed: round down
        else:
            trays = int(math.floor(need / l["upt"] + 0.5))                # nearest valid tray
        # approved scenario cap/floor (pre-bounded to pace at line creation; survives every
        # re-size). Floor runs BEFORE the §5 unreliable-inventory cap so data safety wins.
        if l.get("scn_cap_trays") is not None and trays > l["scn_cap_trays"]:
            trays = l["scn_cap_trays"]
            cap_note = (cap_note or "") + " scenario cap"
        if l.get("scn_floor_trays") and trays < l["scn_floor_trays"]:
            trays = l["scn_floor_trays"]
            cap_note = (cap_note or "") + " scenario floor (pace-bounded)"
        if l["conf"] == "UNUSABLE" and trays:
            trays = min(trays, max(1, int((l["fc"] * 2) // l["upt"])))    # §5: cap / review
            cap_note = (cap_note or "") + " inventory unreliable — capped at 2 wks"
        l.update(need=need, trays=trays, cap_note=cap_note, cover_wk=cov,
                 cadence_grade=cg, cadence_weeks=cw)

    lines = {}               # (store, sku) -> evaluation record
    # Every store x SKU with velocity is evaluated (the §2 objective covers the whole HD
    # program); only plannable SKUs (in this availability + packable + mapped) get trays.
    # Peak-floor seeding: a store x SKU that sold in Apr-Jul but NOT in the last 13 weeks has no
    # sales row, so the §9 loop would never see it — the "self-fulfilling prophecy" (Frank Paul,
    # 2026-11-20): starved lines vanish from the forecast. Seed them at zero recent sales and let
    # the peak floor set fc; every §11/§24 gate still applies. ($84K/wk of Apr-Jul sales had no
    # engine line on the 11/20 audit.)
    if peak_rate and peak_mult > 0:
        _seeded = 0
        for _k, _pk in peak_rate.items():
            if _k not in sales and _pk * peak_mult >= min_rate:
                sales[_k] = {"w2": 0.0, "w4": 0.0, "w8": 0.0, "w10": 0.0, "w13": 0.0,
                             "d13": 0.0, "w13d": 0.0}
                _seeded += 1
        log(f"Peak-floor seeding: {_seeded:,} store x SKU lines with Apr-Jul sales but none in 13 wks")
    for (store_id, sku_id), sv in sales.items():
        if sku_id not in sku_info:
            continue
        is_plannable = sku_id in plannable
        st = stores.get(store_id)
        if not st:
            continue
        r2, r4, r10, r13 = sv["w2"] / 2, sv["w4"] / 4, sv["w10"] / 10, sv["w13"] / 13
        fc = fw["w4"] * r4 + fw["w10"] * r10 + fw["w13"] * r13
        dv = dels.get((store_id, sku_id), {})
        last_del = dv.get("last_del")
        method = "blend 4/10/13"
        # Low recent sales must not become low demand when the store likely lacked product (§7)
        if r4 < 0.5 * r13 and (last_del is None or (anchor - last_del).days > 42):
            fc, method = r13, "13-wk baseline (likely stocked out — low recent sales not trusted)"
        if store_id in edi_gap:
            fc, method = r13, "13-wk baseline (EDI gap)"
        pf = peak_rate.get((store_id, sku_id), 0.0) * peak_mult
        # Starvation evidence option (§7: the exception is for stores that LIKELY LACKED
        # INVENTORY): with peak_floor_require_starvation=true the floor binds only when 13-wk
        # deliveries could not have stocked floor-rate demand. Default false = 11/20 calibration.
        if pf > fc and (not pk_starve or float(dv.get("d13") or 0) < pf * 13):
            fc, method = pf, (f"Apr-Jul peak floor ({peak_rate[(store_id, sku_id)]:.1f}u/wk x "
                              f"{peak_mult:.2f}) — recent sales held down by short supply")
        # approved scenario demand multipliers (clamped; the parser never touches the engine —
        # this deterministic application is the only path, and every hit is counted for audit)
        for _a in scn_mult:
            if _scn_store(_a, store_id) and _scn_sku(_a, sku_id):
                _m = max(0.0, min(scn_max_mult,
                                  float(_a["value_numeric"]) if _a["value_numeric"] is not None else 1.0))
                if _m != 1.0:
                    fc *= _m
                    method = f"{method} × scenario '{_a['label']}' ({_m:g}x)"
                    scn_fx[_a["adj_id"]]["lines"] += 1
                    scn_fx[_a["adj_id"]]["stores"].add(store_id)
        if fc < min_rate:
            continue                        # no velocity → not in scope (Josh: only ship velocity)

        # Sellable on-hand + confidence (§4.6, §5). DTS reported on-hand is NOT sellable
        # on-hand: dumps are booked in bulk cleanups (2026-11-17 audit: 0 dumps in 6 of 7
        # weeks, then 357K in one week; network on-hand = 16 weeks of supply). Sellable is
        # bounded by freshness (§4.5): only units delivered within the freshness window,
        # less units sold since, can still be on the floor. Take the smaller of the two.
        fam_s = sku_info[sku_id]["ipr_family"] or "Unmapped"
        shr = shrink.get(fam_s, 0.0)
        fresh_est = max(0.0, float(dv.get("recent8") or 0) * (1.0 - shr) - sv["w8"])
        shr_txt = f"; shrink {shr:.0%} ({fam_s})"
        lc = lot_credit.get((store_id, sku_id), 0.0)
        lot_bound = lc < fresh_est          # lot-age credit is the tighter, evidence-based bound
        if lot_bound:
            fresh_est = lc
            shr_txt = "; lot-age sellable credit (units x sellable share by age, ~60% lifetime)"
        sn = snap.get((store_id, sku_id))
        if sn is not None and sn["onhand_qty"] is not None and float(sn["onhand_qty"]) < 0:
            oh, conf, oh_src = 0.0, "UNUSABLE", f"DTS snapshot {sn['period_end']} negative — implausible"
        elif sn is not None and sn["onhand_qty"] is not None:
            rep = float(sn["onhand_qty"])
            oh = min(rep, fresh_est)
            conf, oh_src = "LOW", (f"min(DTS {rep:.0f}u, fresh {fresh_est:.0f}u) — "
                                   f"DTS dumps batch-recorded, freshness-capped{shr_txt}")
        else:
            oh, conf, oh_src = fresh_est, "LOW", (f"estimate: {fresh_of.get(sku_id, fresh_days)}-day "
                                                  f"deliveries × (1 − shrink) − sales{shr_txt}")
        # the freshness cap is already the conservative bound — no second haircut on top.
        # When the calibrated lot-age credit set the bound, the LOW confidence factor would
        # be a third stacked haircut (shrink x lot share x 0.70) — skip it (audit 2 N3).
        oh_adj = 0.0 if conf == "UNUSABLE" else max(0.0, oh) * (
            1.0 if (sn is not None or lot_bound) else float(conf_factor[conf]))

        if store_id in it_stores:
            med, low, how = it_lines.get((store_id, sku_id), (0.0, 0.0, None))
            inbound = med + low * it_low
            inbound_src = (f"in transit (loaded, {how})" if how
                           else "in transit (loaded) — SKU not on the truck")
            if low:
                inbound_src += (f" · LOW confidence {low:.0f}u counted at {it_low:.0%} "
                                f"({low * it_low:.0f}u) — store assignment estimated")
        else:
            inbound, inbound_src = inbound_known.get((store_id, sku_id), 0), "prior exported run"
            if not inbound and store_id in in_transit_stores and dv.get("n_del"):
                inbound = (float(dv.get("d26") or 0) / max(1, store_ndel.get(store_id, 0), int(dv["n_del"]))
                           * it_low)
                inbound_src = (f"in transit (manifest) — SKU share of the store's avg truck, counted at "
                               f"{it_low:.0%} · LOW")
                if store_id not in manifest_est_stores:
                    manifest_est_stores.add(store_id)
                    decide("validation", "Manifest shows a delivery after the sales anchor but no "
                                         "in-transit lines are loaded — inbound estimated from avg "
                                         "delivery size (LOW)", store_id=store_id, severity="warning")

        before_arrival = fc / 7.0 * days_to_arrival
        dup = same_week_prior.get((store_id, sku_id), 0)        # duplicate-run control (§24)
        if dup:
            inbound_src = (f"{inbound_src} + {dup}u earlier run, same ship week" if inbound
                           else f"{dup}u earlier run, same ship week (duplicate-run control)")
            inbound += dup
        projected =max(0.0, oh_adj + inbound - before_arrival)
        cover_days = projected / (fc / 7.0) if fc else 999

        if conf == "UNUSABLE" or store_id in edi_gap:
            risk = "Unknown"
        elif oh_adj + inbound < before_arrival:
            # §9: UNRELIABLE on-hand cannot create Critical risk — but confidence was LOW
            # everywhere, so Critical was unreachable and the §8 critical-due bypass was dead
            # code (audit 2 N1). When the delivery arithmetic itself says nothing sellable
            # remains (oh == 0 from evidence) and nothing is inbound, the emptiness is
            # evidence-backed, not a confidence artifact: that line is Critical.
            risk = "Critical" if (oh <= 0 and inbound <= 0) else (
                "High" if conf == "LOW" else "Critical")
        elif cover_days < 7:
            risk = "High"
        elif cover_days < 14:
            risk = "Moderate"
        else:
            risk = "Low"

        fam = sku_info[sku_id]["ipr_family"] or "Unmapped"
        pk = pack.get(sku_id) or {}
        upt = int(pk.get("units_per_tray") or 1)
        realized = (sv["d13"] / sv["w13d"]) if sv["w13d"] else None
        unit_price, price_src = price(sku_id, realized)
        lines[(store_id, sku_id)] = dict(
            store_id=store_id, sku_id=sku_id, family=fam, fc=fc, method=method,
            r2=r2, r4=r4, r10=r10, r13=r13, onhand=oh, conf=conf, oh_src=oh_src, oh_adj=oh_adj,
            inbound=inbound, inbound_src=inbound_src, before_arrival=before_arrival,
            projected=projected, cover_days=cover_days, risk=risk, need=0.0, upt=upt,
            trays=0, cap_note=None, price=unit_price, price_src=price_src,
            last_del=last_del, sales13=sv["w13"], cohort=float(dv.get("cohort") or 0),
            plannable=is_plannable)
        # scenario floor/cap, computed ONCE here (value_numeric is SHELVES) and pre-bounded to
        # the store's selling pace — §17's 13-week window for H2O/Terrarium, the sell-through
        # cover limit otherwise. "Add 2 shelves of bowls in the South" never ships more than
        # the store's pace supports; the trim is counted and audited (scenario_applied).
        l_ = lines[(store_id, sku_id)]
        tps = max(1, int(pk.get("trays_per_shelf") or 1))
        for _a in scn_cap:
            if _scn_store(_a, store_id) and _scn_sku(_a, sku_id):
                _c = int(float(_a["value_numeric"] or 0) * tps)
                if l_.get("scn_cap_trays") is None or _c < l_["scn_cap_trays"]:
                    l_["scn_cap_trays"] = _c
                scn_fx[_a["adj_id"]]["lines"] += 1
                scn_fx[_a["adj_id"]]["stores"].add(store_id)
        for _a in scn_floor:
            if is_plannable and _scn_store(_a, store_id) and _scn_sku(_a, sku_id):
                req_t = int(float(_a["value_numeric"] or 0) * tps)
                if fam in H2O_TERR_FAMILIES:
                    _win = float(S["h2o_terrarium_window_weeks"])
                    hard = int(max(0.0, fc * _win - oh_adj - inbound) // upt)
                else:
                    hard = int((fc * max_cover_wk) // upt)
                grant = max(0, min(req_t, hard))
                scn_fx[_a["adj_id"]]["trays_requested"] += req_t
                scn_fx[_a["adj_id"]]["trays_granted"] += grant
                if grant < req_t:
                    scn_fx[_a["adj_id"]]["trays_trimmed"] += req_t - grant
                if grant > (l_.get("scn_floor_trays") or 0):
                    l_["scn_floor_trays"] = grant
                scn_fx[_a["adj_id"]]["lines"] += 1
                scn_fx[_a["adj_id"]]["stores"].add(store_id)
        size_line(lines[(store_id, sku_id)])
    log(f"Store x SKU lines with velocity: {len(lines):,} (all SKUs, for the objective) · "
        f"plannable: {sum(1 for l in lines.values() if l['plannable']):,} · with need: "
        f"{sum(1 for l in lines.values() if l['trays'] > 0):,}")

    # ══ 5. FAMILY GRADES + MATURE-DELIVERY CONVERSION GATES (§6, §11, §24) ═══
    fam_vel, fam_sales, fam_cohort = defaultdict(float), defaultdict(float), defaultdict(float)
    fam_lastdel = defaultdict(lambda: None)
    for l in lines.values():
        k = (l["store_id"], l["family"])
        fam_vel[k] += l["fc"]
        fam_sales[k] += l["sales13"]
        fam_cohort[k] += l["cohort"]
        if l["last_del"] and (fam_lastdel[k] is None or l["last_del"] > fam_lastdel[k]):
            fam_lastdel[k] = l["last_del"]
    by_family = defaultdict(list)
    for (s, f), v in fam_vel.items():
        by_family[f].append((v, s))
    grade = {}
    for f, lst in by_family.items():
        lst.sort(key=lambda x: (-x[0], x[1]))
        n = len(lst)
        for i, (v, s) in enumerate(lst):
            pct = (i + 1) / n * 100
            grade[(s, f)] = next(g for g in GRADE_ORDER if pct <= float(cutoffs[g]))

    for (s_, f_), g_ in grade.items():
        if s_ not in store_best_grade or GRADE_ORDER.index(g_) < GRADE_ORDER.index(store_best_grade[s_]):
            store_best_grade[s_] = g_
    for l in lines.values():                    # cadence fallback: stores with no volume grade
        if (stores.get(l["store_id"]) or {}).get("volume_code") not in cadence:
            size_line(l)

    # Delivery cadence (§10, §19.1 service urgency): a store is DUE when it has no prior
    # delivery, the gap from its last delivery (incl. loaded in-transit) to this arrival is
    # >= cadence x 7 - 3 days, or any line is Critical risk. Not due -> no order this week.
    store_due = {}
    crit_stores = {l["store_id"] for l in lines.values() if l["risk"] == "Critical"}
    for st_id in {l["store_id"] for l in lines.values() if l["trays"] > 0}:
        cg, cw = store_cadence(st_id)
        due, nxt = due_status(store_last_del.get(st_id), arrival, cw, st_id in crit_stores)
        store_due[st_id] = (due, cg, cw, nxt)
    for st_id in sorted(set(pd_adj) & set(store_due)):
        decide("response_adjustment",
               f"Delivery cadence {'shortened' if pd_adj[st_id] < 0 else 'lengthened'} one week "
               f"from the measured 7–28 day shipment response (§29, post_delivery_feedback=apply)",
               store_id=st_id, severity="info", adjustment_weeks=pd_adj[st_id])

    # FILL TO ONE FULL CC CART (leadership minimum, 2026-11-20). A due store whose cadence-sized
    # order is below one full cart gets its coverage extended a week at a time — forecast demand
    # over a longer window, so its next delivery simply comes later — until the order fills a cart
    # or every line hits its cap (freshness window / sell-through cover limit). Never adds trays
    # beyond forecast. Stores that still cannot fill a cart are pushed by the floor below.
    def shelf_equiv(ls):
        by = defaultdict(float)
        for x in ls:
            if x["trays"] > 0 and x["plannable"]:
                pk = pack[x["sku_id"]]
                by[pk["profile_id"]] += x["trays"] / max(1, int(pk["trays_per_shelf"]))
        return sum(by.values())
    fill_target = min_cart_fill * CART_SHELVES * max(1, min_full_carts)
    by_store = defaultdict(list)
    for l in lines.values():
        if l["plannable"]:
            by_store[l["store_id"]].append(l)
    extended = {}
    for st_id, ls in by_store.items():
        if min_full_carts <= 0:
            continue
        if st_id in store_due:
            is_due = store_due[st_id][0]
        else:                                    # no cadence-sized trays: due by cadence alone?
            is_due = due_status(store_last_del.get(st_id), arrival, store_cadence(st_id)[1],
                                st_id in crit_stores)[0]
        if not is_due:
            continue
        if shelf_equiv(ls) >= fill_target:
            continue
        for extra in range(1, 13):
            for x in ls:
                x["cov_extra"] = extra
                size_line(x)
            if shelf_equiv(ls) >= fill_target or all(
                    x["cover_wk"] < 1 or x["cover_wk"] >= min(max_cover_wk, fresh_of.get(x["sku_id"], fresh_days) / 7.0) - 1e-9
                    for x in ls):
                break
        if shelf_equiv(ls) >= fill_target:
            extended[st_id] = extra
        else:                                   # cannot fill a cart inside the caps: revert
            for x in ls:
                x["cov_extra"] = 0
                size_line(x)
    for st_id in {l["store_id"] for l in lines.values() if l["trays"] > 0} - set(store_due):
        cg, cw = store_cadence(st_id)            # stores that only gained trays via extension
        due, nxt = due_status(store_last_del.get(st_id), arrival, cw, st_id in crit_stores)
        store_due[st_id] = (due, cg, cw, nxt)
    log(f"Fill-to-cart: {len(extended):,} due stores extended to fill one CC cart "
        f"(avg +{(sum(extended.values()) / len(extended)) if extended else 0:.1f} wk coverage)")

    service = {}            # (store, family) -> (status, conversion, confidence)
    for k in fam_vel:
        store_id, f = k
        g = grade[k]
        coh = fam_cohort[k]
        if coh < 6:          # < ~1 tray delivered in the mature window: nothing to judge
            service[k] = ("ELIGIBLE", None, "no mature delivery cohort")
            continue
        conv = fam_sales[k] / coh
        confd = "low (prior inventory likely)" if conv > 1.2 else "normal"
        thr = float(conv_thr[g])
        if conv < hard_block:
            status = "BLOCKED"
        elif conv < thr:
            status = "REVIEW"
        else:
            status = "ELIGIBLE"
        service[k] = (status, round(conv, 3), confd)

    # Approved, unexpired reactivations (§11.3, §27): a manager-approved override that sits
    # outside the normal autonomous flow; the family ships as a controlled test quantity.
    cur.execute("""SELECT id, store_id, ipr_family, test_units FROM ipr_reactivations
                   WHERE status = 'approved' AND expires_at >= %s""", (today,))
    react = {(r["store_id"], r["ipr_family"]): {"id": str(r["id"]), "left": int(r["test_units"])}
             for r in cur.fetchall()}

    # Gate every line (§24): store status, family 13-week result (§11.3). Autonomous — no
    # routine approvals. Below the 60% floor the family is stopped this week; between 60% and
    # the grade threshold it ships a grade-reduced quantity (never an identical shipment).
    ship, held, held_kind, not_due_lines = {}, {}, {}, {}
    blocked_units = stopped_units = reduced_units = rule_blocked_units = 0.0
    for key in sorted(lines, key=lambda k: (k[0], -lines[k]["fc"], k[1])):
        l = lines[key]
        if l["trays"] <= 0:
            continue                                            # existing/inbound covers demand
        st = stores[l["store_id"]]
        status_s = (st.get("store_status") or "active")
        fk = (l["store_id"], l["family"])
        g = grade[fk]
        svc, conv, confd = service[fk]
        l.update(grade=g, service=svc, conversion=conv, conv_conf=confd, reactivation=None,
                 trays_unreduced=l["trays"])
        if not st.get("active") or status_s in BLOCK_STATUSES:
            held[key], held_kind[key] = "store inactive / do-not-ship", "block"
            blocked_units += l["trays"] * l["upt"]
            continue
        # Store master handling rule (hard block): no routine replenishment at all, or only
        # the listed products. Variety (genus) restrictions are enforced at allocation.
        _rule = store_rule.get(l["store_id"])
        if _rule:
            _ok, _why = store_rules.sku_allowed(_rule, sku_info.get(l["sku_id"]))
            if not _ok:
                held[key] = _why
                held_kind[key] = ("store_rule_block" if store_rules.routine_blocked(_rule)
                                  else "sku_restriction_block")
                blocked_units += l["trays"] * l["upt"]
                rule_blocked_units += l["trays"] * l["upt"]
                continue
            _deny = store_deny.get(l["store_id"])
            if _deny and pools.supply(l["sku_id"], _deny) <= 0 < pools.supply(l["sku_id"]):
                held[key] = (f"every available variety of this SKU is "
                             f"{'/'.join(sorted(g.title() for g in _deny))} — not shipped "
                             f"({_rule.get('label')})")
                held_kind[key] = "variety_restriction_block"
                blocked_units += l["trays"] * l["upt"]
                rule_blocked_units += l["trays"] * l["upt"]
                continue
        # Approved assortment (§1 hard block, §4.1, §24): when the store master lists approved
        # families, a family outside the list never ships. NULL/empty = no restriction on file.
        asst = st.get("approved_assortment")
        if isinstance(asst, (list, tuple)) and asst and l["family"] not in asst:
            held[key] = f"{l['family']} is not in the store's approved assortment (§4.1, §24)"
            held_kind[key] = "assortment_block"
            blocked_units += l["trays"] * l["upt"]
            continue
        # approved scenario exclusions (incl. cart_target_override 0 = store removed this week)
        _ex = next((a for a in scn_excl
                    if _scn_store(a, l["store_id"]) and _scn_sku(a, l["sku_id"])), None)
        if _ex:
            held[key] = (f"excluded by approved scenario '{_ex['label']}'"
                         + (f" (approved by {_ex['approved_by']})" if _ex.get("approved_by") else ""))
            held_kind[key] = "scenario_exclusion"
            scn_fx[_ex["adj_id"]]["lines"] += 1
            scn_fx[_ex["adj_id"]]["stores"].add(l["store_id"])
            blocked_units += l["trays"] * l["upt"]
            continue
        if not store_due[l["store_id"]][0]:
            not_due_lines[key] = l
            continue
        if svc in ("BLOCKED", "REVIEW") and fk in react:
            r = react[fk]
            allowed = min(l["trays"], r["left"] // l["upt"])
            if allowed > 0:
                r["left"] -= allowed * l["upt"]
                l["trays"], l["reactivation"] = allowed, r["id"]
                decide("reactivation", f"{l['family']} reactivated (approved override test) — {allowed} "
                                       f"trays within the approved test quantity", store_id=l["store_id"],
                       sku_id=l["sku_id"], reactivation_id=r["id"], conversion=conv, grade=g)
                ship[key] = l
                continue
        if svc == "BLOCKED":
            held[key] = (f"{l['family']} conversion {conv:.0%} < {hard_block:.0%} after 13 wks — "
                         f"service stopped this week (§11.3); need recalculated next week")
            held_kind[key] = "stopped_13wk"
            stopped_units += l["trays"] * l["upt"]
            continue
        if svc == "REVIEW":
            pct = REVIEW_BAND_SHIP_PCT[g]
            new_trays = max(1, int(math.floor(l["trays"] * pct)))
            removed = l["trays"] - new_trays
            reduced_units += removed * l["upt"]
            decide("reduced_13wk", f"{l['family']} conversion {conv:.0%} below {g} threshold "
                                   f"{float(conv_thr[g]):.0%} (>= {hard_block:.0%}) — ships {pct:.0%} of "
                                   f"need: {new_trays} of {l['trays']} trays (§11.3 reduce quantity)",
                   store_id=l["store_id"], sku_id=l["sku_id"], family=l["family"], grade=g,
                   conversion=conv, threshold=float(conv_thr[g]), ship_pct=pct,
                   need_trays=l["trays"], ship_trays=new_trays, upt=l["upt"])
            l["trays"], l["reduced_13wk"] = new_trays, pct
        ship[key] = l

    nd_by_store = defaultdict(list)
    for k, l in not_due_lines.items():
        nd_by_store[k[0]].append(l)
    for st_id, ls in sorted(nd_by_store.items()):
        due, cg, cw, nxt = store_due[st_id]
        last = store_last_del.get(st_id)
        decide("not_due", f"Not due: cadence {cw} wk ({cg}); last delivery {last}; next due arrival "
                          f"{nxt} — no order this week, need recalculated next week",
               store_id=st_id, cadence_grade=cg, cadence_weeks=cw, last_delivery=last,
               next_due=nxt, arrival=arrival, lines=len(ls),
               need_units=sum(l["trays"] * l["upt"] for l in ls),
               shipping_efficiency=shipping_efficiency(store_eff_s.get(st_id, 0), store_eff_d.get(st_id, 0)))
    RULE_KINDS = ("store_rule_block", "sku_restriction_block", "variety_restriction_block")
    _rule_agg = defaultdict(lambda: {"lines": 0, "units": 0, "families": Counter(), "why": None})
    for key, why in held.items():
        l = lines[key]
        if held_kind[key] in RULE_KINDS:            # one decision per store, not per line
            a_ = _rule_agg[(held_kind[key], l["store_id"])]
            a_["lines"] += 1
            a_["units"] += l["trays"] * l["upt"]
            a_["families"][l["family"]] += l["trays"] * l["upt"]
            a_["why"] = a_["why"] or why
            continue
        decide(held_kind[key], why, store_id=l["store_id"], sku_id=l["sku_id"],
               family=l["family"], grade=l.get("grade"), conversion=l.get("conversion"),
               proposed_trays=l["trays"], proposed_units=l["trays"] * l["upt"])
    for (kind_, st_id), a_ in sorted(_rule_agg.items()):
        _r = store_rule.get(st_id) or {}
        _st = stores.get(st_id) or {}
        if kind_ == "store_rule_block":
            msg = (f"No routine order: store master rule '{_r.get('label')}'"
                   + (f" ({_st.get('master_status')})" if _st.get("master_status") else "")
                   + f" — {a_['units']:,}u of calculated need across {a_['lines']} line(s) not shipped")
        elif kind_ == "sku_restriction_block":
            msg = (f"{a_['lines']} line(s) / {a_['units']:,}u outside the store's allowed products not "
                   f"shipped — store master rule '{_r.get('label')}'")
        else:
            msg = (f"{a_['lines']} line(s) / {a_['units']:,}u not shipped: only restricted varieties "
                   f"are available — store master rule '{_r.get('label')}'")
        decide(kind_, msg, store_id=st_id, severity="info", rule=_r.get("code"),
               rule_label=_r.get("label"), master_status=_st.get("master_status"),
               lines=a_["lines"], proposed_units=a_["units"],
               by_family=dict(a_["families"].most_common()))
    n_block = sum(1 for k in held_kind.values() if k == "block")
    n_stop = sum(1 for k in held_kind.values() if k == "stopped_13wk")
    n_reduced = sum(1 for l in ship.values() if l.get("reduced_13wk") is not None)
    log(f"Gates: {len(ship):,} lines eligible ({n_reduced:,} reduced, 13-wk band) · {n_block:,} blocked "
        f"(store status) · {n_stop:,} stopped (13-wk < {hard_block:.0%})")

    # No fixed minimum-drop deferral (removed for the v2 live run): every store with justified
    # need ships its need; small orders surface as low-value carts (§19.1), never padded.

    # ══ 6. ALLOCATE AVAILABILITY (§12, §13, §15) — only after need exists ═══
    if ship:
        inc = _norm({k: l["trays"] * l["upt"] * usd(l) for k, l in ship.items()})
        fv = _norm({k: fam_vel[(l["store_id"], l["family"])] for k, l in ship.items()})
        rec = {k: min(1.0, ((anchor - fam_lastdel[(l["store_id"], l["family"])]).days / 84.0)
                      if fam_lastdel[(l["store_id"], l["family"])] else 1.0) for k, l in ship.items()}
        store_max_fc = defaultdict(float)
        for l in ship.values():
            store_max_fc[l["store_id"]] = max(store_max_fc[l["store_id"]], l["fc"])
    for k, l in ship.items():
        conv01 = 0.5 if l["conversion"] is None else min(1.0, l["conversion"])
        l["priority"] = (sp_w["incremental_sales"] * inc[k] + sp_w["family_velocity"] * fv[k]
                         + sp_w["mature_delivery_sales"] * conv01 + sp_w["stockout_risk"] * RISK_SCORE[l["risk"]]
                         + sp_w["seasonal"] * 0.0 + sp_w["recency"] * rec[k])
        l["sku_fit"] = (sf_w["sku_velocity"] * (l["fc"] / store_max_fc[l["store_id"]])
                        + sf_w["mature_delivery_sales"] * conv01
                        + sf_w["projected_need"] * min(1.0, l["need"] / max(1e-9, l["fc"] * l["cover_wk"]))
                        + sf_w["seasonal"] * 0.0 + sf_w["comparable_fit"] * GRADE_SCORE[l["grade"]])
        for _a in scn_boost:                 # approved scenario priority boosts, clamped
            if _scn_store(_a, l["store_id"]) and _scn_sku(_a, l["sku_id"]):
                l["priority"] += max(0.0, min(scn_boost_cap, float(_a["value_numeric"] or 0)))
                scn_fx[_a["adj_id"]]["lines"] += 1
                scn_fx[_a["adj_id"]]["stores"].add(l["store_id"])

    by_sku = defaultdict(list)
    for k, l in ship.items():
        by_sku[l["sku_id"]].append(l)
    unfilled = defaultdict(float)        # sku -> units of need not covered by supply
    sku_left = {}                        # sku -> supply left after priority allocation
    alloc_units_total = 0
    variety_short = defaultdict(lambda: {"units": 0, "skus": set()})   # store -> restricted shortfall
    for sku_id, lst in by_sku.items():
        want = sum(l["trays"] * l["upt"] for l in lst)
        lst.sort(key=lambda x: (-x["priority"], x["store_id"]))
        for l in lst:
            # a store with a variety restriction draws only from the varieties it may receive
            deny = store_deny.get(l["store_id"], NO_DENY)
            can = min(l["trays"], pools.left(sku_id, deny) // l["upt"])
            l["alloc_trays"] = int(can)
            l["alloc_units"] = int(can) * l["upt"]
            pools.take(sku_id, l["alloc_units"], deny)
            if can < l["trays"]:
                short = (l["trays"] - can) * l["upt"]
                unfilled[sku_id] += short
                l["short_units"] = short
                if deny and pools.left(sku_id) >= l["upt"]:
                    # stock exists, but only in a variety this store may not receive
                    variety_short[l["store_id"]]["units"] += min(
                        short, (pools.left(sku_id) // l["upt"]) * l["upt"])
                    variety_short[l["store_id"]]["skus"].add(sku_id)
        remaining = pools.left(sku_id)
        alloc_units_total += want - unfilled[sku_id]
        if unfilled[sku_id]:
            decide("allocation_shortfall",
                   f"Need {want:,}u vs supply {supply.get(sku_id, 0):,}u — {int(unfilled[sku_id]):,}u unfilled; "
                   f"allocated by store priority (§12)", sku_id=sku_id,
                   need_units=want, supply_units=supply.get(sku_id, 0), unfilled_units=int(unfilled[sku_id]))
        sku_left[sku_id] = remaining

    # ══ 6b. ORDER-GAP FILLING (§21, simple form) — after allocation, priority order unchanged ══
    # Where a store x family has unfilled need, give extra trays to OTHER SKUs in the same
    # store x family that have positive own need (passed every gate) and supply left, ranked by
    # that store's forecast. Guards: the store x family total never exceeds the family's eligible
    # need; a substitute gets at most its own need again (selling-window guard); H2O/Terrarium
    # (§17 tray caps), approved test quantities and unreliable-inventory lines are not used.
    fam_lines = defaultdict(list)
    for l in ship.values():
        fam_lines[(l["store_id"], l["family"])].append(l)
    gaps = []
    for fk, fl in fam_lines.items():
        short = [l for l in fl if l.get("short_units")]
        if short and fk[1] not in H2O_TERR_FAMILIES:
            gaps.append((-max(l["priority"] for l in short), fk))
    gap_fill_units = gap_fill_trays = 0
    gap_fills = 0
    for _p, fk in sorted(gaps):
        fl = fam_lines[fk]
        fam_need = sum(l["trays"] * l["upt"] for l in fl)
        fam_alloc = sum(l["alloc_units"] for l in fl)
        short = sorted((l for l in fl if l.get("short_units")), key=lambda x: (-x["short_units"], x["sku_id"]))
        gdeny = store_deny.get(fk[0], NO_DENY)       # substitutes obey the variety restriction too
        cands = sorted((l for l in fl if not l.get("short_units") and not l.get("reactivation")
                        and l["conf"] != "UNUSABLE" and l["need"] > 0
                        and pools.left(l["sku_id"], gdeny) > 0),
                       key=lambda x: (-x["fc"], x["sku_id"]))
        for c in cands:
            if fam_need - fam_alloc < c["upt"]:
                continue
            add = min(c["trays"] - c.get("gap_fill_trays", 0),           # selling-window guard
                      pools.left(c["sku_id"], gdeny) // c["upt"],
                      (fam_need - fam_alloc) // c["upt"])
            if add <= 0:
                continue
            u = add * c["upt"]
            c["alloc_trays"] += add
            c["alloc_units"] += u
            c["gap_fill_trays"] = c.get("gap_fill_trays", 0) + add
            pools.take(c["sku_id"], u, gdeny)
            sku_left[c["sku_id"]] = pools.left(c["sku_id"])
            fam_alloc += u
            gap_fill_units += u
            gap_fill_trays += add
            gap_fills += 1
            orig = short[0]
            decide("gap_fill", f"{fk[1]}: {orig['short_units']}u of {orig['sku_id']} unavailable — "
                               f"+{add} trays ({u}u) of {c['sku_id']} (forecast {c['fc']:.1f}/wk, own need "
                               f"{c['trays']} trays); family total stays within need (§21)",
                   store_id=fk[0], sku_id=c["sku_id"], family=fk[1], original_sku=orig["sku_id"],
                   original_skus=[l["sku_id"] for l in short], substitute_sku=c["sku_id"], trays=add,
                   units=u, substitute_forecast_wk=round(c["fc"], 2), family_need_units=fam_need,
                   family_alloc_units=fam_alloc,
                   basis="proven SKU in the same store x family with positive own need and supply left")
    alloc_units_total += gap_fill_units
    if gaps:
        log(f"Gap fill: {gap_fills:,} fills · {gap_fill_trays:,} trays · {gap_fill_units:,}u across "
            f"{len(gaps):,} short store x families")

    for st_id, v in sorted(variety_short.items()):
        _r = store_rule.get(st_id) or {}
        decide("variety_restriction", f"{v['units']:,}u short on {len(v['skus'])} SKU(s): stock is "
                                      f"available only in a variety this store may not receive "
                                      f"(store master rule '{_r.get('label')}')",
               store_id=st_id, severity="info", rule=_r.get("code"), units=v["units"],
               skus=sorted(v["skus"]))

    # Scenario audit trail: one decision per directive — what was asked, what the pace
    # allowed, what was trimmed. The independent post-run audit reads exactly these.
    for a in scn_all:
        fx = scn_fx[a["adj_id"]]
        if a in scn_unsupported:
            decide("scenario_applied",
                   f"Scenario '{a['label']}' NOT applied: '{a['adjustment_type']}' is unsupported "
                   f"in v2 (availability must not drive demand §13; cart counts are demand-driven "
                   f"§18). Rephrase as a demand multiplier, floor/cap or exclusion.",
                   severity="warning", scenario_id=a["scenario_id"], applied=False,
                   adjustment_type=a["adjustment_type"], requested_by=a["requested_by"],
                   approved_by=a.get("approved_by"))
            continue
        trim = (f"; requested {fx['trays_requested']} trays, granted {fx['trays_granted']}, "
                f"trimmed {fx['trays_trimmed']} to the stores' selling pace"
                if a["adjustment_type"] in ("allocation_floor", "allocation_cap") else "")
        decide("scenario_applied",
               f"Scenario '{a['label']}' ({a['adjustment_type']}): {fx['lines']} line(s) at "
               f"{len(fx['stores'])} store(s){trim}"
               + ("" if fx["lines"] else " — matched nothing (no store in scope sells at pace)"),
               severity="info" if fx["lines"] else "warning",
               scenario_id=a["scenario_id"], applied=fx["lines"] > 0,
               adjustment_type=a["adjustment_type"], lines=fx["lines"], stores=len(fx["stores"]),
               trays_requested=fx["trays_requested"], trays_granted=fx["trays_granted"],
               trays_trimmed=fx["trays_trimmed"], requested_by=a["requested_by"],
               approved_by=a.get("approved_by"))

    # ══ 7. TRAYS -> CART-LOADING PLAN (§17–19): physical fit only ═══════════
    # §19.1 cart economics — editable assumptions, not rules: delivered cart cost is roughly
    # $180–$300 (default $240); normal wholesale value per cart is roughly $1,700–$3,000.
    cart_cost = float(S.get("cart_delivered_cost", 240) or 240)
    low_value_wh = float(S.get("cart_wholesale_low", 1700) or 1700)
    consolidate = str(S.get("cart_consolidate", True)).lower() not in ("false", "0", "no")
    orders = {}
    for l in ship.values():
        if l.get("alloc_trays", 0) <= 0:
            continue
        orders.setdefault(l["store_id"], []).append(l)

    for store_id, olines in orders.items():
        # shelves per profile, SKUs mixed on a shelf in sku-fit order; partial shelves allowed
        by_prof = defaultdict(list)
        for l in sorted(olines, key=lambda x: (-x["sku_fit"], x["sku_id"])):
            by_prof[pack[l["sku_id"]]["profile_id"]].append(l)
        shelves = []
        for pid, plist in by_prof.items():
            pk0 = pack[plist[0]["sku_id"]]
            cap = int(pk0["trays_per_shelf"])
            cur_items, room = [], cap
            for l in plist:
                left = l["alloc_trays"]
                while left > 0:
                    take = min(left, room)
                    units = take * l["upt"]
                    kit = None                      # assigned network-wide after release/defer
                    cur_items.append((l["sku_id"], take, units, kit, pid))
                    left -= take
                    room -= take
                    if room == 0:
                        shelves.append((pid, cur_items))
                        cur_items, room = [], cap
            if cur_items:
                shelves.append((pid, cur_items))
        # pack shelves into carts, tallest first; first-fit within 5 shelves / 95in / profile max
        shelves.sort(key=lambda s: (PROFILE_HEIGHT_RANK.get(s[0], 3), s[0]))
        carts = []
        if consolidate:
            # §19.1 consolidate within the store: best-fit decreasing by shelf height — each shelf
            # goes to the open cart it fills most tightly (least height left), same physical
            # limits (5 shelves / ceiling / profile max). Shelves and trays are never dropped or
            # added; same-profile partial trays are already merged onto one shelf above.
            shelves.sort(key=lambda s: (-float(pack[s[1][0][0]]["clear_height_in"] or MIN_SHELF_CLEAR_IN),
                                        PROFILE_HEIGHT_RANK.get(s[0], 3), s[0]))
        for pid, items in shelves:
            h = float(pack[items[0][0]]["clear_height_in"] or MIN_SHELF_CLEAR_IN)
            pmax = int(pack[items[0][0]]["max_shelves"] or CART_SHELVES)
            fits = [c for c in carts
                    if (len(c["shelves"]) < CART_SHELVES and c["height"] + h <= CART_CEILING_IN
                        and c["prof_ct"][pid] < pmax)]
            if fits:
                c = (min(fits, key=lambda c: (CART_CEILING_IN - c["height"] - h, len(c["shelves"])))
                     if consolidate else fits[0])
                c["shelves"].append((pid, items))
                c["height"] += h
                c["prof_ct"][pid] += 1
            else:
                carts.append({"shelves": [(pid, items)], "height": h, "prof_ct": defaultdict(int, {pid: 1})})
        out = []
        for ci, c in enumerate(carts, 1):
            ordered = sorted(c["shelves"], key=lambda s: (PROFILE_HEIGHT_RANK.get(s[0], 3), s[0]))
            contents = []
            for pos, (pid, items) in enumerate(ordered, 1):
                for (sku_id, trays, units, kit, p) in items:
                    contents.append((pos, sku_id, trays, units, kit, p))
            # §19.1 cart economics: wholesale value = units x SKU wholesale price (skus.wholesale_price_whl)
            wh_missing = sorted({x[1] for x in contents if not (sku_info[x[1]].get("wholesale_price_whl") or 0)})
            wh_value = sum(x[3] * float(sku_info[x[1]].get("wholesale_price_whl") or 0) for x in contents)
            freight_pct = (cart_cost / wh_value) if wh_value else None
            low_value = wh_value < low_value_wh
            # FULL CC cart = filled shelf space, not shelf count: shelf-equivalents of trays
            # (each shelf's trays / its trays_per_shelf) >= min_cart_fill_pct of the slots this
            # cart can hold (5, or fewer when tall shelves hit the ceiling). Four shelves holding
            # one tray each is NOT a full cart.
            shelf_eq = sum(min(1.0, sum(it[1] for it in items) / max(1, int(pack[items[0][0]]["trays_per_shelf"])))
                           for _, items in ordered)
            ceiling_hit = c["height"] + MIN_SHELF_CLEAR_IN > CART_CEILING_IN
            slots = len(ordered) if (ceiling_hit and len(ordered) < CART_SHELVES) else CART_SHELVES
            tray_fill = shelf_eq / slots
            phys_full = tray_fill >= min_cart_fill
            out.append({"cart_number": ci, "cart_type_id": "cc", "shelves_used": len(ordered),
                        "phys_full": phys_full, "tray_fill": round(tray_fill, 3),
                        "total_units": sum(x[3] for x in contents), "contents": contents,
                        "is_full": len(ordered) >= CART_SHELVES, "is_flagged": False,
                        "wholesale_value": round(wh_value, 2), "wholesale_incomplete": wh_missing,
                        "delivered_cost": cart_cost,
                        "freight_pct": None if freight_pct is None else round(freight_pct, 4),
                        "low_value": low_value})
        # A cart for a variety-restricted store that carries a SKU whose batch supply includes
        # a denied variety gets a handling tag: it becomes its own cart config, so ops never
        # builds it off the same sheet as an unrestricted cart with the same SKU mix.
        _deny = store_deny.get(store_id)
        if _deny:
            _tag = (store_rule[store_id].get("tag") or store_rule[store_id].get("label") or "RESTRICTED")
            for c in out:
                if any(pools.denied_supply(x[1], _deny) > 0 for x in c["contents"]):
                    c["handling_tag"] = _tag
        grade_framework = {"AA": 3, "A": 3, "B": 2, "C+": 2, "C": 1, "C-": 1, "D": 1}
        top_grade = min((l["grade"] for l in olines), key=GRADE_ORDER.index)
        if len(out) > grade_framework[top_grade]:
            decide("review", f"{len(out)} carts exceeds the §18 initial framework ({grade_framework[top_grade]} "
                             f"for best family grade {top_grade}) — demand-driven, shipped (cart caps off)",
                   store_id=store_id, severity="info", carts=len(out))
        orders[store_id] = {"lines": olines, "carts": out}

    # STORE CAPACITY (§1 "AI may not override store-capacity limits", §4.1): when the store
    # master carries capacity_carts, the order never exceeds it. The lowest-wholesale carts are
    # cut, their trays returned to need (recalculated next run), and the cut is logged. Inert
    # (NULL) until the business loads capacities — reported in data_quality.store_hard_block_data.
    cap_cut = {"stores": 0, "carts": 0, "units": 0}
    for store_id in sorted(orders):
        _cap = (stores.get(store_id) or {}).get("capacity_carts")
        o = orders[store_id]
        if not _cap or len(o["carts"]) <= int(_cap):
            continue
        keep_n = max(1, int(_cap))
        ranked = sorted(o["carts"], key=lambda c: (-c["wholesale_value"], c["cart_number"]))
        keep = sorted(ranked[:keep_n], key=lambda c: c["cart_number"])
        drop = ranked[keep_n:]
        by_sku = {l["sku_id"]: l for l in o["lines"]}
        cut_units = 0
        for c in drop:
            for (_pos, sk, tr, u, _k, _pid) in c["contents"]:
                l = by_sku.get(sk)
                if l:
                    l["alloc_trays"] = max(0, l["alloc_trays"] - tr)
                    l["alloc_units"] = max(0, l["alloc_units"] - u)
                cut_units += u
        for i, c in enumerate(keep, 1):
            c["cart_number"] = i
        o["carts"] = keep
        alloc_units_total -= cut_units
        cap_cut["stores"] += 1
        cap_cut["carts"] += len(drop)
        cap_cut["units"] += cut_units
        decide("capacity_cap",
               f"Order cut to the store's capacity: {len(drop)} cart(s) / {cut_units:,}u removed "
               f"(capacity {int(_cap)} carts, §1 hard block); the cut need is recalculated next run",
               store_id=store_id, severity="warning", capacity_carts=int(_cap),
               carts_cut=len(drop), units_cut=cut_units)
    if cap_cut["stores"]:
        log(f"Store capacity: {cap_cut['stores']} stores cut to capacity "
            f"({cap_cut['carts']} carts, {cap_cut['units']:,}u)")

    # MINIMUM ORDER = ONE FULL CC CART (leadership decision, 2026-11-20 meeting; supersedes the
    # "warning only" treatment of light orders in §19.1). HARD STOP: a store whose order does not
    # fill at least `min_order_full_carts` physically full cart(s) is pushed to next week — no
    # Critical/overdue/lost-sales override, and trays are never added to fill a cart. Its need
    # is recalculated next week with coverage extended one week per consecutive push. Orders
    # that pass ship whole (a trailing partial cart rides on the same stop).
    pushed = {"stores": 0, "carts": 0, "units": 0, "wholesale": 0.0, "lost_one_week": 0.0,
              "repeat_stores": 0, "review_stores": 0}
    # Escalation (audit F2, 2026-11-20): a store pushed this many consecutive due cycles has its
    # coverage pinned at the freshness / sell-through cap and will likely never fill a cart — a
    # de-facto service stop. Surface it for DSM review instead of looping silently.
    min_push_review = int(S.get("min_cart_review_after_pushes", 4) or 4)
    for store_id in sorted(orders):
        if min_full_carts <= 0:
            break
        o = orders[store_id]
        n_full = sum(1 for c in o["carts"] if c["phys_full"])
        if n_full >= min_full_carts:
            continue
        W = sum(c["wholesale_value"] for c in o["carts"])
        units = sum(l["alloc_units"] for l in o["lines"])
        lost = sum(min(l["fc"], max(0.0, l["fc"] - l["projected"])) * usd(l) for l in o["lines"])
        shelves = sum(c["shelves_used"] for c in o["carts"])
        streak = push_streak.get(store_id, 0)
        decide("pushed_min_cart",
               f"Order PUSHED to next week: {shelves} shelf(s) on {len(o['carts'])} cart(s), "
               f"{n_full} full — below the one-full-CC-cart minimum (${W:,.0f} wholesale, {units:,}u; "
               f"one-week sales at risk ${lost:,.0f}). Need recalculated next week with "
               f"{streak + 1} extra coverage week(s); never padded (leadership decision 2026-11-20)",
               store_id=store_id, severity="warning" if streak >= 2 else "info", action="push",
               order_wholesale=round(W, 2), carts=len(o["carts"]), shelves=shelves, full_carts=n_full,
               units=units, lost_sales_one_week=round(lost, 2), consecutive_pushes=streak + 1)
        pushed["stores"] += 1
        pushed["carts"] += len(o["carts"])
        pushed["units"] += units
        pushed["wholesale"] += W
        pushed["lost_one_week"] += lost
        pushed["repeat_stores"] += 1 if streak else 0
        if streak + 1 >= min_push_review:
            pushed["review_stores"] += 1
            decide("min_cart_service_review",
                   f"Pushed {streak + 1} consecutive due cycles under the one-full-cart minimum — "
                   f"coverage is already at its freshness/sell-through cap, so this is an effective "
                   f"service stop. Needs DSM review: verify demand, then either a leadership release "
                   f"below the minimum or a recorded stop (§11.3/§27 reserve service stops for the "
                   f"approval chain). One-week sales at risk ${lost:,.0f}.",
                   store_id=store_id, severity="warning", consecutive_pushes=streak + 1,
                   lost_sales_one_week=round(lost, 2), order_wholesale=round(W, 2))
        alloc_units_total -= units
        for l in o["lines"]:
            l["deferred_units"] = l["alloc_units"]
            l["alloc_units"], l["alloc_trays"] = 0, 0
        del orders[store_id]
    log(f"Minimum order (1 full CC cart): {pushed['stores']:,} stores pushed · {pushed['carts']:,} carts · "
        f"{pushed['units']:,}u · ${pushed['wholesale']:,.0f} wholesale")

    # §19.1 low-value order decision (economic, not a size floor; §23 deferral), judged on each
    # store's WHOLE order: W = order wholesale, n = carts, C = n x cart_delivered_cost. RELEASE
    # the order when W/n >= cart_wholesale_low, or one more week of waiting would lose at least
    # C in retail sales — lost = Σ min(fc, max(0, fc − projected stock at arrival)) × price over
    # its lines — or any line is Critical risk. Otherwise DEFER the whole order (no partial-store
    # deferral): need is recalculated next week, never padded. No cross-store consolidation.
    # Deferral history is durable: earlier v2 runs' run_decisions (latest run per earlier ship
    # week, newest first, up to 8 weeks). A preview with no history treats everyone as first-time.
    cur.execute("""
        SELECT DISTINCT ON (shipping_week) id, shipping_week FROM recommendation_runs
        WHERE engine_version = %s AND shipping_week < %s AND COALESCE(status, '') NOT IN %s
        ORDER BY shipping_week DESC, run_at DESC
    """, (ENGINE_VERSION, shipping_week, CANCELLED_RUN_STATUSES))
    hist_runs = sorted(cur.fetchall(), key=lambda r: r["shipping_week"], reverse=True)[:8]
    hist = defaultdict(lambda: [set() for _ in hist_runs])
    if hist_runs:
        idx = {str(r["id"]): i for i, r in enumerate(hist_runs)}
        cur.execute("""SELECT run_id, store_id, decision_type FROM run_decisions
                       WHERE run_id = ANY(%s::uuid[]) AND store_id IS NOT NULL
                         AND decision_type IN ('deferred_economics', 'not_due', 'low_value_cart', 'review')""",
                    (list(idx),))
        for r in cur.fetchall():
            hist[r["store_id"]][idx[str(r["run_id"])]].add(r["decision_type"])
    econ_second = econ_overdue = 0
    econ = {"released_low_value": 0, "stores_released_low_value": 0, "deferred_carts": 0,
            "deferred_units": 0, "deferred_wholesale": 0.0, "deferred_retail_one_week_at_risk": 0.0,
            "deferred_single_cart": 0, "deferred_multi_cart": 0}
    deferred_stores = []
    for store_id in sorted(orders):
        o = orders[store_id]
        n = len(o["carts"])
        W = sum(c["wholesale_value"] for c in o["carts"])
        C = n * cart_cost
        lost = sum(min(l["fc"], max(0.0, l["fc"] - l["projected"])) * usd(l) for l in o["lines"])
        critical = sorted(l["sku_id"] for l in o["lines"] if l["risk"] == "Critical")
        prior_def = prior_economic_deferrals(hist.get(store_id, []))
        overdue = is_overdue(store_last_del.get(store_id), arrival, store_due[store_id][2])
        action = economic_action(W, n, lost, cart_cost, low_value_wh, bool(critical), prior_def, max_econ_def,
                                 overdue=overdue)
        if min_full_carts > 0 and action == "defer":
            action = "release_min_cart"     # floor met: one full CC cart is the agreed minimum
        if action == "pass":
            continue
        fp = (C / W) if W else None
        nums = (f"order wholesale ${W:,.0f} on {n} cart(s) (${W / n:,.0f}/cart < ${low_value_wh:,.0f}); "
                f"delivered cost ${C:,.0f} (freight {'n/a' if fp is None else f'{fp:.0%}'}); "
                f"one-week lost sales if it waits ${lost:,.0f}")
        data = dict(order_wholesale=round(W, 2), carts=n, delivered_cost=C,
                    freight_pct=None if fp is None else round(fp, 4), lost_sales_one_week=round(lost, 2),
                    margin_proxy_wholesale=round(W, 2), critical_skus=critical,
                    units=sum(l["alloc_units"] for l in o["lines"]),
                    wholesale_missing_skus=sorted({x for c in o["carts"] for x in c["wholesale_incomplete"]}))
        data["prior_economic_deferrals"] = prior_def
        if action in ("release", "release_after_deferral", "release_overdue", "release_min_cart"):
            if action == "release_after_deferral":
                econ_second += 1
            if action == "release_overdue":
                econ_overdue += 1
            why = (("Critical-risk line(s) " + ", ".join(str(x) for x in critical)) if critical else
                   "meets the one-full-CC-cart minimum (leadership decision 2026-11-20)"
                   if action == "release_min_cart" else
                   f"lost sales ${lost:,.0f} >= delivered cost ${C:,.0f}" if lost >= C else
                   (f"overdue — released (last delivery {store_last_del.get(store_id)}, beyond "
                    f"{store_due[store_id][2] + 1} wks)") if action == "release_overdue" else
                   f"second due week, released per policy (deferred {prior_def} prior due "
                   f"cycle(s); max_economic_deferrals {max_econ_def})")
            decide("low_value_cart", f"Order RELEASED: {nums} — {why} (§19.1)", store_id=store_id,
                   severity="warning", action="release", **data)
            for c in o["carts"]:
                c["econ_action"] = "release"
            econ["released_low_value"] += n
            econ["stores_released_low_value"] += 1
            continue
        decide("deferred_economics", f"Order DEFERRED: {nums} < delivered cost — waiting a week costs "
                                     f"less than delivering now; deferred once, will ship next time due "
                                     f"regardless; need recalculated then, never padded (§19.1, §23)", store_id=store_id, action="defer", **data)
        econ["deferred_carts"] += n
        econ["deferred_units"] += data["units"]
        econ["deferred_wholesale"] += W
        econ["deferred_retail_one_week_at_risk"] += lost
        econ["deferred_single_cart" if n == 1 else "deferred_multi_cart"] += 1
        alloc_units_total -= data["units"]
        for l in o["lines"]:
            l["deferred_units"] = l["alloc_units"]
            l["alloc_units"], l["alloc_trays"] = 0, 0
        deferred_stores.append(store_id)
        del orders[store_id]
    log(f"Cart economics: {econ['released_low_value']:,} low-value carts released (whole orders) · {econ['deferred_carts']:,} "
        f"deferred ({econ['deferred_units']:,}u, ${econ['deferred_wholesale']:,.0f} wholesale) · "
        f"{len(deferred_stores):,} stores fully deferred")

    # Kit assignment (jute vs ceramic), balanced at NETWORK level per SKU. v2 orders are small
    # per store, so the legacy per-store split (determine_kit_type) put ~99% of 12cm/17cm into
    # jute. Kit options per SKU come from every availability line: a grower-pot (1M) line can be
    # kitted either way; finished 3M kits only as the kit types actually available. When both
    # are possible each shelf item goes to the side behind the target share
    # (kit_split_ceramic_share, default 0.5); exact ties alternate. Done after release/defer so
    # deferred orders do not skew the split of what ships. Units and trays are unchanged.
    _cs = S.get("kit_split_ceramic_share")
    cer_share = 0.5 if _cs is None else float(_cs)
    kit_opts = defaultdict(set)
    for r in avail_rows:
        if (r["code_level"] or "3M") != "3M":
            kit_opts[r["sku_id"]] |= {"jute_kit", "ceramic_kit"}
        elif r["hard_good_type"] in ("jute_kit", "ceramic_kit"):
            kit_opts[r["sku_id"]].add(r["hard_good_type"])
    kit_units = defaultdict(lambda: {"jute_kit": 0, "ceramic_kit": 0})
    kit_tie = defaultdict(bool)
    for store_id in sorted(orders):
        for c in orders[store_id]["carts"]:
            new = []
            for (pos, sku_id, trays, units, _k, pid) in c["contents"]:
                opts = kit_opts.get(sku_id, set())
                if len(opts) == 2:
                    t = kit_units[sku_id]
                    after = t["jute_kit"] + t["ceramic_kit"] + units
                    dev_c = abs(t["ceramic_kit"] + units - cer_share * after)
                    dev_j = abs(t["ceramic_kit"] - cer_share * after)
                    if dev_c == dev_j:
                        kit = "ceramic_kit" if kit_tie[sku_id] else "jute_kit"
                        kit_tie[sku_id] = not kit_tie[sku_id]
                    else:
                        kit = "ceramic_kit" if dev_c < dev_j else "jute_kit"
                    t[kit] += units
                elif len(opts) == 1:
                    kit = next(iter(opts))
                    kit_units[sku_id][kit] += units
                else:
                    kit = avail_meta.get(sku_id, {}).get("hard_good_type", "ceramic_kit")
                new.append((pos, sku_id, trays, units, kit, pid))
            c["contents"] = new

    # ══ 8. REVIEW (§24–25) + $900K OBJECTIVE (§2) + ALLOCATION OUTPUTS (§15) ══
    review = {}
    for store_id, o in orders.items():
        units = sum(l["alloc_units"] for l in o["lines"])
        # LOW confidence is the systemic norm (freshness-capped on-hand) and is reported once
        # at run level; per-store warnings fire only on store-specific evidence gaps.
        unusable = sum(l["alloc_units"] for l in o["lines"] if l["conf"] == "UNUSABLE")
        notes = []
        if store_id in edi_gap:
            notes.append("EDI gap — demand held at baseline")
        if units and unusable / units > 0.5:
            notes.append("most units rest on implausible (negative) on-hand — capped at 2 wks")
        if any(l["cap_note"] for l in o["lines"]):
            notes.append("tray caps applied")
        verdict = "WARNING" if notes else "PASS"
        review[store_id] = (verdict, notes)
        decide("review", f"{verdict}: " + ("; ".join(notes) if notes else "evidence supports the order"),
               store_id=store_id, verdict=verdict, units=units,
               shipping_efficiency=shipping_efficiency(store_eff_s.get(store_id, 0), store_eff_d.get(store_id, 0)),
               shipping_efficiency_by_family={f: eff.get((store_id, f)) for f in sorted({l["family"] for l in o["lines"]})})

    # weekly retail: arrival week, with vs without this plan
    base_units_rev = with_rev = demand_rev = 0.0
    for l in lines.values():
        wk_dem = l["fc"]
        demand_rev += wk_dem * usd(l)
        base = min(wk_dem, l["projected"])
        add = ship.get((l["store_id"], l["sku_id"]), {}).get("alloc_units", 0)
        base_units_rev += base * usd(l)
        with_rev += min(wk_dem, l["projected"] + add) * usd(l)
    incr_rev = with_rev - base_units_rev
    lost_unavail = sum(l.get("short_units", 0) * usd(l) / l["cover_wk"] for l in ship.values())
    lost_held = sum(lines[k]["trays"] * lines[k]["upt"] * usd(lines[k]) / lines[k]["cover_wk"] for k in held)
    edi_risk = sum(l["fc"] * usd(l) for l in lines.values() if l["store_id"] in edi_gap)
    all_carts = [c for o in orders.values() for c in o["carts"]]
    tot_wh = sum(c["wholesale_value"] for c in all_carts)

    # Delivery cadence readout: stores with need, due / not due / shipped by cadence grade
    by_g = {g: {"cadence_weeks": int(cadence[g]), "due": 0, "not_due": 0, "shipped": 0}
            for g in GRADE_ORDER if g in cadence}
    for st_id, (due, cg, _cw, _n) in store_due.items():
        by_g.setdefault(cg, {"cadence_weeks": int(cadence.get(cg, 0)), "due": 0, "not_due": 0, "shipped": 0})
        by_g[cg]["due" if due else "not_due"] += 1
        if st_id in orders:
            by_g[cg]["shipped"] += 1
    cadence_summary = {
        "settings": {"delivery_cadence_weeks": cadence, "cadence_buffer_weeks": cad_buffer,
                     "due_slack_days": DUE_SLACK_DAYS,
                     "economic_deferral_for_due_stores": econ_defer_due,
                     "max_economic_deferrals": max_econ_def},
        "stores_deferred_first_time": len(deferred_stores),
        "stores_released_after_deferral": econ_second,
        "stores_released_overdue": econ_overdue,
        "deferral_history_runs": [str(r["shipping_week"]) for r in hist_runs],
        "stores_due": sum(1 for v in store_due.values() if v[0]),
        "stores_not_due": sum(1 for v in store_due.values() if not v[0]),
        "stores_shipped": len(orders),
        "units": int(sum(l["alloc_units"] for o in orders.values() for l in o["lines"])),
        "carts": len(all_carts),
        "avg_cart_wholesale": round(tot_wh / len(all_carts), 2) if all_carts else None,
        "freight_pct": round(cart_cost * len(all_carts) / tot_wh, 4) if tot_wh else None,
        "by_grade": by_g,
    }
    fam_s, fam_d = defaultdict(float), defaultdict(float)
    for (_st, f), v in eff_sold.items():
        fam_s[f] += v
    for (_st, f), v in eff_del.items():
        fam_d[f] += v
    eff_summary = {
        "definition": "13-wk units sold / 13-wk units delivered, same window, no lag (Josh's KPI); "
                      "reported only — the §11 conversion gate is unchanged",
        "network": shipping_efficiency(sum(fam_s.values()), sum(fam_d.values())),
        "network_sold_13wk": round(sum(fam_s.values())),
        "network_delivered_13wk": round(sum(fam_d.values())),
        "by_family": {f: shipping_efficiency(fam_s.get(f, 0), fam_d.get(f, 0))
                      for f in sorted(set(fam_s) | set(fam_d))},
        "store_families_measured": len(eff),
    }
    price_srcs = defaultdict(int)
    for l in ship.values():
        price_srcs[l["price_src"]] += l.get("alloc_units", 0)

    summary = {
        "engine": ENGINE_VERSION, "spec": "AI Replenishment master spec 2026-11-17",
        "sales_anchor": str(anchor), "arrival": str(arrival),
        "objective": {
            "minimum_weekly_retail": objective,
            "baseline_weekly_retail": round(base_units_rev),
            "incremental_weekly_retail": round(incr_rev),
            "expected_weekly_retail": round(with_rev),
            "gap_to_minimum": round(objective - with_rev),
            "unconstrained_weekly_demand_retail": round(demand_rev),
            "lost_unavailable_inventory": round(lost_unavail),
            "lost_blocks_and_holds": round(lost_held),
            "lost_deferrals_one_week": round(econ["deferred_retail_one_week_at_risk"]),
            "lost_minimum_cart_pushes_one_week": round(pushed["lost_one_week"]),
            "edi_uncertainty_at_risk": round(edi_risk),
            "dollar_basis": "wholesale",
            "price_basis_units": dict(price_srcs),
            "method_note": ("Arrival-week dollars across every store x SKU with velocity: "
                            "min(weekly forecast, projected stock) x price. BASIS IS WHOLESALE "
                            "(leadership 2026-11-20): SKU wholesale (skus.wholesale_price_whl), else "
                            "realized DTS wholesale $/unit, else pricebook retail (flagged). The "
                            "*_retail key names are legacy — see dollar_basis. SKUs with no "
                            "price are never guessed: they ship on units and are excluded from $."),
        },
        "incomplete_price_units": int(price_srcs.get("missing", 0)),
        "delivery_cadence": cadence_summary,
        "shipping_efficiency": eff_summary,
        "shrink": shrink_summary,
        "scenarios": {
            "directives": len(scn_all),
            "applied": sum(1 for a in scn_all if scn_fx[a["adj_id"]]["lines"] > 0),
            "unsupported": len(scn_unsupported),
            "by_directive": [{"label": a["label"], "type": a["adjustment_type"],
                              "lines": scn_fx[a["adj_id"]]["lines"],
                              "stores": len(scn_fx[a["adj_id"]]["stores"]),
                              "trays_trimmed": scn_fx[a["adj_id"]]["trays_trimmed"],
                              "requested_by": a["requested_by"],
                              "approved_by": a.get("approved_by")} for a in scn_all],
        },
        "post_delivery": {
            "mode": pd_mode,
            "store_families_measured": len(pd_response),
            "stores_cadence_adjusted": len(set(pd_adj) & set(store_due)) if pd_mode == "apply" else 0,
            "note": "7–28 day shipment response (§29); observe = measured + reported only; "
                    "apply shifts a measured store's cadence by at most one week",
        },
        "gap_fill": {"fills": gap_fills, "trays": gap_fill_trays, "units": gap_fill_units,
                     "short_store_families": len(gaps)},
        "store_rules": {
            "source": "Home Depot store master (first sheet) — stores.sku_rule / store_status / cross_dock",
            "stores_with_rule": len(store_rule),
            "by_rule": {code: {"label": (rules.get(code) or {}).get("label", code), "stores": n}
                        for code, n in sorted(Counter(r["code"] for r in store_rule.values()).items())},
            "stores_do_not_ship": sum(1 for s_ in stores.values()
                                      if (s_.get("store_status") or "active") == "do_not_ship"),
            "stores_no_routine_order": len({k[0] for k, v in held_kind.items() if v == "store_rule_block"}),
            "stores_products_limited": len({k[0] for k, v in held_kind.items() if v == "sku_restriction_block"}),
            "units_not_shipped_by_rule": int(rule_blocked_units),
            "variety_restricted_stores_shipping": sum(1 for s_ in orders if s_ in store_deny),
            "variety_short_units": int(sum(v["units"] for v in variety_short.values())),
            "carts_with_handling_tag": sum(1 for c in all_carts if c.get("handling_tag")),
            "cross_dock_stores_shipping": dict(Counter(
                (stores.get(s_) or {}).get("cross_dock") for s_ in orders
                if (stores.get(s_) or {}).get("cross_dock"))),
            "denied_genus_units_in_batch": hard_block_data["store_master"]["denied_genus_units_in_batch"],
        },
        "cart_economics": {
            "carts": len(all_carts),
            "wholesale_value": round(tot_wh, 2),
            "delivered_cost": round(cart_cost * len(all_carts), 2),
            "freight_pct": round(cart_cost * len(all_carts) / tot_wh, 4) if tot_wh else None,
            "avg_wholesale_per_cart": round(tot_wh / len(all_carts), 2) if all_carts else None,
            "low_value_carts": sum(1 for c in all_carts if c["low_value"]),
            "carts_released_low_value": econ["released_low_value"],
            "carts_deferred": econ["deferred_carts"],
            "stores_released_low_value": econ["stores_released_low_value"],
            "stores_deferred_single_cart": econ["deferred_single_cart"],
            "stores_deferred_multi_cart": econ["deferred_multi_cart"],
            "deferred_units": econ["deferred_units"],
            "deferred_wholesale": round(econ["deferred_wholesale"], 2),
            "deferred_retail_one_week_at_risk": round(econ["deferred_retail_one_week_at_risk"], 2),
            "stores_deferred": len(deferred_stores),
            "min_order_full_carts": min_full_carts, "min_cart_fill_pct": min_cart_fill,
            "avg_tray_fill_shipping": (round(sum(c["tray_fill"] for c in all_carts) / len(all_carts), 3)
                                       if all_carts else None),
            "pushed_min_cart": {"stores": pushed["stores"], "carts": pushed["carts"], "units": pushed["units"],
                                "wholesale": round(pushed["wholesale"], 2),
                                "lost_one_week": round(pushed["lost_one_week"], 2),
                                "repeat_stores": pushed["repeat_stores"],
                                "service_review_stores": pushed["review_stores"],
                                "review_after_pushes": min_push_review},
            "capacity_cap": cap_cut,
            "kpi_scope": "carts, wholesale, $/cart and freight cover carts SHIPPING this week only; "
                         "pushed and deferred carts are reported separately",
            "avg_freight_pct_released": (round(sum(c["freight_pct"] for c in all_carts if c["freight_pct"])
                                               / max(1, sum(1 for c in all_carts if c["freight_pct"])), 4)
                                         if all_carts else None),
            "carts_missing_wholesale_price": sum(1 for c in all_carts if c["wholesale_incomplete"]),
            "assumptions": {"cart_delivered_cost": cart_cost, "delivered_cart_cost_range": [180, 300],
                            "cart_wholesale_low": low_value_wh,
                            "normal_wholesale_range": [1700, 3000],
                            "note": "editable assumptions (§19.1); judged per store order: released when wholesale/cart "
                                    ">= cart_wholesale_low, one week of lost sales >= carts x cart cost, or a "
                                    "line is Critical; else the whole order is deferred; never padded"},
        },
        "allocation": {
            "available_units": total_supply,
            "unconstrained_need_units": int(sum(l.get("trays_unreduced", l["trays"]) * l["upt"]
                                                for l in lines.values())),
            "eligible_need_units": int(sum(l["trays"] * l["upt"] for l in ship.values())),
            "allocated_units": int(alloc_units_total),
            "unfilled_demand_units": int(sum(unfilled.values())),
            "unallocated_supply_units": int(total_supply - alloc_units_total),
            "blocked_units": int(blocked_units), "stopped_13wk_units": int(stopped_units),
            "reduced_13wk_units_removed": int(reduced_units), "review_held_units": 0,
        },
        "counts": {
            "stores_deferred_economics": len(deferred_stores),
            "carts_deferred_economics": econ["deferred_carts"],
            "stores_with_velocity": len({l["store_id"] for l in lines.values()}),
            "stores_shipped": len(orders),
            "carts": sum(len(o["carts"]) for o in orders.values()),
            "lines_shipped": sum(len(o["lines"]) for o in orders.values()),
            "lines_blocked": n_block,
            "lines_assortment_blocked": sum(1 for k in held_kind.values() if k == "assortment_block"),
            "lines_store_rule_blocked": sum(1 for k in held_kind.values() if k in RULE_KINDS),
            "stores_store_rule_blocked": len({k[0] for k, v in held_kind.items() if v == "store_rule_block"}),
            "lines_stopped_13wk": n_stop,
            "lines_reduced_13wk": n_reduced,
            "lines_review_held": 0,                 # no routine holds in v2 (§1, §24)
            "stores_edi_gap": len(edi_gap),
            "lines_reactivated": sum(1 for l in ship.values() if l.get("reactivation")),
            "review_pass": sum(1 for v in review.values() if v[0] == "PASS"),
            "review_warning": sum(1 for v in review.values() if v[0] == "WARNING"),
        },
        "data_quality": {
            "sales_age_days": stale,
            "onhand_source": (f"LOW confidence everywhere: sellable on-hand = min(DTS reported, "
                              f"{fresh_wk}-wk fresh deliveries − sales); DTS dumps are batch-recorded"
                              if snap else f"estimate only: {fresh_wk}-wk deliveries − sales (LOW confidence)"),
            "family_mappings_unapproved": unapproved_map,
            "store_hard_block_data": hard_block_data,
            "settings_pending_backtest": [k for k in S],
        },
        "not_yet_implemented": [
            "§14 seasonal/ad 13-week protection (needs event records: target in-store date, eligible stores)",
            "§26 DSM information portal (manager-override reactivations are read via ipr_reactivations)",
            "§29 post-delivery measurement, §30 back-testing harness",
            "§25 LLM reviewer (deterministic reviewer runs now; the AI /audit endpoint can layer on)",
        ],
    }
    # §28 autonomous order statuses — this run's progress through the pre-export stages.
    # Hard-rule failures raise before this point (Incomplete Mandatory Data / duplicate run),
    # so reaching here means each stage below completed.
    n_val = Counter(dd.get("severity") for (k, _s, _k, _r, dd) in decisions if k == "validation")
    n_warn = sum(1 for v in review.values() if v[0] == "WARNING")
    stages = [
        ("Input Received", f"batch {batch_id}: {len(avail_rows):,} availability lines, sales to {anchor}"),
        ("Data Validated", f"{n_val.get('block', 0)} SKU exclusions, {n_val.get('warning', 0)} warnings"),
        ("Demand Calculated", f"{len(lines):,} store x SKU lines with velocity; "
                              f"{summary['allocation']['unconstrained_need_units']:,}u need"),
        ("Inventory Allocated", f"{int(alloc_units_total):,}u allocated ({gap_fill_units:,}u gap fill); "
                                f"{int(sum(unfilled.values())):,}u unfilled"),
        ("Cart Built", f"{len(all_carts):,} carts for {len(orders):,} stores; "
                       f"{econ['released_low_value']:,} low-value released, {econ['deferred_carts']:,} deferred"),
        ("Independent AI Review", f"deterministic reviewer: {len(review) - n_warn:,} PASS, {n_warn:,} WARNING "
                                  f"(LLM reviewer not yet implemented)"),
        ("Final Validated", "routine final validation by the agent (§28); no manual approval"
                            if orders else "nothing to ship this week"),
    ]
    summary["pipeline_status"] = {
        "current": stages[-1][0],
        "stages": [{"stage": n, "status": "complete", "detail": dt} for n, dt in stages],
        "next": ["Aster Exported", "Released", "Shipped"],
    }
    o = summary["objective"]
    log(f"\nObjective: baseline ${o['baseline_weekly_retail']:,} + incremental ${o['incremental_weekly_retail']:,} "
        f"= ${o['expected_weekly_retail']:,}  (min ${objective:,.0f}, gap ${o['gap_to_minimum']:,})")
    a = summary["allocation"]
    log(f"Allocation: need {a['unconstrained_need_units']:,}u · allocated {a['allocated_units']:,}u of "
        f"{a['available_units']:,}u available · unfilled {a['unfilled_demand_units']:,}u · "
        f"unallocated supply {a['unallocated_supply_units']:,}u (acceptable, §15)")
    c = summary["counts"]
    log(f"Plan: {c['stores_shipped']} stores · {c['carts']} carts · {c['lines_shipped']} lines · "
        f"blocked {c['lines_blocked']} · stopped {c['lines_stopped_13wk']} · reduced {c['lines_reduced_13wk']} · review PASS {c['review_pass']} / "
        f"WARNING {c['review_warning']}")

    if preview:
        log("\nPREVIEW — nothing written.")
        return {"summary": summary, "orders": orders, "lines": lines, "held": held, "decisions": decisions}

    # ══ WRITE (same tables as legacy, so downstream tools work unchanged) ═══
    total_carts = summary["counts"]["carts"]
    cur.execute("""
        INSERT INTO recommendation_runs
            (batch_id, shipping_week, stores_scored, stores_served, total_carts, total_units,
             inventory_clearance, status, sales_cutoff_date, engine_version, pipeline_status,
             ipr_summary, notes)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'draft',%s,%s,%s,%s,%s)
        RETURNING id
    """, (batch_id, shipping_week, summary["counts"]["stores_with_velocity"], len(orders), total_carts,
          int(alloc_units_total), round(alloc_units_total / total_supply * 100, 2) if total_supply else 0,
          anchor, ENGINE_VERSION, summary["pipeline_status"]["current"], json.dumps(summary, default=str),
          "IPR v2 demand-first run (spec 2026-11-17)"))
    run_id = str(cur.fetchone()["id"])

    iso = shipping_week.isocalendar()
    order_rows, cart_rows, shelf_rows, line_rows, ledger_pending = [], [], [], [], []
    order_ids = {}
    # Availability-line (Aster variety) assignment, per variety-restriction group: stores that
    # may not receive a genus draw only from the lines they may receive; unrestricted stores
    # use up the restricted varieties first. line_remaining[(deny, line)] is that group's share.
    line_remaining, sku_lines_order = {}, {}
    sku_committed = defaultdict(lambda: defaultdict(int))       # sku -> deny set -> units
    for st_id2, o2 in orders.items():
        for l in o2["lines"]:
            sku_committed[l["sku_id"]][store_deny.get(st_id2, NO_DENY)] += l["alloc_units"]
    for sku_id, by_group in sku_committed.items():
        _lines = [[lid, av, line_genus.get(lid, "")] for lid, av in aster_lines.get(sku_id, [])]
        for deny, got in store_rules.assign_lines(by_group, _lines, _distribute_units_across_lines).items():
            sku_lines_order[(sku_id, deny)] = [lid for lid, _av in aster_lines.get(sku_id, []) if got.get(lid)]
            for lid, q in got.items():
                line_remaining[(deny, lid)] = line_remaining.get((deny, lid), 0) + q

    # exact-signature config codes (no cap, no consolidation — every code is buildable blind)
    sig_count = defaultdict(int)
    cart_sig = {}
    for store_id, o2 in orders.items():
        for c in o2["carts"]:
            sig = tuple(sorted((p, sk, tr, kt) for (p, sk, tr, u, kt, _pid) in c["contents"]))
            if c.get("handling_tag"):               # restricted build = its own config
                sig = sig + (("~tag", c["handling_tag"]),)
            cart_sig[(store_id, c["cart_number"])] = sig
            sig_count[sig] += 1
    sig_code = {s: f"CC{i + 1}" for i, s in enumerate(sorted(sig_count, key=lambda s: (-sig_count[s], str(s))))}
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'order_carts' AND column_name = 'handling_tag'""")
    _has_tag_col = bool(cur.fetchone())
    cart_tag_rows = []

    cart_seq = 0
    template = {}
    unassigned_restricted = defaultdict(int)     # variety-restricted units left without a line
    for store_id in sorted(orders):
        o2 = orders[store_id]
        oid = str(uuid.uuid4())
        order_ids[store_id] = oid
        lns = o2["lines"]
        best = max(lns, key=lambda x: x["priority"])
        fk = (store_id, best["family"])
        top_fam_vel = max(fam_vel[(store_id, l["family"])] for l in lns)
        top_recency = max(rec[(l["store_id"], l["sku_id"])] for l in lns)
        order_rows.append((oid, run_id, store_id, "draft", round(best["priority"] * 100, 2),
                           round(top_fam_vel, 4), round(top_recency, 4),
                           service[fk][1], RISK_SCORE[best["risk"]],
                           (anchor - fam_lastdel[fk]).days if fam_lastdel[fk] else None,
                           len(o2["carts"]), sum(l["alloc_units"] for l in lns), False,
                           service[fk][1], True))
        for c in o2["carts"]:
            cart_seq += 1
            cid = str(uuid.uuid4())
            code = sig_code[cart_sig[(store_id, c["cart_number"])]]
            cart_rows.append((cid, oid, store_id, c["cart_number"], "cc", c["shelves_used"], CART_SHELVES,
                              c["total_units"], c["is_full"], False, None, CART_SHELVES,
                              f"WK{iso[0]}{iso[1]:02d}-C{cart_seq:04d}", code, True))
            if c.get("handling_tag"):
                cart_tag_rows.append((c["handling_tag"], cid))
            _deny = store_deny.get(store_id, NO_DENY)
            t = template.setdefault(code, {"cart_count": 0, "stores": set(), "units": 0, "shelves": None})
            t["cart_count"] += 1
            t["stores"].add(store_id)
            t["units"] += c["total_units"]
            if t["shelves"] is None:
                sh = {}
                for (pos, sk, tr, u, kt, pid) in c["contents"]:
                    e = sh.setdefault(pos, {"profile_id": pid, "trays": 0, "units": 0, "varieties": []})
                    e["trays"] += tr
                    e["units"] += u
                    e["varieties"].append((sk, tr, u))
                t["shelves"] = sh
            t.setdefault("cart_ids", []).append(cid)
            for (pos, sk, tr, u, kt, pid) in c["contents"]:
                need_u, trays_left, segs = u, tr, []
                for lid in sku_lines_order.get((sk, _deny), []):
                    if need_u <= 0:
                        break
                    av = line_remaining.get((_deny, lid), 0)
                    if av <= 0:
                        continue
                    take = min(need_u, av)
                    st = trays_left if take == need_u else min(trays_left, max(1, round(tr * take / u)))
                    segs.append((lid, take, st))
                    line_remaining[(_deny, lid)] = av - take
                    need_u -= take
                    trays_left -= st
                if need_u > 0 or not segs:
                    _short = need_u if segs else u
                    segs.append((None, _short, max(0, trays_left)))
                    if _deny and _short > 0:
                        unassigned_restricted[store_id] += _short
                for (lid, su, st) in segs:
                    if su <= 0:
                        continue
                    csid = str(uuid.uuid4())
                    shelf_rows.append((csid, cid, pos, sk, pid, kt, st, su, lid))
                    if lid:
                        ledger_pending.append((oid, sk, csid, lid, su))
        for l in lns:
            line_rows.append((oid, store_id, l["sku_id"], l["alloc_units"], l["alloc_units"], None,
                              avail_meta.get(l["sku_id"], {}).get("hard_good_type"),
                              round(l["sku_fit"], 4), round(fam_vel[(store_id, l["family"])], 4)))

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO orders (id, run_id, store_id, status, priority_score, velocity_component,
            recency_component, pace_component, breadth_component, days_since_last_delivery,
            cart_count, total_units, is_one_cart_exception, pace_ratio, is_due)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, order_rows, page_size=500)
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO order_carts (id, order_id, store_id, cart_number, cart_type_id, shelves_used,
            shelves_capacity, total_units, is_full, is_flagged, flag_reason, effective_max_shelves,
            cart_key, config_code, is_template_exact)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, cart_rows, page_size=500)
    if _has_tag_col and cart_tag_rows:
        psycopg2.extras.execute_batch(cur, "UPDATE order_carts SET handling_tag = %s WHERE id = %s",
                                      cart_tag_rows, page_size=500)
    elif cart_tag_rows:
        decide("validation", f"{len(cart_tag_rows)} cart(s) carry a store handling tag but "
                             f"order_carts.handling_tag does not exist — apply the store_master "
                             f"migration so restricted carts get their own cart config", severity="block")
    for st_id2, u2 in sorted(unassigned_restricted.items()):
        decide("validation", f"{u2:,}u for a variety-restricted store could not be matched to a permitted "
                             f"availability line — assign the variety by hand; the restricted variety "
                             f"was NOT assigned", store_id=st_id2, severity="warning", units=u2)
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO cart_shelves (id, cart_id, shelf_position, sku_id, profile_id, kit_type,
            tray_count, unit_count, availability_line_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, shelf_rows, page_size=500)
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO order_lines (order_id, store_id, sku_id, recommended_qty, draft_qty,
            approved_qty, kit_type, sku_score, group_velocity_score)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT DO NOTHING
    """, line_rows, page_size=500)
    if ledger_pending:
        cur.execute("SELECT id, order_id, sku_id FROM order_lines WHERE order_id = ANY(%s::uuid[])",
                    (list(order_ids.values()),))
        ol = {(str(r["order_id"]), r["sku_id"]): r["id"] for r in cur.fetchall()}
        psycopg2.extras.execute_batch(cur, """
            INSERT INTO order_line_allocations (order_line_id, availability_line_id, cart_shelf_id,
                run_id, batch_id, sku_id, qty, source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'engine_v2')
        """, [(ol.get((oid, sk)), lid, csid, run_id, batch_id, sk, q)
              for (oid, sk, csid, lid, q) in ledger_pending], page_size=500)

    # Cart Configs templates (one per exact signature)
    first_line = {}
    for r in avail_rows:
        first_line.setdefault(r["sku_id"], str(r["id"]))
    for code, t in template.items():
        cur.execute("""
            INSERT INTO cart_config_templates (id, run_id, config_code, cart_count, store_count, total_units)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
        """, (str(uuid.uuid4()), run_id, code, t["cart_count"], len(t["stores"]), t["units"]))
        tid = str(cur.fetchone()["id"])
        for pos, e in sorted(t["shelves"].items()):
            shid = str(uuid.uuid4())
            cur.execute("""INSERT INTO cart_config_template_shelves
                           (id, template_id, shelf_position, profile_id, units_per_shelf, tray_count)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (shid, tid, pos, e["profile_id"], e["units"], e["trays"]))
            for i, (sk, tr, u) in enumerate(e["varieties"]):
                cur.execute("""INSERT INTO cart_config_template_varieties
                               (shelf_id, availability_line_id, sku_id, tray_count, unit_count, sort_order)
                               VALUES (%s,%s,%s,%s,%s,%s)""", (shid, first_line.get(sk), sk, tr, u, i))
        cur.execute("UPDATE order_carts SET template_id = %s WHERE id = ANY(%s::uuid[])", (tid, t["cart_ids"]))

    # Evidence trail: every shipped line + every gate/validation/review decision (§25 "evidence")
    for l in ship.values():
        if l.get("alloc_trays", 0) <= 0:
            continue
        decide("line", f"{l['alloc_trays']} trays: forecast {l['fc']:.1f}/wk, projected {l['projected']:.0f}u at arrival, "
                       f"need {l['need']:.0f}u", store_id=l["store_id"], sku_id=l["sku_id"],
               family=l["family"], grade=l["grade"], forecast_wk=round(l["fc"], 2), method=l["method"],
               rates={"r2": round(l["r2"], 2), "r4": round(l["r4"], 2), "r10": round(l["r10"], 2), "r13": round(l["r13"], 2)},
               onhand=round(l["onhand"], 1), onhand_conf=l["conf"], onhand_src=l["oh_src"],
               inbound=l["inbound"], inbound_src=l["inbound_src"], projected=round(l["projected"], 1),
               risk=l["risk"], need_units=round(l["need"], 1), need_trays=l["trays"],
               alloc_trays=l["alloc_trays"], upt=l["upt"], cap_note=l["cap_note"],
               conversion=l["conversion"], priority=round(l["priority"], 4), sku_fit=round(l["sku_fit"], 4),
               price=None if l["price"] is None else round(l["price"], 2), price_src=l["price_src"],
               cadence_grade=l.get("cadence_grade"), cadence_weeks=l.get("cadence_weeks"),
               coverage_weeks=round(l["cover_wk"], 2),
               shipping_efficiency=eff.get((l["store_id"], l["family"])))
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO run_decisions (id, run_id, decision_type, store_id, sku_id, reason, data, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,now())
    """, [(str(uuid.uuid4()), run_id, k, s, sk, why, json.dumps(dd, default=str))
          for (k, s, sk, why, dd) in decisions], page_size=500)

    # Latest store x family grades / service status (feeds the future DSM portal, §26)
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO ipr_store_family (store_id, ipr_family, grade, weekly_velocity, conversion_13wk,
            conversion_confidence, service_status, run_id, computed_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now())
        ON CONFLICT (store_id, ipr_family) DO UPDATE SET grade=EXCLUDED.grade,
            weekly_velocity=EXCLUDED.weekly_velocity, conversion_13wk=EXCLUDED.conversion_13wk,
            conversion_confidence=EXCLUDED.conversion_confidence, service_status=EXCLUDED.service_status,
            run_id=EXCLUDED.run_id, computed_at=now()
    """, [(s, f, grade[(s, f)], round(fam_vel[(s, f)], 3), service[(s, f)][1], service[(s, f)][2],
           service[(s, f)][0], run_id) for (s, f) in fam_vel], page_size=500)

    conn.commit()
    log(f"\nWrote run {run_id}: {len(order_rows)} orders · {len(cart_rows)} carts · {len(shelf_rows)} shelf rows · "
        f"{len(decisions):,} decisions · {len(template)} exact configs")
    return run_id


def _in_transit_lines(cur, anchor):
    """Loaded in-transit units arriving after the sales anchor.
    Returns ({(store_id, sku_id): (medium_units, low_units, methods)}, {shipping_weeks loaded})."""
    cur.execute("SELECT to_regclass('public.in_transit_lines') IS NOT NULL")
    if not cur.fetchone()[0]:
        return {}, set()
    cur.execute("SELECT DISTINCT shipping_week FROM in_transit_lines")
    weeks = {r[0] for r in cur.fetchall()}
    cur.execute("""SELECT store_id, sku_id,
                          COALESCE(SUM(units) FILTER (WHERE confidence <> 'LOW'), 0) AS med,
                          COALESCE(SUM(units) FILTER (WHERE confidence = 'LOW'), 0) AS low,
                          string_agg(DISTINCT method, '+') AS m
                   FROM in_transit_lines WHERE delivery_date > %s
                   GROUP BY store_id, sku_id""", (anchor,))
    return {(r[0], r[1]): (float(r[2]), float(r[3]), r[4]) for r in cur.fetchall()}, weeks


# Swappable for previews (feed loader output in memory without writing the table).
IN_TRANSIT_SOURCE = _in_transit_lines


if __name__ == "__main__":
    import sys
    b = sys.argv[1]
    wk = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else None
    res = run(b, wk, preview="--write" not in sys.argv)
    if isinstance(res, dict):
        print(json.dumps(res["summary"], indent=2, default=str))
