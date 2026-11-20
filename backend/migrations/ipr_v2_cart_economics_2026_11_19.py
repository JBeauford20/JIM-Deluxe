"""
Migration: §19.1 cart-economics settings for the IPR v2 low-value cart decision.

cart_wholesale_low   1700  carts below this wholesale value get the release/defer decision
cart_delivered_cost   240  estimated delivered cost per cart (assumption range $180-$300)

The engine reads both with these defaults, so the rows only make them editable.
min_drop_shelves is no longer read by the engine (fixed min-drop deferral removed).
Idempotent (existing values are kept); pooler-safe. NOT yet applied.
"""
import os
import psycopg2

ROWS = (
    ("cart_wholesale_low", "1700", "§19.1",
     "Carts below this wholesale value are released only if one week of lost sales >= cart "
     "delivered cost or a line is Critical risk; otherwise deferred (never padded)."),
    ("cart_delivered_cost", "240", "§19.1",
     "Estimated delivered cost per cart; editable assumption, approx $180-$300."),
)


def main():
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    cur = conn.cursor()
    cur.execute("SET TRANSACTION READ WRITE")
    for key, val, ref, note in ROWS:
        cur.execute("""INSERT INTO ipr_settings (key, value, spec_ref, status, note)
                       VALUES (%s, %s::jsonb, %s, 'initial assumption — editable', %s)
                       ON CONFLICT (key) DO NOTHING""", (key, val, ref, note))
    conn.commit()
    conn.close()
    print("ipr_v2_cart_economics_2026_11_19: applied")


if __name__ == "__main__":
    main()
