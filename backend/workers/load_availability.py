"""
JIM Deluxe — Availability Upload Handler
==========================================
Accepts a weekly availability Excel file and loads it into
availability_batches + availability_lines.

Handles both:
  1M codes  → in-house plants, kit decided at order-writing time
  3M codes  → CG plants, hard good already baked in

Auto-detects column positions so minor header variations don't break it.
Aster code is always a clean numeric key in its own column.

Usage:
  python load_availability.py "path/to/availability.xlsx"
  python load_availability.py "path/to/availability.xlsx" --week 2026-10-06 --preview
"""

import sys, re
from pathlib import Path
from datetime import date, timedelta
from collections import defaultdict
import psycopg2, psycopg2.extras, openpyxl

BASE = Path(__file__).parent
ENV_FILE = BASE / "jim.env"

# ── Column detection keywords ─────────────────────────────────
# Script looks for these in header rows to find the right columns
CODE_KEYWORDS = ['aster', 'product code', 'item code', 'item #', 'item#',
                 'code', 'sku', 'item number', 'itemcode', 'productcode']
QTY_KEYWORDS  = ['qty', 'quantity', 'units', 'avail', 'count', 'amount',
                 'available', 'on hand', 'total', 'wk']  # 'wk' catches HD WK31, HD WK32, etc.
DESC_KEYWORDS = ['desc', 'name', 'item name', 'product name', 'description']
ORIGIN_KEYWORDS = ['origin', 'location', 'source', 'grower', 'cg', 'site']

from db_conn import get_conn

def find_col(header, keywords):
    """Find best matching column index for a set of keywords."""
    header_lower = [str(h).strip().lower() if h else '' for h in header]
    # Exact match first
    for kw in keywords:
        for i, h in enumerate(header_lower):
            if h == kw:
                return i
    # Partial match
    for kw in keywords:
        for i, h in enumerate(header_lower):
            if kw in h:
                return i
    return None

def detect_header_row(ws):
    """Find the row that looks like a header (has text, not all numbers)."""
    for i, row in enumerate(ws.iter_rows(max_row=10, values_only=True)):
        non_empty = [c for c in row if c is not None]
        if not non_empty:
            continue
        text_cells = [c for c in non_empty if isinstance(c, str)]
        if len(text_cells) >= 1:
            return i, list(row)
    return 0, []

def safe_int(val):
    try:
        if val is None: return None
        return int(float(str(val).strip().replace(',', '')))
    except (ValueError, TypeError):
        return None

