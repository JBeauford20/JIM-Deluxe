"""
Migration: audit decisions 2026-11-16 (Josh: 1A 2A 3C 4A 5A 6A).

- order_carts.is_template_exact (3C): True when the cart's contents exactly match
  its CC-code template, so the warehouse knows which carts can be pre-built blind.
  Old rows stay NULL = unknown.
- ai_usage (6A): per-user daily AI call budgets (explain 50 / audit 5 / ask 25 /
  summarize 25), enforced in routers/ai_explanations.py.

Idempotent. Reads SUPABASE_DB_URL from env. NOTE: the Supabase pooler runs in
transaction mode — session-level SET does not stick, so the read-write override
is issued inside the same transaction as the DDL.
"""
import os
import psycopg2

DDL = """
ALTER TABLE order_carts ADD COLUMN IF NOT EXISTS is_template_exact boolean;

CREATE TABLE IF NOT EXISTS ai_usage (
    user_email text    NOT NULL,
    day        date    NOT NULL DEFAULT CURRENT_DATE,
    kind       text    NOT NULL,
    count      integer NOT NULL DEFAULT 0,
    PRIMARY KEY (user_email, day, kind)
);
ALTER TABLE ai_usage ENABLE ROW LEVEL SECURITY;
"""

def main():
    dsn = os.environ["SUPABASE_DB_URL"]
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SET TRANSACTION READ WRITE")
    cur.execute(DDL)
    conn.commit()
    conn.close()
    print("audit_decisions_2026_11_16: applied")

if __name__ == "__main__":
    main()
