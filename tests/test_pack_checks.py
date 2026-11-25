"""Pack-plan check list and floor sheets (pack team guide 2026-11-25). No DB.
Run: python -m pytest tests/test_pack_checks.py -q"""
import io
import os
import sys
import zipfile
from datetime import date

import openpyxl

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from services import floor_sheets as FS  # noqa: E402
from services import pack_checks as PC  # noqa: E402


def test_shelf_width_matches_guide_maths():
    assert PC.shelf_width([("euro12", 6)]) == 47.25            # 3 pairs x 15.75
    assert PC.shelf_width([("euro12", 5)]) == 42.5             # 2 pairs + odd tray at 11
    assert PC.shelf_width([("euro12", 1), ("hb12", 1)]) == 16  # odd std + odd HB pair at 16
    assert PC.shelf_width([("littles", 7)]) == 49.63
    # guide example: 3 x 17cm + 4 dish boxes = 57 3/8 in -> does not fit
    assert PC.shelf_width([("box17", 3), ("box8x2", 4)]) > PC.CC_FIT_IN


def test_cart_height_examples_from_guide():
    four_12 = {L: [("euro12", 6, 1, 36)] for L in range(1, 5)}
    assert not [f for f in PC.check_cart(four_12) if f[1] == "B"]          # 84 in ok
    five_12 = {L: [("euro12", 6, 1, 36)] for L in range(1, 6)}
    assert any(f[2] == "Cart over 95 in" for f in PC.check_cart(five_12))  # 105 in
    mix = {1: [("box17", 7, 2, 21)], 2: [("euro12", 6, 1, 36)], 3: [("littles", 7, 3, 84)],
           4: [("littles", 7, 3, 84)]}
    assert not [f for f in PC.check_cart(mix) if f[1] in ("A", "B")]      # 29+21+15+15 = 80


def test_17cm_cart_limited_to_four_shelves_and_bowl_cap():
    c = {1: [("box17", 7, 2, 21)], 2: [("littles", 7, 3, 84)], 3: [("littles", 7, 3, 84)],
         4: [("littles", 7, 3, 84)], 5: [("littles", 7, 3, 84)]}
    assert any(f[2] == "17cm cart over 4 shelves" for f in PC.check_cart(c))
    b = {1: [("bowlEU", 4, 1012142347, 24), ("cer9", 2, 5, 16)]}
    assert any("3 H2O Bowl" in f[2] for f in PC.check_cart(b))


def test_under_filled_shelf_flagged_and_shelf_order():
    c = {1: [("littles", 7, 3, 84)], 2: [("euro12", 2, 1, 12)]}
    titles = [f[2] for f in PC.check_cart(c)]
    assert "Shelf not full" in titles
    assert "Shelf order not tallest-first from shelf 1" in titles


def _rows():
    base = dict(ShipDate=date(2026, 11, 30), Route="49-N1", Upgrade=None, ItemDescription="12cm",
                ItemTrays=6, ItemEaches=36, ItemCustomerDescription="PW Calathea Assorted 12cm", Variation=1)
    return [dict(base, KitDescription="CC 2", KitQuantity=3, Layer=1, Notes=None, SKU="1009655619",
                 AsterCode="1000043", Trays=6, Eaches=36, **{"Total Used": 108}),
            dict(base, KitDescription="CC 2", KitQuantity=2, Layer=2, Notes="Mix A: 2 of 3 carts",
                 SKU="1012138479", AsterCode="1000038", Trays=7, Eaches=84, **{"Total Used": 168}),
            dict(base, KitDescription="CC 2", KitQuantity=1, Layer=2, Notes="Mix A: 1 of 3 carts",
                 SKU="1012138479", AsterCode="1000647", Trays=7, Eaches=84, **{"Total Used": 84}),
            dict(base, KitDescription="CC 1", KitQuantity=4, Layer=1, Notes=None, SKU="1009655619",
                 AsterCode="1009999", Trays=6, Eaches=36, **{"Total Used": 144})]


def test_floor_sheets_one_file_per_route_tabs_ordered_and_no_assorted_names():
    ref = {"skus": {1009655619: {"size_category": "12cm", "upc": "810000000001", "retail_price": 14.98},
                    1012138479: {"size_category": "9cm Littles", "upc": "810000000002", "retail_price": 5.98}},
           "aster_parsed": {"1000043": ("Calathea", "Insignis"), "1000038": ("Fittonia", "Lovers"),
                            "1000647": ("Peperomia", "Hope"), "1009999": ("Assorted", "Calathea Assorted")}}
    data, rep = FS.build_floor_sheets(_rows(), ref, 49)
    assert rep["files"] == ["Week 49 Floor Sheets 49-N1.xlsx"]
    assert rep["assorted_codes_for_catalog"] == ["1009999"]
    z = zipfile.ZipFile(io.BytesIO(data))
    wb = openpyxl.load_workbook(io.BytesIO(z.read(rep["files"][0])))
    assert wb.sheetnames == ["CC 1", "CC 2"]
    ws = wb["CC 2"]
    vals = [[c.value for c in r] for r in ws.iter_rows()]
    assert any(v[1] == "CART" or v[0] == "CART" for v in vals)
    assert [v for v in vals if v[0] == "# OF CARTS"][0][1] == 3
    # top shelf (2) printed before shelf 1; mixed slot counts once in the subtotal
    shelf_col = [v[1] for v in vals if isinstance(v[0], int) and isinstance(v[1], int)]   # item rows
    assert shelf_col[0] == 2
    totals = [v for v in vals if v[5] == "Shelf total"]
    assert totals[0][6] == 84 and totals[0][7] == 252
    assert all("Assorted" not in str(v[4] or "") for v in vals)


def test_full_shelf_means_no_more_trays_fit():
    import shelfkit as SK
    assert SK.shelf_is_full([("euro12", 6)])          # 47.25 in, a 7th tray won't fit
    assert not SK.shelf_is_full([("euro12", 5)])
    assert SK.shelf_is_full([("littles", 7)]) and SK.shelf_is_full([("box17", 7)])
    assert SK.shelf_is_full([("venti", 4)]) and SK.shelf_is_full([("hb12", 6)])
    assert SK.shelf_fill([("euro12", 6)]) == 1.0      # a full shelf counts 100% toward cart fill
