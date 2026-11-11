"""Store research endpoints — the store profile panel."""
from fastapi import APIRouter, Depends, Query
from db import get_db, cursor
from routers.auth import get_current_user

router = APIRouter()

@router.get("/")
def list_stores(
    search:  str  = Query(None),
    tier:    str  = Query(None),
    region:  str  = Query(None),
    merchant: str = Query(None),
    state:   str  = Query(None),
    division: str = Query(None),
    fenceline: bool = Query(None),
    limit:   int  = Query(50, le=2500),  # cap raised so the Stores tab can load all
    offset:  int  = Query(0),
    user=Depends(get_current_user)
):
    """List stores with optional filters. Powers the store picker."""
    with get_db() as conn:
        cur = cursor(conn)
        where, params = ["active = true"], []
        if search:
            where.append("(store_name ILIKE %s OR city ILIKE %s OR CAST(store_id AS text) LIKE %s)")
            params += [f"%{search}%", f"%{search}%", f"%{search}%"]
        if tier:
            where.append("dynamic_velocity_tier = %s"); params.append(tier)
        if region:
            where.append("region ILIKE %s"); params.append(f"%{region}%")
        if merchant:
            where.append("merchant ILIKE %s"); params.append(f"%{merchant}%")
        if state:
            where.append("state = %s"); params.append(state.upper())
        if division:
            where.append("division ILIKE %s"); params.append(f"%{division}%")
        if fenceline is not None:
            where.append("COALESCE(fenceline, false) = %s"); params.append(fenceline)

        sql = f"""
            SELECT store_id, store_name, city, state, region, division, merchant,
                   volume_code, volume_code_updated_at, volume_code_updated_by,
                   dynamic_velocity_tier, dynamic_velocity_score,
                   tier_9cm, tier_12cm, tier_h2o, tier_collectors, tier_boutique,
                   merch_partner, store_status,
                   COALESCE(perm_racks, 0) AS perm_racks, COALESCE(fenceline, false) AS fenceline
            FROM stores
            WHERE {' AND '.join(where)}
            ORDER BY dynamic_velocity_score DESC NULLS LAST
            LIMIT %s OFFSET %s
        """
        cur.execute(sql, params + [limit, offset])
        return cur.fetchall()


