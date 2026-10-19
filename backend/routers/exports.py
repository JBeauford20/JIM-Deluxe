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

        # Pull one row per order line — DISTINCT ON (store_id, order_line_id) ensures
        # each allocation appears exactly once in the CSV so Quantity sums correctly.
        # Preference: 3M kitted codes over 1M, then lowest aster_code for determinism.
        # aster_code will be NULL when no mapping exists — surfaced as a warning, never
        # written as an HD SKU number.
        cur.execute("""
            SELECT DISTINCT ON (o.store_id, ol.id)
                o.store_id                                                      AS store_number,
                apm.aster_code,
                ol.sku_id,
                COALESCE(ol.approved_qty, ol.draft_qty, ol.recommended_qty)    AS quantity,
                ol.kit_type,
                k.description                                                   AS sku_description,
                apm.aster_name,
                apm.code_level
            FROM order_lines ol
            JOIN orders o ON o.id = ol.order_id
            JOIN skus k ON k.sku_id = ol.sku_id
            LEFT JOIN aster_product_mapping apm ON (
                apm.hd_sku_id = ol.sku_id
                AND apm.active = true
                AND (
                    -- 3M match: exact hard_good_type match
                    (ol.kit_type IS NOT NULL AND apm.hard_good_type = ol.kit_type)
                    OR
                    -- 1M match: any 1M code for this SKU covers grower/jute/ceramic
                    (apm.code_level = '1M'
                     AND ol.kit_type IN ('grower_pot', 'jute_kit', 'ceramic_kit', 'mixed'))
                    OR
                    -- Fallback for null kit_type: use any 3M mapping
                    (ol.kit_type IS NULL AND apm.code_level = '3M')
                )
            )
            WHERE o.run_id = %s
              AND o.status = 'approved'
              AND COALESCE(ol.approved_qty, ol.draft_qty, ol.recommended_qty) > 0
            ORDER BY
                o.store_id,
                ol.id,
                apm.code_level DESC NULLS LAST,   -- prefer 3M over 1M
                apm.aster_code   ASC  NULLS LAST  -- lowest code for determinism
        """, (run_id,))
        rows = cur.fetchall()

    if not rows:
        raise HTTPException(404, "No approved order lines with quantity found")

    # Split mapped vs unmapped — never write an HD SKU into the Aster column
    mapped_rows   = [r for r in rows if r["aster_code"] is not None]
    unmapped_skus = {}   # sku_id → {description, kit_type, store_count}
    for r in rows:
        if r["aster_code"] is None:
            sid = r["sku_id"]
            if sid not in unmapped_skus:
                unmapped_skus[sid] = {
                    "description": r["sku_description"],
                    "kit_type":    r["kit_type"] or "",
                    "stores":      set(),
                }
            unmapped_skus[sid]["stores"].add(r["store_number"])

    # Build CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["StoreNumber", "AsterCode", "Quantity", "Description", "KitType"])
    for r in mapped_rows:
        writer.writerow([
            r["store_number"],
            r["aster_code"],
            r["quantity"],
            r["sku_description"],
            r["kit_type"] or "",
        ])

    # Append warning section for unmapped SKUs so they are visible but
    # cannot corrupt the ERP import (rows begin with # so they are skipped
    # by standard CSV parsers but are human-readable)
    if unmapped_skus:
        writer.writerow([])
        writer.writerow(["# UNMAPPED SKUS — no Aster code found, excluded from import above"])
        writer.writerow(["# Add these SKUs to aster_product_mapping to include them in future exports"])
        writer.writerow(["# HD_SKU_ID", "Description", "KitType", "StoresAffected"])
        for sku_id, info in sorted(unmapped_skus.items()):
            writer.writerow([
                f"# {sku_id}",
                info["description"],
                info["kit_type"],
                len(info["stores"]),
            ])

    week = str(run["shipping_week"]).replace("-", "")
    filename = f"JIM_AsterImport_WK{week}_{run_id[:8]}.csv"
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
