"""Cart-config streamlining search (workers/config_merge.py). Pure — no DB.
Run: python -m pytest tests/test_config_merge.py -q"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "workers"))
import config_merge as M  # noqa: E402

BASE = [("p", 1, 4), ("p", 2, 4), ("q", 3, 2)]


def cart(i, items, locked=False):
    return {"id": i, "sig": M.cart_sig(items), "locked": locked}


def test_signature_ignores_shelf_order():
    assert M.cart_sig(BASE) == M.cart_sig(list(reversed(BASE)))


def test_one_swap_joins_the_common_config():
    carts = [cart(i, BASE) for i in range(3)] + [cart(9, [("p", 1, 4), ("p", 5, 4), ("q", 3, 2)])]
    moves = M.streamline(carts, 1, lambda c, t, r, a: True)
    assert len(moves) == 1 and moves[0][0]["id"] == 9
    assert len({c["sig"] for c in carts}) == 1


def test_business_rule_veto_keeps_the_cart():
    carts = [cart(i, BASE) for i in range(3)] + [cart(9, [("p", 1, 4), ("p", 5, 4), ("q", 3, 2)])]
    assert M.streamline(carts, 2, lambda c, t, r, a: False) == []
    assert len({c["sig"] for c in carts}) == 2


def test_profiles_must_pair_and_change_limit_holds():
    # a different shelf type can never be swapped in
    assert not M.compatible([("p", 1, 4)], [("q", 1, 4)], 2)
    # two changes need max_changes >= 2
    assert not M.compatible([("p", 1, 4), ("p", 2, 4)], [("p", 5, 4), ("p", 6, 4)], 1)
    assert M.compatible([("p", 1, 4), ("p", 2, 4)], [("p", 5, 4), ("p", 6, 4)], 2)
    # adding or removing a whole shelf item is never a merge
    assert not M.compatible([("p", 1, 4)], [], 2)


def test_locked_carts_never_move_and_moves_go_to_the_more_common_config():
    other = [("p", 1, 4), ("p", 5, 4), ("q", 3, 2)]
    carts = [cart(0, BASE), cart(1, other), cart(2, other), cart(3, BASE, locked=True)]
    M.streamline(carts, 1, lambda c, t, r, a: True)
    assert carts[3]["sig"] == M.cart_sig(BASE)          # locked
    assert carts[0]["sig"] == M.cart_sig(other)         # 1 free BASE cart joins the 2-cart config
