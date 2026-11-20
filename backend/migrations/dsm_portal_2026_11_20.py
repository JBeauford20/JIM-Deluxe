"""
DSM portal + audit-fix schema (additive, idempotent). From the 2026-11-20 adherence audit
and the §26 DSM portal build.

  stores — §1/§4.1 hard-block and DSM-assignment fields (NULL = inert, engine reads them
           only when present):
      dsm                     text        assigned District Sales Manager (display name)
      dsm_email               text        where that DSM's alerts go
      capacity_carts          integer     store capacity in CC carts (§1 hard block)
      approved_assortment     jsonb       array of approved ipr_family names (§24 block)
      receiving_restrictions  text        free-text receiving constraints (exported, not a gate)

  dsm_store_visits — §26.6 store-visit input (user, date, time and source are required
      by the spec; verified counts may later improve inventory confidence).

  email_outbox — DSM alert emails. Sent immediately when SMTP_* env is configured,
      otherwise queued here and visible in the portal.

  ipr_ai_reviews — §25 independent AI reviewer output + cart-config consolidation
      reports, one row per run per kind.

  ipr_settings — new tunables from the audit (ON CONFLICT DO NOTHING keeps live values).

Reads SUPABASE_DB_URL from env. Safe to re-run.
"""
import json
import os

import psycopg2

DDL = """
ALTER TABLE stores ADD COLUMN IF NOT EXISTS dsm text;
ALTER TABLE stores ADD COLUMN IF NOT EXISTS dsm_email text;
ALTER TABLE stores ADD COLUMN IF NOT EXISTS capacity_carts integer;
ALTER TABLE stores ADD COLUMN IF NOT EXISTS approved_assortment jsonb;
ALTER TABLE stores ADD COLUMN IF NOT EXISTS receiving_restrictions text;

CREATE TABLE IF NOT EXISTS dsm_store_visits (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id        bigint NOT NULL REFERENCES stores(store_id),
    visited_at      date NOT NULL DEFAULT CURRENT_DATE,
    recorded_by     text NOT NULL,
    source          text NOT NULL DEFAULT 'dsm_portal',
    verified_counts jsonb,          -- {sku_id: units} verified on the floor
    merchandising_status text,      -- merchandised / partially / not merchandised / unknown
    product_condition    text,
    discarded_units      integer,
    display_issues       text,
    comments             text,
    recommended_changes  text,
    follow_up            text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dsm_store_visits_store ON dsm_store_visits(store_id, visited_at DESC);
ALTER TABLE dsm_store_visits ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS email_outbox (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    to_email    text NOT NULL,
    subject     text NOT NULL,
    body        text NOT NULL,
    kind        text NOT NULL,              -- dsm_alert | dsm_digest | ...
    context     jsonb,
    status      text NOT NULL DEFAULT 'queued',   -- queued | sent | failed
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz
);
CREATE INDEX IF NOT EXISTS email_outbox_status ON email_outbox(status, created_at);
ALTER TABLE email_outbox ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS ipr_ai_reviews (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      uuid NOT NULL REFERENCES recommendation_runs(id) ON DELETE CASCADE,
    kind        text NOT NULL,              -- review | consolidation
    model       text,
    verdict     text,                       -- PASS | WARNING | BLOCK (review) / summary (consolidation)
    report      jsonb NOT NULL,
    created_by  text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ipr_ai_reviews_run ON ipr_ai_reviews(run_id, kind, created_at DESC);
ALTER TABLE ipr_ai_reviews ENABLE ROW LEVEL SECURITY;
"""

SETTINGS = {
    "min_cart_review_after_pushes": (4, "audit F2 2026-11-20", "initial — requires review",
        "Consecutive min-cart pushes before the engine raises min_cart_service_review "
        "(an effective service stop needs DSM review, §11.3/§27)."),
    "peak_floor_require_starvation": (False, "§7 / audit F3 2026-11-20", "decision pending",
        "True = the Apr-Jul peak floor binds only when 13-wk deliveries could not have "
        "stocked floor-rate demand. False = 11/20 calibration (unconditional)."),
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

    for tbl, col in [("stores", "dsm"), ("stores", "dsm_email"), ("stores", "capacity_carts"),
                     ("stores", "approved_assortment"), ("stores", "receiving_restrictions")]:
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
                    (tbl, col))
        print(f"  {tbl}.{col}: {'OK' if cur.fetchone() else 'MISSING'}")
    for tbl in ("dsm_store_visits", "email_outbox", "ipr_ai_reviews"):
        cur.execute("SELECT to_regclass(%s)", (tbl,))
        print(f"  table {tbl}: {'OK' if cur.fetchone()[0] else 'MISSING'}")
    cur.execute("SELECT key FROM ipr_settings WHERE key = ANY(%s)", (list(SETTINGS),))
    print("  settings:", [r[0] for r in cur.fetchall()])
    conn.close()


if __name__ == "__main__":
    main()
