"""
Migration: capture the DTS report's dollar/on-hand fields (2026-11-17).

The weekly Sell-Thru CSV carries 44 columns; we used 7. This adds:
- sales.dollars — retail SalesAmount per row ($900K objective = retail register
  sales, Josh 2026-11-17; realized price = dollars/units per spec §4.2)
- dts_weekly_snapshot — the rich per-store×SKU weekly pull (on-hand, dumps,
  prior-year, WOS) feeding the AI Replenishment build (P0 data foundations).

Idempotent; pooler-safe (SET TRANSACTION READ WRITE in-txn).
"""
import os
import psycopg2

DDL = """
ALTER TABLE sales ADD COLUMN IF NOT EXISTS dollars numeric;

CREATE TABLE IF NOT EXISTS dts_weekly_snapshot (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id integer NOT NULL,
    sku_id bigint NOT NULL,
    period_start date,
    period_end date,
    sales_qty integer,
    sales_amount numeric,
    realized_price numeric,
    onhand_qty integer,
    onhand_amount numeric,
    dump_qty integer,
    dump_amount numeric,
    prev_yr_qty integer,
    prev_yr_amount numeric,
    weeks_of_supply numeric,
    days_since_last_delivery integer,
    feed_source text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dts_snap_period ON dts_weekly_snapshot(period_end, store_id);
CREATE INDEX IF NOT EXISTS dts_snap_sku ON dts_weekly_snapshot(sku_id);
ALTER TABLE dts_weekly_snapshot ENABLE ROW LEVEL SECURITY;
"""

def main():
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    cur = conn.cursor()
    cur.execute("SET TRANSACTION READ WRITE")
    cur.execute(DDL)
    conn.commit()
    conn.close()
    print("dts_dollars_2026_11_17: applied")

if __name__ == "__main__":
    main()
