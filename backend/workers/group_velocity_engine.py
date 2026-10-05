"""
JIM Deluxe — SKU Group Velocity Engine
=======================================
Scores every store for each SKU group (12cm, H2O Bowl, Collectors, etc.)
Uses same EWMA/percentile/hysteresis approach as the overall velocity engine
but scoped to a product family + legacy tier.

Writes to store_group_velocity and updates denormalized tier columns on stores.
Run AFTER velocity_engine.py each week.
"""

from pathlib import Path
from datetime import date, timedelta
from collections import defaultdict
import psycopg2
import psycopg2.extras

ALPHA     = 0.10
MIN_WEEKS = 6
HYSTERESIS = 0.2

TIER_BOUNDS = {
    'AA': 99.50,
    'A':  90.00,
    'B':  70.00,
    'C':  40.00,
    'D':   0.00,
}

# Denormalized columns on stores table — group_id → column name
STORE_TIER_COLUMNS = {
    '9cm_all':       'tier_9cm',
    '12cm_all':      'tier_12cm',
    '17cm_premium':  'tier_17cm',
    'h2o_all':       'tier_h2o',
    'boutique_all':  'tier_boutique',
    '12cm_collectors': 'tier_collectors',
}

from db_conn import get_conn

def assign_tier(pct):
    for tier, bound in TIER_BOUNDS.items():
        if pct >= bound:
            return tier
    return 'D'

def apply_hysteresis(prior, new_tier, pct):
    if not prior or prior == 'P':
        return new_tier
    order = ['D', 'C', 'B', 'A', 'AA']
    pi, ni = (order.index(prior) if prior in order else -1,
              order.index(new_tier) if new_tier in order else -1)
    if ni == pi:
        return prior
    if ni > pi:
        return new_tier if pct >= TIER_BOUNDS[new_tier] + HYSTERESIS else prior
    return new_tier if pct < TIER_BOUNDS[prior] - HYSTERESIS else prior

def percentile_rank(velocity, sorted_vels):
    n = len(sorted_vels)
    if n == 0:
        return 0.0
    return (sum(1 for v in sorted_vels if v < velocity) / n) * 100.0

