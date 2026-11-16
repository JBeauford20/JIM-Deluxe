"""
Config Editor API — cart config template reads, variety edits, preview, lock.
All writes are single transactions that keep cart_shelves, order_lines,
allocation_reservations, and order_carts in sync.
"""
from typing import List, Optional
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from db import get_db, cursor
from routers.auth import get_current_user, require_role
from services import ledger
from routers.concurrency import log_change

router = APIRouter()

# ── Input models ──────────────────────────────────────────────

class VarietyItem(BaseModel):
    sku_id: int
    availability_line_id: Optional[str] = None
    tray_count: int = 1


class PreviewRequest(BaseModel):
    shelf_position: int
    new_varieties: List[VarietyItem]


class UpdateVarietiesRequest(BaseModel):
    varieties: List[VarietyItem]


# ── Helpers ───────────────────────────────────────────────────

def _sku_line_ids(cur, sku_id: int, batch_id=None):
    """Availability line ids for an HD SKU (optionally within one batch). Used when an edit
    didn't pin a specific Aster line — we consider every line that SKU can draw from."""
    if batch_id:
        cur.execute(
            "SELECT id FROM availability_lines WHERE sku_id = %s AND batch_id = %s",
            (sku_id, batch_id),
        )
    else:
        cur.execute("SELECT id FROM availability_lines WHERE sku_id = %s", (sku_id,))
    return [str(r["id"]) for r in cur.fetchall()]


def _aggregate_templates(rows: list) -> list:
    """
    Fold flat SQL rows (one per variety) into a list of template dicts,
    each containing a shelves list, each shelf containing a varieties list.
    Preserves the ORDER BY config_code, shelf_position DESC, sort_order
    that the query guarantees.
    """
    templates: dict = {}
    template_order: list = []

    for row in rows:
        tid = row["id"]
        if tid not in templates:
            templates[tid] = {
                "id":          tid,
                "config_code": row["config_code"],
                "cart_count":  row["cart_count"],
                "store_count": row["store_count"],
                "total_units": row["total_units"],
                "is_locked":   row["is_locked"],
                "shelves":     {},
                "_shelf_order": [],
            }
            template_order.append(tid)

        t = templates[tid]

        # shelf may be NULL if template has no shelves yet
        shelf_id = row.get("shelf_id")
        if shelf_id is None:
            continue

        if shelf_id not in t["shelves"]:
            t["shelves"][shelf_id] = {
                "id":             shelf_id,
                "shelf_position": row["shelf_position"],
                "profile_id":     row["profile_id"],
                "units_per_shelf": row["units_per_shelf"],
                "tray_count":     row["tray_count"],
                "varieties":      [],
            }
            t["_shelf_order"].append(shelf_id)

        # variety may be NULL if shelf has no varieties yet
        variety_id = row.get("variety_id")
        if variety_id is None:
            continue

        t["shelves"][shelf_id]["varieties"].append({
            "id":                   variety_id,
            "sku_id":               row["v_sku_id"],
            "description":          row["description"],
            "family":               row["family"],
            "legacy_product_group": row["legacy_product_group"],
            "tray_count":           row["v_trays"],
            "unit_count":           row["v_units"],
            "sort_order":           row["sort_order"],
            "availability_line_id": row["availability_line_id"],
        })

    result = []
    for tid in template_order:
        t = templates[tid]
        shelves = [t["shelves"][sid] for sid in t["_shelf_order"]]
        result.append({
            "id":          t["id"],
            "config_code": t["config_code"],
            "cart_count":  t["cart_count"],
            "store_count": t["store_count"],
            "total_units": t["total_units"],
            "is_locked":   t["is_locked"],
            "shelves":     shelves,
        })
    return result


_TEMPLATE_QUERY = """
    SELECT t.id, t.config_code, t.cart_count, t.store_count,
           t.total_units, t.is_locked,
           s.id          AS shelf_id,
           s.shelf_position,
           s.profile_id,
           s.units_per_shelf,
           s.tray_count,
           v.id          AS variety_id,
           v.sku_id      AS v_sku_id,
           v.tray_count  AS v_trays,
           v.unit_count  AS v_units,
           v.sort_order,
           v.availability_line_id,
           sk.description,
           sk.family,
           sk.legacy_product_group
    FROM cart_config_templates t
    LEFT JOIN cart_config_template_shelves s  ON s.template_id = t.id
    LEFT JOIN cart_config_template_varieties v ON v.shelf_id   = s.id
    LEFT JOIN skus sk ON sk.sku_id = v.sku_id
"""


