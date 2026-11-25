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
import re
import uuid
from collections import Counter, defaultdict
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

import config_merge
import demand_history
import inbound_timing
import store_lead
import demand_forecast as DF
import demand_recovery as DR
import shelfkit as SK
import stop_rules as SR
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
# Seasonal index vs a June-July typical week (measured 2026-11-24; same copy as
# services/settings_catalog.py PEAK_SEASONAL_INDEX — tests keep them equal).
PEAK_SEASONAL_INDEX = {"1": 0.79, "2": 1.01, "3": 1.28, "4": 1.39, "5": 1.33, "6": 1.0, "7": 1.0,
                       "8": 1.14, "9": 0.91, "10": 0.65, "11": 0.65, "12": 0.56}
IGNORE_RUN_IDS = set()      # PREVIEW ONLY: run ids left out of the duplicate-run control (what-ifs)
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
SCENARIO_OVERRIDES = []          # preview-only extra directives (what-if / tests); never written
DIRECTED_MAX_SHELVES = 10        # ceiling on one directed shipment per store (setting directed_max_shelves)
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


def pair_partial_shelves(shelves, frac_of, height_of, pids_of, merge, fits=None):
    """Shelf splitting (Josh 2026-11-25: "we can split shelves between 2 different items").
    Full shelves stay as they are. Each partly filled shelf is paired with at most one other
    partial shelf of a DIFFERENT product profile when the two fit on one shelf together
    (fractions <= 1), preferring the closest shelf height, then the fullest result. A shelf
    never carries more than two product profiles. Returns the new shelf list."""
    out, part = [], []
    for sh in shelves:
        (out if frac_of(sh) >= 1.0 - 1e-9 else part).append(sh)
    part.sort(key=lambda sh: (-frac_of(sh), -height_of(sh)))
    used = [False] * len(part)
    for i, a in enumerate(part):
        if used[i]:
            continue
        used[i] = True
        best = None
        for j in range(i + 1, len(part)):
            if used[j]:
                continue
            b = part[j]
            pa, pb = pids_of(a), pids_of(b)
            if pa & pb or len(pa | pb) > 2:
                continue
            f = frac_of(a) + frac_of(b)
            if (not fits(a, b)) if fits else f > 1.0 + 1e-9:
                continue
            key = (abs(height_of(a) - height_of(b)), -f)
            if best is None or key < best[0]:
                best = (key, j)
        if best:
            used[best[1]] = True
            out.append(merge(a, part[best[1]]))
        else:
            out.append(a)
    return out


