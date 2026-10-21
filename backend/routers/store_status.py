"""
Store Status — track per-store shipping status overrides.

Valid statuses:
  active        — normal (default; no special handling)
  paused        — hard exclude from allocation
  do_not_ship   — hard exclude from allocation
  inactive      — hard exclude from allocation
  opening       — new / reopening store: +0.30 priority bonus
  promotion     — in-store event: +0.15 priority bonus
  store_walk    — informational; no engine effect
  fenceline     — informational; no engine effect
  hold          — same as paused (hard exclude)
"""
import io, os, tempfile
import psycopg2.extras
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from pydantic import BaseModel

from db import get_db, cursor
from routers.auth import get_current_user, require_role

router = APIRouter()

VALID_STATUSES = {
    'active', 'paused', 'do_not_ship', 'inactive',
    'opening', 'promotion', 'store_walk', 'fenceline', 'hold',
}

# Human-readable aliases accepted in uploaded files
STATUS_ALIASES = {
    'promotion':   'promotion',
    'do not ship': 'do_not_ship',
    'store walk':  'store_walk',
    'pause':       'paused',
    'paused':      'paused',
    'active':      'active',
    'fenceline':   'fenceline',
    'fence line':  'fenceline',
    'hold':        'hold',
    'opening':     'opening',
    'inactive':    'inactive',
    'do_not_ship': 'do_not_ship',
    'store_walk':  'store_walk',
}


VALID_US_STATES = {
    'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA',
    'KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
    'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT',
    'VA','WA','WV','WI','WY','DC',
}

VALID_VOLUME_CODES = {'AA', 'A', 'B', 'C', 'D'}


class StatusUpdate(BaseModel):
    status: str
    note: Optional[str] = None


class BulkStatusItem(BaseModel):
    store_id: int
    status: str
    note: Optional[str] = None


class NewStoreRequest(BaseModel):
    store_id:    int
    store_name:  str
    city:        str
    state:       str
    merchant:    str
    volume_code: str


class EditStoreRequest(BaseModel):
    store_name:   Optional[str] = None
    merchant:     Optional[str] = None
    volume_code:  Optional[str] = None
    store_status: Optional[str] = None
    status_note:  Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/")
def list_non_active(user=Depends(get_current_user)):
    """All stores that have a non-active status."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT store_id, store_name, city, state, merchant,
                   store_status, status_note,
                   status_updated_at, status_updated_by
            FROM stores
            WHERE store_status != 'active'
            ORDER BY store_status, store_name
        """)
        return cur.fetchall()


