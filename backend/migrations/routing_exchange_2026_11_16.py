"""
Migration: Routing Exchange (2026-11-16).

Real routing is done by an external team. JIM exports a routing request
(stores + carts), the team returns Store/Route/Drop/Ship Date/Del Date,
and the import replaces the engine's provisional loads with real trucks.

- recommendation_runs.routing_status: draft -> submitted -> routed
- loads.ship_date / delivery_date: stamped from the routing response
- loads.source: 'engine' (provisional) vs 'external_routing' (real)

Idempotent. Reads SUPABASE_DB_URL from env; pooler-safe (SET TRANSACTION
READ WRITE inside the same transaction as the DDL).
"""
import os
import psycopg2

DDL = """
ALTER TABLE recommendation_runs ADD COLUMN IF NOT EXISTS routing_status text NOT NULL DEFAULT 'draft';
ALTER TABLE recommendation_runs ADD COLUMN IF NOT EXISTS routing_submitted_at timestamptz;
ALTER TABLE recommendation_runs ADD COLUMN IF NOT EXISTS routing_imported_at timestamptz;
ALTER TABLE recommendation_runs ADD COLUMN IF NOT EXISTS routing_filename text;

ALTER TABLE loads ADD COLUMN IF NOT EXISTS ship_date date;
ALTER TABLE loads ADD COLUMN IF NOT EXISTS delivery_date date;
ALTER TABLE loads ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'engine';
"""

def main():
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    cur = conn.cursor()
    cur.execute("SET TRANSACTION READ WRITE")
    cur.execute(DDL)
    conn.commit()
    conn.close()
    print("routing_exchange_2026_11_16: applied")

if __name__ == "__main__":
    main()
