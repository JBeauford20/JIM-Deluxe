"""Cart-config consolidation (§20): demand-neutral detection must never propose applying
a merge that changes a store's SKUs, trays or units."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from services.ipr_reviewer import analyze_consolidation  # noqa: E402


class FakeCur:
    """Serves the one cart-shelf query analyze_consolidation makes via _load_carts."""
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_a, **_k):
        pass

    def fetchone(self):
        return None                      # column probe: order_carts.handling_tag absent

    def fetchall(self):
        return self.rows


def shelf(cart, store, code, pos, sku, kit, trays=8, units=96, profile="p9"):
    return {"cart_id": cart, "order_id": f"o{store}", "store_id": store, "config_code": code,
            "template_id": f"t-{code}", "shelf_id": f"{cart}-{pos}", "shelf_position": pos,
            "sku_id": sku, "profile_id": profile, "kit_type": kit,
            "tray_count": trays, "unit_count": units}


FAMS = {101: "9cm Standard", 102: "9cm Standard", 201: "12cm Standard"}


KIT_ROWS = [
    shelf("c1", 1, "CC1", 1, 101, "jute_kit"), shelf("c1", 1, "CC1", 2, 201, "ceramic_kit"),
    shelf("c2", 2, "CC1", 1, 101, "jute_kit"), shelf("c2", 2, "CC1", 2, 201, "ceramic_kit"),
    shelf("c3", 3, "CC2", 1, 101, "ceramic_kit"), shelf("c3", 3, "CC2", 2, 201, "ceramic_kit"),
]


def test_kit_only_difference_is_neutral_when_kit_flexible():
    a = analyze_consolidation(FakeCur(KIT_ROWS), "run", FAMS, kit_flexible={101, 201})
    assert len(a["neutral_merges"]) == 1
    m = a["neutral_merges"][0]
    assert m["type"] == "kit_alignment"
    assert m["survivor"] == "CC1" and m["absorbed"] == "CC2"      # bigger config survives
    assert not a["proposals"]


def test_kit_difference_on_finished_product_is_proposal_only():
    # SKU 101 is 3M-only (not kit-flexible): jute vs ceramic are different physical goods.
    a = analyze_consolidation(FakeCur(KIT_ROWS), "run", FAMS, kit_flexible={201})
    assert not a["neutral_merges"]
    assert len(a["proposals"]) == 1
    assert a["proposals"][0]["differs"]["kit_skus"] == [101]


def test_kit_difference_with_unknown_flexibility_never_auto_merges():
    a = analyze_consolidation(FakeCur(KIT_ROWS), "run", FAMS)   # kit_flexible unknown (None)
    assert not a["neutral_merges"]
    assert len(a["proposals"]) == 1


def test_sku_swap_is_proposal_never_neutral():
    rows = [
        shelf("c1", 1, "CC1", 1, 101, "jute_kit"), shelf("c1", 1, "CC1", 2, 201, "ceramic_kit"),
        shelf("c3", 3, "CC2", 1, 102, "jute_kit"), shelf("c3", 3, "CC2", 2, 201, "ceramic_kit"),
    ]
    a = analyze_consolidation(FakeCur(rows), "run", FAMS)
    assert not a["neutral_merges"]
    assert len(a["proposals"]) == 1
    p = a["proposals"][0]
    assert {p["differs"]["sku_a"], p["differs"]["sku_b"]} == {101, 102}
    assert p["differs"]["family"] == "9cm Standard"


def test_cross_family_swap_not_even_proposed():
    fams = {101: "9cm Standard", 102: "12cm Standard", 201: "12cm Standard"}
    rows = [
        shelf("c1", 1, "CC1", 1, 101, "jute_kit"), shelf("c1", 1, "CC1", 2, 201, "ceramic_kit"),
        shelf("c3", 3, "CC2", 1, 102, "jute_kit"), shelf("c3", 3, "CC2", 2, 201, "ceramic_kit"),
    ]
    a = analyze_consolidation(FakeCur(rows), "run", fams)
    assert not a["neutral_merges"] and not a["proposals"]


def test_identical_configs_do_not_merge_with_themselves():
    rows = [shelf("c1", 1, "CC1", 1, 101, "jute_kit"),
            shelf("c2", 2, "CC1", 1, 101, "jute_kit")]
    a = analyze_consolidation(FakeCur(rows), "run", FAMS)
    assert not a["neutral_merges"] and not a["proposals"]
    assert a["config_sizes"] == {"CC1": 2}
