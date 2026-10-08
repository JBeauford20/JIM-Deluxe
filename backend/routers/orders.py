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


@router.get("/runs/{run_id}/cart-configs")
def cart_configs(run_id: str, user=Depends(get_current_user)):
    """
    Unique cart configurations for a run, grouped by canonical config_code
    (CC1, CC2 … CCN, where N ≤ 25).  The engine's consolidation step assigns
    the codes so similar carts share a code even if their shelf contents differ
    slightly due to inventory depletion.
    """
    from collections import defaultdict

    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT
                oc.id            AS cart_id,
                oc.cart_key,
                oc.config_code,
                oc.total_units,
                oc.shelves_used,
                o.store_id,
                json_agg(
                    json_build_object(
                        'pos',    cs.shelf_position,
                        'sku_id', cs.sku_id,
                        'desc',   k.description,
                        'family', k.family,
                        'trays',  cs.tray_count,
                        'units',  cs.unit_count,
                        'kit',    cs.kit_type
                    ) ORDER BY cs.shelf_position
                ) AS shelves
            FROM order_carts oc
            JOIN orders o          ON o.id = oc.order_id
            LEFT JOIN cart_shelves cs ON cs.cart_id = oc.id
            LEFT JOIN skus k       ON k.sku_id = cs.sku_id
            WHERE o.run_id = %s
            GROUP BY oc.id, oc.cart_key, oc.config_code,
                     oc.total_units, oc.shelves_used, o.store_id
            ORDER BY oc.config_code, oc.total_units DESC
        """, (run_id,))
        carts = cur.fetchall()

    configs = defaultdict(lambda: {
        "shelves": None, "cart_count": 0, "store_ids": set(),
        "total_units": 0, "shelves_used": 0, "sample_cart_key": ""
    })

    for cart in carts:
        code = cart["config_code"] or "CC?"
        cfg  = configs[code]
        cfg["cart_count"] += 1
        cfg["total_units"]  = int(cart["total_units"] or 0)
        cfg["shelves_used"] = int(cart["shelves_used"] or 0)
        if cfg["shelves"] is None:
            cfg["shelves"]          = cart["shelves"] or []
            cfg["sample_cart_key"]  = cart["cart_key"] or ""
        cfg["store_ids"].add(int(cart["store_id"]))

    result = []
    for code, cfg in sorted(configs.items(),
                             key=lambda x: (
                                 int(x[0][2:]) if x[0][2:].isdigit() else 99,
                             )):
        result.append({
            "config_code":     code,
            "cart_count":      cfg["cart_count"],
            "store_count":     len(cfg["store_ids"]),
            "total_units":     cfg["total_units"],
            "shelves_used":    cfg["shelves_used"],
            "sample_cart_key": cfg["sample_cart_key"],
            "shelves":         cfg["shelves"],
            "store_ids":       list(cfg["store_ids"])[:10],
        })
    return result


@router.get("/runs/{run_id}/availability/{batch_id}")
def run_availability(run_id: str, batch_id: str, user=Depends(get_current_user)):
    """
    Inventory the order editor can actually use: batch availability minus
    units already committed in draft/approved order_lines for this run.
    """
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT
                al.id               AS availability_line_id,
                al.sku_id,
                al.units_available,
                al.hard_good_type,
                al.code_level,
                al.kit_selection_required,
                k.description,
                k.family,
                k.legacy_product_group,
                al.units_available - COALESCE(
                    (SELECT SUM(ol.draft_qty)
                     FROM order_lines ol
                     JOIN orders o ON o.id = ol.order_id
                     WHERE o.run_id = %s
                       AND ol.sku_id = al.sku_id),
                    0
                ) AS remaining
            FROM availability_lines al
            JOIN skus k ON k.sku_id = al.sku_id
            WHERE al.batch_id = %s
            ORDER BY remaining DESC
        """, (run_id, batch_id))
        return cur.fetchall()


@router.get("/history")
def order_history(user=Depends(get_current_user)):
    """All finalized (exported) recommendation runs, newest first."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT rr.id, rr.shipping_week, rr.stores_served, rr.total_carts,
                   rr.total_units, rr.inventory_clearance, rr.finalized_at,
                   rr.status, ab.source_filename
            FROM recommendation_runs rr
            JOIN availability_batches ab ON ab.id = rr.batch_id
            WHERE rr.status = 'exported'
            ORDER BY rr.shipping_week DESC
        """)
        return cur.fetchall()


@router.post("/runs/{run_id}/finalize")
def finalize_run(run_id: str, user=Depends(require_role("manager", "admin", "order_writer"))):
    """
    Approve all remaining draft orders and lock the run for archiving.
    Sets run status → exported, records finalized_at timestamp.
    """
    with get_db() as conn:
        cur = cursor(conn)
        # Approve all draft orders in one shot
        cur.execute("""
            UPDATE orders SET status = 'approved', version = version + 1
            WHERE run_id = %s AND status = 'draft'
        """, (run_id,))
        # Fill approved_qty for any order lines that don't have it yet
        cur.execute("""
            UPDATE order_lines SET approved_qty = COALESCE(draft_qty, recommended_qty)
            WHERE order_id IN (SELECT id FROM orders WHERE run_id = %s)
              AND approved_qty IS NULL
        """, (run_id,))
        # Lock the run
        cur.execute("""
            UPDATE recommendation_runs
            SET status = 'exported', finalized_at = NOW()
            WHERE id = %s
            RETURNING id
        """, (run_id,))
        if not cur.fetchone():
            from fastapi import HTTPException
            raise HTTPException(404, "Run not found")
        return {"finalized": True}


@router.post("/runs/{run_id}/reopen")
def reopen_run(run_id: str, user=Depends(require_role("manager", "admin", "order_writer"))):
    """Reopen a finalized run so orders can be edited and re-exported."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            UPDATE recommendation_runs
            SET status = 'draft', finalized_at = NULL
            WHERE id = %s
            RETURNING id
        """, (run_id,))
        if not cur.fetchone():
            from fastapi import HTTPException
            raise HTTPException(404, "Run not found")
        return {"reopened": True}


@router.patch("/{order_id}/review")
def mark_reviewed(order_id: str, user=Depends(require_role("order_writer","manager","admin"))):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            UPDATE orders SET is_reviewed = true, reviewed_at = now(), reviewed_by = %s
            WHERE id = %s RETURNING id
        """, (user["email"], order_id))
        if not cur.fetchone():
            from fastapi import HTTPException
            raise HTTPException(404, "Order not found")
        return {"reviewed": True}
