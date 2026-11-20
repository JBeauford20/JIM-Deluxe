"""Unit tests for backend/services/v2_exports.py (pure builders; no DB).
Run: python -m pytest tests/test_v2_exports.py -q"""
import io
import os
import sys
from datetime import date, datetime

import openpyxl
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from services import v2_exports as X  # noqa: E402


def _ref(**over):
    stores = {903: {"store_id": 903, "store_name": "S Plainfield", "state": "NJ", "division": "North",
                    "merchant": "Christine Sweeney", "volume_code": "C", "store_status": "active"},
              111: {"store_id": 111, "store_name": "Blocked", "state": "GA", "division": "South",
                    "merchant": "X", "volume_code": "B", "store_status": "do_not_ship"}}
    skus = {1: {"sku_id": 1, "description": "PW Calathea Assorted 17cm", "family": "17cm",
                "ipr_family": "17cm Standard", "program": "17cm"},
            2: {"sku_id": 2, "description": "PW LEAFJOY LITTLES 9CM", "family": "9cm Littles",
                "ipr_family": "9cm Standard", "program": "Littles"}}
    avail = {"L1": {"plant_id": "1000183", "code_level": "1M", "hard_good_type": "grower_pot", "sku_id": 1},
             "L2": {"plant_id": "1000300", "code_level": "1M", "hard_good_type": "grower_pot", "sku_id": 1},
             "L3": {"plant_id": "1000936", "code_level": "1M", "hard_good_type": "grower_pot", "sku_id": 2}}
    apm = [{"aster_code": "1000183", "aster_name": "17cm Monstera Deliciosa 17cm Everyday",
            "code_level": "1M", "hard_good_type": "grower_pot"},
           {"aster_code": "3000183", "aster_name": "17cm Monstera Deliciosa 17cm Jute Kit",
            "code_level": "3M", "hard_good_type": "jute_kit"},
           {"aster_code": "3000182", "aster_name": "17cm Monstera Deliciosa 17cm Ceramic Kit",
            "code_level": "3M", "hard_good_type": "ceramic_kit"}]
    names = {m["aster_code"]: m["aster_name"] for m in apm}
    profiles = {"box17": {"tray_width_mm": 180.975, "tray_depth_mm": 552.45, "clear_height_in": 29,
                          "units_per_tray": 3},
                "littles": {"tray_width_mm": 180, "tray_depth_mm": 540, "clear_height_in": 15,
                            "units_per_tray": 12}}
    ref = X.build_reference(stores, skus, avail, apm, names, profiles)
    ref.update(over)
    return ref


def _model():
    carts = [{"id": f"c{i}", "order_id": "o1", "store_id": 903, "cart_number": i, "config_code": "CC1",
              "cart_key": f"K{i}"} for i in (1, 2, 3)]
    shelves = []
    for i, c in enumerate(carts):
        # 3 carts x 2 trays of 17cm; aster split 4 trays L1 / 2 trays L2 -> base 1 + mix
        shelves.append({"cart_id": c["id"], "shelf_position": 1, "sku_id": 1, "profile_id": "box17",
                        "kit_type": "jute_kit", "tray_count": 1 if i < 2 else 2,
                        "unit_count": 3 if i < 2 else 6, "availability_line_id": "L1" if i < 2 else "L1"})
        if i < 2:
            shelves.append({"cart_id": c["id"], "shelf_position": 1, "sku_id": 1, "profile_id": "box17",
                            "kit_type": "jute_kit", "tray_count": 1, "unit_count": 3,
                            "availability_line_id": "L2"})
        shelves.append({"cart_id": c["id"], "shelf_position": 2, "sku_id": 2, "profile_id": "littles",
                        "kit_type": None, "tray_count": 7, "unit_count": 84, "availability_line_id": "L3"})
    return {"run": {"id": "r", "batch_id": "b", "shipping_week": date(2026, 11, 23)},
            "orders": [{"id": "o1", "store_id": 903, "status": "approved", "cart_count": 3}],
            "carts": carts, "shelves": shelves,
            "loads": [{"id": "l1", "load_name": "48-N1", "ship_date": date(2026, 11, 23)}],
            "assignments": [{"load_id": "l1", "order_cart_id": c["id"], "store_id": 903, "stop_sequence": 1}
                            for c in carts]}


