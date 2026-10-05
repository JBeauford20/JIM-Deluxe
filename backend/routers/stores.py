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
    limit:   int  = Query(50, le=500),  # cap at 500
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

        sql = f"""
            SELECT store_id, store_name, city, state, region, merchant,
                   volume_code, dynamic_velocity_tier, dynamic_velocity_score,
                   tier_9cm, tier_12cm, tier_h2o, tier_collectors, tier_boutique,
                   merch_partner
            FROM stores
            WHERE {' AND '.join(where)}
            ORDER BY dynamic_velocity_score DESC NULLS LAST
            LIMIT %s OFFSET %s
        """
        cur.execute(sql, params + [limit, offset])
        return cur.fetchall()


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

        return {
            "store":            store,
            "velocity_history": velocity_history,
            "group_velocity":   group_velocity,
            "top_skus":         top_skus,
            "weekly_sales":     weekly_sales,
            "recent_deliveries": recent_deliveries,
        }
