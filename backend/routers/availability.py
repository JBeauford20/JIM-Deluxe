"""Availability batch management — upload, validate, list."""
from fastapi import APIRouter, Depends, UploadFile, File, Form
from db import get_db, cursor
from routers.auth import get_current_user, require_role
import tempfile, os, sys

# Workers live in backend/workers/ — deployed with the repo
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'workers'))

router = APIRouter()

@router.get("/batches")
def list_batches(user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT ab.*, COUNT(al.id) AS line_count,
                   SUM(al.units_available) AS total_units
            FROM availability_batches ab
            LEFT JOIN availability_lines al ON al.batch_id = ab.id
            GROUP BY ab.id ORDER BY ab.created_at DESC LIMIT 20
        """)
        return cur.fetchall()

@router.get("/recent-run")
def recent_run(user=Depends(get_current_user)):
    """Return the most recent recommendation run (for auto-loading the UI)."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT rr.id as run_id, rr.shipping_week, rr.status,
                   rr.stores_served, rr.total_carts, rr.total_units,
                   rr.inventory_clearance,
                   ab.id as batch_id, ab.source_filename, ab.shipping_week_start
            FROM recommendation_runs rr
            JOIN availability_batches ab ON ab.id = rr.batch_id
            ORDER BY rr.run_at DESC LIMIT 1
        """)
        return cur.fetchone()


@router.get("/batches/{batch_id}")
def batch_detail(batch_id: str, user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT * FROM availability_batches WHERE id = %s", (batch_id,))
        batch = cur.fetchone()
        if not batch:
            from fastapi import HTTPException
            raise HTTPException(404, "Batch not found")
        cur.execute("""
            SELECT ar.*, k.description, k.family, k.legacy_product_group
            FROM availability_remaining ar
            JOIN skus k ON k.sku_id = ar.sku_id
            WHERE ar.batch_id = %s ORDER BY ar.units_available DESC
        """, (batch_id,))
        return {**batch, "lines": cur.fetchall()}

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
ALLOWED_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "text/csv",
    "application/csv",
    "application/octet-stream",  # some browsers send this for xlsx
}
ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv"}

@router.post("/upload", status_code=201)
async def upload_availability(
    file: UploadFile = File(...),
    shipping_week: str = Form(...),
    user=Depends(require_role("order_writer", "manager", "admin"))
):
    """Accept an Excel availability file and run the loader."""
    from fastapi import HTTPException
    from load_availability import load_availability
    from datetime import date
    import pathlib

    # Validate extension
    ext = pathlib.Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"File type not allowed. Accepted: {ALLOWED_EXTENSIONS}")

    # Read with size cap
    contents = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large. Maximum 20 MB.")

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    try:
        batch_id = load_availability(
            tmp_path,
            shipping_week=date.fromisoformat(shipping_week),
            verbose=False
        )
        return {"batch_id": str(batch_id), "status": "uploaded"}
    finally:
        os.unlink(tmp_path)