def test_config_names_order_ties_and_permanent():
    def cart(cid, store, sku, code="CC9"):
        return {"id": cid, "order_id": "o", "store_id": store, "cart_number": 1, "config_code": code}
    def shelf(cid, sku, trays=2):
        return {"cart_id": cid, "shelf_position": 1, "sku_id": sku, "kit_type": None, "tray_count": trays}
    m = {"carts": [cart("a", 500, 1), cart("b", 600, 1), cart("c", 300, 2), cart("d", 200, 3),
                   cart("p", 100, 4, code="PC Collector cart")],
         "shelves": [shelf("a", 1), shelf("b", 1), shelf("c", 2), shelf("d", 3), shelf("p", 4)]}
    by_cart, names, new = X.assign_config_names(m)
    assert by_cart == {"a": "CC 1", "b": "CC 1", "d": "CC 2", "c": "CC 3", "p": "Collector cart"}
    # persisted names are kept; a new signature continues after the highest CC number
    m["carts"].append(cart("e", 50, 5)); m["shelves"].append(shelf("e", 5))
    by_cart2, _, new2 = X.assign_config_names(m, names)
    assert by_cart2["a"] == "CC 1" and by_cart2["c"] == "CC 3" and by_cart2["e"] == "CC 4"
    assert [r[1] for r in new2] == ["CC 4"]
    # segments of one shelf (split across availability lines) aggregate to one signature
    seg = [dict(shelf("a", 1, 1)), dict(shelf("a", 1, 1))]
    assert X.cart_signature(cart("a", 1, 1), seg) == X.cart_signature(cart("a", 1, 1), [shelf("a", 1, 2)])


def test_largest_remainder_conserves():
    assert sum(X._largest_remainder(7, [5, 3, 1])) == 7
    assert X._largest_remainder(0, [1, 2]) == [0, 0]
    assert sum(X._largest_remainder(5, [0, 0])) == 5


def test_item_description():
    assert X.item_description({"ipr_family": "12cm Standard"}) == "12cm"
    assert X.item_description({"ipr_family": "17cm Rare Collector"}) == "17cm Collector"
    assert X.item_description({"ipr_family": "12cm Canopy/Hanging Basket"}) == "12cm HB"
    assert X.item_description({"ipr_family": "Climb-ITT", "program": "12cm Climb-itt"}) == "12cm Climb-itt"
    assert X.item_description({"ipr_family": "H2O", "description": "PW LEAFJOY 9CM HANGING BASKET - 9cm"}) \
        == "PW LEAFJOY 9CM HANGING BASKET"


def test_stores_and_carts_layout():
    wb, rep = X.build_stores_and_carts(_model(), _ref())
    ws = wb.active
    assert [c.value for c in ws[1]][:11] == X.SC_META_HEADERS
    row = [c.value for c in ws[2]]
    assert row[:11] == [903, "", "North", "0903-S PLAINFIELD", "NJ", "C", "Christine Sweeney", "", 3, 3, 0]
    assert ws.cell(1, 12).value == "CC 1" and ws.cell(2, 12).value == 3
    assert rep["distinct_configs"] == 1


def test_pack_plan_conserves_units_and_mixes():
    m, ref = _model(), _ref()
    rows, layouts, _ = X.pack_plan_rows(m, ref)
    assert sum(r["Total Used"] for r in rows) == sum(s["unit_count"] for s in m["shelves"])
    trays17 = sum(r["KitQuantity"] * r["Trays"] for r in rows if r["SKU"] == "1")
    assert trays17 == 6
    assert any((r["Notes"] or "").startswith("Mix A:") for r in rows)
    assert layouts[0]["Shelf heights"] == "29 / 15" and layouts[0]["Height (in)"] == 44
    wb, rep = X.build_pack_plan(m, ref)
    assert wb.sheetnames == ["Bloomaker by Day", "Soil by Day", "Totals Used", "Routing Report",
                             "Deductions", "Sheet", "Changes", "Availability Subs", "Cart Layouts"]
    assert [c.value for c in wb["Sheet"][1]] == X.PP_SHEET_HEADERS
    assert wb["Sheet"]["Q2"].value == "=D2*P2"
    assert [c.value for c in wb["Availability Subs"][1]] == X.SUBS_HEADERS
    assert rep["unrouted_rows"] == 0


