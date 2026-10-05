"""Orders router — list, approve, mark reviewed."""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from db import get_db, cursor
from routers.auth import get_current_user, require_role

router = APIRouter()

@router.get("/runs/{run_id}")
def list_orders(run_id: str, user=Depends(get_current_user)):
    """All orders for a recommendation run with summary data."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT o.*, s.store_name, s.city, s.state, s.merchant,
                   s.dynamic_velocity_tier, s.volume_code,
                   l.load_name
            FROM orders o
            JOIN stores s ON s.store_id = o.store_id
            LEFT JOIN load_assignments la ON la.order_cart_id IN (
                SELECT id FROM order_carts WHERE order_id = o.id LIMIT 1
            )
            LEFT JOIN loads l ON l.id = la.load_id
            WHERE o.run_id = %s
            ORDER BY o.priority_score DESC
        """, (run_id,))
        return cur.fetchall()

@router.get("/{order_id}")
def order_detail(order_id: str, user=Depends(get_current_user)):
    """Full order detail: lines + carts + shelves."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
        order = cur.fetchone()
        if not order:
            raise HTTPException(404, "Order not found")

        cur.execute("""
            SELECT ol.*, k.description, k.family, k.legacy_product_group
            FROM order_lines ol JOIN skus k ON k.sku_id = ol.sku_id
            WHERE ol.order_id = %s ORDER BY ol.draft_qty DESC
        """, (order_id,))
        lines = cur.fetchall()

        cur.execute("""
            SELECT oc.*,
                   (SELECT json_agg(cs ORDER BY shelf_position)
                    FROM (SELECT cs.*, k.description
                          FROM cart_shelves cs
                          JOIN skus k ON k.sku_id = cs.sku_id
                          WHERE cs.cart_id = oc.id) cs) AS shelves
            FROM order_carts oc WHERE order_id = %s ORDER BY cart_number
        """, (order_id,))
        carts = cur.fetchall()

        return {**order, "lines": lines, "carts": carts}

@router.patch("/{order_id}/approve")
def approve_order(
    order_id: str,
    version: int,
    user=Depends(require_role("manager", "admin"))
):
    """Approve an order. Locks draft_qty → approved_qty. Version check prevents silent overwrites."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            UPDATE orders SET status = 'approved', version = version + 1
            WHERE id = %s AND version = %s AND status = 'draft'
            RETURNING id
        """, (order_id, version))
        if not cur.fetchone():
            raise HTTPException(409, "Order was modified by another user or already approved. Refresh and retry.")

        cur.execute("""
            UPDATE order_lines SET approved_qty = COALESCE(draft_qty, recommended_qty)
            WHERE order_id = %s
        """, (order_id,))

        cur.execute("""
            UPDATE allocation_reservations SET reservation_type = 'approved'
            WHERE order_id = %s AND reservation_type = 'draft'
        """, (order_id,))

        return {"approved": True}

@router.get("/runs/{run_id}/loads")
def list_loads(run_id: str, user=Depends(get_current_user)):
    """All loads for a recommendation run with their store lists."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT l.id, l.load_number, l.load_name, l.merchant, l.region,
                   l.cart_count, l.store_count, l.capacity,
                   l.utilization_pct, l.is_flagged, l.flag_reason,
                   array_agg(DISTINCT la.store_id) AS store_ids
            FROM loads l
            JOIN load_assignments la ON la.load_id = l.id
            WHERE l.run_id = %s
            GROUP BY l.id, l.load_number, l.load_name, l.merchant, l.region,
                     l.cart_count, l.store_count, l.capacity,
                     l.utilization_pct, l.is_flagged, l.flag_reason
            ORDER BY l.load_number
        """, (run_id,))
        return cur.fetchall()


@router.get("/runs/{run_id}/summary")
def run_summary(run_id: str, user=Depends(get_current_user)):
    """Summary metrics for a recommendation run."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT stores_served, total_carts, total_units,
                   inventory_clearance, status, shipping_week
            FROM recommendation_runs WHERE id = %s
        """, (run_id,))
        return cur.fetchone()


@router.patch("/{order_id}/review")
def mark_reviewed(order_id: str, user=Depends(require_role("order_writer","manager","admin"))):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            UPDATE orders SET is_reviewed = true, reviewed_at = now(), reviewed_by = %s
            WHERE id = %s RETURNING id
        """, (user["email"], order_id))
        return {"reviewed": True}
