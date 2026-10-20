"""
JIM Deluxe — Availability Upload Handler
==========================================
Accepts a weekly availability Excel file and loads it into
availability_batches + availability_lines.

TWO ROUTING MODES — auto-detected from file columns:

  Mode A (preferred): File contains an "HD SKU" column.
    The loader uses the HD SKU directly — no aster_product_mapping lookup
    needed. Holiday SKU exclusion still applies via skus.is_holiday.

  Mode B (fallback): File contains only an Aster code column.
    The loader looks up the Aster code in aster_product_mapping to find
    the HD SKU. Holiday SKUs are excluded by default.

Column header the file needs for Mode A:
  "HD SKU"  (also recognised: "HDSKU", "HD_SKU", "Home Depot SKU")

Usage:
  python load_availability.py "path/to/availability.xlsx"
  python load_availability.py "path/to/availability.xlsx" --week 2026-10-06 --preview
"""

import sys
from pathlib import Path
from datetime import date, timedelta
from collections import defaultdict
import psycopg2, psycopg2.extras, openpyxl

BASE = Path(__file__).parent

# ── Column detection keywords ─────────────────────────────────
HDSKU_KEYWORDS = ['hd sku', 'hd_sku', 'hdsku', 'home depot sku', 'hd skuid']
CODE_KEYWORDS  = ['aster', 'aster code', 'product code', 'item code', 'item #',
                  'item number', 'itemcode', 'productcode']
QTY_KEYWORDS   = ['qty', 'quantity', 'units', 'avail', 'count', 'amount',
                  'available', 'on hand', 'total', 'wk']
DESC_KEYWORDS  = ['desc', 'name', 'item name', 'product name', 'description']
ORIGIN_KEYWORDS = ['origin', 'location', 'source', 'grower', 'cg', 'site']

# Hard-good type derived from SKU family when file drives routing (Mode A)
FAMILY_TO_HARD_GOOD = {
    'H2O Bowl':      'h2o',
    'H2O OPP':       'h2o',
    'H2O Venti':     'h2o',
    'Sm Terrarium':  'terrarium_kit',
    'Med Terrarium': 'terrarium_kit',
    'Lg Terrarium':  'terrarium_kit',
}

from db_conn import get_conn

def find_col(header, keywords):
    """Find best matching column index for a set of keywords."""
    header_lower = [str(h).strip().lower() if h else '' for h in header]
    for kw in keywords:
        for i, h in enumerate(header_lower):
            if h == kw:
                return i
    for kw in keywords:
        for i, h in enumerate(header_lower):
            if kw in h:
                return i
    return None

