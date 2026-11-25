"""MASTER + FLEX cart configurations (Jason's packing recommendation, adopted by Josh 11/25).
Pure Python, no DB.

A cart = a MASTER (its base shelves, identical on every cart that shares it) + up to two FLEX
shelves (store-specific). 5 shelves: 3 base + 2 flex; 4: 2 + 2; 3: 1 + 2. The weekly cap counts
distinct masters. A shelf build = tuple(sorted((sku, trays, profile))) — the exact SKUs and trays.

regroup_store()  re-partitions one store's single-profile full shelves (zero quantity change):
                 full single-SKU shelves first, the remainders into mixed full shelves.
choose_bases()   picks each cart's base shelves so masters are shared as widely as possible.
standardise()    if that leaves too many masters, moves carts onto a kept master by replacing
                 base shelves with the master's (same profile per shelf, so the cart stays
                 physically identical); a callback judges each change for the store.
"""
from __future__ import annotations

import itertools
from collections import Counter, defaultdict


def n_base(n_shelves):
    return max(0, n_shelves - 2)


def shelf_pids(build):
    return frozenset(x[2] for x in build)


def regroup_store(carts, cap_of):
    """carts: [[build, ...], ...] (one list of shelf builds per cart). Single-profile shelves that
    are exactly full (trays == the profile's trays per shelf) are pooled per profile and dealt
    back as full single-SKU shelves first, remainders combined; each cart keeps the same number of
    shelves of each profile, so heights, shelf caps and fill are unchanged."""
    pool = defaultdict(Counter)
    slots = []
    for ci, c in enumerate(carts):
        for b in c:
            pids = shelf_pids(b)
            if len(pids) == 1:
                pid = next(iter(pids))
                if sum(x[1] for x in b) == cap_of(pid, [x[0] for x in b]):
                    for sk, tr, _ in b:
                        pool[pid][sk] += tr
                    slots.append((ci, pid, None))
                    continue
            slots.append((ci, None, b))
    new = defaultdict(list)
    for pid, cnt in pool.items():
        cp = cap_of(pid, list(cnt))
        rem = []
        for sk, t in sorted(cnt.items()):
            new[pid] += [((sk, cp, pid),)] * (t // cp)
            if t % cp:
                rem.append([sk, t % cp])
        rem.sort(key=lambda x: (-x[1], x[0]))
        while rem:
            shelf, left = [], cp
            while left and rem:
                take = min(rem[0][1], left)
                shelf.append((rem[0][0], take, pid))
                left -= take
                rem[0][1] -= take
                if rem[0][1] == 0:
                    rem.pop(0)
            if left:                                    # cannot happen for exactly-full shelves
                raise ValueError(f"profile {pid}: remainders do not fill whole shelves")
            new[pid].append(tuple(sorted(shelf)))
    out = [[] for _ in carts]
    for ci, pid, b in slots:
        out[ci].append(new[pid].pop() if pid else b)
    return out


def _best_base(shelves, mcount, freq):
    b = n_base(len(shelves))
    best = None
    for idx in itertools.combinations(range(len(shelves)), b):
        m = tuple(sorted(shelves[i] for i in idx))
        # most-shared master first; then common, single-profile shelves as base
        key = (mcount.get(m, 0), sum(freq[shelves[i]] for i in idx),
               -sum(len(shelf_pids(shelves[i])) for i in idx))
        if best is None or key > best[0]:
            best = (key, m, idx)
    return best[1], best[2]


def choose_bases(carts, rounds=6):
    """carts: [{"store", "shelves": [build, ...]}]; sets c["master"], c["base_idx"]; also swaps
    same-profile shelves between one store's carts when that lets more carts share a master."""
    freq = Counter(b for c in carts for b in c["shelves"])
    mcount = Counter()
    for c in carts:
        c["master"], c["base_idx"] = _best_base(c["shelves"], {}, freq)
        mcount[c["master"]] += 1
    by_store = defaultdict(list)
    for c in carts:
        by_store[c["store"]].append(c)
    for _ in range(rounds):
        changed = 0
        for c in sorted(carts, key=lambda c: (mcount[c["master"]], str(c["store"]))):
            mcount[c["master"]] -= 1
            m, idx = _best_base(c["shelves"], mcount, freq)
            changed += m != c["master"]
            c["master"], c["base_idx"] = m, idx
            mcount[m] += 1
        mcount = +mcount
        for cs in by_store.values():
            for a, b in itertools.combinations(cs, 2):
                for i in range(len(a["shelves"])):
                    for j in range(len(b["shelves"])):
                        sa, sb = a["shelves"][i], b["shelves"][j]
                        if sa == sb or shelf_pids(sa) != shelf_pids(sb):
                            continue
                        before = (mcount[a["master"]] > 1) + (mcount[b["master"]] > 1)
                        a["shelves"][i], b["shelves"][j] = sb, sa
                        mcount[a["master"]] -= 1
                        mcount[b["master"]] -= 1
                        ma, ia = _best_base(a["shelves"], mcount, freq)
                        mb, ib = _best_base(b["shelves"], mcount, freq)
                        same = int(ma == mb)
                        after = (mcount.get(ma, 0) + same > 0) + (mcount.get(mb, 0) + same > 0)
                        if after > before:
                            a["master"], a["base_idx"], b["master"], b["base_idx"] = ma, ia, mb, ib
                            changed += 1
                        else:
                            a["shelves"][i], b["shelves"][j] = sa, sb
                        mcount[a["master"]] += 1
                        mcount[b["master"]] += 1
        mcount = +mcount
        if not changed:
            break
    return mcount


def match_master(shelves, master, compat=None):
    """Which of the cart's shelves would carry the master's base builds: a one-to-one match on
    identical profile sets, identical builds first; then, if given, compat(cart_shelf, build)
    (e.g. same shelf rung, no 17cm change). Returns the index list or None."""
    used, idx = set(), []
    for mb in sorted(master, key=lambda b: (len(shelf_pids(b)), b)):
        hit = next((i for i, s in enumerate(shelves) if i not in used and s == mb), None)
        if hit is None:
            hit = next((i for i, s in enumerate(shelves) if i not in used and shelf_pids(s) == shelf_pids(mb)), None)
        if hit is None and compat is not None:
            hit = next((i for i, s in enumerate(shelves) if i not in used and compat(s, mb)), None)
        if hit is None:
            return None
        used.add(hit)
        idx.append(hit)
    return idx


def standardise(carts, cap, judge, apply=None, compat=None):
    """Bring the number of masters down to `cap` by moving carts onto kept masters.
    judge(cart, new_shelves) -> score (higher better) or None when the store may not take the
    change (eligibility, caps, supply, sell-through), judged on the store as it stands;
    apply(cart, old_shelves, new_shelves) commits a change (and is called again with the
    arguments swapped to undo it). Repeatedly the master with the fewest carts is dissolved:
    each of its carts moves to the kept master of the same base length whose match scores best;
    a master whose carts cannot all move is kept (and its moves undone).
    Returns (moves [(cart, old_shelves, new_shelves)], master counts)."""
    apply = apply or (lambda c, old, new: None)
    mcount = Counter(c["master"] for c in carts)
    kept_hard = set()
    moves = []
    while len(mcount) > cap:
        cand = [m for m in mcount if m not in kept_hard]
        if not cand:
            break
        m0 = min(cand, key=lambda m: (mcount[m], len(m), m))
        plan, ok = [], True
        for c in [c for c in carts if c["master"] == m0]:
            best = None
            for m in mcount:
                if m == m0 or len(m) != len(m0):
                    continue
                idx = match_master(c["shelves"], m, compat)
                if idx is None:
                    continue
                new = list(c["shelves"])
                for i, b in zip(idx, sorted(m, key=lambda b: (len(shelf_pids(b)), b))):
                    new[i] = b
                sc = judge(c, new)
                if sc is not None and (best is None or (sc, mcount[m]) > best[0]):
                    best = ((sc, mcount[m]), new, m, tuple(sorted(idx)))
            if best is None:
                ok = False
                break
            old = (list(c["shelves"]), c["master"], c["base_idx"])
            apply(c, old[0], best[1])
            c["shelves"], c["master"], c["base_idx"] = best[1], best[2], best[3]
            plan.append((c, old))
        if not ok:
            for c, old in reversed(plan):
                apply(c, c["shelves"], old[0])
                c["shelves"], c["master"], c["base_idx"] = old
            kept_hard.add(m0)
            continue
        for c, old in plan:
            moves.append((c, old[0], c["shelves"]))
            mcount[c["master"]] += 1
        del mcount[m0]
    return moves, mcount
