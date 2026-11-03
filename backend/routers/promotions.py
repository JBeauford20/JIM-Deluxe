"""
Promotions — manage nationwide / regional pushes (Holiday, Black Friday, etc.) and
track rollout: how many in-scope stores have LANDED the promo product, how many are
IN TRANSIT, and how many are LEFT to hit.

Metrics are data-driven:
  scope       = active stores in the promo's regions (regions=['ALL'] -> nationwide)
  landed      = scope stores with a delivery of a promo SKU since start_date
  in_transit  = scope stores on the road (in_transit_loads), not yet landed
  left        = scope - landed - in_transit

An Active promo is the signal for the engine to front-load those stores (scenario hook).
"""
from datetime import date
from collections import defaultdict
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from db import get_db, cursor
from routers.auth import get_current_user, require_role

router = APIRouter()

EXCLUDED_STATUS = ('paused', 'do_not_ship', 'inactive', 'hold')


class PromotionInput(BaseModel):
    name:        str
    description: Optional[str] = None
    regions:     List[str] = ['ALL']          # ['ALL'] nationwide, or divisions e.g. ['South']
    start_date:  Optional[str] = None          # YYYY-MM-DD
    end_date:    Optional[str] = None
    is_active:   bool = False
    sku_ids:     List[int] = []


class PromotionPatch(BaseModel):
    name:        Optional[str] = None
    description: Optional[str] = None
    regions:     Optional[List[str]] = None
    start_date:  Optional[str] = None
    end_date:    Optional[str] = None
    is_active:   Optional[bool] = None
    sku_ids:     Optional[List[int]] = None


def _metrics(cur, pid, regions, sku_ids, start_date):
    """Compute scope / landed / in_transit / left (+ per-division breakdown).
    in_transit counts only in-transit loads TAGGED to this promotion (promotion_id),
    so an everyday load to a store never checks off its promo status."""
    nationwide = (not regions) or ('ALL' in regions)
    if nationwide:
        cur.execute("""
            SELECT store_id, division FROM stores
            WHERE active = true
              AND (store_status IS NULL OR store_status NOT IN %s)
        """, (EXCLUDED_STATUS,))
    else:
        cur.execute("""
            SELECT store_id, division FROM stores
            WHERE active = true
              AND (store_status IS NULL OR store_status NOT IN %s)
              AND division = ANY(%s)
        """, (EXCLUDED_STATUS, regions))
    scope = {r["store_id"]: (r["division"] or "—") for r in cur.fetchall()}
    scope_ids = list(scope)
    since = start_date or date(2000, 1, 1)

    landed = set()
    if sku_ids and scope_ids:
        cur.execute("""
            SELECT DISTINCT store_id FROM deliveries
            WHERE store_id = ANY(%s) AND sku_id = ANY(%s) AND delivery_date >= %s
        """, (scope_ids, sku_ids, since))
        landed = {r["store_id"] for r in cur.fetchall()}

    in_transit = set()
    if scope_ids:
        # "Currently on the road" — recent in-transit records, not tied to the promo
        # start (a store on a truck right now counts toward the active promo).
        cur.execute("""
            SELECT DISTINCT store_id FROM in_transit_loads
            WHERE store_id = ANY(%s)
              AND promotion_id = %s
              AND (shipping_week >= CURRENT_DATE - INTERVAL '45 days'
                   OR delivery_date >= CURRENT_DATE - INTERVAL '7 days')
        """, (scope_ids, pid))
        in_transit = {r["store_id"] for r in cur.fetchall()} & set(scope_ids)
    in_transit -= landed
    left = set(scope_ids) - landed - in_transit

    bd = defaultdict(lambda: {"scope": 0, "landed": 0, "in_transit": 0, "left": 0})
    for sid, div in scope.items():
        bd[div]["scope"] += 1
        if sid in landed:        bd[div]["landed"] += 1
        elif sid in in_transit:  bd[div]["in_transit"] += 1
        else:                    bd[div]["left"] += 1

    return {
        "scope": len(scope_ids), "landed": len(landed),
        "in_transit": len(in_transit), "left": len(left),
        "by_division": [{"division": d, **v} for d, v in sorted(bd.items())],
    }