def load_availability(filepath: str, shipping_week: date = None,
                      preview: bool = False, verbose: bool = True):
    log = print if verbose else lambda *a, **k: None

    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    if shipping_week is None:
        # Default to next Monday
        today = date.today()
        days_ahead = (7 - today.weekday()) % 7 or 7
        shipping_week = today + timedelta(days=days_ahead)

    log(f"\n{'='*60}")
    log(f"Availability Upload — {filepath.name}")
    log(f"Shipping week:  {shipping_week}")
    log(f"Preview mode:   {preview}")
    log(f"{'='*60}\n")

    # ── Parse Excel ───────────────────────────────────────────
    log("Reading Excel file...")
    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)

    # Try each sheet, pick the one with the most data
    best_sheet, best_count = None, 0
    for name in wb.sheetnames:
        ws = wb[name]
        count = sum(1 for _ in ws.iter_rows(min_row=2, max_row=200, values_only=True)
                    if any(c is not None for c in _))
        if count > best_count:
            best_count, best_sheet = count, name

    ws = wb[best_sheet]
    log(f"  Sheet: '{best_sheet}'  ({best_count}+ rows)")

    header_idx, header = detect_header_row(ws)
    code_col  = find_col(header, CODE_KEYWORDS)
    qty_col   = find_col(header, QTY_KEYWORDS)
    desc_col  = find_col(header, DESC_KEYWORDS)
    origin_col = find_col(header, ORIGIN_KEYWORDS)

    log(f"  Columns detected:")
    log(f"    Aster code:  col {code_col}  ('{header[code_col] if code_col is not None else 'NOT FOUND'}')")
    log(f"    Quantity:    col {qty_col}   ('{header[qty_col]  if qty_col  is not None else 'NOT FOUND'}')")
    if desc_col is not None:
        log(f"    Description: col {desc_col}  ('{header[desc_col]}')")
    if origin_col is not None:
        log(f"    Origin:      col {origin_col} ('{header[origin_col]}')")

    if code_col is None or qty_col is None:
        raise ValueError(
            f"Could not find required columns.\n"
            f"Need a column matching: {CODE_KEYWORDS[:4]}\n"
            f"Need a column matching: {QTY_KEYWORDS[:4]}\n"
            f"Header row found: {header}"
        )

    # ── Parse rows ────────────────────────────────────────────
    raw_lines = []
    skipped   = []

    for row in ws.iter_rows(min_row=header_idx + 2, values_only=True):
        code_val = row[code_col] if code_col < len(row) else None
        qty_val  = row[qty_col]  if qty_col  < len(row) else None
        desc_val = row[desc_col] if desc_col is not None and desc_col < len(row) else None
        orig_val = row[origin_col] if origin_col is not None and origin_col < len(row) else None

        aster_code = safe_int(code_val)
        quantity   = safe_int(qty_val)

        # Skip rows with no code, zero, or negative quantity (short/deficit items)
        if not aster_code or not quantity or quantity <= 0:
            if any(v is not None for v in row):
                skipped.append((code_val, qty_val))
            continue

        raw_lines.append({
            'aster_code': str(aster_code),
            'quantity':   quantity,
            'description': str(desc_val).strip() if desc_val else None,
            'origin':      str(orig_val).strip() if orig_val else None,
        })

    wb.close()
    log(f"\n  Parsed {len(raw_lines)} lines  ({len(skipped)} skipped)\n")

    # ── Map Aster codes → HD SKUs ─────────────────────────────
    log("Mapping Aster codes to HD SKUs...")
    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    codes = [r['aster_code'] for r in raw_lines]
    cur.execute("""
        SELECT aster_code, hd_sku_id, aster_name, code_level,
               hard_good_type, legacy_product_group
        FROM aster_product_mapping
        WHERE aster_code = ANY(%s) AND active = true
    """, (codes,))
    mapping = {r['aster_code']: dict(r) for r in cur.fetchall()}

    matched   = []
    unmatched = []
    for line in raw_lines:
        m = mapping.get(line['aster_code'])
        if m:
            matched.append({**line, **m})
        else:
            unmatched.append(line)

    log(f"  Matched:   {len(matched)}")
    log(f"  Unmatched: {len(unmatched)}")
    if unmatched:
        log(f"  Unmatched codes (not in mapping, will be skipped):")
        for u in unmatched[:10]:
            log(f"    {u['aster_code']}  qty={u['quantity']}")

    # ── Build HD SKU rollup for display ───────────────────────
    sku_rollup = defaultdict(lambda: {
        'qty_1m': 0, 'qty_3m_ceramic': 0, 'qty_3m_jute': 0,
        'qty_3m_canopy': 0, 'qty_3m_h2o': 0, 'qty_3m_other': 0,
        'total': 0, 'lines': []
    })

    for line in matched:
        sk = line['hd_sku_id']
        r  = sku_rollup[sk]
        r['total'] += line['quantity']
        r['lines'].append(line)
        if line['code_level'] == '1M':
            r['qty_1m'] += line['quantity']
        elif line['hard_good_type'] == 'ceramic_kit':
            r['qty_3m_ceramic'] += line['quantity']
        elif line['hard_good_type'] == 'jute_kit':
            r['qty_3m_jute'] += line['quantity']
        elif line['hard_good_type'] == 'canopy_kit':
            r['qty_3m_canopy'] += line['quantity']
        elif line['hard_good_type'] == 'h2o':
            r['qty_3m_h2o'] += line['quantity']
        else:
            r['qty_3m_other'] += line['quantity']

    # Fetch HD SKU names for display
    if sku_rollup:
        cur.execute("SELECT sku_id, description FROM skus WHERE sku_id = ANY(%s)",
                    (list(sku_rollup.keys()),))
        sku_names = {r[0]: r[1] for r in cur.fetchall()}
    else:
        sku_names = {}

    log(f"\n{'-'*60}")
    log(f"AVAILABILITY SUMMARY -- {shipping_week}")
    log(f"{'-'*60}")
    log(f"  {'HD SKU':<14} {'Description':<35} {'Total':>7}  Breakdown")
    log(f"  {'-'*14} {'-'*35} {'-'*7}  {'-'*30}")
    total_units = 0
    for sku_id, r in sorted(sku_rollup.items(), key=lambda x: -x[1]['total']):
        name = str(sku_names.get(sku_id, 'Unknown'))[:34]
        parts = []
        if r['qty_1m']:        parts.append(f"{r['qty_1m']} in-house")
        if r['qty_3m_ceramic']:parts.append(f"{r['qty_3m_ceramic']} ceramic(CG)")
        if r['qty_3m_jute']:   parts.append(f"{r['qty_3m_jute']} jute(CG)")
        if r['qty_3m_canopy']: parts.append(f"{r['qty_3m_canopy']} canopy(CG)")
        if r['qty_3m_h2o']:    parts.append(f"{r['qty_3m_h2o']} h2o(CG)")
        if r['qty_3m_other']:  parts.append(f"{r['qty_3m_other']} other(CG)")
        log(f"  {sku_id:<14} {name:<35} {r['total']:>7,}  {' + '.join(parts)}")
        total_units += r['total']
    log(f"  {'-'*60}")
    log(f"  {'TOTAL':<50} {total_units:>7,}")

    if preview:
        log(f"\nPREVIEW MODE — nothing written to database.")
        cur.close()
        conn.close()
        return sku_rollup

    # ── Write to database ─────────────────────────────────────
    log(f"\nWriting to database...")
    conn.rollback()
    conn.autocommit = False

    # Create batch record
    cur.execute("""
        INSERT INTO availability_batches
            (upload_date, shipping_week_start, source_filename, status)
        VALUES (%s, %s, %s, 'active')
        RETURNING id
    """, (date.today(), shipping_week, filepath.name))
    batch_id = cur.fetchone()['id']
    log(f"  Batch ID: {batch_id}")

    # Write availability lines
    line_rows = []
    for line in matched:
        is_1m = line['code_level'] == '1M'
        line_rows.append((
            batch_id,
            line['hd_sku_id'],
            line['quantity'],
            line['aster_code'],                  # plant_id (works for both 1M and 3M)
            None if is_1m else line['aster_code'], # kit_id (only set for 3M)
            line['hard_good_type'],
            is_1m,                               # kit_selection_required
            line['code_level'],
            line['legacy_product_group'],
            line.get('origin'),
        ))

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO availability_lines
            (batch_id, sku_id, units_available, plant_id, kit_id,
             hard_good_type, kit_selection_required, code_level,
             legacy_product_group, shipping_origin)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (batch_id, sku_id) DO UPDATE SET
            units_available       = availability_lines.units_available + EXCLUDED.units_available,
            kit_selection_required = EXCLUDED.kit_selection_required OR availability_lines.kit_selection_required
    """, line_rows, page_size=500)

    conn.commit()
    log(f"  {len(line_rows)} availability lines written.")
    log(f"\nUpload complete. Batch {batch_id} is active for week {shipping_week}.")

    cur.close()
    conn.close()
    return batch_id

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("Usage: python load_availability.py <file.xlsx> [--week YYYY-MM-DD] [--preview]")
        sys.exit(1)

    filepath    = args[0]
    week_arg    = None
    preview     = '--preview' in args

    for i, a in enumerate(args):
        if a == '--week' and i + 1 < len(args):
            week_arg = date.fromisoformat(args[i + 1])

    load_availability(filepath, shipping_week=week_arg, preview=preview)