def test_aster_kits_12_17cm_only_and_blocks():
    m, ref = _model(), _ref()
    m["orders"].append({"id": "o2", "store_id": 111, "status": "approved", "cart_count": 1})
    m["carts"].append({"id": "cx", "order_id": "o2", "store_id": 111, "cart_number": 1, "config_code": "CC2"})
    m["shelves"].append({"cart_id": "cx", "shelf_position": 1, "sku_id": 2, "profile_id": "littles",
                         "kit_type": None, "tray_count": 1, "unit_count": 12, "availability_line_id": "L3"})
    m["shelves"][6]["kit_type"] = "ceramic_kit"   # cart 3's 17cm shelf goes ceramic
    text, rep = X.build_aster_csv(m, ref)
    lines = text.strip().splitlines()
    assert lines[0] == ",".join(X.ASTER_HEADERS)
    body = {tuple(l.split(",")[:3]) for l in lines[1:] if not l.startswith("#")}
    assert ("903", "3000183", "6") in body        # 2 carts x 3u jute -> 3M jute kit
    assert ("903", "3000182", "6") in body        # ceramic cart -> 3M ceramic kit
    assert ("903", "1000936", "252") in body      # 9cm littles stay 1M
    assert not any(b[0] == "111" for b in body)   # do_not_ship store excluded
    assert any(g["reason"].startswith("no 3M jute_kit") for g in rep["gaps"])  # L2 variety has no kit


def test_aster_drafts_excluded_by_default():
    m = _model()
    m["orders"][0]["status"] = "draft"
    assert X.build_aster_csv(m, _ref())[0] is None
    assert X.build_aster_csv(m, _ref(), include_draft=True)[0]


def _workbook_bytes():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Compiled"
    ws.append(["Store", "Load", "Ship Date", "Delivery Date"])
    ws.append(["903", "48-N1", datetime(2026, 11, 23), datetime(2026, 11, 24)])
    ws.append(["903", "48-N2", datetime(2026, 11, 24), datetime(2026, 11, 25)])
    ws.append(["912", "48-N1", datetime(2026, 11, 23), datetime(2026, 11, 24)])
    ws.append(["SHIP-000845 Byron Ace", "48-N2", datetime(2026, 11, 24), datetime(2026, 11, 25)])
    north = wb.create_sheet("North")
    north.append(["Store", "Load", "Drop", "Carts"])
    north.append(["903", "48-N1", 9, 99])
    for name, rows in (("48-N1", [("912", 1, 2), ("903", 2, 1)]), ("48-N2", [("903", 1, 2)])):
        s = wb.create_sheet(name)
        s.append(["Store", "Carts", "Load", "Drop", "Del Time", "Split Stop"])
        for st, drop, carts in rows:
            s.append([st, carts, name, drop, "7AM-8PM", "split" if st == "903" else None])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def test_parse_and_plan_split_stop():
    p = X.parse_routing_workbook(_workbook_bytes())
    assert p["loads"] == ["48-N1", "48-N2"] and p["load_sheets"] == ["48-N1", "48-N2"]
    assert p["non_store_rows"] == ["SHIP-000845 Byron Ace"]
    e = {(x["store_id"], x["route"]): x for x in p["entries"]}
    assert e[(903, "48-N1")]["drop"] == 2 and e[(903, "48-N1")]["carts"] == 1   # region sheet ignored
    assert e[(903, "48-N2")]["deliv"] == date(2026, 11, 25)
    plan = X.plan_routes(p["entries"], {903: ["a", "b", "c"], 912: ["d", "e"], 5: ["z"]})
    assert [s["store_id"] for s in plan["loads"]["48-N1"]["stops"]] == [912, 903]   # by Drop
    assert plan["carts_assigned"] == 5 and plan["stores_in_run_not_routed"] == [5]
    split = {k: [c for s in v["stops"] if s["store_id"] == 903 for c in s["cart_ids"]]
             for k, v in plan["loads"].items()}
    assert split == {"48-N1": ["a"], "48-N2": ["b", "c"]}
    legacy = X.plan_routes(p["entries"], {903: ["a", "b", "c"]}, allow_split=False)
    assert legacy["stores_on_multiple_routes"] == [903] and legacy["carts_assigned"] == 3
