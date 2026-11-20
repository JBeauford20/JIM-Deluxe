"""
Post-delivery measurement (§29 + the 11/20 leadership note "sales increase after a
shipment has landed, 7–28 days"). Additive, idempotent.

  delivery_performance   — one row per store × family × delivery-date cohort:
      units delivered, sales rate 28 days before, sales rate days 7–28 after, the lift,
      sell-through by day 28, and a §29 classification.
  store_family_response  — the engine-readable aggregate per store × family: cohort count,
      average lift, average 28-day sell-through, shares of strong/weak cohorts, and a
      response class. Rebuilt on every compute.
  ipr_settings.post_delivery_feedback — off | observe | apply (default OBSERVE: measure and
      report, change nothing). 'apply' lets the engine shift a store's delivery cadence by
      at most one week on the measured response — flipping that on is a leadership call.

Reads SUPABASE_DB_URL from env. Safe to re-run.
"""
import json
import os

import psycopg2

DDL = """
CREATE TABLE IF NOT EXISTS delivery_performance (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id        bigint NOT NULL,
    ipr_family      text NOT NULL,
    delivery_date   date NOT NULL,
    cohort_units    integer NOT NULL,
    pre28_units     integer,                 -- family sales, 28 days before delivery
    post7_28_units  integer,                 -- family sales, days 7-28 after delivery
    pre_rate        numeric,                 -- units/day before
    post_rate       numeric,                 -- units/day in the 7-28 window
    lift            numeric,                 -- post_rate / pre_rate (NULL when no baseline)
    sell_through_28 numeric,                 -- sold in 28 days / delivered
    classification  text NOT NULL,           -- strong | expected | weak | no_baseline | too_recent
    computed_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (store_id, ipr_family, delivery_date)
);
CREATE INDEX IF NOT EXISTS delivery_performance_store ON delivery_performance(store_id, ipr_family);
ALTER TABLE delivery_performance ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS store_family_response (
    store_id        bigint NOT NULL,
    ipr_family      text NOT NULL,
    cohorts         integer NOT NULL,
    avg_lift        numeric,
    avg_sell28      numeric,
    strong_share    numeric,
    weak_share      numeric,
    last_delivery   date,
    response_class  text NOT NULL,           -- strong | expected | weak | unmeasured
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (store_id, ipr_family)
);
ALTER TABLE store_family_response ENABLE ROW LEVEL SECURITY;
"""

SETTINGS = {
    "post_delivery_feedback": ("observe", "§29 / leadership note 2026-11-20",
        "observe = default: measure + report only",
        "off = skip entirely; observe = measure, report in the run summary, change nothing; "
        "apply = measured response may shift a store's cadence by at most one week "
        "(bounded, logged per store). Switching to 'apply' is a leadership decision."),
    "post_delivery_window_weeks": (26, "§29", "initial — requires back-testing",
        "How far back compute() builds delivery cohorts."),
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
    for tbl in ("delivery_performance", "store_family_response"):
        cur.execute("SELECT to_regclass(%s)", (tbl,))
        print(f"  table {tbl}: {'OK' if cur.fetchone()[0] else 'MISSING'}")
    cur.execute("SELECT key, value FROM ipr_settings WHERE key = ANY(%s)", (list(SETTINGS),))
    print("  settings:", dict(cur.fetchall()))
    conn.close()


if __name__ == "__main__":
    main()
