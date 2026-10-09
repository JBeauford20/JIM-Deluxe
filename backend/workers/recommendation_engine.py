"""
JIM Deluxe — Recommendation Engine
=====================================
Given an availability batch, produces store-level orders with full
cart-by-cart, shelf-by-shelf packing plans and truck load assignments.

Algorithm:
  Stage 0   — Store Clock: compute static grade + dynamic DUE gate per store.
               Stores that have not earned out their last delivery (not DUE)
               are excluded from allocation entirely.
  Stage 1   — Score every eligible store (velocity + recency + pace + breadth)
  Stage 2+3 — For each store, score SKUs using tier-avg group velocity,
               then build mixed carts (max 2 shelves/SKU, fill greedily)
  Stage 4.5 — Consolidate cart configs down to ≤ MAX_CART_CONFIGS unique codes
  Stage 5   — Build loads (45 carts/truck, grouped by geography)
  Stage 6   — Write all output to Supabase

Usage:
  python recommendation_engine.py <batch_id> [--preview] [--week YYYY-MM-DD]
"""

import sys, math, uuid
from pathlib import Path
from datetime import date, timedelta
from collections import defaultdict
import psycopg2, psycopg2.extras

try:
    from playbook_engine import PlaybookEngine, load_adjustments
except ImportError:
    class PlaybookEngine:
        def __init__(self, *a, **kw): pass
        def stage1_priority_bonus(self, *a): return 0.0
        def stage1_cart_target(self, sid, default): return default
        def stage2_sku_score_modifier(self, sid, sku_id, vel, avail): return vel, avail
        def print_summary(self, log=print): log("  Playbook: not available (import error)")
    def load_adjustments(cur, week): return []

BASE = Path(__file__).parent
from db_conn import get_conn

# ── Algorithm weights ─────────────────────────────────────────
W_VELOCITY  = 0.40   # overall store EWMA velocity score
W_RECENCY   = 0.30   # days since last delivery
W_PACE      = 0.20   # sell-through rate: scans ÷ deliveries (12-week window)
W_BREADTH   = 0.10   # how many SKU groups the store sells well

W_SKU_VEL   = 0.65   # tier-avg group velocity for this SKU
W_SKU_AVAIL = 0.35   # availability weight (clears more of what we have)

# ── Operational constants ─────────────────────────────────────
CART_SHELVES      = 5
MAX_SHELVES_SKU   = 2
CARTS_PER_TRUCK   = 45
MIN_CARTS         = 2
JUTE_CERAMIC_SPLIT = 0.5
PACE_WINDOW_DAYS  = 84   # 12-week rolling window for pace / dynamic clock
MAX_CART_CONFIGS  = 25   # hard cap on unique cart config codes per run

TIER_TARGET_CARTS = {'AA': 6, 'A': 4, 'B': 3, 'C': 2, 'D': 2, 'P': 2}

# ── Store Clock constants (manager-editable) ──────────────────
# Ken's formula: clock = ROUNDUP(STATIC_DROP_CARTS × STATIC_CART_WHL × TARGET_PACE / scans_wk)
# Josh's DUE gate: due = weeks_since_last >= CEIL(actual_delivery_whl × TARGET_PACE / scans_12wk)
STATIC_DROP_CARTS         = 2        # minimum 2-cart standard drop for letter/grade
STATIC_CART_WHL           = 2534.00  # wholesale value of a standard 213-unit cart
TARGET_PACE_CLOCK         = 0.70     # 70% sell-through target
CLOCK_MIN_WEEKS           = 2        # floor on any clock value
CLOCK_MAX_WEEKS           = 13       # ceiling — beyond this product has likely aged out
CLOCK_OVERSTOCK_FLAG_WKS  = 7        # dynamic clock > this → overshipped flag
CLOCK_LONG_WINDOW_DAYS    = 280      # ~40 weeks for static grade (stable store property)
WHOLESALE_PRICE_FALLBACK  = 11.89    # per-unit fallback if SKU has no price on file

RECENCY_CURVE = [
    (0,   0.00),
    (7,   0.20),
    (14,  0.55),
    (21,  0.85),
    (28,  1.00),
]

# ── Scoring helpers ───────────────────────────────────────────

def recency_score(days):
    if days is None:
        return 0.50
    days = max(0, days)
    for i in range(len(RECENCY_CURVE) - 1):
        d0, s0 = RECENCY_CURVE[i]
        d1, s1 = RECENCY_CURVE[i + 1]
        if d0 <= days < d1:
            t = (days - d0) / (d1 - d0)
            return s0 + t * (s1 - s0)
    return 1.0

def normalize(val, min_val, max_val):
    if max_val <= min_val:
        return 0.5
    return max(0.0, min(1.0, (val - min_val) / (max_val - min_val)))

# ── Store Clock helpers ───────────────────────────────────────

def clock_grade(raw_weeks):
    """Letter band from the unrounded clock weeks (Ken's bands)."""
    if raw_weeks <= 3:  return 'A'
    if raw_weeks <= 5:  return 'B'
    if raw_weeks <= 8:  return 'C'
    if raw_weeks <= 12: return 'D'
    return 'F'

