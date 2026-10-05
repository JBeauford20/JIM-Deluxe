"""
JIM Deluxe — Velocity Scoring Engine
=====================================
Calculates dynamic store velocity tiers from sales history.

Algorithm:
  1. Pull weekly sales totals per store from the sales table
  2. Process chronologically (oldest → newest) to build EWMA baseline
  3. Rank all eligible stores by current smoothed velocity
  4. Assign tiers (AA/A/B/C/D) with hysteresis bands
  5. Write to velocity_history + update stores table

EWMA: V_t = ALPHA * S_t + (1 - ALPHA) * V_{t-1}
      ALPHA = 0.10  →  slow decay, half-life ~6.6 weeks
"""

from pathlib import Path
from datetime import date, timedelta
import psycopg2
import psycopg2.extras

# ── Constants ─────────────────────────────────────────────────
ALPHA            = 0.10    # EWMA smoothing factor (slow/conservative)
MIN_WEEKS        = 6       # weeks of data required before real tier assigned
PROVISIONAL_TIER = 'P'     # tier label for stores with < MIN_WEEKS data

# Percentile boundaries for each tier (upper bound, exclusive)
# e.g. AA = top 0.5% → percentile rank >= 99.5
TIER_BOUNDS = {
    'AA': 99.50,   # top 0.5%
    'A':  90.00,   # 0.5% – 10%
    'B':  70.00,   # 10%  – 30%
    'C':  40.00,   # 30%  – 60%
    'D':   0.00,   # bottom 40%
}

# Hysteresis: store must cross boundary by this many percentile points
# to actually change tier. Prevents bouncing at boundaries.
HYSTERESIS = 0.2   # ± 0.2 percentile points (~4 stores at 2,000 store scale)

# ── DB connection ─────────────────────────────────────────────
from db_conn import get_conn

# ── Tier assignment ───────────────────────────────────────────
def assign_tier(percentile: float) -> str:
    """Assign a tier based on percentile rank (0–100, higher = better)."""
    if percentile >= TIER_BOUNDS['AA']:
        return 'AA'
    elif percentile >= TIER_BOUNDS['A']:
        return 'A'
    elif percentile >= TIER_BOUNDS['B']:
        return 'B'
    elif percentile >= TIER_BOUNDS['C']:
        return 'C'
    else:
        return 'D'

def apply_hysteresis(current_tier: str, new_tier: str,
                     percentile: float) -> str:
    """
    Only change tier if the store has crossed the boundary by
    more than HYSTERESIS points. Prevents flickering at boundaries.
    """
    if current_tier is None or current_tier == PROVISIONAL_TIER:
        return new_tier   # no prior tier — assign freely

    tier_order = ['D', 'C', 'B', 'A', 'AA']
    curr_idx = tier_order.index(current_tier) if current_tier in tier_order else -1
    new_idx  = tier_order.index(new_tier)  if new_tier  in tier_order else -1

    if new_idx == curr_idx:
        return current_tier   # same tier, no change

    # Moving UP: only if percentile is clearly above the boundary
    if new_idx > curr_idx:
        boundary = TIER_BOUNDS[new_tier]
        return new_tier if percentile >= boundary + HYSTERESIS else current_tier

    # Moving DOWN: only if percentile is clearly below the boundary
    if new_idx < curr_idx:
        boundary = TIER_BOUNDS[current_tier]
        return new_tier if percentile < boundary - HYSTERESIS else current_tier

    return current_tier

