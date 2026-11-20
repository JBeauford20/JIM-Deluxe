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

from db_conn import get_conn
# Proven pieces carried over from the legacy engine (physical/packing truths, not policy)
from recommendation_engine import (CART_SHELVES, CART_CEILING_IN, MIN_SHELF_CLEAR_IN,
                                   PROFILE_HEIGHT_RANK, determine_kit_type,
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
    cover_wk = float(S["coverage_weeks"])
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

    cur.execute("""SELECT al.id, al.sku_id, al.units_available, al.code_level, al.hard_good_type
                   FROM availability_lines al WHERE al.batch_id = %s ORDER BY al.sku_id, al.id""",
                (batch_id,))
    avail_rows = cur.fetchall()
    if not avail_rows:
        raise ValueError("Incomplete Data: availability batch has no lines")
    supply = defaultdict(int)
    avail_meta, aster_lines = {}, defaultdict(list)
    for r in avail_rows:
        supply[r["sku_id"]] += int(r["units_available"] or 0)
        avail_meta.setdefault(r["sku_id"], {"code_level": r["code_level"],
                                            "hard_good_type": r["hard_good_type"]})
        aster_lines[r["sku_id"]].append([str(r["id"]), int(r["units_available"] or 0)])
    for s in aster_lines:
        aster_lines[s].sort(key=lambda x: (-x[1], x[0]))
    total_supply = sum(supply.values())

    # SKU master: family, prices, packing (units per tray, shelf capacity, height)
    cur.execute("""
        SELECT k.sku_id, k.description, k.wholesale_price_whl, k.is_holiday,
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

    plannable = set()
    for sku_id in supply:
        info, pk = sku_info.get(sku_id), pack.get(sku_id)
        if not info:
            decide("validation", "Availability SKU not in SKU master — excluded", sku_id=sku_id, severity="block")
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
        if realized:
            return realized, "realized"
        info = sku_info.get(sku_id) or {}
        if info.get("retail_price"):
            return float(info["retail_price"]), "pricebook"
        # No retail price: never guessed (wholesale x2 removed). The line still plans on units;
        # it is excluded from every $ total and counted in summary.incomplete_price_units.
        return None, "missing"

    # Stores
    cur.execute("""SELECT store_id, store_name, active, store_status, dynamic_velocity_tier,
                          region, merchant, state
                   FROM stores ORDER BY store_id""")
    stores = {r["store_id"]: dict(r) for r in cur.fetchall()}

    # ══ 2. WHAT CAN THE STORE SELL? (§7) ═════════════════════════════════
    d = lambda n: anchor - timedelta(days=n)
    cur.execute("""
        SELECT store_id, sku_id,
               SUM(units_sold) FILTER (WHERE sale_date > %s)  AS w2,
               SUM(units_sold) FILTER (WHERE sale_date > %s)  AS w4,
               SUM(units_sold) FILTER (WHERE sale_date > %s)  AS w8,
               SUM(units_sold) FILTER (WHERE sale_date > %s)  AS w10,
               SUM(units_sold) FILTER (WHERE sale_date > %s)  AS w13,
               SUM(dollars)    FILTER (WHERE sale_date > %s)  AS d13,
               SUM(units_sold) FILTER (WHERE sale_date > %s AND dollars IS NOT NULL) AS w13d
        FROM sales
        WHERE sale_date > %s AND sale_date <= %s
        GROUP BY store_id, sku_id
    """, (d(14), d(28), d(fresh_days), d(70), d(91), d(91), d(91), d(max(91, fresh_days)), anchor))
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
        SELECT store_id, sku_id,
               MAX(delivery_date) AS last_del,
               SUM(units_delivered) FILTER (WHERE delivery_date > %s AND delivery_date <= %s) AS cohort,
               SUM(units_delivered) FILTER (WHERE delivery_date > %s) AS recent8,
               SUM(units_delivered) AS d26, COUNT(DISTINCT delivery_date) AS n_del
        FROM deliveries
        WHERE delivery_date > %s
        GROUP BY store_id, sku_id
    """, (d(91 + lag_days), d(lag_days), d(fresh_days), d(182)))
    dels = {(r["store_id"], r["sku_id"]): dict(r) for r in cur.fetchall()}

    # On-hand: DTS snapshot (MEDIUM) when fresh, else deliveries−sales estimate (LOW)
    cur.execute("""
        SELECT DISTINCT ON (store_id, sku_id) store_id, sku_id, onhand_qty, period_end
        FROM dts_weekly_snapshot
        WHERE period_end > %s
        ORDER BY store_id, sku_id, period_end DESC
    """, (d(14),))
    snap = {(r["store_id"], r["sku_id"]): r for r in cur.fetchall()}

    # Inbound (§8: in-transit is supply, not a block): exported prior-run quantities for
    # stores the manifest shows arriving after the sales anchor.
    cur.execute("SELECT DISTINCT store_id FROM delivery_manifest WHERE delivery_date > %s", (anchor,))
    in_transit_stores = {r["store_id"] for r in cur.fetchall()}
    cur.execute("""
        SELECT o.store_id, ol.sku_id,
               SUM(COALESCE(ol.approved_qty, ol.draft_qty, ol.recommended_qty)) AS qty
        FROM recommendation_runs r
        JOIN orders o ON o.run_id = r.id
        JOIN order_lines ol ON ol.order_id = o.id
        WHERE r.status IN ('exported','finalized','approved')
          AND r.shipping_week > %s AND r.shipping_week < %s
        GROUP BY o.store_id, ol.sku_id
    """, (anchor - timedelta(days=7), shipping_week))
    inbound_known = {(r["store_id"], r["sku_id"]): int(r["qty"] or 0) for r in cur.fetchall()}

    # ══ 3+4. PROJECTED INVENTORY AT ARRIVAL + UNCONSTRAINED NEED (§8–10) ════
    lines = {}               # (store, sku) -> evaluation record
    # Every store x SKU with velocity is evaluated (the §2 objective covers the whole HD
    # program); only plannable SKUs (in this availability + packable + mapped) get trays.
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
        if fc < min_rate:
            continue                        # no velocity → not in scope (Josh: only ship velocity)

        # Sellable on-hand + confidence (§4.6, §5). DTS reported on-hand is NOT sellable
        # on-hand: dumps are booked in bulk cleanups (2026-11-17 audit: 0 dumps in 6 of 7
        # weeks, then 357K in one week; network on-hand = 16 weeks of supply). Sellable is
        # bounded by freshness (§4.5): only units delivered within the freshness window,
        # less units sold since, can still be on the floor. Take the smaller of the two.
        fresh_est = max(0.0, float(dv.get("recent8") or 0) - sv["w8"])
        sn = snap.get((store_id, sku_id))
        if sn is not None and sn["onhand_qty"] is not None and float(sn["onhand_qty"]) < 0:
            oh, conf, oh_src = 0.0, "UNUSABLE", f"DTS snapshot {sn['period_end']} negative — implausible"
        elif sn is not None and sn["onhand_qty"] is not None:
            rep = float(sn["onhand_qty"])
            oh = min(rep, fresh_est)
            conf, oh_src = "LOW", (f"min(DTS {rep:.0f}u, fresh {fresh_est:.0f}u) — "
                                   f"DTS dumps batch-recorded, freshness-capped")
        else:
            oh, conf, oh_src = fresh_est, "LOW", f"estimate: {fresh_wk}-wk deliveries − sales"
        # the freshness cap is already the conservative bound — no second haircut on top
        oh_adj = 0.0 if conf == "UNUSABLE" else max(0.0, oh) * (
            1.0 if sn is not None else float(conf_factor[conf]))

        inbound, inbound_src = inbound_known.get((store_id, sku_id), 0), "prior exported run"
        if not inbound and store_id in in_transit_stores and dv.get("n_del"):
            inbound = round(float(dv.get("d26") or 0) / max(1, int(dv["n_del"])))
            inbound_src = "in transit (manifest) — qty estimated from avg delivery"

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
            # unreliable on-hand cannot create Critical risk (§9)
            risk = "High" if conf == "LOW" else "Critical"
        elif cover_days < 7:
            risk = "High"
        elif cover_days < 14:
            risk = "Moderate"
        else:
            risk = "Low"

        need = max(0.0, fc * cover_wk - projected)                       # §10
        fam = sku_info[sku_id]["ipr_family"] or "Unmapped"
        pk = pack.get(sku_id) or {}
        upt = int(pk.get("units_per_tray") or 1)
        cap_note = None
        if not is_plannable:
            trays = 0                                                     # objective-only line
        elif fam in H2O_TERR_FAMILIES:                                   # §17 tray control
            win = float(S["h2o_terrarium_window_weeks"])
            cap = max(0.0, fc * win - oh_adj - inbound)
            if need > cap:
                need, cap_note = cap, f"capped at {int(win)}-wk demand ({cap:.0f}u)"
            trays = int(need // upt)                                      # never exceed: round down
        else:
            trays = int(math.floor(need / upt + 0.5))                     # nearest valid tray
        if conf == "UNUSABLE" and trays:
            trays = min(trays, max(1, int((fc * 2) // upt)))              # §5: cap / review
            cap_note = (cap_note or "") + " inventory unreliable — capped at 2 wks"
        realized = (sv["d13"] / sv["w13d"]) if sv["w13d"] else None
        unit_price, price_src = price(sku_id, realized)
        lines[(store_id, sku_id)] = dict(
            store_id=store_id, sku_id=sku_id, family=fam, fc=fc, method=method,
            r2=r2, r4=r4, r10=r10, r13=r13, onhand=oh, conf=conf, oh_src=oh_src, oh_adj=oh_adj,
            inbound=inbound, inbound_src=inbound_src, before_arrival=before_arrival,
            projected=projected, cover_days=cover_days, risk=risk, need=need, upt=upt,
            trays=trays, cap_note=cap_note, price=unit_price, price_src=price_src,
            last_del=last_del, sales13=sv["w13"], cohort=float(dv.get("cohort") or 0),
            plannable=is_plannable)
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
    ship, held, held_kind = {}, {}, {}
    blocked_units = stopped_units = reduced_units = 0.0
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

    for key, why in held.items():
        l = lines[key]
        decide(held_kind[key], why, store_id=l["store_id"], sku_id=l["sku_id"],
               family=l["family"], grade=l.get("grade"), conversion=l.get("conversion"),
               proposed_trays=l["trays"], proposed_units=l["trays"] * l["upt"])
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
                        + sf_w["projected_need"] * min(1.0, l["need"] / max(1e-9, l["fc"] * cover_wk))
                        + sf_w["seasonal"] * 0.0 + sf_w["comparable_fit"] * GRADE_SCORE[l["grade"]])

    by_sku = defaultdict(list)
    for k, l in ship.items():
        by_sku[l["sku_id"]].append(l)
    unfilled = defaultdict(float)        # sku -> units of need not covered by supply
    sku_left = {}                        # sku -> supply left after priority allocation
    alloc_units_total = 0
    for sku_id, lst in by_sku.items():
        remaining = supply.get(sku_id, 0)
        want = sum(l["trays"] * l["upt"] for l in lst)
        lst.sort(key=lambda x: (-x["priority"], x["store_id"]))
        for l in lst:
            can = min(l["trays"], remaining // l["upt"])
            l["alloc_trays"] = int(can)
            l["alloc_units"] = int(can) * l["upt"]
            remaining -= l["alloc_units"]
            if can < l["trays"]:
                short = (l["trays"] - can) * l["upt"]
                unfilled[sku_id] += short
                l["short_units"] = short
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
        cands = sorted((l for l in fl if not l.get("short_units") and not l.get("reactivation")
                        and l["conf"] != "UNUSABLE" and l["need"] > 0 and sku_left.get(l["sku_id"], 0) > 0),
                       key=lambda x: (-x["fc"], x["sku_id"]))
        for c in cands:
            if fam_need - fam_alloc < c["upt"]:
                continue
            add = min(c["trays"] - c.get("gap_fill_trays", 0),           # selling-window guard
                      sku_left[c["sku_id"]] // c["upt"],
                      (fam_need - fam_alloc) // c["upt"])
            if add <= 0:
                continue
            u = add * c["upt"]
            c["alloc_trays"] += add
            c["alloc_units"] += u
            c["gap_fill_trays"] = c.get("gap_fill_trays", 0) + add
            sku_left[c["sku_id"]] -= u
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

    # ══ 7. TRAYS -> CART-LOADING PLAN (§17–19): physical fit only ═══════════
    # §19.1 cart economics — editable assumptions, not rules: delivered cart cost is roughly
    # $180–$300 (default $240); normal wholesale value per cart is roughly $1,700–$3,000.
    cart_cost = float(S.get("cart_delivered_cost", 240) or 240)
    low_value_wh = float(S.get("cart_wholesale_low", 1700) or 1700)
    consolidate = str(S.get("cart_consolidate", True)).lower() not in ("false", "0", "no")
    orders = {}
    store_kit = defaultdict(lambda: defaultdict(int))
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
                    kit = determine_kit_type(l["sku_id"], units, store_kit[store_id], avail_meta)
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
            out.append({"cart_number": ci, "cart_type_id": "cc", "shelves_used": len(ordered),
                        "total_units": sum(x[3] for x in contents), "contents": contents,
                        "is_full": len(ordered) >= CART_SHELVES, "is_flagged": False,
                        "wholesale_value": round(wh_value, 2), "wholesale_incomplete": wh_missing,
                        "delivered_cost": cart_cost,
                        "freight_pct": None if freight_pct is None else round(freight_pct, 4),
                        "low_value": low_value})
        grade_framework = {"AA": 3, "A": 3, "B": 2, "C+": 2, "C": 1, "C-": 1, "D": 1}
        top_grade = min((l["grade"] for l in olines), key=GRADE_ORDER.index)
        if len(out) > grade_framework[top_grade]:
            decide("review", f"{len(out)} carts exceeds the §18 initial framework ({grade_framework[top_grade]} "
                             f"for best family grade {top_grade}) — demand-driven, shipped (cart caps off)",
                   store_id=store_id, severity="info", carts=len(out))
        orders[store_id] = {"lines": olines, "carts": out}

    # §19.1 low-value order decision (economic, not a size floor; §23 deferral), judged on each
    # store's WHOLE order: W = order wholesale, n = carts, C = n x cart_delivered_cost. RELEASE
    # the order when W/n >= cart_wholesale_low, or one more week of waiting would lose at least
    # C in retail sales — lost = Σ min(fc, max(0, fc − projected stock at arrival)) × price over
    # its lines — or any line is Critical risk. Otherwise DEFER the whole order (no partial-store
    # deferral): need is recalculated next week, never padded. No cross-store consolidation.
    econ = {"released_low_value": 0, "stores_released_low_value": 0, "deferred_carts": 0,
            "deferred_units": 0, "deferred_wholesale": 0.0, "deferred_retail_one_week_at_risk": 0.0,
            "deferred_single_cart": 0, "deferred_multi_cart": 0}
    deferred_stores = []
    for store_id in sorted(orders):
        o = orders[store_id]
        n = len(o["carts"])
        W = sum(c["wholesale_value"] for c in o["carts"])
        C = n * cart_cost
        if n and W / n >= low_value_wh:
            continue
        lost = sum(min(l["fc"], max(0.0, l["fc"] - l["projected"])) * usd(l) for l in o["lines"])
        critical = sorted(l["sku_id"] for l in o["lines"] if l["risk"] == "Critical")
        fp = (C / W) if W else None
        nums = (f"order wholesale ${W:,.0f} on {n} cart(s) (${W / n:,.0f}/cart < ${low_value_wh:,.0f}); "
                f"delivered cost ${C:,.0f} (freight {'n/a' if fp is None else f'{fp:.0%}'}); "
                f"one-week lost sales if it waits ${lost:,.0f}")
        data = dict(order_wholesale=round(W, 2), carts=n, delivered_cost=C,
                    freight_pct=None if fp is None else round(fp, 4), lost_sales_one_week=round(lost, 2),
                    margin_proxy_wholesale=round(W, 2), critical_skus=critical,
                    units=sum(l["alloc_units"] for l in o["lines"]),
                    wholesale_missing_skus=sorted({x for c in o["carts"] for x in c["wholesale_incomplete"]}))
        if lost >= C or critical:
            why = ("Critical-risk line(s) " + ", ".join(critical)) if critical else                   f"lost sales ${lost:,.0f} >= delivered cost ${C:,.0f}"
            decide("low_value_cart", f"Order RELEASED: {nums} — {why} (§19.1)", store_id=store_id,
                   severity="warning", action="release", **data)
            for c in o["carts"]:
                c["econ_action"] = "release"
            econ["released_low_value"] += n
            econ["stores_released_low_value"] += 1
            continue
        decide("deferred_economics", f"Order DEFERRED: {nums} < delivered cost — waiting a week costs "
                                     f"less than delivering now; need recalculated next week, never "
                                     f"padded (§19.1, §23)", store_id=store_id, action="defer", **data)
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
               store_id=store_id, verdict=verdict, units=units)

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
    lost_unavail = sum(l.get("short_units", 0) * usd(l) for l in ship.values()) / cover_wk
    lost_held = sum(lines[k]["trays"] * lines[k]["upt"] * usd(lines[k]) for k in held) / cover_wk
    edi_risk = sum(l["fc"] * usd(l) for l in lines.values() if l["store_id"] in edi_gap)
    all_carts = [c for o in orders.values() for c in o["carts"]]
    tot_wh = sum(c["wholesale_value"] for c in all_carts)
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
            "edi_uncertainty_at_risk": round(edi_risk),
            "price_basis_units": dict(price_srcs),
            "method_note": ("Arrival-week retail across every store x SKU with velocity: "
                            "min(weekly forecast, projected stock) x price. Prices are realized "
                            "(DTS dollars) where loaded, else pricebook retail. SKUs with no "
                            "price are never guessed: they ship on units and are excluded from $."),
        },
        "incomplete_price_units": int(price_srcs.get("missing", 0)),
        "gap_fill": {"fills": gap_fills, "trays": gap_fill_trays, "units": gap_fill_units,
                     "short_store_families": len(gaps)},
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
    line_remaining, sku_lines_order = {}, {}
    sku_committed = defaultdict(int)
    for o2 in orders.values():
        for l in o2["lines"]:
            sku_committed[l["sku_id"]] += l["alloc_units"]
    for sku_id, tot in sku_committed.items():
        alloc = _distribute_units_across_lines(tot, aster_lines.get(sku_id, []))
        sku_lines_order[sku_id] = [lid for lid, _q in alloc]
        for lid, q in alloc:
            line_remaining[lid] = line_remaining.get(lid, 0) + q

    # exact-signature config codes (no cap, no consolidation — every code is buildable blind)
    sig_count = defaultdict(int)
    cart_sig = {}
    for store_id, o2 in orders.items():
        for c in o2["carts"]:
            sig = tuple(sorted((p, sk, tr, kt) for (p, sk, tr, u, kt, _pid) in c["contents"]))
            cart_sig[(store_id, c["cart_number"])] = sig
            sig_count[sig] += 1
    sig_code = {s: f"CC{i + 1}" for i, s in enumerate(sorted(sig_count, key=lambda s: (-sig_count[s], s)))}

    cart_seq = 0
    template = {}
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
                for lid in sku_lines_order.get(sk, []):
                    if need_u <= 0:
                        break
                    av = line_remaining.get(lid, 0)
                    if av <= 0:
                        continue
                    take = min(need_u, av)
                    st = trays_left if take == need_u else min(trays_left, max(1, round(tr * take / u)))
                    segs.append((lid, take, st))
                    line_remaining[lid] = av - take
                    need_u -= take
                    trays_left -= st
                if need_u > 0 or not segs:
                    segs.append((None, need_u if segs else u, max(0, trays_left)))
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
               price=None if l["price"] is None else round(l["price"], 2), price_src=l["price_src"])
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


if __name__ == "__main__":
    import sys
    b = sys.argv[1]
    wk = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else None
    res = run(b, wk, preview="--write" not in sys.argv)
    if isinstance(res, dict):
        print(json.dumps(res["summary"], indent=2, default=str))
