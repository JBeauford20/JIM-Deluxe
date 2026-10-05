"""
JIM Deluxe — Playbook Engine
==============================
Loads and applies weekly_adjustments to modify recommendation engine behavior.
Called by recommendation_engine.py before Stage 1 and Stage 2.

Adjustment types and where they apply:
  Stage 1 (store scoring):
    priority_boost      → add flat bonus to specific stores' priority scores
    cart_target_override→ override tier-based cart target for stores in scope

  Stage 2 (SKU scoring):
    demand_multiplier   → multiply group_velocity_score for store+SKU combos
    sku_push            → boost availability_weight for a SKU group (move more of it)
    allocation_floor    → ensure stores in scope get at least N carts of this SKU
    allocation_cap      → limit stores in scope to max N carts of this SKU
    exclusion           → zero out a SKU for stores in scope
"""

from collections import defaultdict
import psycopg2.extras


def load_adjustments(cur, shipping_week: str) -> list:
    """Load all active adjustments for the shipping week."""
    cur.execute("""
        SELECT * FROM weekly_adjustments
        WHERE active = true
          AND effective_start <= %s
          AND (effective_end IS NULL OR effective_end >= %s)
        ORDER BY priority ASC
    """, (shipping_week, shipping_week))
    return [dict(r) for r in cur.fetchall()]


def build_store_scope(adj: dict, store_rows: dict) -> set:
    """Return the set of store_ids this adjustment applies to."""
    result = set()
    for store_id, meta in store_rows.items():
        if adj['scope_store_ids'] and store_id not in adj['scope_store_ids']:
            continue
        if adj['scope_merchant'] and meta.get('merchant') != adj['scope_merchant']:
            continue
        if adj['scope_region'] and meta.get('region') != adj['scope_region']:
            continue
        if adj['scope_state'] and meta.get('state') != adj['scope_state']:
            continue
        if adj['scope_market_number'] and meta.get('market_number') != adj['scope_market_number']:
            continue
        if adj['scope_velocity_tier'] and meta.get('dynamic_velocity_tier') != adj['scope_velocity_tier']:
            continue
        result.add(store_id)
    return result


def sku_in_scope(adj: dict, sku_id: int, sku_family: dict) -> bool:
    """Return True if a SKU falls within this adjustment's product scope."""
    if adj['scope_sku_id'] and sku_id != adj['scope_sku_id']:
        return False
    fam, tier = sku_family.get(sku_id, (None, None))
    if adj['scope_family'] and fam != adj['scope_family']:
        return False
    if adj['scope_legacy_tier'] and tier != adj['scope_legacy_tier']:
        return False
    # scope_sku_group_id handled separately via group membership
    return True