def detect_header_row(ws):
    for i, row in enumerate(ws.iter_rows(max_row=10, values_only=True)):
        non_empty = [c for c in row if c is not None]
        if not non_empty:
            continue
        if len([c for c in non_empty if isinstance(c, str)]) >= 1:
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
    hdsku_col  = find_col(header, HDSKU_KEYWORDS)
    code_col   = find_col(header, CODE_KEYWORDS)
    qty_col    = find_col(header, QTY_KEYWORDS)
    desc_col   = find_col(header, DESC_KEYWORDS)
    origin_col = find_col(header, ORIGIN_KEYWORDS)

    mode = 'A' if hdsku_col is not None else 'B'
    log(f"  Routing mode: {'A — HD SKU column found, using directly' if mode == 'A' else 'B — Aster code only, using mapping table'}")
    if mode == 'A':
        log(f"    HD SKU col:  col {hdsku_col}  ('{header[hdsku_col]}')")
    if code_col is not None:
        log(f"    Aster col:   col {code_col}  ('{header[code_col]}')")
    log(f"    Quantity:    col {qty_col}   ('{header[qty_col] if qty_col is not None else 'NOT FOUND'}')")

    if qty_col is None:
        raise ValueError(f"Could not find quantity column. Need one of: {QTY_KEYWORDS[:4]}")
    if mode == 'B' and code_col is None:
        raise ValueError(f"No HD SKU column or Aster code column found. Add 'HD SKU' column for direct routing.")

    # ── Parse rows ────────────────────────────────────────────
    raw_lines = []
    skipped   = []

    for row in ws.iter_rows(min_row=header_idx + 2, values_only=True):
        qty_val  = row[qty_col] if qty_col < len(row) else None
        desc_val = row[desc_col] if desc_col is not None and desc_col < len(row) else None
        orig_val = row[origin_col] if origin_col is not None and origin_col < len(row) else None
        quantity = safe_int(qty_val)

        if not quantity or quantity <= 0:
            if any(v is not None for v in row):
                skipped.append(qty_val)
            continue

        entry = {
            'quantity':    quantity,
            'description': str(desc_val).strip() if desc_val else None,
            'origin':      str(orig_val).strip() if orig_val else None,
        }

        if mode == 'A':
            hd_sku = safe_int(row[hdsku_col] if hdsku_col < len(row) else None)
            aster  = safe_int(row[code_col]  if code_col is not None and code_col < len(row) else None)
            if not hd_sku:
                skipped.append(qty_val)
                continue
            entry['hd_sku_id']  = hd_sku
            entry['aster_code'] = str(aster) if aster else None
        else:
            aster = safe_int(row[code_col] if code_col < len(row) else None)
            if not aster:
                skipped.append(qty_val)
                continue
            entry['aster_code'] = str(aster)

        raw_lines.append(entry)

    wb.close()
    log(f"\n  Parsed {len(raw_lines)} lines  ({len(skipped)} skipped)\n")

    # ── Connect and resolve SKUs ──────────────────────────────
    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    matched   = []
    unmatched = []

    if mode == 'A':
        # ── MODE A: HD SKU provided directly ─────────────────
        log("Mode A: Validating HD SKUs against skus table...")
        hd_skus = list({r['hd_sku_id'] for r in raw_lines})
        cur.execute("""
            SELECT sku_id, family, legacy_product_group, is_holiday
            FROM skus
            WHERE sku_id = ANY(%s) AND active = true
        """, (hd_skus,))
        sku_meta = {r['sku_id']: dict(r) for r in cur.fetchall()}

        for line in raw_lines:
            sk = sku_meta.get(line['hd_sku_id'])
            if not sk:
                unmatched.append(line)
                continue
            if sk['is_holiday']:
                log(f"  Skipping holiday SKU {line['hd_sku_id']} ({sk['family']})")
                continue

            # Derive code_level from Aster prefix; default 1M if no Aster code
            aster = line.get('aster_code')
            code_level = '3M' if aster and aster.startswith('3') else '1M'

            # Derive hard_good_type from SKU family
            family = sk['family'] or ''
            hard_good = FAMILY_TO_HARD_GOOD.get(family)
            if hard_good is None:
                hard_good = 'grower_pot' if code_level == '1M' else 'ceramic_kit'

            matched.append({
                **line,
                'hd_sku_id':           line['hd_sku_id'],
                'code_level':          code_level,
                'hard_good_type':      hard_good,
                'legacy_product_group': sk['legacy_product_group'],
                'kit_selection_required': code_level == '1M',
            })

    else:
        # ── MODE B: Aster code lookup (fallback) ─────────────
        log("Mode B: Mapping Aster codes → HD SKUs via mapping table...")
        codes = [r['aster_code'] for r in raw_lines]
        cur.execute("""
            SELECT apm.aster_code, apm.hd_sku_id, apm.aster_name, apm.code_level,
                   apm.hard_good_type, apm.legacy_product_group, sk.is_holiday
            FROM aster_product_mapping apm
            JOIN skus sk ON sk.sku_id = apm.hd_sku_id
            WHERE apm.aster_code = ANY(%s)
              AND apm.active = true
              AND sk.is_holiday = false
            ORDER BY apm.aster_code
        """, (codes,))
        mapping = {}
        for r in cur.fetchall():
            if r['aster_code'] not in mapping:
                mapping[r['aster_code']] = dict(r)

        for line in raw_lines:
            m = mapping.get(line['aster_code'])
            if m:
                matched.append({**line, **m,
                                 'kit_selection_required': m['code_level'] == '1M'})
            else:
                unmatched.append(line)

    log(f"  Matched:   {len(matched)}")
    log(f"  Unmatched: {len(unmatched)}")
    if unmatched:
        log(f"  Unmatched (skipped — no HD SKU or mapping entry):")
        for u in unmatched[:10]:
            log(f"    Aster {u.get('aster_code','?')}  hd={u.get('hd_sku_id','?')}  qty={u['quantity']}")
        if len(unmatched) > 10:
            log(f"    ... and {len(unmatched)-10} more")

    # ── HD SKU rollup for display ──────────────────────────────
    sku_rollup = defaultdict(lambda: {'total': 0, 'lines': []})
    for line in matched:
        sk = line['hd_sku_id']
        sku_rollup[sk]['total']  += line['quantity']
        sku_rollup[sk]['lines'].append(line)

    if sku_rollup:
        cur.execute("SELECT sku_id, description, family FROM skus WHERE sku_id = ANY(%s)",
                    (list(sku_rollup.keys()),))
        sku_names = {r['sku_id']: (r['description'], r['family']) for r in cur.fetchall()}
    else:
        sku_names = {}

    log(f"\n{'-'*60}")
    log(f"AVAILABILITY SUMMARY ({mode}) — {shipping_week}")
    log(f"{'-'*60}")
    log(f"  {'HD SKU':<14} {'Family':<18} {'Description':<28} {'Units':>7}")
    log(f"  {'-'*70}")
    total_units = 0
    for sku_id, r in sorted(sku_rollup.items(), key=lambda x: -x[1]['total']):
        name, family = sku_names.get(sku_id, ('Unknown', ''))
        log(f"  {sku_id:<14} {(family or ''):<18} {(name or '')[:27]:<28} {r['total']:>7,}")
        total_units += r['total']
    log(f"  {'-'*70}")
    log(f"  {'TOTAL':<62} {total_units:>7,}")

    if preview:
        log(f"\nPREVIEW MODE — nothing written to database.")
        cur.close()
        conn.close()
        return sku_rollup

    # ── Write to database ─────────────────────────────────────
    log(f"\nWriting to database...")
    conn.rollback()
    conn.autocommit = False

    cur.execute("""
        INSERT INTO availability_batches
            (upload_date, shipping_week_start, source_filename, status)
        VALUES (%s, %s, %s, 'active')
        RETURNING id
    """, (date.today(), shipping_week, filepath.name))
    batch_id = cur.fetchone()['id']
    log(f"  Batch ID: {batch_id}")

    line_rows = []
    for line in matched:
        is_1m = line['code_level'] == '1M'
        aster  = line.get('aster_code')
        line_rows.append((
            batch_id,
            line['hd_sku_id'],
            line['quantity'],
            aster,                    # plant_id
            None if is_1m else aster, # kit_id
            line['hard_good_type'],
            line['kit_selection_required'],
            line['code_level'],
            line['legacy_product_group'],
            line.get('origin'),
        ))

    # One row per Aster code so the Inventory Draw can show the full variety
    # breakdown. The engine aggregates with SUM(units_available) GROUP BY sku_id
    # so multiple rows per HD SKU cause no issues for allocation.
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO availability_lines
            (batch_id, sku_id, units_available, plant_id, kit_id,
             hard_good_type, kit_selection_required, code_level,
             legacy_product_group, shipping_origin)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, line_rows, page_size=500)

    conn.commit()
    log(f"  {len(line_rows)} availability lines written.")
    log(f"\nUpload complete. Batch {batch_id} — Mode {mode} — {total_units:,} units.")

    cur.close()
    conn.close()
    return batch_id


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("Usage: python load_availability.py <file.xlsx> [--week YYYY-MM-DD] [--preview]")
        sys.exit(1)

    filepath = args[0]
    week_arg = None
    preview  = '--preview' in args

    for i, a in enumerate(args):
        if a == '--week' and i + 1 < len(args):
            week_arg = date.fromisoformat(args[i + 1])

    load_availability(filepath, shipping_week=week_arg, preview=preview)
