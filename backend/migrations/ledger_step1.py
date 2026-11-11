"""
Step 1 of the variety-level work — the ALLOCATION LEDGER (additive, non-breaking).

Creates `order_line_allocations` (the authoritative, Aster-variety-level record of what
each order draws from each availability line) + the correct per-variety remaining views,
then BACKFILLS it from the current physical carts (cart_shelves) for the latest run per
batch. Nothing in the app reads these yet — this just establishes the foundation and lets
us verify it reproduces the real draws and exposes the per-line negatives the old
reservation path was hiding.

Idempotent: safe to re-run (recreates views, re-does the 'backfill' rows only).
Reads SUPABASE_DB_URL from env.
"""
import os
import psycopg2
import psycopg2.extras

DDL = """
CREATE TABLE IF NOT EXISTS order_line_allocations (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_line_id        uuid REFERENCES order_lines(id) ON DELETE CASCADE,
    availability_line_id uuid NOT NULL REFERENCES availability_lines(id),
    cart_shelf_id        uuid REFERENCES cart_shelves(id) ON DELETE CASCADE,
    run_id               uuid REFERENCES recommendation_runs(id),
    batch_id             uuid REFERENCES availability_batches(id),
    sku_id               bigint,
    qty                  integer NOT NULL CHECK (qty >= 0),
    source               text NOT NULL DEFAULT 'engine',
    locked               boolean NOT NULL DEFAULT false,
    created_at           timestamptz DEFAULT now(),
    updated_at           timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ola_avail_line ON order_line_allocations(availability_line_id);
CREATE INDEX IF NOT EXISTS ola_order_line ON order_line_allocations(order_line_id);
CREATE INDEX IF NOT EXISTS ola_cart_shelf ON order_line_allocations(cart_shelf_id);
CREATE INDEX IF NOT EXISTS ola_batch      ON order_line_allocations(batch_id);
CREATE INDEX IF NOT EXISTS ola_run        ON order_line_allocations(run_id);

ALTER TABLE order_line_allocations ENABLE ROW LEVEL SECURITY;

-- allocated units per Aster variety line
CREATE OR REPLACE VIEW v_alloc_variety AS
    SELECT availability_line_id, batch_id, sku_id, SUM(qty)::integer AS allocated
    FROM order_line_allocations
    GROUP BY availability_line_id, batch_id, sku_id;

-- allocated units rolled up to HD SKU
CREATE OR REPLACE VIEW v_alloc_hd_sku AS
    SELECT batch_id, sku_id, SUM(qty)::integer AS allocated
    FROM order_line_allocations
    GROUP BY batch_id, sku_id;

-- AUTHORITATIVE per-variety remaining: units on that Aster line minus what's allocated.
-- This is the correct denominator the SKU-level availability_remaining view hides.
CREATE OR REPLACE VIEW v_availability_remaining_variety AS
    SELECT al.id   AS availability_line_id,
           al.batch_id,
           al.sku_id,
           al.units_available,
           COALESCE(va.allocated, 0)                      AS allocated,
           (al.units_available - COALESCE(va.allocated, 0)) AS remaining
    FROM availability_lines al
    LEFT JOIN v_alloc_variety va ON va.availability_line_id = al.id;
"""

# Backfill from the physical carts of the LATEST run per batch (avoids double-counting
# superseded draft re-runs against the same batch — the root of the current negatives).
BACKFILL = """
DELETE FROM order_line_allocations WHERE source = 'backfill';

WITH latest AS (
    SELECT DISTINCT ON (batch_id) id AS run_id, batch_id
    FROM recommendation_runs
    ORDER BY batch_id, run_at DESC
)
INSERT INTO order_line_allocations
    (order_line_id, availability_line_id, cart_shelf_id, run_id, batch_id, sku_id, qty, source)
SELECT ol.id, cs.availability_line_id, cs.id, o.run_id, l.batch_id, cs.sku_id, cs.unit_count, 'backfill'
FROM cart_shelves cs
JOIN order_carts oc ON oc.id = cs.cart_id
JOIN orders o       ON o.id = oc.order_id
JOIN latest l       ON l.run_id = o.run_id
LEFT JOIN order_lines ol ON ol.order_id = o.id AND ol.sku_id = cs.sku_id
WHERE cs.availability_line_id IS NOT NULL
  AND cs.unit_count > 0;
"""


def main():
    con = psycopg2.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=25)
    con.autocommit = False
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(DDL)
        cur.execute(BACKFILL)
        con.commit()
    except Exception:
        con.rollback()
        raise

    # ── Verify ──────────────────────────────────────────────────
    cur.execute("SELECT count(*) n, COALESCE(SUM(qty),0) u FROM order_line_allocations WHERE source='backfill'")
    r = cur.fetchone()
    print(f"ledger backfill: {r['n']} rows, {r['u']:,} units")

    # ledger units per batch vs physical cart_shelves (latest run) — should match
    cur.execute("""
        WITH latest AS (
            SELECT DISTINCT ON (batch_id) id AS run_id, batch_id
            FROM recommendation_runs ORDER BY batch_id, run_at DESC
        ),
        phys AS (
            SELECT l.batch_id, SUM(cs.unit_count) u
            FROM cart_shelves cs
            JOIN order_carts oc ON oc.id = cs.cart_id
            JOIN orders o ON o.id = oc.order_id
            JOIN latest l ON l.run_id = o.run_id
            WHERE cs.availability_line_id IS NOT NULL AND cs.unit_count > 0
            GROUP BY l.batch_id
        ),
        led AS (SELECT batch_id, SUM(qty) u FROM order_line_allocations GROUP BY batch_id)
        SELECT COALESCE(p.batch_id,l.batch_id) batch_id, p.u phys_u, l.u ledger_u
        FROM phys p FULL OUTER JOIN led l ON l.batch_id=p.batch_id
        ORDER BY 1
    """)
    print("\nphysical vs ledger units per batch (should match):")
    for row in cur.fetchall():
        match = "OK" if (row['phys_u'] or 0) == (row['ledger_u'] or 0) else "MISMATCH"
        print(f"  {str(row['batch_id'])[:8]}: phys={row['phys_u']} ledger={row['ledger_u']} [{match}]")

    # per-variety remaining: how many lines are negative now (authoritative)
    cur.execute("SELECT count(*) n FROM v_availability_remaining_variety WHERE remaining < 0")
    neg = cur.fetchone()['n']
    cur.execute("SELECT count(*) n FROM v_availability_remaining_variety WHERE allocated > 0")
    used = cur.fetchone()['n']
    print(f"\nper-variety remaining: {used} lines with allocation, {neg} NEGATIVE (overdrawn)")
    if neg:
        cur.execute("""SELECT availability_line_id, sku_id, units_available, allocated, remaining
                       FROM v_availability_remaining_variety WHERE remaining < 0
                       ORDER BY remaining LIMIT 8""")
        for row in cur.fetchall():
            print(f"    line {str(row['availability_line_id'])[:8]} sku {row['sku_id']}: "
                  f"avail {row['units_available']} - alloc {row['allocated']} = {row['remaining']}")
    con.close()


if __name__ == "__main__":
    main()
