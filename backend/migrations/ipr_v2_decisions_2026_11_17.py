"""
Migration: Josh's IPR v2 decisions (submitted 2026-11-17 21:41 via the decisions page).

1A  defer stores whose eligible need is below one full shelf (spec §23 deferral — never pad)
2A  enforce hard blocks + minimal two-step reactivation approval (spec §11.5, §27)
3A  freshness window 8 weeks — confirmed
4A  coverage 4 weeks — confirmed
5A  SKU→family mapping approved as proposed (spec §4.5)
6A  grade cut lines accepted
7B  legacy v1 engine retired (enforced in code)
8A  verification drafts deleted (done separately)

Idempotent; pooler-safe.
"""
import json
import os
import psycopg2

DDL = """
CREATE TABLE IF NOT EXISTS ipr_reactivations (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id           integer NOT NULL,
    ipr_family         text NOT NULL,
    status             text NOT NULL DEFAULT 'pending_dsm'
                       CHECK (status IN ('pending_dsm','pending_director','approved','rejected')),
    conversion_13wk    numeric,
    grade              text,
    diagnosis          text NOT NULL,
    corrective_action  text NOT NULL,
    test_units         integer NOT NULL CHECK (test_units > 0),
    expires_at         date NOT NULL,
    review_date        date NOT NULL,
    requested_by       text NOT NULL,
    requested_at       timestamptz NOT NULL DEFAULT now(),
    dsm_by             text, dsm_at timestamptz,
    director_by        text, director_at timestamptz,
    rejected_by        text, rejected_at timestamptz, reject_reason text
);
CREATE INDEX IF NOT EXISTS ipr_react_store_fam ON ipr_reactivations(store_id, ipr_family, status);
ALTER TABLE ipr_reactivations ENABLE ROW LEVEL SECURITY;
"""

APPROVED = "approved — Josh 2026-11-17"


def main():
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    cur = conn.cursor()
    cur.execute("SET TRANSACTION READ WRITE")
    cur.execute(DDL)
    cur.execute("""INSERT INTO ipr_settings (key, value, spec_ref, status, note)
                   VALUES ('min_drop_shelves', '1.0'::jsonb, '§23', %s,
                           'Stores whose eligible need is below this many full shelves are deferred: '
                           'need is recalculated next week; carts are never padded.')
                   ON CONFLICT (key) DO UPDATE SET status = EXCLUDED.status, updated_at = now()""",
                (f"{APPROVED} (decision 1A)",))
    for key, dec in (("freshness_weeks", "3A"), ("coverage_weeks", "4A"), ("grade_cutoffs_pct", "6A")):
        cur.execute("UPDATE ipr_settings SET status = %s, updated_at = now() WHERE key = %s",
                    (f"{APPROVED} (decision {dec}) — revisit after back-test", key))
    cur.execute("""UPDATE ipr_family_map SET approved = true, updated_at = now(),
                          source = 'approved as proposed — Josh 2026-11-17 (decision 5A)'
                   WHERE ipr_family <> 'Unmapped' AND NOT approved""")
    print(f"family mappings approved: {cur.rowcount}")
    conn.commit()
    conn.close()
    print("ipr_v2_decisions_2026_11_17: applied")


if __name__ == "__main__":
    main()
