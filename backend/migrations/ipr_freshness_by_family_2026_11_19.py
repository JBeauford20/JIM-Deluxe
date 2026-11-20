"""
Settings: freshness window per product family (Josh's dump cycle, 2026-11-19).

  Soil 9cm / 12cm / 17cm ............. 90 days
  Terrariums (small, medium, holiday) . 275 days
  Glass / H2O (H2O bowls, Venti bowls, Shenandoah, Elkhorn, any water item) . 120 days

Writes two ipr_settings rows read by IPR Engine v2 (replacing the single freshness_weeks=8):
  freshness_days_by_family  {ipr_family: days, "_default": 90}  — every ipr_family present
  freshness_days_by_sku     {sku_id: days}  — every glass/H2O and terrarium SKU, explicitly, so
                            the window survives family remaps (e.g. Shenandoah sits in Holiday)
Families not in Josh's list get 90 days and are marked INFERRED in the note; a family that
appears later and is missing from the map gets the 90-day default plus an engine flag.
freshness_weeks is kept (engine fallback when the map is absent) but marked superseded.

Idempotent (upsert). Dry-run by default:
  python backend/migrations/ipr_freshness_by_family_2026_11_19.py           # show diff
  python backend/migrations/ipr_freshness_by_family_2026_11_19.py --apply   # write
"""
import argparse
import json
import os
import re

import psycopg2

SOIL, TERR, H2O = 90, 275, 120
DEFAULT = 90
JOSH = {  # stated by Josh 2026-11-19
    "9cm Standard": SOIL, "9cm Canopy/Hanging Basket": SOIL,
    "12cm Standard": SOIL, "12cm Collector": SOIL, "12cm Rare Collector": SOIL,
    "12cm Other": SOIL, "12cm Canopy/Hanging Basket": SOIL,
    "17cm Standard": SOIL, "17cm Collector": SOIL, "17cm Rare Collector": SOIL, "17cm Other": SOIL,
    "Terrarium": TERR,
    "H2O": H2O,
}
INFERRED = {  # not named by Josh — soil-grown product, so the soil cycle; confirm
    "Climb-ITT": SOIL, "Dish Garden": SOIL, "Holiday": SOIL, "Seasonal/Holiday": SOIL,
}
H2O_RX = re.compile(r"h2o|h20|venti|shenandoah|elkhorn|water|bowl|deco glass|mason jar", re.I)
TERR_RX = re.compile(r"terrari|terraform", re.I)


def build(cur):
    cur.execute("SELECT DISTINCT ipr_family FROM ipr_family_map ORDER BY 1")
    fams = [r[0] for r in cur.fetchall()]
    fmap, notes = {"_default": DEFAULT}, []
    for f in sorted(set(fams) | set(INFERRED)):
        if f in JOSH:
            fmap[f] = JOSH[f]
        elif f in INFERRED:
            fmap[f] = INFERRED[f]
            notes.append(f"{f}={INFERRED[f]} (inferred)")
        else:
            notes.append(f"{f} -> default {DEFAULT} (flagged)")
    cur.execute("""SELECT k.sku_id, k.description, k.family, fm.ipr_family
                   FROM skus k LEFT JOIN ipr_family_map fm USING (sku_id) ORDER BY 1""")
    fsku, sku_lines = {}, []
    for sku, desc, fam, ipr in cur.fetchall():
        text = f"{desc or ''} {fam or ''}"
        want = TERR if TERR_RX.search(text) and "venti" not in text.lower() else (
            H2O if H2O_RX.search(text) else None)
        if want:   # explicit for every glass/H2O/terrarium SKU: survives family remaps
            fsku[str(sku)] = want
            sku_lines.append(f"{sku} {desc!r} [{ipr}] -> {want}")
    return fmap, fsku, notes, sku_lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    if not a.apply:
        conn.set_session(readonly=True)
    cur = conn.cursor()
    fmap, fsku, notes, sku_lines = build(cur)
    cur.execute("""SELECT key, value FROM ipr_settings
                   WHERE key IN ('freshness_weeks','freshness_days_by_family','freshness_days_by_sku')""")
    cur_vals = dict(cur.fetchall())
    print("current:", json.dumps(cur_vals, default=str))
    print("freshness_days_by_family ->", json.dumps(fmap, indent=1))
    print("notes:", "; ".join(notes) or "none")
    print(f"freshness_days_by_sku -> {len(fsku)} overrides")
    for line in sku_lines:
        print("   ", line)
    if not a.apply:
        print("DRY RUN - nothing written. Re-run with --apply.")
        return
    cur.execute("SET TRANSACTION READ WRITE")
    note_f = ("Sellable on-hand window per ipr_family (Josh dump cycle 2026-11-19: soil 9/12/17cm 90d, "
              "terrariums 275d, glass/H2O 120d). " + ("; ".join(notes) if notes else ""))
    for key, val, note in (
            ("freshness_days_by_family", fmap, note_f),
            ("freshness_days_by_sku", fsku,
             "Per-SKU freshness override for glass/H2O and terrarium SKUs mapped to another family.")):
        cur.execute("""INSERT INTO ipr_settings (key, value, spec_ref, status, note)
                       VALUES (%s, %s::jsonb, '§4.5', 'Josh 2026-11-19', %s)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, status = EXCLUDED.status,
                                                     note = EXCLUDED.note, updated_at = now()""",
                    (key, json.dumps(val), note))
    cur.execute("""UPDATE ipr_settings SET status = 'superseded by freshness_days_by_family (2026-11-19) '
                   '- engine fallback only', updated_at = now() WHERE key = 'freshness_weeks'""")
    conn.commit()
    print("COMMITTED")


if __name__ == "__main__":
    main()
