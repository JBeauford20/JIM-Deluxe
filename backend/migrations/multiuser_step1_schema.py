"""
Multi-user concurrency — schema migration (additive, non-breaking, idempotent).

Adds every column/table the concurrency build needs, across all phases, in ONE re-runnable
script (mirrors the style of ledger_step1.py). Nothing here drops or rewrites existing data.

  Phase 1 — optimistic version checks:
      order_carts.version            int  not null default 1

  Phase 2 — change log + polling:
      change_log(run_id, entity, entity_id, action, user_email, created_at)  — new table

  Phase 3 — presence + store-level soft claims:
      plan_presence(run_id, user_email, last_seen)                           — new table
      orders.claimed_by             text
      orders.claimed_at             timestamptz

  Phase 4 — Active Plans dashboard:
      recommendation_runs.created_by        text
      recommendation_runs.last_activity_at  timestamptz  default now()

RLS: new tables get RLS enabled (deny-all to the anon REST API, matching the project's
Nov-2026 security posture). The backend uses the postgres role via SUPABASE_DB_URL and bypasses
RLS, so the app is unaffected.

DO NOT run against the live DB from the sandbox (host is IPv6-only / unreachable). Reads
SUPABASE_DB_URL from env. Safe to re-run.
"""
import os
import psycopg2
import psycopg2.extras

DDL = """
-- ── Phase 1: order_carts.version ──────────────────────────────
ALTER TABLE order_carts ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1;

-- ── Phase 4: recommendation_runs activity/ownership ───────────
ALTER TABLE recommendation_runs ADD COLUMN IF NOT EXISTS created_by text;
ALTER TABLE recommendation_runs ADD COLUMN IF NOT EXISTS last_activity_at timestamptz DEFAULT now();

-- ── Phase 3: per-store soft claims ────────────────────────────
ALTER TABLE orders ADD COLUMN IF NOT EXISTS claimed_by text;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
CREATE INDEX IF NOT EXISTS orders_claimed_at ON orders(claimed_at);

-- ── Phase 2: change log (append-only feed, polled by the frontend) ──
CREATE TABLE IF NOT EXISTS change_log (
    id          bigserial PRIMARY KEY,
    run_id      uuid REFERENCES recommendation_runs(id) ON DELETE CASCADE,
    entity      text NOT NULL,          -- 'order' | 'cart' | 'shelf' | 'config' | 'presence' | ...
    entity_id   text,                   -- uuid/text id of the touched record (nullable)
    action      text NOT NULL,          -- 'add_cart' | 'remove_cart' | 'edit_shelf' | 'approve' | ...
    user_email  text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS change_log_run_ts ON change_log(run_id, created_at);
ALTER TABLE change_log ENABLE ROW LEVEL SECURITY;

-- ── Phase 3: presence heartbeat (who is in which run right now) ──
CREATE TABLE IF NOT EXISTS plan_presence (
    run_id      uuid NOT NULL REFERENCES recommendation_runs(id) ON DELETE CASCADE,
    user_email  text NOT NULL,
    last_seen   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, user_email)
);
CREATE INDEX IF NOT EXISTS plan_presence_seen ON plan_presence(last_seen);
ALTER TABLE plan_presence ENABLE ROW LEVEL SECURITY;
"""


def main():
    con = psycopg2.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=25)
    con.autocommit = False
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(DDL)
        con.commit()
    except Exception:
        con.rollback()
        raise

    # ── Verify ──────────────────────────────────────────────────
    checks = [
        ("order_carts", "version"),
        ("recommendation_runs", "created_by"),
        ("recommendation_runs", "last_activity_at"),
        ("orders", "claimed_by"),
        ("orders", "claimed_at"),
    ]
    for tbl, col in checks:
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
            (tbl, col),
        )
        print(f"  {tbl}.{col}: {'OK' if cur.fetchone() else 'MISSING'}")
    for tbl in ("change_log", "plan_presence"):
        cur.execute("SELECT to_regclass(%s)", (tbl,))
        print(f"  table {tbl}: {'OK' if cur.fetchone()['to_regclass'] else 'MISSING'}")
    con.close()


if __name__ == "__main__":
    main()
