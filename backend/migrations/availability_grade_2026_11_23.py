"""
Availability grade (additive, idempotent). 2026-11-23.

The weekly availability file carries a Grade on every line. Grade 3 = more mature plants and
plants coming in from a contract grower: they must leave first. Grade 2 = everything else
(Josh, 2026-11-23). The upload now keeps it and the engine draws grade 3 units before
grade 2 within a SKU (setting availability_grade_first).

  availability_lines.grade  integer  NULL = not given (older uploads)

Reads SUPABASE_DB_URL from env. Safe to re-run.
"""
import os

import psycopg2


def main():
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=25)
    cur = conn.cursor()
    cur.execute("ALTER TABLE availability_lines ADD COLUMN IF NOT EXISTS grade integer")
    conn.commit()
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'availability_lines' AND column_name = 'grade'""")
    print("  availability_lines.grade:", "OK" if cur.fetchone() else "MISSING")
    conn.close()


if __name__ == "__main__":
    main()