# ── Routes ────────────────────────────────────────────────────

@router.get("/run/{run_id}")
def get_run_templates(run_id: str, user=Depends(get_current_user)):
    """All CC templates for a run with full shelf+variety detail."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute(
            _TEMPLATE_QUERY + "WHERE t.run_id = %s ORDER BY t.config_code, s.shelf_position DESC, v.sort_order",
            (run_id,),
        )
        rows = cur.fetchall()
    return _aggregate_templates(rows)


@router.get("/templates/{template_id}")
def get_template(template_id: str, user=Depends(get_current_user)):
    """Single template with full shelf+variety detail, plus batch_id from the run."""
    with get_db() as conn:
        cur = cursor(conn)

        # Get batch_id via the run
        cur.execute("""
            SELECT rr.batch_id
            FROM cart_config_templates t
            JOIN recommendation_runs rr ON rr.id = t.run_id
            WHERE t.id = %s
        """, (template_id,))
        meta = cur.fetchone()
        if not meta:
            raise HTTPException(404, "Template not found")
        batch_id = meta["batch_id"]

        cur.execute(
            _TEMPLATE_QUERY + "WHERE t.id = %s ORDER BY s.shelf_position DESC, v.sort_order",
            (template_id,),
        )
        rows = cur.fetchall()

    templates = _aggregate_templates(rows)
    if not templates:
        raise HTTPException(404, "Template not found")

    result = templates[0]
    result["batch_id"] = batch_id
    return result


@router.get("/templates/{template_id}/inventory")
def get_template_inventory(template_id: str, user=Depends(get_current_user)):
    """Remaining inventory for all SKUs in the batch linked to this template's run."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT ar.availability_line_id,
                   ar.sku_id,
                   ar.units_available,
                   ar.remaining,
                   ar.draft_reserved,
                   ar.approved_reserved,
                   al.code_level,
                   al.hard_good_type,
                   sk.description,
                   sk.family,
                   sk.legacy_product_group
            FROM cart_config_templates t
            JOIN recommendation_runs rr ON rr.id = t.run_id
            JOIN availability_remaining ar ON ar.batch_id = rr.batch_id
            JOIN availability_lines al ON al.id = ar.availability_line_id
            JOIN skus sk ON sk.sku_id = ar.sku_id
            WHERE t.id = %s
            ORDER BY sk.family, ar.units_available DESC
        """, (template_id,))
        return cur.fetchall()


@router.get("/templates/{template_id}/sku/{sku_id}/alternatives")
def get_sku_alternatives(template_id: str, sku_id: int, user=Depends(get_current_user)):
    """
    One-to-many Aster->SKU swap options. A plant (product_code / aster input) can be
    potted into different hardgoods and become different HD SKUs. This returns the
    ALTERNATIVE HD SKUs reachable from the plants that make up `sku_id`, joined to this
    template's batch inventory so only SKUs we actually have stock for are offered
    (with the availability_line_id the writer needs to swap). Flags tier downgrades
    (Collector/Specialty/Boutique/Rare must never drop to Premium).
    """
    DOWNGRADE_RANK = {"premium": 0, "collectors": 1, "collector": 1, "specialty": 2,
                      "boutique": 3, "rare collectors": 4, "rare": 4}
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT rr.batch_id FROM cart_config_templates t
            JOIN recommendation_runs rr ON rr.id = t.run_id WHERE t.id = %s
        """, (template_id,))
        meta = cur.fetchone()
        if not meta:
            raise HTTPException(404, "Template not found")
        batch_id = meta["batch_id"]

        # current SKU's tier (for downgrade check)
        cur.execute("SELECT legacy_product_group FROM skus WHERE sku_id = %s", (sku_id,))
        crow = cur.fetchone()
        cur_tier = (crow["legacy_product_group"] or "").strip().lower() if crow else ""

        cur.execute("""
            WITH plants AS (
                SELECT DISTINCT product_code
                FROM aster_sku_options
                WHERE hd_sku_id = %s AND active
            ),
            alts AS (
                SELECT o.hd_sku_id,
                       count(*) AS shared_plants
                FROM aster_sku_options o
                JOIN plants p ON p.product_code = o.product_code
                WHERE o.hd_sku_id <> %s AND o.active
                GROUP BY o.hd_sku_id
            )
            SELECT a.hd_sku_id,
                   a.shared_plants,
                   sk.description,
                   sk.family,
                   sk.legacy_product_group,
                   sk.is_holiday,
                   ar.availability_line_id,
                   ar.remaining
            FROM alts a
            JOIN skus sk ON sk.sku_id = a.hd_sku_id
            LEFT JOIN availability_remaining ar
                   ON ar.batch_id = %s AND ar.sku_id = a.hd_sku_id
            ORDER BY (ar.remaining IS NULL), ar.remaining DESC NULLS LAST, a.shared_plants DESC
        """, (sku_id, sku_id, batch_id))
        rows = cur.fetchall()

    out = []
    for r in rows:
        alt_tier = (r["legacy_product_group"] or "").strip().lower()
        is_downgrade = (cur_tier in DOWNGRADE_RANK and alt_tier in DOWNGRADE_RANK
                        and DOWNGRADE_RANK[alt_tier] < DOWNGRADE_RANK[cur_tier])
        out.append({
            "sku_id":               r["hd_sku_id"],
            "description":          r["description"],
            "family":               r["family"],
            "legacy_product_group": r["legacy_product_group"],
            "is_holiday":           r["is_holiday"],
            "shared_plants":        r["shared_plants"],
            "availability_line_id": r["availability_line_id"],
            "remaining":            r["remaining"] or 0,
            "in_stock":             r["availability_line_id"] is not None and (r["remaining"] or 0) > 0,
            "tier_downgrade":       is_downgrade,
        })
    return out


