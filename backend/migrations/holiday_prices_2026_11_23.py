"""
Wholesale prices for three holiday SKUs that had none (Josh 2026-11-23). Idempotent.

Without a wholesale price these SKUs count as $0 wherever product is valued at wholesale.
Only fills a price that is missing: a SKU that already has one is left alone and reported.

    1016800408  PW LEAFJOY HOLIDAY 9CM GLASS         7.98
    1016674195  H2O Holiday Elkhorn                  12.78
    1016674196  PW LEAFJOY HOLIDAY DISH GARDEN 8IN   19.98

Reads SUPABASE_DB_URL from env. Dry run by default; pass --apply to write.
"""
import os
import sys

import psycopg2

PRICES = [(1016800408, 7.98), (1016674195, 12.78), (1016674196, 19.98)]


def main():
    apply = "--apply" in sys.argv
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=25)
    cur = conn.cursor()
    for sku_id, price in PRICES:
        cur.execute("SELECT description, wholesale_price_whl FROM skus WHERE sku_id = %s", (sku_id,))
        row = cur.fetchone()
        if not row:
            print(f"{sku_id}: NOT FOUND, skipped")
            continue
        if row[1] is not None:
            print(f"{sku_id} {row[0].strip()}: already priced at {row[1]}, left alone")
            continue
        print(f"{sku_id} {row[0].strip()}: no price -> {price}" + ("" if apply else "  (dry run)"))
        if apply:
            cur.execute("UPDATE skus SET wholesale_price_whl = %s, updated_at = now() "
                        "WHERE sku_id = %s AND wholesale_price_whl IS NULL", (price, sku_id))
    if apply:
        conn.commit()
        print("COMMITTED")
    else:
        print("Dry run only. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
