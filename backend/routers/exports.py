"""
Export router — generates Aster ERP import files and Cart Config Excel exports.
"""
import io, csv, math
from collections import defaultdict
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from db import get_db, cursor
from routers.auth import require_role, get_current_user

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


# ── CART CONFIG EXCEL EXPORT ──────────────────────────────────────────────────

H2O_FAMILIES = {"H2O Bowl", "H2O OPP", "H2O Venti"}
LABEL_REQ    = "Retail/SKU/UPC/PBS/Genus/Size"
COL_HEADERS  = [
    "Cart Key", "# of carts", "Shelf #", "Size",
    "Product", "Items", "Aster Code",
    "Qty per display", "Total QT",
    "Upgrade", "Tray", "Tags",
    "SKU Label", "UPC Label", "Retail",
    "Label Requirements:", "Notes:"
]


def _distribute_trays(total_trays, aster_list):
    """
    Spread total_trays across (aster_code, units_available) pairs
    proportionally using the largest-remainder method so the sum
    always equals total_trays exactly.
    """
    if not aster_list or total_trays <= 0:
        return []
    total_units = sum(u for _, u in aster_list)
    if total_units == 0:
        base = total_trays // len(aster_list)
        return [(code, base) for code, _ in aster_list]

    raw      = [(code, total_trays * units / total_units)
                for code, units in aster_list]
    floors   = [(code, int(r), r - int(r)) for code, r in raw]
    used     = sum(f for _, f, _ in floors)
    remainder = total_trays - used
    # Give the leftover trays to the entries with the largest fractional parts
    sorted_by_frac = sorted(range(len(floors)), key=lambda i: -floors[i][2])
    result = {code: f for code, f, _ in floors}
    for i in range(remainder):
        code = floors[sorted_by_frac[i]][0]
        result[code] += 1

    return [(code, result[code]) for code, _ in aster_list]


