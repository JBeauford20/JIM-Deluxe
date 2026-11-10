"""
Delivery manifest view — per-store delivery/ship recency from delivery_manifest
(the stacked weekly Compiled shipment sheets). Powers the Deliveries sub-view in the
Order History tab. Last DELIVERED = most recent delivery_date <= today; last SHIPPED =
most recent ship_date (may be a future/in-transit shipment).
"""
from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from db import get_db, cursor
from routers.auth import get_current_user

router = APIRouter()


@router.get("/stores")
def manifest_stores(user=Depends(get_current_user)):
    """One row per store: last delivered (date+week) and last shipped (date+week)."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT DISTINCT ON (store_id) store_id, delivery_date, week_number
            FROM delivery_manifest
            WHERE delivery_date IS NOT NULL AND delivery_date <= CURRENT_DATE
            ORDER BY store_id, delivery_date DESC
        """)
        delivered = {r["store_id"]: r for r in cur.fetchall()}
        cur.execute("""
            SELECT DISTINCT ON (store_id) store_id, ship_date, delivery_date, week_number
            FROM delivery_manifest
            WHERE ship_date IS NOT NULL
            ORDER BY store_id, ship_date DESC
        """)
        shipped = {r["store_id"]: r for r in cur.fetchall()}
        ids = sorted(set(delivered) | set(shipped))
        meta = {}
        if ids:
            cur.execute("""
                SELECT store_id, store_name, city, state,
                       TRIM(volume_code) AS volume_code, region
                FROM stores WHERE store_id = ANY(%s::bigint[])
            """, (ids,))
            meta = {r["store_id"]: r for r in cur.fetchall()}
    today = date.today()
    out = []
    for sid in ids:
        d = delivered.get(sid); sh = shipped.get(sid); m = meta.get(sid, {})
        out.append({
            "store_id": sid,
            "store_name": m.get("store_name"), "city": m.get("city"),
            "state": m.get("state"), "volume_code": m.get("volume_code"),
            "region": m.get("region"),
            "last_delivery_date": d["delivery_date"] if d else None,
            "last_delivery_week": d["week_number"] if d else None,
            "last_ship_date": sh["ship_date"] if sh else None,
            "last_ship_week": sh["week_number"] if sh else None,
            "in_transit": bool(sh and sh["delivery_date"] and sh["delivery_date"] > today),
        })
    return out


@router.get("/store/{store_id}")
def manifest_store_detail(store_id: int, user=Depends(get_current_user)):
    """Full shipment history for one store (all manifest rows, newest first)."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT week_number, ship_date, delivery_date, load_code, source
            FROM delivery_manifest WHERE store_id = %s
            ORDER BY COALESCE(delivery_date, ship_date) DESC
        """, (store_id,))
        return cur.fetchall()
