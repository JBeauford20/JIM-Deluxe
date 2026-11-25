"""Weekly cart menu (workers/cart_menu.py). Pure — no DB.
Run: python -m pytest tests/test_cart_menu.py -q"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "workers"))
import cart_menu as CM  # noqa: E402

# toy world: templates are product bundles; a store values a product it sells at 1 per unit
# up to its demand; a set qualifies only if every product is one the store sells
TEMPLATES = {"A": {"x": 2}, "B": {"y": 2}, "C": {"x": 1, "y": 1}, "D": {"z": 2}}
DEMAND = {1: {"x": 4}, 2: {"y": 2}, 3: {"x": 1, "y": 1}, 4: {"z": 1, "x": 1}}


def evaluate(store, ms):
    q = {}
    for m in ms:
        for p, n in TEMPLATES[m].items():
            q[p] = q.get(p, 0) + n
    if any(p not in DEMAND[store] for p in q):
        return None
    sold = sum(min(n, DEMAND[store][p]) for p, n in q.items())
    if sold / sum(q.values()) < 0.5:                         # sell-through gate
        return None
    return float(sold)


def test_signatures():
    c = [(1, 11, 6, 36, None, "euro12"), (2, 12, 3, 18, "jute_kit", "cer9")]
    assert CM.cart_signature(c) == CM.cart_signature(list(reversed(c)))
    assert CM.cart_signature(c) != CM.cart_signature(c, "NO HEDERA")
    assert CM.export_signature(c)[0] == ((1, 11, 6, ""), (2, 12, 3, "jute_kit"))


def test_best_set_fills_slots_and_respects_the_gate():
    s, v = CM.best_set(1, 2, ["A", "B", "C"], evaluate)
    assert s == ["A", "A"] and v == 4.0                     # store 1 sells only x
    s, v = CM.best_set(4, 2, ["A", "D"], evaluate)
    assert sorted(s) == ["A", "D"] and v == 2.0              # third pack would fail the gate
    assert CM.best_set(2, 1, ["A"], evaluate) == ([], 0.0)   # nothing qualifies: cart dropped


def test_select_menu_picks_most_valuable_templates_within_k():
    stores = {1: 2, 2: 1, 3: 1, 4: 1}
    menu, cur = CM.select_menu(stores, list(TEMPLATES), evaluate, 2)
    assert len(menu) == 2 and menu[0] == "A"
    assert cur[1][1] == 4.0
    menu1, _ = CM.select_menu(stores, list(TEMPLATES), evaluate, 0)
    assert menu1 == []
    full, cur = CM.select_menu(stores, list(TEMPLATES), evaluate, 10)
    assert len(full) <= 4 and cur[2][1] == 2.0 and cur[3][1] == 2.0
