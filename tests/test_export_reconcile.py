"""Release reconciliation (services/export_reconcile.py; Jason's review point 5).
The export builders are stubbed so each layer can be broken on purpose. Pure — no DB.
Run: python -m pytest tests/test_export_reconcile.py -q"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from services import export_reconcile as R  # noqa: E402

MODEL = {"carts": [{"id": "c1", "store_id": 101}, {"id": "c2", "store_id": 102}],
         "shelves": [{"cart_id": "c1", "sku_id": 1, "unit_count": 12, "line": "L1"},
                     {"cart_id": "c1", "sku_id": 1, "unit_count": 12, "line": "L1"},
                     {"cart_id": "c2", "sku_id": 2, "unit_count": 6, "line": "L2"}]}
REF = {"skus": {1: {"ipr_family": "9cm Standard"}, 2: {"ipr_family": "Prebuilt Cart"}}}
CODES = {"L1": "A100", "L2": "B200"}


def stub(monkeypatch, codes=CODES, csv_extra=None, pack=None, sc_mismatch=()):
    def code_for(s, ref):
        c = codes.get(s["line"])
        return (c, "", None) if c else (None, "", "no availability line (no Aster variety assigned)")

    def aster_csv(model, ref, include_draft=False):
        tot = {}
        for s in model["shelves"]:
            c = codes.get(s["line"])
            if c:
                sid = {"c1": 101, "c2": 102}[s["cart_id"]]
                tot[(sid, c)] = tot.get((sid, c), 0) + s["unit_count"]
        tot.update(csv_extra or {})
        return "StoreId,Code,Qty,Name,Kit\n" + "".join(f"{k[0]},{k[1]},{v},,\n" for k, v in tot.items()), {}

    monkeypatch.setattr(R.v2x, "aster_code_for", code_for)
    monkeypatch.setattr(R.v2x, "build_aster_csv", aster_csv)
    monkeypatch.setattr(R.v2x, "pack_plan_rows", lambda m, r: (
        [{"SKU": str(k), "Total Used": v} for k, v in (pack or {1: 24, 2: 6}).items()], [], {}))
    monkeypatch.setattr(R.v2x, "build_stores_and_carts",
                        lambda m, r: (None, {"cart_count_mismatch": list(sc_mismatch)}))


ORDER = {(101, 1): 24, (102, 2): 6}


def test_everything_matches_passes(monkeypatch):
    stub(monkeypatch)
    r = R.reconcile(MODEL, REF, ORDER)
    assert r["status"] == "PASS" and r["totals"]["aster_units"] == 30


def test_jasons_case_missing_mapping_blocks(monkeypatch):
    # 24 units saved; one 12-unit line has no Aster code -> the file would carry 12. FAIL.
    model = {**MODEL, "shelves": [dict(MODEL["shelves"][0]), {**MODEL["shelves"][1], "line": "L9"},
                                  dict(MODEL["shelves"][2])]}
    stub(monkeypatch)
    r = R.reconcile(model, REF, ORDER)
    assert r["status"] == "FAIL" and r["counts"]["unmapped_aster"] == 1
    assert r["totals"]["aster_units"] == 18 and r["totals"]["unmapped_units"] == 12


def test_order_and_carts_disagree_blocks(monkeypatch):
    stub(monkeypatch)
    r = R.reconcile(MODEL, REF, {(101, 1): 36, (102, 2): 6})
    assert r["status"] == "FAIL" and r["issues"]["order_vs_carts"][0]["difference"] == -12


def test_aster_file_and_pack_plan_and_stores_and_carts_each_checked(monkeypatch):
    stub(monkeypatch, csv_extra={(101, "A100"): 20})
    assert R.reconcile(MODEL, REF, ORDER)["counts"]["carts_vs_aster"] == 1
    stub(monkeypatch, pack={1: 12, 2: 6})
    assert R.reconcile(MODEL, REF, ORDER)["counts"]["carts_vs_pack_plan"] == 1
    stub(monkeypatch, sc_mismatch=[101])
    assert R.reconcile(MODEL, REF, ORDER)["counts"]["carts_vs_stores_and_carts"] == 1


def test_exempt_family_is_listed_not_blocking(monkeypatch):
    stub(monkeypatch, codes={"L1": "A100"})          # the pre-built cart line has no Aster code
    assert R.reconcile(MODEL, REF, ORDER)["status"] == "FAIL"
    r = R.reconcile(MODEL, REF, ORDER, exempt_families=["Prebuilt Cart"])
    assert r["status"] == "PASS" and r["totals"]["exempt_units"] == 6