def static_clock(scans_per_week_whl_long):
    """
    Store property — how many weeks does this store take to earn a standard 2-cart drop?
    Used for letter/grade. Stable; recomputed each run from the long sales window.
    """
    if not scans_per_week_whl_long or scans_per_week_whl_long <= 0:
        return CLOCK_MAX_WEEKS, 'F'
    raw = (STATIC_DROP_CARTS * STATIC_CART_WHL * TARGET_PACE_CLOCK) / scans_per_week_whl_long
    weeks = max(CLOCK_MIN_WEEKS, min(CLOCK_MAX_WEEKS, math.ceil(raw)))
    return weeks, clock_grade(raw)

def dynamic_clock(last_delivery_whl, scans_per_week_whl_12w):
    """
    This-week DUE test — has the store earned out its actual last delivery?
    Accounts for real cart count and real product mix value (using per-SKU wholesale prices).
    """
    if not last_delivery_whl or last_delivery_whl <= 0:
        return CLOCK_MIN_WEEKS   # no delivery on file → always due
    if not scans_per_week_whl_12w or scans_per_week_whl_12w <= 0:
        return CLOCK_MAX_WEEKS   # no scan data → conservatively not due
    raw = (last_delivery_whl * TARGET_PACE_CLOCK) / scans_per_week_whl_12w
    return max(CLOCK_MIN_WEEKS, min(CLOCK_MAX_WEEKS, math.ceil(raw)))

# ── Config consolidation ──────────────────────────────────────

def consolidate_configs(all_orders, max_configs=MAX_CART_CONFIGS):
    """
    Assign canonical CC config codes to every cart in all_orders.

    1. Compute a config signature per cart: sorted (sku_id, num_shelves) tuples.
    2. If unique signatures > max_configs, iteratively merge the rarest config
       into its most similar neighbor (Jaccard similarity on SKU sets) until
       we are at or below the limit.
    3. Assign human-readable codes: CC1 (most common) through CCN.

    Returns:
        config_codes: dict  (store_id, cart_index) → "CC7" etc.
        unique_count: int   number of unique configs after consolidation
        before_count: int   number of unique configs before consolidation
    """
    def signature(cart):
        return tuple(sorted(
            (sku_id, num_shelves)
            for sku_id, num_shelves, _units, _kit in cart['contents']
        ))

    def jaccard(a, b):
        sa = {x[0] for x in a}
        sb = {x[0] for x in b}
        u = sa | sb
        return len(sa & sb) / len(u) if u else 0.0

    # Build signature map
    sig_map   = {}   # (store_id, cart_idx) → signature
    sig_count = defaultdict(int)   # signature → total carts

    for store_id, order in all_orders.items():
        for i, cart in enumerate(order['carts']):
            sig = signature(cart)
            sig_map[(store_id, i)] = sig
            sig_count[sig] += 1

    before_count = len(sig_count)
    active = dict(sig_count)          # signature → total carts (mutable)
    canonical = {s: s for s in active}  # signature → its current canonical sig

    # Greedy merge: rarest → nearest neighbour
    while len(active) > max_configs:
        rarest = min(active, key=lambda s: active[s])
        others = [s for s in active if s != rarest]
        if not others:
            break
        best = max(others, key=lambda s: jaccard(rarest, s))
        active[best] += active.pop(rarest)
        for s in list(canonical):
            if canonical[s] == rarest:
                canonical[s] = best

    # Assign CC codes, most common first
    cc_codes = {
        sig: f"CC{i+1}"
        for i, sig in enumerate(sorted(active, key=lambda s: -active[s]))
    }

    config_codes = {
        key: cc_codes[canonical[sig]]
        for key, sig in sig_map.items()
    }

    return config_codes, len(active), before_count

# ── Cart building ─────────────────────────────────────────────

def build_carts_for_store(store_id, ranked_skus, inventory,
                           shelf_configs, sku_profiles,
                           target_carts, avail_meta):
    carts     = []
    committed = defaultdict(int)
    flags     = []

    for cart_num in range(1, target_carts + 1):
        shelves_left  = CART_SHELVES
        cart_contents = []
        sku_shelf_ct  = defaultdict(int)

        for sku_id, score in ranked_skus:
            if shelves_left == 0:
                break
            if sku_shelf_ct[sku_id] >= MAX_SHELVES_SKU:
                continue
            profile_id = sku_profiles.get(sku_id)
            if not profile_id or profile_id not in shelf_configs:
                continue
            cfg = shelf_configs[profile_id]
            ups = cfg['units_per_shelf']
            if not ups:
                continue

            avail_net = inventory.get(sku_id, 0) - committed[sku_id]
            if avail_net <= 0:
                continue

            shelves_wanted = min(MAX_SHELVES_SKU - sku_shelf_ct[sku_id], shelves_left)
            units_to_take  = min(shelves_wanted * ups, avail_net)
            if units_to_take <= 0:
                continue

            actual_shelves = math.ceil(units_to_take / ups)
            actual_units   = min(actual_shelves * ups, avail_net)
            actual_shelves = math.ceil(actual_units / ups)

            kit_type = determine_kit_type(sku_id, actual_units, committed, avail_meta)
            cart_contents.append((sku_id, actual_shelves, actual_units, kit_type))
            committed[sku_id]    += actual_units
            sku_shelf_ct[sku_id] += actual_shelves
            shelves_left         -= actual_shelves

        if not cart_contents:
            flags.append(f"Cart {cart_num}: no inventory available to fill")
            break

        shelves_used = CART_SHELVES - shelves_left
        is_full      = shelves_left == 0
        if not is_full and shelves_used > 0:
            flags.append(f"Cart {cart_num}: only {shelves_used}/{CART_SHELVES} shelves filled")

        carts.append({
            'cart_number':  cart_num,
            'cart_type_id': 'cc',
            'shelves_used': shelves_used,
            'total_units':  sum(c[2] for c in cart_contents),
            'is_full':      is_full,
            'is_flagged':   not is_full,
            'flag_reason':  flags[-1] if not is_full else None,
            'contents':     cart_contents,
        })

    return carts, dict(committed)

