"""
Backfill delivery_manifest with the routed history from every weekly shipping workbook, so
store delivery profiles (workers/store_lead.py) rest on the whole year, not the last few weeks.

Reads each "WK NN" folder's Workbook: one tab per truck load ("47-N1", "47-S3", "47-W2"), each
with Store / Ship Date / Del Date. Those are the tabs the 'Compiled' sheet is stacked from, so
this also covers the weeks that never had a Compiled sheet.

  python backend/ingest/load_delivery_history.py              # dry run: what would be added
  python backend/ingest/load_delivery_history.py --apply      # write
  flags: --weeks 1-40   --year 2026   --shipments "<folder>"

Only ADDS rows: a (week, store, load) already in delivery_manifest is left exactly as it is
(the weekly ingest's Compiled rows and routing imports stay the source of truth for their
weeks). Rows with no delivery date, or dated more than three weeks from their week number
(typos, wrong year), are skipped and counted. New rows carry source = 'workbook_tabs'.
"""
import argparse
import re
import sys
import uuid
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import openpyxl
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import get_conn, Tee

DEFAULT_DIR = (Path.home() / "The Plant Company" / "TPC Sales - Documents"
               / "! Clients - z Big Box" / "Home Depot")
LOAD_RE = re.compile(r"^\s*(\d{1,2})\s*-\s*([NSW])\s*(\d+)\s*([A-Za-z]?)\s*$")


def to_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        s = v.strip()
        for f in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%m.%d.%Y", "%m.%d.%y"):
            try:
                return datetime.strptime(s[:10], f).date()
            except ValueError:
                pass
    return None


def store_id(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() and 0 < v < 100000 else None
    m = re.match(r"^0*(\d{2,5})\b", str(v).strip())
    return int(m.group(1)) if m else None


def hkey(v):
    return re.sub(r"[^a-z]", "", str(v or "").lower())


def read_workbook(path: Path, week: int, year: int):
    """[(store, load, ship, delivery)] + counts of skipped rows, from the load tabs."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    monday = date.fromisocalendar(year, week, 1)
    rows, skipped = [], Counter()
    for name in wb.sheetnames:
        if not LOAD_RE.match(name):
            continue
        it = wb[name].iter_rows(values_only=True)
        hdr = None
        for _ in range(6):
            try:
                r = next(it)
            except StopIteration:
                break
            keys = [hkey(c) for c in r]
            if "store" in keys and ("deldate" in keys or "deliverydate" in keys):
                hdr = keys
                break
        if not hdr:
            skipped["tabs_without_header"] += 1
            continue
        ci = hdr.index("store")
        cs = hdr.index("shipdate") if "shipdate" in hdr else None
        cd = hdr.index("deldate") if "deldate" in hdr else hdr.index("deliverydate")
        for r in it:
            if not r or ci >= len(r):
                continue
            sid = store_id(r[ci])
            if sid is None:
                continue
            dl = to_date(r[cd]) if cd < len(r) else None
            ship = to_date(r[cs]) if cs is not None and cs < len(r) else None
            if dl is None:
                skipped["no_delivery_date"] += 1
                continue
            if abs((dl - monday).days) > 21:
                skipped["date_far_from_week"] += 1
                continue
            if ship and abs((ship - monday).days) > 21:
                ship = None
            rows.append((sid, name.strip(), ship, dl))
    wb.close()
    return rows, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--year", type=int, default=date.today().year)
    ap.add_argument("--weeks", default="1-53")
    ap.add_argument("--shipments")
    a = ap.parse_args()
    lo, hi = (int(x) for x in a.weeks.split("-")) if "-" in a.weeks else (int(a.weeks), int(a.weeks))
    base = Path(a.shipments) if a.shipments else DEFAULT_DIR / str(a.year) / "shipments"
    log = Tee("delivery_history")
    log(f"Delivery history backfill from {base} (weeks {lo}-{hi}, {a.year})")

    found, skipped_all, no_wb = {}, Counter(), []
    for d in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        m = re.match(r"^wk\s*(\d+)$", d.name, flags=re.I)
        if not d.is_dir() or not m or not (lo <= int(m.group(1)) <= hi):
            continue
        wk = int(m.group(1))
        cands = [p for p in d.glob("*.xlsx") if "workbook" in p.name.lower()
                 and "backup" not in p.name.lower() and not p.name.startswith("~$")]
        if not cands:
            no_wb.append(wk)
            continue
        src = max(cands, key=lambda p: p.stat().st_mtime)
        try:
            rows, sk = read_workbook(src, wk, a.year)
        except Exception as e:
            log(f"  WK {wk}: could not read {src.name} ({type(e).__name__}) - skipped")
            no_wb.append(wk)
            continue
        skipped_all.update(sk)
        for sid, load, ship, dl in rows:
            found[(wk, sid, load)] = (ship, dl)          # last tab wins on an exact repeat
        log(f"  WK {wk:>2}: {len(rows):>4} deliveries from {src.name}")

    conn = get_conn()
    if not a.apply:
        conn.set_session(readonly=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT store_id FROM stores")
    known = {r["store_id"] for r in cur.fetchall()}
    cur.execute("SELECT week_number, store_id, load_code FROM delivery_manifest")
    have = {(r["week_number"], r["store_id"], r["load_code"]) for r in cur.fetchall()}
    unknown = sorted({k[1] for k in found if k[1] not in known})
    new = {k: v for k, v in found.items() if k not in have and k[1] in known}
    log(f"input rows {len(found):,} | already in manifest {sum(1 for k in found if k in have):,} "
        f"| unknown stores skipped {sum(1 for k in found if k[1] not in known):,} ({len(unknown)} stores) "
        f"| to add {len(new):,} across {len({k[0] for k in new})} weeks, {len({k[1] for k in new}):,} stores")
    log(f"skipped while reading: {dict(skipped_all)} | week folders with no readable workbook: {no_wb}")
    log(f"manifest rows before {len(have):,} -> after {len(have) + len(new):,}")
    if not a.apply:
        log("DRY RUN - nothing written. Re-run with --apply.")
        return
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO delivery_manifest (id, store_id, ship_date, delivery_date, week_number,
                                       load_code, source, uploaded_at)
        VALUES (%s,%s,%s,%s,%s,%s,'workbook_tabs', now())""",
        [(str(uuid.uuid4()), sid, ship, dl, wk, load) for (wk, sid, load), (ship, dl) in new.items()],
        page_size=500)
    conn.commit()
    cur.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT week_number) AS w FROM delivery_manifest")
    r = cur.fetchone()
    log(f"COMMITTED - added {len(new):,} rows; manifest now {r['n']:,} rows over {r['w']} weeks")


if __name__ == "__main__":
    main()