def _load_promo(cur, pid):
    cur.execute("SELECT * FROM promotions WHERE id = %s", (pid,))
    p = cur.fetchone()
    if not p:
        raise HTTPException(404, "Promotion not found")
    cur.execute("SELECT sku_id FROM promotion_skus WHERE promotion_id = %s", (pid,))
    p = dict(p)
    p["sku_ids"] = [r["sku_id"] for r in cur.fetchall()]
    return p


@router.get("/")
def list_promotions(user=Depends(get_current_user)):
    """All promotions with rollout metrics."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT * FROM promotions ORDER BY is_active DESC, start_date DESC NULLS LAST, id")
        promos = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT promotion_id, sku_id FROM promotion_skus")
        sk = defaultdict(list)
        for r in cur.fetchall():
            sk[r["promotion_id"]].append(r["sku_id"])
        out = []
        for p in promos:
            p["sku_ids"] = sk.get(p["id"], [])
            p["metrics"] = _metrics(cur, p["id"], p["regions"], p["sku_ids"], p["start_date"])
            out.append(p)
        return out


@router.get("/skus")
def list_skus(user=Depends(get_current_user)):
    """SKU list for the promotion SKU picker."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT sku_id, description, family, legacy_product_group, is_holiday
            FROM skus WHERE active = true ORDER BY is_holiday DESC, family, description
        """)
        return cur.fetchall()


@router.get("/{pid}")
def get_promotion(pid: int, user=Depends(get_current_user)):
    """One promotion with full rollout metrics + per-division breakdown + its SKUs."""
    with get_db() as conn:
        cur = cursor(conn)
        p = _load_promo(cur, pid)
        p["metrics"] = _metrics(cur, p["id"], p["regions"], p["sku_ids"], p["start_date"])
        if p["sku_ids"]:
            cur.execute("SELECT sku_id, description, family FROM skus WHERE sku_id = ANY(%s)", (p["sku_ids"],))
            p["skus"] = cur.fetchall()
        else:
            p["skus"] = []
        return p


@router.post("/", status_code=201)
def create_promotion(body: PromotionInput, user=Depends(require_role("order_writer", "manager", "admin"))):
    if not body.name.strip():
        raise HTTPException(400, "name required")
    email = user.get("email", "system") if isinstance(user, dict) else "system"
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            INSERT INTO promotions (name, description, regions, start_date, end_date, is_active, created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (body.name.strip(), body.description, body.regions or ['ALL'],
              body.start_date or None, body.end_date or None, body.is_active, email))
        pid = cur.fetchone()["id"]
        for s in set(body.sku_ids):
            cur.execute("INSERT INTO promotion_skus (promotion_id, sku_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (pid, s))
        return _load_promo(cur, pid)


@router.patch("/{pid}")
def update_promotion(pid: int, body: PromotionPatch, user=Depends(require_role("order_writer", "manager", "admin"))):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT id FROM promotions WHERE id = %s", (pid,))
        if not cur.fetchone():
            raise HTTPException(404, "Promotion not found")
        sets, params = [], []
        for col in ("name", "description", "regions", "start_date", "end_date", "is_active"):
            val = getattr(body, col)
            if val is not None:
                sets.append(f"{col} = %s")
                params.append(val if col != "start_date" and col != "end_date" else (val or None))
        if sets:
            sets.append("updated_at = now()")
            params.append(pid)
            cur.execute(f"UPDATE promotions SET {', '.join(sets)} WHERE id = %s", params)
        if body.sku_ids is not None:
            cur.execute("DELETE FROM promotion_skus WHERE promotion_id = %s", (pid,))
            for s in set(body.sku_ids):
                cur.execute("INSERT INTO promotion_skus (promotion_id, sku_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (pid, s))
        return _load_promo(cur, pid)


@router.delete("/{pid}")
def delete_promotion(pid: int, user=Depends(require_role("manager", "admin"))):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("DELETE FROM promotions WHERE id = %s", (pid,))
        return {"deleted": cur.rowcount}
