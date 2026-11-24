"""Cart-config streamlining across stores (leadership 11/20 "consolidate carts when alike";
Josh 2026-11-24: "as few configs as possible while getting stores exactly what they need, never
giving a store what it doesn't sell just to lower cart counts").

A cart config is the cart's contents ignoring shelf order and kit: the multiset of
(profile, SKU, trays). A cart in a rarer config may take on a more common config when the
difference is at most `max_changes` items, the items pair up profile for profile (so the target's
physical layout — a real, buildable cart — fits), and the engine's business check accepts the
change for that store (store sells the product, same family, supply, weeks-of-supply ceiling,
never below need). This module is the search; the business rules live in the engine's callback.
"""
from collections import Counter


def cart_sig(items):
    """items: iterable of (profile_id, sku_id, trays). Shelf order and kit are not part of it."""
    return tuple(sorted(Counter(items).items()))


def sig_items(sig):
    out = []
    for item, n in sig:
        out.extend([item] * n)
    return out


def diff(sig_a, sig_b):
    """(removed, added) item lists turning config a into config b."""
    a, b = Counter(dict(sig_a)), Counter(dict(sig_b))
    return sig_items(tuple(sorted((a - b).items()))), sig_items(tuple(sorted((b - a).items())))


def compatible(removed, added, max_changes):
    """At most max_changes items differ and they pair up by profile (same shelf types)."""
    if len(removed) != len(added) or not removed or len(removed) > max_changes:
        return False
    return Counter(p for p, _, _ in removed) == Counter(p for p, _, _ in added)


def streamline(carts, max_changes, try_change, passes=3):
    """carts: list of dicts with 'sig' and optional 'locked'. try_change(cart, target_sig,
    removed, added) -> bool: checks the business rules and, when True, has already committed
    the change (lines, supply). Returns [(cart, old_sig, new_sig)] applied, in order.

    Greedy: carts in the rarest configs go first; each moves to the MOST common config it can
    legally reach (ties: fewer changes, then a stable order). Only toward a config at least as
    common as its own (strictly more common, or equal with a lower sort key), so it never
    ping-pongs. Repeats until a pass changes nothing."""
    counts = Counter(c["sig"] for c in carts if not c.get("locked"))
    moves = []
    for _ in range(passes):
        changed = False
        order = sorted((c for c in carts if not c.get("locked")),
                       key=lambda c: (counts[c["sig"]], str(c["sig"]), str(c.get("id"))))
        for c in order:
            own = c["sig"]
            cands = []
            for t, n in counts.items():
                if t == own or n <= 0:
                    continue
                if n < counts[own] or (n == counts[own] and str(t) >= str(own)):
                    continue
                removed, added = diff(own, t)
                if compatible(removed, added, max_changes):
                    cands.append((-n, len(removed), str(t), t, removed, added))
            for _n, _k, _s, t, removed, added in sorted(cands):
                if try_change(c, t, removed, added):
                    counts[own] -= 1
                    counts[t] += 1
                    c["sig"] = t
                    moves.append((c, own, t))
                    changed = True
                    break
        if not changed:
            break
    return moves
