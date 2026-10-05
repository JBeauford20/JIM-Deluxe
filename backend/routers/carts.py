"""
Cart editor endpoints — the hardest part.
Every edit is ONE transaction that updates:
  cart_shelves, order_lines, allocation_reservations, order_carts, orders
If anything fails, everything rolls back.
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from db import get_db, cursor
from routers.auth import get_current_user, require_role

router = APIRouter()

# ── Input models ──────────────────────────────────────────────

class AddTraysRequest(BaseModel):
    sku_id:               int
    tray_count:           int
    kit_type:             str | None = None    # 'jute_kit' | 'ceramic_kit' | None
    availability_line_id: str | None = None    # which supply source to draw from
    shelf_position:       int | None = None    # if None, engine picks next available shelf

class RemoveTraysRequest(BaseModel):
    shelf_position: int
    tray_count:     int

class MoveShelfRequest(BaseModel):
    from_position: int
    to_position:   int

# ── Helpers ───────────────────────────────────────────────────

def get_shelf_config(cur, profile_id: str) -> dict:
    cur.execute("""
        SELECT units_per_shelf, max_shelves, units_per_cart
        FROM shelf_configurations
        WHERE profile_id = %s AND cart_type_id = 'cc' AND approved = true
    """, (profile_id,))
    cfg = cur.fetchone()
    if not cfg:
        raise HTTPException(400, f"No approved shelf config for profile {profile_id}")
    return cfg

def get_sku_profile(cur, sku_id: int) -> str:
    cur.execute("""
        SELECT profile_id FROM sku_packing_profiles
        WHERE sku_id = %s AND is_default = true
    """, (sku_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(400, f"No default packing profile for SKU {sku_id}")
    return row["profile_id"]

def recalc_cart(cur, cart_id: str):
    """Recalculate shelves_used, total_units, is_full after any shelf change."""
    cur.execute("""
        SELECT COALESCE(SUM(unit_count), 0)  AS total_units,
               COUNT(*)                       AS shelves_used
        FROM cart_shelves WHERE cart_id = %s
    """, (cart_id,))
    agg = cur.fetchone()

    cur.execute("""
        SELECT effective_max_shelves, shelves_capacity
        FROM order_carts WHERE id = %s
    """, (cart_id,))
    cart = cur.fetchone()
    eff_max = cart["effective_max_shelves"] or cart["shelves_capacity"] or 5
    is_full = agg["shelves_used"] >= eff_max

    cur.execute("""
        UPDATE order_carts SET
            shelves_used = %s,
            total_units  = %s,
            is_full      = %s,
            is_flagged   = NOT %s
        WHERE id = %s
    """, (agg["shelves_used"], agg["total_units"], is_full, is_full, cart_id))
    return agg["total_units"]

def recalc_order(cur, order_id: str):
    """Recalculate order totals and bump version after any cart change."""
    cur.execute("""
        SELECT COALESCE(SUM(total_units), 0) AS total, COUNT(*) AS carts
        FROM order_carts WHERE order_id = %s
    """, (order_id,))
    agg = cur.fetchone()
    cur.execute("""
        UPDATE orders SET
            total_units = %s,
            cart_count  = %s,
            version     = version + 1
        WHERE id = %s
    """, (agg["total"], agg["carts"], order_id))

# ── Routes ────────────────────────────────────────────────────

@router.get("/{cart_id}")
def get_cart(cart_id: str, user=Depends(get_current_user)):
    """Full cart detail with shelf breakdown."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT oc.*, o.store_id, o.run_id
            FROM order_carts oc
            JOIN orders o ON o.id = oc.order_id
            WHERE oc.id = %s
        """, (cart_id,))
        cart = cur.fetchone()
        if not cart:
            raise HTTPException(404, "Cart not found")

        cur.execute("""
            SELECT cs.*, k.description, k.family, k.legacy_product_group,
                   p.name AS profile_name, p.units_per_tray
            FROM cart_shelves cs
            JOIN skus k ON k.sku_id = cs.sku_id
            LEFT JOIN packing_profiles p ON p.id = cs.profile_id
            WHERE cs.cart_id = %s
            ORDER BY cs.shelf_position
        """, (cart_id,))
        shelves = cur.fetchall()
        return {**cart, "shelves": shelves}


@router.post("/{cart_id}/shelves", status_code=201)
def add_trays(
    cart_id: str,
    req: AddTraysRequest,
    user=Depends(require_role("order_writer", "manager", "admin"))
):
    """
    Add trays of a SKU to a cart shelf.
    Single transaction: shelf + order line + reservation + totals.
    """
    with get_db() as conn:
        cur = cursor(conn)

        # Get cart + order
        cur.execute("""
            SELECT oc.id, oc.order_id, oc.shelves_used,
                   oc.effective_max_shelves, oc.shelves_capacity
            FROM order_carts oc WHERE oc.id = %s FOR UPDATE
        """, (cart_id,))
        cart = cur.fetchone()
        if not cart:
            raise HTTPException(404, "Cart not found")

        max_shelves = cart["effective_max_shelves"] or cart["shelves_capacity"] or 5
        if cart["shelves_used"] >= max_shelves:
            raise HTTPException(400, "Cart is full — no shelf positions available")

        # Determine shelf position
        pos = req.shelf_position
        if pos is None:
            pos = cart["shelves_used"] + 1

        # Get profile and config
        profile_id = get_sku_profile(cur, req.sku_id)
        cfg = get_shelf_config(cur, profile_id)
        units = req.tray_count * cfg["units_per_shelf"] // (cfg["units_per_shelf"] // cfg["units_per_shelf"])
        units = req.tray_count * (cfg["units_per_shelf"] // max(1, 6))  # trays × units_per_tray
        # Simpler: units = tray_count × units_per_tray from packing_profiles
        cur.execute("SELECT units_per_tray FROM packing_profiles WHERE id = %s", (profile_id,))
        units = req.tray_count * cur.fetchone()["units_per_tray"]

        # Check availability
        if req.availability_line_id:
            cur.execute("""
                SELECT remaining FROM availability_remaining
                WHERE availability_line_id = %s FOR UPDATE
            """, (req.availability_line_id,))
            avail = cur.fetchone()
            if not avail or avail["remaining"] < units:
                raise HTTPException(400, f"Insufficient availability: need {units}, have {avail['remaining'] if avail else 0}")

        # Write shelf
        cur.execute("""
            INSERT INTO cart_shelves
                (cart_id, shelf_position, sku_id, profile_id, kit_type,
                 tray_count, unit_count, availability_line_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (cart_id, shelf_position) DO UPDATE SET
                sku_id      = EXCLUDED.sku_id,
                profile_id  = EXCLUDED.profile_id,
                kit_type    = EXCLUDED.kit_type,
                tray_count  = EXCLUDED.tray_count,
                unit_count  = EXCLUDED.unit_count
            RETURNING id
        """, (cart_id, pos, req.sku_id, profile_id, req.kit_type,
              req.tray_count, units, req.availability_line_id))
        shelf_id = cur.fetchone()["id"]

        # Reserve availability
        if req.availability_line_id:
            cur.execute("""
                INSERT INTO allocation_reservations
                    (availability_line_id, order_id, cart_shelf_id, reserved_qty, reservation_type)
                VALUES (%s, %s, %s, %s, 'draft')
            """, (req.availability_line_id, cart["order_id"], shelf_id, units))

        # Update order line draft_qty
        cur.execute("""
            INSERT INTO order_lines (order_id, store_id, sku_id, recommended_qty, draft_qty, kit_type)
            SELECT %s, o.store_id, %s, 0, 0, %s FROM orders o WHERE o.id = %s
            ON CONFLICT (order_id, sku_id, COALESCE(kit_type,'none')) DO UPDATE SET
                draft_qty = order_lines.draft_qty + %s
        """, (cart["order_id"], req.sku_id, req.kit_type, cart["order_id"], units))

        # Recalculate totals
        recalc_cart(cur, cart_id)
        recalc_order(cur, cart["order_id"])

        return {"shelf_id": shelf_id, "shelf_position": pos, "units_added": units}


@router.delete("/{cart_id}/shelves/{shelf_position}")
def remove_shelf(
    cart_id: str,
    shelf_position: int,
    user=Depends(require_role("order_writer", "manager", "admin"))
):
    """Remove a shelf from a cart. Releases availability reservation."""
    with get_db() as conn:
        cur = cursor(conn)

        # Get shelf
        cur.execute("""
            SELECT cs.*, oc.order_id
            FROM cart_shelves cs
            JOIN order_carts oc ON oc.id = cs.cart_id
            WHERE cs.cart_id = %s AND cs.shelf_position = %s FOR UPDATE
        """, (cart_id, shelf_position))
        shelf = cur.fetchone()
        if not shelf:
            raise HTTPException(404, "Shelf not found")

        # Release reservation
        cur.execute("""
            UPDATE allocation_reservations
            SET reservation_type = 'released', released_at = now()
            WHERE cart_shelf_id = %s
        """, (shelf["id"],))

        # Remove shelf
        cur.execute("DELETE FROM cart_shelves WHERE id = %s", (shelf["id"],))

        # Update order line draft_qty
        cur.execute("""
            UPDATE order_lines
            SET draft_qty = GREATEST(0, draft_qty - %s)
            WHERE order_id = %s AND sku_id = %s
        """, (shelf["unit_count"], shelf["order_id"], shelf["sku_id"]))

        recalc_cart(cur, cart_id)
        recalc_order(cur, shelf["order_id"])

        return {"removed": True, "units_released": shelf["unit_count"]}
