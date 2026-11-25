"""
Migration: master + flex cart builds on order_carts (Josh 2026-11-25).

The weekly cap counts MASTER cart configurations: a cart = its master's base shelves + up to
two store-specific flex shelves. The engine (cart_menu_mode = master_flex) writes each cart's
master_code ("M 1".."M n", most used first; directed / pre-built carts stay NULL and keep their
own names) and flex_positions (the shelf positions that are flex). The exports name kits by
base build when these are present; runs without them export exactly as before. The engine
writes nothing here until this is applied (it checks information_schema first).

Additive and idempotent; pooler-safe. NOT yet applied.
"""
import os
import psycopg2

DDL = """
ALTER TABLE order_carts
    ADD COLUMN IF NOT EXISTS master_code text,
    ADD COLUMN IF NOT EXISTS flex_positions int[];
"""


def main():
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    cur = conn.cursor()
    cur.execute("SET TRANSACTION READ WRITE")
    cur.execute(DDL)
    conn.commit()
    conn.close()
    print("order_carts_master_2026_11_25: applied")


if __name__ == "__main__":
    main()
