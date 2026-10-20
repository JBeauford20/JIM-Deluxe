"""
In-Transit Loads — stores that received product whose scan data is not yet in the DTS system.
Bridges the ~1-week lag between a shipment going out and the Store/SKU/Scan report reflecting it.

Upload columns expected: Store | Vol | Sum of Carts  (from the dock manifest InTransit export)
"""
import io, os, tempfile
import psycopg2.extras
from datetime import date, timedelta
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from db import get_db, cursor
from routers.auth import get_current_user, require_role

router = APIRouter()


def _detect_col(header: list, keywords: list):
    h = [str(v or '').lower().strip() for v in header]
    for kw in keywords:
        for i, col in enumerate(h):
            if kw == col or kw in col:
                return i
    return None


def _parse_rows(raw_rows: list) -> list:
    """Return list of (store_id, vol_code, cart_count) from raw Excel/CSV rows."""
    if not raw_rows:
        return []
    # Find header row (first row with text)
    header_idx = 0
    for i, r in enumerate(raw_rows[:5]):
        if r and any(str(v or '').strip() for v in r):
            header_idx = i
            break
    header = raw_rows[header_idx]

    store_col = _detect_col(header, ['store', 'store_id', 'storeidnum', 'store #', 'store#'])
    vol_col   = _detect_col(header, ['vol', 'volume', 'vol code', 'volume code'])
    cart_col  = _detect_col(header, ['sum of carts', 'carts', 'cart_count', 'sum of cart'])

    if store_col is None or cart_col is None:
        raise ValueError("Could not find Store and Carts columns. "
                         "Expected headers: Store, Vol, Sum of Carts")

    parsed = []
    for r in raw_rows[header_idx + 1:]:
        if not r or all(v is None for v in r):
            continue
        try:
            store_id = int(float(str(r[store_col] or '')))
        except (ValueError, TypeError, IndexError):
            continue
        try:
            carts = int(float(str(r[cart_col] or 0)))
        except (ValueError, TypeError, IndexError):
            continue
        vol = str(r[vol_col]).strip() if vol_col is not None and vol_col < len(r) and r[vol_col] else ''
        if store_id > 0 and carts > 0:
            parsed.append((store_id, vol, carts))
    return parsed


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/")
def list_in_transit(user=Depends(get_current_user)):
    """All current in-transit records with store details."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT itl.store_id, itl.shipping_week, itl.cart_count, itl.vol_code,
                   itl.uploaded_at,
                   s.store_name, s.city, s.state,
                   TRIM(s.dynamic_velocity_tier) AS tier
            FROM in_transit_loads itl
            JOIN stores s ON s.store_id = itl.store_id
            WHERE itl.shipping_week >= CURRENT_DATE - INTERVAL '60 days'
            ORDER BY itl.shipping_week DESC, itl.store_id
        """)
        return cur.fetchall()


@router.get("/summary")
def in_transit_summary(user=Depends(get_current_user)):
    """Summary by shipping week — count and total carts."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT shipping_week,
                   COUNT(*)          AS store_count,
                   SUM(cart_count)   AS total_carts,
                   MAX(uploaded_at)  AS last_upload
            FROM in_transit_loads
            WHERE shipping_week >= CURRENT_DATE - INTERVAL '60 days'
            GROUP BY shipping_week
            ORDER BY shipping_week DESC
        """)
        return cur.fetchall()


@router.post("/upload", status_code=201)
async def upload_in_transit(
    file: UploadFile = File(...),
    shipping_week: str = Form(None),
    user=Depends(require_role("order_writer", "manager", "admin"))
):
    """
    Upload In-Transit stores report (Excel or CSV).
    Expected columns: Store | Vol | Sum of Carts
    Replaces all existing records for the specified shipping_week.
    """
    import pathlib
    ext = pathlib.Path(file.filename or "").suffix.lower()
    if ext not in ('.xlsx', '.xls', '.csv'):
        raise HTTPException(400, "Unsupported file type. Use .xlsx or .csv")

    contents = await file.read(20 * 1024 * 1024)

    # Parse file
    raw_rows = []
    if ext in ('.xlsx', '.xls'):
        import openpyxl
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        tmp.write(contents); tmp.close()
        try:
            wb = openpyxl.load_workbook(tmp.name, read_only=True, data_only=True)
            raw_rows = list(wb.active.iter_rows(values_only=True))
            wb.close()
        finally:
            os.unlink(tmp.name)
    else:
        import csv, io as _io
        reader = csv.reader(_io.StringIO(contents.decode('utf-8-sig', errors='replace')))
        raw_rows = list(reader)

    try:
        parsed = _parse_rows(raw_rows)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not parsed:
        raise HTTPException(400, "No valid store rows found.")

    # Resolve shipping week
    if shipping_week:
        try:
            week = date.fromisoformat(shipping_week)
        except ValueError:
            raise HTTPException(400, "shipping_week must be YYYY-MM-DD")
    else:
        today = date.today()
        week = today - timedelta(days=today.weekday())  # Monday of current week

    # Validate store IDs exist
    with get_db() as conn:
        cur = cursor(conn)
        store_ids = [s for s, _, _ in parsed]
        cur.execute("SELECT store_id FROM stores WHERE store_id = ANY(%s)", (store_ids,))
        valid = {r["store_id"] for r in cur.fetchall()}
        filtered = [(s, v, c) for s, v, c in parsed if s in valid]
        skipped = len(parsed) - len(filtered)

        # Replace this week's data
        cur.execute("DELETE FROM in_transit_loads WHERE shipping_week = %s", (week,))
        deleted = cur.rowcount

        if filtered:
            email = user.get("email", "system") if isinstance(user, dict) else "system"
            psycopg2.extras.execute_values(cur, """
                INSERT INTO in_transit_loads
                    (store_id, shipping_week, cart_count, vol_code, uploaded_by)
                VALUES %s
                ON CONFLICT (store_id, shipping_week) DO UPDATE
                    SET cart_count  = EXCLUDED.cart_count,
                        vol_code    = EXCLUDED.vol_code,
                        uploaded_by = EXCLUDED.uploaded_by,
                        uploaded_at = now()
            """, [(s, week, c, v, email) for s, v, c in filtered],
                page_size=500)

    return {
        "shipping_week": str(week),
        "stores_loaded": len(filtered),
        "stores_skipped": skipped,
        "prior_records_replaced": deleted,
    }


@router.delete("/{shipping_week}")
def clear_week(
    shipping_week: str,
    user=Depends(require_role("manager", "admin"))
):
    """Delete all in-transit records for a given shipping week."""
    try:
        week = date.fromisoformat(shipping_week)
    except ValueError:
        raise HTTPException(400, "Date must be YYYY-MM-DD")
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("DELETE FROM in_transit_loads WHERE shipping_week = %s", (week,))
        return {"deleted": cur.rowcount, "shipping_week": str(week)}
