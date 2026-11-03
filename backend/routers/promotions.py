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
import io, os, tempfile
from datetime import date, datetime
from collections import defaultdict
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from pydantic import BaseModel

from db import get_db, cursor
from routers.auth import get_current_user, require_role

router = APIRouter()

EXCLUDED_STATUS = ('paused', 'do_not_ship', 'inactive', 'hold')


def _parse_date(v):
    if v is None or str(v).strip() == '':
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y",
                "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


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


class AddStoresRequest(BaseModel):
    store_ids:     List[int]
    status:        str = "landed"            # 'landed' (received) | 'in_transit'
    received_date: Optional[str] = None


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
    scope_set = set(scope_ids)
    since = start_date or date(2000, 1, 1)
    today = date.today()

    landed = set()
    in_transit = set()

    # (1) DTS deliveries of a promo SKU since the start -> landed
    if sku_ids and scope_ids:
        cur.execute("""
            SELECT DISTINCT store_id FROM deliveries
            WHERE store_id = ANY(%s) AND sku_id = ANY(%s) AND delivery_date >= %s
        """, (scope_ids, sku_ids, since))
        landed |= {r["store_id"] for r in cur.fetchall()}

    # (2) In-transit loads TAGGED to this promo — classify by DELIVERY DATE so cards
    # update automatically as days pass: on/before today = landed, future = in transit.
    if scope_ids:
        cur.execute("""
            SELECT store_id, delivery_date FROM in_transit_loads
            WHERE store_id = ANY(%s) AND promotion_id = %s
        """, (scope_ids, pid))
        for r in cur.fetchall():
            if r["store_id"] not in scope_set:
                continue
            d = r["delivery_date"]
            if d is not None and d <= today:
                landed.add(r["store_id"])
            else:
                in_transit.add(r["store_id"])

    # (3) Manually-added shipments — same date rule; if no date, fall back to status.
    cur.execute("SELECT store_id, status, received_date FROM promotion_store_shipments WHERE promotion_id = %s", (pid,))
    for r in cur.fetchall():
        if r["store_id"] not in scope_set:
            continue
        d = r["received_date"]
        if d is not None:
            (landed if d <= today else in_transit).add(r["store_id"])
        else:
            (landed if r["status"] == "landed" else in_transit).add(r["store_id"])

    in_transit -= landed
    left = scope_set - landed - in_transit

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


@router.get("/{pid}/stores")
def list_promo_stores(pid: int, user=Depends(get_current_user)):
    """Stores manually added to this promotion (received / in-transit)."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT pss.store_id, pss.status, pss.received_date, pss.added_by, pss.added_at,
                   s.store_name, s.city, s.state, s.division
            FROM promotion_store_shipments pss
            JOIN stores s ON s.store_id = pss.store_id
            WHERE pss.promotion_id = %s
            ORDER BY pss.added_at DESC
        """, (pid,))
        return cur.fetchall()


@router.post("/{pid}/stores", status_code=201)
def add_promo_stores(pid: int, body: AddStoresRequest,
                     user=Depends(require_role("order_writer", "manager", "admin"))):
    """Mark a list of stores as having received (landed) or shipped (in-transit) the
    promo product. Updates the promotion's metrics/cards immediately."""
    status = body.status if body.status in ("landed", "in_transit") else "landed"
    email = user.get("email", "system") if isinstance(user, dict) else "system"
    ids = list({int(s) for s in body.store_ids if s})
    if not ids:
        raise HTTPException(400, "No store ids provided")
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT id FROM promotions WHERE id = %s", (pid,))
        if not cur.fetchone():
            raise HTTPException(404, "Promotion not found")
        cur.execute("SELECT store_id FROM stores WHERE store_id = ANY(%s)", (ids,))
        valid = {r["store_id"] for r in cur.fetchall()}
        not_found = [s for s in ids if s not in valid]
        added = 0
        for s in ids:
            if s not in valid:
                continue
            cur.execute("""
                INSERT INTO promotion_store_shipments (promotion_id, store_id, status, received_date, source, added_by)
                VALUES (%s,%s,%s,%s,'manual',%s)
                ON CONFLICT (promotion_id, store_id) DO UPDATE
                    SET status = EXCLUDED.status, received_date = EXCLUDED.received_date,
                        added_by = EXCLUDED.added_by, added_at = now()
            """, (pid, s, status, body.received_date or None, email))
            added += 1
        return {"added": added, "not_found": not_found, "status": status}