@router.get("/{store_id}/last-order")
def store_last_order(store_id: int, user=Depends(get_current_user)):
    """The store's most recent ACTUAL shipment/delivery: the date, its week, and a pivoted
    SKU list (what physically went to the store on that delivery)."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT MAX(delivery_date) AS d FROM deliveries WHERE store_id = %s", (store_id,))
        row = cur.fetchone()
        last_date = row["d"] if row else None
        if not last_date:
            return {"delivery_date": None, "week": None, "lines": []}
        cur.execute("""
            SELECT d.sku_id, k.description, k.family, SUM(d.units_delivered) AS units
            FROM deliveries d JOIN skus k ON k.sku_id = d.sku_id
            WHERE d.store_id = %s AND d.delivery_date = %s
            GROUP BY d.sku_id, k.description, k.family
            ORDER BY units DESC
        """, (store_id, last_date))
        lines = cur.fetchall()
    return {"delivery_date": last_date, "week": int(last_date.isocalendar()[1]), "lines": lines}


@router.get("/{store_id}")
def store_profile(store_id: int, user=Depends(get_current_user)):
    """Full store profile — velocity, SKU breakdown, recent deliveries."""
    with get_db() as conn:
        cur = cursor(conn)

        # Core store info
        cur.execute("SELECT * FROM stores WHERE store_id = %s", (store_id,))
        store = cur.fetchone()
        if not store:
            from fastapi import HTTPException
            raise HTTPException(404, "Store not found")

        # Velocity history (last 12 weeks)
        cur.execute("""
            SELECT week_start, static_tier, dynamic_tier, dynamic_score, tier_changed
            FROM velocity_history
            WHERE store_id = %s
            ORDER BY week_start DESC LIMIT 12
        """, (store_id,))
        velocity_history = cur.fetchall()

        # SKU group velocity
        cur.execute("""
            SELECT sg.display_name, sg.id, sgv.dynamic_tier,
                   sgv.ewma_score, sgv.percentile, sgv.weeks_of_data
            FROM store_group_velocity sgv
            JOIN sku_groups sg ON sg.id = sgv.sku_group_id
            WHERE sgv.store_id = %s
              AND sgv.week_start = (SELECT MAX(week_start) FROM store_group_velocity)
            ORDER BY sgv.ewma_score DESC NULLS LAST
        """, (store_id,))
        group_velocity = cur.fetchall()

        # Top SKUs (all time)
        cur.execute("""
            SELECT k.sku_id, k.description, k.family, k.legacy_product_group,
                   SUM(s.units_sold) AS total_units,
                   COUNT(DISTINCT s.sale_date) AS sale_days
            FROM sales s
            JOIN skus k ON k.sku_id = s.sku_id
            WHERE s.store_id = %s
            GROUP BY k.sku_id, k.description, k.family, k.legacy_product_group
            ORDER BY total_units DESC LIMIT 10
        """, (store_id,))
        top_skus = cur.fetchall()

        # Weekly sales trend (last 26 weeks)
        cur.execute("""
            SELECT date_trunc('week', sale_date)::date AS week_start,
                   SUM(units_sold) AS units
            FROM sales
            WHERE store_id = %s
            GROUP BY date_trunc('week', sale_date)::date
            ORDER BY week_start DESC LIMIT 26
        """, (store_id,))
        weekly_sales = cur.fetchall()

        # Recent deliveries
        cur.execute("""
            SELECT delivery_date, SUM(units_delivered) AS units, load_id
            FROM deliveries
            WHERE store_id = %s
            GROUP BY delivery_date, load_id
            ORDER BY delivery_date DESC LIMIT 10
        """, (store_id,))
        recent_deliveries = cur.fetchall()

        # Delivery manifest recency: last delivered (<= today) + last shipped (may be future = in transit)
        cur.execute("""
            SELECT (SELECT delivery_date FROM delivery_manifest
                    WHERE store_id = %s AND delivery_date IS NOT NULL AND delivery_date <= CURRENT_DATE
                    ORDER BY delivery_date DESC LIMIT 1) AS last_delivery_date,
                   (SELECT week_number FROM delivery_manifest
                    WHERE store_id = %s AND delivery_date IS NOT NULL AND delivery_date <= CURRENT_DATE
                    ORDER BY delivery_date DESC LIMIT 1) AS last_delivery_week,
                   (SELECT week_number FROM delivery_manifest
                    WHERE store_id = %s AND ship_date IS NOT NULL
                    ORDER BY ship_date DESC LIMIT 1) AS last_ship_week,
                   EXISTS (SELECT 1 FROM delivery_manifest
                    WHERE store_id = %s AND delivery_date > CURRENT_DATE) AS in_transit
        """, (store_id, store_id, store_id, store_id))
        manifest = cur.fetchone()

        # YTD sales in wholesale dollars (units_sold × wholesale_price_whl, this calendar year)
        cur.execute("""
            SELECT COALESCE(SUM(s.units_sold * COALESCE(sk.wholesale_price_whl, 0)), 0) AS ytd_whl
            FROM sales s JOIN skus sk ON sk.sku_id = s.sku_id
            WHERE s.store_id = %s AND s.sale_date >= date_trunc('year', CURRENT_DATE)
        """, (store_id,))
        ytd_sales_whl = float(cur.fetchone()["ytd_whl"] or 0)

        return {
            "store":            store,
            "ytd_sales_whl":    ytd_sales_whl,
            "velocity_history": velocity_history,
            "group_velocity":   group_velocity,
            "top_skus":         top_skus,
            "weekly_sales":     weekly_sales,
            "recent_deliveries": recent_deliveries,
            "manifest":         manifest,
        }
