"""
Compute the Terrarium + Climb-itt program velo codes (AA/A/B/C/D) per store and write them to
stores.tier_terrarium / stores.tier_climbitt. Same national-percentile buckets the velocity
engine uses (AA top 0.5%, A top 10%, B top 30%, C top 60%, D rest), ranked among stores that
sold the category in the trailing 52 weeks; non-sellers = D. Re-run to refresh. — Josh, Nov 2026

Terrarium = SKU description contains 'terrarium'. Climb-itt = family 'Climb-itt' or name contains
climb/stake/pole. Reads SUPABASE_DB_URL from env.
"""
import os
from collections import Counter
import psycopg2, psycopg2.extras

TERRARIUM_WHERE = "description ILIKE '%terrarium%'"
CLIMBITT_WHERE = ("family='Climb-itt' OR description ILIKE '%climb%' "
                  "OR description ILIKE '%stake%' OR description ILIKE '%pole%'")


def compute(cur, label, where_sku, col):
    cur.execute(f"""
        WITH cat AS (SELECT sku_id FROM skus WHERE {where_sku}),
        u AS (SELECT s.store_id, SUM(s.units_sold) AS units FROM sales s
              WHERE s.sku_id IN (SELECT sku_id FROM cat)
                AND s.sale_date >= CURRENT_DATE - INTERVAL '364 days'
              GROUP BY s.store_id)
        SELECT st.store_id, COALESCE(u.units, 0) AS units
        FROM stores st LEFT JOIN u ON u.store_id = st.store_id WHERE st.active = true
    """)
    rows = cur.fetchall()
    pos = sorted([r for r in rows if r["units"] > 0], key=lambda r: -r["units"])
    n = len(pos)
    tmap = {}
    for i, r in enumerate(pos):
        f = i / n if n else 1
        tmap[r["store_id"]] = ('AA' if f < 0.005 else 'A' if f < 0.10 else
                               'B' if f < 0.30 else 'C' if f < 0.60 else 'D')
    updates = [(tmap.get(r["store_id"], 'D'), r["store_id"]) for r in rows]
    psycopg2.extras.execute_batch(cur, f"UPDATE stores SET {col}=%s WHERE store_id=%s",
                                  updates, page_size=500)
    print(f"{label}: {n} sellers; dist {dict(Counter(t for t, _ in updates))}")


def main():
    con = psycopg2.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=25); con.autocommit = True
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS tier_terrarium text")
    cur.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS tier_climbitt text")
    cur.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS climb_itt_bundle boolean DEFAULT false")
    compute(cur, "Terrarium", TERRARIUM_WHERE, "tier_terrarium")
    compute(cur, "Climb-itt", CLIMBITT_WHERE, "tier_climbitt")
    con.close()


if __name__ == "__main__":
    main()
