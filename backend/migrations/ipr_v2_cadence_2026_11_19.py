"""
Migration: delivery cadence by store grade (Josh approved 2026-11-19).

delivery_cadence_weeks             {"AA":1,"A":1,"B":2,"C+":3,"C":3,"C-":4,"D":4}
cadence_buffer_weeks               1
economic_deferral_for_due_stores   false
kit_split_ceramic_share            0.5
cart_consolidate                   true

The engine reads all of these with the same defaults; the rows make them editable.
Idempotent (existing values kept); pooler-safe. NOT yet applied.
"""
import json
import os
import psycopg2

ROWS = (
    ("delivery_cadence_weeks", {"AA": 1, "A": 1, "B": 2, "C+": 3, "C": 3, "C-": 4, "D": 4}, "§10, §19.1",
     "Weeks between deliveries by store cadence grade (stores.volume_code, else best family grade, "
     "else C). Due when last delivery -> arrival >= cadence x 7 - 3 days or any line is Critical."),
    ("cadence_buffer_weeks", 1, "§10", "Coverage for a due store's lines = cadence + this buffer "
                                       "(capped by family freshness)."),
    ("economic_deferral_for_due_stores", False, "§19.1",
     "When false, due stores release even if low value (warning only)."),
    ("kit_split_ceramic_share", 0.5, "§16", "Target ceramic share of jute/ceramic kits, network-wide per SKU."),
    ("cart_consolidate", True, "§19.1", "Best-fit-decreasing repack of a store's shelves into carts."),
)


def main():
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    cur = conn.cursor()
    cur.execute("SET TRANSACTION READ WRITE")
    for key, val, ref, note in ROWS:
        cur.execute("""INSERT INTO ipr_settings (key, value, spec_ref, status, note)
                       VALUES (%s, %s::jsonb, %s, 'approved — Josh 2026-11-19', %s)
                       ON CONFLICT (key) DO NOTHING""", (key, json.dumps(val), ref, note))
    conn.commit()
    conn.close()
    print("ipr_v2_cadence_2026_11_19: applied")


if __name__ == "__main__":
    main()
