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

# Column widths (A=1 … Q=17)
_COL_WIDTHS = [8, 10, 8, 20, 18, 22, 12, 16, 12, 10, 14, 6, 14, 14, 8, 28, 12]
# Bold columns in data rows: E=5, F=6, G=7
_DATA_BOLD_COLS = {5, 6, 7}


def _style_header(ws):
    """Last appended row → all cells bold + centered."""
    from openpyxl.styles import Font, Alignment
    for cell in ws[ws.max_row]:
        cell.font      = Font(bold=True)
        cell.alignment = Alignment(horizontal='center')


def _style_data(ws):
    """Last appended row → E/F/G bold+centered, rest centered."""
    from openpyxl.styles import Font, Alignment
    for i, cell in enumerate(ws[ws.max_row], 1):
        cell.alignment = Alignment(horizontal='center')
        if i in _DATA_BOLD_COLS:
            cell.font = Font(bold=True)


def _style_subtotal(ws):
    """Last appended row → only columns H(8) and I(9) bold+centered."""
    from openpyxl.styles import Font, Alignment
    for i, cell in enumerate(ws[ws.max_row], 1):
        if i in (8, 9):
            cell.font      = Font(bold=True)
            cell.alignment = Alignment(horizontal='center')


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

        # Availability pool: sku_id → [(aster_code_text, units), ...] sorted by units desc
        # plant_id is stored as text — keep as-is, convert to int for variety_ref lookup later
        cur.execute("""
            SELECT sku_id, plant_id AS aster_code, units_available
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

        # Aggregate actual totals across ALL carts per config+shelf+SKU.
        # Using representative-cart × cart_count was wrong: after Jaccard
        # config consolidation many carts share a code but don't all carry
        # every SKU — the representative cart may have a SKU that only a
        # fraction of the config's carts actually received.
        cur.execute("""
            SELECT
                oc.config_code,
                cs.shelf_position,
                cs.sku_id,
                cs.profile_id,
                cs.kit_type,
                sk.family,
                sk.description,
                SUM(cs.unit_count)          AS actual_total_units,
                cc.cart_count,
                cc.cart_key,
                -- Divide by COUNT(*) = carts that actually carry this SKU,
                -- not cc.cart_count which is all carts with this config code.
                -- e.g. CC1=173 carts but only 27 have 17cm → 1134÷27=42, not 1134÷173=7
                ROUND(SUM(cs.unit_count)::numeric / NULLIF(COUNT(*), 0), 0)
                                            AS avg_unit_count
            FROM cart_shelves cs
            JOIN order_carts oc ON oc.id = cs.cart_id
            JOIN orders o       ON o.id  = oc.order_id
            JOIN skus sk        ON sk.sku_id = cs.sku_id
            JOIN (
                SELECT oc2.config_code,
                       COUNT(*)                                           AS cart_count,
                       (array_agg(oc2.cart_key ORDER BY oc2.cart_key))[1] AS cart_key
                FROM order_carts oc2
                JOIN orders o2 ON o2.id = oc2.order_id
                WHERE o2.run_id = %s
                GROUP BY oc2.config_code
            ) cc ON cc.config_code = oc.config_code
            WHERE o.run_id = %s
            GROUP BY oc.config_code, cs.shelf_position, cs.sku_id,
                     cs.profile_id, cs.kit_type, sk.family, sk.description,
                     cc.cart_count, cc.cart_key
            ORDER BY oc.config_code, cs.shelf_position DESC, cs.sku_id
        """, (run_id, run_id))
        all_shelves = cur.fetchall()

    # Index by config_code
    shelves_by_config = defaultdict(list)
    for s in all_shelves:
        shelves_by_config[s["config_code"]].append(s)

    # Unique ordered config codes
    seen_codes = []
    for s in all_shelves:
        if s["config_code"] not in seen_codes:
            seen_codes.append(s["config_code"])

    # ── Build workbook ────────────────────────────────────────────────────────
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cart Configurations"

    # Set column widths to match template proportions
    from openpyxl.utils import get_column_letter
    for i, w in enumerate(_COL_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    for code in seen_codes:
        shelves    = shelves_by_config.get(code, [])
        if not shelves:
            continue
        cart_count = int(shelves[0]["cart_count"] or 1)
        cart_key   = shelves[0]["cart_key"] or code

        shelf_groups = defaultdict(list)
        for s in shelves:
            shelf_groups[s["shelf_position"]].append(s)

        for pos in sorted(shelf_groups.keys(), reverse=True):
            sku_rows = shelf_groups[pos]

            # ONE header per shelf position — all SKUs at this position share it
            ws.append(COL_HEADERS)
            _style_header(ws)

            shelf_total_disp = 0
            shelf_total_qt   = 0

            for shelf_row in sku_rows:
                sku_id     = shelf_row["sku_id"]
                family     = shelf_row["family"] or ""
                desc       = shelf_row["description"] or ""
                profile_id = shelf_row["profile_id"] or ""
                unit_count      = int(shelf_row["avg_unit_count"] or 0)
                actual_total    = int(shelf_row["actual_total_units"] or 0)

                upt      = profiles_upt.get(profile_id, 1)
                tray_lbl = f"{upt} pack tray"
                upc, retail = pricebook.get(sku_id, (None, None))

                if family in H2O_FAMILIES:
                    h2o_items = family.replace("H2O ", "")
                    ws.append([
                        code, cart_count, pos, family,
                        "H2O", h2o_items,
                        sku_id, unit_count, actual_total,
                        "", tray_lbl, "PW",
                        sku_id, upc, retail, LABEL_REQ, None
                    ])
                    _style_data(ws)
                    shelf_total_disp += unit_count
                    shelf_total_qt   += actual_total

                else:
                    asters = aster_pool.get(sku_id, [])
                    if not asters:
                        ws.append([
                            code, cart_count, pos, family,
                            desc, desc,
                            sku_id, unit_count, actual_total,
                            "", tray_lbl, "PW",
                            sku_id, upc, retail, LABEL_REQ, None
                        ])
                        _style_data(ws)
                        shelf_total_disp += unit_count
                        shelf_total_qt   += actual_total
                    else:
                        total_sku_avail = sum(u for _, u in asters) or 1

                        # Largest-remainder on actual_total for Total QT
                        raw_tot   = [(ac, actual_total * u / total_sku_avail) for ac, u in asters]
                        fl_tot    = [(ac, int(r), r - int(r)) for ac, r in raw_tot]
                        used_tot  = sum(f for _, f, _ in fl_tot)
                        rem_tot   = actual_total - used_tot
                        by_frac_t = sorted(range(len(fl_tot)), key=lambda i: -fl_tot[i][2])
                        qt_map    = {ac: f for ac, f, _ in fl_tot}
                        for i in range(rem_tot):
                            qt_map[fl_tot[by_frac_t[i]][0]] += 1

                        # Largest-remainder on unit_count for Qty per display
                        raw_d    = [(ac, unit_count * u / total_sku_avail) for ac, u in asters]
                        fl_d     = [(ac, int(r), r - int(r)) for ac, r in raw_d]
                        used_d   = sum(f for _, f, _ in fl_d)
                        rem_d    = unit_count - used_d
                        by_frac_d = sorted(range(len(fl_d)), key=lambda i: -fl_d[i][2])
                        disp_map  = {ac: f for ac, f, _ in fl_d}
                        for i in range(rem_d):
                            disp_map[fl_d[by_frac_d[i]][0]] += 1

                        for aster_code, aster_avail in asters:
                            total_qt = qt_map.get(aster_code, 0)
                            qty      = disp_map.get(aster_code, 0)
                            if total_qt <= 0 and qty <= 0:
                                continue
                            shelf_total_disp += qty
                            shelf_total_qt   += total_qt
                            try:
                                aster_int = int(aster_code)
                            except (ValueError, TypeError):
                                aster_int = 0
                            genus, variety = variety_ref.get(aster_int, ("", ""))
                            if not genus and not variety:
                                genus   = family
                                variety = str(aster_code)
                            ws.append([
                                code, cart_count, pos, family,
                                genus, variety,
                                aster_code, qty, total_qt,
                                "", tray_lbl, "PW",
                                sku_id, upc, retail, LABEL_REQ, None
                            ])
                            _style_data(ws)

            # ONE subtotal for the entire shelf position (all SKUs combined)
            ws.append([None]*7 + [shelf_total_disp, shelf_total_qt] + [None]*8)
            _style_subtotal(ws)

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