@router.patch("/{store_id}")
def update_store_status(
    store_id: int,
    body: StatusUpdate,
    user=Depends(require_role("order_writer", "manager", "admin"))
):
    """Update a single store's status."""
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status '{body.status}'. Valid: {sorted(VALID_STATUSES)}")

    email = user.get("email", "system") if isinstance(user, dict) else "system"
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            UPDATE stores
               SET store_status       = %s,
                   status_note        = %s,
                   status_updated_at  = now(),
                   status_updated_by  = %s
             WHERE store_id = %s
             RETURNING store_id, store_status, status_note
        """, (body.status, body.note, email, store_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"Store {store_id} not found")
        return row


@router.post("/bulk", status_code=200)
def bulk_update_status(
    items: List[BulkStatusItem],
    user=Depends(require_role("order_writer", "manager", "admin"))
):
    """Bulk-update store statuses from a JSON array."""
    invalid_statuses = [i.status for i in items if i.status not in VALID_STATUSES]
    if invalid_statuses:
        raise HTTPException(400, f"Invalid statuses: {invalid_statuses}. Valid: {sorted(VALID_STATUSES)}")

    email = user.get("email", "system") if isinstance(user, dict) else "system"
    with get_db() as conn:
        cur = cursor(conn)
        # Validate all store IDs up front
        ids = [i.store_id for i in items]
        cur.execute("SELECT store_id FROM stores WHERE store_id = ANY(%s)", (ids,))
        valid_ids = {r['store_id'] for r in cur.fetchall()}
        skipped = [i.store_id for i in items if i.store_id not in valid_ids]

        rows = [(i.status, i.note, email, i.store_id)
                for i in items if i.store_id in valid_ids]
        for status, note, em, sid in rows:
            cur.execute("""
                UPDATE stores
                   SET store_status       = %s,
                       status_note        = %s,
                       status_updated_at  = now(),
                       status_updated_by  = %s
                 WHERE store_id = %s
            """, (status, note, em, sid))

    return {"updated": len(rows), "skipped": len(skipped), "skipped_ids": skipped}


@router.post("/upload", status_code=200)
async def upload_status_file(
    file: UploadFile = File(...),
    status_code: Optional[str] = Form(None),
    user=Depends(require_role("order_writer", "manager", "admin"))
):
    """
    Upload an Excel or CSV file to bulk-set store statuses.

    Expected columns: Store Number | Status Code
    Status Code may use human-readable aliases (e.g. "Do Not Ship", "Promotion").
    If the optional status_code form field is provided it overrides the file column.
    """
    import pathlib
    ext = pathlib.Path(file.filename or "").suffix.lower()
    if ext not in ('.xlsx', '.xls', '.csv'):
        raise HTTPException(400, "Unsupported file type. Use .xlsx or .csv")

    contents = await file.read(20 * 1024 * 1024)

    # ── Parse file ────────────────────────────────────────────────
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
        import csv
        reader = csv.reader(io.StringIO(contents.decode('utf-8-sig', errors='replace')))
        raw_rows = list(reader)

    if not raw_rows:
        raise HTTPException(400, "File is empty")

    # Find header row
    header_idx = 0
    for i, r in enumerate(raw_rows[:5]):
        if r and any(str(v or '').strip() for v in r):
            header_idx = i
            break
    header = [str(v or '').lower().strip() for v in raw_rows[header_idx]]

    # Detect columns
    store_col = None
    status_col = None
    for i, h in enumerate(header):
        if 'store' in h and store_col is None:
            store_col = i
        if ('status' in h or 'code' in h) and status_col is None:
            status_col = i

    if store_col is None:
        raise HTTPException(400, "Could not find 'Store Number' column.")

    # ── Parse rows ────────────────────────────────────────────────
    parsed = []          # (store_id, resolved_status, note)
    invalid_status_vals = []

    for r in raw_rows[header_idx + 1:]:
        if not r or all(v is None for v in r):
            continue
        try:
            sid = int(float(str(r[store_col] or '')))
        except (ValueError, TypeError, IndexError):
            continue
        if sid <= 0:
            continue

        # Determine status
        if status_code:
            raw_st = status_code
        elif status_col is not None and status_col < len(r) and r[status_col]:
            raw_st = str(r[status_col]).strip()
        else:
            raw_st = 'active'

        resolved = STATUS_ALIASES.get(raw_st.lower().strip())
        if resolved is None:
            if raw_st not in invalid_status_vals:
                invalid_status_vals.append(raw_st)
            continue

        parsed.append((sid, resolved))

    if not parsed:
        raise HTTPException(400, "No valid rows found in file.")

    # ── Apply updates ─────────────────────────────────────────────
    email = user.get("email", "system") if isinstance(user, dict) else "system"
    with get_db() as conn:
        cur = cursor(conn)
        ids = [s for s, _ in parsed]
        cur.execute("SELECT store_id FROM stores WHERE store_id = ANY(%s)", (ids,))
        valid_ids = {r['store_id'] for r in cur.fetchall()}
        skipped = len(parsed) - len([s for s, _ in parsed if s in valid_ids])

        updated = 0
        for sid, st in parsed:
            if sid not in valid_ids:
                continue
            cur.execute("""
                UPDATE stores
                   SET store_status      = %s,
                       status_updated_at = now(),
                       status_updated_by = %s
                 WHERE store_id = %s
            """, (st, email, sid))
            updated += 1

    return {
        "updated": updated,
        "skipped": skipped,
        "invalid_status": invalid_status_vals,
    }


@router.get("/valid-merchants")
def valid_merchants(user=Depends(get_current_user)):
    """Sorted list of distinct non-null merchant values from the stores table."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT DISTINCT merchant FROM stores
            WHERE merchant IS NOT NULL AND merchant != ''
            ORDER BY merchant
        """)
        return [r["merchant"] for r in cur.fetchall()]


@router.post("/add-store", status_code=201)
def add_store(
    body: NewStoreRequest,
    user=Depends(require_role("manager", "admin"))
):
    """Add a new store with Opening status."""
    # Validate store_name
    store_name = body.store_name.strip()
    if not store_name:
        raise HTTPException(400, "store_name must not be empty")

    # Validate city
    city = body.city.strip()
    if not city:
        raise HTTPException(400, "city must not be empty")

    # Validate state
    state = body.state.upper().strip()
    if state not in VALID_US_STATES:
        raise HTTPException(400, "State must be a valid 2-letter US abbreviation")

    # Validate volume_code
    if body.volume_code not in VALID_VOLUME_CODES:
        raise HTTPException(400, f"Volume code must be one of: AA, A, B, C, D")

    with get_db() as conn:
        cur = cursor(conn)

        # Check for duplicate store_id
        cur.execute("SELECT 1 FROM stores WHERE store_id = %s", (body.store_id,))
        if cur.fetchone():
            raise HTTPException(409, f"Store {body.store_id} already exists")

        # Validate merchant against DB values
        cur.execute("""
            SELECT DISTINCT merchant FROM stores
            WHERE merchant IS NOT NULL AND merchant != ''
            ORDER BY merchant
        """)
        valid_merchants = [r["merchant"] for r in cur.fetchall()]
        if body.merchant not in valid_merchants:
            raise HTTPException(400, f"Invalid merchant. Valid merchants: {valid_merchants}")

        # Insert new store
        cur.execute("""
            INSERT INTO stores (store_id, store_name, city, state, merchant, volume_code,
                                store_status, active, dynamic_velocity_tier, dynamic_velocity_score)
            VALUES (%s, %s, %s, %s, %s, %s, 'opening', true, %s, 0.0)
        """, (body.store_id, store_name, city, state, body.merchant, body.volume_code, body.volume_code))

    return {
        "store_id": body.store_id,
        "store_name": store_name,
        "status": "opening",
        "message": "Store added successfully. Status set to Opening — will be prioritized as AA until sales data accumulates.",
    }


@router.put("/{store_id}/edit")
def edit_store(
    store_id: int,
    body: EditStoreRequest,
    user=Depends(require_role("manager", "admin"))
):
    """Update name, merchant, volume_code, status, and/or note on an existing store."""
    # Validate fields if provided
    if body.store_name is not None:
        body.store_name = body.store_name.strip()
        if not body.store_name:
            raise HTTPException(400, "store_name must not be empty")

    if body.volume_code is not None:
        if body.volume_code not in VALID_VOLUME_CODES:
            raise HTTPException(400, f"volume_code must be one of: AA, A, B, C, D")

    if body.store_status is not None:
        if body.store_status not in VALID_STATUSES:
            raise HTTPException(400, f"store_status must be one of: {sorted(VALID_STATUSES)}")

    email = user.get("email", "system") if isinstance(user, dict) else "system"

    with get_db() as conn:
        cur = cursor(conn)

        # Check store exists
        cur.execute("SELECT store_id FROM stores WHERE store_id = %s", (store_id,))
        if not cur.fetchone():
            raise HTTPException(404, f"Store {store_id} not found")

        # Validate merchant against DB values if provided
        if body.merchant is not None:
            cur.execute("""
                SELECT DISTINCT merchant FROM stores
                WHERE merchant IS NOT NULL AND merchant != ''
                ORDER BY merchant
            """)
            valid_merchants_list = [r["merchant"] for r in cur.fetchall()]
            if body.merchant not in valid_merchants_list:
                raise HTTPException(400, f"Invalid merchant. Valid merchants: {valid_merchants_list}")

        # Build dynamic SET clause — only update what was provided
        set_parts = []
        params = []

        if body.store_name is not None:
            set_parts.append("store_name = %s")
            params.append(body.store_name)

        if body.merchant is not None:
            set_parts.append("merchant = %s")
            params.append(body.merchant)

        if body.volume_code is not None:
            set_parts.append("volume_code = %s")
            params.append(body.volume_code)
            # Keep dynamic_velocity_tier in sync for manually-managed stores
            set_parts.append("dynamic_velocity_tier = %s")
            params.append(body.volume_code)

        if body.store_status is not None:
            set_parts.append("store_status = %s")
            params.append(body.store_status)

        if body.status_note is not None:
            set_parts.append("status_note = %s")
            params.append(body.status_note)

        # Always stamp who/when
        set_parts.append("status_updated_at = now()")
        set_parts.append("status_updated_by = %s")
        params.append(email)

        params.append(store_id)
        cur.execute(
            f"UPDATE stores SET {', '.join(set_parts)} WHERE store_id = %s "
            "RETURNING store_id, store_name, city, state, merchant, volume_code, "
            "store_status, status_note, status_updated_at, status_updated_by",
            params
        )
        row = cur.fetchone()

    return row
