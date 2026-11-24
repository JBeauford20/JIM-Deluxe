"""Order -> carts -> pack plan -> Stores & Carts -> Aster reconciliation (Jason's review, point 5;
Josh 2026-11-24: "a missing mapping or mismatch should prevent release").

Every unit the engine saved for a store x SKU must appear, unchanged, in each file built from
the run. One mismatch or one unmapped Aster line = FAIL, and release is refused until fixed.

Layers compared (units unless noted):
  1. order lines (approved, else draft, else recommended qty)   vs cart shelves     store x SKU
  2. cart shelves                                                vs Aster CSV         store x SKU
     (every shelf needs an Aster code; the CSV's store x code rows must add up per store)
  3. cart shelves                                                vs pack plan         SKU (network)
  4. cart count per store                                        vs Stores & Carts    store (carts)
"""
import csv
import io
from collections import defaultdict

from services import v2_exports as v2x

MAX_ROWS = 200      # detail rows returned per layer (counts are always complete)


def reconcile(model: dict, ref: dict, order_units: dict, exempt_families=()) -> dict:
    """model/ref as built for the exports (v2_exports.load_run / load_reference);
    order_units: {(store_id, sku_id): units} from order_lines; exempt_families: product families
    deliberately handled outside Aster (Policies: aster_reconcile_exempt_families) — their
    unmapped lines are listed as exempt, not failures. Pure: no DB."""
    exempt_families = {str(f).strip() for f in (exempt_families or ()) if str(f).strip()}
    cart_store = {c["id"]: c["store_id"] for c in model["carts"]}
    shelf_units = defaultdict(int)
    for s in model["shelves"]:
        sid = cart_store.get(s["cart_id"])
        if sid is not None and s.get("unit_count"):
            shelf_units[(sid, s["sku_id"])] += int(s["unit_count"])
    issues = {"order_vs_carts": [], "carts_vs_aster": [], "unmapped_aster": [],
              "carts_vs_pack_plan": [], "carts_vs_stores_and_carts": []}
    exempt = []

    # 1. order lines vs carts
    for k in sorted(set(order_units) | set(shelf_units)):
        a, b = int(order_units.get(k, 0) or 0), shelf_units.get(k, 0)
        if a != b:
            issues["order_vs_carts"].append({"store_id": k[0], "sku_id": k[1], "order_units": a,
                                             "cart_units": b, "difference": b - a})

    # 2. carts vs Aster (per shelf mapping, then the CSV text itself)
    mapped = defaultdict(int)                  # (store, sku) -> units with an Aster code
    by_code = defaultdict(int)                 # (store, code) -> units expected in the CSV
    for s in model["shelves"]:
        sid = cart_store.get(s["cart_id"])
        if sid is None or not s.get("unit_count"):
            continue
        code, _kit, gap = v2x.aster_code_for(s, ref)
        if code is None:
            sk = ref["skus"].get(s["sku_id"]) or {}
            row = {"store_id": sid, "sku_id": s["sku_id"], "units": int(s["unit_count"]), "reason": gap,
                   "family": sk.get("ipr_family") or sk.get("family")}
            if {sk.get("ipr_family"), sk.get("family")} & exempt_families:
                exempt.append(row)
            else:
                issues["unmapped_aster"].append(row)
            continue
        mapped[(sid, s["sku_id"])] += int(s["unit_count"])
        by_code[(sid, code)] += int(s["unit_count"])
    text, _rep = v2x.build_aster_csv(model, ref, include_draft=True)
    csv_qty = defaultdict(int)
    if text:
        for row in csv.reader(io.StringIO(text)):
            if not row or row[0].startswith("#") or not row[0].strip().isdigit():
                continue
            csv_qty[(int(row[0]), str(row[1]))] += int(float(row[2] or 0))
    for k in sorted(set(by_code) | set(csv_qty), key=str):
        if by_code.get(k, 0) != csv_qty.get(k, 0):
            issues["carts_vs_aster"].append({"store_id": k[0], "aster_code": k[1],
                                             "cart_units": by_code.get(k, 0),
                                             "aster_units": csv_qty.get(k, 0)})

    # 3. carts vs pack plan (network units per SKU)
    rows, _layouts, _notes = v2x.pack_plan_rows(model, ref)
    pp = defaultdict(int)
    for r in rows:
        pp[int(r["SKU"])] += int(r["Total Used"] or 0)
    by_sku = defaultdict(int)
    for (sid, sk), u in shelf_units.items():
        by_sku[sk] += u
    for sk in sorted(set(pp) | set(by_sku)):
        if pp.get(sk, 0) != by_sku.get(sk, 0):
            issues["carts_vs_pack_plan"].append({"sku_id": sk, "cart_units": by_sku.get(sk, 0),
                                                 "pack_plan_units": pp.get(sk, 0)})

    # 4. carts per store vs Stores & Carts
    _wb, sc = v2x.build_stores_and_carts(model, ref)
    for sid in sc.get("cart_count_mismatch") or []:
        issues["carts_vs_stores_and_carts"].append({"store_id": sid,
                                                    "note": "desired cart count differs from carts built"})

    counts = {k: len(v) for k, v in issues.items()}
    totals = {"order_units": sum(int(v or 0) for v in order_units.values()),
              "cart_units": sum(shelf_units.values()),
              "aster_units": sum(csv_qty.values()),
              "pack_plan_units": sum(pp.values()),
              "unmapped_units": sum(x["units"] for x in issues["unmapped_aster"]),
              "exempt_units": sum(x["units"] for x in exempt),
              "stores": len({k[0] for k in shelf_units}), "store_sku_pairs": len(shelf_units)}
    status = "PASS" if not any(counts.values()) else "FAIL"
    return {"status": status, "counts": counts, "totals": totals,
            "issues": {k: v[:MAX_ROWS] for k, v in issues.items()},
            "exempt_from_aster": exempt[:MAX_ROWS], "exempt_families": sorted(exempt_families)}


def reconcile_run(cur, run_id: str) -> dict:
    """Load the run like the exports do (dict cursor) and reconcile it. Read-only."""
    model = v2x.load_run(cur, run_id)
    if not model:
        return {"status": "FAIL", "counts": {}, "totals": {}, "issues": {}, "error": "run not found"}
    ref = v2x.load_reference(cur, model["run"]["batch_id"])
    cur.execute("""SELECT o.store_id, l.sku_id,
                          SUM(COALESCE(l.approved_qty, l.draft_qty, l.recommended_qty)) AS u
                   FROM order_lines l JOIN orders o ON o.id = l.order_id
                   WHERE o.run_id = %s GROUP BY 1, 2""", (run_id,))
    order_units = {(r["store_id"], r["sku_id"]): int(r["u"] or 0) for r in cur.fetchall()}
    cur.execute("SELECT value FROM ipr_settings WHERE key = 'aster_reconcile_exempt_families'")
    row = cur.fetchone()
    exempt = (row["value"] if row else None) or []
    return reconcile(model, ref, order_units, exempt)
