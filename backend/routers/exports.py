"""
Export router — generates Aster ERP import files from approved orders.
Format: Store, Item (Aster 3M code), Quantity
"""
import io, csv
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from db import get_db, cursor
from routers.auth import require_role

router = APIRouter()

@router.get("/runs/{run_id}/aster-import")
def export_aster(
    run_id: str,
    user=Depends(require_role("manager", "admin"))
):
    """
    Export approved orders for a run as Aster ERP import CSV.
    Columns: StoreNumber, AsterCode, Quantity
    Only approved orders; only lines with approved_qty set.
    """
    with get_db() as conn:
        cur = cursor(conn)

        # Confirm run exists and has approved orders
        cur.execute("""
            SELECT rr.shipping_week, COUNT(o.id) AS approved_count
            FROM recommendation_runs rr
            JOIN orders o ON o.run_id = rr.id AND o.status = 'approved'
            WHERE rr.id = %s
            GROUP BY rr.shipping_week
        """, (run_id,))
        run = cur.fetchone()
        if not run:
            raise HTTPException(404, "No approved orders found for this run")

        # Pull all approved order lines with their Aster codes
        cur.execute("""
            SELECT
                o.store_id                          AS store_number,
                COALESCE(
                    apm.aster_code,
                    CAST(ol.sku_id AS text)
                )                                   AS aster_code,
                COALESCE(ol.approved_qty, ol.draft_qty, ol.recommended_qty) AS quantity,
                ol.kit_type,
                k.description                       AS sku_description,
                apm.aster_name,
                apm.code_level
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            JOIN skus k ON k.sku_id = ol.sku_id
            LEFT JOIN aster_product_mapping apm ON (
                apm.hd_sku_id = ol.sku_id
                AND apm.active = true
                AND (
                    -- Match on kit_type if specified
                    (ol.kit_type IS NOT NULL AND apm.hard_good_type = ol.kit_type)
                    OR
                    -- Fall back to default (first active mapping for this SKU)
                    (ol.kit_type IS NULL AND apm.code_level = '3M')
                )
            )
            WHERE o.run_id = %s
              AND o.status = 'approved'
              AND COALESCE(ol.approved_qty, ol.draft_qty, ol.recommended_qty) > 0
            ORDER BY o.store_id, k.description
        """, (run_id,))
        rows = cur.fetchall()

    if not rows:
        raise HTTPException(404, "No approved order lines with quantity found")

    # Build CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["StoreNumber", "AsterCode", "Quantity", "Description", "KitType"])
    for r in rows:
        writer.writerow([
            r["store_number"],
            r["aster_code"],
            r["quantity"],
            r["sku_description"],
            r["kit_type"] or "",
        ])

    week = str(run["shipping_week"]).replace("-", "")
    filename = f"JIM_AsterImport_WK{week}_{run_id[:8]}.csv"
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
