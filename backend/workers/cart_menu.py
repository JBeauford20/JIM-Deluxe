"""Weekly CART MENU (leadership 11/25: at most 50 unique cart configurations a week; the pack
team builds from named kits). Pure Python, no DB: the engine supplies `evaluate`, which knows
the store's demand, rules, tray caps, supply and sell-through gate.

    evaluate(store, [template, ...]) -> projected sold $ of that set of carts at the store,
                                        or None when the set does not qualify there.

select_menu() picks at most K templates (greedy on the total projected sold $ the menu lets
the stores keep); best_set() then gives each store its best qualifying set of menu carts.
"""
from __future__ import annotations


def cart_signature(contents, tag=""):
    """Config key of a cart before kits are assigned: (shelf position, sku, trays, profile)."""
    return (tuple(sorted((p, sk, tr, pid) for (p, sk, tr, _u, _k, pid) in contents)), tag or "")


def export_signature(contents, tag=""):
    """Config key the exports use after kit assignment: (position, sku, trays, kit) + handling tag."""
    return (tuple(sorted((p, sk, tr, kt or "") for (p, sk, tr, _u, kt, _pid) in contents)), tag or "")


def _options(cur, n, m):
    """Sets reachable from `cur` by adding m (once, or in every free slot) or swapping one slot for m."""
    out = []
    if len(cur) < n:
        out.append(cur + [m])
        if n - len(cur) > 1:
            out.append(cur + [m] * (n - len(cur)))      # every free slot with it
    for i in range(len(cur)):
        if cur[i] != m:
            out.append(cur[:i] + [m] + cur[i + 1:])
    return out


def best_set(store, n, menu, evaluate, swaps=2):
    """Best qualifying set of at most n menu carts for one store: fill the slots one at a time
    with the menu cart adding the most value, then try single swaps. Returns (set, value)."""
    cur, val = [], 0.0
    for _ in range(n):
        best = None
        for m in menu:
            v = evaluate(store, cur + [m])
            if v is not None and v > val + 1e-9 and (best is None or v > best[1]):
                best = (m, v)
        if best is None:
            break
        cur, val = cur + [best[0]], best[1]
    for _ in range(swaps):
        improved = False
        for i in range(len(cur)):
            for m in menu:
                if m == cur[i]:
                    continue
                trial = cur[:i] + [m] + cur[i + 1:]
                v = evaluate(store, trial)
                if v is not None and v > val + 1e-9:
                    cur, val, improved = trial, v, True
        if not improved:
            break
    return cur, val


def select_menu(stores, templates, evaluate, k, fits=None):
    """Greedy menu: at most k templates. stores {store: number of routine carts}.
    fits {template: stores where it qualifies on its own} limits the work (default: all).
    Each round adds the template that raises the stores' total value most, where a store's
    value is its current set improved by adding / swapping in that one template.
    Returns (menu, {store: (set, value)})."""
    menu = []
    cur = {s: ([], 0.0) for s in stores}
    cand = list(templates)
    for _ in range(max(0, k)):
        best = None
        for m in cand:
            if m in menu:
                continue
            gain, upd = 0.0, {}
            for s in (fits.get(m, ()) if fits is not None else stores):
                cs, cv = cur[s]
                bo = None
                for o in _options(cs, stores[s], m):
                    v = evaluate(s, o)
                    if v is not None and v > cv + 1e-9 and (bo is None or v > bo[1]):
                        bo = (o, v)
                if bo:
                    gain += bo[1] - cv
                    upd[s] = bo
            if gain > 1e-9 and (best is None or gain > best[1]):
                best = (m, gain, upd)
        if best is None:
            break
        menu.append(best[0])
        cur.update(best[2])
    return menu, cur


def select_menu_supply(stores, templates, evaluate, units_of, supply, k, fits, order):
    """Supply-aware greedy menu. Each round simulates the template's take-up store by store
    (`order`: best stores first) against the units still free, a store adding it or swapping
    one of its carts for it only when the extra units it needs are free; the template whose
    take-up adds the most value is kept and its take-up committed.
    evaluate(store, set) -> value | None; units_of(store, set) -> {sku: units}.
    Returns (menu, {store: (set, value, units)}, units left)."""
    menu = []
    cur = {s: ([], 0.0, {}) for s in stores}
    live = dict(supply)
    fit = {m: set(v) for m, v in fits.items()}
    for _ in range(max(0, k)):
        best = None
        for m in templates:
            if m in menu or not fit.get(m):
                continue
            tmp, gain, upd = {}, 0.0, {}
            for s in order:
                if s not in fit[m]:
                    continue
                cs, cv, cu = cur[s]
                bo = None
                for o in _options(cs, stores[s], m):
                    v = evaluate(s, o)
                    if v is None or v <= cv + 1e-9 or (bo is not None and v <= bo[1]):
                        continue
                    u = units_of(s, o)
                    if all(u.get(sk, 0) - cu.get(sk, 0) <= live.get(sk, 0) - tmp.get(sk, 0) for sk in u):
                        bo = (o, v, u)
                if bo:
                    gain += bo[1] - cv
                    upd[s] = bo
                    for sk in set(bo[2]) | set(cu):
                        tmp[sk] = tmp.get(sk, 0) + bo[2].get(sk, 0) - cu.get(sk, 0)
            if gain > 1e-9 and (best is None or gain > best[1]):
                best = (m, gain, upd, tmp)
        if best is None:
            break
        menu.append(best[0])
        cur.update(best[2])
        for sk, d in best[3].items():
            live[sk] = live.get(sk, 0) - d
    return menu, cur, live
