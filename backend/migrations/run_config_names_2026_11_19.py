"""
Migration: per-run cart config names (Josh 2026-11-19).

Every distinct cart config in a run is named "CC 1", "CC 2", ... ascending (by carts
using it desc, ties by first store number); permanent carts keep their names. The first
export of a run persists (run_id, signature -> name) here so Stores and Carts, the ops
Pack Plan, Aster and the UI all use the same, stable names. Exports work without this
table (names computed by the same rule) but are then not pinned across later edits.

Idempotent; pooler-safe. NOT yet applied.
"""
import os
import psycopg2

DDL = """
CREATE TABLE IF NOT EXISTS run_config_names (
    run_id      uuid    NOT NULL REFERENCES recommendation_runs(id) ON DELETE CASCADE,
    signature   text    NOT NULL,
    name        text    NOT NULL,
    cart_count  integer,
    first_store integer,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, signature),
    UNIQUE (run_id, name)
);
"""


def main():
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    cur = conn.cursor()
    cur.execute("SET TRANSACTION READ WRITE")
    cur.execute(DDL)
    conn.commit()
    conn.close()
    print("run_config_names_2026_11_19: applied")


if __name__ == "__main__":
    main()