def determine_kit_type(sku_id, units, committed, avail_meta):
    meta = avail_meta.get(sku_id, {})
    if meta.get('code_level', '3M') == '3M':
        return meta.get('hard_good_type', 'ceramic_kit')
    already_jute    = committed.get(f'{sku_id}_jute', 0)
    already_ceramic = committed.get(f'{sku_id}_ceramic', 0)
    total_so_far    = already_jute + already_ceramic
    jute_target     = math.floor((total_so_far + units) * JUTE_CERAMIC_SPLIT)
    jute_this       = max(0, jute_target - already_jute)
    ceramic_this    = units - jute_this
    if jute_this > 0 and ceramic_this > 0:
        return 'mixed'
    return 'jute_kit' if jute_this > 0 else 'ceramic_kit'

# ── Main engine ───────────────────────────────────────────────

def run(batch_id, shipping_week=None, preview=False, verbose=True):

    log = print if verbose else lambda *a, **k: None

    today = date.today()
    if shipping_week is None:
        days_ahead = (7 - today.weekday()) % 7 or 7
        shipping_week = today + timedelta(days=days_ahead)

    log(f"\n{'='*62}")
    log(f"JIM Recommendation Engine")
    log(f"Batch:   {batch_id}")
    log(f"Week:    {shipping_week}")
    log(f"Weights: vel={W_VELOCITY} rec={W_RECENCY} pace={W_PACE} brd={W_BREADTH}")
    log(f"{'='*62}\n")

    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # ── LOAD DATA ─────────────────────────────────────────────

    log("Loading availability batch...")
    cur.execute("""
        SELECT al.sku_id, SUM(al.units_available) AS units,
               al.code_level, al.hard_good_type, al.kit_selection_required
        FROM availability_lines al
        WHERE al.batch_id = %s
        GROUP BY al.sku_id, al.code_level, al.hard_good_type, al.kit_selection_required
    """, (batch_id,))
    avail_rows = cur.fetchall()
    if not avail_rows:
        raise ValueError(f"No availability lines found for batch {batch_id}")

    inventory  = defaultdict(int)
    avail_meta = {}
    for r in avail_rows:
        inventory[r['sku_id']] += r['units']
        avail_meta[r['sku_id']] = {
            'code_level':             r['code_level'],
            'hard_good_type':         r['hard_good_type'],
            'kit_selection_required': r['kit_selection_required'],
        }

    total_available = sum(inventory.values())
    log(f"  {len(inventory)} SKUs | {total_available:,} total units\n")

    log("Loading store velocity scores...")
    cur.execute("""
        SELECT store_id, dynamic_velocity_score, dynamic_velocity_tier,
               merchant, region, state, market_number
        FROM stores
        WHERE dynamic_velocity_tier IS NOT NULL AND active = true
    """)
    store_rows  = {r['store_id']: dict(r) for r in cur.fetchall()}
    vel_scores  = [float(r['dynamic_velocity_score'])
                   for r in store_rows.values() if r['dynamic_velocity_score']]
    vel_min, vel_max = (min(vel_scores), max(vel_scores)) if vel_scores else (0, 1)
    log(f"  {len(store_rows):,} stores")

    log("Loading delivery recency...")
    cur.execute("""
        SELECT store_id, MAX(delivery_date) AS last_delivery
        FROM deliveries GROUP BY store_id
    """)
    last_delivery = {r['store_id']: r['last_delivery'] for r in cur.fetchall()}

    # JIM order history supersedes raw delivery dates where available
    cur.execute("""
        SELECT o.store_id, MAX(ab.shipping_week_start) AS last_order_week
        FROM orders o
        JOIN recommendation_runs rr ON rr.id = o.run_id
        JOIN availability_batches ab ON ab.id = rr.batch_id
        WHERE o.status != 'draft'
        GROUP BY o.store_id
    """)
    for r in cur.fetchall():
        if r['last_order_week']:
            existing = last_delivery.get(r['store_id'])
            if existing is None or r['last_order_week'] > existing:
                last_delivery[r['store_id']] = r['last_order_week']

    # ── PACE: sell-through rate (scans ÷ deliveries, 12-week window) ──
    log("Loading sell-through pace...")
    cur.execute("""
        WITH del_w AS (
            SELECT store_id, SUM(units_delivered) AS delivered
            FROM deliveries
            WHERE delivery_date >= CURRENT_DATE - INTERVAL '84 days'
            GROUP BY store_id
        ),
        sal_w AS (
            SELECT store_id, SUM(units_sold) AS sold
            FROM sales
            WHERE sale_date >= CURRENT_DATE - INTERVAL '84 days'
            GROUP BY store_id
        )
        SELECT d.store_id,
               d.delivered,
               COALESCE(s.sold, 0) AS sold
        FROM del_w d
        LEFT JOIN sal_w s ON s.store_id = d.store_id
        WHERE d.delivered > 0
    """)
    store_pace = {}
    for r in cur.fetchall():
        store_pace[r['store_id']] = min(1.0, r['sold'] / r['delivered'])
    log(f"  {len(store_pace)} stores with pace data")

    # ── STAGE 0: STORE CLOCK DATA ─────────────────────────────
    log("Stage 0: Loading Store Clock data...")

    # Dollar-weighted scans per week — two windows
    # Long window (~40 wks): stable store property → static grade/letter
    # 12-week window: current pace → dynamic DUE gate
    cur.execute("""
        SELECT s.store_id,
               SUM(s.units_sold * COALESCE(sk.wholesale_price_whl, %s))
                   / %s AS scans_wk_long,
               SUM(CASE WHEN s.sale_date >= CURRENT_DATE - INTERVAL '84 days'
                        THEN s.units_sold * COALESCE(sk.wholesale_price_whl, %s) END)
                   / 12.0 AS scans_wk_12w
        FROM sales s
        JOIN skus sk ON sk.sku_id = s.sku_id
        WHERE s.sale_date >= CURRENT_DATE - INTERVAL '%s days'
        GROUP BY s.store_id
    """, (WHOLESALE_PRICE_FALLBACK, CLOCK_LONG_WINDOW_DAYS / 7.0,
          WHOLESALE_PRICE_FALLBACK, CLOCK_LONG_WINDOW_DAYS))
    store_scan_whl = {r['store_id']: (float(r['scans_wk_long'] or 0),
                                      float(r['scans_wk_12w'] or 0))
                     for r in cur.fetchall()}

    # Last delivery wholesale value per store from raw AGS delivery history
    cur.execute("""
        WITH last_dates AS (
            SELECT store_id, MAX(delivery_date) AS last_date
            FROM deliveries GROUP BY store_id
        )
        SELECT d.store_id,
               SUM(d.units_delivered * COALESCE(sk.wholesale_price_whl, %s)) AS delivery_whl
        FROM deliveries d
        JOIN last_dates ld ON ld.store_id = d.store_id
                           AND d.delivery_date = ld.last_date
        LEFT JOIN skus sk ON sk.sku_id = d.sku_id
        GROUP BY d.store_id
    """, (WHOLESALE_PRICE_FALLBACK,))
    last_delivery_whl = {r['store_id']: float(r['delivery_whl'] or 0)
                         for r in cur.fetchall()}

    # Override with JIM order values where available and more recent
    cur.execute("""
        SELECT o.store_id,
               SUM(ol.approved_qty * COALESCE(sk.wholesale_price_whl, %s)) AS delivery_whl,
               MAX(ab.shipping_week_start) AS last_week
        FROM orders o
        JOIN order_lines ol ON ol.order_id = o.id
        JOIN recommendation_runs rr ON rr.id = o.run_id
        JOIN availability_batches ab ON ab.id = rr.batch_id
        LEFT JOIN skus sk ON sk.sku_id = ol.sku_id
        WHERE o.status IN ('approved', 'exported')
          AND ol.approved_qty > 0
        GROUP BY o.store_id
    """, (WHOLESALE_PRICE_FALLBACK,))
    for r in cur.fetchall():
        if r['delivery_whl']:
            # JIM history takes precedence — it has exact SKU-level pricing
            last_delivery_whl[r['store_id']] = float(r['delivery_whl'])

    # Compute clock values for every store
    store_clock = {}
    skipped_not_due = 0
    for store_id, meta in store_rows.items():
        scan_long, scan_12w = store_scan_whl.get(store_id, (0.0, 0.0))
        del_whl = last_delivery_whl.get(store_id, 0.0)
        last_del = last_delivery.get(store_id)
        days_ago = (today - last_del).days if last_del else None
        weeks_ago = (days_ago / 7.0) if days_ago is not None else None

        s_weeks, s_grade = static_clock(scan_long)
        d_weeks = dynamic_clock(del_whl, scan_12w)
        gap = d_weeks - s_weeks
        is_due = (weeks_ago is None) or (weeks_ago >= d_weeks)
        is_over = gap > CLOCK_OVERSTOCK_FLAG_WKS

        store_clock[store_id] = {
            'static_clock':  s_weeks,
            'static_grade':  s_grade,
            'dynamic_clock': d_weeks,
            'clock_gap':     gap,
            'is_due':        is_due,
            'is_overshipped': is_over,
            'last_del_whl':  del_whl,
        }
        if not is_due:
            skipped_not_due += 1

    log(f"  {len(store_rows) - skipped_not_due:,} stores DUE | "
        f"{skipped_not_due:,} not yet due (excluded from allocation)")
    log(f"  Overshipped flags: "
        f"{sum(1 for c in store_clock.values() if c['is_overshipped'])}")

    log("Loading SKU group velocity...")
    cur.execute("""
        SELECT store_id, sku_group_id, ewma_score
        FROM store_group_velocity
        WHERE week_start = (SELECT MAX(week_start) FROM store_group_velocity)
          AND dynamic_tier IS NOT NULL
    """)
    group_vel = defaultdict(dict)
    for r in cur.fetchall():
        group_vel[r['store_id']][r['sku_group_id']] = float(r['ewma_score'] or 0)

    cur.execute("""
        SELECT s.sku_id, s.family, s.legacy_product_group
        FROM skus s WHERE s.sku_id = ANY(%s)
    """, (list(inventory.keys()),))
    sku_family = {r['sku_id']: (r['family'], r['legacy_product_group'])
                  for r in cur.fetchall()}

    def sku_to_group_ids(sku_id):
        fam, tier = sku_family.get(sku_id, (None, None))
        if not fam:
            return []
        groups = []
        fam_lower  = fam.lower().replace(' ', '_').replace('-', '_')
        tier_lower = (tier or 'premium').lower().replace(' ', '_')
        groups.append(f"{fam_lower}_{tier_lower}")
        if '9cm'  in fam.lower(): groups.append('9cm_all')
        if '12cm' in fam.lower(): groups.append('12cm_all')
        if '17cm' in fam.lower(): groups.append('17cm_premium')
        if 'h2o'  in fam.lower(): groups.append('h2o_all')
        if tier in ('Collectors', 'Specialty', 'Boutique', 'Rare Collectors'):
            groups.append('boutique_all')
        return groups

    cur.execute("""
        SELECT sc.profile_id, sc.units_per_shelf, sc.trays_per_shelf,
               sc.max_shelves, sc.units_per_cart,
               pp.units_per_tray, spp.sku_id
        FROM shelf_configurations sc
        JOIN sku_packing_profiles spp ON spp.profile_id = sc.profile_id
        JOIN packing_profiles pp ON pp.id = sc.profile_id
        WHERE sc.cart_type_id = 'cc' AND sc.approved = true
    """)
    shelf_configs = {}
    sku_profiles  = {}
    for r in cur.fetchall():
        shelf_configs[r['profile_id']] = {
            'units_per_shelf':  r['units_per_shelf'],
            'trays_per_shelf':  r['trays_per_shelf'],
            'units_per_tray':   r['units_per_tray'],
            'max_shelves':      r['max_shelves'] or CART_SHELVES,
            'units_per_cart':   r['units_per_cart'],
        }
        if r['sku_id'] not in sku_profiles:
            sku_profiles[r['sku_id']] = r['profile_id']

    log(f"  {len(shelf_configs)} approved shelf configs")

    log("Loading weekly playbook...")
    adjustments = load_adjustments(cur, str(shipping_week))
    playbook    = PlaybookEngine(adjustments, store_rows, sku_family)
    playbook.print_summary(log)

    # ── STAGE 1: SCORE STORES ─────────────────────────────────
    log("\nStage 1: Scoring stores (velocity + recency + pace + breadth)...")
    store_scores = {}

    for store_id, meta in store_rows.items():
        raw_vel  = float(meta['dynamic_velocity_score'] or 0)
        vel_norm = normalize(raw_vel, vel_min, vel_max)

        last_del  = last_delivery.get(store_id)
        days_ago  = (today - last_del).days if last_del else None
        rec_score = recency_score(days_ago)

        # Pace: sell-through rate — neutral (0.5) when no delivery history
        pace_ratio = store_pace.get(store_id)
        pace_sc    = pace_ratio if pace_ratio is not None else 0.5

        store_groups = group_vel.get(store_id, {})
        breadth_raw  = sum(1 for gid, sc in store_groups.items()
                           if sc > 0 and gid.endswith('_all'))
        breadth_norm = normalize(breadth_raw, 0, 5)

        priority = (vel_norm     * W_VELOCITY +
                    rec_score    * W_RECENCY  +
                    pace_sc      * W_PACE     +
                    breadth_norm * W_BREADTH)

        priority = min(1.0, priority + playbook.stage1_priority_bonus(store_id))

        clk = store_clock.get(store_id, {})
        store_scores[store_id] = {
            'priority':        round(priority, 6),
            'vel_component':   round(vel_norm  * W_VELOCITY, 6),
            'rec_component':   round(rec_score * W_RECENCY,  6),
            'pace_component':  round(pace_sc   * W_PACE,     6),
            'brd_component':   round(breadth_norm * W_BREADTH, 6),
            'pace_ratio':      round(pace_ratio, 4) if pace_ratio is not None else None,
            'days_since':      days_ago,
            'tier':            meta['dynamic_velocity_tier'],
            'merchant':        meta['merchant'],
            'region':          meta['region'],
            'state':           meta['state'],
            'market_number':   meta['market_number'],
            # Store Clock
            'static_clock':    clk.get('static_clock'),
            'static_grade':    clk.get('static_grade'),
            'dynamic_clock':   clk.get('dynamic_clock'),
            'clock_gap':       clk.get('clock_gap'),
            'is_due':          clk.get('is_due', True),
            'is_overshipped':  clk.get('is_overshipped', False),
        }

    ranked_stores = sorted(store_scores.items(), key=lambda x: -x[1]['priority'])
    log(f"  {len(ranked_stores):,} stores scored")

    # ── PRE-COMPUTE TIER SKU TEMPLATES ────────────────────────
    # Average group velocity per tier so same-tier stores get the same SKU
    # priority ranking → naturally fewer unique cart configs.
    log("Pre-computing tier SKU templates...")
    tier_gv_sum   = defaultdict(lambda: defaultdict(float))
    tier_gv_count = defaultdict(lambda: defaultdict(int))
    for store_id, meta in store_rows.items():
        tier = meta['dynamic_velocity_tier']
        if not tier or tier == 'P':
            continue
        for gid, score in group_vel.get(store_id, {}).items():
            tier_gv_sum[tier][gid]   += score
            tier_gv_count[tier][gid] += 1

    tier_avg_gv = {
        tier: {gid: tier_gv_sum[tier][gid] / tier_gv_count[tier][gid]
               for gid in tier_gv_sum[tier]}
        for tier in tier_gv_sum
    }
    log(f"  Templates ready for tiers: {sorted(tier_avg_gv.keys())}")

    # ── STAGES 2+3: ALLOCATE & BUILD CARTS ───────────────────
    log("Stages 2+3: Allocating inventory and building carts...")
    all_orders    = {}
    avail_max     = total_available

    for store_id, score_meta in ranked_stores:
        if sum(inventory.values()) == 0:
            break

        # ── STORE CLOCK DUE GATE ─────────────────────────────
        # If this store has not earned out its last delivery, skip it.
        # This is a hard binary gate — not a scoring penalty.
        if not score_meta.get('is_due', True):
            continue

        tier = score_meta['tier']

        # Stage 2: Score SKUs using TIER-AVERAGE group velocities.
        # All stores in the same tier see the same velocity ranking,
        # which caps unique config count to ≈ number of active tiers.
        store_avg_gv    = tier_avg_gv.get(tier, group_vel.get(store_id, {}))
        max_avg_gv      = max(store_avg_gv.values(), default=1) or 1
        avail_max_units = max(inventory.values()) if inventory else 1

        sku_scores = {}
        for sku_id, avail_units in inventory.items():
            if avail_units <= 0:
                continue
            group_ids   = sku_to_group_ids(sku_id)
            group_score = max((store_avg_gv.get(gid, 0) for gid in group_ids), default=0)
            vel_norm_sku  = normalize(group_score, 0, max_avg_gv)
            avail_norm    = avail_units / avail_max_units

            vel_norm_sku, avail_norm = playbook.stage2_sku_score_modifier(
                store_id, sku_id, vel_norm_sku, avail_norm
            )
            sku_scores[sku_id] = vel_norm_sku * W_SKU_VEL + avail_norm * W_SKU_AVAIL

        ranked_skus = sorted(sku_scores.items(), key=lambda x: -x[1])
        if not ranked_skus:
            continue

        # Stage 3: Build carts
        target_carts = playbook.stage1_cart_target(
            store_id, TIER_TARGET_CARTS.get(tier, MIN_CARTS)
        )
        carts, committed = build_carts_for_store(
            store_id, ranked_skus, inventory,
            shelf_configs, sku_profiles, target_carts, avail_meta
        )
        if not carts:
            continue

        total_carts  = len(carts)
        is_exception = False
        if total_carts < MIN_CARTS:
            days_ago = score_meta['days_since']
            if days_ago is not None and days_ago <= 7 and tier in ('AA', 'A', 'B'):
                is_exception = True
            elif tier == 'D':
                is_exception = True
            else:
                continue

        for sku_id, units in committed.items():
            if '_jute' not in str(sku_id) and '_ceramic' not in str(sku_id):
                inventory[sku_id] = max(0, inventory.get(sku_id, 0) - units)

        all_orders[store_id] = {
            'score_meta':  score_meta,
            'carts':       carts,
            'committed':   committed,
            'total_carts': total_carts,
            'total_units': sum(c['total_units'] for c in carts),
            'is_exception': is_exception,
            'sku_scores':  {k: round(v, 4) for k, v in sku_scores.items()},
        }

    log(f"  {len(all_orders):,} stores allocated")
    log(f"  {sum(o['total_carts'] for o in all_orders.values()):,} total carts")
    allocated_units = sum(o['total_units'] for o in all_orders.values())
    clearance = (allocated_units / total_available * 100) if total_available else 0
    log(f"  {allocated_units:,} / {total_available:,} units ({clearance:.1f}% clearance)")

    # ── STAGE 4.5: CONFIG CONSOLIDATION ──────────────────────
    log(f"\nStage 4.5: Consolidating cart configs (target: ≤{MAX_CART_CONFIGS})...")
    cart_config_codes, unique_after, unique_before = consolidate_configs(all_orders)
    log(f"  {unique_before} unique configs → {unique_after} after consolidation")

    # ── STAGE 5: BUILD LOADS ──────────────────────────────────
    log("\nStage 5: Building truck loads...")

    def load_sort_key(store_id):
        meta = all_orders[store_id]['score_meta']
        return (
            meta['merchant']      or 'ZZZ',
            meta['region']        or 'ZZZ',
            meta['state']         or 'ZZ',
            meta['market_number'] or 9999,
            -all_orders[store_id]['score_meta']['priority'],
        )

    sorted_for_loads    = sorted(all_orders.keys(), key=load_sort_key)
    loads_out           = []
    current_load_carts  = 0
    current_load_stores = []
    load_number         = 1

    def flush_load():
        nonlocal current_load_carts, current_load_stores, load_number
        if not current_load_stores:
            return
        meta = all_orders[current_load_stores[0]]['score_meta']
        util = current_load_carts / CARTS_PER_TRUCK * 100
        loads_out.append({
            'load_number':  load_number,
            'load_name':    f"WK{shipping_week.isocalendar()[1]}-L{load_number}",
            'merchant':     meta['merchant'],
            'region':       meta['region'],
            'cart_count':   current_load_carts,
            'store_count':  len(current_load_stores),
            'utilization':  round(util, 1),
            'is_flagged':   util < 70,
            'flag_reason':  f"Under-utilized ({util:.0f}%)" if util < 70 else None,
            'stores':       list(current_load_stores),
        })
        load_number          += 1
        current_load_carts    = 0
        current_load_stores   = []

    for store_id in sorted_for_loads:
        store_carts = all_orders[store_id]['total_carts']
        if current_load_carts + store_carts > CARTS_PER_TRUCK:
            flush_load()
        current_load_carts += store_carts
        current_load_stores.append(store_id)
    flush_load()
    log(f"  {len(loads_out)} loads built")

    # ── SUMMARY ───────────────────────────────────────────────
    log(f"\n{'='*62}")
    log(f"RECOMMENDATION SUMMARY")
    log(f"{'='*62}")
    log(f"  Stores served:      {len(all_orders):,}")
    log(f"  Total carts:        {sum(o['total_carts'] for o in all_orders.values()):,}")
    log(f"  Units allocated:    {allocated_units:,}")
    log(f"  Inventory clearance:{clearance:.1f}%")
    log(f"  Truck loads:        {len(loads_out)}")
    log(f"  Unique cart configs:{unique_after} (was {unique_before})")
    log(f"\n  Top 10 stores by priority:")
    log(f"  {'Store':<8} {'Tier':<4} {'Grade':<6} {'SClock':>6} {'DClock':>7} "
        f"{'Pace':>6} {'Carts':>6} {'Days':>6}")
    log(f"  {'-'*60}")
    for store_id, data in list(all_orders.items())[:10]:
        sm = data['score_meta']
        pace_pct = f"{sm['pace_ratio']*100:.0f}%" if sm.get('pace_ratio') is not None else "  --"
        flag = " !" if sm.get('is_overshipped') else "  "
        log(f"  {store_id:<8} {sm['tier']:<4} "
            f"{sm.get('static_grade','?'):<6} "
            f"{str(sm.get('static_clock','?')):>6} "
            f"{str(sm.get('dynamic_clock','?')):>7}"
            f"{flag} {pace_pct:>6} {data['total_carts']:>6} "
            f"{str(sm['days_since'] or 'new'):>6}")

    if preview:
        log("\nPREVIEW MODE — nothing written to database.")
        conn.close()
        return None

    # ── WRITE TO DATABASE ─────────────────────────────────────
    log("\nWriting to database...")
    conn.rollback()
    conn.autocommit = False

    cur.execute("""
        INSERT INTO recommendation_runs
            (batch_id, shipping_week, stores_scored, stores_served,
             total_carts, total_units, inventory_clearance,
             weight_velocity, weight_recency, weight_breadth, weight_pace,
             weight_sku_velocity, weight_sku_avail, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft')
        RETURNING id
    """, (
        batch_id, shipping_week,
        len(ranked_stores), len(all_orders),
        sum(o['total_carts'] for o in all_orders.values()), allocated_units,
        round(clearance, 2),
        W_VELOCITY, W_RECENCY, W_BREADTH, W_PACE, W_SKU_VEL, W_SKU_AVAIL
    ))
    run_id = cur.fetchone()['id']
    log(f"  Run ID: {run_id}")

    # Orders
    order_id_map = {}
    order_rows   = []
    for store_id, data in all_orders.items():
        sm  = data['score_meta']
        oid = str(uuid.uuid4())
        order_id_map[store_id] = oid
        order_rows.append((
            oid, str(run_id), store_id, 'draft',
            sm['priority'],      sm['vel_component'],
            sm['rec_component'], sm['pace_component'],
            sm['brd_component'],
            sm['days_since'],    data['total_carts'],
            data['total_units'], data['is_exception'],
            sm.get('pace_ratio'),
            sm.get('static_clock'),
            sm.get('static_grade'),
            sm.get('dynamic_clock'),
            sm.get('clock_gap'),
            sm.get('is_overshipped', False),
            sm.get('is_due', True),
        ))

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO orders
            (id, run_id, store_id, status, priority_score,
             velocity_component, recency_component, pace_component,
             breadth_component,
             days_since_last_delivery, cart_count, total_units,
             is_one_cart_exception, pace_ratio,
             static_clock_weeks, static_grade,
             dynamic_clock_weeks, clock_gap,
             is_overshipped, is_due)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, order_rows, page_size=500)
    log(f"  {len(order_rows)} orders written")

    # Carts + shelves + order lines
    line_rows   = []
    cart_rows   = []
    shelf_rows  = []
    cart_id_map = {}
    cart_seq    = 0
    week_num    = shipping_week.isocalendar()[1]

    for store_id, data in all_orders.items():
        order_id   = order_id_map[store_id]
        sku_totals = defaultdict(int)

        for cart_idx, cart in enumerate(data['carts']):
            cart_seq += 1
            cart_key    = f"WK{week_num:02d}-C{cart_seq:04d}"
            cart_id     = str(uuid.uuid4())
            config_code = cart_config_codes.get((store_id, cart_idx), 'CC1')
            cart_id_map[(store_id, cart['cart_number'])] = cart_id
            eff_max = CART_SHELVES

            cart_rows.append((
                cart_id, order_id, store_id,
                cart['cart_number'], cart['cart_type_id'],
                cart['shelves_used'], CART_SHELVES,
                cart['total_units'],
                cart['shelves_used'] >= eff_max,
                cart['is_flagged'], cart.get('flag_reason'),
                eff_max, cart_key, config_code
            ))

            shelf_pos = 1
            for (sku_id, num_shelves, total_units, kit_type) in cart['contents']:
                profile_id = sku_profiles.get(sku_id)
                cfg        = shelf_configs.get(profile_id, {})
                ups        = cfg.get('units_per_shelf', 1)
                tps        = cfg.get('trays_per_shelf') or \
                             max(1, ups // max(1, cfg.get('units_per_tray', 1)))
                clean_kit  = kit_type if kit_type != 'mixed' else 'ceramic_kit'

                for _ in range(num_shelves):
                    shelf_rows.append((
                        cart_id, shelf_pos, sku_id, profile_id,
                        clean_kit, tps, ups,
                    ))
                    shelf_pos += 1

                sku_totals[sku_id] += total_units

        for sku_id, total_units in sku_totals.items():
            kit = avail_meta.get(sku_id, {}).get('hard_good_type')
            line_rows.append((
                order_id, store_id, sku_id,
                total_units, total_units, None, kit,
                data['sku_scores'].get(sku_id),
            ))

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO order_carts
            (id, order_id, store_id, cart_number, cart_type_id,
             shelves_used, shelves_capacity, total_units,
             is_full, is_flagged, flag_reason, effective_max_shelves,
             cart_key, config_code)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, cart_rows, page_size=500)

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO cart_shelves
            (cart_id, shelf_position, sku_id, profile_id,
             kit_type, tray_count, unit_count)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
    """, shelf_rows, page_size=500)

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO order_lines
            (order_id, store_id, sku_id, recommended_qty, draft_qty,
             approved_qty, kit_type, sku_score)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT DO NOTHING
    """, line_rows, page_size=500)

    log(f"  {len(cart_rows)} carts | {len(shelf_rows)} shelves | {len(line_rows)} order lines")

    # Loads + assignments
    load_id_map = {}
    load_rows   = []
    assign_rows = []

    for load in loads_out:
        lid = str(uuid.uuid4())
        load_id_map[load['load_number']] = lid
        load_rows.append((
            lid, str(run_id), load['load_number'], load['load_name'],
            load['merchant'], load['region'],
            load['cart_count'], load['store_count'],
            CARTS_PER_TRUCK, load['utilization'],
            load['is_flagged'], load.get('flag_reason')
        ))
        for stop_seq, sid in enumerate(load['stores'], start=1):
            for cart in all_orders[sid]['carts']:
                cid = cart_id_map.get((sid, cart['cart_number']))
                if cid:
                    assign_rows.append((lid, cid, sid, stop_seq))

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO loads
            (id, run_id, load_number, load_name, merchant, region,
             cart_count, store_count, capacity, utilization_pct,
             is_flagged, flag_reason)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, load_rows, page_size=200)

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO load_assignments (load_id, order_cart_id, store_id, stop_sequence)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT (load_id, order_cart_id) DO NOTHING
    """, assign_rows, page_size=500)

    log(f"  {len(load_rows)} loads | {len(assign_rows)} cart assignments")
    conn.commit()
    log(f"\nRun complete. ID: {run_id}")
    log(f"Status: DRAFT — review in JIM before approving.")
    cur.close()
    conn.close()
    return str(run_id)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("Usage: python recommendation_engine.py <batch_id> [--preview] [--week YYYY-MM-DD]")
        sys.exit(1)
    batch_id = args[0]
    preview  = '--preview' in args
    week_arg = None
    for i, a in enumerate(args):
        if a == '--week' and i + 1 < len(args):
            week_arg = date.fromisoformat(args[i + 1])
    run(batch_id, shipping_week=week_arg, preview=preview)
