"""
Add stores that ship but are missing from the stores master (2026-11-19: 738, 4425, 6338).

Why it matters: sales/deliveries ingest and the in-transit loader drop rows for unknown
stores (FK to stores), so these stores have DTS snapshot + manifest rows but no sales,
no deliveries and no in-transit lines.

Source: the HD shipment week workbooks (route sheets "NN-X#" and "Stores and Carts"):
Address/City/State/Zip/Division/Merchant/Vol. Newest week wins per field. Region,
region_name, market_number and merch_partner are not in those files and stay NULL.

Idempotent (INSERT ... ON CONFLICT DO NOTHING). Dry-run by default:
  python -I backend/migrations/stores_add_2026_11_19.py --weeks 44 45 47
  python -I backend/migrations/stores_add_2026_11_19.py --weeks 44 45 47 --apply
"""
import argparse
import os
import re
from pathlib import Path

import openpyxl
import psycopg2

SHIPMENTS = Path(os.environ.get("HD_SHIPMENTS_DIR", r"C:\Users\JoshBeauford\The Plant Company\TPC Sales - Documents"
                                r"\! Clients - z Big Box\Home Depot\2026\shipments"))
FIELDS = ("Address", "City", "State", "Zip", "Division", "Merchant", "Vol")


def scan(weeks, wanted):
    found = {s: {} for s in wanted}
    for wk in sorted(weeks):
        folder = SHIPMENTS / f"WK {wk}"
        for p in sorted(folder.glob("*.xlsx")):
            if p.name.startswith("~$") or "Floor" in p.name or "Pack Plan" in p.name:
                continue
            wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
            for ws in wb.worksheets:
                it = ws.iter_rows(values_only=True)
                h = [str(x).strip() if x else "" for x in (next(it, None) or [])]
                if "Store" not in h:
                    continue
                si = h.index("Store")
                for r in it:
                    try:
                        s = int(str(r[si]).strip())
                    except (TypeError, ValueError, IndexError):
                        continue
                    if s not in found:
                        continue
                    for f in FIELDS:
                        if f in h:
                            v = r[h.index(f)]
                            v = str(v).strip() if v not in (None, "") else ""
                            if f == "City" and re.match(r"^\d+-", v):   # S&C "0738-COOKEVILLE" label
                                continue
                            if v:
                                found[s][f] = v
                    found[s].setdefault("_src", set()).add(f"WK{wk}/{p.name}")
            wb.close()
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stores", type=int, nargs="+", default=[738, 4425, 6338])
    ap.add_argument("--weeks", type=int, nargs="+", default=[44, 45, 46, 47])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    if not a.apply:
        conn.set_session(readonly=True)
    cur = conn.cursor()
    cur.execute("SELECT store_id FROM stores WHERE store_id = ANY(%s)", (a.stores,))
    exists = {r[0] for r in cur.fetchall()}
    found = scan(a.weeks, [s for s in a.stores if s not in exists])
    rows = []
    for s in a.stores:
        if s in exists:
            print(f"  {s}: already in stores — skip")
            continue
        f = found.get(s, {})
        if not f.get("City") or not f.get("State"):
            print(f"  {s}: NO source data with city/state in WK {a.weeks} — not added")
            continue
        vol = f.get("Vol") if f.get("Vol") in ("AA", "A", "B", "C", "D") else None
        row = (s, f["City"].title(), f["City"].upper(), f["State"].upper()[:2], vol,
               f.get("Merchant"), f.get("Division"))
        rows.append(row)
        print(f"  {s}: name={row[1]!r} city={row[2]!r} state={row[3]} vol={vol} merchant={row[5]!r} "
              f"division={row[6]!r} addr={f.get('Address')!r} zip={f.get('Zip')!r}  <- {sorted(f['_src'])[:3]}")
    cur.execute("""SELECT store_id, COUNT(*) FROM dts_weekly_snapshot WHERE store_id = ANY(%s)
                   GROUP BY 1""", (a.stores,))
    print("  DTS snapshot rows waiting:", dict(cur.fetchall()))
    if not a.apply:
        print("DRY RUN - nothing written. Re-run with --apply.")
        return
    cur.execute("SET TRANSACTION READ WRITE")
    for r in rows:
        cur.execute("""INSERT INTO stores (store_id, store_name, city, state, volume_code, merchant, division,
                                           active, store_status, status_note, status_updated_at, status_updated_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, true, 'active',
                               'added 2026-11-19 from HD shipment workbooks', now(), 'stores_add_2026_11_19')
                       ON CONFLICT (store_id) DO NOTHING""", r)
    conn.commit()
    print(f"COMMITTED - {len(rows)} stores. Re-run the sales ingest / DTS repair to backfill their history.")


if __name__ == "__main__":
    main()
