"""Master + flex cart configurations (workers/master_flex.py). Pure — no DB."""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "workers"))
import master_flex as MF  # noqa: E402

CAP = {"littles": 7, "cer9": 6, "euro12": 6}
cap_of = lambda pid, skus: CAP[pid]
LIT = (("L", 7, "littles"),)
CER = (("C", 6, "cer9"),)


def test_n_base():
    assert [MF.n_base(n) for n in (5, 4, 3, 2, 1)] == [3, 2, 1, 0, 0]


def test_regroup_makes_single_sku_shelves_without_changing_quantities():
    mixed = (("A", 3, "euro12"), ("B", 3, "euro12"))
    mixed2 = (("A", 3, "euro12"), ("B", 3, "euro12"))
    out = MF.regroup_store([[mixed, LIT], [mixed2, CER]], cap_of)
    shelves = [b for c in out for b in c]
    assert (("A", 6, "euro12"),) in shelves and (("B", 6, "euro12"),) in shelves
    tot = Counter()
    for b in shelves:
        for sk, tr, _ in b:
            tot[sk] += tr
    assert tot == Counter({"A": 6, "B": 6, "L": 7, "C": 6})
    assert [sorted(len(MF.shelf_pids(b)) for b in c) for c in out] == [[1, 1], [1, 1]]


def test_choose_bases_shares_common_shelves():
    odd1, odd2 = (("X", 6, "euro12"),), (("Y", 6, "euro12"),)
    carts = [{"store": 1, "shelves": [LIT, LIT, odd1, CER]},
             {"store": 2, "shelves": [odd2, LIT, CER, LIT]}]
    m = MF.choose_bases(carts)
    assert len(m) == 1 and carts[0]["master"] == carts[1]["master"]


def test_standardise_moves_rare_master_when_judge_allows():
    X, Y, Z, W, V, U = [((k, 6, "euro12"),) for k in "XYZWVU"]
    mk = lambda: [{"store": 1, "shelves": [LIT, LIT, X, Y]}, {"store": 2, "shelves": [LIT, LIT, Z, W]},
                  {"store": 3, "shelves": [CER, CER, V, U]}]
    carts = mk()
    assert len(MF.choose_bases(carts)) == 2
    # same profile only: CER shelves cannot become LIT shelves -> master kept
    _m, mc = MF.standardise(carts, 1, judge=lambda cart, new: 0.0)
    assert len(mc) == 2
    # with a compatibility rule (same rung) the rare master moves onto the common one
    carts = mk()
    MF.choose_bases(carts)
    moves, mc = MF.standardise(carts, 1, judge=lambda cart, new: 0.0, compat=lambda s, b: True)
    assert len(mc) == 1 and len(moves) == 1 and carts[2]["shelves"][:2] == [LIT, LIT]
    # a judge that refuses keeps it
    carts = mk()
    MF.choose_bases(carts)
    _m, mc = MF.standardise(carts, 1, judge=lambda cart, new: None, compat=lambda s, b: True)
    assert len(mc) == 2
