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
            FROM availability_remaining ar
            JOIN recommendation_runs rr ON rr.batch_id = ar.batch_id
            JOIN cart_config_templates t ON t.run_id = rr.id
            JOIN availability_lines al
                   ON al.availability_line_id = ar.availability_line_id
            JOIN skus sk ON sk.sku_id = ar.sku_id
            WHERE t.id = %s
              AND ar.batch_id = rr.batch_id
            GROUP BY ar.availability_line_id, ar.sku_id, ar.units_available,
                     ar.remaining, ar.draft_reserved, ar.approved_reserved,
                     al.code_level, al.hard_good_type,
                     sk.description, sk.family, sk.legacy_product_group
            ORDER BY sk.family, ar.units_available DESC
        """, (template_id,))
        return cur.fetchall()


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

        # Fetch template
        cur.execute("""
            SELECT cart_count FROM cart_config_templates WHERE id = %s
        """, (template_id,))
        tmpl = cur.fetchone()
        if not tmpl:
            raise HTTPException(404, "Template not found")
        cart_count = tmpl["cart_count"]

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
            FROM shelf_configurations
            WHERE profile_id = %s AND approved = true
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

        # Check availability per variety
        per_variety_check = []
        all_ok = True
        for v in req.new_varieties:
            required = v.tray_count * units_per_tray * cart_count

            if v.availability_line_id:
                cur.execute("""
                    SELECT remaining FROM availability_remaining
                    WHERE availability_line_id = %s
                """, (v.availability_line_id,))
            else:
                cur.execute("""
                    SELECT MIN(remaining) AS remaining
                    FROM availability_remaining
                    WHERE sku_id = %s
                """, (v.sku_id,))
            avail = cur.fetchone()
            available = int(avail["remaining"] or 0) if avail else 0
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
            FROM shelf_configurations
            WHERE profile_id = %s AND approved = true
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

        # 7. Availability check for each variety
        for v in req.varieties:
            required = v.tray_count * units_per_tray * cart_count
            if v.availability_line_id:
                cur.execute("""
                    SELECT remaining FROM availability_remaining
                    WHERE availability_line_id = %s FOR UPDATE
                """, (v.availability_line_id,))
            else:
                cur.execute("""
                    SELECT MIN(remaining) AS remaining FROM availability_remaining
                    WHERE sku_id = %s FOR UPDATE
                """, (v.sku_id,))
            avail = cur.fetchone()
            available = int(avail["remaining"] or 0) if avail else 0
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
                WHERE cart_id = ANY(%s) AND shelf_position = %s
            """, (cart_ids, shelf_position))

            # ── Insert new cart_shelves rows (variety × cart) ──
            for cart_id in cart_ids:
                for v in req.varieties:
                    unit_count = v.tray_count * units_per_tray
                    cur.execute("""
                        INSERT INTO cart_shelves
                            (cart_id, shelf_position, sku_id, profile_id,
                             tray_count, unit_count, availability_line_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (cart_id, shelf_position, v.sku_id, profile_id,
                          v.tray_count, unit_count, v.availability_line_id))

            # ── Recalc each order_cart totals ──────────────────
            cur.execute("""
                UPDATE order_carts
                SET total_units   = (SELECT COALESCE(SUM(unit_count), 0)
                                     FROM cart_shelves WHERE cart_id = order_carts.id),
                    shelves_used  = (SELECT COUNT(DISTINCT shelf_position)
                                     FROM cart_shelves WHERE cart_id = order_carts.id)
                WHERE id = ANY(%s)
            """, (cart_ids,))

            # ── 9f. Release old allocation_reservations for this shelf position ──
            # Identify the old cart_shelf IDs that were deleted so we can release
            # any reservations that referenced them. Since we already deleted them,
            # release by cart_id + reservation matching the run.
            cur.execute("""
                UPDATE allocation_reservations
                SET reservation_type = 'released',
                    released_at      = now()
                WHERE run_id = %s
                  AND cart_shelf_id IN (
                      SELECT cs_hist.id FROM cart_shelves cs_hist
                      WHERE cs_hist.cart_id = ANY(%s)
                        AND cs_hist.shelf_position = %s
                  )
                  AND reservation_type IN ('draft', 'approved')
            """, (run_id, cart_ids, shelf_position))

            # Create new draft reservations for each new variety × each cart
            for cart_id_row in cart_rows:
                cart_id  = cart_id_row["cart_id"]
                order_id = cart_id_row["order_id"]
                for v in req.varieties:
                    if not v.availability_line_id:
                        continue
                    unit_count = v.tray_count * units_per_tray
                    # Get the cart_shelf id we just inserted
                    cur.execute("""
                        SELECT id FROM cart_shelves
                        WHERE cart_id = %s AND shelf_position = %s AND sku_id = %s
                        ORDER BY created_at DESC LIMIT 1
                    """, (cart_id, shelf_position, v.sku_id))
                    cs_row = cur.fetchone()
                    if not cs_row:
                        continue
                    cur.execute("""
                        INSERT INTO allocation_reservations
                            (availability_line_id, run_id, order_id, cart_shelf_id,
                             reserved_qty, reservation_type)
                        VALUES (%s, %s, %s, %s, %s, 'draft')
                    """, (v.availability_line_id, run_id, order_id,
                          cs_row["id"], unit_count))

            # ── 9g. Recalculate order_lines.draft_qty for affected stores ─────
            # Get all store_ids for orders that contain these carts
            if order_ids:
                cur.execute("""
                    SELECT DISTINCT o.store_id, ol.sku_id
                    FROM orders o
                    JOIN order_lines ol ON ol.order_id = o.id
                    WHERE o.id = ANY(%s)
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