@router.post("/templates/{template_id}/preview")
def preview_variety_change(
    template_id: str,
    req: PreviewRequest,
    user=Depends(get_current_user),
):
    """
    Preview the impact of a proposed variety change WITHOUT committing.
    Returns unit delta and per-variety availability check.
    """
    with get_db() as conn:
        cur = cursor(conn)

        # Fetch template (+ batch via run, so ledger remaining is scoped to this week)
        cur.execute("""
            SELECT t.cart_count, rr.batch_id
            FROM cart_config_templates t
            JOIN recommendation_runs rr ON rr.id = t.run_id
            WHERE t.id = %s
        """, (template_id,))
        tmpl = cur.fetchone()
        if not tmpl:
            raise HTTPException(404, "Template not found")
        cart_count = tmpl["cart_count"]
        batch_id = tmpl["batch_id"]

        # Fetch current shelf
        cur.execute("""
            SELECT id, profile_id, units_per_shelf
            FROM cart_config_template_shelves
            WHERE template_id = %s AND shelf_position = %s
        """, (template_id, req.shelf_position))
        shelf = cur.fetchone()
        if not shelf:
            raise HTTPException(404, f"Shelf position {req.shelf_position} not found on template")

        current_units_committed = (shelf["units_per_shelf"] or 0) * cart_count

        # Get units_per_tray for this profile
        cur.execute("""
            SELECT units_per_tray
            FROM packing_profiles
            WHERE id = %s
            LIMIT 1
        """, (shelf["profile_id"],))
        sc = cur.fetchone()
        if not sc:
            raise HTTPException(400, f"No approved shelf config for profile {shelf['profile_id']}")
        units_per_tray = sc["units_per_tray"]

        # Calculate new shelf units
        new_units_per_shelf = sum(v.tray_count * units_per_tray for v in req.new_varieties)
        new_units_committed = new_units_per_shelf * cart_count
        net_change = new_units_committed - current_units_committed

        # Units this edit would free (credited back below) — read from the LEDGER (the single
        # source of truth), mirroring the commit path. The old reservations-based credit missed
        # engine-created carts entirely (the engine writes the ledger, not reservations).
        cur.execute("""
            SELECT cs.sku_id, COALESCE(SUM(ola.qty), 0) AS freed
            FROM order_line_allocations ola
            JOIN cart_shelves cs ON cs.id = ola.cart_shelf_id
            JOIN order_carts  oc ON oc.id = cs.cart_id
            WHERE oc.template_id = %s
              AND cs.shelf_position = %s
            GROUP BY cs.sku_id
        """, (template_id, req.shelf_position))
        freed_by_sku = {r["sku_id"]: int(r["freed"] or 0) for r in cur.fetchall()}

        # Check availability per variety (ledger remaining + what this edit frees back)
        per_variety_check = []
        all_ok = True
        for v in req.new_varieties:
            required = v.tray_count * units_per_tray * cart_count

            if v.availability_line_id:
                available_base = ledger.remaining_for_line(cur, v.availability_line_id)
            else:
                rem = ledger.remaining_for_lines(cur, _sku_line_ids(cur, v.sku_id, batch_id))
                available_base = min(rem.values()) if rem else 0
            available = available_base + freed_by_sku.get(v.sku_id, 0)
            ok = available >= required
            if not ok:
                all_ok = False
            per_variety_check.append({
                "sku_id":    v.sku_id,
                "required":  required,
                "available": available,
                "ok":        ok,
            })

        return {
            "current_units_committed": current_units_committed,
            "new_units_committed":     new_units_committed,
            "net_change":              net_change,
            "cart_count":              cart_count,
            "per_variety_check":       per_variety_check,
            "all_ok":                  all_ok,
        }


