"""
SKU master fixes for the first IPR v2 live run (2026-11-19).

1. Seasonal SKUs sitting in routine ipr_families (Fall, Halloween, Mother's Day, Spring,
   holiday-specific) move to ipr_family 'Seasonal/Holiday', so their past-season sales stop
   inflating routine family velocity/grades and they can be excluded from routine
   replenishment. Rule: family is not already Holiday/Seasonal AND (skus.is_holiday OR the
   description names a season). The existing 'Holiday' family is NOT touched (open question).
   Unmapped SKUs with a seasonal description are mapped too; unmapped SKUs with no
   description are listed as candidates only.
   Also writes ipr_settings.excluded_families = ["Seasonal/Holiday"] (engine gate pending).
2. SKU 1013310817 "Littles in Ceramics" (PW LEAFJOY CERAMIC 9CM) -> packing profile 'cer9'
   (8 per tray) as its default instead of 'littles' (12 per tray).

Backs up every row it changes to backups/sku_master_fixes_<timestamp>.json before writing.
Idempotent. Dry-run by default:
  python backend/migrations/sku_master_fixes_2026_11_19.py
  python backend/migrations/sku_master_fixes_2026_11_19.py --apply
"""
import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

import psycopg2

TARGET = "Seasonal/Holiday"
KEEP = {"Holiday", TARGET}
SEASON_RX = re.compile(r"\b(fall|halloween|mother'?s day|spring|easter|valentine|christmas|holiday|"
                       r"thanksgiving|harvest)\b", re.I)
CER_SKU, CER_PROFILE = 1013310817, "cer9"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    if not a.apply:
        conn.set_session(readonly=True)
    cur = conn.cursor()

    cur.execute("""SELECT k.sku_id, k.description, k.is_holiday, fm.ipr_family, fm.approved, fm.source
                   FROM skus k JOIN ipr_family_map fm USING (sku_id) ORDER BY fm.ipr_family, k.sku_id""")
    moves, candidates, backup = [], [], {"ipr_family_map": [], "sku_packing_profiles": []}
    for sku, desc, hol, fam, appr, src in cur.fetchall():
        if fam in KEEP:
            continue
        named = bool(SEASON_RX.search(desc or ""))
        if named or hol:
            why = "description" if named else "skus.is_holiday"
            moves.append((sku, desc, fam, why))
            backup["ipr_family_map"].append(dict(sku_id=sku, ipr_family=fam, approved=appr, source=src))
        elif fam == "Unmapped" and (desc or "").startswith("SKU "):
            candidates.append((sku, desc))
    print(f"1. Seasonal remap -> '{TARGET}': {len(moves)} SKUs")
    for sku, desc, fam, why in moves:
        print(f"   {sku} {desc.strip()!r:<48} {fam:<16} ({why})")
    if candidates:
        print("   not moved (no description; confirm): " + ", ".join(str(s) for s, _ in candidates))

    cur.execute("""SELECT spp.id, spp.profile_id, spp.is_default FROM sku_packing_profiles spp
                   WHERE spp.sku_id = %s ORDER BY spp.is_default DESC, spp.profile_id""", (CER_SKU,))
    spp = cur.fetchall()
    cur.execute("SELECT units_per_tray FROM packing_profiles WHERE id = %s", (CER_PROFILE,))
    upt = cur.fetchone()
    if not upt:
        raise SystemExit(f"packing profile {CER_PROFILE} missing")
    print(f"2. SKU {CER_SKU}: profiles now {[(p, d) for _, p, d in spp]} -> default '{CER_PROFILE}' "
          f"({upt[0]}/tray)")
    backup["sku_packing_profiles"] = [dict(id=str(i), profile_id=p, is_default=d) for i, p, d in spp]
    cer_done = any(p == CER_PROFILE and d for _, p, d in spp) and sum(1 for _, _, d in spp if d) == 1
    if cer_done:
        print("   already default — no change")

    if not a.apply:
        print("DRY RUN - nothing written. Re-run with --apply.")
        return

    bdir = Path("backups")
    bdir.mkdir(exist_ok=True)
    bfile = bdir / f"sku_master_fixes_{datetime.now():%Y%m%d_%H%M%S}.json"
    bfile.write_text(json.dumps(backup, indent=1, default=str))
    print(f"backup -> {bfile.resolve()}")
    cur.execute("SET TRANSACTION READ WRITE")
    for sku, _desc, fam, why in moves:
        cur.execute("""UPDATE ipr_family_map SET ipr_family = %s, approved = true, updated_at = now(),
                              source = %s WHERE sku_id = %s""",
                    (TARGET, f"seasonal remap from '{fam}' ({why}) - Josh 2026-11-19", sku))
    cur.execute("""INSERT INTO ipr_settings (key, value, spec_ref, status, note)
                   VALUES ('excluded_families', %s::jsonb, '§4.5', 'Josh 2026-11-19',
                           'Families excluded from routine replenishment (seasonal / off-cycle).')
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                (json.dumps([TARGET]),))
    if not cer_done:
        if any(p == CER_PROFILE for _, p, _ in spp):
            cur.execute("UPDATE sku_packing_profiles SET is_default = (profile_id = %s) WHERE sku_id = %s",
                        (CER_PROFILE, CER_SKU))
        else:
            cur.execute("UPDATE sku_packing_profiles SET is_default = false WHERE sku_id = %s", (CER_SKU,))
            cur.execute("""INSERT INTO sku_packing_profiles (id, sku_id, profile_id, is_default, created_at)
                           VALUES (gen_random_uuid(), %s, %s, true, now())""", (CER_SKU, CER_PROFILE))
        cur.execute("UPDATE skus SET units_per_tray = %s, updated_at = now() WHERE sku_id = %s",
                    (upt[0], CER_SKU))
    conn.commit()
    print("COMMITTED")


if __name__ == "__main__":
    main()
