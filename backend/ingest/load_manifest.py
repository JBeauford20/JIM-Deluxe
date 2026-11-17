"""
Load a week's shipment manifest (the Compiled sheet) into delivery_manifest.

The engine's in-transit logic reads delivery_manifest (in_transit_loads is
deprecated), so loading this each week is what keeps the DUE gate honest.

Source, in preference order, inside the HD shipments week folder:
  1. "Week {N} Compiled.xlsx"            (standalone, sheet 'Compiled')
  2. "WK {N}-2026 Workbook.xlsx"         (sheet 'Compiled', when present)
Columns: Store | Load | Ship Date | Delivery Date

Natural key: (store_id, load_code). Existing rows are skipped; rows whose
dates changed are updated. Never deletes.

Run:  python backend/ingest/load_manifest.py            (latest week folder)
      python backend/ingest/load_manifest.py --week 46
      python backend/ingest/load_manifest.py --file "path\\to\\Compiled.xlsx" --week 46
      add --dry-run to preview without writing
"""
import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

import openpyxl
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import get_conn, Tee

SHIPMENTS = Path(r"C:\Users\JoshBeauford\The Plant Company\TPC Sales - Documents"
                 r"\! Clients - z Big Box\Home Depot\2026\shipments")


def latest_week() -> int:
    """Newest week folder that actually HAS a Compiled source — a pre-created
    empty WK folder must not hard-fail the Monday ingest."""
    weeks = []
    for p in SHIPMENTS.glob("WK *"):
        m = re.fullmatch(r"WK (\d{1,2})", p.name)
        if m and p.is_dir():
            weeks.append(int(m.group(1)))
    if not weeks:
        raise SystemExit(f"No 'WK NN' folders under {SHIPMENTS}")
    for wk in sorted(weeks, reverse=True):
        try:
            find_source(wk, None)
            return wk
        except SystemExit:
            print(f"  (WK {wk} folder has no Compiled source yet — skipping)")
    raise SystemExit("No week folder contains a Compiled source")


def find_source(week: int, override: str | None) -> tuple[Path, str]:
    if override:
        return Path(override), "Compiled"
    folder = SHIPMENTS / f"WK {week}"
    stand = folder / f"Week {week} Compiled.xlsx"
    if stand.exists():
        return stand, "Compiled"
    wb = folder / f"WK {week}-2026 Workbook.xlsx"
    if wb.exists():
        return wb, "Compiled"
    raise SystemExit(f"No Compiled source found in {folder}")


def to_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(v).strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_rows(path: Path, sheet: str, log) -> list[dict]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    if sheet not in wb.sheetnames:
        raise SystemExit(f"Sheet '{sheet}' not in {path.name} (has {wb.sheetnames})")
    ws = wb[sheet]
    it = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h else "" for h in next(it)]
    try:
        ci = {k: header.index(k) for k in ("Store", "Load", "Delivery Date")}
    except ValueError:
        raise SystemExit(f"Expected headers Store/Load/Delivery Date, got {header}")
    # Ship Date is present in some weeks' Compiled sheets, absent in others
    ci["Ship Date"] = header.index("Ship Date") if "Ship Date" in header else None
    rows, bad = [], 0
    for r in it:
        if r is None or all(v is None for v in r):
            continue
        try:
            store = int(r[ci["Store"]])
        except (TypeError, ValueError):
            bad += 1
            continue
        load = str(r[ci["Load"]]).strip() if r[ci["Load"]] else None
        if not load:
            bad += 1
            continue
        rows.append({
            "store_id": store,
            "load_code": load,
            "ship_date": to_date(r[ci["Ship Date"]]) if ci["Ship Date"] is not None else None,
            "delivery_date": to_date(r[ci["Delivery Date"]]),
        })
    wb.close()
    if bad:
        log(f"  skipped {bad} unparseable rows")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    ap.add_argument("--file")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    log = Tee("manifest")
    week = args.week or latest_week()
    path, sheet = find_source(week, args.file)
    log(f"Week {week}: reading {path.name} [{sheet}]")
    rows = parse_rows(path, sheet, log)
    log(f"  {len(rows)} manifest rows, {len({r['store_id'] for r in rows})} stores, "
        f"{len({r['load_code'] for r in rows})} loads")
    if not rows:
        raise SystemExit("Nothing to load")

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT store_id FROM stores")
    valid = {r["store_id"] for r in cur.fetchall()}
    unknown = sorted({r["store_id"] for r in rows if r["store_id"] not in valid})
    if unknown:
        log(f"  WARNING: {len(unknown)} store ids not in stores table (skipped): {unknown[:10]}")
        rows = [r for r in rows if r["store_id"] in valid]

    cur.execute("""SELECT store_id, load_code, ship_date, delivery_date
                   FROM delivery_manifest WHERE week_number = %s""", (week,))
    existing = {(r["store_id"], r["load_code"]): (r["ship_date"], r["delivery_date"])
                for r in cur.fetchall()}

    inserts, updates, same = [], [], 0
    for r in rows:
        key = (r["store_id"], r["load_code"])
        if key not in existing:
            inserts.append(r)
            continue
        old_ship, old_deliv = existing[key]
        deliv_changed = r["delivery_date"] != old_deliv
        ship_changed = r["ship_date"] is not None and r["ship_date"] != old_ship
        if deliv_changed or ship_changed:
            updates.append(r)
        else:
            same += 1
    log(f"  new: {len(inserts)}  changed-dates: {len(updates)}  unchanged: {same}")

    if args.dry_run:
        log("  DRY RUN — no writes")
        conn.close()
        return

    src = f"compiled_wk{week}"
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO delivery_manifest (id, store_id, ship_date, delivery_date,
                                       week_number, load_code, source, uploaded_at)
        VALUES (gen_random_uuid(), %(store_id)s, %(ship_date)s, %(delivery_date)s,
                %(week)s, %(load_code)s, %(src)s, now())
    """, [{**r, "week": week, "src": src} for r in inserts], page_size=500)
    psycopg2.extras.execute_batch(cur, """
        UPDATE delivery_manifest
        SET ship_date = COALESCE(%(ship_date)s, ship_date),
            delivery_date = %(delivery_date)s,
            source = %(src)s, uploaded_at = now()
        WHERE week_number = %(week)s AND store_id = %(store_id)s AND load_code = %(load_code)s
    """, [{**r, "week": week, "src": src} for r in updates], page_size=500)
    conn.commit()

    cur.execute("SELECT COUNT(*), MAX(delivery_date) FROM delivery_manifest WHERE week_number=%s", (week,))
    n, mx = cur.fetchone()
    log(f"  COMMITTED — week {week} now has {n} rows, max delivery {mx}")
    conn.close()


if __name__ == "__main__":
    main()