@router.delete("/{pid}/stores/{store_id}")
def remove_promo_store(pid: int, store_id: int,
                       user=Depends(require_role("order_writer", "manager", "admin"))):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("DELETE FROM promotion_store_shipments WHERE promotion_id = %s AND store_id = %s",
                    (pid, store_id))
        return {"deleted": cur.rowcount}


@router.post("/{pid}/stores/upload", status_code=201)
async def upload_promo_stores(pid: int, file: UploadFile = File(...),
                              user=Depends(require_role("order_writer", "manager", "admin"))):
    """
    Upload an Excel/CSV of stores that got this promo's product.
    Columns: Store | Delivery Date. Landed vs In-Transit is deduced automatically
    from the delivery date (past/today = landed, future = in transit) — no manual flag.
    """
    import pathlib
    ext = pathlib.Path(file.filename or "").suffix.lower()
    if ext not in ('.xlsx', '.xls', '.csv'):
        raise HTTPException(400, "Unsupported file type. Use .xlsx or .csv")
    contents = await file.read(20 * 1024 * 1024)

    raw = []
    if ext in ('.xlsx', '.xls'):
        import openpyxl
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        tmp.write(contents); tmp.close()
        try:
            wb = openpyxl.load_workbook(tmp.name, read_only=True, data_only=True)
            raw = list(wb.active.iter_rows(values_only=True))
            wb.close()
        finally:
            os.unlink(tmp.name)
    else:
        import csv
        raw = list(csv.reader(io.StringIO(contents.decode('utf-8-sig', errors='replace'))))
    if not raw:
        raise HTTPException(400, "File is empty")

    hdr_i = 0
    for i, r in enumerate(raw[:5]):
        if r and any(str(v or '').strip() for v in r):
            hdr_i = i; break
    header = [str(v or '').lower().strip() for v in raw[hdr_i]]
    store_c = date_c = None
    for i, h in enumerate(header):
        if 'store' in h and store_c is None: store_c = i
        if ('delivery' in h or 'del date' in h or 'ship date' in h or h == 'date') and date_c is None: date_c = i
    if store_c is None:
        raise HTTPException(400, "Need a 'Store' column.")

    parsed = {}  # store_id -> delivery_date (last wins)
    for r in raw[hdr_i + 1:]:
        if not r or all(v is None for v in r): continue
        try: sid = int(float(str(r[store_c] or '')))
        except (ValueError, TypeError, IndexError): continue
        if sid <= 0: continue
        d = _parse_date(r[date_c]) if (date_c is not None and date_c < len(r)) else None
        parsed[sid] = d
    if not parsed:
        raise HTTPException(400, "No valid store rows found.")

    today = date.today()
    email = user.get("email", "system") if isinstance(user, dict) else "system"
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT id FROM promotions WHERE id = %s", (pid,))
        if not cur.fetchone():
            raise HTTPException(404, "Promotion not found")
        cur.execute("SELECT store_id FROM stores WHERE store_id = ANY(%s)", (list(parsed),))
        valid = {r["store_id"] for r in cur.fetchall()}
        not_found = [s for s in parsed if s not in valid]
        added = landed = in_transit = no_date = 0
        for sid, d in parsed.items():
            if sid not in valid: continue
            # status is deduced from the delivery date (metrics also recompute by date)
            st = 'landed' if (d is not None and d <= today) else 'in_transit'
            if d is None: no_date += 1
            elif d <= today: landed += 1
            else: in_transit += 1
            cur.execute("""
                INSERT INTO promotion_store_shipments (promotion_id, store_id, status, received_date, source, added_by)
                VALUES (%s,%s,%s,%s,'manual',%s)
                ON CONFLICT (promotion_id, store_id) DO UPDATE
                    SET status = EXCLUDED.status, received_date = EXCLUDED.received_date,
                        added_by = EXCLUDED.added_by, added_at = now()
            """, (pid, sid, st, d, email))
            added += 1
        return {"added": added, "landed": landed, "in_transit": in_transit,
                "missing_date": no_date, "not_found": not_found}
