"""Floor sheets: one workbook per route, one tab per cart build (guide section 9; replaces the
pack team's build_floor_sheets.py for engine-written runs). Pure Python + openpyxl, no DB.

Layout per tab: header block (WEEK / CUSTOMER / SHIP DATE / CART / ROUTE + the NO SUN sign
box), then each shelf TOP SHELF FIRST: column headers, item rows, subtotal. Size / UPC /
Retail are plain values (no external formulas). Product / Items come from the Aster variety
reference; an "Assorted" name is never printed (Items left blank and listed for the catalog).
Mixed rows keep their own cart count, get a colour block, and count once in the subtotal.
"""
import io
import re
import zipfile
from collections import defaultdict

FS_HEADERS = ["# of carts", "Shelf #", "Size", "Product", "Items", "Aster Code", "Qty per display",
              "Total QT", "Upgrade", "Tray", "Tags", "SKU Label", "UPC Label", "Retail",
              "Label Requirements", "Notes"]
FS_WIDTHS = [9, 7, 16, 18, 34, 11, 10, 10, 9, 13, 8, 12, 15, 9, 16, 30]
MIX_FILLS = ["FFF2CC", "DDEBF7", "E2EFDA", "FCE4D6", "EDE7F6", "FFE6EE"]
GLOBE_SKU = 1015258015            # Climb-itt Globe (3 per tray, 6 pack tray)


def kit_cart_count(krows):
    """Carts of one route x kit: a whole row's KitQuantity, or the sum of one mixed slot's rows."""
    whole = [int(r["KitQuantity"] or 0) for r in krows if "Mix" not in str(r.get("Notes") or "")]
    if whole:
        return max(whole)
    first = min(krows, key=lambda r: (int(r["Layer"]), str(r.get("Notes"))))
    m = re.match(r".*Mix\s+([A-Z])", str(first.get("Notes")))
    tag = f"Mix {m.group(1)}" if m else ""
    return sum(int(r["KitQuantity"] or 0) for r in krows
               if int(r["Layer"]) == int(first["Layer"]) and tag in str(r.get("Notes")))


def _cc_num(name):
    m = re.match(r"\s*CC\s*(\d+)", str(name or ""), re.I)
    return int(m.group(1)) if m else None


def tab_order_key(kit):
    """Holiday AA..D first, then 'CC n' by n, then everything else by name."""
    k = str(kit or "")
    hol = re.match(r"\s*Holiday\s+(AA|A|B|C|D)\b", k, re.I)
    if hol:
        return (0, ["AA", "A", "B", "C", "D"].index(hol.group(1).upper()), k)
    n = _cc_num(k)
    if n is not None:
        return (1, n, k)
    return (2, 0, k)


def tray_label(sku_id, size, trays, eaches, profile=None):
    s = str(size or "").lower()
    if int(sku_id) == GLOBE_SKU:
        return "6 pack tray"
    if profile == "box8x2" or "dish" in s:
        return "2 pack tray"
    if "17cm" in s:
        return "3 pack tray"
    if "12cm" in s:
        return "6 pack tray"
    if trays:
        return f"{int(eaches // trays)} pack tray"
    return ""


def product_items(code, ref, flagged):
    """(Product, Items) from the Aster variety reference; never print an 'Assorted' name."""
    g, v = (ref.get("aster_parsed") or {}).get(str(code or ""), (None, None))
    if not g and not v:
        nm = (ref.get("aster_names") or {}).get(str(code or ""), "") or ""
        parts = re.sub(r"^\d+cm\s+", "", nm).split()
        g, v = (parts[0] if parts else ""), " ".join(parts[1:])
        v = re.sub(r"\s+\d+cm\b.*$", "", v)
    if "assorted" in f"{g} {v}".lower():
        flagged.add(str(code))
        return (g if (g or "").lower() != "assorted" else ""), ""
    return g or "", v or ""