def run_group_scoring(score_week=None, verbose=True):
    if score_week is None:
        today = date.today()
        score_week = today - timedelta(days=today.weekday())

    log = print if verbose else lambda *a, **k: None
    log(f"\n{'='*60}")
    log(f"Group Velocity Engine — week: {score_week}")
    log(f"{'='*60}")

    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # ── Load SKU group definitions ────────────────────────────
    cur.execute("SELECT id, family, legacy_tier FROM sku_groups WHERE active = true")
    groups = cur.fetchall()
    log(f"Scoring {len(groups)} SKU groups.\n")

    # ── Load SKU → (family, legacy_product_group) mapping ────
    cur.execute("""
        SELECT sku_id, family, legacy_product_group
        FROM skus
        WHERE active = true AND family IS NOT NULL
    """)
    sku_meta = {r['sku_id']: r for r in cur.fetchall()}

    # ── Load all weekly sales, pre-aggregated ─────────────────
    log("Loading weekly sales data...")
    cur.execute("""
        SELECT
            store_id,
            sku_id,
            date_trunc('week', sale_date)::date AS week_start,
            SUM(units_sold) AS units
        FROM sales
        WHERE sale_date < %s + INTERVAL '7 days'
          AND sku_id IN (SELECT sku_id FROM skus WHERE active = true AND family IS NOT NULL)
        GROUP BY store_id, sku_id, date_trunc('week', sale_date)::date
        ORDER BY store_id, week_start
    """, (score_week,))
    raw_rows = cur.fetchall()
    log(f"  {len(raw_rows):,} store-sku-week records.")

    # ── Fetch current group tiers for hysteresis ──────────────
    cur.execute("""
        SELECT store_id, sku_group_id, dynamic_tier
        FROM store_group_velocity
        WHERE week_start = (
            SELECT MAX(week_start) FROM store_group_velocity
        )
    """)
    prior_tiers = {(r['store_id'], r['sku_group_id']): r['dynamic_tier']
                   for r in cur.fetchall()}

    # ── Build group → matching SKU IDs ───────────────────────
    def skus_for_group(grp):
        gfam   = grp['family']
        gtier  = grp['legacy_tier']
        gid    = grp['id']
        result = set()
        for sku_id, meta in sku_meta.items():
            fam  = meta['family']
            tier = meta['legacy_product_group']
            # Special aggregate groups
            if gid == '9cm_all'      and fam in ('9cm Littles','9cm Ceramic','9cm Canopy'): result.add(sku_id)
            elif gid == '12cm_all'   and fam in ('12cm','12cm HB','12cm DocBlock'):           result.add(sku_id)
            elif gid == 'h2o_all'    and fam in ('H2O Bowl','H2O OPP','H2O Venti'):           result.add(sku_id)
            elif gid == 'boutique_all' and tier in ('Collectors','Specialty','Boutique','Rare Collectors'): result.add(sku_id)
            # Exact family+tier match
            elif gfam and gtier and fam == gfam and tier == gtier: result.add(sku_id)
            elif gfam and not gtier and fam == gfam:                result.add(sku_id)
        return result

    all_history_rows = []
    store_col_updates = defaultdict(dict)  # store_id → {col: tier}

    for grp in groups:
        gid   = grp['id']
        skus  = skus_for_group(grp)
        if not skus:
            continue

        # Aggregate weekly sales for this group per store
        store_week_units = defaultdict(lambda: defaultdict(float))
        for row in raw_rows:
            if row['sku_id'] in skus:
                store_week_units[row['store_id']][row['week_start']] += float(row['units'])

        if not store_week_units:
            continue

        # EWMA per store
        store_ewma  = {}
        store_weeks = {}
        for sid, week_data in store_week_units.items():
            for week in sorted(week_data.keys()):
                units = week_data[week]
                if sid not in store_ewma:
                    store_ewma[sid]  = units
                    store_weeks[sid] = 1
                else:
                    store_ewma[sid]  = ALPHA * units + (1 - ALPHA) * store_ewma[sid]
                    store_weeks[sid] += 1

        eligible    = {s: v for s, v in store_ewma.items() if store_weeks[s] >= MIN_WEEKS}
        provisional = {s: v for s, v in store_ewma.items() if store_weeks[s] < MIN_WEEKS}

        sorted_vels = sorted(eligible.values())

        aa_count = 0
        for sid, vel in {**eligible, **provisional}.items():
            is_prov = sid in provisional
            if is_prov:
                tier = 'P'
                pct  = None
            else:
                pct      = percentile_rank(vel, sorted_vels)
                raw_tier = assign_tier(pct)
                prior    = prior_tiers.get((sid, gid))
                tier     = apply_hysteresis(prior, raw_tier, pct)

            if tier == 'AA':
                aa_count += 1

            changed = (
                prior_tiers.get((sid, gid)) not in (None, 'P')
                and prior_tiers.get((sid, gid)) in ('AA','A','B','C','D')
                and tier != prior_tiers.get((sid, gid))
            )

            all_history_rows.append((
                sid, gid, score_week, tier,
                round(vel, 4), round(pct, 4) if pct is not None else None,
                store_weeks[sid], changed
            ))

            # Populate denormalized store column if applicable
            if gid in STORE_TIER_COLUMNS:
                store_col_updates[sid][STORE_TIER_COLUMNS[gid]] = tier

        log(f"  {gid:<25} {len(eligible):>5} eligible  AA={aa_count}")

    # ── Write store_group_velocity ────────────────────────────
    log(f"\nWriting {len(all_history_rows):,} group velocity records...")
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO store_group_velocity
            (store_id, sku_group_id, week_start, dynamic_tier,
             ewma_score, percentile, weeks_of_data, tier_changed)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (store_id, sku_group_id, week_start) DO UPDATE SET
            dynamic_tier  = EXCLUDED.dynamic_tier,
            ewma_score    = EXCLUDED.ewma_score,
            percentile    = EXCLUDED.percentile,
            weeks_of_data = EXCLUDED.weeks_of_data,
            tier_changed  = EXCLUDED.tier_changed
    """, all_history_rows, page_size=1000)

    # ── Update denormalized tier columns on stores ────────────
    log("Updating store tier columns...")
    valid_cols = set(STORE_TIER_COLUMNS.values())
    for sid, cols in store_col_updates.items():
        set_parts = ', '.join(f"{col} = %s" for col in cols if col in valid_cols)
        vals = [v for c, v in cols.items() if c in valid_cols]
        if set_parts:
            cur.execute(f"UPDATE stores SET {set_parts} WHERE store_id = %s",
                        vals + [sid])

    conn.commit()
    log("Group velocity scoring complete.")
    cur.close()
    conn.close()

if __name__ == "__main__":
    run_group_scoring(verbose=True)