# ── Main scoring run ──────────────────────────────────────────
def run_velocity_scoring(score_week: date = None, verbose: bool = True,
                         min_weeks_override: int = None):
    """
    Run the velocity scoring engine for a given week.
    Defaults to the most recent complete week.
    """
    if score_week is None:
        # Most recent Monday
        today = date.today()
        score_week = today - timedelta(days=today.weekday())

    log = print if verbose else lambda *a, **k: None
    log(f"\n{'='*60}")
    log(f"Velocity Engine — scoring week: {score_week}")
    effective_min_weeks = min_weeks_override if min_weeks_override is not None else MIN_WEEKS
    log(f"ALPHA={ALPHA}  MIN_WEEKS={effective_min_weeks}  HYSTERESIS={HYSTERESIS}")
    log(f"{'='*60}\n")

    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # ── Step 1: Pull all weekly sales aggregates ──────────────
    # Aggregate sales by store × week (week_start = Monday)
    log("Loading weekly sales aggregates...")
    cur.execute("""
        SELECT
            store_id,
            date_trunc('week', sale_date)::date AS week_start,
            SUM(units_sold)                      AS units_sold
        FROM sales
        WHERE sale_date < %s + INTERVAL '7 days'
        GROUP BY store_id, date_trunc('week', sale_date)::date
        ORDER BY store_id, week_start
    """, (score_week,))
    rows = cur.fetchall()
    log(f"  {len(rows)} store-week records loaded.")

    # ── Step 2: Build EWMA per store ──────────────────────────
    # Process chronologically per store: oldest week first
    log("Computing EWMA velocities...")

    store_ewma   = {}   # store_id → current smoothed velocity
    store_weeks  = {}   # store_id → number of weeks of data
    store_history = {}  # store_id → {week: velocity} for history writes

    for row in rows:
        sid   = row['store_id']
        week  = row['week_start']
        units = float(row['units_sold'])

        if sid not in store_ewma:
            # Initialise: first week's raw sales is the starting velocity
            store_ewma[sid]    = units
            store_weeks[sid]   = 1
            store_history[sid] = {week: units}
        else:
            # EWMA update: V_t = α × S_t + (1-α) × V_{t-1}
            v_prev = store_ewma[sid]
            v_new  = ALPHA * units + (1 - ALPHA) * v_prev
            store_ewma[sid]    = v_new
            store_weeks[sid]  += 1
            store_history[sid][week] = v_new

    log(f"  {len(store_ewma)} stores have velocity data.")

    # ── Step 3: Rank eligible stores ──────────────────────────
    # Only rank stores with >= MIN_WEEKS of data
    eligible = {
        sid: vel for sid, vel in store_ewma.items()
        if store_weeks[sid] >= effective_min_weeks
    }
    provisional = {
        sid: vel for sid, vel in store_ewma.items()
        if store_weeks[sid] < effective_min_weeks
    }

    log(f"  Eligible (>={effective_min_weeks} weeks): {len(eligible)}")
    log(f"  Provisional (<{effective_min_weeks} weeks): {len(provisional)}")

    # Compute percentile rank for each eligible store
    # percentile = (number of stores with lower velocity / total eligible) * 100
    sorted_velocities = sorted(eligible.values())
    n = len(sorted_velocities)

    def percentile_rank(velocity: float) -> float:
        """Percentile rank 0–100, higher = better."""
        if n == 0:
            return 0.0
        below = sum(1 for v in sorted_velocities if v < velocity)
        return (below / n) * 100.0

    store_percentiles = {
        sid: percentile_rank(vel) for sid, vel in eligible.items()
    }

    # ── Step 4: Pull current tiers for hysteresis ─────────────
    log("Fetching current dynamic tiers for hysteresis...")
    cur.execute("""
        SELECT store_id, dynamic_velocity_tier, volume_code
        FROM stores
        WHERE store_id = ANY(%s)
    """, (list(store_ewma.keys()),))
    store_meta = {r['store_id']: dict(r) for r in cur.fetchall()}

    # ── Step 5: Assign tiers ──────────────────────────────────
    log("Assigning tiers...")
    tier_counts = {t: 0 for t in ['AA', 'A', 'B', 'C', 'D', PROVISIONAL_TIER]}
    results = {}   # store_id → {tier, score, percentile, weeks, changed}

    # Eligible stores
    for sid, velocity in eligible.items():
        pct          = store_percentiles[sid]
        raw_tier     = assign_tier(pct)
        prior_tier   = (store_meta.get(sid) or {}).get('dynamic_velocity_tier')
        final_tier   = apply_hysteresis(prior_tier, raw_tier, pct)
        static_tier  = (store_meta.get(sid) or {}).get('volume_code')
        # Only flag as changed if there was a REAL prior tier that actually flipped
        # null → tier = first assignment, not a change
        # PROVISIONAL → tier = graduation, not a change
        tier_changed = (
            prior_tier is not None
            and prior_tier != PROVISIONAL_TIER
            and prior_tier in ('AA', 'A', 'B', 'C', 'D')
            and final_tier != prior_tier
        )

        results[sid] = {
            'tier':         final_tier,
            'score':        round(velocity, 4),
            'percentile':   round(pct, 4),
            'weeks':        store_weeks[sid],
            'changed':      tier_changed,
            'static_tier':  static_tier,
        }
        tier_counts[final_tier] = tier_counts.get(final_tier, 0) + 1

    # Provisional stores
    for sid, velocity in provisional.items():
        static_tier = (store_meta.get(sid) or {}).get('volume_code')
        results[sid] = {
            'tier':        PROVISIONAL_TIER,
            'score':       round(velocity, 4),
            'percentile':  None,
            'weeks':       store_weeks[sid],
            'changed':     False,
            'static_tier': static_tier,
        }
        tier_counts[PROVISIONAL_TIER] += 1

    # ── Step 6: Write velocity_history ────────────────────────
    log("Writing velocity_history...")
    history_rows = []
    for sid, r in results.items():
        history_rows.append((
            sid,
            score_week,
            r['static_tier'],
            r['tier'],
            r['score'],
            r['changed'],
            r['weeks'],
            r['score'],   # avg_weekly_units = current smoothed velocity
        ))

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO velocity_history
            (store_id, week_start, static_tier, dynamic_tier,
             dynamic_score, tier_changed, weeks_of_sales_data,
             avg_weekly_units)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (store_id, week_start) DO UPDATE SET
            static_tier         = EXCLUDED.static_tier,
            dynamic_tier        = EXCLUDED.dynamic_tier,
            dynamic_score       = EXCLUDED.dynamic_score,
            tier_changed        = EXCLUDED.tier_changed,
            weeks_of_sales_data = EXCLUDED.weeks_of_sales_data,
            avg_weekly_units    = EXCLUDED.avg_weekly_units
    """, history_rows, page_size=500)

    # ── Step 7: Update stores table ───────────────────────────
    log("Updating stores.dynamic_velocity_tier...")
    store_update_rows = [
        (r['tier'], r['score'], score_week, sid)
        for sid, r in results.items()
    ]
    psycopg2.extras.execute_batch(cur, """
        UPDATE stores SET
            dynamic_velocity_tier    = %s,
            dynamic_velocity_score   = %s,
            velocity_last_calculated = %s
        WHERE store_id = %s
    """, store_update_rows, page_size=500)

    conn.commit()

    # ── Summary ───────────────────────────────────────────────
    log("\n-- Results ------------------------------------------")
    for tier in ['AA', 'A', 'B', 'C', 'D', PROVISIONAL_TIER]:
        count = tier_counts.get(tier, 0)
        bar   = '#' * min(count // 10, 50)
        log(f"  {tier:<4} {count:>5} stores  {bar}")

    changed = sum(1 for r in results.values() if r['changed'])
    log(f"\n  Stores that changed tier this week: {changed}")

    if changed > 0:
        log("\n  Tier changes:")
        for sid, r in results.items():
            if r['changed']:
                prior = (store_meta.get(sid) or {}).get('dynamic_velocity_tier', '?')
                log(f"    Store {sid:>6}  {prior} -> {r['tier']}  "
                    f"(score={r['score']:.1f}, pct={r['percentile']:.1f})")

    cur.close()
    conn.close()
    log("\nVelocity scoring complete.")
    return results


# ── CLI ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    score_week = None
    if len(sys.argv) > 1:
        score_week = date.fromisoformat(sys.argv[1])
    run_velocity_scoring(score_week=score_week, verbose=True)