def build_route_workbook(route, rows, ref, week, ship_date, flagged):
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    skus = ref.get("skus") or {}
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    thin = Side(style="thin", color="999999")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    bold = Font(bold=True)
    by_kit = defaultdict(list)
    for r in rows:
        by_kit[r["KitDescription"]].append(r)
    used = set()
    for kit in sorted(by_kit, key=tab_order_key):
        krows = by_kit[kit]
        title = re.sub(r"[\[\]\*\?/\\:]", "", str(kit))[:31] or "Cart"
        base, i = title, 2
        while title in used:
            title = f"{base[:28]}~{i}"
            i += 1
        used.add(title)
        ws = wb.create_sheet(title)
        n_carts = kit_cart_count(krows)
        hdr = [("WEEK", week), ("CUSTOMER", "Home Depot"), ("SHIP DATE", ship_date), ("CART", kit),
               ("ROUTE", route), ("# OF CARTS", n_carts)]
        for j, (k, v) in enumerate(hdr, 1):
            ws.cell(j, 1, k).font = bold
            c = ws.cell(j, 2, v)
            if k == "SHIP DATE" and v is not None:
                c.number_format = "mm/dd/yyyy"
        ns = ws.cell(1, 5, "Include NO SUN sign")
        ns.font = Font(bold=True, color="FFFFFF")
        ns.fill = PatternFill("solid", fgColor="E75480")
        ns.alignment = Alignment(horizontal="center")
        ws.merge_cells(start_row=1, start_column=5, end_row=2, end_column=8)
        r0 = len(hdr) + 2
        if any("SOIL" in str(x.get("ItemDescription") or "").upper() for x in krows):
            ws.cell(r0 - 1, 5, "MUST INCLUDE SOIL PO").font = bold
        by_layer = defaultdict(list)
        for r in krows:
            by_layer[int(r["Layer"])].append(r)
        rr = r0
        for layer in sorted(by_layer, reverse=True):              # top shelf first
            for j, h in enumerate(FS_HEADERS, 1):
                c = ws.cell(rr, j, h)
                c.font = bold
                c.fill = PatternFill("solid", fgColor="D9D9D9")
                c.border = box
            rr += 1
            mix_seen, per_display, total = {}, 0, 0
            for r in sorted(by_layer[layer], key=lambda x: (str(x.get("Notes") or ""), str(x.get("AsterCode") or ""))):
                sku = int(r["SKU"])
                info = skus.get(sku) or {}
                size = info.get("size_category") or ""
                prod, items = product_items(r.get("AsterCode"), ref, flagged)
                note = r.get("Notes") or ""
                m = re.match(r".*Mix\s+([A-Z])", str(note))
                vals = [int(r["KitQuantity"] or 0), layer, size, prod, items, r.get("AsterCode"),
                        int(r["Eaches"] or 0), int(r["KitQuantity"] or 0) * int(r["Eaches"] or 0),
                        r.get("Upgrade"), tray_label(sku, size, int(r["Trays"] or 0), int(r["Eaches"] or 0)),
                        None, str(sku), info.get("upc"), info.get("retail_price"), None, note or None]
                for j, v in enumerate(vals, 1):
                    c = ws.cell(rr, j, v)
                    c.border = box
                    if m:
                        c.fill = PatternFill("solid", fgColor=MIX_FILLS[(ord(m.group(1)) - 65) % len(MIX_FILLS)])
                ws.cell(rr, 14).number_format = '"$"#,##0.00'
                if m:
                    mix_seen[m.group(1)] = max(mix_seen.get(m.group(1), 0), int(r["Eaches"] or 0))
                else:
                    per_display += int(r["Eaches"] or 0)
                total += vals[7]
                rr += 1
            per_display += sum(mix_seen.values())                 # each mixed slot counts once
            ws.cell(rr, 6, "Shelf total").font = bold
            ws.cell(rr, 7, per_display).font = bold
            ws.cell(rr, 8, total).font = bold
            rr += 2
        for j, w in enumerate(FS_WIDTHS, 1):
            ws.column_dimensions[get_column_letter(j)].width = w
    return wb


def build_floor_sheets(rows, ref, week):
    """(zip bytes, report): one 'Week NN Floor Sheets <route>.xlsx' per routed route."""
    by_route = defaultdict(list)
    for r in rows:
        by_route[r["Route"]].append(r)
    flagged, files = set(), []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for route in sorted(by_route, key=str):
            if route in (None, "UNROUTED"):
                continue
            rs = by_route[route]
            ship = next((r["ShipDate"] for r in rs if r.get("ShipDate")), None)
            wb = build_route_workbook(route, rs, ref, week, ship, flagged)
            b = io.BytesIO()
            wb.save(b)
            name = f"Week {week} Floor Sheets {route}.xlsx"
            z.writestr(name, b.getvalue())
            files.append(name)
    return buf.getvalue(), {"files": files, "routes": len(files),
                            "unrouted_rows": sum(1 for r in rows if r["Route"] in (None, "UNROUTED")),
                            "assorted_codes_for_catalog": sorted(flagged)}
