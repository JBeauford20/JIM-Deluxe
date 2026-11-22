"""
Load the Home Depot store master (first sheet of the "Home Depot Store List" workbook).

  python backend/ingest/load_store_master.py "Home Depot Store List 11 20 2026.xlsx"           # dry run
  python backend/ingest/load_store_master.py "<file>.xlsx" --apply                              # write
  python backend/ingest/load_store_master.py "<file>.xlsx" --apply --update-descriptive         # also take
        the sheet's merchant / region / city where the app already has a different value

Needs backend/migrations/store_master_2026_11_22.py applied first. Dry run is the default and
opens the database read-only. Before writing, the complete before/after change list is saved
to backups/store_master_<timestamp>.json (and to the store_master_loads table).
What the load does and does not touch: services/store_master.py.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import store_master  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--update-descriptive", action="store_true")
    ap.add_argument("--by", default="load_store_master.py")
    ap.add_argument("--json", help="write the full plan (incl. every change) to this file")
    a = ap.parse_args()

    parsed = store_master.parse_workbook(Path(a.xlsx).read_bytes(), Path(a.xlsx).name)
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=25)
    if not a.apply:
        conn.set_session(readonly=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    db, have = store_master.load_db_stores(cur)
    pl = store_master.plan(parsed, db, update_descriptive=a.update_descriptive)
    qa = pl["qa"]
    print(f"file: {pl['file']} | sheet read: '{pl['sheet']}' | other tabs ignored: {len(pl['sheets_ignored'])}")
    print(f"rows in {qa['input_rows']} | accepted {qa['rows_accepted']} | rejected {qa['rows_rejected']} "
          f"| matched {qa['matched']} | sheet-not-in-app {qa['unmatched_sheet_not_in_app']} "
          f"| app-not-in-sheet {qa['app_stores_not_in_sheet']} | duplicates {qa['duplicate_keys']}")
    print(f"stores changed {qa['stores_changed']} | field changes {qa['field_changes']} "
          f"| by field {json.dumps(qa['changes_by_field'])}")
    print("before:", json.dumps(qa["before"]))
    print("after: ", json.dumps(qa["after"]))
    for s in pl["status_changes"]:
        print(f"  STATUS {s['store_id']} {s['store']} {s['state']}: {s['from']} -> {s['to']}")
    for s in pl["not_in_sheet"]:
        print(f"  NOT IN SHEET (untouched): {s['store_id']} {s['store_name']} {s['state']}")
    for s in pl["not_in_app"]:
        print(f"  NOT IN APP (skipped): {s['store_id']} {s['facility_name']} {s['state']}")
    if pl["conflicts_total"]:
        print(f"descriptive conflicts kept as-is: {pl['conflicts_total']} (top below; "
              f"--update-descriptive takes the sheet's value)")
        for c in pl["conflicts_summary"][:15]:
            print(f"  {c['field']}: app '{c['app']}' vs sheet '{c['sheet']}' x {c['stores']}")
    inf, unres = pl["merchandiser_inferred"], pl["merchandiser_unresolved"]
    print(f"merchandiser filled from neighbouring stores: {len(inf)} | still blank: {len(unres)}")
    for x in inf:
        print(f"  INFERRED {x['store_id']}: {x['merchandiser']} ({x['basis']})")
    for x in unres:
        print(f"  UNRESOLVED {x['store_id']} {x['state']} {x['market']}: market has "
              f"{x['market_merchandisers'] or 'none'}; district has {x['district_merchandisers'] or 'none'}")
    for p in pl["problems"]:
        print(f"  PROBLEM {p}")
    if a.json:
        Path(a.json).write_text(json.dumps(pl, indent=1, default=str), encoding="utf-8")
    if not store_master.migration_applied(have):
        print("NOTE: store-master columns are not in the database yet — run "
              "backend/migrations/store_master_2026_11_22.py before --apply.")
    if not a.apply:
        print("DRY RUN - nothing written. Re-run with --apply.")
        return
    if pl["problems"]:
        raise SystemExit("Refusing to apply with problems — fix the sheet (or the rule definitions).")
    if not store_master.migration_applied(have):
        raise SystemExit("Refusing to apply: migration not applied.")
    Path("backups").mkdir(exist_ok=True)
    bfile = Path("backups") / f"store_master_{datetime.now():%Y%m%d_%H%M%S}.json"
    bfile.write_text(json.dumps({"file": pl["file"], "qa": qa, "changes": pl["changes"]},
                                indent=1, default=str), encoding="utf-8")
    print(f"backup (before/after of every change) -> {bfile.resolve()}")
    res = store_master.apply(cur, pl, a.by)
    conn.commit()
    print(f"COMMITTED - {res['stores_changed']} stores, {res['field_changes']} field changes "
          f"(load {res['load_id']})")


if __name__ == "__main__":
    main()
