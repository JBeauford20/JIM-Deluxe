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
from services import ledger
from routers.concurrency import log_change, claim_blocker

router = APIRouter()

# ── Input models ──────────────────────────────────────────────

class AddTraysRequest(BaseModel):
    sku_id:               int
    tray_count:           int
    kit_type:             str | None = None    # 'jute_kit' | 'ceramic_kit' | None
    availability_line_id: str | None = None    # which supply source to draw from
    shelf_position:       int | None = None    # if None, engine picks next available shelf
    version:              int | None = None    # optimistic cart version the client last read

class RemoveTraysRequest(BaseModel):
    shelf_position: int
    tray_count:     int
    version:        int | None = None

class MoveShelfRequest(BaseModel):
    from_position: int
    to_position:   int


def _check_cart_version(cart: dict, client_version):
    """Optimistic concurrency: if the client sent the version it read and it no longer matches
    the row, someone else edited this cart first — 409 so the UI can refresh and reapply."""
    if client_version is None:
        return
    if int(cart.get("version") or 0) != int(client_version):
        raise HTTPException(409, "This cart was just changed by someone else — refresh and retry.")

# ── Helpers ───────────────────────────────────────────────────

def get_shelf_config(cur, profile_id: str) -> dict:
    cur.execute("""
        SELECT sc.units_per_shelf, pp.units_per_tray, sc.trays_per_shelf,
               sc.max_shelves, sc.units_per_cart
        FROM shelf_configurations sc
        JOIN packing_profiles pp ON pp.id = sc.profile_id
        WHERE sc.profile_id = %s AND sc.cart_type_id = 'cc' AND sc.approved = true
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
               COUNT(DISTINCT shelf_position)  AS shelves_used
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
            is_flagged   = NOT %s,
            version      = version + 1
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
    Single transaction: shelf + order line + ledger allocation + totals.
    Honors optimistic cart version + store soft-claim; draws through the single ledger path.
    """
    with get_db() as conn:
        cur = cursor(conn)

        # Get cart + order (+ version for optimistic check, + run_id for ledger/change-log)
        cur.execute("""
            SELECT oc.id, oc.order_id, oc.shelves_used, oc.version,
                   oc.effective_max_shelves, oc.shelves_capacity,
                   o.run_id, o.claimed_by
            FROM order_carts oc
            JOIN orders o ON o.id = oc.order_id
            WHERE oc.id = %s FOR UPDATE
        """, (cart_id,))
        cart = cur.fetchone()
        if not cart:
            raise HTTPException(404, "Cart not found")

        _check_cart_version(cart, req.version)
        holder = claim_blocker(cur, cart["order_id"], user.get("email"))
        if holder:
            raise HTTPException(409, f"{holder} is editing this store — refresh before editing.")

        run_id = cart["run_id"]
        cur.execute("SELECT batch_id FROM recommendation_runs WHERE id = %s", (run_id,))
        _br = cur.fetchone()
        batch_id = _br["batch_id"] if _br else None

        max_shelves = cart["effective_max_shelves"] or cart["shelves_capacity"] or 5
        if cart["shelves_used"] >= max_shelves:
            raise HTTPException(400, "Cart is full — no shelf positions available")

        # Determine shelf position
        pos = req.shelf_position
        if pos is None:
            pos = cart["shelves_used"] + 1

        # Get profile, config, and compute units for this shelf
        profile_id = get_sku_profile(cur, req.sku_id)
        cfg = get_shelf_config(cur, profile_id)
        units_per_tray = cfg["units_per_tray"] or cfg["trays_per_shelf"] or 1
        if not units_per_tray:
            raise HTTPException(400, f"No units_per_tray configured for profile {profile_id}")
        units = req.tray_count * units_per_tray

        # Check availability through the single ledger path — availability_line_id strongly
        # recommended to prevent double-spend. Lock the line now; allocate() below re-checks
        # under that lock and raises if another writer drew it out from under us.
        if req.availability_line_id:
            ledger.lock_lines(cur, [req.availability_line_id])
            have = ledger.remaining_for_line(cur, req.availability_line_id)
            if have < units:
                raise HTTPException(400, f"Insufficient availability: need {units}, have {have}")
        else:
            # No availability_line_id supplied — skipping ledger draw.
            # This allows manual builds but risks double-spend. Always supply
            # availability_line_id when building from a batch.
            pass

        # Write shelf
        cur.execute("""
            INSERT INTO cart_shelves
                (cart_id, shelf_position, sku_id, profile_id, kit_type,
                 tray_count, unit_count, availability_line_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (cart_id, pos, req.sku_id, profile_id, req.kit_type,
              req.tray_count, units, req.availability_line_id))
        shelf_id = cur.fetchone()["id"]
        # Plain INSERT: cart_shelves has NO unique(cart_id, shelf_position) — that
        # constraint was dropped Oct 2026 because a shelf position may hold multiple
        # variety rows. An ON CONFLICT upsert here 500s (no matching constraint) and
        # its replace semantics would be wrong for this schema anyway.

        # Update order line draft_qty (and capture its id for the ledger row)
        cur.execute("""
            INSERT INTO order_lines (order_id, store_id, sku_id, recommended_qty, draft_qty, kit_type)
            SELECT %s, o.store_id, %s, 0, %s, %s FROM orders o WHERE o.id = %s
            ON CONFLICT (order_id, sku_id, COALESCE(kit_type,'none')) DO UPDATE SET
                draft_qty = order_lines.draft_qty + %s
            RETURNING id
        """, (cart["order_id"], req.sku_id, units, req.kit_type, cart["order_id"], units))
        _ol = cur.fetchone()
        order_line_id = _ol["id"] if _ol else None

        # Draw availability through the single ledger (locks + re-checks under FOR UPDATE)
        if req.availability_line_id:
            try:
                ledger.allocate(
                    cur,
                    availability_line_id=req.availability_line_id,
                    qty=units,
                    order_line_id=order_line_id,
                    cart_shelf_id=shelf_id,
                    run_id=run_id,
                    batch_id=batch_id,
                    sku_id=req.sku_id,
                    source="manual",
                    lock=False,  # already locked above
                )
            except ledger.AllocationError as e:
                raise HTTPException(400, f"Insufficient availability: need {e.need}, have {e.have}")

        # Recalculate totals
        recalc_cart(cur, cart_id)
        recalc_order(cur, cart["order_id"])
        log_change(cur, run_id, "cart", cart_id, "add_trays", user.get("email"))

        return {"shelf_id": shelf_id, "shelf_position": pos, "units_added": units}


@router.delete("/{cart_id}")
def remove_cart(
    cart_id: str,
    version: int | None = None,
    user=Depends(require_role("order_writer", "manager", "admin"))
):
    """Remove a whole cart from a store's plan. Deleting its shelves cascades the
    allocation ledger + reservations (units return to remaining), then order_lines and
    order totals are recomputed from what's left."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT oc.id, oc.order_id, oc.total_units, oc.version, o.run_id
            FROM order_carts oc
            JOIN orders o ON o.id = oc.order_id
            WHERE oc.id = %s FOR UPDATE
        """, (cart_id,))
        cart = cur.fetchone()
        if not cart:
            raise HTTPException(404, "Cart not found")
        order_id = cart["order_id"]
        _check_cart_version(cart, version)
        holder = claim_blocker(cur, order_id, user.get("email"))
        if holder:
            raise HTTPException(409, f"{holder} is editing this store — refresh before editing.")

        # Delete shelves → ON DELETE CASCADE frees order_line_allocations (ledger) and
        # allocation_reservations tied to those shelves, so remaining goes back up.
        cur.execute("DELETE FROM cart_shelves WHERE cart_id = %s", (cart_id,))
        cur.execute("DELETE FROM order_carts WHERE id = %s", (cart_id,))

        # Recompute each order line's draft_qty from the shelves that remain on this order.
        cur.execute("""
            UPDATE order_lines ol
            SET draft_qty = COALESCE((
                    SELECT SUM(cs.unit_count)
                    FROM order_carts oc JOIN cart_shelves cs ON cs.cart_id = oc.id
                    WHERE oc.order_id = ol.order_id AND cs.sku_id = ol.sku_id), 0)
            WHERE ol.order_id = %s
        """, (order_id,))

        recalc_order(cur, order_id)
        log_change(cur, cart["run_id"], "cart", cart_id, "remove_cart", user.get("email"))
        return {"removed": True, "units_released": cart["total_units"]}


@router.delete("/{cart_id}/shelves/{shelf_position}")
def remove_shelf(
    cart_id: str,
    shelf_position: int,
    version: int | None = None,
    user=Depends(require_role("order_writer", "manager", "admin"))
):
    """Remove a shelf from a cart. Returns its draw to the ledger."""
    with get_db() as conn:
        cur = cursor(conn)

        # Get shelf (+ cart version for optimistic check, + order claim/run for guard/change-log)
        cur.execute("""
            SELECT cs.*, oc.order_id, oc.version AS cart_version, o.run_id
            FROM cart_shelves cs
            JOIN order_carts oc ON oc.id = cs.cart_id
            JOIN orders o ON o.id = oc.order_id
            WHERE cs.cart_id = %s AND cs.shelf_position = %s FOR UPDATE
        """, (cart_id, shelf_position))
        shelf = cur.fetchone()
        if not shelf:
            raise HTTPException(404, "Shelf not found")

        _check_cart_version({"version": shelf["cart_version"]}, version)
        holder = claim_blocker(cur, shelf["order_id"], user.get("email"))
        if holder:
            raise HTTPException(409, f"{holder} is editing this store — refresh before editing.")

        # Return the shelf's draw through the single ledger (deleting the shelf would cascade
        # the ledger rows anyway, but we delete explicitly so remaining is right regardless).
        ledger.release_by_shelf(cur, shelf["id"])

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
        log_change(cur, shelf["run_id"], "cart", cart_id, "remove_shelf", user.get("email"))

        return {"removed": True, "units_released": shelf["unit_count"]}
