"""Stop-level rules and the release scorecard (routing review 2026-11-25). Pure Python, no DB.

Routing rejected an order where most stores got ONE cart: a 45-cart truck would need ~33 stops
(cap 20). These helpers give the engine one definition of
  * how close a candidate cart build is to N full carts (the shared routine-cart builder),
  * routing feasibility (carts, stops, trucks, stops per truck) overall and by region,
  * the pack team's physical findings for one cart (ShelfKit model),
  * the release scorecard: every reviewer's check in one pass/fail list.
"""
from __future__ import annotations

import math
from collections import defaultdict

try:
    from workers import shelfkit as SK
except ImportError:                       # backend/workers on sys.path (engine, tests)
    import shelfkit as SK


# ── N-cart build progress ──────────────────────────────────────────────────
def top_k_progress(fills, k):
    """Sum of the k fullest cart fills (each capped at 1.0). With k=1 this is the fullest cart's
    fill, i.e. exactly the single-cart builder's objective."""
    if k <= 0:
        return 0.0
    return sum(sorted((min(1.0, float(f)) for f in fills), reverse=True)[:k])


def full_carts(fills, min_fill):
    """Carts at or above the fill floor."""
    return sum(1 for f in fills if f >= min_fill - 1e-9)


def top_k_full(fills, k, min_fill):
    """True when the k fullest carts all meet the fill floor."""
    if k <= 0:
        return True
    top = sorted(fills, reverse=True)[:k]
    return len(top) == k and all(f >= min_fill - 1e-9 for f in top)


def routine_target(min_stop, protected_carts, need_at_least_one=True):
    """Total carts a store must reach. protected_carts = directed / named-variety / pre-built carts
    it already has (never changed). The stop minimum counts every cart; the routine build adds at
    least one cart when need_at_least_one (second-chance semantics)."""
    t = max(int(min_stop or 0), 0)
    if need_at_least_one:
        t = max(t, protected_carts + 1)
    return t


# ── routing feasibility ─────────────────────────────────────────────────────
def routing_block(stops, capacity, max_stops):
    """stops: iterable of (carts, directed_only) for stores with >= 1 cart."""
    stops = [(int(c), bool(d)) for c, d in stops if c > 0]
    carts = sum(c for c, _ in stops)
    n = len(stops)
    trucks = math.ceil(carts / capacity) if capacity and carts else 0
    spt = (n / trucks) if trucks else 0.0
    single = [d for c, d in stops if c == 1]
    return {"carts": carts, "stops": n, "trucks": trucks,
            "stops_per_truck": round(spt, 2),
            "carts_per_stop": round(carts / n, 2) if n else 0.0,
            "single_cart_stops": len(single),
            "single_cart_stops_directed_only": sum(1 for d in single if d),
            "pass": spt <= max_stops + 1e-9}


def routing_feasibility(stop_info, region_of, capacity=45, max_stops=20):
    """stop_info: {store_id: (carts, directed_only)}; region_of: {store_id: region}.
    Trucks = ceil(carts / capacity) — a lower bound (perfect consolidation within the region)."""
    by = defaultdict(list)
    for st, v in stop_info.items():
        by[region_of.get(st) or "?"].append(v)
    out = routing_block(stop_info.values(), capacity, max_stops)
    out.update(truck_cart_capacity=capacity, max_stops_per_truck=max_stops,
               by_region={r: routing_block(v, capacity, max_stops) for r, v in sorted(by.items(), key=lambda kv: str(kv[0]))})
    return out


# ── physical (pack team) findings for one cart ──────────────────────────────
def cart_physical(contents, skip_fullness=False):
    """contents: [(pos, sku_id, trays, units, kit, profile_id), ...] of one cart.
    Returns counts {shelves_too_wide, over_height, over_shelf_cap, shelves_not_full, height_in}.
    skip_fullness: directed / pre-built carts are not held to every-shelf-full."""
    layers = defaultdict(list)
    for it in contents:
        layers[it[0]].append((it[5], int(it[2])))
    pids = {p for its in layers.values() for p, _ in its}
    height = sum(SK.shelf_rung({p for p, _ in its}) for its in layers.values())
    too_wide = sum(1 for its in layers.values() if not SK.fits_shelf(its))
    not_full = 0 if skip_fullness else sum(1 for its in layers.values()
                                           if SK.fits_shelf(its) and not SK.shelf_is_full(its))
    return {"shelves_too_wide": too_wide,
            "over_height": int(height > SK.CC_MAX_HEIGHT_IN),
            "over_shelf_cap": int(len(layers) > min(SK.CC_MAX_SHELVES, SK.max_shelves(pids))),
            "shelves_not_full": not_full, "height_in": height}


# ── release scorecard ───────────────────────────────────────────────────────
_OPS = {"<=": lambda v, t: v <= t + 1e-9, ">=": lambda v, t: v >= t - 1e-9, "==": lambda v, t: v == t}


def check(name, value, target, op, audience, **extra):
    """One scorecard line. value None (not measurable) fails."""
    ok = value is not None and _OPS[op](value, target)
    return {"check": name, "value": value, "target": target, "op": op, "pass": bool(ok),
            "audience": audience, **extra}


def scorecard(checks):
    failing = [c["check"] for c in checks if not c["pass"]]
    return {"checks": checks, "pass": not failing, "failing": failing}