@router.put("/templates/{template_id}/shelves/{shelf_position}/varieties")
def update_shelf_varieties(
    template_id: str,
    shelf_position: int,
    req: UpdateVarietiesRequest,
    user=Depends(require_role("order_writer", "manager", "admin")),
):
    """
    Commit a new variety mix to a template shelf.
    One transaction: updates template shelves, physical cart_shelves,
    allocation_reservations, and order_lines.
    """
    with get_db() as conn:
        cur = cursor(conn)

        # 1. Fetch and lock template — check is_locked
        cur.execute("""
            SELECT id, cart_count, run_id, is_locked
            FROM cart_config_templates
            WHERE id = %s FOR UPDATE
        """, (template_id,))
        tmpl = cur.fetchone()
        if not tmpl:
            raise HTTPException(404, "Template not found")
        if tmpl["is_locked"]:
            raise HTTPException(400, "Template is locked and cannot be edited")

        cart_count = tmpl["cart_count"]
        run_id = tmpl["run_id"]

        # 2. Fetch current shelf
        cur.execute("""
            SELECT id, profile_id, units_per_shelf, tray_count
            FROM cart_config_template_shelves
            WHERE template_id = %s AND shelf_position = %s FOR UPDATE
        """, (template_id, shelf_position))
        shelf = cur.fetchone()
        if not shelf:
            raise HTTPException(404, f"Shelf position {shelf_position} not found on template")

        shelf_id = shelf["id"]
        profile_id = shelf["profile_id"]
        old_units_per_shelf = shelf["units_per_shelf"] or 0

        # 3. Get units_per_tray for this profile
        cur.execute("""
            SELECT units_per_tray
            FROM packing_profiles
            WHERE id = %s
            LIMIT 1
        """, (profile_id,))
        sc = cur.fetchone()
        if not sc:
            raise HTTPException(400, f"No approved shelf config for profile {profile_id}")
        units_per_tray = sc["units_per_tray"]

        # 4-5. Calculate new units_per_shelf and tray totals
        new_units_per_shelf = sum(v.tray_count * units_per_tray for v in req.varieties)
        new_total_trays = sum(v.tray_count for v in req.varieties)

        # 6. Delta per cart
        delta_per_cart = new_units_per_shelf - old_units_per_shelf

        # 6a. Row-lock availability for every SKU this edit touches, so two concurrent
        # edits can't both pass the check and oversubscribe (double-spend race).
        cur.execute("SELECT batch_id FROM recommendation_runs WHERE id = %s", (run_id,))
        brow = cur.fetchone()
        batch_id = brow["batch_id"] if brow else None

        # 6b. Units this edit will FREE: everything currently ALLOCATED (ledger) by this
        # template's carts at this shelf position. They're cascade-deleted when we rewrite the
        # shelf below (order_line_allocations.cart_shelf_id is ON DELETE CASCADE), so credit
        # them back when checking availability — otherwise keeping/raising an existing SKU is
        # falsely rejected because its old units aren't released until after the check.
        # Reads the LEDGER (single source), which also covers engine-created carts.
        cur.execute("""
            SELECT cs.sku_id, COALESCE(SUM(ola.qty), 0) AS freed
            FROM order_line_allocations ola
            JOIN cart_shelves cs ON cs.id = ola.cart_shelf_id
            JOIN order_carts  oc ON oc.id = cs.cart_id
            WHERE oc.template_id = %s
              AND cs.shelf_position = %s
            GROUP BY cs.sku_id
        """, (template_id, shelf_position))
        freed_by_sku = {r["sku_id"]: int(r["freed"] or 0) for r in cur.fetchall()}

        # Lock every availability line this edit could touch (both the pinned lines and all
        # lines for the touched SKUs within the batch) in a stable order before checking.
        touched_skus = {v.sku_id for v in req.varieties} | set(freed_by_sku.keys())
        lock_ids = {v.availability_line_id for v in req.varieties if v.availability_line_id}
        if batch_id and touched_skus:
            for sk in touched_skus:
                lock_ids.update(_sku_line_ids(cur, sk, batch_id))
        ledger.lock_lines(cur, lock_ids)

        # 7. Availability check for each variety (crediting back the units this edit frees)
        for v in req.varieties:
            required = v.tray_count * units_per_tray * cart_count
            if v.availability_line_id:
                available_base = ledger.remaining_for_line(cur, v.availability_line_id)
            else:
                rem = ledger.remaining_for_lines(cur, _sku_line_ids(cur, v.sku_id, batch_id))
                available_base = min(rem.values()) if rem else 0
            available = available_base + freed_by_sku.get(v.sku_id, 0)
            if available < required:
                raise HTTPException(
                    400,
                    f"Insufficient units: need {required} for SKU {v.sku_id}, only {available} available",
                )

        # ── 9a. Delete old template varieties ─────────────────
        cur.execute("""
            DELETE FROM cart_config_template_varieties WHERE shelf_id = %s
        """, (shelf_id,))

        # ── 9b. Insert new template varieties ─────────────────
        for sort_order, v in enumerate(req.varieties):
            unit_count = v.tray_count * units_per_tray
            cur.execute("""
                INSERT INTO cart_config_template_varieties
                    (shelf_id, sku_id, availability_line_id, tray_count, unit_count, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (shelf_id, v.sku_id, v.availability_line_id, v.tray_count, unit_count, sort_order))

        # ── 9c. Update template shelf totals ──────────────────
        cur.execute("""
            UPDATE cart_config_template_shelves
            SET units_per_shelf = %s,
                tray_count      = %s
            WHERE id = %s
        """, (new_units_per_shelf, new_total_trays, shelf_id))

        # ── 9d. Update template total_units + updated_at ──────
        cur.execute("""
            UPDATE cart_config_templates
            SET total_units = total_units + (%s * %s),
                updated_at  = now()
            WHERE id = %s
        """, (delta_per_cart, cart_count, template_id))

        # ── Find all physical carts for this template ──────────
        cur.execute("""
            SELECT oc.id AS cart_id, oc.order_id
            FROM order_carts oc
            WHERE oc.template_id = %s
        """, (template_id,))
        cart_rows = cur.fetchall()
        cart_ids = [r["cart_id"] for r in cart_rows]
        order_ids = list({r["order_id"] for r in cart_rows})

        if cart_ids:
            # ── 9e. Delete old cart_shelves at this position ───
            cur.execute("""
                DELETE FROM cart_shelves
                WHERE cart_id = ANY(%s::uuid[]) AND shelf_position = %s
            """, (cart_ids, shelf_position))

            # ── Insert new cart_shelves rows (variety × cart) and draw each through the
            #    single LEDGER. Deleting the old shelves above cascaded their ledger rows
            #    (units returned to remaining); allocate() re-checks under the locks taken
            #    in step 6a so a concurrent edit can't oversell. Replaces the old parallel
            #    allocation_reservations write path (now retired for this endpoint).
            for cart_id in cart_ids:
                for v in req.varieties:
                    unit_count = v.tray_count * units_per_tray
                    cur.execute("""
                        INSERT INTO cart_shelves
                            (cart_id, shelf_position, sku_id, profile_id,
                             tray_count, unit_count, availability_line_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, (cart_id, shelf_position, v.sku_id, profile_id,
                          v.tray_count, unit_count, v.availability_line_id))
                    cs_id = cur.fetchone()["id"]
                    if v.availability_line_id and unit_count > 0:
                        try:
                            ledger.allocate(
                                cur,
                                availability_line_id=v.availability_line_id,
                                qty=unit_count,
                                order_line_id=None,   # linked in step 9g recompute
                                cart_shelf_id=cs_id,
                                run_id=run_id,
                                batch_id=batch_id,
                                sku_id=v.sku_id,
                                source="config_editor",
                                lock=False,  # lines already locked in step 6a
                            )
                        except ledger.AllocationError as e:
                            raise HTTPException(
                                400,
                                f"Insufficient units: need {e.need} for SKU {v.sku_id}, only {e.have} available",
                            )

            # ── Recalc each order_cart totals ──────────────────
            cur.execute("""
                UPDATE order_carts
                SET total_units   = (SELECT COALESCE(SUM(unit_count), 0)
                                     FROM cart_shelves WHERE cart_id = order_carts.id),
                    shelves_used  = (SELECT COUNT(DISTINCT shelf_position)
                                     FROM cart_shelves WHERE cart_id = order_carts.id),
                    version       = version + 1
                WHERE id = ANY(%s::uuid[])
            """, (cart_ids,))

            # ── 9g. Recalculate order_lines.draft_qty for affected stores ─────
            # Get all store_ids for orders that contain these carts
            if order_ids:
                # A store newly receiving an edited variety must get an order_line,
                # or the Aster export (which reads order_lines) would miss it even
                # though the physical cart has it. Create any missing lines first;
                # the recalc below sets draft_qty from the physical cart_shelves.
                for oid in order_ids:
                    for v in req.varieties:
                        cur.execute("""
                            INSERT INTO order_lines
                                (order_id, store_id, sku_id, recommended_qty, draft_qty, is_manual_edit)
                            SELECT o.id, o.store_id, %s, 0, 0, true
                            FROM orders o
                            WHERE o.id = %s
                              AND NOT EXISTS (
                                  SELECT 1 FROM order_lines ol
                                  WHERE ol.order_id = o.id AND ol.sku_id = %s)
                        """, (v.sku_id, oid, v.sku_id))

                cur.execute("""
                    SELECT DISTINCT o.store_id, ol.sku_id
                    FROM orders o
                    JOIN order_lines ol ON ol.order_id = o.id
                    WHERE o.id = ANY(%s::uuid[])
                """, (order_ids,))
                affected = cur.fetchall()

                for row in affected:
                    cur.execute("""
                        UPDATE order_lines
                        SET draft_qty = (
                            SELECT COALESCE(SUM(cs.unit_count), 0)
                            FROM order_carts oc
                            JOIN cart_shelves cs ON cs.cart_id = oc.id
                            WHERE oc.order_id = order_lines.order_id
                              AND cs.sku_id   = order_lines.sku_id
                        )
                        WHERE order_id IN (
                            SELECT id FROM orders WHERE store_id = %s
                              AND run_id = %s
                        )
                          AND sku_id = %s
                    """, (row["store_id"], run_id, row["sku_id"]))

        # Record the edit for pollers (bumps run last_activity_at too).
        log_change(cur, run_id, "config", template_id, "edit_shelf",
                   user.get("email") if isinstance(user, dict) else None)

    # ── Return updated template ────────────────────────────────
    with get_db() as conn2:
        cur2 = cursor(conn2)
        cur2.execute(
            _TEMPLATE_QUERY + "WHERE t.id = %s ORDER BY s.shelf_position DESC, v.sort_order",
            (template_id,),
        )
        rows = cur2.fetchall()

    templates = _aggregate_templates(rows)
    result = templates[0] if templates else {}
    result["impact"] = {
        "delta_per_cart":       delta_per_cart,
        "total_delta":          delta_per_cart * cart_count,
        "carts_updated":        len(cart_ids),
        "orders_updated":       len(order_ids),
        "new_units_per_shelf":  new_units_per_shelf,
    }
    return result


@router.post("/templates/{template_id}/lock")
def lock_template(
    template_id: str,
    user=Depends(require_role("manager", "admin")),
):
    """Lock a template so it cannot be edited."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            UPDATE cart_config_templates
            SET is_locked = true, updated_at = now()
            WHERE id = %s
            RETURNING id
        """, (template_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Template not found")
    return {"locked": True, "template_id": template_id}


@router.delete("/templates/{template_id}/lock")
def unlock_template(
    template_id: str,
    user=Depends(require_role("admin")),
):
    """Unlock a template (admin only)."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            UPDATE cart_config_templates
            SET is_locked = false, updated_at = now()
            WHERE id = %s
            RETURNING id
        """, (template_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Template not found")
    return {"locked": False, "template_id": template_id}