@router.get("/runs/{run_id}/cart-config-export")
def cart_config_export(run_id: str, user=Depends(get_current_user)):
    """
    Generate Excel cart config export in the standard TPC format:
    one block per shelf per HD SKU group, with one tray-row per Aster variety.
    H2O SKUs use a single HD-SKU-level row (no variety breakdown).
    """
    try:
        import openpyxl
    except ImportError:
        raise HTTPException(503, "openpyxl not installed")

    with get_db() as conn:
        cur = cursor(conn)

        # Run metadata
        cur.execute(
            "SELECT batch_id, shipping_week FROM recommendation_runs WHERE id = %s",
            (run_id,)
        )
        run_row = cur.fetchone()
        if not run_row:
            raise HTTPException(404, "Run not found")
        batch_id     = run_row["batch_id"]
        shipping_week = run_row["shipping_week"]

        # Variety reference: aster_code → (genus, variety)
        cur.execute(
            "SELECT aster_code, parsed_genus, parsed_variety FROM variety_parsed_reference"
        )
        variety_ref = {
            r["aster_code"]: (r["parsed_genus"] or "", r["parsed_variety"] or "")
            for r in cur.fetchall()
        }

        # Pricebook: sku_id → (upc, retail)
        cur.execute("SELECT sku_id, upc, retail_price FROM pricebook_hd")
        pricebook = {r["sku_id"]: (r["upc"], r["retail_price"]) for r in cur.fetchall()}

        # Availability pool: sku_id → [(aster_code_int, units), ...] sorted by units desc
        cur.execute("""
            SELECT sku_id, plant_id::bigint AS aster_code, units_available
            FROM availability_lines
            WHERE batch_id = %s AND plant_id IS NOT NULL
            ORDER BY sku_id, units_available DESC
        """, (batch_id,))
        aster_pool = defaultdict(list)
        for r in cur.fetchall():
            aster_pool[r["sku_id"]].append((r["aster_code"], r["units_available"]))

        # Packing profiles
        cur.execute(
            "SELECT id, units_per_tray FROM packing_profiles WHERE active = true"
        )
        profiles_upt = {r["id"]: r["units_per_tray"] or 1 for r in cur.fetchall()}

        # Cart configs: one representative cart + count per config code
        cur.execute("""
            SELECT oc.config_code,
                   MIN(oc.id)        AS cart_id,
                   COUNT(oc.id)      AS cart_count,
                   MIN(oc.cart_key)  AS cart_key
            FROM order_carts oc
            JOIN orders o ON o.id = oc.order_id
            WHERE o.run_id = %s
            GROUP BY oc.config_code
            ORDER BY oc.config_code
        """, (run_id,))
        configs = cur.fetchall()
        if not configs:
            raise HTTPException(404, "No cart configurations found")

        # Shelves for each representative cart
        cart_ids = [c["cart_id"] for c in configs]
        cur.execute("""
            SELECT cs.cart_id, cs.shelf_position, cs.sku_id,
                   cs.profile_id, cs.tray_count, cs.unit_count, cs.kit_type,
                   sk.family, sk.description
            FROM cart_shelves cs
            JOIN skus sk ON sk.sku_id = cs.sku_id
            WHERE cs.cart_id = ANY(%s)
            ORDER BY cs.cart_id, cs.shelf_position DESC, cs.sku_id
        """, (cart_ids,))
        all_shelves = cur.fetchall()

    # Index shelves by cart_id
    shelves_by_cart = defaultdict(list)
    for s in all_shelves:
        shelves_by_cart[s["cart_id"]].append(s)

    # ── Build workbook ────────────────────────────────────────────────────────
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cart Configurations"

    for cfg in configs:
        code       = cfg["config_code"] or "CC?"
        cart_count = cfg["cart_count"]
        cart_key   = cfg["cart_key"] or code
        cart_id    = cfg["cart_id"]
        shelves    = shelves_by_cart.get(cart_id, [])

        # Group by shelf position (already sorted DESC = top → bottom)
        shelf_groups = defaultdict(list)
        for s in shelves:
            shelf_groups[s["shelf_position"]].append(s)

        for pos in sorted(shelf_groups.keys(), reverse=True):
            for shelf_row in shelf_groups[pos]:
                sku_id     = shelf_row["sku_id"]
                family     = shelf_row["family"] or ""
                desc       = shelf_row["description"] or ""
                profile_id = shelf_row["profile_id"] or ""
                tray_count = shelf_row["tray_count"] or 0
                unit_count = shelf_row["unit_count"] or 0

                upt       = profiles_upt.get(profile_id, 1)
                tray_lbl  = f"{upt} pack tray"
                upc, retail = pricebook.get(sku_id, (None, None))

                # Write block header
                ws.append(COL_HEADERS)

                if family in H2O_FAMILIES:
                    # H2O → single row at HD SKU level
                    h2o_items = family.replace("H2O ", "")
                    ws.append([
                        code, cart_count, pos, family,
                        "H2O", h2o_items,
                        sku_id, unit_count, unit_count,
                        "", tray_lbl, "PW",
                        sku_id, upc, retail, LABEL_REQ, None
                    ])
                    ws.append([None]*7 + [unit_count, unit_count] + [None]*8)

                else:
                    asters = aster_pool.get(sku_id, [])
                    if not asters:
                        # Fallback — no batch data, show HD SKU row
                        ws.append([
                            code, cart_count, pos, family,
                            desc, desc,
                            sku_id, unit_count, unit_count,
                            "", tray_lbl, "PW",
                            sku_id, upc, retail, LABEL_REQ, None
                        ])
                        ws.append([None]*7 + [unit_count, unit_count] + [None]*8)
                    else:
                        distribution = _distribute_trays(tray_count, asters)
                        shelf_total  = 0
                        for aster_code, aster_trays in distribution:
                            if aster_trays <= 0:
                                continue
                            qty = aster_trays * upt
                            shelf_total += qty
                            genus, variety = variety_ref.get(aster_code, ("", ""))
                            if not genus and not variety:
                                genus   = family
                                variety = str(aster_code)
                            ws.append([
                                code, cart_count, pos, family,
                                genus, variety,
                                aster_code, qty, qty,
                                "", tray_lbl, "PW",
                                sku_id, upc, retail, LABEL_REQ, None
                            ])
                        ws.append([None]*7 + [shelf_total, shelf_total] + [None]*8)

    # Stream xlsx
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    wk  = str(shipping_week).replace("-", "")
    fname = f"JIM_CartConfigs_WK{wk}_{run_id[:8]}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )
