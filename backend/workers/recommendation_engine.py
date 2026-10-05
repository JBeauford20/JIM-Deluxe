"""
JIM Deluxe — Recommendation Engine
=====================================
Given an availability batch, produces store-level orders with full
cart-by-cart, shelf-by-shelf packing plans and truck load assignments.

Algorithm:
  Stage 1 — Score every store (velocity + recency + breadth)
  Stage 2 — For each store, score available SKUs (group velocity + avail weight)
  Stage 3 — Build mixed carts (max 2 shelves/SKU, fill greedily, no air)
  Stage 4 — Enforce minimums (2 carts/store, 1-cart exception rules)
  Stage 5 — Build loads (45 carts/truck, group by geography)
  Stage 6 — Write all output to Supabase

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
    # playbook_engine not available — use no-op stubs
    class PlaybookEngine:
        def __init__(self, *a, **kw): pass
        def stage1_priority_bonus(self, *a): return 0.0
        def stage1_cart_target(self, sid, default): return default
        def stage2_sku_score_modifier(self, sid, sku_id, vel, avail): return vel, avail
        def print_summary(self, log=print): log("  Playbook: not available (import error)")
    def load_adjustments(cur, week): return []

BASE = Path(__file__).parent
ENV_FILE = BASE / "jim.env"

# ── Algorithm weights (tunable) ───────────────────────────────
W_VELOCITY  = 0.45   # overall store velocity score
W_RECENCY   = 0.40   # days since last delivery
W_BREADTH   = 0.15   # how many SKU groups the store sells well

W_SKU_VEL   = 0.65   # store's group velocity for this SKU
W_SKU_AVAIL = 0.35   # availability weight (clears more of what we have)

# ── Cart & load constants ─────────────────────────────────────
CART_SHELVES     = 5     # CC cart max shelves
MAX_SHELVES_SKU  = 2     # max shelves of same SKU per cart
CARTS_PER_TRUCK  = 45
MIN_CARTS        = 2     # minimum carts per store delivery
JUTE_CERAMIC_SPLIT = 0.5 # 50% jute, 50% ceramic for 1M in-house plants

# Cart count targets by dynamic tier
TIER_TARGET_CARTS = {
    'AA': 6,
    'A':  4,
    'B':  3,
    'C':  2,
    'D':  2,
    'P':  2,
}

# Recency score breakpoints: (days, score)
RECENCY_CURVE = [
    (0,   0.00),
    (7,   0.20),
    (14,  0.55),
    (21,  0.85),
    (28,  1.00),
]

from db_conn import get_conn

# ── Scoring helpers ───────────────────────────────────────────

def recency_score(days: int | None) -> float:
    """Convert days-since-last-delivery to a 0-1 recency score."""
    if days is None:
        return 0.50   # never ordered — neutral
    days = max(0, days)
    for i in range(len(RECENCY_CURVE) - 1):
        d0, s0 = RECENCY_CURVE[i]
        d1, s1 = RECENCY_CURVE[i + 1]
        if d0 <= days < d1:
            t = (days - d0) / (d1 - d0)
            return s0 + t * (s1 - s0)
    return 1.0

def normalize(val: float, min_val: float, max_val: float) -> float:
    if max_val <= min_val:
        return 0.5
    return max(0.0, min(1.0, (val - min_val) / (max_val - min_val)))

# ── Cart building ─────────────────────────────────────────────

def build_carts_for_store(store_id, ranked_skus, inventory,
                           shelf_configs, sku_profiles,
                           target_carts, avail_meta):
    """
    Fill target_carts carts for a store using ranked_skus.
    ranked_skus: [(sku_id, score), ...] sorted by score desc
    inventory: {sku_id: units_remaining} — shared mutable dict
    Returns list of cart dicts, and dict of {sku_id: units_allocated}.
    """
    carts        = []
    committed    = defaultdict(int)   # units committed for THIS store
    flags        = []

    for cart_num in range(1, target_carts + 1):
        shelves_left  = CART_SHELVES
        cart_contents = []                # list of (sku_id, shelves, units, kit_type)
        sku_shelf_ct  = defaultdict(int)  # shelves used per SKU THIS cart

        for sku_id, score in ranked_skus:
            if shelves_left == 0:
                break
            if sku_shelf_ct[sku_id] >= MAX_SHELVES_SKU:
                continue

            profile_id = sku_profiles.get(sku_id)
            if not profile_id or profile_id not in shelf_configs:
                continue

            cfg = shelf_configs[profile_id]
            units_per_shelf = cfg['units_per_shelf']
            if not units_per_shelf:
                continue

            avail_net = inventory.get(sku_id, 0) - committed[sku_id]
            if avail_net <= 0:
                continue

            # Shelves we want for this SKU this cart (max 2, max what's left)
            shelves_wanted = min(
                MAX_SHELVES_SKU - sku_shelf_ct[sku_id],
                shelves_left
            )
            units_wanted = shelves_wanted * units_per_shelf
            units_to_take = min(units_wanted, avail_net)

            if units_to_take <= 0:
                continue

            # Round up to full shelf increments
            actual_shelves = math.ceil(units_to_take / units_per_shelf)
            actual_units   = actual_shelves * units_per_shelf
            # Don't over-take
            actual_units   = min(actual_units, avail_net)
            actual_shelves = math.ceil(actual_units / units_per_shelf)

            # Determine kit type for 1M items
            kit_type = determine_kit_type(sku_id, actual_units, committed, avail_meta)

            cart_contents.append((sku_id, actual_shelves, actual_units, kit_type))
            committed[sku_id]      += actual_units
            sku_shelf_ct[sku_id]   += actual_shelves
            shelves_left           -= actual_shelves

        if not cart_contents:
            flags.append(f"Cart {cart_num}: no inventory available to fill")
            break

        shelves_used = CART_SHELVES - shelves_left
        is_full      = shelves_left == 0

        if not is_full and shelves_used > 0:
            flags.append(
                f"Cart {cart_num}: only {shelves_used}/{CART_SHELVES} shelves filled "
                f"(insufficient remaining inventory)"
            )

        total_units = sum(c[2] for c in cart_contents)
        carts.append({
            'cart_number':   cart_num,
            'cart_type_id':  'cc',
            'shelves_used':  shelves_used,
            'total_units':   total_units,
            'is_full':       is_full,
            'is_flagged':    not is_full,
            'flag_reason':   '; '.join(flags[-1:]) if not is_full else None,
            'contents':      cart_contents,   # [(sku_id, shelves, units, kit_type)]
        })

    return carts, dict(committed)

def determine_kit_type(sku_id, units, committed, avail_meta):
    """
    For 1M (in-house) items: return 'jute', 'ceramic', or 'mixed'.
    For 3M (CG) items: return the predetermined hard_good_type.
    Default: 50/50 split.
    """
    meta = avail_meta.get(sku_id, {})
    code_level = meta.get('code_level', '3M')
    if code_level == '3M':
        return meta.get('hard_good_type', 'ceramic_kit')

    # 1M: use 50/50 default
    # Track running totals to alternate jute/ceramic
    already_jute   = committed.get(f'{sku_id}_jute', 0)
    already_ceramic = committed.get(f'{sku_id}_ceramic', 0)
    total_so_far   = already_jute + already_ceramic

    jute_target = math.floor((total_so_far + units) * JUTE_CERAMIC_SPLIT)
    jute_this   = max(0, jute_target - already_jute)
    ceramic_this = units - jute_this

    if jute_this > 0 and ceramic_this > 0:
        return 'mixed'
    elif jute_this > 0:
        return 'jute_kit'
    else:
        return 'ceramic_kit'

# ── Main engine ───────────────────────────────────────────────

def run(batch_id: str, shipping_week: date = None,
        preview: bool = False, verbose: bool = True):

    log = print if verbose else lambda *a, **k: None

    if shipping_week is None:
        today = date.today()
        days_ahead = (7 - today.weekday()) % 7 or 7
        shipping_week = today + timedelta(days=days_ahead)

    log(f"\n{'='*62}")
    log(f"JIM Recommendation Engine")
    log(f"Batch:  {batch_id}")
    log(f"Week:   {shipping_week}")
    log(f"Weights: velocity={W_VELOCITY} recency={W_RECENCY} breadth={W_BREADTH}")
    log(f"{'='*62}\n")

    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # ──────────────────────────────────────────────────────────
    # LOAD DATA
    # ──────────────────────────────────────────────────────────

    # Availability
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

    # Inventory pool: {sku_id: total_units}
    inventory = defaultdict(int)
    avail_meta = {}
    for r in avail_rows:
        inventory[r['sku_id']] += r['units']
        avail_meta[r['sku_id']] = {
            'code_level':   r['code_level'],
            'hard_good_type': r['hard_good_type'],
            'kit_selection_required': r['kit_selection_required'],
        }

    total_available = sum(inventory.values())
    log(f"  {len(inventory)} SKUs available | {total_available:,} total units\n")

    # Store velocity scores
    log("Loading store velocity scores...")
    cur.execute("""
        SELECT store_id, dynamic_velocity_score, dynamic_velocity_tier,
               merchant, region, state, market_number
        FROM stores
        WHERE dynamic_velocity_tier IS NOT NULL
          AND active = true
    """)
    store_rows = {r['store_id']: dict(r) for r in cur.fetchall()}
    vel_scores = [float(r['dynamic_velocity_score']) for r in store_rows.values()
                  if r['dynamic_velocity_score']]
    vel_min, vel_max = (min(vel_scores), max(vel_scores)) if vel_scores else (0, 1)
    log(f"  {len(store_rows):,} stores with velocity scores")

    # Last delivery date per store (DTS history, or JIM orders if available)
    log("Loading delivery recency...")
    cur.execute("""
        SELECT store_id, MAX(delivery_date) AS last_delivery
        FROM deliveries
        GROUP BY store_id
    """)
    last_delivery = {r['store_id']: r['last_delivery'] for r in cur.fetchall()}

    # Also check JIM order history (takes precedence when available)
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

    # SKU group velocity per store
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

    # SKU → group mapping (which group does each available SKU belong to?)
    cur.execute("""
        SELECT s.sku_id, s.family, s.legacy_product_group
        FROM skus s
        WHERE s.sku_id = ANY(%s)
    """, (list(inventory.keys()),))
    sku_family = {r['sku_id']: (r['family'], r['legacy_product_group'])
                  for r in cur.fetchall()}

    def sku_to_group_ids(sku_id):
        """Return all group IDs this SKU contributes to."""
        fam, tier = sku_family.get(sku_id, (None, None))
        if not fam:
            return []
        groups = []
        # Size-specific + tier-specific groups
        fam_lower = fam.lower().replace(' ', '_').replace('-', '_')
        tier_lower = (tier or 'premium').lower().replace(' ', '_')
        groups.append(f"{fam_lower}_{tier_lower}")
        # Aggregate groups
        if '9cm' in fam.lower():    groups.append('9cm_all')
        if '12cm' in fam.lower():   groups.append('12cm_all')
        if '17cm' in fam.lower():   groups.extend(['17cm_premium'])
        if 'h2o' in fam.lower():    groups.append('h2o_all')
        if tier in ('Collectors','Specialty','Boutique','Rare Collectors'):
            groups.append('boutique_all')
        return groups

    # Shelf configurations: profile_id → {units_per_shelf, trays_per_shelf, units_per_tray, max_shelves}
    cur.execute("""
        SELECT sc.profile_id, sc.units_per_shelf, sc.trays_per_shelf,
               sc.max_shelves, sc.units_per_cart,
               pp.units_per_tray,
               spp.sku_id
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
        if r['sku_id'] not in sku_profiles:  # first/default profile wins
            sku_profiles[r['sku_id']] = r['profile_id']

    log(f"  {len(shelf_configs)} approved shelf configs")

    # ── Load weekly playbook adjustments ──────────────────────
    log("Loading weekly playbook...")
    adjustments = load_adjustments(cur, str(shipping_week))
    playbook = PlaybookEngine(adjustments, store_rows, sku_family)
    playbook.print_summary(log)

    # ──────────────────────────────────────────────────────────
    # STAGE 1 — SCORE STORES
    # ──────────────────────────────────────────────────────────
    log("\nStage 1: Scoring stores...")

    store_scores = {}
    today = date.today()

    for store_id, meta in store_rows.items():
        raw_vel  = float(meta['dynamic_velocity_score'] or 0)
        vel_norm = normalize(raw_vel, vel_min, vel_max)

        last_del = last_delivery.get(store_id)
        days_ago = (today - last_del).days if last_del else None
        rec_score = recency_score(days_ago)

        # Breadth: how many SKU groups does this store have a tier for?
        store_groups = group_vel.get(store_id, {})
        breadth_raw = sum(1 for gid, score in store_groups.items()
                          if score > 0 and gid.endswith('_all'))
        breadth_norm = normalize(breadth_raw, 0, 5)

        priority = (vel_norm * W_VELOCITY +
                    rec_score * W_RECENCY +
                    breadth_norm * W_BREADTH)

        # Apply playbook priority boost (e.g. promotion in this merchant's territory)
        priority = min(1.0, priority + playbook.stage1_priority_bonus(store_id))

        store_scores[store_id] = {
            'priority':       round(priority, 6),
            'vel_component':  round(vel_norm * W_VELOCITY, 6),
            'rec_component':  round(rec_score * W_RECENCY, 6),
            'brd_component':  round(breadth_norm * W_BREADTH, 6),
            'days_since':     days_ago,
            'tier':           meta['dynamic_velocity_tier'],
            'merchant':       meta['merchant'],
            'region':         meta['region'],
            'state':          meta['state'],
            'market_number':  meta['market_number'],
        }

    ranked_stores = sorted(store_scores.items(), key=lambda x: -x[1]['priority'])
    log(f"  {len(ranked_stores):,} stores scored")

    # ──────────────────────────────────────────────────────────
    # STAGE 2 + 3 — ALLOCATE & BUILD CARTS
    # ──────────────────────────────────────────────────────────
    log("Stages 2+3: Allocating inventory and building carts...")

    all_orders   = {}   # store_id → {score_meta, carts, committed}
    avail_max    = total_available

    for store_id, score_meta in ranked_stores:
        if sum(inventory.values()) == 0:
            break

        tier = score_meta['tier']

        # Stage 2: Score available SKUs for this store
        store_gv = group_vel.get(store_id, {})
        avail_remaining_max = max(inventory.values()) if inventory else 1
        sku_scores = {}

        for sku_id, avail_units in inventory.items():
            if avail_units <= 0:
                continue
            group_ids  = sku_to_group_ids(sku_id)
            group_score = max(
                (store_gv.get(gid, 0) for gid in group_ids),
                default=0
            )
            vel_norm_sku = normalize(group_score,
                                     0,
                                     max(store_gv.values(), default=1) or 1)
            avail_norm   = avail_units / avail_remaining_max

            # Apply playbook modifier (demand multiplier, sku push, or exclusion)
            vel_norm_sku, avail_norm = playbook.stage2_sku_score_modifier(
                store_id, sku_id, vel_norm_sku, avail_norm
            )

            sku_scores[sku_id] = (vel_norm_sku * W_SKU_VEL +
                                  avail_norm   * W_SKU_AVAIL)

        ranked_skus = sorted(sku_scores.items(), key=lambda x: -x[1])
        if not ranked_skus:
            continue

        # Stage 3: Build carts — use playbook override if set, else tier default
        target_carts = playbook.stage1_cart_target(
            store_id, TIER_TARGET_CARTS.get(tier, MIN_CARTS)
        )
        carts, committed = build_carts_for_store(
            store_id, ranked_skus, inventory,
            shelf_configs, sku_profiles, target_carts, avail_meta
        )

        if not carts:
            continue

        total_carts = len(carts)

        # Check minimum cart rule
        is_exception = False
        if total_carts < MIN_CARTS:
            days_ago = score_meta['days_since']
            # 1-cart exception: store got delivery recently and is a strong seller,
            # or it's a D-tier refresh store
            if (days_ago is not None and days_ago <= 7 and
                    tier in ('AA', 'A', 'B')):
                is_exception = True
            elif tier == 'D':
                is_exception = True
            else:
                # Can't meet minimum — skip this store
                continue

        # Deduct from shared inventory
        for sku_id, units in committed.items():
            if '_jute' not in str(sku_id) and '_ceramic' not in str(sku_id):
                inventory[sku_id] = max(0, inventory.get(sku_id, 0) - units)

        all_orders[store_id] = {
            'score_meta':       score_meta,
            'carts':            carts,
            'committed':        committed,
            'total_carts':      total_carts,
            'total_units':      sum(c['total_units'] for c in carts),
            'is_exception':     is_exception,
            'sku_scores':       {k: round(v, 4) for k, v in sku_scores.items()},
        }

    log(f"  {len(all_orders):,} stores allocated")
    log(f"  {sum(o['total_carts'] for o in all_orders.values()):,} total carts")
    allocated_units = sum(o['total_units'] for o in all_orders.values())
    clearance = (allocated_units / total_available * 100) if total_available else 0
    log(f"  {allocated_units:,} / {total_available:,} units allocated ({clearance:.1f}% clearance)")

    # ──────────────────────────────────────────────────────────
    # STAGE 5 — BUILD LOADS (45 carts / truck)
    # ──────────────────────────────────────────────────────────
    log("\nStage 5: Building truck loads...")

    # Sort stores for load building: merchant → region → state → market_number
    def load_sort_key(store_id):
        meta = all_orders[store_id]['score_meta']
        return (
            meta['merchant']     or 'ZZZ',
            meta['region']       or 'ZZZ',
            meta['state']        or 'ZZ',
            meta['market_number'] or 9999,
            -all_orders[store_id]['score_meta']['priority'],  # high priority first
        )

    sorted_for_loads = sorted(all_orders.keys(), key=load_sort_key)

    loads_out   = []
    current_load_carts = 0
    current_load_stores = []
    load_number  = 1

    def flush_load():
        nonlocal current_load_carts, current_load_stores, load_number
        if not current_load_stores:
            return
        meta = all_orders[current_load_stores[0]]['score_meta']
        util = (current_load_carts / CARTS_PER_TRUCK * 100)
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
        current_load_carts  += store_carts
        current_load_stores.append(store_id)

    flush_load()
    log(f"  {len(loads_out)} loads built")

    # ──────────────────────────────────────────────────────────
    # SUMMARY PREVIEW
    # ──────────────────────────────────────────────────────────
    log(f"\n{'='*62}")
    log(f"RECOMMENDATION SUMMARY")
    log(f"{'='*62}")
    log(f"  Stores served:     {len(all_orders):,}")
    log(f"  Total carts:       {sum(o['total_carts'] for o in all_orders.values()):,}")
    log(f"  Total units:       {allocated_units:,}")
    log(f"  Inventory used:    {clearance:.1f}%")
    log(f"  Truck loads:       {len(loads_out)}")
    log(f"\n  Top 10 stores by priority:")
    log(f"  {'Store':<8} {'Tier':<5} {'Priority':>9} {'Carts':>6} {'Units':>7} {'Days Ago':>9}")
    log(f"  {'-'*52}")
    for store_id, data in list(all_orders.items())[:10]:
        sm = data['score_meta']
        log(f"  {store_id:<8} {sm['tier']:<5} {sm['priority']:>9.4f} "
            f"{data['total_carts']:>6} {data['total_units']:>7,} "
            f"{str(sm['days_since'] or 'new'):>9}")

    if preview:
        log("\nPREVIEW MODE — nothing written to database.")
        conn.close()
        return None

    # ──────────────────────────────────────────────────────────
    # WRITE TO DATABASE
    # ──────────────────────────────────────────────────────────
    log("\nWriting to database...")
    conn.rollback()
    conn.autocommit = False

    # Recommendation run
    cur.execute("""
        INSERT INTO recommendation_runs
            (batch_id, shipping_week, stores_scored, stores_served,
             total_carts, total_units, inventory_clearance,
             weight_velocity, weight_recency, weight_breadth,
             weight_sku_velocity, weight_sku_avail, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft')
        RETURNING id
    """, (
        batch_id, shipping_week,
        len(ranked_stores), len(all_orders),
        sum(o['total_carts'] for o in all_orders.values()), allocated_units,
        round(clearance, 2),
        W_VELOCITY, W_RECENCY, W_BREADTH, W_SKU_VEL, W_SKU_AVAIL
    ))
    run_id = cur.fetchone()['id']
    log(f"  Run ID: {run_id}")

    # Orders
    order_id_map = {}
    order_rows = []
    for store_id, data in all_orders.items():
        sm = data['score_meta']
        oid = str(uuid.uuid4())
        order_id_map[store_id] = oid
        order_rows.append((
            oid, str(run_id), store_id, 'draft',
            sm['priority'], sm['vel_component'],
            sm['rec_component'], sm['brd_component'],
            sm['days_since'], data['total_carts'],
            data['total_units'], data['is_exception']
        ))

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO orders
            (id, run_id, store_id, status, priority_score,
             velocity_component, recency_component, breadth_component,
             days_since_last_delivery, cart_count, total_units, is_one_cart_exception)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, order_rows, page_size=500)
    log(f"  {len(order_rows)} orders written")

    # Order lines + carts + shelves
    line_rows    = []
    cart_rows    = []
    shelf_rows   = []
    cart_id_map  = {}  # (store_id, cart_number) → cart_id
    cart_seq     = 0   # global cart counter — WK{week_num}-C{seq}
    week_num     = shipping_week.isocalendar()[1]

    for store_id, data in all_orders.items():
        order_id = order_id_map[store_id]
        sku_totals = defaultdict(int)  # sku_id → total units this store

        for cart in data['carts']:
            cart_seq += 1
            cart_key = f"WK{week_num:02d}-C{cart_seq:04d}"   # e.g. WK41-C0001
            cart_id = str(uuid.uuid4())
            cart_id_map[(store_id, cart['cart_number'])] = cart_id
            # Effective max shelves = physical cart capacity (5 for CC).
            # Product-level shelf caps are enforced during building (MAX_SHELVES_SKU=2 per SKU).
            # A mixed cart legitimately uses all 5 shelves across multiple SKUs.
            eff_max = CART_SHELVES

            cart_rows.append((
                cart_id, order_id, store_id,
                cart['cart_number'], cart['cart_type_id'],
                cart['shelves_used'], CART_SHELVES,
                cart['total_units'],
                cart['shelves_used'] >= eff_max,
                cart['is_flagged'], cart.get('flag_reason'),
                eff_max,
                cart_key                            # WK41-C0001 etc.
            ))

            # Write one cart_shelves row PER PHYSICAL SHELF (not per SKU).
            # A SKU occupying 2 shelves → 2 rows, each with shelf-level quantities.
            shelf_pos = 1
            for (sku_id, num_shelves, total_units, kit_type) in cart['contents']:
                profile_id     = sku_profiles.get(sku_id)
                cfg            = shelf_configs.get(profile_id, {})
                ups            = cfg.get('units_per_shelf', 1)    # units per shelf
                tps            = cfg.get('trays_per_shelf') or \
                                 max(1, ups // max(1, cfg.get('units_per_tray', 1)))
                clean_kit      = kit_type if kit_type not in ('mixed',) else 'ceramic_kit'

                for _ in range(num_shelves):
                    shelf_rows.append((
                        cart_id, shelf_pos, sku_id, profile_id,
                        clean_kit,
                        tps,   # trays on THIS shelf
                        ups,   # units on THIS shelf
                    ))
                    shelf_pos += 1

                sku_totals[sku_id] += total_units

        for sku_id, total_units in sku_totals.items():
            cfg  = shelf_configs.get(sku_profiles.get(sku_id), {})
            kit  = avail_meta.get(sku_id, {}).get('hard_good_type')
            line_rows.append((
                order_id, store_id, sku_id,
                total_units, total_units,   # recommended_qty, draft_qty (start equal)
                None, kit,                  # approved_qty, kit_type
                data['sku_scores'].get(sku_id),
            ))

    psycopg2.extras.execute_batch(cur, """
        INSERT INTO order_carts
            (id, order_id, store_id, cart_number, cart_type_id,
             shelves_used, shelves_capacity, total_units,
             is_full, is_flagged, flag_reason, effective_max_shelves, cart_key)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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

    log(f"  {len(cart_rows)} carts, {len(shelf_rows)} shelf lines, {len(line_rows)} order lines")

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
        for stop_seq, store_id in enumerate(load['stores'], start=1):
            for cart in all_orders[store_id]['carts']:
                cart_id = cart_id_map.get((store_id, cart['cart_number']))
                if cart_id:
                    assign_rows.append((
                        lid, cart_id, store_id, stop_seq
                    ))

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

    log(f"  {len(load_rows)} loads, {len(assign_rows)} cart assignments")

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

    batch_id  = args[0]
    preview   = '--preview' in args
    week_arg  = None
    for i, a in enumerate(args):
        if a == '--week' and i + 1 < len(args):
            week_arg = date.fromisoformat(args[i + 1])

    run(batch_id, shipping_week=week_arg, preview=preview)
