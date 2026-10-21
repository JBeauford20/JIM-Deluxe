"""Routing router — store coordinates and load map data."""
import urllib.request, urllib.parse, json, time
from fastapi import APIRouter, Depends
from db import get_db, cursor
from routers.auth import get_current_user, require_role

router = APIRouter()


def _nominatim(city: str, state: str):
    q = urllib.parse.urlencode({"q": f"{city}, {state}, USA", "format": "json", "limit": 1, "countrycodes": "us"})
    url = f"https://nominatim.openstreetmap.org/search?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "JIMDeluxe/1.0 jbeauford@theplantcompany.com"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None, None


@router.get("/run/{run_id}")
def routing_run(run_id: str, user=Depends(get_current_user)):
    """Returns all loads + stores with coordinates for a run."""
    with get_db() as conn:
        cur = cursor(conn)

        # Fetch loads with their store_ids
        cur.execute("""
            SELECT l.id AS load_id, l.load_name, l.region, l.merchant,
                   l.cart_count, l.store_count,
                   array_agg(DISTINCT la.store_id) AS store_ids
            FROM loads l
            JOIN load_assignments la ON la.load_id = l.id
            WHERE l.run_id = %s
            GROUP BY l.id, l.load_name, l.region, l.merchant,
                     l.cart_count, l.store_count
            ORDER BY l.load_number
        """, (run_id,))
        load_rows = cur.fetchall()

        # Collect all store_ids across all loads
        all_store_ids = set()
        for lr in load_rows:
            for sid in (lr["store_ids"] or []):
                if sid is not None:
                    all_store_ids.add(int(sid))

        stores_dict = {}
        missing_count = 0

        if all_store_ids:
            id_list = list(all_store_ids)
            cur.execute("""
                SELECT store_id, store_name, city, state, lat, lng
                FROM stores
                WHERE store_id = ANY(%s)
            """, (id_list,))
            for row in cur.fetchall():
                has_coords = row["lat"] is not None and row["lng"] is not None
                if not has_coords:
                    missing_count += 1
                stores_dict[str(row["store_id"])] = {
                    "store_id": row["store_id"],
                    "store_name": row["store_name"],
                    "city": row["city"],
                    "state": row["state"],
                    "lat": row["lat"],
                    "lng": row["lng"],
                    "has_coords": has_coords,
                }

        loads_out = []
        for lr in load_rows:
            loads_out.append({
                "load_id": lr["load_id"],
                "load_name": lr["load_name"],
                "region": lr["region"],
                "merchant": lr["merchant"],
                "cart_count": lr["cart_count"],
                "store_count": lr["store_count"],
                "store_ids": [int(s) for s in (lr["store_ids"] or []) if s is not None],
            })

        return {
            "loads": loads_out,
            "stores": stores_dict,
            "stores_missing_coords": missing_count,
        }


@router.post("/geocode-batch")
def geocode_batch(user=Depends(require_role("order_writer", "manager", "admin"))):
    """Geocode up to 20 stores that have no lat/lng using Nominatim."""
    with get_db() as conn:
        cur = cursor(conn)

        # Fetch up to 20 stores missing coordinates
        cur.execute("""
            SELECT store_id, city, state
            FROM stores
            WHERE lat IS NULL AND city IS NOT NULL AND state IS NOT NULL
            LIMIT 20
        """)
        rows = cur.fetchall()

        if not rows:
            return {"geocoded": 0, "remaining": 0}

        # Deduplicate by (city, state)
        seen = {}
        for row in rows:
            key = (row["city"].strip().lower(), row["state"].strip().upper())
            if key not in seen:
                seen[key] = (row["city"], row["state"])

        geocoded = 0
        for key, (city, state) in seen.items():
            lat, lng = _nominatim(city, state)
            if lat is not None and lng is not None:
                cur.execute("""
                    UPDATE stores SET lat = %s, lng = %s
                    WHERE city = %s AND state = %s AND lat IS NULL
                """, (lat, lng, city, state))
                geocoded += cur.rowcount
            time.sleep(1.1)  # Nominatim rate limit: 1 request/second

        # Count remaining
        cur.execute("SELECT COUNT(*) AS cnt FROM stores WHERE lat IS NULL")
        remaining = cur.fetchone()["cnt"]

        return {"geocoded": geocoded, "remaining": remaining}
