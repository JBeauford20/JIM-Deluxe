"""
One-command store status loader (do-not-ship list etc.) from a CSV: store,status,reason

  python backend/ingest/load_store_status.py dns.csv            # dry-run (default): show changes
  python backend/ingest/load_store_status.py dns.csv --apply    # write

status accepts the same values/aliases as the Store Status page ("do not ship",
"do_not_ship", "paused", "hold", "inactive", "active", ...). The engine blocks
do_not_ship / paused / inactive / hold. Only rows whose status or reason differ are
written (idempotent). Unknown stores and invalid statuses are reported and the run
refuses to apply until they are fixed (or --skip-bad is given). Before writing, the
current status of every touched store is saved to backups/store_status_<ts>.csv.
"""
import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

import psycopg2

VALID = {'active', 'paused', 'do_not_ship', 'inactive', 'opening', 'promotion', 'store_walk',
         'fenceline', 'hold'}
ALIASES = {'do not ship': 'do_not_ship', 'dns': 'do_not_ship', 'pause': 'paused',
           'store walk': 'store_walk', 'fence line': 'fenceline'}


def norm_status(v):
    v = (v or "").strip().lower()
    v = ALIASES.get(v, v.replace(" ", "_"))
    return v if v in VALID else None


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        cols = {c.lower().strip(): c for c in rd.fieldnames or []}
        for need in ("store", "status"):
            if need not in cols:
                raise SystemExit(f"CSV needs columns store,status[,reason]; got {rd.fieldnames}")
        out, bad = [], []
        for i, r in enumerate(rd, 2):
            raw = (r[cols["store"]] or "").strip()
            st = norm_status(r[cols["status"]])
            try:
                sid = int(float(raw))
            except ValueError:
                bad.append(f"line {i}: store '{raw}'")
                continue
            if not st:
                bad.append(f"line {i}: status '{r[cols['status']]}'")
                continue
            out.append((sid, st, (r.get(cols.get("reason", ""), "") or "").strip() or None))
    return out, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--skip-bad", action="store_true", help="apply valid rows even if some are bad")
    ap.add_argument("--by", default="load_store_status.py")
    a = ap.parse_args()
    rows, bad = read_csv(a.csv)
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    if not a.apply:
        conn.set_session(readonly=True)
    cur = conn.cursor()
    cur.execute("SELECT store_id, store_status, status_note FROM stores WHERE store_id = ANY(%s)",
                ([r[0] for r in rows],))
    cur_st = {s: (st, n) for s, st, n in cur.fetchall()}
    unknown = sorted({r[0] for r in rows if r[0] not in cur_st})
    bad += [f"store {s} not in stores master" for s in unknown]
    changes = [r for r in rows if r[0] in cur_st and (cur_st[r[0]][0], cur_st[r[0]][1]) != (r[1], r[2])]
    print(f"rows {len(rows)} | changes {len(changes)} | unchanged {len(rows) - len(changes) - len(unknown)} "
          f"| problems {len(bad)}")
    for s, st, why in changes:
        print(f"  {s}: {cur_st[s][0]} -> {st}  ({why or ''})")
    for b in bad:
        print(f"  PROBLEM {b}")
    if not a.apply:
        print("DRY RUN - nothing written. Re-run with --apply.")
        return
    if bad and not a.skip_bad:
        raise SystemExit("Refusing to apply with problems (fix the CSV or pass --skip-bad).")
    Path("backups").mkdir(exist_ok=True)
    bfile = Path("backups") / f"store_status_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(bfile, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["store", "status", "reason"])
        for s, _, _ in changes:
            w.writerow([s, cur_st[s][0], cur_st[s][1] or ""])
    print(f"backup (restore with this script) -> {bfile.resolve()}")
    for s, st, why in changes:
        cur.execute("""UPDATE stores SET store_status = %s, status_note = %s, status_updated_at = now(),
                              status_updated_by = %s WHERE store_id = %s""", (st, why, a.by, s))
    conn.commit()
    print(f"COMMITTED - {len(changes)} stores updated")


if __name__ == "__main__":
    main()
