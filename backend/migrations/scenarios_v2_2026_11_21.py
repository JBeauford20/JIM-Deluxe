"""
Scenario approval chain (checks and balances, Josh 2026-11-20). Additive, idempotent.

  planning_scenarios gains an approval lifecycle:
      status         pending_approval | approved | rejected   (legacy rows -> approved)
      interpretation the parser's plain-English restatement, shown to the approver
      approved_by / approved_at / reject_reason
  The approver must hold manager / sales_director / admin and can NEVER be the person
  who entered the scenario. The v2 engine applies only APPROVED scenarios.

  ipr_settings:
      scenario_max_multiplier   cap on demand_multiplier boosts (default 3.0)
      scenario_priority_boost_cap  cap on priority_boost (default 0.2 on the 0-1 scale)

Reads SUPABASE_DB_URL from env. Safe to re-run.
"""
import json
import os

import psycopg2

DDL = """
ALTER TABLE planning_scenarios ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'approved';
ALTER TABLE planning_scenarios ADD COLUMN IF NOT EXISTS interpretation text;
ALTER TABLE planning_scenarios ADD COLUMN IF NOT EXISTS approved_by text;
ALTER TABLE planning_scenarios ADD COLUMN IF NOT EXISTS approved_at timestamptz;
ALTER TABLE planning_scenarios ADD COLUMN IF NOT EXISTS reject_reason text;
CREATE INDEX IF NOT EXISTS planning_scenarios_status ON planning_scenarios(status, shipping_week);
"""

SETTINGS = {
    "scenario_max_multiplier": (3.0, "scenarios / checks-and-balances 2026-11-20",
        "guardrail", "A scenario demand_multiplier is clamped to this ceiling."),
    "scenario_priority_boost_cap": (0.2, "scenarios / checks-and-balances 2026-11-20",
        "guardrail", "A scenario priority_boost is clamped to this (priority is 0-1)."),
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
    for col in ("status", "interpretation", "approved_by", "approved_at", "reject_reason"):
        cur.execute("""SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'planning_scenarios' AND column_name = %s""", (col,))
        print(f"  planning_scenarios.{col}: {'OK' if cur.fetchone() else 'MISSING'}")
    conn.close()


if __name__ == "__main__":
    main()