class PlaybookEngine:
    """
    Wraps weekly adjustments into fast lookup structures for the
    recommendation engine to apply during scoring and allocation.
    """

    def __init__(self, adjustments: list, store_rows: dict, sku_family: dict):
        self.adjustments = adjustments
        self.store_rows  = store_rows
        self.sku_family  = sku_family

        # Pre-compute scopes
        self._priority_boosts    = defaultdict(float)   # store_id → bonus
        self._cart_overrides     = {}                    # store_id → cart_count
        self._demand_multipliers = defaultdict(lambda: defaultdict(float))  # store_id → sku_id → multiplier
        self._sku_push           = defaultdict(float)    # sku_id → extra avail weight
        self._allocation_floors  = defaultdict(lambda: defaultdict(int))   # store_id → sku_id → min_carts
        self._allocation_caps    = defaultdict(lambda: defaultdict(int))   # store_id → sku_id → max_carts
        self._exclusions         = defaultdict(set)      # store_id → {sku_id, ...}

        self._summary = []  # human-readable summary of what was applied

        for adj in adjustments:
            self._apply(adj)

    def _apply(self, adj: dict):
        atype  = adj['adjustment_type']
        val    = float(adj['value_numeric'] or 0)
        stores = build_store_scope(adj, self.store_rows)
        n_stores = len(stores)

        if atype == 'priority_boost':
            for sid in stores:
                self._priority_boosts[sid] += val
            self._summary.append(
                f"Priority boost +{val:.2f} → {n_stores} stores "
                f"({adj['scope_merchant'] or adj['scope_region'] or 'custom scope'})"
            )

        elif atype == 'cart_target_override':
            for sid in stores:
                self._cart_overrides[sid] = int(val)
            self._summary.append(
                f"Cart target override → {int(val)} carts for {n_stores} stores "
                f"({adj['label']})"
            )

        elif atype == 'demand_multiplier':
            skus_in_scope = [s for s in self.sku_family if sku_in_scope(adj, s, self.sku_family)]
            for sid in stores:
                for sku_id in skus_in_scope:
                    self._demand_multipliers[sid][sku_id] = max(
                        self._demand_multipliers[sid][sku_id], val
                    )
            self._summary.append(
                f"Demand x{val:.1f} for {len(skus_in_scope)} SKUs -> {n_stores} stores "
                f"({adj['label']})"
            )

        elif atype == 'sku_push':
            skus_in_scope = [s for s in self.sku_family if sku_in_scope(adj, s, self.sku_family)]
            for sku_id in skus_in_scope:
                self._sku_push[sku_id] = max(self._sku_push[sku_id], val)
            self._summary.append(
                f"SKU push +{val:.2f} avail weight for {len(skus_in_scope)} SKUs "
                f"({adj['label']})"
            )

        elif atype == 'allocation_floor':
            skus_in_scope = [s for s in self.sku_family if sku_in_scope(adj, s, self.sku_family)]
            for sid in stores:
                for sku_id in skus_in_scope:
                    self._allocation_floors[sid][sku_id] = max(
                        self._allocation_floors[sid][sku_id], int(val)
                    )
            self._summary.append(
                f"Floor {int(val)} carts of {adj['scope_family'] or 'SKU'} "
                f"→ {n_stores} stores ({adj['label']})"
            )

        elif atype == 'allocation_cap':
            skus_in_scope = [s for s in self.sku_family if sku_in_scope(adj, s, self.sku_family)]
            for sid in stores:
                for sku_id in skus_in_scope:
                    self._allocation_caps[sid][sku_id] = int(val)
            self._summary.append(
                f"Cap {int(val)} carts of {adj['scope_family'] or 'SKU'} "
                f"→ {n_stores} stores ({adj['label']})"
            )

        elif atype == 'exclusion':
            skus_in_scope = [s for s in self.sku_family if sku_in_scope(adj, s, self.sku_family)]
            for sid in stores:
                self._exclusions[sid].update(skus_in_scope)
            self._summary.append(
                f"Exclusion: {len(skus_in_scope)} SKUs blocked from {n_stores} stores "
                f"({adj['label']})"
            )

    # ── Public interface called by recommendation_engine.py ───

    def stage1_priority_bonus(self, store_id: int) -> float:
        """Additional priority score bonus for this store (Stage 1)."""
        return self._priority_boosts.get(store_id, 0.0)

    def stage1_cart_target(self, store_id: int, default: int) -> int:
        """Cart target for this store, potentially overridden (Stage 1)."""
        return self._cart_overrides.get(store_id, default)

    def stage2_sku_score_modifier(self, store_id: int, sku_id: int,
                                   base_vel_score: float,
                                   base_avail_weight: float) -> tuple:
        """
        Returns (modified_vel_score, modified_avail_weight) for this store+SKU.
        Called during SKU scoring (Stage 2).
        """
        if sku_id in self._exclusions.get(store_id, set()):
            return 0.0, 0.0  # excluded — zero it out

        multiplier   = self._demand_multipliers[store_id].get(sku_id, 1.0) or 1.0
        push_bonus   = self._sku_push.get(sku_id, 0.0)

        return (
            base_vel_score  * multiplier,
            min(1.0, base_avail_weight + push_bonus)
        )

    def allocation_floor(self, store_id: int, sku_id: int) -> int:
        """Minimum carts of this SKU for this store (0 = no floor)."""
        return self._allocation_floors[store_id].get(sku_id, 0)

    def allocation_cap(self, store_id: int, sku_id: int) -> int | None:
        """Maximum carts of this SKU for this store (None = no cap)."""
        return self._allocation_caps[store_id].get(sku_id)

    def print_summary(self, log=print):
        if not self.adjustments:
            log("  Playbook: Business as Usual (no adjustments)")
            return
        log(f"  Playbook: {len(self.adjustments)} adjustment(s) active")
        for line in self._summary:
            log(f"    - {line}")