def dual_use_target(supply_from, supply_to, need_from, need_to, dual_units, max_units=0):
    """Units of dual-use plants to move from the ceramic SKU to Littles: enough that both are
    covered at the same share of their need, (supply_to + x) / need_to = (supply_from - x) /
    need_from, never more than Littles are short, the dual-use stock, or max_units (0 = none)."""
    if need_to <= supply_to or dual_units <= 0:
        return 0
    if need_from > 0:
        x = (supply_from * need_to - supply_to * need_from) / float(need_to + need_from)
    else:
        x = float(need_to - supply_to)
    lim = [x, need_to - supply_to, dual_units] + ([max_units] if max_units and max_units > 0 else [])
    return int(max(0, min(lim)))


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
    timing_on = str(S.get("inbound_by_arrival_date", True)).lower() not in ("false", "0", "no")
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
                  if (r["engine_version"] == ENGINE_VERSION or r["status"] in RELEASED_RUN_STATUSES
                      or (r["pipeline_status"] or "") in RELEASED_PIPELINE)
                  and not (preview and str(r["id"]) in IGNORE_RUN_IDS)]
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

    # Availability grade (Josh 2026-11-23): grade 3 = more mature plants and contract-grower
    # stock, which must leave first; grade 2 = everything else. Grade never changes how much a
    # store is sent — only which plants are picked for it (see the line assignment at write).
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'availability_lines' AND column_name = 'grade'""")
    _has_grade = bool(cur.fetchone())
    cur.execute(f"""SELECT al.id, al.sku_id, al.units_available, al.code_level, al.hard_good_type,
                          al.plant_id, {'al.grade' if _has_grade else 'NULL::int'} AS grade
                   FROM availability_lines al WHERE al.batch_id = %s ORDER BY al.sku_id, al.id""",
                (batch_id,))
    avail_rows = cur.fetchall()
    _gf = S.get("availability_grade_first", 3)
    grade_first = int(_gf) if _gf not in (None, "", False) else 0          # 0 = no preference
    line_grade = {str(r["id"]): r["grade"] for r in avail_rows if r["grade"] is not None}
    # lower rank = used first: the preferred grade, then other grades from high to low, then ungraded
    line_rank = ({lid: (0 if g == grade_first else 1000 - int(g)) for lid, g in line_grade.items()}
                 if grade_first and line_grade else None)
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
    line_plant = {str(r["id"]): str(r["plant_id"] or "") for r in avail_rows}   # line -> Aster code
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
               k.family, k.legacy_product_group, k.program,
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

    excluded_skus = {str(x) for x in (S.get("excluded_skus") if S.get("excluded_skus") is not None
                                      else [1015443116])}       # Climb-itt Plank (Jason 11/25)
    excluded_fams = set(S.get("excluded_families") or [])   # e.g. Seasonal/Holiday: not routine replenishment
    plannable = set()
    for sku_id in supply:
        info, pk = sku_info.get(sku_id), pack.get(sku_id)
        if not info:
            decide("validation", "Availability SKU not in SKU master — excluded", sku_id=sku_id, severity="block")
        elif str(sku_id) in excluded_skus:
            decide("validation", "SKU excluded from routine replenishment by setting excluded_skus "
                                 "(Jason's Week 49 brief: no Climb-itt Plank)", sku_id=sku_id, severity="info")
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
               wa.adjustment_type, wa.value_numeric, wa.value_text,
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
    if preview and SCENARIO_OVERRIDES:
        for _i, _a in enumerate(SCENARIO_OVERRIDES):
            scn_all.append({**{k: None for k in (
                "scenario_id", "value_numeric", "value_text", "scope_merchant", "scope_region", "scope_state",
                "scope_velocity_tier", "scope_store_ids", "scope_sku_id", "scope_family",
                "scope_legacy_tier", "scenario_name", "requested_by", "approved_by")},
                "adj_id": f"preview-{_i}", "label": f"preview {_i}", **_a})
    # DIRECTED SHIPMENT (Josh 2026-11-23): "send N shelves of X to these stores", shipped whether
    # or not the store has sold X before. It is a manager-approved override that sits outside
    # the normal autonomous flow (§27), so it skips the pace trim, the 13-week gates, the due
    # test and the full-cart minimum. It never skips a hard block: do-not-ship, store-master
    # restrictions, variety restrictions and supply all still win; an approved exclusion wins
    # too, unless the directed shipment names the store by number and the exclusion does not.
    scn_directed = [a for a in scn_all if a["adjustment_type"] == "directed_shipment"]
    _scn_by_id = {a["adj_id"]: a for a in scn_all}
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
                           region, merchant, state, division, volume_code{''.join(', ' + c for c in _hb_cols)}
                    FROM stores ORDER BY store_id""")
    stores = {r["store_id"]: dict(r) for r in cur.fetchall()}
    if preview and STORE_OVERRIDES:            # what-if store attributes (preview only, never written)
        for _sid, _patch in STORE_OVERRIDES.items():
            if _sid in stores:
                stores[_sid].update(_patch)
        _hb_cols = sorted(set(_hb_cols) | {k for _p in STORE_OVERRIDES.values() for k in _p})
    # ARRIVAL PER STORE (store delivery profile, workers/store_lead.py). The engine has always
    # assumed every store receives its order 2 days after the Monday of the ship week; routed
    # history says the usual figure is 7 (North 6, South 7, West 8). With
    # arrival_from_store_profile on, each store's own usual day is used for the sales it will
    # make before the truck lands and for the due test. Off = the old flat 2 days.
    lead_on = str(S.get("arrival_from_store_profile", False)).lower() in ("true", "1", "yes")
    store_arrival, lead_summary = {}, None
    if lead_on:
        cur.execute(store_lead.MANIFEST_SQL)
        _lp, lead_summary = store_lead.build_profiles(
            [dict(r) for r in cur.fetchall()],
            {sid: {"state": s_.get("state"), "division": s_.get("division")} for sid, s_ in stores.items()})
        store_arrival = {sid: shipping_week + timedelta(days=p_["days"]) for sid, p_ in _lp.items()}
        log(f"Arrival from store delivery profiles: network {lead_summary['network_days']}d after ship-week "
            f"Monday; {lead_summary['stores_with_history']:,} stores on their own history")

    def arrival_of(sid):
        return store_arrival.get(sid, arrival)

    def dta_of(sid):
        return max(0, (arrival_of(sid) - anchor).days)

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
        if a["scope_state"] and (st_.get("state") or "").strip().upper() != a["scope_state"].strip().upper():
            return False
        if a["scope_velocity_tier"] and (st_.get("dynamic_velocity_tier") or "").strip() != a["scope_velocity_tier"]:
            return False
        return True

    def _has_store_scope(a):
        return bool(a["scope_store_ids"] or a["scope_merchant"] or a["scope_region"]
                    or a["scope_state"] or a["scope_velocity_tier"])

    def _scn_sku(a, sku_id):
        info = sku_info.get(sku_id) or {}
        if a["scope_sku_id"] and sku_id != a["scope_sku_id"]:
            return False
        if a["scope_family"] and (info.get("family") or "") != a["scope_family"]:
            return False
        if a["scope_legacy_tier"] and (info.get("legacy_product_group") or "") != a["scope_legacy_tier"]:
            return False
        return True

    # Directed shipments -> exact trays per store x SKU. A family / tier scope is spread a
    # shelf at a time across the scoped SKUs this batch can pack (most supply first). Needs
    # both a store scope and a product scope; shelves are clamped to directed_max_shelves.
    dir_max = int(S.get("directed_max_shelves", DIRECTED_MAX_SHELVES) or DIRECTED_MAX_SHELVES)
    dir_trays = {}                         # (store, sku) -> [trays, adj_id, variety codes | None, name]
    for a in scn_directed:
        # value_text "aster:1001312,3000668|Monstera Bulbasaur 17cm" = ship ONLY that variety.
        # HD SKUs are assortments; the variety is the Aster plant code on the availability line.
        _vt = str(a.get("value_text") or "")
        _codes, _vname = None, None
        if _vt.startswith("aster:"):
            _c, _, _vname = _vt[6:].partition("|")
            _codes = frozenset(x.strip() for x in _c.split(",") if x.strip()) or None
        a["_codes"], a["_vname"] = _codes, (_vname or None)
        _sh = min(dir_max, int(round(float(a["value_numeric"] or 0))))
        _skus = sorted((k for k in supply
                        if k in sku_info and (pack.get(k) or {}).get("trays_per_shelf")
                        and (pack.get(k) or {}).get("units_per_tray")
                        and (sku_info[k]["ipr_family"] or "Unmapped") != "Unmapped"
                        and (a["scope_sku_id"] or a["scope_family"] or a["scope_legacy_tier"])
                        and _scn_sku(a, k)), key=lambda k: (-supply[k], k))
        if _sh <= 0 or not _skus or not _has_store_scope(a):
            continue
        for sid in sorted(s_ for s_ in stores if _scn_store(a, s_)):
            for i in range(_sh):
                k = _skus[i % len(_skus)]
                t = int(pack[k]["trays_per_shelf"])
                e_ = dir_trays.setdefault((sid, k), [0, a["adj_id"], a["_codes"], a["_vname"]])
                e_[0] += t
                scn_fx[a["adj_id"]]["trays_requested"] += t

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
    pk_idx = {int(k): float(v) for k, v in (S.get("peak_seasonal_index") or PEAK_SEASONAL_INDEX).items()}
    pk_share = float(S.get("peak_floor_share", 0.9) or 0.9)   # Jason 11/25: 90% of stocked pace
    pk_starve = str(S.get("peak_floor_require_starvation", False)).lower() in ("true", "1", "yes")
    pk_month = (shipping_week or anchor).month
    peak_rate = {}
    _dh_on = str(S.get("demand_history", "on")).lower() not in ("off", "false", "0", "no")
    if pk_month in pk_idx and not _dh_on:
        cur.execute("""SELECT store_id, sku_id, SUM(units_sold)::float AS u FROM sales
                       WHERE sale_date BETWEEN %s AND %s GROUP BY 1, 2""", tuple(pk_win))
        _wk = ((date.fromisoformat(pk_win[1]) - date.fromisoformat(pk_win[0])).days + 1) / 7.0
        peak_rate = {(r["store_id"], r["sku_id"]): r["u"] / _wk for r in cur.fetchall() if r["u"]}
    peak_mult = pk_share * pk_idx.get(pk_month, 0.0)
    # Demand by selling opportunity (CEO review 2026-11-24, workers/demand_history.py): 13 months
    # of weekly history; rates over weeks the line could sell; the benchmark becomes June-July's
    # typical week (fallback to another reliably stocked period); comparables for new lines.
    # demand_history = "off" restores the calendar-week rates and the Apr-Jul peak window.
    # lot-age sellable curve (calibration 2026-11-20; used below for on-hand credit too)
    lot_curve = [tuple(x) for x in (S.get("sellable_share_by_age_days")
                                    or [[0, 0.60], [28, 0.25], [42, 0.14], [56, 0.07], [63, 0.05], [70, 0.0]])]
    H = None
    peak_label = "Apr-Jul peak"
    if _dh_on:
        H = demand_history.load(
            cur, anchor, fresh_of, fresh_days, DEDUP_DELIVERIES, stores,
            {k: (v.get("ipr_family") or "Unmapped") for k, v in sku_info.items()},
            (shipping_week or anchor).month, S, log,
            lot_curve=lot_curve)
        if pk_month in pk_idx:
            peak_rate = {k: b[0] for k, b in H["bench"].items() if b[0] > 0}
        peak_label = f"{H['periods'][0]} benchmark"
    # DEMAND RECOVERY ESTIMATOR (Jason's Week 49 demand method, 2026-11-25; workers/demand_recovery.py):
    # store x SKU weekly demand from a hierarchy of stocked-period evidence and stock at receipt
    # from recent receipts. recovery = it sets each line's forecast and projected stock; engine =
    # the engine's own blend (the estimator is still computed and shown on the line for review).
    est_mode = str(S.get("demand_estimator", "recovery") or "engine").lower()
    est_lag = int(float(S.get("receipt_lag_days", 10) or 10))
    est_in, est_win = ({}, None)
    if est_mode in ("recovery", "compare"):
        est_in, est_win = DR.load_inputs(cur, anchor, est_lag, DEDUP_DELIVERIES)
    est_receipt = shipping_week + timedelta(days=est_lag)
    est_receipt_day = (est_receipt - anchor).days
    est_stats = {"mode": est_mode, "receipt_lag_days": est_lag, "receipt_date": str(est_receipt),
                 "lines": 0, "lines_rate_up": 0, "lines_rate_down": 0, "sources": Counter()}
    # Stockout recovery (Jason's review 11/24, Madison's Climb-itt accessories: 215 delivered in
    # March, 188 sold by July, nothing since, and the engine planned 0 trays). The starved-line
    # floor sizes every quiet line at peak_floor_share of its benchmark because a quiet line may
    # be a dead line. A line whose history shows it SOLD THROUGH its last restock and has had
    # nothing to sell since is not dead: it is out of stock. It is planned at its benchmark x
    # this month's seasonal index (no floor-share haircut). Every gate after this still applies.
    so_on = _dh_on and str(S.get("stockout_recovery", True)).lower() not in ("false", "0", "no")
    so_min_st = float(S.get("stockout_recovery_min_sellthrough", 0.6) or 0.6)
    so_idx = pk_idx.get(pk_month, 0.0)
    # Shelf check (see the line loop): cap = ship at most oh_check_wk weeks; flag = report only.
    oh_check = str(S.get("onhand_check", "cap") or "cap").lower()
    oh_check_wk = float(S.get("onhand_check_cap_weeks", 2) or 2)
    # Per-line tray limits (Jason's Week 49 order, 11/25). H2O / Terrarium: at most
    # ht_max trays per store x SKU unless VERIFIED FULL-PRICE sales support more — most of the
    # 13-week H2O volume sold on clearance ($2-6 realized on a $19.98 bowl), and markdown sales
    # are not a reason to send more. Climb-itt accessories: at most acc_max boxes per SKU per
    # store. 0 = no limit. Directed shipments and approved scenario floors are not limited here.
    ht_max = int(float(S.get("h2o_terrarium_max_trays", 2) or 0))
    ht_fp_share = float(S.get("h2o_terrarium_fullprice_share", 0.8) or 0.8)
    acc_max = int(float(S.get("climbitt_accessory_max_boxes", 2) or 0))
    # Jason's Week 49 brief: cart completion may refill a product up to the largest earlier
    # restock the store sold through; AA/A/B stores are served first (store priority boost).
    hist_fill = str(S.get("historical_lot_fill", True)).lower() not in ("false", "0", "no")
    # SELL-DOWN HORIZON BY STORE GRADE (Jason's Week 49 brief item 10; Josh 2026-11-25): D stores
    # are allowed 70 days (10 weeks) to sell a delivery instead of the normal cadence + buffer, so a
    # low-volume store builds one full cart and is serviced less often. Store grade = the store's
    # DTS volume code (cadence grade). Never H2O/Terrarium (they keep 35 days / their own limits)
    # and never past the product's freshness window.
    sell_down = {k: float(v) for k, v in
                 ((S.get("sell_down_weeks_by_grade") if S.get("sell_down_weeks_by_grade") is not None
                   else {"D": 10})).items() if v}

    def sell_down_wk(l):
        """Extra sell-down weeks for this line's store grade (0 when none applies)."""
        if l["family"] in H2O_TERR_FAMILIES:
            return 0.0
        _w = sell_down.get(store_cadence(l["store_id"])[0], 0.0)
        return min(_w, fresh_of.get(l["sku_id"], fresh_days) / 7.0) if _w else 0.0

    # MARGINAL PACK VALUE (Jason's Week 49 agent direction 2026-11-25, section 4-5): a tray or
    # box is only planned while it adds projected sales inside the selling window:
    #   F(q,h) = min(q*f, max(0, h*a*r - s)); next pack adds F(q+p) - F(q).
    # r = the line's stocked weekly pace (fc), s = projected sellable stock at receipt, h = 35
    # days (D-store core plants / accessories 70), a = pace factor, f = sellable fraction
    # (plants 0.83, hard goods 1.0). A pack is worth sending while it adds at least
    # marginal_pack_min_share of its sellable units. Every sizing and top-up step is capped here.
    mg_on = str(S.get("marginal_allocation", True)).lower() not in ("false", "0", "no")
    mg_share = float(S.get("marginal_pack_min_share", 0.5) or 0.5)
    mg_a = float(S.get("forecast_pace_factor", DF.BASE["a"]) or DF.BASE["a"])
    mg_f = float(S.get("plant_sellable_fraction", DF.BASE["f_plant"]) or DF.BASE["f_plant"])
    cart_st_min = float(S.get("cart_min_sellthrough", 0.68) or 0)     # blended, grade horizon

    def line_horizon(l):
        return DF.horizon_weeks(store_cadence(l["store_id"])[0], l["family"],
                                d_weeks=int(sell_down.get("D", DF.D_HORIZON_WEEKS)) or DF.D_HORIZON_WEEKS)

    def line_f(l):
        return DF.BASE["f_hard"] if l.get("accessory") else mg_f

    def pack_cap(l):
        """Most trays/boxes worth sending to this line (None when marginal allocation is off)."""
        if not mg_on or l.get("directed_trays"):
            return None
        return DF.ceiling_packs(l["fc"], max(0.0, l.get("projected", 0.0)), line_horizon(l),
                                max(1, l["upt"]), mg_a, line_f(l), mg_share)

    # Josh 2026-11-25: a shelf may be split between two different products.
    split_shelves = str(S.get("split_shelves", True)).lower() not in ("false", "0", "no")
    # Josh 2026-11-25 (agrees with Jason): Climb-itt hard goods never expire, so a store that has
    # sold the accessory gets its need rounded UP to a whole box (still at most acc_max boxes).
    acc_round_up = str(S.get("accessory_round_up", True)).lower() not in ("false", "0", "no")
    acc_min_share = float(S.get("accessory_round_up_min_share", 0.5) or 0)   # share of a box the need must reach
    def lot_trays(l):
        """Trays a line may reach in the re-offer / target top-up: its own need, or up to the
        largest earlier restock the store sold through (historical_lot_fill), never past the
        line's tray limit; shelf-check and unreliable-inventory lines keep their own need."""
        t = l.get("trays", 0)
        _bl = l.get("best_lot") if hist_fill else None
        if _bl and l.get("conf") != "UNUSABLE" and not l.get("oh_conflict"):
            t = max(t, int((_bl["units"] - max(0.0, l.get("projected", 0.0))) // max(1, l["upt"])))
        if l.get("tray_cap") is not None:
            t = min(t, max(l.get("trays", 0), l["tray_cap"]))
        if l.get("mcap") is not None:
            t = min(t, max(l.get("trays", 0), l["mcap"]))
        return t

    grade_boost = {**{"AA": 0.3, "A": 0.2, "B": 0.1},
                   **{k: float(v) for k, v in (S.get("store_grade_priority_boost") or {}).items()}}

    def is_accessory(sku_id):
        """Climb-itt hard goods (wire, clips, stakes, poles, planks): the Climb-ITT family
        without a pot size in the description (the 12cm / 17cm Climb-itt SKUs are plants)."""
        i = sku_info.get(sku_id) or {}
        return (i.get("ipr_family") == "Climb-ITT"
                and not re.search(r"\d+\s*cm", str(i.get("description") or "").lower()))

    def sold_out(k):
        """(last restock, benchmark) when the line sold >= so_min_st of its last restock, that
        restock is at least 4 weeks old and nothing has sold for 4 weeks; else None. Judged on
        sales and deliveries directly: the stock model counts a line "available" indefinitely
        once it has sold past its plant lot life (hard goods), so its selling weeks cannot tell
        a sell-out here. Shelf and inbound evidence is checked at the line (DTS stock or a truck
        on the way means it is not a stockout)."""
        if not (so_on and H and so_idx > 0):
            return None
        hl_ = H["lines"].get(k)
        lot = (hl_ or {}).get("last_lot")
        if (not lot or lot["sell_through"] < so_min_st or lot["weeks_since_delivery"] < 4
                or (lot["weeks_since_sale"] is not None and lot["weeks_since_sale"] < 4)):
            return None
        b = H["bench"].get(k)
        if not b or b[0] <= 0:
            return None
        return lot, b
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

    # Full-price selling rate for H2O / Terrarium (the tray limit above): units sold at
    # >= ht_fp_share of the Home Depot retail price over the last 13 weeks, per week of
    # dollar-reported sales. No dollar data = not verified = the limit stands.
    fp_rate, fp_weeks = {}, 0.0
    if ht_max > 0:
        cur.execute("""SELECT MIN(sale_date) AS mn FROM sales
                       WHERE dollars IS NOT NULL AND sale_date > %s AND sale_date <= %s""", (d(91), anchor))
        _mn = cur.fetchone()["mn"]
        if _mn:
            fp_weeks = max(1.0, ((anchor - _mn).days + 1) / 7.0)
            cur.execute("""SELECT s.store_id, s.sku_id,
                                  SUM(s.units_sold) FILTER (WHERE s.dollars >= %s * pb.retail_price * s.units_sold) AS fp
                           FROM sales s
                           JOIN ipr_family_map fm ON fm.sku_id = s.sku_id
                           JOIN pricebook_hd pb ON pb.sku_id = s.sku_id
                           WHERE fm.ipr_family = ANY(%s) AND s.dollars IS NOT NULL AND s.units_sold > 0
                             AND pb.retail_price > 0 AND s.sale_date > %s AND s.sale_date <= %s
                           GROUP BY s.store_id, s.sku_id""",
                        (ht_fp_share, sorted(H2O_TERR_FAMILIES), d(91), anchor))
            fp_rate = {(r["store_id"], r["sku_id"]): float(r["fp"] or 0) / fp_weeks for r in cur.fetchall()}

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
    cur.execute("""SELECT store_id, MIN(delivery_date) AS d FROM delivery_manifest
                   WHERE delivery_date > %s GROUP BY 1""", (anchor,))
    manifest_date = {r["store_id"]: r["d"] for r in cur.fetchall()}
    in_transit_stores = set(manifest_date)
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
    log(f"Inbound after {anchor}: loaded in-transit {sum(v[0] + v[1] for v in it_lines.values()):,.0f}u "
        f"({sum(v[1] for v in it_lines.values()):,.0f}u LOW at {it_low:.0%}) "
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
                         AND decision_type IN ('pushed_min_cart', 'held_stop_minimum', 'not_due')""", (_pr,))
        _ph = defaultdict(lambda: [set() for _ in _pr])
        for r in cur.fetchall():
            _ph[r["store_id"]][_pr.index(r["run_id"])].add(r["decision_type"])
        for st_id, weeks in _ph.items():
            n = 0
            for kinds in weeks:
                if "pushed_min_cart" in kinds or "held_stop_minimum" in kinds:
                    n += 1            # held under the stop minimum counts as a push (need grows)
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
        _sd = sell_down_wk(l)
        if _sd > cov:
            cov = _sd                                                     # 70-day D-store horizon
        need = max(0.0, l["fc"] * cov - l["projected"])
        cap_note = None
        if l.get("inbound_later") and l.get("stock_at_arrival") is not None:
            # a later truck cannot cover the days before it lands (Jason point 3)
            _gap = inbound_timing.gap_need(l["stock_at_arrival"], l["inbound_later"], l["fc"] / 7.0, cov * 7)
            if _gap > need:
                need, cap_note = _gap, f"sized to last until the next truck ({_gap:.0f}u)"
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
        # per-line tray limit (Jason's Week 49 order): H2O/Terrarium unless full-price sales
        # support more; Climb-itt accessories always. Every later top-up step honours tray_cap.
        l["tray_cap"] = None
        l.pop("trays_before_tray_cap", None)
        if l["family"] in H2O_TERR_FAMILIES and ht_max > 0:
            _fp = float(l.get("fp_rate") or 0.0)
            l["tray_cap"] = max(ht_max, int((_fp * cov) // l["upt"]))
            _why = (f"H2O/Terrarium limit {l['tray_cap']} trays (full-price sales {_fp:.1f}u/wk over "
                    f"{cov:g} wks)" if l["tray_cap"] > ht_max else
                    f"H2O/Terrarium limit {ht_max} trays — full-price sales {_fp:.1f}u/wk do not support more")
        elif l.get("accessory") and acc_max > 0:
            if acc_round_up and l.get("acc_hist") and l["plannable"]:
                # never expire: size over the selling window (35 days, D 70), whole boxes while a box
                # sells at least accessory_round_up_min_share of itself (Jason: 504 one box, 509 two)
                trays = max(trays, DF.ceiling_packs(l["fc"], max(0.0, l.get("projected", 0.0)),
                                                    line_horizon(l), max(1, l["upt"]), mg_a, 1.0,
                                                    min_share=acc_min_share))
            l["tray_cap"] = acc_max
            _why = f"Climb-itt accessory limit {acc_max} boxes per store"
        if l["tray_cap"] is not None and trays > l["tray_cap"]:
            l["trays_before_tray_cap"] = trays
            trays = l["tray_cap"]
            cap_note = ((cap_note or "") + " " + _why).strip()
        _mc = pack_cap(l) if l["plannable"] else None
        l["mcap"] = _mc
        l.pop("trays_before_marginal", None)
        if _mc is not None and trays > _mc:
            l["trays_before_marginal"] = trays
            trays = _mc
            cap_note = ((cap_note or "") + f" next pack would add under {mg_share:.0%} of its units in "
                        f"{line_horizon(l):g} wks — sized to sellable demand").strip()
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
        _oc = l.get("oh_conflict")
        if _oc and trays and oh_check == "cap":
            _ct = max(1, int((l["fc"] * oh_check_wk) // l["upt"]))
            if trays > _ct:
                _oc["trays_before_cap"] = trays
                trays = _ct
                cap_note = ((cap_note or "") + f" shelf check: DTS shows {_oc['dts_onhand']}u and no sales in "
                            f"{_oc['quiet_weeks']} wks — capped at {oh_check_wk:g} wks until the DSM confirms "
                            f"the shelf").strip()
        if l.get("directed_trays"):                                       # exact, by approval
            trays = l["directed_trays"]
            cap_note = ((cap_note or "") + " directed shipment (manager-approved scenario)").strip()
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
            if _k not in sales and (_pk * peak_mult >= min_rate
                                    or (_pk * so_idx >= min_rate and sold_out(_k))):
                sales[_k] = {"w2": 0.0, "w4": 0.0, "w8": 0.0, "w10": 0.0, "w13": 0.0,
                             "d13": 0.0, "w13d": 0.0}
                _seeded += 1
        log(f"Peak-floor seeding: {_seeded:,} store x SKU lines with {peak_label} sales but none in 13 wks")
    for _k in dir_trays:                    # a directed store x SKU needs no sales history
        sales.setdefault(_k, {"w2": 0.0, "w4": 0.0, "w8": 0.0, "w10": 0.0, "w13": 0.0,
                              "d13": 0.0, "w13d": 0.0})
    for (store_id, sku_id), sv in sales.items():
        if sku_id not in sku_info:
            continue
        _dir = dir_trays.get((store_id, sku_id))
        is_plannable = sku_id in plannable or bool(_dir)
        st = stores.get(store_id)
        if not st:
            continue
        r2, r4, r10, r13 = sv["w2"] / 2, sv["w4"] / 4, sv["w10"] / 10, sv["w13"] / 13
        dv = dels.get((store_id, sku_id), {})
        last_del = dv.get("last_del")
        hl = H["lines"].get((store_id, sku_id)) if H else None
        if H:
            # Rates over the weeks the line could sell (CEO 2026-11-24): before the product first
            # reached the store, while the store was not reporting, or with no stock on hand, a
            # week is not a selling opportunity. A window with too few selling weeks drops out
            # of the 4/10/13 blend and the rest re-weight. This replaces the 42-day guess.
            hl = hl or {}
            parts = [(fw["w4"], hl.get("r4"), hl.get("a4", 0), 2),
                     (fw["w10"], hl.get("r10"), hl.get("a10", 0), 3),
                     (fw["w13"], hl.get("r13"), hl.get("a13", 0), 3)]
            use = [(w, r) for w, r, a, need_ in parts if r is not None and a >= need_]
            if not use and hl.get("r13") is not None:
                use = [(1.0, hl["r13"])]
            wsum = sum(w for w, _ in use)
            fc = sum(w * r for w, r in use) / wsum if wsum else 0.0
            r4 = hl.get("r4") if hl.get("r4") is not None else r4
            r10 = hl.get("r10") if hl.get("r10") is not None else r10
            r13 = hl.get("r13") if hl.get("r13") is not None else r13
            a13 = hl.get("a13", 0)
            method = ("blend 4/10/13 over selling weeks" if a13 >= 13 else
                      f"blend over {a13} selling weeks of the last 13 (no stock, not reporting, "
                      f"or not yet launched in the others)")
            if store_id in edi_gap:
                method += " · EDI gap weeks excluded"
            # Limited history (new store or new SKU): size from comparable stores (same velocity
            # code and region) for this SKU, else this family's typical SKU there; the store's
            # own selling takes over one quarter per selling week until `limited_weeks`.
            a_tot, lw = hl.get("a_total", 0), H["limited_weeks"]
            if a_tot < lw and not _dir:
                grp = H["group"].get(store_id)
                fam_k = sku_info[sku_id].get("ipr_family") or "Unmapped"
                cmp_r, cmp_src = H["cmp_sku"].get((grp, sku_id)), "this SKU"
                if cmp_r is None:
                    cmp_r, cmp_src = H["cmp_fam"].get((grp, fam_k)), f"{fam_k} family"
                if cmp_r is not None:
                    own_w = a_tot / lw
                    fc = own_w * fc + (1 - own_w) * cmp_r
                    method = (f"limited history ({a_tot} selling wk): {1 - own_w:.0%} comparable "
                              f"stores' {cmp_src} rate {cmp_r:.1f}u/wk, {own_w:.0%} own sell-down")
        else:
            fc = fw["w4"] * r4 + fw["w10"] * r10 + fw["w13"] * r13
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
            _bl = (f"{H['bench'][(store_id, sku_id)][1]} typical week" if H and (store_id, sku_id) in H["bench"]
                   else "Apr-Jul peak")
            fc, method = pf, (f"{_bl} floor ({peak_rate[(store_id, sku_id)]:.1f}u/wk x "
                              f"{peak_mult:.2f}) — recent sales held down by short supply")
        _so = None if _dir else sold_out((store_id, sku_id))
        if _so:
            _lot, _b = _so
            _sn = snap.get((store_id, sku_id))
            _dts = float(_sn["onhand_qty"] or 0) if _sn is not None and _sn["onhand_qty"] is not None else 0.0
            _it = it_lines.get((store_id, sku_id))
            _inb = ((_it[0] + _it[1]) if _it else 0) + inbound_known.get((store_id, sku_id), 0) \
                + same_week_prior.get((store_id, sku_id), 0)
            _rate = _b[0] * so_idx
            if _dts <= 0 and _inb <= 0 and store_id not in edi_gap and _rate > fc:
                fc = _rate
                _ws = _lot["weeks_since_sale"]
                method = (f"stockout recovery: sold {_lot['sold_since']:.0f} of the {_lot['delivered']:.0f} "
                          f"delivered {_lot['weeks_since_delivery']} wks ago, nothing to sell since"
                          f"{'' if _ws is None else f' (last sale {_ws} wks ago)'}; planned at {_b[1]} "
                          f"typical week {_b[0]:.1f}u/wk x {so_idx:.2f} seasonal index")
            else:
                _so = None
        _est = None
        if est_in and not _dir:
            _fw = ((H["lines"].get((store_id, sku_id)) or {}).get("fast") or {}) if H else {}
            _est = DR.line_estimate(
                est_in.get((store_id, sku_id)), est_win, store_cadence(store_id)[0],
                int((pack.get(sku_id) or {}).get("units_per_tray") or 1),
                float((sku_info.get(sku_id) or {}).get("wholesale_price_whl") or 0) or None,
                est_receipt_day, est_receipt, exact_success_pace=_fw.get("pace"), success=_fw.get("pace"))
            if _est and est_mode == "recovery":
                est_stats["lines"] += 1
                est_stats["sources"][_est["source"]] += 1
                if _est["rate"] > fc + 1e-9:
                    est_stats["lines_rate_up"] += 1
                elif _est["rate"] < fc - 1e-9:
                    est_stats["lines_rate_down"] += 1
                _prev_fc, fc = fc, _est["rate"]
                method = (f"demand recovery: {_est['source']} {fc:.1f}u/wk (baseline "
                          f"{(_est.get('baseline') or 0):.1f}, strongest {(_est.get('strongest') or 0):.1f}"
                          f"{', supply light' if _est.get('light') else ''}; engine estimate {_prev_fc:.1f})")
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
        if fc < min_rate and not _dir:
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
        # Shelf check (Jason's review 11/24, Racine H2O Bowl: DTS 31 on hand for 3 weeks, no
        # sales, the delivery ledger says 0 so the engine planned 144). When DTS reports at least
        # a week of stock that the ledger says cannot be there AND the reporting store sold none
        # of it for 2 weeks, the two sources disagree: phantom stock (ship) or stuck stock (do
        # not). Neither is proven, so the line is capped at oh_check_wk weeks and listed for a
        # DSM shelf check instead of being sized as an empty shelf.
        oh_conflict = None
        if (oh_check != "off" and conf != "UNUSABLE" and sn is not None and sn["onhand_qty"] is not None
                and not _dir and store_id not in edi_gap and store_recent.get(store_id, 0) > 0):
            _rep = float(sn["onhand_qty"])
            if _rep >= max(1.0, fc) and fresh_est < 0.5 * _rep and sv["w2"] <= 0:
                oh_conflict = {"dts_onhand": round(_rep), "ledger_onhand": round(fresh_est, 1),
                               "snapshot": str(sn["period_end"]), "quiet_weeks": 2}

        if store_id in it_stores:
            med, low, how, *_ev = it_lines.get((store_id, sku_id), (0.0, 0.0, None, []))
            inbound = med + low * it_low
            inbound_events = [((d_ - anchor).days, m_ + l_ * it_low) for d_, m_, l_ in (_ev[0] if _ev else [])
                              if d_ is not None]
            inbound_src = (f"in transit (loaded, {how})" if how
                           else "in transit (loaded) — SKU not on the truck")
            if low:
                inbound_src += (f" · LOW confidence {low:.0f}u counted at {it_low:.0%} "
                                f"({low * it_low:.0f}u) — store assignment estimated")
        else:
            inbound_events = None
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

        before_arrival = fc / 7.0 * dta_of(store_id)
        _D = dta_of(store_id)
        if inbound_events is None:
            # undated inbound (prior exported run, manifest estimate): the store's manifest
            # delivery date when known, else with our own truck (the old assumption)
            _md = manifest_date.get(store_id)
            _day = min(_D, (_md - anchor).days) if _md else _D
            inbound_events = [(_day, inbound)] if inbound else []
        dup = same_week_prior.get((store_id, sku_id), 0)        # duplicate-run control (§24)
        if dup:
            inbound_src = (f"{inbound_src} + {dup}u earlier run, same ship week" if inbound
                           else f"{dup}u earlier run, same ship week (duplicate-run control)")
            inbound += dup
            inbound_events = inbound_events + [(_D, float(dup))]
        # Inbound by arrival date (Jason point 3): walk stock forward; lost sales never consume a
        # later truck, and a truck after our delivery is kept separate for the gap check.
        if timing_on:
            stock_at_arrival, inbound_later, lost_before = inbound_timing.simulate(
                oh_adj, fc / 7.0, _D, inbound_events)
            projected = stock_at_arrival + sum(u for _, u in inbound_later)
        else:
            stock_at_arrival, inbound_later, lost_before = None, [], 0.0
            projected = max(0.0, oh_adj + inbound - before_arrival)
        if _est and est_mode == "recovery":
            projected = float(_est.get("stock") or 0.0)      # recent receipts depleted to receipt (credited once)
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
            stock_at_arrival=stock_at_arrival, inbound_later=inbound_later, lost_before_arrival=lost_before,
            old_projected=max(0.0, oh_adj + inbound - before_arrival),
            plannable=is_plannable, oh_conflict=oh_conflict,
            fp_rate=(fp_rate.get((store_id, sku_id), 0.0) if fam in H2O_TERR_FAMILIES else None),
            accessory=is_accessory(sku_id),
            acc_hist=bool(sv["w13"] > 0 or (((H["lines"].get((store_id, sku_id)) or {}).get("last_lot") or {})
                                             .get("sold_since", 0) > 0 if H else False)),
            best_lot=((H["lines"].get((store_id, sku_id)) or {}).get("best_lot") if H else None),
            demand_estimate=({k: (round(v, 3) if isinstance(v, float) else v) for k, v in _est.items()}
                             if _est else None),
            stockout_recovery=(dict(_so[0], benchmark=round(_so[1][0], 2), period=_so[1][1]) if _so else None))
        # scenario floor/cap, computed ONCE here (value_numeric is SHELVES) and pre-bounded to
        # the store's selling pace — §17's 13-week window for H2O/Terrarium, the sell-through
        # cover limit otherwise. "Add 2 shelves of bowls in the South" never ships more than
        # the store's pace supports; the trim is counted and audited (scenario_applied).
        l_ = lines[(store_id, sku_id)]
        if _dir:
            l_["directed_trays"], l_["directed_adj"] = _dir[0], _dir[1]
            if _dir[2]:
                l_["variety_codes"], l_["variety_name"] = _dir[2], (_dir[3] or "named variety")
            scn_fx[_dir[1]]["lines"] += 1
            scn_fx[_dir[1]]["stores"].add(store_id)
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
        due, nxt = due_status(store_last_del.get(st_id), arrival_of(st_id), cw, st_id in crit_stores)
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
            is_due = due_status(store_last_del.get(st_id), arrival_of(st_id), store_cadence(st_id)[1],
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
        due, nxt = due_status(store_last_del.get(st_id), arrival_of(st_id), cw, st_id in crit_stores)
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
    # CORRECTIVE TESTS (spec V2 §11.3, §24; CEO review 2026-11-23): a 13-week result below the
    # stop line "does not receive another identical routine shipment", but it is a demand signal,
    # not a universal block. Where the weak result has an explainable cause the family ships a
    # capped test instead of stopping, with no manual approval (§11.3):
    #   AA / A family grade      always a test, and flagged for investigation (§31 alert)
    #   any grade, near miss     within `corrective_test_near_miss_points` of the stop line AND
    #                            the product's recent sales were not a fair read (likely stocked
    #                            out, or a sales-reporting gap)
    # Test size = `corrective_test_share` of the normal quantity, at least one tray; reviewed
    # four weeks after it lands. Everything else stays stopped.
    ct_on = str(S.get("corrective_test_13wk", True)).lower() not in ("false", "0", "no")
    ct_share = float(S.get("corrective_test_share", 0.5) or 0.5)
    ct_near = float(S.get("corrective_test_near_miss_points", 0.10) or 0.10)
    ct = {"lines": 0, "units": 0, "units_held_back": 0, "top_grade_lines": 0, "near_miss_lines": 0,
          "alerts": 0, "enabled": ct_on}
    ct_alerted = set()
    gate_mode = str(S.get("conversion_gate_mode", "flag_only") or "flag_only").lower()
    flagged = {"mode": gate_mode, "stop": {"lines": 0, "units": 0}, "reduce": {"lines": 0, "units": 0}}
    rule_overrides = defaultdict(list)       # store -> [(sku, the rule reason that was overridden)]
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
            # "when directed" = an approved scenario that names this store (not an exclusion)
            _directed = bool(l.get("directed_trays")) or bool(_rule.get("directed_only")) and any(
                a["scope_store_ids"] and l["store_id"] in a["scope_store_ids"]
                and a not in scn_excl and a not in scn_unsupported and _scn_sku(a, l["sku_id"])
                for a in scn_all)
            _ok, _why = store_rules.sku_allowed(_rule, sku_info.get(l["sku_id"]), directed=_directed)
            if not _ok and l.get("directed_trays") and store_rules.directed_overrides(_rule):
                # an approved directed shipment is an exception to the store's day-to-day
                # product rule; the override is recorded per store (rule_overrides below)
                _ok = True
                rule_overrides[l["store_id"]].append((l["sku_id"], _why))
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
        # A directed shipment that names this store by number is the more specific instruction:
        # it wins over a broader exclusion (territory, region, state, all stores) for its own
        # product only (Josh 2026-11-24: "no Chris Rodriguez stores until 12/15 EXCEPT the
        # stores listed on the Bulbasaur order"). Everything else at the store stays excluded,
        # and an exclusion that itself lists the store by number still wins.
        if _ex and l.get("directed_trays"):
            _da = _scn_by_id.get(l.get("directed_adj"))
            if (_da and l["store_id"] in (_da.get("scope_store_ids") or ())
                    and l["store_id"] not in (_ex.get("scope_store_ids") or ())):
                decide("scenario", f"Directed shipment '{_da['label']}' names this store, so it ships "
                                   f"despite '{_ex['label']}' (that exclusion still applies to every "
                                   f"other product here)", store_id=l["store_id"], sku_id=l["sku_id"],
                       severity="info")
                _ex = None
        if _ex:
            held[key] = (f"excluded by approved scenario '{_ex['label']}'"
                         + (f" (approved by {_ex['approved_by']})" if _ex.get("approved_by") else ""))
            held_kind[key] = "scenario_exclusion"
            scn_fx[_ex["adj_id"]]["lines"] += 1
            scn_fx[_ex["adj_id"]]["stores"].add(l["store_id"])
            blocked_units += l["trays"] * l["upt"]
            continue
        if l.get("directed_trays"):          # approved override: no due test, no 13-week gate
            ship[key] = l
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
        if gate_mode == "flag_only" and svc in ("BLOCKED", "REVIEW"):
            # FLAG ONLY (Jason's review point 2; Josh 2026-11-24, departs from V2 doc §11.3):
            # a weak 13-week result is reported, not acted on. The line keeps its full quantity.
            # Hard blocks above (inactive / do-not-ship, store rules, variety bans, approved
            # exclusions, approved assortment) and the due test still apply.
            _thr = hard_block if svc == "BLOCKED" else float(conv_thr[g])
            decide("flag_13wk", f"{l['family']} sold {conv:.0%} of its last deliveries by 13 weeks "
                                f"({'below the stop line' if svc == 'BLOCKED' else f'below the {g} threshold'} "
                                f"{_thr:.0%}) — FLAGGED, not cut: ships the full {l['trays']} tray(s) "
                                f"(13-week gate in flag-only mode)",
                   store_id=l["store_id"], sku_id=l["sku_id"], family=l["family"], grade=g,
                   conversion=conv, threshold=_thr, would_have=("stop" if svc == "BLOCKED" else "reduce"),
                   need_trays=l["trays"], upt=l["upt"], severity="warning")
            l["flag_13wk"] = "stop" if svc == "BLOCKED" else "reduce"
            flagged[l["flag_13wk"]]["lines"] += 1
            flagged[l["flag_13wk"]]["units"] += l["trays"] * l["upt"]
            ship[key] = l
            continue
        if svc == "BLOCKED" and ct_on:
            _top = g in ("AA", "A")
            _unfair = ("stocked out" in l["method"]) or ("EDI gap" in l["method"])
            _nearm = conv is not None and conv >= hard_block - ct_near and _unfair
            if _top or _nearm:
                new_trays = max(1, int(math.floor(l["trays"] * ct_share)))
                _why = ("an AA/A family is investigated, not stopped" if _top else
                        f"within {ct_near * 100:.0f} points of the stop line and recent sales were not a "
                        f"fair read ({'likely stocked out' if 'stocked out' in l['method'] else 'sales-reporting gap'})")
                _rev = arrival_of(l["store_id"]) + timedelta(days=28)
                decide("corrective_test",
                       f"{l['family']} sold {conv:.0%} of its last deliveries by 13 weeks (stop line "
                       f"{hard_block:.0%}). Ships a capped corrective test, not a stop: {new_trays} of "
                       f"{l['trays']} trays — {_why}. Review {_rev} (spec §11.3).",
                       store_id=l["store_id"], sku_id=l["sku_id"], family=l["family"], grade=g,
                       conversion=conv, need_trays=l["trays"], test_trays=new_trays, upt=l["upt"],
                       cause="top_grade" if _top else "near_miss_unfair_read", review_date=str(_rev),
                       severity="warning" if _top else "info")
                ct["lines"] += 1
                ct["units"] += new_trays * l["upt"]
                ct["units_held_back"] += (l["trays"] - new_trays) * l["upt"]
                ct["top_grade_lines" if _top else "near_miss_lines"] += 1
                if _top and fk not in ct_alerted:
                    ct_alerted.add(fk)
                    ct["alerts"] += 1
                    decide("grade_alert",
                           f"{g} store family below the 13-week stop line: {l['family']} sold {conv:.0%} "
                           f"of its last deliveries (stop line {hard_block:.0%}). A capped test is shipping; "
                           f"the store needs a look — wrong items, timing, pricing or display (spec §31).",
                           store_id=l["store_id"], family=l["family"], grade=g, conversion=conv,
                           severity="warning")
                l["trays"], l["corrective_test"] = new_trays, True
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
               next_due=nxt, arrival=arrival_of(st_id), lines=len(ls),
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
    for st_id, ov in sorted(rule_overrides.items()):
        _r = store_rule.get(st_id) or {}
        decide("store_rule_override",
               f"Store master rule '{_r.get('label')}' OVERRIDDEN for a manager-approved directed "
               f"shipment of {len(ov)} SKU(s). The rule still applies to everything else at this store.",
               store_id=st_id, severity="warning", rule=_r.get("code"), rule_label=_r.get("label"),
               skus=sorted(s_ for s_, _w in ov))
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
        f"(store status) · {n_stop:,} stopped (13-wk < {hard_block:.0%}) · 13-wk gate mode {gate_mode}: "
        f"{flagged['stop']['lines']:,} lines / {flagged['stop']['units']:,}u flagged instead of stopped, "
        f"{flagged['reduce']['lines']:,} / {flagged['reduce']['units']:,}u flagged instead of reduced")

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
        l["priority"] += grade_boost.get((stores.get(l["store_id"]) or {}).get("volume_code"), 0.0)
        if l.get("directed_trays"):
            l["priority"] += 10.0            # filled before routine need when supply is short
        l["sku_fit"] = (sf_w["sku_velocity"] * (l["fc"] / max(1e-9, store_max_fc[l["store_id"]]))
                        + sf_w["mature_delivery_sales"] * conv01
                        + sf_w["projected_need"] * min(1.0, l["need"] / max(1e-9, l["fc"] * l["cover_wk"]))
                        + sf_w["seasonal"] * 0.0 + sf_w["comparable_fit"] * GRADE_SCORE[l["grade"]])
        for _a in scn_boost:                 # approved scenario priority boosts, clamped
            if _scn_store(_a, l["store_id"]) and _scn_sku(_a, l["sku_id"]):
                l["priority"] += max(0.0, min(scn_boost_cap, float(_a["value_numeric"] or 0)))
                scn_fx[_a["adj_id"]]["lines"] += 1
                scn_fx[_a["adj_id"]]["stores"].add(l["store_id"])

    # H2O + TERRARIUM PER STORE (Jason's Week 49 brief: "the conservative combined cap of two
    # H2O/terrarium trays per store"): the store's H2O and Terrarium lines together get at most
    # ht_store_max trays, highest-forecast first, unless full-price sales lifted a line's own limit.
    ht_store_max = int(float(S.get("h2o_terrarium_store_max_trays", 2) or 0))
    if ht_store_max > 0:
        _ht = defaultdict(list)
        for l in ship.values():
            if l["family"] in H2O_TERR_FAMILIES and not l.get("directed_trays") and l["trays"] > 0:
                _ht[l["store_id"]].append(l)
        for _st, _ls in _ht.items():
            if any((l.get("tray_cap") or 0) > ht_max for l in _ls):
                continue                      # verified full-price seller: per-line limits only
            left = ht_store_max
            for l in sorted(_ls, key=lambda x: (-x["fc"], x["sku_id"])):
                t = min(l["trays"], left)
                if t < l["trays"]:
                    l["trays_before_tray_cap"] = l.get("trays_before_tray_cap") or l["trays"]
                    l["cap_note"] = ((l.get("cap_note") or "") +
                                     f" H2O/Terrarium store limit {ht_store_max} trays combined").strip()
                l["trays"], l["tray_cap"] = t, t
                left -= t
        ship = {k: l for k, l in ship.items() if l["trays"] > 0 or l.get("directed_trays")}

    # DUAL-USE CONVERSION (Jason's Week 49 order, 11/25): Aster plant lines named
    # "... Everyday/Ceramic" on the 9cm Ceramic SKU are the same 9cm plant the Littles program
    # packs. When Littles are short of need and Ceramic has room, whole lines move to Littles
    # until both are covered at the same share of their need (or the dual-use stock / the
    # setting's maximum runs out). Whole lines only, so every Aster line stays under one HD SKU.
    # Operations confirms the physical repack before release (decision dual_use_conversion).
    dual = {"mode": str(S.get("dual_use_conversion", "auto") or "off").lower(), "from_sku": None,
            "to_sku": None, "dual_use_units": 0, "target_units": 0, "converted_units": 0, "lines": []}
    _prog = lambda p: [s_ for s_, i in sku_info.items() if (i.get("program") or "") == p and s_ in supply]
    _f, _t = _prog("Littles Ceramic"), _prog("Littles")
    if dual["mode"] == "auto" and len(_f) == 1 and len(_t) == 1:
        _f, _t = _f[0], _t[0]
        dual.update(from_sku=_f, to_sku=_t)
        _want = lambda sk: sum(l["trays"] * l["upt"] for l in ship.values() if l["sku_id"] == sk)
        wF, wT, sF, sT = _want(_f), _want(_t), supply.get(_f, 0), supply.get(_t, 0)
        _dl = [[lid, av] for lid, av in aster_lines.get(_f, [])
               if "everyday/ceramic" in (plant_name.get(line_plant.get(lid, ""), "") or "").lower() and av > 0]
        dual["dual_use_units"] = sum(av for _l, av in _dl)
        dual.update(need_from=wF, need_to=wT, supply_from=sF, supply_to=sT)
        if wT > sT and _dl:
            x = dual_use_target(sF, sT, wF, wT, dual["dual_use_units"],
                                int(float(S.get("dual_use_max_units", 0) or 0)))
            dual["target_units"] = x
            for lid, av in sorted(_dl, key=lambda e: (-e[1], e[0])):
                if dual["converted_units"] + av > x:
                    continue
                g = line_genus.get(lid, "")
                aster_lines[_f] = [e for e in aster_lines[_f] if e[0] != lid]
                aster_lines[_t].append([lid, av])
                pools.rem[_f][g] -= av
                pools.total[_f][g] -= av
                pools.add(_t, g, av)
                supply[_f] -= av
                supply[_t] += av
                dual["converted_units"] += av
                dual["lines"].append({"line_id": lid, "plant": line_plant.get(lid),
                                      "name": plant_name.get(line_plant.get(lid, ""), ""), "units": av})
            aster_lines[_t].sort(key=lambda e: (-e[1], e[0]))
        if dual["converted_units"]:
            decide("dual_use_conversion",
                   f"{dual['converted_units']:,} 9cm dual-use (Everyday/Ceramic) plants on "
                   f"{len(dual['lines'])} Aster line(s) moved from {_f} Littles Ceramic to {_t} Littles: "
                   f"Littles need {wT:,}u vs {sT:,}u supply, Ceramic need {wF:,}u vs {sF:,}u. Operations: "
                   f"confirm these plants can be repacked as Littles before release.",
                   sku_id=_t, severity="warning", units=dual["converted_units"],
                   from_sku=_f, lines=dual["lines"])
        log(f"Dual-use conversion: {dual['converted_units']:,}u of {dual['dual_use_units']:,}u dual-use "
            f"ceramic moved to Littles (target {dual['target_units']:,}u; Littles need {wT:,}u / supply {sT:,}u, "
            f"Ceramic need {wF:,}u / supply {sF:,}u)")

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
        # Named-variety directed shipments first: they may only use that variety's lines, and
        # what they take is removed from the SKU's general pool before anyone else draws.
        _vleft = {}
        for l in lst:
            vc = l.get("variety_codes")
            if not vc:
                continue
            if vc not in _vleft:
                _vleft[vc] = sum(av for lid, av in aster_lines.get(sku_id, []) if line_plant.get(lid) in vc)
            deny = store_deny.get(l["store_id"], NO_DENY)
            _vg = {line_genus.get(lid, "") for lid, _av in aster_lines.get(sku_id, []) if line_plant.get(lid) in vc}
            can = 0 if (deny & _vg) else min(l["trays"], _vleft[vc] // l["upt"])
            if can < l["trays"]:
                can = 0          # all or nothing per store: a named shipment is not sent part-filled
            l["alloc_trays"], l["alloc_units"] = int(can), int(can) * l["upt"]
            _vleft[vc] -= l["alloc_units"]
            for _g in sorted(_vg):                       # take it out of that genus's pool
                _t = min(l["alloc_units"], pools.rem[sku_id].get(_g, 0))
                pools.rem[sku_id][_g] -= _t
            if can < l["trays"]:
                short = (l["trays"] - can) * l["upt"]
                unfilled[sku_id] += short
                l["short_units"] = short
                l["variety_short"] = True
        for l in lst:
            if l.get("variety_codes"):
                continue
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
        if l.get("directed_trays"):
            # a directed shipment is exact: its shortfall is never made up with something
            # else, and it is never topped up as a substitute for another line
            continue
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
                        and l["conf"] != "UNUSABLE" and not l.get("oh_conflict") and l["need"] > 0
                        and pools.left(l["sku_id"], gdeny) > 0),
                       key=lambda x: (-x["fc"], x["sku_id"]))
        for c in cands:
            if fam_need - fam_alloc < c["upt"]:
                continue
            add = min(c["trays"] - c.get("gap_fill_trays", 0),           # selling-window guard
                      pools.left(c["sku_id"], gdeny) // c["upt"],
                      (fam_need - fam_alloc) // c["upt"])
            if c.get("tray_cap") is not None:                             # per-line tray limit
                add = min(add, c["tray_cap"] - c["alloc_trays"])
            if c.get("mcap") is not None:                                 # next pack must add sales
                add = min(add, c["mcap"] - c["alloc_trays"])
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

    directed_stores = set()
    _vmiss = defaultdict(lambda: [0, 0, None])
    _vleft = defaultdict(list)           # adjustment -> stores left out, for the ranked list
    _store_wk = defaultdict(float)       # store -> forecast weekly sales, all products ($)
    for l in lines.values():
        _store_wk[l["store_id"]] += l["fc"] * usd(l)
    for l in ship.values():
        if l.get("variety_short"):
            _vmiss[l["directed_adj"]][0] += 1
            _vmiss[l["directed_adj"]][1] += l["short_units"]
            _vmiss[l["directed_adj"]][2] = l["variety_name"]
        if l.get("directed_trays") and l.get("alloc_trays", 0) < l["directed_trays"]:
            _vleft[l["directed_adj"]].append({
                "store_id": l["store_id"], "store_name": (stores.get(l["store_id"]) or {}).get("store_name"),
                "state": (stores.get(l["store_id"]) or {}).get("state"),
                "units_short": (l["directed_trays"] - l.get("alloc_trays", 0)) * l["upt"],
                "value_short": round((l["directed_trays"] - l.get("alloc_trays", 0)) * l["upt"] * usd(l), 2),
                "store_weekly_sales": round(_store_wk[l["store_id"]])})
    for _aid, (_n, _u, _nm) in sorted(_vmiss.items()):
        decide("validation", f"Directed shipment of {_nm}: {_u:,} units for {_n} store(s) could not be "
                             f"filled — this availability does not have enough of that variety. Nothing "
                             f"else was substituted.", severity="warning", variety=_nm, units=_u, stores=_n)
    for _aid, _rows in sorted(_vleft.items()):
        # Stores left out, strongest first: the order to fill them in if more product arrives.
        _rows.sort(key=lambda r: (-r["store_weekly_sales"], r["store_id"]))
        _lab = next((a["label"] for a in scn_all if str(a.get("adj_id")) == str(_aid)), "directed shipment")
        decide("directed_left_out",
               f"'{_lab}': {len(_rows)} store(s) left out or short, {sum(r['units_short'] for r in _rows):,} "
               f"units (${sum(r['value_short'] for r in _rows):,.0f}). The product is not available. Ranked "
               f"by the store's weekly sales, strongest first — fill in this order if more arrives.",
               severity="warning", adjustment_id=str(_aid), label=_lab, left_out=_rows)
    for l in ship.values():
        if l.get("directed_trays"):
            _g = min(l.get("alloc_trays", 0), l["directed_trays"])
            scn_fx[l["directed_adj"]]["trays_granted"] += _g
            scn_fx[l["directed_adj"]]["trays_trimmed"] += l["directed_trays"] - _g
            if _g > 0:
                directed_stores.add(l["store_id"])
    for k_, why_ in held.items():           # a directed line a hard block stopped
        if lines[k_].get("directed_trays"):
            scn_fx[lines[k_]["directed_adj"]]["trays_trimmed"] += lines[k_]["directed_trays"]
    for st_id in sorted(directed_stores):
        _dl = [l for l in ship.values() if l["store_id"] == st_id and l.get("directed_trays")]
        decide("directed_shipment",
               f"Directed shipment (manager-approved scenario): {sum(l['alloc_trays'] for l in _dl)} "
               f"tray(s) / {sum(l['alloc_units'] for l in _dl):,}u across {len(_dl)} SKU(s), shipped "
               f"regardless of sales history; exempt from the due test, 13-week gates and the "
               f"full-cart minimum",
               store_id=st_id, severity="info", skus=sorted(l["sku_id"] for l in _dl),
               trays=sum(l["alloc_trays"] for l in _dl), units=sum(l["alloc_units"] for l in _dl))
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
        _why_trim = ("held by hard blocks (do-not-ship, store-master rules) or short supply"
                     if a["adjustment_type"] == "directed_shipment" else "to the stores' selling pace")
        trim = (f"; requested {fx['trays_requested']} trays, granted {fx['trays_granted']}, "
                f"trimmed {fx['trays_trimmed']} {_why_trim}"
                if a["adjustment_type"] in ("allocation_floor", "allocation_cap", "directed_shipment") else "")
        decide("scenario_applied",
               f"Scenario '{a['label']}' ({a['adjustment_type']}): {fx['lines']} line(s) at "
               f"{len(fx['stores'])} store(s){trim}"
               + ("" if fx["lines"] else
                  " — did nothing: it must name both stores and a product this availability can pack"
                  if a["adjustment_type"] == "directed_shipment" else
                  " — matched nothing (no store in scope sells at pace)"),
               severity="info" if fx["lines"] else "warning",
               scenario_id=a["scenario_id"], applied=fx["lines"] > 0,
               adjustment_type=a["adjustment_type"], lines=fx["lines"], stores=len(fx["stores"]),
               trays_requested=fx["trays_requested"], trays_granted=fx["trays_granted"],
               trays_trimmed=fx["trays_trimmed"], requested_by=a["requested_by"],
               approved_by=a.get("approved_by"))

    # ══ 6c. CART COMPLETION — qualified gap-fill to one full cart (CEO review, 2026-11-23) ══
    # "Keep the full-cart requirement, but add qualified gap-fill logic. A store should not be
    # rejected because its initial demand calculation fills only part of the cart." A due store
    # whose order would not fill one cart gets trays added, best product first, until one cart
    # is full. Spec V2 §21 bounds it: the product must have forecast demand AT THAT STORE and
    # the store may not exceed its approved selling window. Here that window is a weeks-of-supply
    # ceiling by store-family grade (gap_fill_weeks_supply_by_grade: looser for AA/A, tighter for
    # C/D), never past the freshness window or the §17 H2O/Terrarium 13-week limit. Products
    # stopped at the 13-week review, store-rule blocks and variety bans stay out. If no qualified
    # product can complete the cart, nothing is added and the order is pushed as before.
    # Phase 1 uses the store's own selling SKUs; comparable-store and test items come later.
    completion = {"stores_tried": 0, "stores": 0, "trays": 0, "units": 0, "wholesale": 0.0,
                  "stores_short": 0, "enabled": False}
    _cc_on = str(S.get("cart_completion", True)).lower() not in ("false", "0", "no")
    if min_full_carts > 0 and _cc_on:
        completion["enabled"] = True
        _wos_set = S.get("gap_fill_weeks_supply_by_grade") or {}
        _wos_def = {"AA": 10, "A": 10, "B": 8, "C+": 8, "C": 7, "C-": 6, "D": 6}
        _cc_consol = str(S.get("cart_consolidate", True)).lower() not in ("false", "0", "no")

        def _cc_best_fill(state):
            """Fill of the fullest cart the §7 packer would build from {profile: [trays, cap,
            height, max shelves]} — the same shelf and cart rules, on tray counts alone."""
            return max(_cc_cart_fills(state), default=0.0)

        def _cc_cart_fills(state, full_only=False):
            """Fill of EVERY cart the §7 packer would build from the state (see _cc_best_fill).
            full_only: partial shelves left out — the carts as they stand once every partial shelf
            is cut (the every-shelf-full rule), so a build judged on them survives that pass."""
            # (pids, fill by real width, height, {pid: max shelves}, [(pid, trays)]) — the same
            # ShelfKit width maths the packer uses, so the estimate and the real pack agree
            shelves = []
            for pid, (t, cap, h, pmax) in state.items():
                full, rem = divmod(int(t), cap)
                shelves += [(frozenset([pid]), SK.shelf_fill([(pid, cap)]), h, {pid: pmax}, [(pid, cap)])] * full
                if rem:
                    shelves.append((frozenset([pid]), SK.shelf_fill([(pid, rem)]), h, {pid: pmax}, [(pid, rem)]))
            if split_shelves:
                shelves = pair_partial_shelves(
                    shelves, lambda x: x[1], lambda x: x[2], lambda x: x[0],
                    lambda a, b: (a[0] | b[0], SK.shelf_fill(a[4] + b[4]), max(a[2], b[2]),
                                  {**a[3], **b[3]}, a[4] + b[4]),
                    fits=lambda a, b: SK.fits_shelf(a[4] + b[4]))
            if full_only:
                shelves = [x for x in shelves if SK.shelf_is_full(x[4])]
            _rk = lambda x: min(PROFILE_HEIGHT_RANK.get(p_, 3) for p_ in x[0])
            shelves.sort(key=lambda x: (_rk(x), sorted(x[0])))
            if _cc_consol:
                shelves.sort(key=lambda x: (-x[2], _rk(x), sorted(x[0])))
            carts = []
            for pids, frac, h, pmx, _its in shelves:
                fits = [c for c in carts
                        if len(c["sh"]) < min(CART_SHELVES, SK.max_shelves(set(c["pc"]) | set(pids)))
                        and c["h"] + h <= CART_CEILING_IN and all(c["pc"][p_] < pmx[p_] for p_ in pids)]
                if fits:
                    c = (min(fits, key=lambda c: (CART_CEILING_IN - c["h"] - h, len(c["sh"])))
                         if _cc_consol else fits[0])
                else:
                    c = {"sh": [], "h": 0.0, "pc": defaultdict(int)}
                    carts.append(c)
                c["sh"].append(frac)
                c["h"] += h
                for p_ in pids:
                    c["pc"][p_] += 1
            fills = []
            for c in carts:
                hit = c["h"] + MIN_SHELF_CLEAR_IN > CART_CEILING_IN
                _mx = min(CART_SHELVES, SK.max_shelves(set(c["pc"])))
                slots = len(c["sh"]) if (hit or len(c["sh"]) >= _mx) else _mx
                fills.append(sum(c["sh"]) / slots)
            return fills

        def _cc_prof(sku_id):
            pk = pack[sku_id]
            return (pk["profile_id"], int(pk["trays_per_shelf"]),
                    float(pk["clear_height_in"] or MIN_SHELF_CLEAR_IN), int(pk["max_shelves"] or CART_SHELVES))

        _cc_by_store = defaultdict(list)
        for l in ship.values():
            if (l["plannable"] and not l.get("variety_codes") and not l.get("directed_trays")
                    and l["sku_id"] in pack
                    and (sku_info[l["sku_id"]].get("family") or "") != "Prebuilt Cart"):
                _cc_by_store[l["store_id"]].append(l)      # routine lines only (directed ship as approved)
        # Jason 11/25: completion must consider EVERY product with supported historical demand at the
        # store, not only the SKUs base sizing gave trays. Lines with demand evidence whose base order
        # rounded to zero join the pool (gates, store rules, blocked families, shelf checks and
        # unreliable inventory still exclude; H2O/Terrarium are never filler).
        cc_extra_added = 0
        for (st_, sk_), l in lines.items():
            if (st_, sk_) in ship or st_ not in _cc_by_store or sk_ not in pack:
                continue
            if (not l.get("plannable") or l.get("variety_codes") or l.get("directed_trays") or l["fc"] <= 0
                    or l.get("oh_conflict") or l.get("conf") == "UNUSABLE" or l.get("reactivation")
                    or l["family"] in H2O_TERR_FAMILIES
                    or (sku_info[sk_].get("family") or "") == "Prebuilt Cart" or (st_, sk_) in held):
                continue
            if (service.get((st_, l["family"])) or ("",))[0] == "BLOCKED":
                continue
            _r = store_rule.get(st_)
            if _r and not store_rules.sku_allowed(_r, sku_info[sk_], directed=False)[0]:
                continue
            _ev = l.get("demand_estimate") or {}
            if not (_ev.get("rate") or l.get("best_lot") or (l.get("sales13") or 0) > 0):
                continue                             # demand must be evidenced, not assumed
            l.setdefault("alloc_trays", 0)
            l.setdefault("alloc_units", 0)
            l.setdefault("priority", min((x.get("priority", 0) for x in _cc_by_store[st_]), default=0))
            l.setdefault("sku_fit", 0.0)
            # fields the gate step sets on shipping lines
            l.setdefault("grade", grade.get((st_, l["family"]), _cc_by_store[st_][0].get("grade")))
            l.setdefault("service", "ELIGIBLE")
            l.setdefault("conversion", None)
            l.setdefault("conv_conf", "candidate added by cart completion")
            l.setdefault("reactivation", None)
            l.setdefault("trays_unreduced", 0)
            l["cc_candidate"] = True
            _cc_by_store[st_].append(l)
            cc_extra_added += 1
        completion["extra_candidates"] = cc_extra_added

        def _cc_state(ls, trays_of):
            state = {}
            for l in ls:
                t = trays_of(l)
                if t > 0:
                    pid, cap, h, pmax = _cc_prof(l["sku_id"])
                    state.setdefault(pid, [0, cap, h, pmax])[0] += t
            return state

        # Short stores: the order as allocated would not fill one cart, so today all of it is
        # pushed and the stock it was holding ships nowhere. Hand that stock back, then serve
        # the short stores best first: each gets its own need back while stock lasts, then
        # qualified gap-fill, and is kept only if one cart ends up full.
        short = []
        for st_id, ls in _cc_by_store.items():
            # stores with a directed shipment too (Josh 11/25): their ROUTINE lines must make a cart
            # of at least min fill from demand-backed product, or the routine cart does not ship
            if not any(l.get("alloc_trays", 0) > 0 for l in ls):
                continue
            if _cc_best_fill(_cc_state(ls, lambda l: l.get("alloc_trays", 0))) < min_cart_fill:
                short.append(st_id)
        short.sort(key=lambda st: (-max(l.get("priority", 0) for l in _cc_by_store[st]), st))
        orig = {}
        for st_id in short:
            deny = store_deny.get(st_id, NO_DENY)
            for l in _cc_by_store[st_id]:
                if l.get("alloc_trays", 0) > 0:
                    orig[(st_id, l["sku_id"])] = l["alloc_trays"]
                    pools.give_back(l["sku_id"], l["alloc_units"], deny)
        completion["stores_tried"] = len(short)
        # TEST ITEMS (spec V2 §21 items 3 and 5; CEO review): when a store's own products cannot
        # finish the cart, a small number of products it has NOT sold may ride along, chosen from
        # what comparable stores sell (same overall grade and region, at least `_cmp_min` of them
        # selling it). One tray per product, at most `_cmp_max` products, and never more than the
        # grade's share of one cart (AA/A 20%, B 10%, C and D none). Stopped families, store rules,
        # variety bans, the approved assortment, hard goods and seasonal items stay out.
        _cmp_on = str(S.get("gap_fill_test_items", True)).lower() not in ("false", "0", "no")
        _cmp_share = {"AA": .20, "A": .20, "B": .10}
        _cmp_min, _cmp_max = 10, 3
        _cmp = defaultdict(lambda: defaultdict(list))      # (grade, region) -> sku -> [weekly rates]
        if _cmp_on:
            for (_st, _sk), _l in lines.items():
                if _l["fc"] > 0:
                    _s = stores.get(_st) or {}
                    _cmp[(_s.get("volume_code"), _s.get("region"))][_sk].append(_l["fc"])
        completion.update(test_stores=0, test_trays=0, test_units=0)
        for st_id in short:
            ls = _cc_by_store[st_id]
            deny = store_deny.get(st_id, NO_DENY)
            got = defaultdict(int)             # sku -> trays this store holds in this pass
            for l in sorted(ls, key=lambda x: (-x.get("sku_fit", 0), x["sku_id"])):
                o = orig.get((st_id, l["sku_id"]), 0)
                if o:                          # its own allocation back, while stock lasts
                    got[l["sku_id"]] = min(o, int(pools.left(l["sku_id"], deny) // l["upt"]))
            state = _cc_state(ls, lambda l: got[l["sku_id"]])
            fill = _cc_best_fill(state)
            cands = sorted((l for l in ls if (l["fc"] > 0 or (hist_fill and l.get("best_lot")))
                            and not l.get("reactivation")),
                           key=lambda x: (-x.get("sku_fit", 0), -x["fc"], x["sku_id"]))

            def _room(l):
                g = l.get("grade")
                w = max(float(_wos_set.get(g) or _wos_def.get(g, 6)), sell_down_wk(l))
                w = min(w, fresh_of.get(l["sku_id"], fresh_days) / 7.0)
                if l["family"] in H2O_TERR_FAMILIES:
                    w = min(w, float(S["h2o_terrarium_window_weeks"]))
                if l["conf"] == "UNUSABLE":
                    w = min(w, 4.0)             # §5: unreliable inventory keeps a tight ceiling
                if l.get("oh_conflict"):
                    w = min(w, oh_check_wk)     # shelf check pending: never past the cap
                have = got[l["sku_id"]]
                ceiling = int((l["fc"] * w - max(0.0, l["projected"])) // l["upt"])
                _bl = l.get("best_lot") if hist_fill else None
                if _bl and l["conf"] != "UNUSABLE" and not l.get("oh_conflict"):
                    # Jason's Week 49 brief: a store may get back up to the largest earlier
                    # restock it sold through ("successful exact store/SKU delivery sell-down")
                    ceiling = max(ceiling, int((_bl["units"] - max(0.0, l["projected"])) // l["upt"]))
                if mg_on and not l.get("directed_trays"):
                    # completion: a pack only has to add projected sales; the CART must reach the
                    # blended sell-through target (Jason: 68% at the grade horizon, not per SKU)
                    _D = DF.sellable_demand(l["fc"], max(0.0, l.get("projected", 0.0)), line_horizon(l), mg_a)
                    _p = max(1, l["upt"])
                    # a product may go up to the quantity that still sells cart_min_sellthrough of
                    # itself in the window (D / 0.68), never past a pack that adds no sales at all
                    _st_cap = int(_D / (cart_st_min * _p)) if cart_st_min > 0 else 10 ** 6
                    ceiling = min(ceiling, max(DF.ceiling_packs(l["fc"], max(0.0, l.get("projected", 0.0)),
                                                                line_horizon(l), _p, mg_a, line_f(l),
                                                                min_share=1e-6), _st_cap))
                by_wos = max(l["trays"], ceiling) - have        # its own need always qualifies
                if l.get("tray_cap") is not None:
                    by_wos = min(by_wos, max(l["trays"], l["tray_cap"]) - have)   # per-line tray limit
                by_supply = int(pools.left(l["sku_id"], deny) // l["upt"]) - have
                return max(0, min(by_wos, by_supply)), w, by_wos, by_supply

            for _ in range(600):
                if fill >= min_cart_fill:
                    break
                pick = None
                if mg_on:                      # the pack that adds the most projected sales first
                    cands.sort(key=lambda x: (-(DF.marginal_units(got[x["sku_id"]] * x["upt"], x["upt"], x["fc"],
                                                                  max(0.0, x.get("projected", 0.0)), line_horizon(x),
                                                                  mg_a, line_f(x)) * usd(x)),
                                              -x.get("sku_fit", 0), x["sku_id"]))
                for l in cands:                # best product first; it must move the cart forward
                    if _room(l)[0] <= 0:
                        continue
                    pid, cap, h, pmax = _cc_prof(l["sku_id"])
                    trial = {k: list(v) for k, v in state.items()}
                    trial.setdefault(pid, [0, cap, h, pmax])[0] += 1
                    f2 = _cc_best_fill(trial)
                    if f2 > fill + 1e-9:
                        pick = (l, trial, f2)
                        break
                if not pick:
                    break
                got[pick[0]["sku_id"]] += 1
                state, fill = pick[1], pick[2]
            tests = []                          # new store x SKU lines for test items
            _sinfo = stores.get(st_id) or {}
            _share = _cmp_share.get(_sinfo.get("volume_code"), 0) if _cmp_on else 0
            if fill < min_cart_fill and _share > 0 and ls:
                _rule = store_rule.get(st_id)
                _asst = _sinfo.get("approved_assortment")
                _pool = _cmp.get((_sinfo.get("volume_code"), _sinfo.get("region"))) or {}
                _opts = []
                for _sk, _rates in _pool.items():
                    if (len(_rates) < _cmp_min or (st_id, _sk) in lines or _sk not in plannable
                            or _sk not in pack or pools.left(_sk, deny) < int(pack[_sk].get("units_per_tray") or 1)):
                        continue
                    _i = sku_info.get(_sk) or {}
                    _fam = _i.get("ipr_family") or "Unmapped"
                    if (_fam == "Prebuilt Cart" or (_fam == "Climb-ITT" and not (_i.get("program") or "").strip())
                            or (service.get((st_id, _fam)) or ("",))[0] == "BLOCKED"):
                        continue
                    if isinstance(_asst, (list, tuple)) and _asst and _fam not in _asst:
                        continue
                    if _rule and not store_rules.sku_allowed(_rule, _i, directed=False)[0]:
                        continue
                    _med = sorted(_rates)[len(_rates) // 2]
                    # the test tray must sell inside the 13-week review window at the comparable
                    # stores' pace — a slow product never rides along to finish a cart (§21)
                    if _med <= 0 or int(pack[_sk].get("units_per_tray") or 1) / _med > 13:
                        continue
                    _opts.append((-_med * (price(_sk)[0] or 0.0), _sk, _med, len(_rates), _fam))
                _used = 0.0
                for _neg, _sk, _med, _nc, _fam in sorted(_opts):
                    if fill >= min_cart_fill or len(tests) >= _cmp_max:
                        break
                    pid, cap, h, pmax = _cc_prof(_sk)
                    if _used + 1.0 / cap > _share * CART_SHELVES + 1e-9:
                        continue
                    trial = {k: list(v) for k, v in state.items()}
                    trial.setdefault(pid, [0, cap, h, pmax])[0] += 1
                    f2 = _cc_best_fill(trial)
                    if f2 <= fill + 1e-9:
                        continue
                    state, fill, _used = trial, f2, _used + 1.0 / cap
                    tests.append((_sk, _med, _nc, _fam))
            if fill >= min_cart_fill and mg_on and cart_st_min > 0:
                _num = _den = 0.0
                for l in ls:
                    q = got[l["sku_id"]] * l["upt"]
                    if q <= 0:
                        continue
                    v = usd(l)
                    _num += DF.sold_units(q, l["fc"], max(0.0, l.get("projected", 0.0)), line_horizon(l),
                                          mg_a, line_f(l)) * v
                    _den += q * v
                if _den and _num / _den < cart_st_min:
                    completion.setdefault("failed_sellthrough", 0)
                    completion["failed_sellthrough"] += 1
                    fill = 0.0                          # the completed cart would not sell through
            if fill < min_cart_fill:
                completion["stores_short"] += 1     # nothing qualified completes the cart
                _w = sum(1 for l in cands if _room(l)[2] <= 0)
                _s = sum(1 for l in cands if _room(l)[2] > 0 and _room(l)[3] <= 0)
                _why = ("no_selling_product" if not cands else "weeks_of_supply_limit" if _s == 0
                        else "no_stock_available" if _w == 0 else "both_limits")
                completion.setdefault("short_why", {}).setdefault(_why, 0)
                completion["short_why"][_why] += 1
                completion.setdefault("short_fill_bands", [0, 0, 0, 0])[min(3, int(fill / 0.25))] += 1
                for l in ls:                   # their trays are ALREADY back in the pool (above)
                    if l.get("alloc_trays", 0) > 0:
                        l["alloc_in_pool"] = True
                continue                       # lines keep their original figures; pushed below
            parts, tr_gap, u_gap, w_gap, u_delta = [], 0, 0, 0.0, 0
            for _sk, _med, _nc, _fam in tests:
                # a full line record, copied from one of the store's own lines so every field the
                # order writer reads is present, then set to this product's own figures
                nl = dict(ls[0])
                _upt = int(pack[_sk].get("units_per_tray") or 1)
                _p, _psrc = price(_sk)
                for _k in ("completion_trays", "completion_wos", "gap_fill_trays", "short_units", "variety_short",
                           "reduced_13wk", "corrective_test", "directed_trays", "directed_adj", "variety_codes",
                           "variety_name", "scn_cap_trays", "scn_floor_trays", "deferred_units"):
                    nl.pop(_k, None)
                nl.update(sku_id=_sk, family=_fam, upt=_upt, fc=_med, r2=0.0, r4=0.0, r10=0.0, r13=0.0,
                          method=f"test item: median of {_nc} comparable stores (same grade and region)",
                          onhand=0.0, oh_adj=0.0, conf="LOW", oh_src="no history at this store",
                          inbound=0, inbound_src="none", before_arrival=0.0, projected=0.0, risk="Unknown",
                          need=float(_upt), trays=1, trays_unreduced=1, cap_note="test item: one tray",
                          price=_p, price_src=_psrc, sales13=0.0, cohort=0.0, plannable=True,
                          grade=grade.get((st_id, _fam), ls[0].get("grade")), service="ELIGIBLE",
                          conversion=None, conv_conf="no history", reactivation=None,
                          sku_fit=0.0, priority=min(x.get("priority", 0) for x in ls),
                          alloc_trays=0, alloc_units=0, test_item=True,
                          oh_conflict=None, stockout_recovery=None, fp_rate=None, accessory=False,
                          tray_cap=None, mcap=None)
                nl.pop("trays_before_tray_cap", None)
                lines[(st_id, _sk)] = nl
                ship[(st_id, _sk)] = nl
                ls.append(nl)
                got[_sk] = 1
                completion["test_trays"] += 1
                completion["test_units"] += _upt
            if tests:
                completion["test_stores"] += 1
            for l in ls:
                n, o = got[l["sku_id"]], orig.get((st_id, l["sku_id"]), 0)
                if n <= 0 and o <= 0:
                    continue
                if n > 0:
                    pools.take(l["sku_id"], n * l["upt"], deny)
                    if (st_id, l["sku_id"]) not in ship:
                        ship[(st_id, l["sku_id"])] = l      # proven seller added by completion
                u_delta += (n - o) * l["upt"]
                l["alloc_trays"], l["alloc_units"] = n, n * l["upt"]
                gap = max(0, n - max(l["trays"], o))            # trays beyond the store's own need
                if l.get("test_item"):
                    l["completion_trays"], l["completion_wos"] = n, round(n * l["upt"] / l["fc"], 1)
                    tr_gap, u_gap, w_gap = tr_gap + n, u_gap + n * l["upt"], w_gap + n * l["upt"] * usd(l)
                    parts.append(f"TEST ITEM {sku_info[l['sku_id']]['description'].strip()} +{n} tray "
                                 f"({n * l['upt']}u; not sold here before, comparable stores sell "
                                 f"{l['fc']:.1f}/wk)")
                elif gap:
                    wos_after = (max(0.0, l["projected"]) + l["alloc_units"]) / l["fc"]
                    l["completion_trays"], l["completion_wos"] = gap, round(wos_after, 1)
                    tr_gap, u_gap, w_gap = tr_gap + gap, u_gap + gap * l["upt"], w_gap + gap * l["upt"] * usd(l)
                    parts.append(f"{sku_info[l['sku_id']]['description'].strip()} +{gap} tray(s) "
                                 f"({gap * l['upt']}u; sells {l['fc']:.1f}/wk here, {wos_after:.1f} wks of "
                                 f"supply after delivery, limit {_room(l)[1]:.0f})")
                orig.pop((st_id, l["sku_id"]), None)
            alloc_units_total += u_delta
            completion["stores"] += 1
            completion["trays"] += tr_gap
            completion["units"] += u_gap
            completion["wholesale"] += w_gap
            completion["primary_units"] = completion.get("primary_units", 0) + sum(
                l["alloc_units"] for l in ls) - u_gap
            decide("cart_completion",
                   (f"Cart completed: this store's own need fills the cart once stock is taken from "
                    f"lower-priority short orders" if not tr_gap else
                    f"Cart completed with qualified gap-fill: {tr_gap} tray(s) / {u_gap}u beyond the "
                    f"store's own need so one cart is full. Every product sells at this store and stays "
                    f"inside its weeks-of-supply limit: " + "; ".join(parts)),
                   store_id=st_id, severity="info", trays=tr_gap, units=u_gap, wholesale=round(w_gap, 2),
                   skus=sorted(l["sku_id"] for l in ls if l.get("completion_trays")))
        for sk in {k[1] for k in orig} | {l["sku_id"] for st in short for l in _cc_by_store[st]}:
            if sk in sku_left:
                sku_left[sk] = pools.left(sk)
        completion["wholesale"] = round(completion["wholesale"], 2)
        log(f"Cart completion: {completion['stores']:,} of {completion['stores_tried']:,} short stores completed "
            f"with qualified gap-fill (+{completion['units']:,}u); {completion['stores_short']:,} had no "
            f"qualified product to finish a cart")

    # ══ 7. TRAYS -> CART-LOADING PLAN (§17–19): physical fit only ═══════════
    # §19.1 cart economics — editable assumptions, not rules: delivered cart cost is roughly
    # $180–$300 (default $240); normal wholesale value per cart is roughly $1,700–$3,000.
    cart_cost = float(S.get("cart_delivered_cost", 240) or 240)
    low_value_wh = float(S.get("cart_wholesale_low", 1700) or 1700)
    cv_hard = str(S.get("cart_value_floor", "off") or "off").lower() == "hard"
    consolidate = str(S.get("cart_consolidate", True)).lower() not in ("false", "0", "no")
    orders = {}
    for l in ship.values():
        if l.get("alloc_trays", 0) <= 0:
            continue
        orders.setdefault(l["store_id"], []).append(l)

    prebuilt_alone = []          # stores whose only product this week is a pre-built cart
    def _sh_h(items):
        """Shelf rung (ShelfKit v3.68): the tallest product's rung; profiles the model does not
        know use their packing-profile clear height."""
        return max((float(SK.PROFILE_FIT[it[4]][2]) if it[4] in SK.PROFILE_FIT
                    else float(pack[it[0]]["clear_height_in"] or MIN_SHELF_CLEAR_IN)) for it in items)

    def _sh_frac(items):
        """Share of one CC shelf the items use, by real tray widths (ShelfKit width maths;
        a leftover strip under 3 in counts as filled)."""
        return SK.shelf_fill([(it[4], it[1]) for it in items])

    def _pack_store(store_id, olines):
        """Build one store's carts from its allocated lines (also used to re-pack a store
        that received re-offered trays)."""
        nonlocal alloc_units_total
        # A PRE-BUILT CART (e.g. the Climb-itt Bundle) only travels with at least one CC cart of
        # product (Josh 2026-11-23). A store with nothing else this week does not get it.
        _pbl = [l for l in olines if (sku_info[l["sku_id"]].get("family") or "") == "Prebuilt Cart"
                and l.get("alloc_trays", 0) > 0]
        if _pbl and not any(l.get("alloc_trays", 0) > 0 for l in olines if l not in _pbl):
            decide("validation",
                   f"{', '.join(sorted({sku_info[l['sku_id']]['description'] for l in _pbl}))} NOT sent: the "
                   f"store has no other product this week, and a pre-built cart only travels with at "
                   f"least one CC cart.", store_id=store_id, severity="warning")
            for l in _pbl:
                alloc_units_total -= l["alloc_units"]
                l["alloc_trays"] = l["alloc_units"] = 0
            prebuilt_alone.append(store_id)
            orders[store_id] = {"lines": olines, "carts": []}
            return
        # A NAMED-VARIETY directed shipment ("one full cart of Monstera Bulbasaur") rides on its
        # own cart(s): its trays are never mixed onto shelves with other product.
        _ded = [l for l in olines if l.get("variety_codes") and l.get("alloc_trays", 0) > 0]
        ded_carts = []
        for l in sorted(_ded, key=lambda x: x["sku_id"]):
            pk0 = pack[l["sku_id"]]
            cap, pid0 = int(pk0["trays_per_shelf"]), pk0["profile_id"]
            h0 = float(pk0["clear_height_in"] or MIN_SHELF_CLEAR_IN)
            pmax0 = min(CART_SHELVES, int(pk0["max_shelves"] or CART_SHELVES))
            left, cur_c = l["alloc_trays"], None
            while left > 0:
                take = min(left, cap)
                if cur_c is None or len(cur_c["shelves"]) >= pmax0 or cur_c["height"] + h0 > CART_CEILING_IN:
                    cur_c = {"shelves": [], "height": 0.0, "prof_ct": defaultdict(int)}
                    ded_carts.append(cur_c)
                cur_c["shelves"].append((pid0, [(l["sku_id"], take, take * l["upt"], None, pid0)]))
                cur_c["height"] += h0
                cur_c["prof_ct"][pid0] += 1
                left -= take
        # shelves per profile, SKUs mixed on a shelf in sku-fit order; partial shelves allowed
        by_prof = defaultdict(list)
        for l in sorted((x for x in olines if x not in _ded), key=lambda x: (-x["sku_fit"], x["sku_id"])):
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
        if split_shelves:
            # two different products may share one shelf (Josh 2026-11-25)
            shelves = pair_partial_shelves(
                shelves, lambda sh: _sh_frac(sh[1]), lambda sh: _sh_h(sh[1]),
                lambda sh: frozenset(it[4] for it in sh[1]),
                lambda a, b: ((a if _sh_frac(a[1]) >= _sh_frac(b[1]) else b)[0], a[1] + b[1]),
                fits=lambda a, b: SK.fits_shelf([(it[4], it[1]) for it in a[1] + b[1]]))
        # pack shelves into carts, tallest first; first-fit within 5 shelves / 95in / profile max
        shelves.sort(key=lambda s: (PROFILE_HEIGHT_RANK.get(s[0], 3), s[0]))
        carts = []
        if consolidate:
            # §19.1 consolidate within the store: best-fit decreasing by shelf height — each shelf
            # goes to the open cart it fills most tightly (least height left), same physical
            # limits (5 shelves / ceiling / profile max). Shelves and trays are never dropped or
            # added; same-profile partial trays are already merged onto one shelf above.
            shelves.sort(key=lambda s: (-_sh_h(s[1]), PROFILE_HEIGHT_RANK.get(s[0], 3), s[0]))
        for pid, items in shelves:
            h = _sh_h(items)
            _pm = {}                                   # profile -> its max shelves per cart
            for it in items:
                _pm.setdefault(it[4], int(pack[it[0]]["max_shelves"] or CART_SHELVES))
            fits = [c for c in carts
                    if (len(c["shelves"]) < min(CART_SHELVES, SK.max_shelves(set(c["prof_ct"]) | set(_pm)))
                        and c["height"] + h <= CART_CEILING_IN
                        and all(c["prof_ct"][p_] < m_ for p_, m_ in _pm.items()))]
            if fits:
                c = (min(fits, key=lambda c: (CART_CEILING_IN - c["height"] - h, len(c["shelves"])))
                     if consolidate else fits[0])
                c["shelves"].append((pid, items))
                c["height"] += h
            else:
                c = {"shelves": [(pid, items)], "height": h, "prof_ct": defaultdict(int)}
                carts.append(c)
            for p_ in _pm:
                c["prof_ct"][p_] += 1
        carts = ded_carts + carts            # dedicated carts first; nothing else was packed onto them
        out = []
        for ci, c in enumerate(carts, 1):
            ordered = sorted(c["shelves"], key=lambda s: (-_sh_h(s[1]), PROFILE_HEIGHT_RANK.get(s[0], 3), s[0]))
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
            shelf_eq = sum(min(1.0, _sh_frac(items)) for _, items in ordered)
            ceiling_hit = c["height"] + MIN_SHELF_CLEAR_IN > CART_CEILING_IN
            _maxsh = min(CART_SHELVES, SK.max_shelves({it[4] for _, its in ordered for it in its}))
            slots = len(ordered) if (ceiling_hit or len(ordered) >= _maxsh) else _maxsh
            tray_fill = shelf_eq / max(1, slots)
            phys_full = tray_fill >= min_cart_fill
            out.append({"cart_number": ci, "cart_type_id": "cc", "shelves_used": len(ordered),
                        "phys_full": phys_full, "tray_fill": round(tray_fill, 3),
                        "total_units": sum(x[3] for x in contents), "contents": contents,
                        "is_full": len(ordered) >= _maxsh, "is_flagged": False,
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
        _vn = {l["sku_id"]: l["variety_name"] for l in olines if l.get("variety_codes")}
        if _vn:
            for c in out:
                _names = sorted({_vn[x[1]] for x in c["contents"] if x[1] in _vn})
                if _names:
                    c["handling_tag"] = "; ".join(filter(None, [c.get("handling_tag"),
                                                                 "ONLY " + " / ".join(_names).upper()]))
        grade_framework = {"AA": 3, "A": 3, "B": 2, "C+": 2, "C": 1, "C-": 1, "D": 1}
        top_grade = min((l["grade"] for l in olines), key=GRADE_ORDER.index)
        if len(out) > grade_framework[top_grade]:
            decide("review", f"{len(out)} carts exceeds the §18 initial framework ({grade_framework[top_grade]} "
                             f"for best family grade {top_grade}) — demand-driven, shipped (cart caps off)",
                   store_id=store_id, severity="info", carts=len(out))
        orders[store_id] = {"lines": olines, "carts": out}
    for store_id, olines in list(orders.items()):
        _pack_store(store_id, olines)
    for store_id in prebuilt_alone:
        if not any(l.get("alloc_trays", 0) > 0 for l in orders[store_id]["lines"]):
            del orders[store_id]

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
    returned = defaultdict(int)     # sku -> units handed back by held (pushed/deferred) orders
    held_orders = {}                 # store -> its held order (lines keep deferred_units), for the target step
    # EVERY SHELF FULL (Josh 2026-11-25: "80% fill means carts, not shelves — all shelves should be
    # full"). A routine shelf is full when its trays leave less than a 3 in strip (ShelfKit). For each
    # product profile that leaves a partial shelf at a store: first COMPLETE it with more trays of
    # that profile's products the store sells, while each added tray still adds projected sales in
    # the selling window, within supply and tray limits; otherwise CUT the partial shelf (its
    # lowest-value trays go back to the pool). The store is re-packed with full shelves only. A
    # store whose routine order no longer fills one cart to min fill loses its routine cart(s)
    # (pushed to next week); directed / pre-built carts are never touched.
    fullsh = {"enabled": str(S.get("require_full_shelves", True)).lower() not in ("false", "0", "no"),
              "stores": 0, "shelves_completed": 0, "trays_added": 0, "shelves_cut": 0, "trays_cut": 0,
              "units_added": 0, "units_cut": 0, "stores_routine_pushed": 0}
    def _full_shelves_pass(stage, only=None):
        """Every-shelf-full pass (complete partial shelves, else cut; push stores whose routine
        order then fills no cart). Runs before the partial-cart trim and again after config merge."""
        nonlocal alloc_units_total
        def _is_prot_line(l):
            return bool(l.get("directed_trays") or l.get("variety_codes")
                        or (sku_info[l["sku_id"]].get("family") or "") == "Prebuilt Cart")

        def _partial_profiles(o):
            by = {l["sku_id"]: l for l in o["lines"]}
            out = set()
            for c in o["carts"]:
                layers = defaultdict(list)
                for (pos, sk, tr, u, kt, pid) in c["contents"]:
                    layers[pos].append((pid, tr, sk))
                for its in layers.values():
                    if any(_is_prot_line(by.get(sk) or {"sku_id": sk}) for _, _, sk in its):
                        continue
                    if not SK.shelf_is_full([(pp, t) for pp, t, _ in its]):
                        out |= {pp for pp, _, _ in its}
            return out

        def _next_units(l, extra_trays=0):
            q = l.get("alloc_units", 0) + extra_trays * l["upt"]
            return DF.marginal_units(q, l["upt"], l["fc"], max(0.0, l.get("projected", 0.0)),
                                     line_horizon(l), mg_a, line_f(l))

        _sh_order = sorted((s_ for s_ in orders if only is None or s_ in only),
                           key=lambda s_: (-max((l.get("priority", 0) for l in orders[s_]["lines"]), default=0), s_))
        for store_id in _sh_order:
            o = orders.get(store_id)
            if not o or not o["carts"]:
                continue
            if any(l.get("alloc_in_pool") for l in o["lines"]):
                continue                 # trays already handed back by cart completion (pushed later)
            deny = store_deny.get(store_id, NO_DENY)
            touched = False
            for _it in range(3):
                bad = _partial_profiles(o)
                if not bad:
                    break
                for pid in sorted(bad):
                    plines = [l for l in o["lines"] if not _is_prot_line(l) and l["sku_id"] in pack
                              and pack[l["sku_id"]]["profile_id"] == pid and l.get("alloc_trays", 0) > 0]
                    if not plines:
                        continue
                    cap = max(1, int(pack[plines[0]["sku_id"]]["trays_per_shelf"] or 1))
                    rem = sum(l["alloc_trays"] for l in plines) % cap
                    if not rem:
                        continue
                    need = cap - rem
                    # candidate lines of this profile at the store (shipping or not), best next tray first
                    cands = [l for (st_, sk_), l in ship.items()
                             if st_ == store_id and sk_ in pack and pack[sk_]["profile_id"] == pid
                             and l.get("plannable") and not _is_prot_line(l) and not l.get("oh_conflict")
                             and l.get("conf") != "UNUSABLE" and (l["fc"] > 0)]
                    adds = defaultdict(int)
                    for _k in range(need):
                        best = None
                        for l in cands:
                            a = adds[id(l)]
                            if l.get("tray_cap") is not None and l.get("alloc_trays", 0) + a + 1 > l["tray_cap"]:
                                continue
                            if pools.left(l["sku_id"], deny) < (a + 1) * l["upt"]:
                                continue
                            mu = _next_units(l, a)
                            if mu <= 1e-9:
                                continue
                            v = mu * usd(l)
                            if best is None or v > best[0]:
                                best = (v, l)
                        if not best:
                            break
                        adds[id(best[1])] += 1
                    if sum(adds.values()) != need:
                        # same product type cannot finish the shelf: try ANOTHER product the store
                        # sells that shares the shelf (ShelfKit fit; two products per shelf max),
                        # added only while each tray adds projected sales, within supply / limits
                        adds = defaultdict(int)
                        _base = [(pid, rem)]
                        xc = []
                        for (st_, sk_), l in ship.items():
                            if (st_ != store_id or sk_ not in pack or not l.get("plannable") or _is_prot_line(l)
                                    or l.get("oh_conflict") or l.get("conf") == "UNUSABLE" or l["fc"] <= 0
                                    or l["family"] in H2O_TERR_FAMILIES):
                                continue
                            q = pack[sk_]["profile_id"]
                            if q == pid:
                                continue
                            qcap = max(1, int(pack[sk_]["trays_per_shelf"] or 1))
                            if sum(x.get("alloc_trays", 0) for x in o["lines"] if x["sku_id"] in pack
                                   and pack[x["sku_id"]]["profile_id"] == q and not _is_prot_line(x)) % qcap:
                                continue                      # q already leaves its own partial shelf
                            xc.append((q, l))
                        best = None
                        for q, l in xc:
                            t, units_gain = 0, 0.0
                            while True:
                                t += 1
                                if not SK.fits_shelf(_base + [(q, t)]):
                                    t = 0
                                    break
                                if (l.get("tray_cap") is not None and l.get("alloc_trays", 0) + t > l["tray_cap"]) \
                                        or pools.left(l["sku_id"], deny) < t * l["upt"]:
                                    t = 0
                                    break
                                mu = _next_units(l, t - 1)
                                if mu <= 1e-9:
                                    t = 0
                                    break
                                units_gain += mu
                                if SK.shelf_is_full(_base + [(q, t)]):
                                    break
                            if t and (best is None or units_gain * usd(l) > best[0]):
                                best = (units_gain * usd(l), l, t)
                        if best:
                            adds[id(best[1])] = best[2]
                            cands = [best[1]]
                            need = best[2]
                    if sum(adds.values()) == need and need > 0:
                        for l in cands:
                            a = adds[id(l)]
                            if not a:
                                continue
                            u = a * l["upt"]
                            pools.take(l["sku_id"], u, deny)
                            l["alloc_trays"] = l.get("alloc_trays", 0) + a
                            l["alloc_units"] = l.get("alloc_units", 0) + u
                            l["shelf_completion_trays"] = l.get("shelf_completion_trays", 0) + a
                            if l not in o["lines"]:
                                o["lines"].append(l)
                            alloc_units_total += u
                            fullsh["units_added"] += u
                        fullsh["trays_added"] += need
                        fullsh["shelves_completed"] += 1
                    else:
                        # cut the partial shelf: lowest-value last trays first
                        left = rem
                        while left > 0:
                            l = min((x for x in plines if x.get("alloc_trays", 0) > 0),
                                    key=lambda x: (_next_units(x, -1) * usd(x), x["sku_id"]))
                            l["alloc_trays"] -= 1
                            l["alloc_units"] -= l["upt"]
                            l["shelf_cut_trays"] = l.get("shelf_cut_trays", 0) + 1
                            pools.give_back(l["sku_id"], l["upt"], deny)
                            alloc_units_total -= l["upt"]
                            fullsh["units_cut"] += l["upt"]
                            fullsh["trays_cut"] += 1
                            left -= 1
                        fullsh["shelves_cut"] += 1
                    touched = True
                _pack_store(store_id, [l for l in o["lines"] if l.get("alloc_trays", 0) > 0])
                o = orders[store_id]
            if not touched:
                continue
            fullsh["stores"] += 1
            by_g = {l["sku_id"]: l for l in o["lines"]}
            _rc = [c for c in o["carts"] if not any(_is_prot_line(by_g.get(x[1]) or {"sku_id": x[1]})
                                                     for x in c["contents"])]
            if _rc and not any(c["tray_fill"] >= min_cart_fill - 1e-9 for c in _rc):
                # routine order no longer fills one cart: routine trays back to the pool
                for l in o["lines"]:
                    if _is_prot_line(l) or not l.get("alloc_units"):
                        continue
                    pools.give_back(l["sku_id"], l["alloc_units"], deny)
                    alloc_units_total -= l["alloc_units"]
                    l["deferred_units"] = l["alloc_units"]
                    l["alloc_units"] = l["alloc_trays"] = 0
                keep = [l for l in o["lines"] if l.get("alloc_trays", 0) > 0]
                fullsh["stores_routine_pushed"] += 1
                decide("pushed_min_cart", "Routine order pushed to next week: with every shelf full it no "
                                          f"longer fills one cart to {min_cart_fill:.0%} (Josh 11/25)",
                       store_id=store_id, severity="info", stage="full_shelves")
                if keep:
                    _pack_store(store_id, keep)
                else:
                    del orders[store_id]
        log(f"Every shelf full: {fullsh['stores']:,} stores · {fullsh['shelves_completed']:,} shelves completed "
            f"(+{fullsh['units_added']:,}u) · {fullsh['shelves_cut']:,} partial shelves cut (-{fullsh['units_cut']:,}u) · "
            f"{fullsh['stores_routine_pushed']:,} routine orders pushed")

    if fullsh["enabled"]:
        _full_shelves_pass("pre_trim")      # complete partial shelves before any cart is judged

    # FULL CARTS ONLY (Jason's Week 49 order, 11/25: "all carts fully allocated"): a store with
    # more than one cart drops a trailing cart filled below min_cart_fill — the lowest-value
    # partial cart's trays go back to the pool (re-offered to other stores' open space; this
    # store's need is recalculated next week). A cart holding a directed or named-variety line
    # is never cut. Single-cart orders are judged by the minimum-order rule below.
    trim = {"enabled": str(S.get("trim_partial_carts", True)).lower() not in ("false", "0", "no"),
            "stores": 0, "carts": 0, "units": 0, "wholesale": 0.0}
    if trim["enabled"]:
        for store_id in sorted(orders):
            # stores with a directed shipment are trimmed too (Jason 11/25 found 1-shelf routine carts
            # riding with Bulbasaur): only carts holding a directed / named-variety line are protected
            if (stores.get(store_id) or {}).get("capacity_carts"):
                continue
            o = orders[store_id]
            if any(l.get("alloc_in_pool") for l in o["lines"]):
                continue                 # cart completion could not fill it: trays already pooled, pushed below
            _deny_t = store_deny.get(store_id, NO_DENY)
            _cut_u, _cut_c, _cut_w = 0, 0, 0.0
            for _i in range(40):
                cs = o["carts"]
                if len(cs) <= 1:
                    break
                by_sku_t = {l["sku_id"]: l for l in o["lines"]}
                _prot = lambda c: any((by_sku_t.get(x[1]) or {}).get("directed_trays")
                                      or (by_sku_t.get(x[1]) or {}).get("variety_codes")
                                      or (sku_info[x[1]].get("family") or "") == "Prebuilt Cart"
                                      for x in c["contents"])
                routine = [c for c in cs if not _prot(c)]
                _cc_other = [c for c in cs if _prot(c) and not all(
                    (sku_info[x[1]].get("family") or "") == "Prebuilt Cart" for x in c["contents"])]
                if not routine or (len(routine) <= 1 and not _cc_other):
                    break          # a store keeps one regular CC cart (a pre-built cart rides only with one)
                part = [c for c in routine if c["tray_fill"] < min_cart_fill - 1e-9]
                if not part:
                    break
                # Jason 11/25 (store 628): cutting a whole partial cart, repacking, then cutting the
                # next abandoned demand that made more than one full cart. With several routine
                # carts, cut ONE shelf at a time (the lowest-value shelf of the least-full cart) and
                # repack, so the order settles on its full carts. A store's only routine cart that is
                # short (beside a directed cart) goes whole: shelf cuts cannot fill it.
                c = min(part, key=lambda c_: (c_["tray_fill"], c_["wholesale_value"], -c_["cart_number"]))
                if len(routine) > 1:
                    _lay = defaultdict(list)
                    for x in c["contents"]:
                        _lay[x[0]].append(x)
                    _lv = lambda p_: sum(x[3] * float(sku_info[x[1]].get("wholesale_price_whl") or 0) for x in _lay[p_])
                    _cut_items = _lay[min(_lay, key=lambda p_: (_lv(p_), p_))]
                else:
                    _cut_items = c["contents"]
                for (_pos, sk, tr, u, _k, _pid) in _cut_items:
                    l = by_sku_t.get(sk)
                    if not l:
                        continue
                    tr_, u_ = min(tr, l["alloc_trays"]), min(u, l["alloc_units"])
                    l["alloc_trays"] -= tr_
                    l["alloc_units"] -= u_
                    l["trimmed_units"] = l.get("trimmed_units", 0) + u_
                    pools.give_back(sk, u_, _deny_t)
                    returned[sk] += u_
                    _cut_u += u_
                _cut_c += 1
                _cut_w += sum(x[3] * float(sku_info[x[1]].get("wholesale_price_whl") or 0) for x in _cut_items)
                _pack_store(store_id, [l for l in o["lines"] if l.get("alloc_trays", 0) > 0])
                o = orders[store_id]
            if _cut_c:
                alloc_units_total -= _cut_u
                trim["stores"] += 1
                trim["carts"] += _cut_c
                trim["units"] += _cut_u
                trim["wholesale"] += _cut_w
                decide("partial_cart_trimmed",
                       f"Partial shelves/carts removed so every cart ships full: {_cut_c} cut(s), "
                       f"{_cut_u:,}u, ${_cut_w:,.0f} back to the pool; the store keeps "
                       f"{len(o['carts'])} full cart(s) and its need is recalculated next week",
                       store_id=store_id, severity="info", carts_cut=_cut_c, units_cut=_cut_u,
                       wholesale_cut=round(_cut_w, 2))
        log(f"Full carts only: {trim['stores']:,} stores trimmed ({trim['carts']:,} partial carts, "
            f"{trim['units']:,}u, ${trim['wholesale']:,.0f} back to the pool)")
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
        if n_full >= min_full_carts or store_id in directed_stores:
            # Cart completion judged this store short and handed its trays back to the pool
            # (alloc_in_pool), but the real pack fills a cart, so it ships: take its trays back
            # out of the pool. If another store drew some of them meanwhile, the line keeps only
            # what is left and the store is re-packed (never ship stock counted as free).
            _pooled = [l for l in o["lines"] if l.get("alloc_in_pool")]
            if _pooled:
                _deny_r = store_deny.get(store_id, NO_DENY)
                _cut = False
                for l in _pooled:
                    want = int(l.get("alloc_units", 0) or 0)
                    got_u = min(want, int(pools.left(l["sku_id"], _deny_r)))
                    got_t = got_u // max(1, l["upt"])
                    pools.take(l["sku_id"], got_t * l["upt"], _deny_r)
                    if got_t * l["upt"] < want:
                        _cut = True
                        alloc_units_total -= want - got_t * l["upt"]
                    l["alloc_trays"], l["alloc_units"] = got_t, got_t * l["upt"]
                    l.pop("alloc_in_pool", None)
                if _cut:
                    _pack_store(store_id, [l for l in o["lines"] if l.get("alloc_trays", 0) > 0])
                    o = orders[store_id]
                    if not any(c["phys_full"] for c in o["carts"]) and store_id not in directed_stores:
                        pass                 # falls through to the push below with its reduced order
                    else:
                        continue
                else:
                    continue
            else:
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
               f"{streak + 1} extra coverage week(s); no qualified gap-fill product could complete "
               f"a cart inside the store's weeks-of-supply limit (leadership decision 2026-11-20)",
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
        _deny_p = store_deny.get(store_id, NO_DENY)
        for l in o["lines"]:
            if l.get("alloc_units", 0) > 0:          # trays go back to the pool (Jason point 4)
                if not l.get("alloc_in_pool"):       # cart completion already returned these
                    pools.give_back(l["sku_id"], l["alloc_units"], _deny_p)
                returned[l["sku_id"]] += l["alloc_units"]
            l["deferred_units"] = l["alloc_units"]
            l["alloc_units"], l["alloc_trays"] = 0, 0
        held_orders[store_id] = o
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
        if not n:
            continue                          # nothing left to ship (no cart to judge)
        W = sum(c["wholesale_value"] for c in o["carts"])
        C = n * cart_cost
        lost = sum(min(l["fc"], max(0.0, l["fc"] - l["projected"])) * usd(l) for l in o["lines"])
        critical = sorted(l["sku_id"] for l in o["lines"] if l["risk"] == "Critical")
        prior_def = prior_economic_deferrals(hist.get(store_id, []))
        overdue = is_overdue(store_last_del.get(store_id), arrival_of(store_id), store_due[store_id][2])
        action = economic_action(W, n, lost, cart_cost, low_value_wh, bool(critical), prior_def, max_econ_def,
                                 overdue=overdue)
        if store_id in directed_stores and action == "defer":
            action = "release"               # an approved directed shipment is never deferred
        if min_full_carts > 0 and action == "defer":
            action = "release_min_cart"     # floor met: one full CC cart is the agreed minimum
        # Cart value floor (Jason's Week 49 order, 11/25: "every routine cart exceeds $1,700"):
        # hard = an order averaging under cart_wholesale_low per cart is held, whatever the
        # min-cart / lost-sales / overdue releases say. Directed shipments still ship.
        floor_hold = cv_hard and action != "pass" and store_id not in directed_stores
        if floor_hold:
            action = "defer"
            econ["held_below_value_floor"] = econ.get("held_below_value_floor", 0) + 1
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
        decide("deferred_economics", (f"Order HELD: {nums} — below the ${low_value_wh:,.0f}-per-cart value floor "
                                      f"(cart_value_floor = hard); its trays go back to the pool for other "
                                      f"stores, need recalculated next week") if floor_hold else
               (f"Order DEFERRED: {nums} < delivered cost — waiting a week costs "
                f"less than delivering now; deferred once, will ship next time due "
                f"regardless; need recalculated then, never padded (§19.1, §23)"),
               store_id=store_id, action="defer", value_floor=bool(floor_hold), **data)
        econ["deferred_carts"] += n
        econ["deferred_units"] += data["units"]
        econ["deferred_wholesale"] += W
        econ["deferred_retail_one_week_at_risk"] += lost
        econ["deferred_single_cart" if n == 1 else "deferred_multi_cart"] += 1
        alloc_units_total -= data["units"]
        _deny_d = store_deny.get(store_id, NO_DENY)
        for l in o["lines"]:
            if l.get("alloc_units", 0) > 0:          # trays go back to the pool (Jason point 4)
                if not l.get("alloc_in_pool"):       # cart completion already returned these
                    pools.give_back(l["sku_id"], l["alloc_units"], _deny_d)
                returned[l["sku_id"]] += l["alloc_units"]
            l["deferred_units"] = l["alloc_units"]
            l["alloc_units"], l["alloc_trays"] = 0, 0
        deferred_stores.append(store_id)
        held_orders[store_id] = o
        del orders[store_id]
    log(f"Cart economics: {econ['released_low_value']:,} low-value carts released (whole orders) · {econ['deferred_carts']:,} "
        f"deferred ({econ['deferred_units']:,}u, ${econ['deferred_wholesale']:,.0f} wholesale) · "
        f"{len(deferred_stores):,} stores fully deferred")

    # WEEKLY WHOLESALE TARGET (leadership 2026-11-24; replaces V2 doc §2 "the $900,000 minimum must not
    # justify overloading stores"): ship at least `weekly_wholesale_target` wholesale and let the
    # engine pick the best orders to get there. Josh's rules: a FLOOR (whole store orders finish, so
    # the week can land above it); RELEASE HELD STORES FIRST (orders held below one full cart or
    # deferred, highest priority first), then TOP UP shipping stores' lines still short of demand;
    # quantities MATCH DEMAND (a released store ships its own calculated order, a top-up never goes
    # past a line's need) with no extra weeks-of-supply padding. Hard blocks (do-not-ship, store
    # rules, variety bans, approved exclusions) were applied upstream and still hold; stores with a
    # cart-capacity limit are not touched. 0 = off (ship need only).
    _tw = S.get("weekly_wholesale_target")
    target_w = float(1200000 if _tw is None else (_tw or 0))
    tgt = {"target": target_w, "plan_before": 0, "released_stores": 0, "released_units": 0,
           "released_wholesale": 0.0, "topup_lines": 0, "topup_units": 0, "topup_wholesale": 0.0,
           "final": 0, "reached": None}
    _ship_w = lambda: sum(c["wholesale_value"] for o_ in orders.values() for c in o_["carts"])
    _whl = lambda sk: float((sku_info.get(sk) or {}).get("wholesale_price_whl") or 0)
    released_stores = set()
    if target_w > 0:
        tgt["plan_before"] = round(_ship_w())
        cur_w = float(tgt["plan_before"])
        _rank = sorted(held_orders, key=lambda s_: (-max((l.get("priority", 0) for l in held_orders[s_]["lines"]),
                                                        default=0), s_))
        for st_id in _rank:
            if cur_w >= target_w:
                break
            if (stores.get(st_id) or {}).get("capacity_carts"):
                continue
            o = held_orders[st_id]
            deny = store_deny.get(st_id, NO_DENY)
            got = 0
            for l in o["lines"]:
                want_t = int((l.get("deferred_units") or 0) // max(1, l["upt"]))
                t = min(want_t, int(pools.left(l["sku_id"], deny) // l["upt"]))
                if t > 0:
                    pools.take(l["sku_id"], t * l["upt"], deny)
                    returned[l["sku_id"]] -= t * l["upt"]
                    l["alloc_trays"], l["alloc_units"] = t, t * l["upt"]
                    l["deferred_units"], l["target_released"] = 0, True
                    got += t * l["upt"]
            if not got:
                continue
            _pack_store(st_id, [l for l in o["lines"] if l.get("alloc_trays", 0) > 0])
            # no sparse carts (Josh 2026-11-24): a released order must still fill at least one cart
            # to the minimum fill (min_cart_fill_pct); otherwise it stays held and its trays go back
            _rc = orders[st_id]["carts"]
            if (min_full_carts > 0 and not any(c["phys_full"] for c in _rc)) or (
                    cv_hard and _rc and sum(c["wholesale_value"] for c in _rc) / len(_rc) < low_value_wh):
                for l in o["lines"]:
                    if l.get("target_released"):
                        pools.give_back(l["sku_id"], l["alloc_units"], deny)
                        returned[l["sku_id"]] += l["alloc_units"]
                        l["deferred_units"] = l["alloc_units"]
                        l["alloc_units"], l["alloc_trays"], l["target_released"] = 0, 0, False
                del orders[st_id]
                tgt["skipped_below_fill"] = tgt.get("skipped_below_fill", 0) + 1
                continue
            add = sum(c["wholesale_value"] for c in orders[st_id]["carts"])
            cur_w += add
            alloc_units_total += got
            released_stores.add(st_id)
            tgt["released_stores"] += 1
            tgt["released_units"] += got
            tgt["released_wholesale"] += add
            decide("target_release", f"Order released to reach the ${target_w:,.0f} weekly wholesale target "
                                     f"(leadership 2026-11-24): {got:,}u, ${add:,.0f} wholesale on "
                                     f"{len(orders[st_id]['carts'])} cart(s) — the store's own calculated "
                                     f"demand, previously held below one full cart",
                   store_id=st_id, severity="info", units=got, wholesale=round(add, 2))
        if released_stores:
            decisions[:] = [d for d in decisions if not (
                d[0] in ("pushed_min_cart", "deferred_economics", "min_cart_service_review")
                and d[1] in released_stores)]
        log(f"Wholesale target ${target_w:,.0f}: plan ${tgt['plan_before']:,.0f} · released "
            f"{tgt['released_stores']:,} held stores ({tgt['released_units']:,}u, ${tgt['released_wholesale']:,.0f})")

    # RE-OFFER HELD TRAYS (Jason's review point 4; Josh 2026-11-24). Trays allocated to orders that
    # were then pushed (below the one-full-cart minimum) or deferred (cart economics) went back to
    # the pool above. Offer them once, highest priority first, to stores whose orders ARE shipping
    # and whose line for that SKU is still short of its own need (need trays − allocated trays).
    # Never beyond need, never a new store order, never a directed line, never past supply or a
    # store's variety restriction. A store that gains trays is re-packed (more product only adds
    # to carts, so the full-cart minimum still holds) — but only into open space on carts already
    # going to it: a grant that would add a cart is cut back tray by tray, or refused
    # (reoffer_allow_new_carts = true lifts this). Stores with a cart-capacity limit are skipped.
    # What is still left stays in the pool and is reported as unallocated.
    reoffer = {"returned_units": int(sum(returned.values())), "skus_returned": len(returned),
               "reoffered_units": 0, "reoffered_trays": 0, "stores": 0, "lines": 0}
    if returned and str(S.get("reoffer_held_trays", True)).lower() not in ("false", "0", "no"):
        _cands = []
        for (st_id, sk), l in ship.items():
            if (sk in returned and st_id in orders and not l.get("directed_trays")
                    and not (stores.get(st_id) or {}).get("capacity_carts")
                    and lot_trays(l) > l.get("alloc_trays", 0)):
                _cands.append(l)
        _cands.sort(key=lambda x: (-x.get("priority", 0), x["store_id"], x["sku_id"]))
        _touched = set()
        _new_carts_ok = str(S.get("reoffer_allow_new_carts", False)).lower() in ("true", "1", "yes")
        reoffer["refused_would_add_cart"] = 0
        for l in _cands:
            deny = store_deny.get(l["store_id"], NO_DENY)
            short_t = lot_trays(l) - l.get("alloc_trays", 0)
            give_t = min(short_t, int(pools.left(l["sku_id"], deny) // l["upt"]))
            if give_t <= 0:
                continue
            o = orders[l["store_id"]]
            _was_in, _old_carts = l in o["lines"], o["carts"]
            _old_t, _old_u = l.get("alloc_trays", 0), l.get("alloc_units", 0)
            _rel = any(c.get("econ_action") == "release" for c in _old_carts)
            while give_t > 0:
                # only into open space on carts already going to the store (no extra cart)
                l["alloc_trays"], l["alloc_units"] = _old_t + give_t, _old_u + give_t * l["upt"]
                if not _was_in:
                    o["lines"].append(l)
                _pack_store(l["store_id"], o["lines"])
                o = orders[l["store_id"]]
                if _new_carts_ok or len(o["carts"]) <= len(_old_carts):
                    break
                if not _was_in:
                    o["lines"].remove(l)
                orders[l["store_id"]]["carts"] = _old_carts
                o = orders[l["store_id"]]
                give_t -= 1
            l["alloc_trays"], l["alloc_units"] = _old_t, _old_u
            if give_t <= 0:
                reoffer["refused_would_add_cart"] += 1
                continue
            if _rel:
                for c in o["carts"]:
                    c["econ_action"] = "release"
            u = give_t * l["upt"]
            pools.take(l["sku_id"], u, deny)
            l["alloc_trays"] = _old_t + give_t
            l["alloc_units"] = _old_u + u
            l["reoffered_trays"] = l.get("reoffered_trays", 0) + give_t
            alloc_units_total += u
            reoffer["reoffered_units"] += u
            reoffer["reoffered_trays"] += give_t
            reoffer["lines"] += 1
            _touched.add(l["store_id"])
            decide("reoffer", f"{give_t} tray(s) ({u}u) re-offered from orders held this week; this line "
                              f"was still {short_t} tray(s) short of its own need",
                   store_id=l["store_id"], sku_id=l["sku_id"], severity="info", trays=give_t, units=u)
        reoffer["stores"] = len(_touched)
    reoffer["returned_units"] = int(sum(v for v in returned.values() if v > 0))
    reoffer["left_in_pool_units"] = reoffer["returned_units"] - reoffer["reoffered_units"]
    # TARGET TOP-UP: still below the weekly wholesale target after releasing held stores and the
    # re-offer -> fill shipping stores' lines still short of their own demand, highest priority first
    # (carts may be added here: the target outranks the open-space rule). Never past need or supply.
    if target_w > 0:
        cur_w = _ship_w()
        if cur_w < target_w:
            _tc = [l for (st_id, sk), l in ship.items()
                   if st_id in orders and not l.get("directed_trays")
                   and not (stores.get(st_id) or {}).get("capacity_carts")
                   and lot_trays(l) > l.get("alloc_trays", 0)]
            _tc.sort(key=lambda x: (-x.get("priority", 0), x["store_id"], x["sku_id"]))
            _tt, added = set(), 0.0
            for l in _tc:
                if cur_w + added >= target_w:
                    break
                deny = store_deny.get(l["store_id"], NO_DENY)
                short_t = lot_trays(l) - l.get("alloc_trays", 0)
                t = min(short_t, int(pools.left(l["sku_id"], deny) // l["upt"]))
                if t <= 0:
                    continue
                # no sparse carts (Josh 2026-11-24): only into open space on carts already going to
                # the store — a grant that would add a cart is cut back tray by tray, or refused
                o = orders[l["store_id"]]
                _was_in, _old_carts = l in o["lines"], o["carts"]
                _ot, _ou = l.get("alloc_trays", 0), l.get("alloc_units", 0)
                _rel = any(c.get("econ_action") == "release" for c in _old_carts)
                while t > 0:
                    l["alloc_trays"], l["alloc_units"] = _ot + t, _ou + t * l["upt"]
                    if not _was_in:
                        o["lines"].append(l)
                    _pack_store(l["store_id"], o["lines"])
                    o = orders[l["store_id"]]
                    if len(o["carts"]) <= len(_old_carts):
                        break
                    if not _was_in:
                        o["lines"].remove(l)
                    orders[l["store_id"]]["carts"] = _old_carts
                    o = orders[l["store_id"]]
                    t -= 1
                l["alloc_trays"], l["alloc_units"] = _ot, _ou
                if t <= 0:
                    continue
                if _rel:
                    for c in o["carts"]:
                        c["econ_action"] = "release"
                u = t * l["upt"]
                pools.take(l["sku_id"], u, deny)
                l["alloc_trays"] = _ot + t
                l["alloc_units"] = _ou + u
                l["target_topup_trays"] = l.get("target_topup_trays", 0) + t
                alloc_units_total += u
                added += u * _whl(l["sku_id"])
                _tt.add(l["store_id"])
                tgt["topup_lines"] += 1
                tgt["topup_units"] += u
                decide("target_topup", f"{t} tray(s) ({u}u) added toward the ${target_w:,.0f} weekly wholesale "
                                       f"target; the line was {short_t} tray(s) short of its own demand",
                       store_id=l["store_id"], sku_id=l["sku_id"], severity="info", trays=t, units=u)
            tgt["topup_wholesale"] = round(added, 2)
        tgt["final"] = round(_ship_w())
        tgt["reached"] = tgt["final"] >= target_w
        tgt["released_wholesale"] = round(tgt["released_wholesale"], 2)
        log(f"Wholesale target ${target_w:,.0f}: final ${tgt['final']:,.0f} "
            f"({'reached' if tgt['reached'] else 'NOT reached: no more demand-backed orders with supply'}) · "
            f"top-up {tgt['topup_lines']:,} lines / {tgt['topup_units']:,}u")
    # FILL OPEN SHELF POSITIONS (Jason's Week 49 brief item 9: "Fill all shelf positions and
    # secure trays, not just a nominal percentage of shelf count"; Josh 2026-11-25). A cart that
    # passed the minimum fill may still have empty tray positions. Fill them on carts ALREADY
    # going to the store — never a new cart or stop — with products the store has proven it
    # sells: forecast demand within the grade's weeks of supply (D: the sell-down horizon) or up
    # to the largest restock it sold through, inside tray limits and supply. H2O/Terrarium are
    # never used as filler (§17 "do not add trays merely to complete a shelf or cart"); shelf-
    # check, unreliable-inventory, directed and reactivated lines are left alone. AA/A/B first.
    fillpos = {"enabled": str(S.get("fill_open_positions", True)).lower() not in ("false", "0", "no"),
               "stores": 0, "lines": 0, "trays": 0, "units": 0, "wholesale": 0.0}
    if fillpos["enabled"] and orders:
        _wos_fp = {**{"AA": 10, "A": 10, "B": 8, "C+": 8, "C": 7, "C-": 6, "D": 6},
                   **{k: v for k, v in (S.get("gap_fill_weeks_supply_by_grade") or {}).items() if v}}

        def _fp_ceiling(l):
            w = max(float(_wos_fp.get(l.get("grade"), 6)), sell_down_wk(l))
            w = min(w, fresh_of.get(l["sku_id"], fresh_days) / 7.0)
            t = int((l["fc"] * w - max(0.0, l.get("projected", 0.0))) // max(1, l["upt"]))
            t = max(t, lot_trays(l), l.get("trays", 0))
            if l.get("tray_cap") is not None:
                t = min(t, max(l.get("trays", 0), l["tray_cap"]))
            if l.get("mcap") is not None:
                t = min(t, max(l.get("trays", 0), l["mcap"]))
            return t

        _fp_by_store = defaultdict(list)
        for (st_id, sk), l in ship.items():
            if (st_id in orders and l.get("plannable") and sk in pack
                    and (l["fc"] > 0 or (hist_fill and l.get("best_lot")))
                    and l["family"] not in H2O_TERR_FAMILIES
                    and (sku_info[sk].get("family") or "") != "Prebuilt Cart"
                    and not l.get("directed_trays") and not l.get("variety_codes")
                    and not l.get("reactivation") and not l.get("oh_conflict")
                    and l.get("conf") != "UNUSABLE"):
                _fp_by_store[st_id].append(l)
        _fp_order = sorted(_fp_by_store, key=lambda s_: (-max(x.get("priority", 0) for x in _fp_by_store[s_]), s_))
        for st_id in _fp_order:
            if (stores.get(st_id) or {}).get("capacity_carts"):
                continue
            o = orders[st_id]
            if not o["carts"]:
                continue
            deny = store_deny.get(st_id, NO_DENY)
            n_carts = len(o["carts"])
            _rel = any(c.get("econ_action") == "release" for c in o["carts"])
            _w0 = sum(c["wholesale_value"] for c in o["carts"])
            _hit = False
            for l in sorted(_fp_by_store[st_id], key=lambda x: (-x.get("sku_fit", 0), -x["fc"], x["sku_id"])):
                room = min(_fp_ceiling(l) - l.get("alloc_trays", 0), int(pools.left(l["sku_id"], deny) // l["upt"]))
                if room <= 0:
                    continue
                _ot, _ou = l.get("alloc_trays", 0), l.get("alloc_units", 0)
                _was_in = l in o["lines"]
                _old_carts = o["carts"]

                def _fits(t):
                    l["alloc_trays"], l["alloc_units"] = _ot + t, _ou + t * l["upt"]
                    if not _was_in and l not in o["lines"]:
                        o["lines"].append(l)
                    _pack_store(st_id, o["lines"])
                    return len(orders[st_id]["carts"]) <= n_carts

                lo, hi, best = 1, room, 0               # largest t that adds no cart
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if _fits(mid):
                        best, lo = mid, mid + 1
                    else:
                        hi = mid - 1
                if best:
                    _fits(best)
                else:
                    l["alloc_trays"], l["alloc_units"] = _ot, _ou
                    if not _was_in and l in o["lines"]:
                        o["lines"].remove(l)
                    orders[st_id]["carts"] = _old_carts
                o = orders[st_id]
                if not best:
                    continue
                u = best * l["upt"]
                pools.take(l["sku_id"], u, deny)
                l["fill_position_trays"] = l.get("fill_position_trays", 0) + best
                alloc_units_total += u
                fillpos["lines"] += 1
                fillpos["trays"] += best
                fillpos["units"] += u
                _hit = True
            if _hit:
                if _rel:
                    for c in o["carts"]:
                        c["econ_action"] = "release"
                _dw = sum(c["wholesale_value"] for c in o["carts"]) - _w0
                fillpos["stores"] += 1
                fillpos["wholesale"] += _dw
                decide("fill_open_positions",
                       f"Empty tray positions on the store's {n_carts} cart(s) filled with proven products: "
                       f"+${_dw:,.0f} wholesale, no extra cart (Jason's brief item 9)",
                       store_id=st_id, severity="info", wholesale_added=round(_dw, 2))
        fillpos["wholesale"] = round(fillpos["wholesale"], 2)
        log(f"Fill open positions: {fillpos['stores']:,} stores, {fillpos['trays']:,} trays / "
            f"{fillpos['units']:,}u (+${fillpos['wholesale']:,.0f}) on carts already shipping")

    # SUPPLY INVARIANT: no SKU is ever allocated beyond its availability (checked, not assumed)
    _alloc_by = defaultdict(int)
    for o_ in orders.values():
        for l in o_["lines"]:
            _alloc_by[l["sku_id"]] += int(l.get("alloc_units", 0) or 0)
    _over = {sk: (u, sum(pools.total[sk].values())) for sk, u in _alloc_by.items()
             if sk in pools.total and sum(pools.total[sk].values()) and u > sum(pools.total[sk].values())}
    if _over:
        raise ValueError(f"Allocation exceeds supply for {len(_over)} SKU(s): {dict(list(_over.items())[:5])}")
    log(f"Held trays: {reoffer['returned_units']:,}u returned by pushed/deferred orders · "
        f"{reoffer['reoffered_units']:,}u re-offered to {reoffer['stores']:,} shipping stores "
        f"({reoffer['lines']:,} lines still short of need; {reoffer.get('refused_would_add_cart', 0):,} refused, "
        f"no open cart space) · {reoffer['left_in_pool_units']:,}u left unallocated")

    # CART-CONFIG STREAMLINING across stores (leadership 2026-11-20 "consolidate carts when
    # alike"; Josh 2026-11-24: as few configs as possible while every store gets what it needs,
    # never a product it does not sell just to lower the config count). A cart in a rarer config
    # takes on a more common one when at most `config_merge_max_changes` shelf items differ and
    # every change passes, for THAT store: a product added is one the store sells (eligible line,
    # selling history, 13-week gates passed, store rules and variety bans allow it), in the same
    # family and pot size as what it replaces, with supply, inside the store's weeks-of-supply
    # ceiling; trays removed outside a same-family swap only come from above the store's need;
    # a full cart never becomes a less full one. Restricted, named-variety, directed and
    # pre-built carts never change. Done before kits, Aster lines and the totals below.
    merge = {"enabled": False, "configs_before": 0, "configs_after": 0, "carts_changed": 0,
             "swaps": 0, "trays_added": 0, "trays_removed": 0, "units_net": 0}
    if str(S.get("config_merge", True)).lower() not in ("false", "0", "no"):
        merge["enabled"] = True
        _mx = int(S.get("config_merge_max_changes", 2) or 0)
        _wos_m = {**{"AA": 10, "A": 10, "B": 8, "C+": 8, "C": 7, "C-": 6, "D": 6},
                  **{k: v for k, v in (S.get("gap_fill_weeks_supply_by_grade") or {}).items() if v}}

        def _m_ceiling(l):
            w = max(float(_wos_m.get(l.get("grade"), 6)), sell_down_wk(l))
            w = min(w, fresh_of.get(l["sku_id"], fresh_days) / 7.0)
            if l["family"] in H2O_TERR_FAMILIES:
                w = min(w, float(S["h2o_terrarium_window_weeks"]))
            if l.get("conf") == "UNUSABLE":
                w = min(w, 4.0)
            if l.get("oh_conflict"):
                w = min(w, oh_check_wk)
            _u = l["fc"] * w - max(0.0, l.get("projected", 0.0))
            if l.get("mcap") is not None:
                _u = min(_u, max(l.get("trays", 0), l["mcap"]) * l["upt"])
            return _u

        _mcarts, _rep = [], {}
        for st_id in sorted(orders):
            o = orders[st_id]
            _locked_skus = {l["sku_id"] for l in o["lines"]
                            if l.get("variety_codes") or l.get("directed_trays")}
            for c in o["carts"]:
                items = [(pid, sk, tr) for (_p, sk, tr, _u, _k, pid) in c["contents"]]
                sig = config_merge.cart_sig(items)
                locked = bool(c.get("handling_tag")) or any(
                    sk in _locked_skus or (sku_info[sk].get("family") or "") == "Prebuilt Cart"
                    for _, sk, _ in items)
                mc = {"id": (st_id, c["cart_number"]), "store": st_id, "cart": c, "sig": sig,
                      "locked": locked}
                _mcarts.append(mc)
                if not locked:
                    # the config's representative: its layout is what a merged cart adopts
                    cur_rep = _rep.get(sig)
                    if cur_rep is None or (c["phys_full"] and not cur_rep["phys_full"]):
                        _rep[sig] = c
        merge["configs_before"] = len({m["sig"] for m in _mcarts})

        def _m_line(st_id, by_sku, sk):
            return by_sku.get(sk) or ship.get((st_id, sk))

        _rej = Counter()

        def _m_try(mc, target, removed, added):
            st_id, o = mc["store"], orders[mc["store"]]
            rep = _rep.get(target)
            if rep is None or (mc["cart"]["phys_full"] and not rep["phys_full"]):
                _rej["would make a full cart less full"] += 1
                return False
            deny = store_deny.get(st_id, NO_DENY)
            by_sku = {l["sku_id"]: l for l in o["lines"]}
            dt = defaultdict(int)                            # net trays per SKU
            for _p, sk, tr in removed:
                dt[sk] -= tr
            for _p, sk, tr in added:
                dt[sk] += tr
            fam_units = defaultdict(int)
            plan_ = []
            for sk, d in dt.items():
                if d == 0:
                    continue
                l = _m_line(st_id, by_sku, sk)
                if l is None:
                    _rej["store does not sell the product"] += 1
                    return False
                fam_units[l["family"]] += d * l["upt"]
                plan_.append((sk, d, l))
            fams_swapped = {f for f, u in fam_units.items() if u == 0}
            rem_fp = {((_m_line(st_id, by_sku, r[1]) or {}).get("family"), r[0]) for r in removed}
            for sk, d, l in plan_:
                u = d * l["upt"]
                if d > 0:
                    why = ("store does not sell the product" if l.get("fc", 0) < min_rate else
                           "product held or stopped at this store" if (st_id, sk) in held else
                           "store rule" if (store_rule.get(st_id) and not store_rules.sku_allowed(
                               store_rule[st_id], sku_info[sk], directed=False)[0]) else
                           "no supply left" if pools.left(sk, deny) < u else
                           "over the store's weeks of supply" if l.get("alloc_units", 0) + u > _m_ceiling(l) + 1e-9
                           # §17: H2O/Terrarium are sized rounding DOWN and never exceed their own
                           # need; a config match is not a reason to send more (Racine 11/24)
                           else "over the product's tray limit" if (
                               l.get("tray_cap") is not None and l.get("alloc_trays", 0) + d > l["tray_cap"])
                           else "H2O/Terrarium above its own need" if (
                               l["family"] in H2O_TERR_FAMILIES and l.get("alloc_trays", 0) + d > l.get("trays", 0))
                           else "different family or pot size" if (l["family"], pack[sk]["profile_id"]) not in rem_fp
                           else None)
                    if why:
                        _rej[why] += 1
                        return False
                elif (l["family"] not in fams_swapped and l.get("alloc_trays", 0) + d < l.get("trays", 0)
                      and (l.get("alloc_units", 0) + u + max(0.0, l.get("projected", 0.0))
                           < l["fc"] * float(l.get("cadence_weeks") or 1) - 1e-9)):
                    # below the coverage target is allowed only while the store still has
                    # enough to sell until its next scheduled delivery — never run short
                    _rej["would leave the store short before its next delivery"] += 1
                    return False
            for sk, d, l in plan_:                            # commit
                u = d * l["upt"]
                if d > 0:
                    pools.take(sk, u, deny)
                    if l not in o["lines"]:
                        l.setdefault("alloc_trays", 0)
                        l.setdefault("alloc_units", 0)
                        o["lines"].append(l)
                        by_sku[sk] = l
                    merge["trays_added"] += d
                else:
                    pools.give_back(sk, -u, deny)
                    merge["trays_removed"] -= d
                l["alloc_trays"] = l.get("alloc_trays", 0) + d
                l["alloc_units"] = l.get("alloc_units", 0) + u
                l["config_merge_trays"] = l.get("config_merge_trays", 0) + d
                merge["units_net"] += u
            c = mc["cart"]
            new = [(pos, sk, tr, tr * by_sku[sk]["upt"], None, pid)
                   for (pos, sk, tr, _u, _k, pid) in rep["contents"]]
            c["contents"] = new
            wh = sum(x[3] * float(sku_info[x[1]].get("wholesale_price_whl") or 0) for x in new)
            c.update(total_units=sum(x[3] for x in new), wholesale_value=round(wh, 2),
                     shelves_used=rep["shelves_used"], tray_fill=rep["tray_fill"],
                     phys_full=rep["phys_full"], is_full=rep["is_full"], low_value=wh < low_value_wh,
                     freight_pct=round(cart_cost / wh, 4) if wh else None,
                     wholesale_incomplete=sorted({x[1] for x in new
                                                  if not (sku_info[x[1]].get("wholesale_price_whl") or 0)}),
                     config_merged=True)
            if {x[1] for x in removed} != {x[1] for x in added}:
                merge["swaps"] += 1
            return True

        _moves = config_merge.streamline(_mcarts, _mx, _m_try) if _mx > 0 else []
        alloc_units_total += merge["units_net"]
        merge["carts_changed"] = len({id(m[0]) for m in _moves})
        merge["configs_after"] = len({m["sig"] for m in _mcarts})
        merge["locked_carts"] = sum(1 for m in _mcarts if m["locked"])
        merge["rejected_checks"] = dict(_rej.most_common())
        for mc, _old, _new in _moves:
            decide("config_merge", f"Cart {mc['cart']['cart_number']} matched to a common cart config "
                                   f"(same family and pot size, products the store sells, within its "
                                   f"weeks of supply)", store_id=mc["store"], severity="info")
        log(f"Cart configs streamlined: {merge['configs_before']:,} -> {merge['configs_after']:,} configs · "
            f"{merge['carts_changed']:,} carts changed ({merge['swaps']:,} with a same-family swap) · "
            f"+{merge['trays_added']:,}/-{merge['trays_removed']:,} trays · net {merge['units_net']:+,}u · "
            f"{merge['locked_carts']:,} special carts untouched · rejected: {merge['rejected_checks']}")

    if fullsh["enabled"]:
        _full_shelves_pass("final")

    # FINAL FULL-CART GUARD (Jason 11/25: one-shelf carts found in the live run). Steps after the
    # trim (config merge edits carts in place) can leave a routine cart under the fill floor. At a
    # store with more than one cart, every routine cart must meet min_cart_fill; one that does not
    # is dropped and its trays go back to the pool. Directed / named-variety carts and pre-built
    # carts are never dropped, and a store always keeps at least one cart.
    guard = {"stores": 0, "carts": 0, "units": 0, "wholesale": 0.0}
    if trim["enabled"]:
        for store_id in sorted(orders):
            o = orders[store_id]
            if len(o["carts"]) <= 1:
                continue
            by_sku_g = {l["sku_id"]: l for l in o["lines"]}

            def _protected(c):
                return any((by_sku_g.get(x[1]) or {}).get("directed_trays")
                           or (by_sku_g.get(x[1]) or {}).get("variety_codes")
                           or (sku_info[x[1]].get("family") or "") == "Prebuilt Cart" for x in c["contents"])
            drop = sorted((c for c in o["carts"] if c["tray_fill"] < min_cart_fill - 1e-9 and not _protected(c)),
                          key=lambda c: (c["wholesale_value"], -c["cart_number"]))
            if not drop:
                continue
            _routine_g = [c for c in o["carts"] if not _protected(c)]
            _cc_other_g = [c for c in o["carts"] if _protected(c) and not all(
                (sku_info[x[1]].get("family") or "") == "Prebuilt Cart" for x in c["contents"])]
            _keep_min = 0 if _cc_other_g else 1     # a directed CC cart already carries the stop
            if len(drop) > len(_routine_g) - _keep_min:
                drop = drop[:max(0, len(_routine_g) - _keep_min)]
            if not drop:
                continue
            _deny_g = store_deny.get(store_id, NO_DENY)
            for c in drop:
                for (_pos, sk, tr, u, _k, _pid) in c["contents"]:
                    l = by_sku_g.get(sk)
                    if not l:
                        continue
                    tr_, u_ = min(tr, l.get("alloc_trays", 0)), min(u, l.get("alloc_units", 0))
                    l["alloc_trays"] -= tr_
                    l["alloc_units"] -= u_
                    l["trimmed_units"] = l.get("trimmed_units", 0) + u_
                    pools.give_back(sk, u_, _deny_g)
                    alloc_units_total -= u_
                    guard["units"] += u_
                guard["carts"] += 1
                guard["wholesale"] += c["wholesale_value"]
            o["carts"] = [c for c in o["carts"] if c not in drop]
            for i, c in enumerate(o["carts"], 1):
                c["cart_number"] = i
            o["lines"] = [l for l in o["lines"] if l.get("alloc_trays", 0) > 0 or l.get("directed_trays")]
            guard["stores"] += 1
            decide("partial_cart_trimmed",
                   f"Final check: {len(drop)} cart(s) under the {min_cart_fill:.0%} fill floor removed after "
                   f"cart-config matching; trays back to the pool", store_id=store_id, severity="info",
                   carts_cut=len(drop), stage="final_guard")
        guard["wholesale"] = round(guard["wholesale"], 2)
        log(f"Final full-cart guard: {guard['stores']:,} stores, {guard['carts']:,} under-filled carts removed "
            f"({guard['units']:,}u, ${guard['wholesale']:,.0f})")

    # SECOND-CHANCE CART BUILD (Jason 11/25: "cart building has to be iterative — if a proposed cart
    # fails, go back into the qualified demand pool, try alternative SKUs, recalculate expected
    # sales and build a better cart; do not simply abandon the store"). Cart completion runs once,
    # early, while stores that later get trimmed, cut to full shelves or pushed still hold their
    # stock (store 561: 0 Littles left at its turn, 12,000 unused at the end). After every step that
    # frees stock, each due store with no routine cart gets a second build from the freed pool,
    # AA/A/B first: its proven sellers, best next pack first, each product up to the quantity that
    # still sells cart_min_sellthrough of itself in the window, the cart's blended projected sell-
    # through >= cart_min_sellthrough, every shelf full, the cart >= min fill. Else nothing changes.
    #
    # MINIMUM CARTS PER STOP (routing review 11/25: 247 of 326 stores got ONE cart, so a 45-cart
    # truck needed ~33 stops against a cap of 20). Every stop (store in the order) must carry at
    # least min_carts_per_stop carts, counting routine, directed and pre-built carts. A store whose
    # only carts are directed / pre-built ships as approved (reported as an exception). Every other
    # store under the minimum first hands its ROUTINE trays back to the pool (all of them first,
    # so the stock single-cart stores held is free for everyone), then, AA/A/B first, is rebuilt
    # to the minimum from that pool with the second-chance rules (shared builder below). A store
    # that cannot reach the minimum gets no routine cart this week (its trays stay in the pool).
    second = {"enabled": str(S.get("second_chance_carts", True)).lower() not in ("false", "0", "no"),
              "stores_tried": 0, "stores": 0, "carts": 0, "units": 0, "wholesale": 0.0}
    _msv = S.get("min_carts_per_stop")
    min_stop = 2 if _msv is None else int(_msv or 0)
    stopmin = {"min": min_stop, "enabled": min_stop >= 2, "stores_under": 0, "stores_rebuilt": 0,
               "stores_held": 0, "units_held": 0, "wholesale_held": 0.0, "units_rebuilt": 0,
               "wholesale_rebuilt": 0.0, "held_by_reason": {}, "held_by_grade": {},
               "held_with_directed_cart": 0, "prebuilt_exceptions": 0, "capacity_exceptions": 0,
               "single_cart_exceptions_directed": 0, "stores_added_after_handback": 0,
               "carts_added_after_handback": 0, "wholesale_added_after_handback": 0.0,
               "keep_original_order": str(S.get("stop_minimum_keep_original_order", False)).lower()
               in ("true", "1", "yes")}
    _builder_on = bool(completion.get("enabled")) and min_full_carts > 0

    def _is_routine(l):
        return not (l.get("directed_trays") or l.get("variety_codes")
                    or (sku_info[l["sku_id"]].get("family") or "") == "Prebuilt Cart")

    def _cart_has_routine(c, by):
        return any(_is_routine(by.get(x[1]) or {"sku_id": x[1]}) for x in c["contents"])

    def _routine_shelves_not_full(c, by):
        """Shelves of a cart holding only routine product that are not full (ShelfKit)."""
        layers = defaultdict(list)
        for (pos, sk, tr, _u, _k, pid) in c["contents"]:
            layers[pos].append((pid, tr, sk))
        return sum(1 for its in layers.values()
                   if all(_is_routine(by.get(sk) or {"sku_id": sk}) for _, _, sk in its)
                   and SK.fits_shelf([(p, t) for p, t, _ in its])
                   and not SK.shelf_is_full([(p, t) for p, t, _ in its]))

    if stopmin["enabled"] and not _builder_on:
        stopmin["skipped"] = "needs cart_completion on and min_order_full_carts > 0"
        log("Stop minimum skipped: it builds carts with the cart-completion estimator, which is off")
    if _builder_on and (second["enabled"] or stopmin["enabled"]):
        _sc_lines = defaultdict(list)
        for (st_, sk_), l in ship.items():
            if (_is_routine(l) and l.get("plannable") and sk_ in pack and l["fc"] > 0
                    and not l.get("oh_conflict") and l.get("conf") != "UNUSABLE" and not l.get("reactivation")):
                _sc_lines[st_].append(l)
        for (st_, sk_), l in lines.items():
            if (st_, sk_) in ship or st_ not in _sc_lines or not l.get("cc_candidate"):
                continue
            _sc_lines[st_].append(l)
        _seed_lines, _seeds, _pack_miss = {}, {}, []                 # stop minimum: each store's handed-back order
        _ship_of = defaultdict(list)                 # store -> its ship lines (undo snapshots)
        for (st_, sk_), l in ship.items():
            _ship_of[st_].append(l)

        def _has_routine_cart(st_):
            o_ = orders.get(st_)
            return bool(o_) and any(_is_routine(l) and l.get("alloc_trays", 0) > 0 for l in o_["lines"])

        def _sc_cap(l):
            """Most packs for a line: proven demand up to cart_min_sellthrough of itself, tray limits."""
            _D = DF.sellable_demand(l["fc"], max(0.0, l.get("projected", 0.0)), line_horizon(l), mg_a)
            _p = max(1, l["upt"])
            c_ = max(DF.ceiling_packs(l["fc"], max(0.0, l.get("projected", 0.0)), line_horizon(l), _p, mg_a,
                                      line_f(l), min_share=1e-6),
                     int(_D / (cart_st_min * _p)) if cart_st_min > 0 else 0)
            if l["family"] in H2O_TERR_FAMILIES:
                c_ = min(c_, max(l.get("trays", 0), 0))          # never filler: own need only
            if l.get("tray_cap") is not None:
                c_ = min(c_, l["tray_cap"])
            return max(0, c_)

        def _store_prio(st_):
            ls_ = _sc_lines.get(st_) or (orders.get(st_) or {}).get("lines") or []
            return max((x.get("priority", 0) for x in ls_), default=0)

        def _build_plan(st_id, target, seed=None):
            """What a build for `target` carts in total starts from: (lines, caps, base estimator
            state of the store's protected non-variety lines, k = carts the estimator must fill,
            free-stock screen passed). seed {id(line): trays}: the store's own earlier routine
            order, kept as the starting point (stop_minimum_keep_original_order)."""
            seed = seed or {}
            ls = list(_sc_lines.get(st_id) or [])
            _ids = {id(l) for l in ls}
            ls += [l for l in _seed_lines.get(st_id, []) if id(l) in seed and id(l) not in _ids]
            deny = store_deny.get(st_id, NO_DENY)
            o_ = orders.get(st_id)
            pl = [l for l in (o_["lines"] if o_ else []) if l.get("alloc_trays", 0) > 0]
            by = {l["sku_id"]: l for l in pl}
            # named-variety carts are packed on their own; everything else shares the estimator
            n_ded = sum(1 for c in (o_["carts"] if o_ else []) if c["contents"]
                        and all((by.get(x[1]) or {}).get("variety_codes") for x in c["contents"]))
            base = _cc_state([l for l in pl if not _is_routine(l) and not l.get("variety_codes")],
                             lambda l: l.get("alloc_trays", 0))
            caps = {id(l): min(max(_sc_cap(l), seed.get(id(l), 0)),
                               int(pools.left(l["sku_id"], deny) // max(1, l["upt"]))) for l in ls}
            k = target - n_ded
            allst = {p: list(v) for p, v in base.items()}
            for p, v in _cc_state(ls, lambda l: caps[id(l)]).items():
                allst.setdefault(p, [0] + list(v[1:]))[0] += v[0]
            screen = bool(ls) and k > 0 and SR.top_k_full(_cc_cart_fills(allst, True), k, min_cart_fill)
            return ls, caps, base, k, screen

        def _snapshot(st_id, ls):
            """Everything a build attempt can change, so a failed attempt is undone exactly
            (lines, the pool for the store's SKUs, ship keys, the order, counters, decisions)."""
            o_ = orders.get(st_id)
            touched = {id(l): l for l in list(ls) + _ship_of.get(st_id, []) + (o_["lines"] if o_ else [])}
            return {"lines": [(l, dict(l)) for l in touched.values()],
                    "pool": {sk: dict(pools.rem[sk]) for sk in {l["sku_id"] for l in touched.values()}},
                    "ship_keys": {(st_id, l["sku_id"]) for l in ls if (st_id, l["sku_id"]) in ship},
                    "order": None if o_ is None else {"lines": list(o_["lines"]), "carts": o_["carts"]},
                    "alloc_total": alloc_units_total, "fullsh": dict(fullsh),
                    "n_dec": len(decisions), "n_pba": len(prebuilt_alone)}

        def _restore(sn, st_id, ls):
            nonlocal alloc_units_total
            for l, d in sn["lines"]:
                l.clear()
                l.update(d)
            for sk, d in sn["pool"].items():
                r_ = pools.rem[sk]
                r_.clear()
                r_.update(d)
            for l in ls:
                if (st_id, l["sku_id"]) not in sn["ship_keys"]:
                    ship.pop((st_id, l["sku_id"]), None)
            if sn["order"] is None:
                orders.pop(st_id, None)
            else:
                orders[st_id] = {"lines": list(sn["order"]["lines"]), "carts": sn["order"]["carts"]}
            alloc_units_total = sn["alloc_total"]
            fullsh.clear()
            fullsh.update(sn["fullsh"])
            del decisions[sn["n_dec"]:]
            del prebuilt_alone[sn["n_pba"]:]

        def _routine_sellthrough(o_):
            num = den = 0.0
            for l in o_["lines"]:
                q = l.get("alloc_units", 0)
                if q <= 0 or not _is_routine(l):
                    continue
                v = usd(l)
                num += DF.sold_units(q, l["fc"], max(0.0, l.get("projected", 0.0)), line_horizon(l),
                                     mg_a, line_f(l)) * v
                den += q * v
            return (num / den) if den else None

        def _plan_routine_carts(st_id, target, seed=None):
            """The estimator half of the builder (no commit): proven sellers, best next pack
            first, until the k fullest estimated carts reach min fill, then the blended
            sell-through test. Returns (ok, info with ls / got / k / caps or why)."""
            ls, caps, base, k, screen = _build_plan(st_id, target, seed)
            if not ls:
                return False, {"why": "no_proven_sellers", "tried": False}
            if k <= 0:
                return True, {"carts": 0, "units": 0, "wholesale": 0.0, "tried": False, "k": k}
            if not screen:
                return False, {"why": "free_stock_cannot_fill", "tried": False}
            got = defaultdict(int)
            state = {p: list(v) for p, v in base.items()}
            for l in ls:                          # the store's own earlier order first, if kept
                n0 = min(int((seed or {}).get(id(l), 0)), caps[id(l)])
                if n0 > 0:
                    got[id(l)] = n0
                    pid, cap, h, pmax = _cc_prof(l["sku_id"])
                    state.setdefault(pid, [0, cap, h, pmax])[0] += n0
            fills = _cc_cart_fills(state)
            full_fills = _cc_cart_fills(state, True)
            prog = SR.top_k_progress(fills, k)
            for _k in range(400 * k):
                # done when the k fullest carts are full on FULL shelves alone (partial shelves
                # would be cut by the every-shelf-full pass and the cart fall under min fill)
                if SR.top_k_full(full_fills, k, min_cart_fill):
                    break
                best = None
                for l in ls:
                    t = got[id(l)]
                    if t >= caps[id(l)]:
                        continue
                    mu = DF.marginal_units(t * l["upt"], l["upt"], l["fc"], max(0.0, l.get("projected", 0.0)),
                                           line_horizon(l), mg_a, line_f(l))
                    if mu <= 1e-9:
                        continue
                    pid, cap, h, pmax = _cc_prof(l["sku_id"])
                    trial = {p: list(v) for p, v in state.items()}
                    trial.setdefault(pid, [0, cap, h, pmax])[0] += 1
                    f2 = _cc_cart_fills(trial)
                    p2 = SR.top_k_progress(f2, k)
                    if p2 <= prog + 1e-9:
                        continue
                    key = (mu * usd(l), l.get("sku_fit", 0))
                    if best is None or key > best[0]:
                        best = (key, l, trial, f2, p2)
                if not best:
                    break
                got[id(best[1])] += 1
                state, fills, prog = best[2], best[3], best[4]
                full_fills = _cc_cart_fills(state, True)
            if not SR.top_k_full(full_fills, k, min_cart_fill):
                return False, {"why": "cannot_fill_carts", "tried": True}
            _num = _den = 0.0
            for l in ls:
                q = got[id(l)] * l["upt"]
                if q:
                    v = usd(l)
                    _num += DF.sold_units(q, l["fc"], max(0.0, l.get("projected", 0.0)), line_horizon(l),
                                          mg_a, line_f(l)) * v
                    _den += q * v
            if cart_st_min > 0 and _den and _num / _den < cart_st_min:
                return False, {"why": "sellthrough_below_target", "tried": True}
            return True, {"tried": True, "ls": ls, "got": got, "k": k, "caps": caps,
                          "est_fills": [round(f, 3) for f in sorted(full_fills, reverse=True)]}

        def _build_routine_carts(st_id, target, stage, seed=None):
            """Build full routine carts for one store from the FREE pool until it has `target`
            carts in total (directed / pre-built carts it already has count; they never change).
            Its proven sellers only (marginal units > 0), each product up to _sc_cap and supply,
            best next-pack sales first; the estimator's `k` fullest carts must reach min fill, the
            routine lines' blended projected sell-through >= cart_min_sellthrough, then the REAL
            packer and the every-shelf-full pass must give >= target carts, every routine cart
            >= min fill, every routine shelf full, sell-through still met. Else everything is
            undone exactly. Returns (ok, info)."""
            nonlocal alloc_units_total
            ok_, plan_ = _plan_routine_carts(st_id, target, seed)
            if not ok_ or plan_.get("k", 1) <= 0:
                return ok_, plan_
            ls, got = plan_["ls"], plan_["got"]
            # commit, pack, complete shelves; verify with the real packer
            deny = store_deny.get(st_id, NO_DENY)
            sn = _snapshot(st_id, ls)
            o_prev = orders.get(st_id)
            prev_lines = list(o_prev["lines"]) if o_prev else []
            taken, _u = [], 0
            for l in ls:
                n = got[id(l)]
                if n <= 0:
                    continue
                u = n * l["upt"]
                pools.take(l["sku_id"], u, deny)
                taken.append(l)
                l["alloc_trays"], l["alloc_units"] = n, u
                l.pop("alloc_in_pool", None)           # its stock is now properly taken from the pool
                l["second_chance_trays" if stage == "second_chance" else "stop_minimum_trays"] = n
                l["deferred_units"] = 0
                ship[(st_id, l["sku_id"])] = l
                _u += u
            alloc_units_total += _u
            new_lines = prev_lines + [l for l in taken if l not in prev_lines]
            _pack_store(st_id, [l for l in new_lines if l.get("alloc_trays", 0) > 0])
            if fullsh["enabled"]:
                _full_shelves_pass(stage, only={st_id})

            def _verify():
                o_ = orders.get(st_id)
                if not o_ or not o_["carts"]:
                    return "no_cart_after_packing", o_, [], None
                by = {l["sku_id"]: l for l in o_["lines"]}
                rc = [c for c in o_["carts"] if _cart_has_routine(c, by)]
                st_ = _routine_sellthrough(o_)
                if not rc:
                    return "no_routine_cart_after_packing", o_, rc, st_
                if len(o_["carts"]) < target:
                    return "real_pack_below_target_carts", o_, rc, st_
                if any(c["tray_fill"] < min_cart_fill - 1e-9 for c in rc):
                    return "real_pack_cart_under_fill", o_, rc, st_
                if fullsh["enabled"] and any(_routine_shelves_not_full(c, by) for c in rc):
                    return "shelf_not_full", o_, rc, st_
                if cart_st_min > 0 and st_ is not None and st_ < cart_st_min:
                    return "sellthrough_below_target", o_, rc, st_
                return None, o_, rc, st_
            why, o_, rc, st_ = _verify()
            if why == "real_pack_cart_under_fill":
                # the real packer laid the trays out as `target` full carts plus a short extra one:
                # the short cart's routine trays go back to the pool, the store is re-packed and
                # checked again (iterate rather than abandon the store)
                by = {l["sku_id"]: l for l in o_["lines"]}
                short = [c for c in rc if c["tray_fill"] < min_cart_fill - 1e-9]
                if (len(o_["carts"]) - len(short) >= target
                        and all(_is_routine(by.get(x[1]) or {"sku_id": x[1]}) for c in short for x in c["contents"])):
                    for c in short:
                        for (_pos, sk, tr, u, _kt, _pid) in c["contents"]:
                            l = by.get(sk)
                            if not l:
                                continue
                            tr_, u_ = min(tr, l.get("alloc_trays", 0)), min(u, l.get("alloc_units", 0))
                            l["alloc_trays"] -= tr_
                            l["alloc_units"] -= u_
                            pools.give_back(sk, u_, deny)
                            alloc_units_total -= u_
                    _pack_store(st_id, [l for l in o_["lines"] if l.get("alloc_trays", 0) > 0])
                    if fullsh["enabled"]:
                        _full_shelves_pass(stage, only={st_id})
                    why, o_, rc, st_ = _verify()
            if why:
                _real = ([round(c["tray_fill"], 3) for c in o_["carts"]] if o_ else [])
                _restore(sn, st_id, ls)
                if len(_pack_miss) < 12:
                    _pack_miss.append({"store": st_id, "stage": stage, "why": why, "target": target,
                                       "estimated_full_shelf_fills": plan_.get("est_fills"),
                                       "real_fills": _real})
                return False, {"why": why, "tried": True}
            return True, {"tried": True, "carts": len(rc), "units": sum(l.get("alloc_units", 0) for l in o_["lines"]
                                                                       if _is_routine(l)),
                          "wholesale": sum(c["wholesale_value"] for c in rc), "sellthrough": st_}

        def _protected_cart_count(st_):
            o_ = orders.get(st_)
            if not o_:
                return 0
            by = {l["sku_id"]: l for l in o_["lines"]}
            return sum(1 for c in o_["carts"] if not _cart_has_routine(c, by))

        # SECOND-CHANCE CART BUILD (see above). With a stop minimum of 2+, the build aims for the
        # minimum, so it never creates a new single-cart stop.
        if second["enabled"]:
            _sc_stores = [st_ for st_ in _sc_lines if not _has_routine_cart(st_)
                          and not (stores.get(st_) or {}).get("capacity_carts")]
            _sc_stores.sort(key=lambda st_: (-max(x.get("priority", 0) for x in _sc_lines[st_]), st_))
            _sfr = Counter()
            for st_id in _sc_stores:
                _tgt = SR.routine_target(min_stop if stopmin["enabled"] else 0, _protected_cart_count(st_id))
                ok, info = _build_routine_carts(st_id, _tgt, "second_chance")
                second["stores_tried"] += bool(info.get("tried"))
                if not ok or not info.get("carts"):
                    if info.get("tried"):
                        _sfr[info.get("why") or "?"] += 1
                    continue
                second["stores"] += 1
                second["carts"] += info["carts"]
                second["units"] += info["units"]
                second["wholesale"] += info["wholesale"]
                decisions[:] = [d_ for d_ in decisions if not (d_[0] == "pushed_min_cart" and d_[1] == st_id)]
                _sv = info.get("sellthrough")
                decide("second_chance_cart",
                       f"Second cart build from stock freed later in the run: {info['carts']} full routine "
                       f"cart(s), {info['units']:,}u, projected blended sell-through {(_sv or 0):.0%} in the "
                       f"selling window (Jason 11/25: iterate, do not abandon the store)", store_id=st_id,
                       severity="info", units=info["units"], sellthrough=round(_sv, 4) if _sv is not None else None)
            second["wholesale"] = round(second["wholesale"], 2)
            second["failed_by_reason"] = dict(_sfr.most_common())
            log(f"Second-chance carts: {second['stores']:,} of {second['stores_tried']:,} stores rebuilt "
                f"({second['carts']:,} carts, {second['units']:,}u, ${second['wholesale']:,.0f})")

        # MINIMUM CARTS PER STOP: hand back every under-minimum store's routine trays first, then
        # rebuild best stores first. Stores at or above the minimum are never reduced.
        if stopmin["enabled"]:
            under, _before = [], {}
            for st_id in sorted(orders):
                o_ = orders[st_id]
                n_c = len(o_["carts"])
                if n_c == 0 or n_c >= min_stop:
                    continue
                by = {l["sku_id"]: l for l in o_["lines"]}
                if not any(_is_routine(l) and l.get("alloc_trays", 0) > 0 for l in o_["lines"]):
                    continue                     # directed / pre-built only: approved, ships as is
                _prot = [l for l in o_["lines"] if not _is_routine(l) and l.get("alloc_trays", 0) > 0]
                if _prot and all((sku_info[l["sku_id"]].get("family") or "") == "Prebuilt Cart" for l in _prot):
                    stopmin["prebuilt_exceptions"] += 1      # a pre-built cart cannot travel alone
                    decide("stop_minimum_exception",
                           f"{n_c} cart(s), under the {min_stop}-cart stop minimum, kept: its pre-built cart "
                           f"only travels with a regular cart", store_id=st_id, severity="warning", carts=n_c)
                    continue
                _capc = (stores.get(st_id) or {}).get("capacity_carts")
                if _capc and int(_capc) < min_stop:
                    stopmin["capacity_exceptions"] += 1
                    continue
                under.append(st_id)
                _before[st_id] = (n_c, sum(c["wholesale_value"] for c in o_["carts"] if _cart_has_routine(c, by)))
            stopmin["stores_under"] = len(under)
            _held_u = {}
            for st_id in under:                   # 1) every under-minimum store's routine trays back
                o_ = orders[st_id]
                deny = store_deny.get(st_id, NO_DENY)
                hu = 0
                _seed_lines[st_id], _seeds[st_id] = [], {}
                for l in o_["lines"]:
                    if not _is_routine(l) or (l.get("alloc_trays", 0) <= 0 and not l.get("alloc_units")):
                        continue
                    _seed_lines[st_id].append(l)
                    _seeds[st_id][id(l)] = int(l.get("alloc_trays", 0) or 0)
                    u = int(l.get("alloc_units", 0) or 0)
                    if not l.get("alloc_in_pool"):
                        pools.give_back(l["sku_id"], u, deny)
                    l.pop("alloc_in_pool", None)
                    alloc_units_total -= u
                    hu += u
                    l["deferred_units"] = u
                    l["alloc_units"] = l["alloc_trays"] = 0
                _held_u[st_id] = hu
                keep = [l for l in o_["lines"] if l.get("alloc_trays", 0) > 0]
                if keep:
                    _pack_store(st_id, keep)
                else:
                    del orders[st_id]
            # 2) rebuild to the minimum, AA/A/B first. Due stores with no routine cart (second
            # chance found too little stock earlier; directed-only stops) compete for the freed
            # stock in the same priority order: their build also aims for the stop minimum.
            _under_set = set(under)
            _extra = [st_ for st_ in _sc_lines if st_ not in _under_set and not _has_routine_cart(st_)
                      and not (stores.get(st_) or {}).get("capacity_carts")]
            _hr, _hg = Counter(), Counter()
            for st_id in sorted(under + _extra, key=lambda st_: (-_store_prio(st_), st_)):
                if st_id not in _under_set:
                    _tgt = SR.routine_target(min_stop, _protected_cart_count(st_id))
                    _n0 = len((orders.get(st_id) or {}).get("carts") or [])
                    ok, info = _build_routine_carts(st_id, _tgt, "stop_minimum")
                    if ok and info.get("carts"):
                        stopmin["stores_added_after_handback"] += 1
                        stopmin["carts_added_after_handback"] += info["carts"]
                        stopmin["wholesale_added_after_handback"] += info["wholesale"]
                        decisions[:] = [d_ for d_ in decisions
                                        if not (d_[0] == "pushed_min_cart" and d_[1] == st_id)]
                        decide("stop_minimum_rebuilt",
                               f"Built from stock freed by the {min_stop}-cart stop minimum: {info['carts']} full "
                               f"routine cart(s) ({_n0} directed / pre-built before), {info['units']:,}u, projected "
                               f"sell-through {(info.get('sellthrough') or 0):.0%}", store_id=st_id,
                               severity="info", units=info["units"], carts_before=_n0,
                               carts_after=len(orders[st_id]["carts"]))
                    continue
                ok, info = _build_routine_carts(st_id, min_stop, "stop_minimum",
                                                _seeds.get(st_id) if stopmin["keep_original_order"] else None)
                n0, wh0 = _before[st_id]
                if ok and info.get("carts"):
                    stopmin["stores_rebuilt"] += 1
                    stopmin["units_rebuilt"] += info["units"]
                    stopmin["wholesale_rebuilt"] += info["wholesale"]
                    decisions[:] = [d_ for d_ in decisions if not (d_[0] == "pushed_min_cart" and d_[1] == st_id)]
                    decide("stop_minimum_rebuilt",
                           f"Rebuilt from {n0} to {len(orders[st_id]['carts'])} carts to meet the {min_stop}-cart "
                           f"stop minimum (routing: max {int(S.get('max_stops_per_truck', 20) or 20)} stops per "
                           f"truck): {info['carts']} full routine cart(s), {info['units']:,}u, projected "
                           f"sell-through {(info.get('sellthrough') or 0):.0%}", store_id=st_id, severity="info",
                           units=info["units"], carts_before=n0, carts_after=len(orders[st_id]["carts"]))
                    continue
                _g = store_cadence(st_id)[0]
                stopmin["stores_held"] += 1
                stopmin["units_held"] += _held_u[st_id]
                stopmin["wholesale_held"] += wh0
                stopmin["held_with_directed_cart"] += st_id in orders
                _hr[info.get("why") or "?"] += 1
                _hg[_g] += 1
                decide("held_stop_minimum",
                       f"Routine order held this week ({_held_u[st_id]:,}u, ${wh0:,.0f} wholesale): it fills "
                       f"{n0} cart(s) and the stock left could not build the {min_stop}-cart stop minimum with "
                       f"demand-backed, full, selling-through carts ({info.get('why')}). Trays stay in the pool.",
                       store_id=st_id, severity="info", units=_held_u[st_id], wholesale=round(wh0, 2),
                       carts_before=n0, why=info.get("why"), grade=_g)
            stopmin["held_by_reason"] = dict(_hr.most_common())
            stopmin["real_pack_rejections_examples"] = _pack_miss
            stopmin["held_by_grade"] = dict(_hg)
            stopmin["wholesale_held"] = round(stopmin["wholesale_held"], 2)
            stopmin["wholesale_rebuilt"] = round(stopmin["wholesale_rebuilt"], 2)
            stopmin["wholesale_added_after_handback"] = round(stopmin["wholesale_added_after_handback"], 2)
            log(f"Stop minimum ({min_stop} carts): {stopmin['stores_under']:,} stores under · "
                f"{stopmin['stores_rebuilt']:,} rebuilt · {stopmin['stores_added_after_handback']:,} more stores "
                f"built from the freed stock · {stopmin['stores_held']:,} held "
                f"({stopmin['units_held']:,}u, ${stopmin['wholesale_held']:,.0f}) · reasons {stopmin['held_by_reason']}")

        # DEMAND-CONVERSION AUDIT (Josh 11/25: "the audits should catch these"): every due store left
        # without a routine cart is tested against the stock still free at the END of the run. If its
        # proven sellers and that stock could have built its full carts (the stop minimum when on)
        # by the builder's own plan — demand-backed packs, full estimated carts, blended sell-through
        # met — qualified demand was lost between forecasting, allocation and cart building: a
        # warning per store, counted by grade. Stores where free stock exists but no such plan does
        # are counted apart (stores_stock_but_no_qualified_plan), not flagged.
        lost = {"stores": 0, "by_grade": Counter(), "units_possible": 0, "examples": [], "stock_only": 0}
        for st_id in _sc_lines:
            if _has_routine_cart(st_id) or (stores.get(st_id) or {}).get("capacity_carts"):
                continue
            _tgt = SR.routine_target(min_stop if stopmin["enabled"] else 0, _protected_cart_count(st_id))
            ok_, plan_ = _plan_routine_carts(st_id, _tgt)
            if plan_.get("tried") and not ok_:
                lost["stock_only"] += 1          # free stock was there, but no full selling cart plan
            if not ok_ or plan_.get("k", 0) <= 0:
                continue
            ls, _k = plan_["ls"], plan_["k"]
            _g = store_cadence(st_id)[0]
            _pu = sum(plan_["got"][id(l)] * l["upt"] for l in ls)
            lost["stores"] += 1
            lost["by_grade"][_g] += 1
            lost["units_possible"] += _pu
            if len(lost["examples"]) < 15:
                lost["examples"].append(st_id)
            decide("demand_lost_audit", f"AUDIT: no routine cart, yet its proven sellers and the stock still "
                                        f"free could build {_k} full cart(s) ({_pu:,}u possible). Demand was "
                                        f"lost between allocation and cart building — investigate before release.",
                   store_id=st_id, severity="warning", grade=_g, units_possible=_pu)
        plant_left = Counter()
        for sk_, info_ in sku_info.items():
            if sk_ in pools.total and not str(info_.get("ipr_family") or "").startswith(("Climb-ITT", "H2O")):
                plant_left[info_.get("ipr_family") or "?"] += int(pools.left(sk_))
        second["demand_lost_audit"] = {"stores": lost["stores"], "by_grade": dict(lost["by_grade"]),
                                       "stores_stock_but_no_qualified_plan": lost["stock_only"],
                                       "units_possible": lost["units_possible"], "examples": lost["examples"],
                                       "plant_stock_left_by_family": dict(plant_left.most_common(8))}
        log(f"Demand-conversion audit: {lost['stores']:,} due stores without a routine cart COULD have had "
            f"their full carts from free stock ({dict(lost['by_grade'])})")

    # SUPPLY INVARIANT, again after the second-chance / stop-minimum rebuilds
    _alloc_by2 = defaultdict(int)
    for o_ in orders.values():
        for l in o_["lines"]:
            _alloc_by2[l["sku_id"]] += int(l.get("alloc_units", 0) or 0)
    _over2 = {sk: (u, sum(pools.total[sk].values())) for sk, u in _alloc_by2.items()
              if sk in pools.total and sum(pools.total[sk].values()) and u > sum(pools.total[sk].values())}
    if _over2:
        raise ValueError(f"Allocation exceeds supply after the stop-minimum rebuild for {len(_over2)} SKU(s): "
                         f"{dict(list(_over2.items())[:5])}")

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
    # CERAMIC-ONLY FAMILIES (Josh 2026-11-24: "we don't have any 12cm jute on hand, so all 12cm
    # units ship in their ceramic pot with ceramic kit codes"). Families listed in
    # kit_ceramic_only_families never get a jute kit; every unit is ceramic_kit, so the Aster
    # file carries the variety's 3M ceramic code.
    _cer_only = {str(f).strip() for f in (S.get("kit_ceramic_only_families", ["12cm"]) or []) if str(f).strip()}
    if _cer_only:
        for _sk in list(kit_opts) + [l["sku_id"] for o_ in orders.values() for l in o_["lines"]]:
            if (sku_info.get(_sk) or {}).get("family") in _cer_only:
                kit_opts[_sk] = {"ceramic_kit"}
    kit_units = defaultdict(lambda: {"jute_kit": 0, "ceramic_kit": 0})
    kit_tie = defaultdict(bool)
    # Identical carts get identical kits: one decision per cart config, weighted by how many
    # carts share it, so the kit never splits one config into two (Josh 2026-11-24).
    _kgroups = defaultdict(list)
    for store_id in sorted(orders):
        for c in orders[store_id]["carts"]:
            _kgroups[(tuple(sorted((p, sk, tr, pid) for (p, sk, tr, _u, _k, pid) in c["contents"])),
                      c.get("handling_tag") or "")].append(c)
    for _gk, _gcarts in sorted(_kgroups.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
        _n = len(_gcarts)
        for c in _gcarts[:1]:
            new = []
            for (pos, sku_id, trays, units, _k, pid) in c["contents"]:
                units = units * _n                  # decided for the whole config at once
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
                new.append((pos, sku_id, kit))
            _kit_at = {(p, sk): k for (p, sk, k) in new}
            for cc in _gcarts:
                cc["contents"] = [(p, sk, tr, u, _kit_at[(p, sk)], pid)
                                  for (p, sk, tr, u, _k, pid) in cc["contents"]]

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

    # IDEAL PRODUCTION by family (release report): what stores sell per week, what they need
    # now before any gate, what the greenhouse has, what ships, and the gap either way.
    _prod = defaultdict(lambda: {"weekly_demand_units": 0.0, "weekly_demand_usd": 0.0, "need_units": 0,
                                 "need_usd": 0.0, "available_units": 0, "shipped_units": 0})
    for l in lines.values():
        _p = _prod[l["family"]]
        _p["weekly_demand_units"] += l["fc"]
        _p["weekly_demand_usd"] += l["fc"] * usd(l)
        _u = l.get("trays_unreduced", l["trays"]) * l["upt"]
        _p["need_units"] += _u
        _p["need_usd"] += _u * usd(l)
    if not what_if_unlimited:
        for _sk, _u in supply.items():
            _prod[(sku_info.get(_sk) or {}).get("ipr_family") or "Unmapped"]["available_units"] += int(_u)
    for _o in orders.values():
        for l in _o["lines"]:
            _prod[l["family"]]["shipped_units"] += l["alloc_units"]
    for _p in _prod.values():
        _p["weekly_demand_units"] = round(_p["weekly_demand_units"])
        _p["weekly_demand_usd"] = round(_p["weekly_demand_usd"])
        _p["need_usd"] = round(_p["need_usd"])
        _p["short_units"] = max(0, _p["need_units"] - _p["available_units"])
        _p["surplus_units"] = max(0, _p["available_units"] - _p["need_units"])
    # The same picture SKU by SKU, for the growing team's production review (2026-11-23).
    # Every SKU stores sell is here, on this availability or not: a SKU that is not on it has
    # weekly demand but no need_units (the engine only sizes orders for what it can ship).
    _psku = defaultdict(lambda: {"weekly_demand_units": 0.0, "weekly_demand_usd": 0.0, "stores": 0,
                                 "need_units": 0, "shipped_units": 0, "plannable": False})
    for l in lines.values():
        _p = _psku[l["sku_id"]]
        _p["weekly_demand_units"] += l["fc"]
        _p["weekly_demand_usd"] += l["fc"] * usd(l)
        _p["stores"] += 1 if l["fc"] > 0 else 0
        _p["need_units"] += l.get("trays_unreduced", l["trays"]) * l["upt"]
        _p["plannable"] = _p["plannable"] or bool(l.get("plannable"))
    for _o in orders.values():
        for l in _o["lines"]:
            _psku[l["sku_id"]]["shipped_units"] += l["alloc_units"]
    _by_sku = {}
    for _sk in set(_psku) | (set() if what_if_unlimited else set(supply)):
        _p, _i = _psku[_sk], (sku_info.get(_sk) or {})
        _by_sku[str(_sk)] = {
            "description": (_i.get("description") or "").strip(), "family": _i.get("ipr_family") or "Unmapped",
            "weekly_demand_units": round(_p["weekly_demand_units"], 1),
            "weekly_demand_usd": round(_p["weekly_demand_usd"]), "stores": _p["stores"],
            "need_units": int(_p["need_units"]), "shipped_units": int(_p["shipped_units"]),
            "available_units": 0 if what_if_unlimited else int(supply.get(_sk, 0)),
            "on_availability": (not what_if_unlimited) and _sk in supply, "plannable": _p["plannable"]}
    production = {
        "by_sku": _by_sku,
        "by_family": {f: dict(v) for f, v in sorted(_prod.items())},
        "totals": {k: sum(v[k] for v in _prod.values()) for k in
                   ("weekly_demand_units", "weekly_demand_usd", "need_units", "need_usd",
                    "available_units", "shipped_units", "short_units", "surplus_units")},
        "note": "need = units to fill every store's need this week before any gate; short / surplus "
                "compare that need with this availability, family by family",
    }
    # Grade readout: what each grade has, and what this plan takes from it when the preferred
    # grade is picked first within each SKU (variety restrictions can shift a little at write).
    grades = None
    if line_grade:
        _ga, _gs = defaultdict(int), defaultdict(int)
        _by_sku = defaultdict(lambda: defaultdict(int))
        for r in avail_rows:
            if r["grade"] is not None:
                _ga[int(r["grade"])] += int(r["units_available"] or 0)
                _by_sku[r["sku_id"]][int(r["grade"])] += int(r["units_available"] or 0)
        _ship_sku = defaultdict(int)
        for _o in orders.values():
            for l in _o["lines"]:
                _ship_sku[l["sku_id"]] += l["alloc_units"]
        for _sk, _u in _ship_sku.items():
            _left = _u
            for _g in sorted(_by_sku.get(_sk, {}), key=lambda g: (g != grade_first, -g)):
                _t = min(_left, _by_sku[_sk][_g])
                _gs[_g] += _t
                _left -= _t
        grades = {"picked_first": grade_first or None,
                  "by_grade": {str(g): {"available_units": _ga[g], "shipping_units": _gs.get(g, 0),
                                        "left_units": _ga[g] - _gs.get(g, 0)} for g in sorted(_ga, reverse=True)},
                  "note": "grade 3 = more mature plants and contract-grower stock; it is used before "
                          "grade 2 within each SKU. Grade never changes how much a store is sent."}
    _dh_summary = None
    if H:
        _mc = Counter()
        for _l in lines.values():
            _m = _l.get("method") or ""
            _mc["limited history (comparables)" if _m.startswith("limited history") else
                "benchmark floor" if "floor" in _m else
                "fewer than 13 selling weeks" if "selling weeks of the last 13" in _m else
                "13 of 13 selling weeks" if _m.startswith("blend 4/10/13 over") else "other"] += 1
        _md_units = sum(H["md_rate"].values())
        _dh_summary = {
            "basis": "13 months weekly; rates over selling weeks (launched, reporting, stock on hand)",
            "benchmark_periods": H["periods"], "benchmark_index_to_primary": H["bench_index"],
            "benchmark_lines": len(H["bench"]),
            "benchmark_seasonal_multiplier": round(peak_mult, 3),
            "mothers_day": {"date": str(H["md_date"]), "lines": len(H["md_rate"]),
                            "units": round(_md_units),
                            "note": "measured separately; never part of a routine benchmark"},
            "lines_by_method": dict(_mc),
            "growth_vs_coverage": H["growth"],
        }
    # Stockout recovery and shelf checks (Jason's review 11/24): every shipped line is recorded
    # with its evidence; shelf checks are the DSM's to-do list before release.
    _shipped = [l for o in orders.values() for l in o["lines"] if l.get("alloc_trays", 0) > 0]
    so_summary = {"enabled": so_on, "min_sellthrough": so_min_st, "seasonal_index": so_idx,
                  "lines_found": sum(1 for l in lines.values() if l.get("stockout_recovery")),
                  "lines_shipped": 0, "stores": 0, "units": 0, "wholesale": 0.0, "by_family": {}}
    _so_st = set()
    for l in _shipped:
        r = l.get("stockout_recovery")
        if not r:
            continue
        u = l["alloc_units"]
        so_summary["lines_shipped"] += 1
        so_summary["units"] += u
        so_summary["wholesale"] += u * usd(l)
        so_summary["by_family"][l["family"]] = so_summary["by_family"].get(l["family"], 0) + u
        _so_st.add(l["store_id"])
        decide("stockout_recovery", f"{l['alloc_trays']} tray(s) ({u}u): sold {r['sold_since']:.0f} of "
                                    f"{r['delivered']:.0f} delivered {r['weeks_since_delivery']} wks ago, out of "
                                    f"stock since; planned at {r['period']} {r['benchmark']:.1f}u/wk x "
                                    f"{so_idx:.2f} = {l['fc']:.1f}u/wk",
               store_id=l["store_id"], sku_id=l["sku_id"], severity="info", family=l["family"],
               units=u, **{k: r[k] for k in ("delivered", "sold_since", "sell_through",
                                             "weeks_since_delivery", "weeks_since_sale", "benchmark")})
    so_summary["stores"] = len(_so_st)
    so_summary["wholesale"] = round(so_summary["wholesale"], 2)
    oc_summary = {"mode": oh_check, "cap_weeks": oh_check_wk,
                  "lines_found": sum(1 for l in lines.values() if l.get("oh_conflict") and l["plannable"]),
                  "lines_shipped": 0, "stores": 0, "units_shipped": 0, "units_capped_off": 0}
    _oc_st = set()
    for l in _shipped:
        oc = l.get("oh_conflict")
        if not oc:
            continue
        cut = max(0, oc.get("trays_before_cap", 0) - l["trays"]) * l["upt"]
        oc_summary["lines_shipped"] += 1
        oc_summary["units_shipped"] += l["alloc_units"]
        oc_summary["units_capped_off"] += cut
        _oc_st.add(l["store_id"])
        decide("onhand_check", f"Shelf check: DTS shows {oc['dts_onhand']}u on hand ({oc['snapshot']}) but "
                               f"deliveries less sales leave {oc['ledger_onhand']:.0f}u and nothing sold in "
                               f"{oc['quiet_weeks']} wks. Shipping {l['alloc_units']}u"
                               + (f" (capped from {oc['trays_before_cap'] * l['upt']}u)" if cut else "")
                               + ". DSM: confirm the shelf before release.",
               store_id=l["store_id"], sku_id=l["sku_id"], severity="warning", family=l["family"],
               units=l["alloc_units"], units_capped_off=cut, **oc)
    oc_summary["stores"] = len(_oc_st)
    log(f"Stockout recovery: {so_summary['lines_found']:,} sold-out lines found · {so_summary['lines_shipped']:,} "
        f"shipping ({so_summary['units']:,}u, ${so_summary['wholesale']:,.0f}) at {so_summary['stores']:,} stores · "
        f"shelf checks: {oc_summary['lines_shipped']:,} shipping lines at {oc_summary['stores']:,} stores "
        f"({oc_summary['units_capped_off']:,}u capped off)")
    # SHIPMENT FORECAST (Jason's agent direction section 9): what THIS shipment is projected to sell,
    # per store x SKU, F(q,h) at 35 days and at the grade horizon (D 70 days), base and slower
    # case; routine only (directed programs reported apart); rates are ratios of sums by value.
    fc_sum = {"routine_units": 0, "routine_wholesale": 0.0, "sold35_units": 0.0, "sold35_wholesale": 0.0,
              "soldh_units": 0.0, "soldh_wholesale": 0.0, "slower_h_wholesale": 0.0,
              "by_grade": defaultdict(lambda: {"stores": set(), "wholesale": 0.0, "sold35": 0.0, "soldh": 0.0}),
              "lines_last_pack_low": 0}
    for l in _shipped:
        if l.get("directed_trays") or l.get("variety_codes"):
            continue
        q, r_, s_ = l["alloc_units"], l["fc"], max(0.0, l.get("projected", 0.0))
        v = float((sku_info.get(l["sku_id"]) or {}).get("wholesale_price_whl") or 0)
        f_ = line_f(l)
        h_ = line_horizon(l)
        u35 = DF.sold_units(q, r_, s_, DF.HORIZON_WEEKS, mg_a, f_)
        uh = DF.sold_units(q, r_, s_, h_, mg_a, f_)
        us = DF.sold_units(q, r_, s_, h_, DF.SLOWER["a"],
                           DF.SLOWER["f_hard"] if l.get("accessory") else DF.SLOWER["f_plant"])
        last = DF.marginal_units(max(0, q - l["upt"]), l["upt"], r_, s_, h_, mg_a, f_)
        l["forecast"] = {"horizon_wks": h_, "sold35_units": round(u35, 2), "sold_h_units": round(uh, 2),
                         "sold35_wholesale": round(u35 * v, 2), "sold_h_wholesale": round(uh * v, 2),
                         "last_pack_units": round(last, 2), "rate": round(r_, 3), "stock_at_receipt": round(s_, 1)}
        if last < mg_share * l["upt"] * f_:
            fc_sum["lines_last_pack_low"] += 1
        fc_sum["routine_units"] += q
        fc_sum["routine_wholesale"] += q * v
        fc_sum["sold35_units"] += u35
        fc_sum["sold35_wholesale"] += u35 * v
        fc_sum["soldh_units"] += uh
        fc_sum["soldh_wholesale"] += uh * v
        fc_sum["slower_h_wholesale"] += us * v
        g_ = fc_sum["by_grade"][store_cadence(l["store_id"])[0]]
        g_["stores"].add(l["store_id"])
        g_["wholesale"] += q * v
        g_["sold35"] += u35 * v
        g_["soldh"] += uh * v
    _rw = fc_sum["routine_wholesale"] or 1.0
    forecast_summary = {
        "assumptions": {"pace_factor": mg_a, "plant_sellable_fraction": mg_f, "hard_good_fraction": 1.0,
                        "horizon_weeks": DF.HORIZON_WEEKS, "d_horizon_weeks": int(sell_down.get("D", 10)),
                        "slower": DF.SLOWER, "marginal_pack_min_share": mg_share, "marginal_allocation": mg_on},
        "routine_units": fc_sum["routine_units"], "routine_wholesale": round(fc_sum["routine_wholesale"], 2),
        "sold35_wholesale": round(fc_sum["sold35_wholesale"], 2),
        "sellthrough35": round(fc_sum["sold35_wholesale"] / _rw, 4),
        "sold_grade_horizon_wholesale": round(fc_sum["soldh_wholesale"], 2),
        "sellthrough_grade_horizon": round(fc_sum["soldh_wholesale"] / _rw, 4),
        "sellthrough_grade_horizon_slower": round(fc_sum["slower_h_wholesale"] / _rw, 4),
        "lines_last_pack_under_share": fc_sum["lines_last_pack_low"],
        "by_dts_grade": {g: {"stores": len(x["stores"]), "wholesale": round(x["wholesale"], 2),
                             "sellthrough35": round(x["sold35"] / x["wholesale"], 4) if x["wholesale"] else None,
                             "sellthrough_grade_horizon": round(x["soldh"] / x["wholesale"], 4) if x["wholesale"] else None}
                         for g, x in sorted(fc_sum["by_grade"].items(), key=lambda kv: str(kv[0]))},
        "note": "Projected, not achieved: F(q,h) = min(q*f, max(0, h*a*r - s)) per store x SKU at its "
                "shipped quantity; measure actual receipt cohorts at 14/28/35 days (D: 70).",
    }
    log(f"Shipment forecast: routine ${forecast_summary['routine_wholesale']:,.0f} → 35-day "
        f"${forecast_summary['sold35_wholesale']:,.0f} ({forecast_summary['sellthrough35']:.1%}) · grade horizon "
        f"{forecast_summary['sellthrough_grade_horizon']:.1%} (slower {forecast_summary['sellthrough_grade_horizon_slower']:.1%})")

    # Tray limits / dual-use conversion / cart value floor (Jason's Week 49 order, 11/25)
    tl_summary = {"h2o_terrarium_max_trays": ht_max, "fullprice_share": ht_fp_share,
                  "fullprice_weeks": round(fp_weeks, 1), "accessory_max_boxes": acc_max,
                  "h2o_terrarium": {"lines_limited": 0, "trays_cut": 0, "lines_above_limit_fullprice": 0},
                  "accessories": {"lines_limited": 0, "trays_cut": 0, "stores": 0, "boxes": 0}}
    for l in lines.values():
        if not l.get("plannable") or l.get("tray_cap") is None:
            continue
        _g = tl_summary["h2o_terrarium" if l["family"] in H2O_TERR_FAMILIES else "accessories"]
        if l.get("trays_before_tray_cap"):
            _g["lines_limited"] += 1
            _g["trays_cut"] += l["trays_before_tray_cap"] - l["tray_cap"]
        if l["family"] in H2O_TERR_FAMILIES and l["tray_cap"] > ht_max:
            _g["lines_above_limit_fullprice"] += 1
    _acc = [l for l in _shipped if l.get("accessory")]
    tl_summary["accessories"].update(stores=len({l["store_id"] for l in _acc}),
                                     boxes=sum(l["alloc_trays"] for l in _acc))
    _lit = [l for l in _shipped if dual.get("to_sku") and l["sku_id"] == dual["to_sku"]]
    dual.update(littles_stores=len({l["store_id"] for l in _lit}),
                littles_units=sum(l["alloc_units"] for l in _lit),
                littles_stores_with_need=len({l["store_id"] for l in lines.values()
                                              if dual.get("to_sku") and l["sku_id"] == dual["to_sku"]
                                              and l.get("trays", 0) > 0}))
    _cpv = [c["wholesale_value"] for o in orders.values() for c in o["carts"]]
    cv_summary = {"mode": "hard" if cv_hard else "off", "per_cart_floor": low_value_wh,
                  "stores_held": econ.get("held_below_value_floor", 0),
                  "carts_below_floor": sum(1 for v in _cpv if v < low_value_wh),
                  "avg_wholesale_per_cart": round(sum(_cpv) / len(_cpv), 2) if _cpv else 0}
    log(f"Tray limits: H2O/Terrarium {tl_summary['h2o_terrarium']['lines_limited']:,} lines limited "
        f"({tl_summary['h2o_terrarium']['trays_cut']:,} trays) · accessories "
        f"{tl_summary['accessories']['lines_limited']:,} lines limited, {tl_summary['accessories']['boxes']:,} boxes "
        f"to {tl_summary['accessories']['stores']:,} stores · Littles to {dual['littles_stores']:,} stores "
        f"({dual['littles_units']:,}u) · value floor {cv_summary['mode']}: {cv_summary['stores_held']:,} stores held, "
        f"avg ${cv_summary['avg_wholesale_per_cart']:,.0f}/cart")
    # ROUTING FEASIBILITY + RELEASE SCORECARD (11/25: four reviewers — demand / sell-through, pack
    # team, routing, cart fill — each audited a different thing and orders kept failing someone's
    # audit). One pass/fail list over the FINAL order, computed here so every number is final.
    truck_cap = int(S.get("truck_cart_capacity", 45) or 45)
    max_spt = float(S.get("max_stops_per_truck", 20) or 20)
    _sc_st = S.get("scorecard_min_sellthrough35")
    sc_min_st = 0.68 if _sc_st is None else float(_sc_st)
    _sc_fr = S.get("scorecard_max_freight_pct")
    sc_max_fr = 0.10 if _sc_fr is None else float(_sc_fr)
    _sc_ab = S.get("scorecard_min_aab_share")
    sc_min_aab = 0.75 if _sc_ab is None else float(_sc_ab)
    _stop_info, _phys = {}, Counter()
    _under_fill, _under_fill_exc, _rw_grade = 0, 0, Counter()
    for st_id, o in orders.items():
        if not o["carts"]:
            continue
        by = {l["sku_id"]: l for l in o["lines"]}
        _rcs = [c for c in o["carts"] if _cart_has_routine(c, by)]
        _rids = {id(c) for c in _rcs}
        _stop_info[st_id] = (len(o["carts"]), not _rcs)
        _pb = any((sku_info[l["sku_id"]].get("family") or "") == "Prebuilt Cart" and l.get("alloc_trays", 0) > 0
                  for l in o["lines"])
        _any_full = any(c["tray_fill"] >= min_cart_fill - 1e-9 for c in _rcs)
        for c in o["carts"]:
            ph = SR.cart_physical(c["contents"], skip_fullness=True)
            _phys["shelves_too_wide"] += ph["shelves_too_wide"]
            _phys["carts_over_95in"] += ph["over_height"]
            _phys["carts_over_shelf_cap"] += ph["over_shelf_cap"]
            if id(c) in _rids:
                _phys["routine_shelves_not_full"] += _routine_shelves_not_full(c, by)
                if c["tray_fill"] < min_cart_fill - 1e-9:
                    if _pb and not _any_full:
                        _under_fill_exc += 1     # kept only so a pre-built cart can travel
                    else:
                        _under_fill += 1
        for l in o["lines"]:
            if l.get("alloc_units", 0) > 0 and _is_routine(l):
                _rw_grade[store_cadence(st_id)[0]] += l["alloc_units"] * float(
                    (sku_info.get(l["sku_id"]) or {}).get("wholesale_price_whl") or 0)
    routing = SR.routing_feasibility(_stop_info, {s_: (stores.get(s_) or {}).get("region") for s_ in _stop_info},
                                     truck_cap, max_spt)
    stopmin["single_cart_exceptions_directed"] = routing["single_cart_stops_directed_only"]
    _rw_tot = sum(_rw_grade.values())
    _aab = (sum(_rw_grade[g] for g in ("AA", "A", "B")) / _rw_tot) if _rw_tot else None
    _all_c = [c for o in orders.values() for c in o["carts"]]
    _twh = sum(c["wholesale_value"] for c in _all_c)
    _frt = (cart_cost * len(_all_c) / _twh) if _twh else None
    _sup_over = 0
    _alloc_by3 = defaultdict(int)
    for o in orders.values():
        for l in o["lines"]:
            _alloc_by3[l["sku_id"]] += int(l.get("alloc_units", 0) or 0)
    for sk, u in _alloc_by3.items():
        _tot_s = sum(pools.total[sk].values()) if sk in pools.total else 0
        if _tot_s and u > _tot_s:
            _sup_over += 1
    _dl = (second.get("demand_lost_audit") or {}).get("stores")
    _checks = [
        SR.check("projected_35day_routine_sellthrough", forecast_summary["sellthrough35"], sc_min_st, ">=",
                 "demand / sell-through"),
        SR.check("stores_with_demand_lost_and_free_stock", _dl, 0, "==", "demand / sell-through",
                 note=None if _dl is not None else "audit not run (second-chance and stop minimum off)"),
        SR.check("shelves_too_wide", _phys["shelves_too_wide"], 0, "==", "pack team"),
        SR.check("carts_over_95in_or_shelf_cap", _phys["carts_over_95in"] + _phys["carts_over_shelf_cap"], 0, "==",
                 "pack team", over_95in=_phys["carts_over_95in"], over_shelf_cap=_phys["carts_over_shelf_cap"]),
        SR.check("routine_shelves_not_full", _phys["routine_shelves_not_full"], 0, "==", "pack team"),
        SR.check("routine_carts_under_min_fill", _under_fill, 0, "==", "cart fill", min_fill=min_cart_fill,
                 prebuilt_companion_exceptions=_under_fill_exc),
        SR.check("stops_per_truck", routing["stops_per_truck"], max_spt, "<=", "routing",
                 trucks=routing["trucks"], stops=routing["stops"], carts=routing["carts"]),
        SR.check("single_cart_stops_not_directed", routing["single_cart_stops"]
                 - routing["single_cart_stops_directed_only"], 0, "==", "routing",
                 directed_only_exceptions=routing["single_cart_stops_directed_only"]),
        SR.check("freight_pct", round(_frt, 4) if _frt is not None else None, sc_max_fr, "<=", "cart economics",
                 cart_cost=cart_cost),
        SR.check("aa_a_b_share_of_routine_wholesale", round(_aab, 4) if _aab is not None else None, sc_min_aab,
                 ">=", "demand / sell-through",
                 by_grade={g: round(v, 2) for g, v in sorted(_rw_grade.items(), key=lambda kv: str(kv[0]))}),
        SR.check("skus_over_supply", _sup_over, 0, "==", "supply"),
    ]
    release_scorecard = SR.scorecard(_checks)
    log(f"Routing: {routing['carts']:,} carts / {routing['stops']:,} stops -> {routing['trucks']:,} trucks of "
        f"{truck_cap} · {routing['stops_per_truck']:.1f} stops per truck (max {max_spt:g}) · "
        f"{routing['single_cart_stops']:,} single-cart stops ({routing['single_cart_stops_directed_only']:,} "
        f"directed-only)")
    for c_ in _checks:
        if not c_["pass"]:
            log(f"RELEASE SCORECARD FAIL [{c_['audience']}] {c_['check']}: {c_['value']} (target {c_['op']} "
                f"{c_['target']})")
    log(f"Release scorecard: {'PASS' if release_scorecard['pass'] else 'FAIL'} "
        f"({len(_checks) - len(release_scorecard['failing'])}/{len(_checks)} checks)")
    if not release_scorecard["pass"]:
        decide("release_scorecard",
               "Release scorecard FAILED: " + "; ".join(
                   f"{c_['check']} = {c_['value']} (target {c_['op']} {c_['target']}, {c_['audience']})"
                   for c_ in _checks if not c_["pass"]),
               severity="warning", failing=release_scorecard["failing"])
    summary = {
        "engine": ENGINE_VERSION, "spec": "AI Replenishment master spec 2026-11-17",
        "shipment_forecast": forecast_summary,
        "demand_estimator": {**est_stats, "sources": dict(est_stats["sources"])},
        "tray_limits": tl_summary,
        "dual_use_conversion": {k: v for k, v in dual.items() if k != "lines"} | {"lines": len(dual["lines"])},
        "cart_value_floor": cv_summary,
        "partial_cart_trim": {**trim, "wholesale": round(trim["wholesale"], 2)},
        "fill_open_positions": fillpos,
        "final_full_cart_guard": guard,
        "every_shelf_full": fullsh,
        "second_chance_carts": second,
        "stop_minimum": stopmin,
        "routing_feasibility": routing,
        "release_scorecard": release_scorecard,
        "stockout_recovery": so_summary,
        "onhand_check": oc_summary,
        "demand_history": _dh_summary,
        "held_trays_reoffer": reoffer,
        "wholesale_target": tgt,
        "gate_13wk": flagged,
        "config_merge": merge,
        "availability_grades": grades,
        "production": production,
        "cart_completion": completion,
        "corrective_tests": ct,
        "sales_anchor": str(anchor), "arrival": str(arrival),
        "arrival_basis": ({"mode": "store delivery profile", **{k: lead_summary[k] for k in
                           ("network_days", "stores_with_history", "by_basis", "by_division", "deliveries")}}
                          if lead_summary else
                          {"mode": f"flat {ARRIVAL_OFFSET_DAYS} days after the ship-week Monday for every store"}),
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
            "directed_shipment_stores": len(directed_stores),
            "directed_shipment_units": int(sum(l["alloc_units"] for l in ship.values()
                                               if l.get("directed_trays"))),
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
            "stores_rule_overridden_by_directed_shipment": len(rule_overrides),
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
    line_group = {}                                             # (store, sku) -> its line group
    for st_id2, o2 in orders.items():
        for l in o2["lines"]:
            _grp = (("variety", l["variety_codes"]) if l.get("variety_codes")
                    else store_deny.get(st_id2, NO_DENY))
            line_group[(st_id2, l["sku_id"])] = _grp
            sku_committed[l["sku_id"]][_grp] += l["alloc_units"]
    for sku_id, by_group in sku_committed.items():
        _lines = [[lid, av, line_genus.get(lid, "")] for lid, av in aster_lines.get(sku_id, [])]
        for deny, got in store_rules.assign_sku_lines(by_group, _lines, line_plant,
                                                      _distribute_units_across_lines,
                                                      rank=line_rank).items():
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
    merge["final_configs"] = len(sig_count)
    log(f"Cart configs to build (with kits and handling tags): {len(sig_count):,} for "
        f"{sum(sig_count.values()):,} carts")
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
        top_fam_vel = max(fam_vel.get((store_id, l["family"]), 0.0) for l in lns)
        top_recency = max(rec.get((l["store_id"], l["sku_id"]), 0.0) for l in lns)
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
            # a pre-built cart keeps its own name on Stores and Carts ("Climb-itt Bundle"),
            # the way permanent carts do, instead of a numbered CC config
            _pb = {(sku_info[x[1]].get("description") or "").strip() for x in c["contents"]
                   if (sku_info[x[1]].get("family") or "") == "Prebuilt Cart"}
            if len(_pb) == 1 and all((sku_info[x[1]].get("family") or "") == "Prebuilt Cart"
                                     for x in c["contents"]):
                code = "PC " + next(iter(_pb))
            cart_rows.append((cid, oid, store_id, c["cart_number"], "cc", c["shelves_used"], CART_SHELVES,
                              c["total_units"], c["is_full"], False, None, CART_SHELVES,
                              f"WK{iso[0]}{iso[1]:02d}-C{cart_seq:04d}", code, True))
            if c.get("handling_tag"):
                cart_tag_rows.append((c["handling_tag"], cid))
            _store_deny = store_deny.get(store_id, NO_DENY)
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
                _deny = line_group.get((store_id, sk), _store_deny)    # deny set, or a named variety
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
                              round(l["sku_fit"], 4), round(fam_vel.get((store_id, l["family"]), 0.0), 4)))

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
               primary_trays=max(0, l["alloc_trays"] - l.get("completion_trays", 0) - l.get("gap_fill_trays", 0)),
               gap_fill_trays=min(l["alloc_trays"], l.get("completion_trays", 0) + l.get("gap_fill_trays", 0)),
               gap_fill_reason=("test item: sells at comparable stores, one tray to complete the cart"
                                if l.get("test_item") else
                                "cart completion: sells at this store, inside the weeks-of-supply limit"
                                if l.get("completion_trays") else
                                "same-family substitute for a short product" if l.get("gap_fill_trays") else None),
               weeks_supply_after=l.get("completion_wos"),
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
    cur.execute("""SELECT store_id, sku_id, delivery_date,
                          COALESCE(SUM(units) FILTER (WHERE confidence <> 'LOW'), 0) AS med,
                          COALESCE(SUM(units) FILTER (WHERE confidence = 'LOW'), 0) AS low,
                          string_agg(DISTINCT method, '+') AS m
                   FROM in_transit_lines WHERE delivery_date > %s
                   GROUP BY store_id, sku_id, delivery_date""", (anchor,))
    out = {}
    for r in cur.fetchall():
        e = out.setdefault((r[0], r[1]), [0.0, 0.0, set(), []])
        e[0] += float(r[3])
        e[1] += float(r[4])
        e[2].update((r[5] or "").split("+"))
        e[3].append((r[2], float(r[3]), float(r[4])))     # (delivery date, medium u, low u)
    return {k: (v[0], v[1], "+".join(sorted(x for x in v[2] if x)), v[3]) for k, v in out.items()}, weeks


# Swappable for previews (feed loader output in memory without writing the table).
IN_TRANSIT_SOURCE = _in_transit_lines


if __name__ == "__main__":
    import sys
    b = sys.argv[1]
    wk = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else None
    res = run(b, wk, preview="--write" not in sys.argv)
    if isinstance(res, dict):
        print(json.dumps(res["summary"], indent=2, default=str))
