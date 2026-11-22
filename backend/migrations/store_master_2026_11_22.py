"""
Store master handling fields (additive, idempotent). 2026-11-22.

Source: "Home Depot Store List" workbook, first sheet — the business's master list of how
each store must be handled. Loaded by backend/ingest/load_store_master.py (CLI) or the
Stores tab upload (services/store_master.py); the meaning of each restriction lives in
workers/store_rules.py.

  stores
      master_status      text   sheet 'Status' as given: Do Not Ship / With Exceptions / Kurt Weiss
      sku_restriction    text   sheet 'SKU Restrictions' as given (display)
      sku_rule           text   rule code the engine enforces (store_rules.DEFAULT_RULES)
      cross_dock         text   cross-dock the store ships through (ICS-Mass / ICS-NY / Free State)
      merchandiser       text   merchandising partner, sheet granularity ('Altman - W', 'Stinchcomb NJ')
      facility_name      text   HD facility name
      street_address     text
      postal_code        text
      district           text   HD district # (zero-padded text, e.g. '0074')
      market_name        text   HD market name (MKT_NM)
      byo_nbr            integer
      master_loaded_at   timestamptz   last time the store master touched this store
      master_source      text          file name of that load

  order_carts
      handling_tag       text   e.g. 'NO HEDERA' — the cart must be built to the store's
                                variety restriction; part of the cart-config signature so a
                                restricted cart never shares a build sheet with a normal one

  store_master_loads — one row per applied load (who, when, file, counts, full change list)

  ipr_settings.store_sku_rules — optional overrides/extensions of the rule definitions.

Reads SUPABASE_DB_URL from env. Safe to re-run.
"""
import json
import os

import psycopg2

STORE_COLS = [("master_status", "text"), ("sku_restriction", "text"), ("sku_rule", "text"),
              ("cross_dock", "text"), ("merchandiser", "text"), ("facility_name", "text"),
              ("street_address", "text"), ("postal_code", "text"), ("district", "text"),
              ("market_name", "text"), ("byo_nbr", "integer"),
              ("master_loaded_at", "timestamptz"), ("master_source", "text")]

DDL = "\n".join(f"ALTER TABLE stores ADD COLUMN IF NOT EXISTS {c} {t};" for c, t in STORE_COLS) + """
ALTER TABLE order_carts ADD COLUMN IF NOT EXISTS handling_tag text;

CREATE TABLE IF NOT EXISTS store_master_loads (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    loaded_by     text NOT NULL,
    source_file   text,
    sheet_name    text,
    rows_read     integer,
    stores_changed integer,
    summary       jsonb NOT NULL,       -- counts by status / rule / cross dock, QA totals
    changes       jsonb NOT NULL        -- [{store_id, field, old, new}] — the rollback record
);
ALTER TABLE store_master_loads ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS stores_sku_rule ON stores(sku_rule) WHERE sku_rule IS NOT NULL;
"""

SETTINGS = {
    "store_sku_rules": ({}, "store master 2026-11-22", "business rule — store master",
        "Overrides/extensions of the store handling rules, keyed by rule code "
        "(workers/store_rules.py DEFAULT_RULES: no_hedera, littles_collectors_only, "
        "holiday_only_directed). Empty = the defaults apply."),
}


def main():
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=25)
    cur = conn.cursor()
    cur.execute("SET TRANSACTION READ WRITE")
    cur.execute(DDL)
    for k, (v, ref, status, note) in SETTINGS.items():
        cur.execute("""INSERT INTO ipr_settings (key, value, spec_ref, status, note)
                       VALUES (%s, %s::jsonb, %s, %s, %s)
                       ON CONFLICT (key) DO NOTHING""", (k, json.dumps(v), ref, status, note))
    conn.commit()
    for c, _t in STORE_COLS:
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name='stores' AND column_name=%s", (c,))
        print(f"  stores.{c}: {'OK' if cur.fetchone() else 'MISSING'}")
    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name='order_carts' AND column_name='handling_tag'")
    print(f"  order_carts.handling_tag: {'OK' if cur.fetchone() else 'MISSING'}")
    cur.execute("SELECT to_regclass('store_master_loads')")
    print(f"  table store_master_loads: {'OK' if cur.fetchone()[0] else 'MISSING'}")
    conn.close()


if __name__ == "__main__":
    main()
