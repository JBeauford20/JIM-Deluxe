"""IPR v2 run exports — the files routing and operations actually receive.

Pure builders over a plain-dict "run model" so the same code serves the API (loaded
from the DB by `load_run` / `load_reference`) and offline proofs (fixtures).

  build_stores_and_carts  -> "Week NN Stores and Carts.xlsx"   (to the routing team)
  parse_routing_workbook  <- "Week NN-YYYY Workbook.xlsx"      (from the routing team)
  plan_routes             -> per-load stops/cart assignment from the parsed workbook
  build_pack_plan         -> "Week NN Pack Plan File.xlsx"     (to operations)
  build_aster_csv         -> Aster ERP import (3M kit codes for 12/17cm jute/ceramic)

Layouts mirror the Week 46/47 files in
  TPC Sales - Documents/! Clients - z Big Box/Home Depot/2026/shipments/WK 46|47/.

Run model (all plain dicts / lists):
  run        {id, batch_id, shipping_week(date)}
  orders     [{id, store_id, status, cart_count, total_units}]
  carts      [{id, order_id, store_id, cart_number, config_code, cart_key}]
  shelves    [{cart_id, shelf_position, sku_id, profile_id, kit_type, tray_count,
               unit_count, availability_line_id}]
  loads      [{id, load_name, ship_date, delivery_date, source}]
  assignments[{load_id, order_cart_id, store_id, stop_sequence}]
Reference model: see `load_reference`.
"""
from __future__ import annotations

import csv
import io
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime

try:                                    # store handling rules (workers/ is not a package)
    import store_rules
except ImportError:                     # app process: add backend/workers to the path
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "workers"))
    import store_rules

# ── constants ────────────────────────────────────────────────────────────────
CART_SHELF_DEPTH_MM = 22.25 * 25.4          # cart_types 'cc' shelf_depth_in
BLOCK_STATUSES = ("do_not_ship", "paused", "inactive", "hold")
KIT_FAMILIES = {"12cm", "17cm"}             # skus.family values Aster kits as 3M jute/ceramic
KIT_TYPES = ("jute_kit", "ceramic_kit")
ROUTE_RE = re.compile(r"^[\w\s\-./#]{1,40}$")
UNROUTED = "UNROUTED"

SC_META_HEADERS = ["Store", "Address", "Division", "City", "State", "Vol", "Merchant", "Notes",
                   "Desired Cart Count", "Sum of Carts", "Cart Difference"]
SC_META_WIDTHS = [9.86, 13.14, 12.71, 63.71, 9.71, 8.14, 19.57, 10.29, 14.57, 10.86, 12.14]

PP_SHEET_HEADERS = ["ShipDate", "Route", "KitDescription", "KitQuantity", "Variation", "Layer",
                    "Upgrade", "Notes", "ItemDescription", "ItemTrays", "ItemEaches",
                    "ItemCustomerDescription", "SKU", "AsterCode", "Trays", "Eaches", "Total Used"]
PP_SHEET_WIDTHS = [10.6, 8.1, 23.8, 12.3, 10.7, 7.7, 10.1, 17.9, 46.6, 11.3, 12.7, 40.7, 11.0,
                   11.7, 7.6, 8.9, 12.0]
CHANGES_HEADERS = ["Type", "Route", "Cart", "Shelf", "What", "Detail", "Units (all carts)"]
# Availability Subs = the post-release substitution file template (one row per
# route x cart config x shelf x code swap). Columns, as used in Week 47:
#   Route               load name (e.g. 47-N2)
#   Cart                cart config name as printed on the Cart Layouts / Sheet tabs
#   Shelf               layer number (1 = bottom / tallest shelf)
#   Change              human summary, e.g. "1000453 -> 1002268 (mixed)" or "12cm trial -> 1001629"
#   New Aster           Aster code going ON the shelf
#   New description     Aster product name of the new code
#   Units in            units of the new code across all carts of that config on that route
#   Units off old code  negative units removed from the old code (blank for placeholder fills)
#   Old Aster           Aster code coming OFF (or a placeholder such as TRIAL)
#   Old description     Aster product name / placeholder text of the old code
SUBS_HEADERS = ["Route", "Cart", "Shelf", "Change", "New Aster", "New description", "Units in",
                "Units off old code", "Old Aster", "Old description"]
LAYOUT_HEADERS = ["Route", "Cart", "# carts", "Shelves", "Height (in)", "Shelf heights",
                  "Shelf widths (in)"]
ASTER_HEADERS = ["StoreNumber", "AsterCode", "Quantity", "Description", "KitType"]

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xl(v):
    """Excel formula-injection guard for string cells (= + - @ prefixes)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def week_number(d) -> int:
    return d.isocalendar()[1] if d else 0


def _date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        s = v.strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
            except ValueError:
                continue
    return None


def _store_id(v):
    """HD store number from a cell; None for non-HD rows (e.g. 'SHIP-000845 Byron Ace')."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() and v > 0 else None
    s = str(v).strip()
    return int(s) if s.isdigit() else None


# ── DB loaders ───────────────────────────────────────────────────────────────

def load_run(cur, run_id: str) -> dict | None:
    """Everything run-specific the builders need (RealDictCursor)."""
    cur.execute("SELECT id, batch_id, shipping_week, engine_version FROM recommendation_runs "
                "WHERE id = %s", (run_id,))
    run = cur.fetchone()
    if not run:
        return None
    cur.execute("""SELECT id, store_id, status, cart_count, total_units
                   FROM orders WHERE run_id = %s""", (run_id,))
    orders = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT 1 AS ok FROM information_schema.columns
                   WHERE table_name = 'order_carts' AND column_name = 'handling_tag'""")
    tag_col = ", oc.handling_tag" if cur.fetchone() else ""
    cur.execute("""SELECT count(*) AS n FROM information_schema.columns WHERE table_name = 'order_carts'
                   AND column_name IN ('master_code', 'flex_positions')""")
    _mrow = cur.fetchone()
    if (_mrow["n"] if isinstance(_mrow, dict) else _mrow[0]) == 2:
        tag_col += ", oc.master_code, oc.flex_positions"
    cur.execute(f"""SELECT oc.id, oc.order_id, oc.store_id, oc.cart_number, oc.config_code, oc.cart_key
                          {tag_col}
                   FROM order_carts oc JOIN orders o ON o.id = oc.order_id
                   WHERE o.run_id = %s ORDER BY oc.store_id, oc.cart_number""", (run_id,))
    carts = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT cs.cart_id, cs.shelf_position, cs.sku_id, cs.profile_id, cs.kit_type,
                          cs.tray_count, cs.unit_count, cs.availability_line_id
                   FROM cart_shelves cs JOIN order_carts oc ON oc.id = cs.cart_id
                   JOIN orders o ON o.id = oc.order_id
                   WHERE o.run_id = %s""", (run_id,))
    shelves = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT id, load_name, ship_date, delivery_date, source
                   FROM loads WHERE run_id = %s""", (run_id,))
    loads = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT la.load_id, la.order_cart_id, la.store_id, la.stop_sequence
                   FROM load_assignments la JOIN loads l ON l.id = la.load_id
                   WHERE l.run_id = %s""", (run_id,))
    assignments = [dict(r) for r in cur.fetchall()]
    for x in carts + shelves + loads + assignments + orders:
        for k in ("id", "order_id", "cart_id", "availability_line_id", "load_id", "order_cart_id"):
            if x.get(k) is not None:
                x[k] = str(x[k])
    return {"run": {"id": str(run["id"]), "batch_id": str(run["batch_id"]) if run["batch_id"] else None,
                    "shipping_week": run["shipping_week"], "engine_version": run.get("engine_version")},
            "orders": orders, "carts": carts, "shelves": shelves, "loads": loads,
            "assignments": assignments}


def _norm_name(n: str) -> str:
    """Aster product name -> variety key shared by its 1M and 3M (jute/ceramic) codes.
    '17cm Monstera Deliciosa 17cm Everyday' / '... 17cm Jute Kit' -> '17cmmonsteradeliciosa17cm'."""
    n = (n or "").strip()
    n = re.sub(r"\s+(everyday|jute kit|ceramic kit|canopy|grower pot|grower)$", "", n, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", n.lower())


def load_reference(cur, batch_id: str | None, sku_ids=None) -> dict:
    """Store/SKU/Aster/packing reference data (RealDictCursor)."""
    # store-master handling columns are read when present (store_master_2026_11_22 migration)
    cur.execute("""SELECT column_name::text AS c FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'stores' AND column_name IN
                         ('street_address', 'postal_code', 'cross_dock', 'sku_rule',
                          'sku_restriction', 'master_status', 'merchandiser')""")
    extra = "".join(", " + r["c"] for r in cur.fetchall())
    cur.execute(f"""SELECT store_id, store_name, city, state, division, merchant, volume_code,
                          dynamic_velocity_tier, store_status{extra} FROM stores""")
    stores = {r["store_id"]: dict(r) for r in cur.fetchall()}
    cur.execute("""SELECT k.sku_id, k.description, k.family, k.size_category, k.program,
                          fm.ipr_family, k.upc, pb.retail_price
                   FROM skus k LEFT JOIN ipr_family_map fm ON fm.sku_id = k.sku_id
                   LEFT JOIN pricebook_hd pb ON pb.sku_id = k.sku_id""")
    skus = {r["sku_id"]: dict(r) for r in cur.fetchall()}
    avail = {}
    if batch_id:
        cur.execute("""SELECT id, sku_id, plant_id, code_level, hard_good_type, units_available,
                              shipping_origin
                       FROM availability_lines WHERE batch_id = %s""", (batch_id,))
        avail = {str(r["id"]): dict(r) for r in cur.fetchall()}
    cur.execute("""SELECT aster_code, aster_name, code_level, hard_good_type, hd_sku_id
                   FROM aster_product_mapping WHERE active = true""")
    apm = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT aster_code, product_name, parsed_genus, parsed_variety FROM variety_parsed_reference")
    _vp = cur.fetchall()
    aster_names = {str(r["aster_code"]): r["product_name"] for r in _vp}
    aster_parsed = {str(r["aster_code"]): (r["parsed_genus"], r["parsed_variety"]) for r in _vp}
    for m in apm:
        aster_names.setdefault(str(m["aster_code"]), m["aster_name"])
    cur.execute("""SELECT id, tray_width_mm, tray_depth_mm, units_per_tray, clear_height_in
                   FROM packing_profiles""")
    profiles = {r["id"]: dict(r) for r in cur.fetchall()}
    ref = build_reference(stores, skus, avail, apm, aster_names, profiles)
    ref["aster_parsed"] = aster_parsed
    return ref


def build_reference(stores, skus, avail, apm, aster_names, profiles) -> dict:
    # 1M variety -> 3M jute/ceramic code, keyed on the shared variety name. A variety with
    # two different 3M codes for the same kit is ambiguous and deliberately left unmapped.
    kit3m, amb = defaultdict(dict), set()
    for m in apm:
        if m.get("code_level") == "3M" and m.get("hard_good_type") in KIT_TYPES:
            key = (_norm_name(m["aster_name"]), m["hard_good_type"])
            prev = kit3m[key[0]].get(key[1])
            if prev and prev != str(m["aster_code"]):
                amb.add(key)
            kit3m[key[0]][key[1]] = str(m["aster_code"])
    for k, kt in amb:
        kit3m[k].pop(kt, None)
    return {"stores": stores, "skus": skus, "avail": avail, "aster_names": aster_names,
            "kit3m": dict(kit3m), "profiles": profiles}


# ── config naming ────────────────────────────────────────────────────────────
# Josh 2026-11-19: every distinct cart config is named "CC 1", "CC 2", ... ascending,
# ordered by number of carts using it (desc), ties by first (lowest) store number.
# Permanent carts keep their existing names. The mapping is persisted per run in
# run_config_names (run_id, signature -> name) so Pack Plan, Stores and Carts, Aster
# and the UI all show the same name, and names stay stable after later edits.

def cart_signature(cart: dict, shelves: list) -> str:
    """Exact-content key of a cart: (layer, sku, kit) -> trays, aggregated over the
    availability-line segments. Permanent carts are keyed by their own name."""
    code = (cart.get("config_code") or "").strip()
    if code.startswith("PC ") and len(code) > 3:
        return "PC:" + code[3:].strip()
    agg = defaultdict(int)
    for s in shelves:
        agg[(int(s["shelf_position"]), int(s["sku_id"]), s.get("kit_type") or "")] += int(s.get("tray_count") or 0)
    sig = "|".join(f"{p}:{k}:{kt}:{t}" for (p, k, kt), t in sorted(agg.items()))
    # A cart built to a store handling restriction (e.g. NO HEDERA) is its own config even
    # when its SKU/tray content equals an unrestricted cart: ops must not build both from
    # one sheet, because the Aster varieties on it differ.
    tag = (cart.get("handling_tag") or "").strip()
    return f"{sig}|TAG:{tag}" if tag else sig


def name_key(cart: dict, shelves: list) -> str:
    """What a kit NAME is keyed on: the cart's master (base build) when the run carries one —
    carts sharing a master share a name and differ only in their flex shelves — else the exact
    content signature (runs written before master + flex export exactly as before)."""
    code = (cart.get("master_code") or "").strip()
    if code and not (cart.get("config_code") or "").strip().startswith("PC "):
        return "MASTER:" + code
    return cart_signature(cart, shelves)


def has_masters(model: dict) -> bool:
    return any((c.get("master_code") or "").strip() for c in model.get("carts", []))


def _cc_number(name: str):
    m = re.fullmatch(r"CC (\d+)", name or "")
    return int(m.group(1)) if m else None


def assign_config_names(model: dict, existing: dict | None = None):
    """Returns ({cart_id: name}, {signature: name}, new_rows).

    existing: persisted {signature: name} for the run — kept as-is; signatures not yet
    named continue the sequence after the highest existing CC number.
    new_rows: [(signature, name, cart_count, first_store)] to persist."""
    existing = dict(existing or {})
    by_cart = defaultdict(list)
    for s in model["shelves"]:
        by_cart[s["cart_id"]].append(s)
    sig_of, count, first = {}, Counter(), {}
    for c in model["carts"]:
        sig = name_key(c, by_cart.get(c["id"], []))
        sig_of[c["id"]] = sig
        count[sig] += 1
        first[sig] = min(first.get(sig, 10 ** 9), int(c["store_id"]))
    names = dict(existing)
    n = max([_cc_number(v) or 0 for v in existing.values()] + [0])
    new_rows = []
    for sig in sorted((s for s in count if s not in names), key=lambda s: (-count[s], first[s], s)):
        if sig.startswith("PC:"):
            names[sig] = sig[3:]
        else:
            n += 1
            names[sig] = f"CC {n}"
        new_rows.append((sig, names[sig], count[sig], first[sig]))
    return {cid: names[sig] for cid, sig in sig_of.items()}, names, new_rows


def ensure_config_names(cur, model: dict) -> dict:
    """Load/persist the run's names and attach model['config_names'] = {cart_id: name}.
    If the run_config_names migration is not applied yet, names are computed (same
    deterministic rule) but not persisted; model['config_names_persisted'] says which."""
    cur.execute("SELECT to_regclass('public.run_config_names') AS t")
    row = cur.fetchone()
    has_table = bool(row and (row["t"] if isinstance(row, dict) else row[0]))
    existing = {}
    if has_table:
        cur.execute("SELECT signature, name FROM run_config_names WHERE run_id = %s",
                    (model["run"]["id"],))
        existing = {r["signature"]: r["name"] for r in cur.fetchall()}
    by_cart, names, new_rows = assign_config_names(model, existing)
    if has_table and new_rows:
        cur.executemany("""INSERT INTO run_config_names (run_id, signature, name, cart_count, first_store)
                           VALUES (%s,%s,%s,%s,%s) ON CONFLICT (run_id, signature) DO NOTHING""",
                        [(model["run"]["id"], *r) for r in new_rows])
        # a concurrent export may have won the insert race — re-read the persisted truth
        cur.execute("SELECT signature, name FROM run_config_names WHERE run_id = %s",
                    (model["run"]["id"],))
        by_cart, names, _ = assign_config_names(model, {r["signature"]: r["name"] for r in cur.fetchall()})
    model["config_names"] = by_cart
    model["config_names_persisted"] = has_table
    return by_cart


def config_names(model: dict) -> dict:
    """{cart_id: name} — persisted names when ensure_config_names ran, else computed."""
    if "config_names" not in model:
        model["config_names"] = assign_config_names(model)[0]
    return model["config_names"]


def _config_sort_key(label: str):
    n = _cc_number(label)
    return (0, n, "") if n is not None else (1, 0, (label or "").lower())


# ── 1. Stores and Carts (to routing) ─────────────────────────────────────────

def store_city_label(store_id: int, s: dict) -> str:
    """'0903-S PLAINFIELD' — matches 88-91% of the Week 46/47 City column; the rest are
    legacy 'HOME DEPOT OF X, ST' strings that no table carries."""
    name = (s.get("store_name") or s.get("city") or "").strip().upper()
    return f"{store_id:04d}-{name}"


def store_vol(s: dict) -> str:
    """Vol = stores.volume_code (the store's DTS volume / Velo code). No v1 Stores and
    Carts generator ever existed, so there is no other source to stay consistent with."""
    return (s.get("volume_code") or "").strip()


def store_address(s: dict) -> str:
    """Street address + ZIP from the store master (blank until the master is loaded)."""
    a = (s.get("street_address") or "").strip()
    z = (s.get("postal_code") or "").strip()
    return f"{a}, {z}" if a and z else a


def store_notes(s: dict) -> str:
    """Routing notes from the store master: cross dock + handling restriction."""
    return store_rules.handling_notes(s)


def build_stores_and_carts(model: dict, ref: dict):
    """Returns (openpyxl Workbook, report dict)."""
    import openpyxl
    from openpyxl.utils import get_column_letter

    carts_by_store = defaultdict(list)
    for c in model["carts"]:
        carts_by_store[c["store_id"]].append(c)
    desired = {o["store_id"]: o.get("cart_count") for o in model["orders"]}

    labels = config_names(model)
    cols = sorted(set(labels.values()), key=_config_sort_key)
    col_idx = {lab: i for i, lab in enumerate(cols)}

    stores, missing_meta, noted = ref["stores"], [], []
    div_order = {"North": 0, "South": 1, "West": 2}

    def sort_key(sid):
        s = stores.get(sid) or {}
        return (div_order.get(s.get("division"), 9), s.get("division") or "", sid)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet"
    ws.append(SC_META_HEADERS + cols)
    for sid in sorted((s for s in carts_by_store if carts_by_store[s]), key=sort_key):
        s = stores.get(sid)
        if not s:
            missing_meta.append(sid)
            s = {}
        counts = [None] * len(cols)
        for c in carts_by_store[sid]:
            i = col_idx[labels[c["id"]]]
            counts[i] = (counts[i] or 0) + 1
        total = sum(x for x in counts if x)
        want = desired.get(sid)
        want = int(want) if want is not None else total
        note = store_notes(s)
        if note:
            noted.append(sid)
        ws.append([sid, xl(store_address(s)), xl(s.get("division") or ""), xl(store_city_label(sid, s)),
                   xl((s.get("state") or "").strip()), xl(store_vol(s)),
                   xl(s.get("merchant") or ""), xl(note), want, total, want - total] + counts)
    for i, w in enumerate(SC_META_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for j, lab in enumerate(cols, len(SC_META_HEADERS) + 1):
        ws.column_dimensions[get_column_letter(j)].width = max(9.0, min(20.0, len(lab) + 4))
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(SC_META_HEADERS) + len(cols))}{ws.max_row}"
    report = {
        "stores": ws.max_row - 1,
        "carts": len(model["carts"]),
        "distinct_configs": len(cols),
        "config_names_persisted": bool(model.get("config_names_persisted")),
        "permanent_cart_configs": [c for c in cols if _cc_number(c) is None],
        "stores_missing_metadata": missing_meta,
        "stores_with_handling_notes": len(noted),
        "cross_dock_stores": dict(Counter((stores.get(sid) or {}).get("cross_dock")
                                          for sid in carts_by_store
                                          if carts_by_store[sid] and (stores.get(sid) or {}).get("cross_dock"))),
        "blocked_stores_in_run": sorted(sid for sid in carts_by_store if carts_by_store[sid]
                                        and ((stores.get(sid) or {}).get("store_status") or "active") in BLOCK_STATUSES),
        "stores_missing_vol": [ws.cell(r, 1).value for r in range(2, ws.max_row + 1)
                               if not ws.cell(r, 6).value],
        "cart_count_mismatch": [ws.cell(r, 1).value for r in range(2, ws.max_row + 1)
                                if ws.cell(r, 11).value],
    }
    return wb, report


# ── 2. Routing Workbook (from routing) ───────────────────────────────────────

def _hdr_index(row) -> dict:
    return {str(v).strip().lower(): i for i, v in enumerate(row or ()) if v is not None and str(v).strip()}


def _pick(idx: dict, *names):
    for n in names:
        if n in idx:
            return idx[n]
    return None


def parse_routing_workbook(data: bytes) -> dict:
    """Parse the routing team's 'Week NN-YYYY Workbook.xlsx'.

    'Compiled' (Store | Load | Ship Date | Delivery Date) is the authoritative store->load
    list; per-load sheets (sheet title == load name, e.g. '47-N2') add Drop, Load Sequence,
    Carts, Del Time, PU Number and Split Stop. Region sheets (North/South/West), Cross Dock,
    _meta and _cls_* are ignored. A store on two loads is a split stop."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        names = wb.sheetnames
        comp_name = next((n for n in names if n.strip().lower() == "compiled"), None)
        entries, warnings = {}, []
        non_store, bad_routes, bad_rows = set(), set(), 0

        def add(store, load, **kw):
            e = entries.setdefault((store, load), {"store_id": store, "route": load, "drop": None,
                                                   "load_sequence": None, "carts": None, "ship": None,
                                                   "deliv": None, "del_time": None, "pu_number": None,
                                                   "split_note": None, "source": set()})
            for k, v in kw.items():
                if k == "source":
                    e["source"].add(v)
                elif v is not None and e.get(k) is None:
                    e[k] = v
            return e

        def norm_route(v):
            r = str(v).strip() if v is not None else ""
            if not r:
                return None
            if not ROUTE_RE.match(r):
                bad_routes.add(r[:60])
                return None
            return r

        compiled_loads = set()
        if comp_name:
            rows = wb[comp_name].iter_rows(values_only=True)
            idx = _hdr_index(next(rows, ()))
            c_s, c_l = _pick(idx, "store", "store #", "store id"), _pick(idx, "load", "route")
            c_sh = _pick(idx, "ship date", "shipdate")
            c_d = _pick(idx, "delivery date", "del date", "del. date")
            if c_s is None or c_l is None:
                warnings.append(f"'Compiled' sheet lacks Store/Load headers: {sorted(idx)}")
            else:
                for r in rows:
                    if not r or all(v is None for v in r):
                        continue
                    sv = r[c_s] if c_s < len(r) else None
                    st = _store_id(sv)
                    if st is None:
                        if sv not in (None, ""):
                            non_store.add(str(sv)[:60])
                        continue
                    lo = norm_route(r[c_l] if c_l < len(r) else None)
                    if not lo:
                        bad_rows += 1
                        continue
                    compiled_loads.add(lo)
                    add(st, lo, ship=_date(r[c_sh]) if c_sh is not None and c_sh < len(r) else None,
                        deliv=_date(r[c_d]) if c_d is not None and c_d < len(r) else None,
                        source="compiled")
        else:
            warnings.append("No 'Compiled' sheet — using per-load sheets only")

        load_sheets = []
        for n in names:
            if n == comp_name or n.startswith("_"):
                continue
            ws = wb[n]
            rows = ws.iter_rows(values_only=True)
            idx = _hdr_index(next(rows, ()))
            if not {"store", "load", "drop"} <= set(idx):
                continue
            title = n.strip()
            if compiled_loads and title not in compiled_loads:
                continue          # region roll-ups (North/South/West) repeat the load sheets
            body = [r for r in rows if r and any(v is not None for v in r)]
            sheet_loads = {str(r[idx["load"]]).strip() for r in body
                           if idx["load"] < len(r) and r[idx["load"]] is not None}
            if not compiled_loads and sheet_loads != {title}:
                continue
            load_sheets.append(title)
            g = lambda r, *ks: (r[_pick(idx, *ks)] if _pick(idx, *ks) is not None
                                and _pick(idx, *ks) < len(r) else None)
            for r in body:
                st = _store_id(g(r, "store"))
                if st is None:
                    if g(r, "store") not in (None, ""):
                        non_store.add(str(g(r, "store"))[:60])
                    continue
                lo = norm_route(g(r, "load") or title)
                if not lo:
                    bad_rows += 1
                    continue
                if comp_name and (st, lo) not in entries:
                    warnings.append(f"Store {st} is on sheet {title} but not in Compiled for {lo}")

                def _int(v):
                    try:
                        return int(float(v)) if v not in (None, "") else None
                    except (TypeError, ValueError):
                        return None
                split = g(r, "split stop")
                add(st, lo, drop=_int(g(r, "drop", "stop")), load_sequence=_int(g(r, "load sequence")),
                    carts=_int(g(r, "carts")), ship=_date(g(r, "ship date")),
                    deliv=_date(g(r, "del date", "delivery date")),
                    del_time=str(g(r, "del time"))[:40] if g(r, "del time") else None,
                    pu_number=str(g(r, "pu number"))[:40] if g(r, "pu number") else None,
                    split_note=str(split)[:80] if split else None, source="load_sheet")
        missing_sheets = sorted(compiled_loads - set(load_sheets))
        if missing_sheets:
            warnings.append(f"No per-load sheet (drops unknown) for: {missing_sheets}")
        out = []
        for e in entries.values():
            e["source"] = sorted(e["source"])
            out.append(e)
        out.sort(key=lambda e: (e["route"], e["drop"] if e["drop"] is not None else 10 ** 6))
        return {"entries": out, "load_sheets": sorted(load_sheets),
                "loads": sorted({e["route"] for e in out}), "non_store_rows": sorted(non_store),
                "bad_route_names": sorted(bad_routes), "rows_bad": bad_rows, "warnings": warnings}
    finally:
        wb.close()


def plan_routes(entries: list, carts_by_store: dict, allow_split: bool = True) -> dict:
    """Assign every run cart to exactly one routed load.

    carts_by_store: {store_id: [cart_id, ...] in cart_number order}.
    Split stops (store on >1 load): carts are divided using each load sheet's Carts count
    when those counts add up to the store's carts; otherwise as evenly as possible (and
    flagged). allow_split=False keeps the legacy CSV rule: first load listed wins."""
    run_stores = set(carts_by_store)
    by_store = defaultdict(list)
    for e in entries:
        by_store[e["store_id"]].append(e)
    unmatched = sorted(set(by_store) - run_stores)
    unrouted = sorted(run_stores - set(by_store))
    loads = defaultdict(lambda: {"stops": [], "ship": None, "deliv": None})
    splits, split_uneven, multi_first = [], [], []
    for sid, es in by_store.items():
        if sid not in run_stores:
            continue
        carts = list(carts_by_store[sid])
        if len(es) > 1 and not allow_split:
            multi_first.append(sid)
            es = es[:1]
        if len(es) == 1:
            parts = [carts]
        else:
            want = [e.get("carts") for e in es]
            if all(isinstance(w, int) and w >= 0 for w in want) and sum(want) == len(carts):
                sizes = want
            else:
                base, rem = divmod(len(carts), len(es))
                sizes = [base + (1 if i < rem else 0) for i in range(len(es))]
                split_uneven.append(sid)
            parts, pos = [], 0
            for n in sizes:
                parts.append(carts[pos:pos + n])
                pos += n
            splits.append({"store_id": sid, "loads": {e["route"]: len(p) for e, p in zip(es, parts)}})
        for e, part in zip(es, parts):
            L = loads[e["route"]]
            L["stops"].append({"store_id": sid, "drop": e.get("drop"), "cart_ids": part,
                               "ship": e.get("ship"), "deliv": e.get("deliv")})
            L["ship"] = L["ship"] or e.get("ship")
            L["deliv"] = L["deliv"] or e.get("deliv")
    for L in loads.values():
        L["stops"].sort(key=lambda s: (s["drop"] if s["drop"] is not None else 10 ** 6))
        for i, s in enumerate(L["stops"], 1):
            if s["drop"] is None:
                s["drop"] = i
    return {"loads": dict(loads), "stores_in_file_not_in_run": unmatched,
            "stores_in_run_not_routed": unrouted, "split_stops": splits,
            "split_stops_uneven": split_uneven, "stores_on_multiple_routes": multi_first,
            "carts_assigned": sum(len(s["cart_ids"]) for L in loads.values() for s in L["stops"])}


# ── 3. Ops Pack Plan ─────────────────────────────────────────────────────────

def item_description(sku: dict | None) -> str:
    """Pack-plan ItemDescription (the product group ops pulls by), per Week 47:
    12cm/17cm standard -> '12cm'/'17cm'; collectors -> '12cm Collector'; 12cm HB -> '12cm HB';
    Climb-itt -> '12cm Climb-itt'/'17cm Climb-itt'; everything else -> SKU description."""
    if not sku:
        return ""
    fam = (sku.get("ipr_family") or "").strip()
    m = re.match(r"(\d+cm) ", fam)
    size = m.group(1) if m else None
    if fam in ("12cm Standard", "17cm Standard"):
        return size
    if size and "Collector" in fam:
        return f"{size} Collector"
    if fam == "12cm Canopy/Hanging Basket":
        return "12cm HB"
    if fam.lower() == "climb-itt":
        prog = (sku.get("program") or "").strip()
        return prog if re.match(r"\d+cm Climb-itt$", prog, flags=re.I) else "Climb-itt"
    desc = (sku.get("description") or "").strip()
    return re.sub(r"\s+-\s+\S+$", "", desc).upper() if desc else ""


def _largest_remainder(total: int, weights: list) -> list:
    if total <= 0 or not weights:
        return [0] * len(weights)
    s = sum(weights)
    if s <= 0:
        base, rem = divmod(total, len(weights))
        return [base + (1 if i < rem else 0) for i in range(len(weights))]
    raw = [total * w / s for w in weights]
    out = [int(r) for r in raw]
    for i in sorted(range(len(raw)), key=lambda i: (-(raw[i] - out[i]), i))[:total - sum(out)]:
        out[i] += 1
    return out


def _routing_index(model: dict):
    load_by_id = {l["id"]: l for l in model["loads"]}
    route_of = {}
    for a in model["assignments"]:
        L = load_by_id.get(a["load_id"])
        if L:
            route_of[a["order_cart_id"]] = (L["load_name"], L.get("ship_date"))
    return route_of


def pack_plan_rows(model: dict, ref: dict):
    """Rows for the 'Sheet' tab + per-(route, config) layouts. One row per tray-variety:
    KitQuantity carts each carry Trays/Eaches of AsterCode on Layer; Total Used = D*P.
    Aster splits that don't divide evenly across a config's carts become 'Mix' rows."""
    skus, avail, profiles = ref["skus"], ref["avail"], ref["profiles"]
    route_of = _routing_index(model)
    names = config_names(model)
    shelves_by_cart = defaultdict(list)
    for s in model["shelves"]:
        shelves_by_cart[s["cart_id"]].append(s)

    groups = defaultdict(list)            # (ship, route, label, variant) -> carts
    tag_of = {}                           # config label -> handling tag (NO HEDERA ...)
    masters = has_masters(model)
    for c in model["carts"]:
        route, ship = route_of.get(c["id"], (UNROUTED, None))
        # master runs: a kit name is a BASE build; each flex variant (exact content) on a route is
        # its own Variation so every row group still describes identical carts
        variant = cart_signature(c, shelves_by_cart.get(c["id"], [])) if masters else ""
        groups[(ship, route, names[c["id"]], variant)].append(c)
        if (c.get("handling_tag") or "").strip():
            tag_of[names[c["id"]]] = c["handling_tag"].strip()

    def gkey(k):
        ship, route, lab, variant = k
        return (ship or date.max, route == UNROUTED, route, _config_sort_key(lab), -len(groups[k]), variant)

    variation_seq = defaultdict(int)
    rows, layouts, notes = [], [], Counter()
    for key in sorted(groups, key=gkey):
        ship, route, lab, _variant = key
        carts = groups[key]
        K = len(carts)
        variation_seq[lab] += 1
        variation = variation_seq[lab]
        flex_pos = {int(p) for p in (carts[0].get("flex_positions") or [])}
        # per-cart layer content (identical across carts sharing one config name, since
        # names are assigned per exact content signature)
        per_layer = defaultdict(lambda: {"trays": 0, "units": 0, "profile": None, "aster": defaultdict(int)})
        for c in carts:
            for s in shelves_by_cart.get(c["id"], []):
                L = per_layer[(s["shelf_position"], s["sku_id"])]
                L["trays"] += int(s["tray_count"] or 0)
                L["units"] += int(s["unit_count"] or 0)
                L["profile"] = L["profile"] or s.get("profile_id")
                line = avail.get(str(s.get("availability_line_id"))) if s.get("availability_line_id") else None
                code = str(line["plant_id"]) if line and line.get("plant_id") else None
                L["aster"][code] += int(s["unit_count"] or 0)
        item_tot = defaultdict(lambda: [0, 0])   # (layer, item_desc) -> per-cart trays, units
        for (pos, sku_id), L in per_layer.items():
            idesc = item_description(skus.get(sku_id))
            item_tot[(pos, idesc)][0] += L["trays"] / K
            item_tot[(pos, idesc)][1] += L["units"] / K
        mix_letter = 0
        for (pos, sku_id) in sorted(per_layer, key=lambda k: (k[0], item_description(skus.get(k[1])), k[1])):
            L = per_layer[(pos, sku_id)]
            sk = skus.get(sku_id) or {}
            idesc = item_description(sk)
            it, ie = item_tot[(pos, idesc)]
            T = L["trays"]                       # trays across all K carts
            upt = (L["units"] // L["trays"]) if L["trays"] else 0
            codes = sorted(L["aster"], key=lambda c: (-L["aster"][c], c or "~"))
            alloc = _largest_remainder(T, [L["aster"][c] for c in codes]) if T else [0] * len(codes)
            base_rows, mix_rows = [], []
            for code, t in zip(codes, alloc):
                b, r = divmod(t, K)
                if b:
                    base_rows.append((code, K, b, None))
                if r:
                    mix_rows.append((code, r, 1))
            if mix_rows:
                mix_letter += 1
                letter = chr(64 + min(mix_letter, 26))
                for code, r, t1 in mix_rows:
                    base_rows.append((code, r, t1, f"Mix {letter}: {r} of {K} carts"))
                notes["mix_rows"] += len(mix_rows)
            if not T:                            # unit-only shelf (manual edits): keep the line
                base_rows = [(codes[0] if codes else None, K, 0, None)]
            for code, kq, trays, note in base_rows:
                if tag_of.get(lab):          # restricted build: say so on every row of the config
                    note = f"{tag_of[lab]} — {note}" if note else tag_of[lab]
                    notes["handling_tag_rows"] += 1
                if int(pos) in flex_pos:     # store-specific shelf on a base build
                    note = f"Flex shelf — {note}" if note else "Flex shelf"
                    notes["flex_rows"] += 1
                eaches = trays * upt if trays else (L["units"] // K if K else 0)
                if code is None:
                    notes["rows_without_aster_code"] += 1
                rows.append({
                    "ShipDate": ship, "Route": route, "KitDescription": lab, "KitQuantity": kq,
                    "Variation": variation, "Layer": pos, "Upgrade": None, "Notes": note,
                    "ItemDescription": idesc, "ItemTrays": int(round(it)), "ItemEaches": int(round(ie)),
                    "ItemCustomerDescription": sk.get("description") or "", "SKU": str(sku_id),
                    "AsterCode": code, "Trays": trays, "Eaches": eaches, "Total Used": kq * eaches,
                    "Flex": int(pos) in flex_pos,
                })
        # Cart Layouts: shelf heights/widths from packing profiles (per cart)
        layer_prof = defaultdict(lambda: defaultdict(int))
        for (pos, sku_id), L in per_layer.items():
            layer_prof[pos][L["profile"]] += L["trays"] / K
        heights, widths = [], []
        for pos in sorted(layer_prof):
            h, w = 0.0, 0.0
            for pid, trays in layer_prof[pos].items():
                p = profiles.get(pid) or {}
                h = max(h, float(p.get("clear_height_in") or 0))
                depth = float(p.get("tray_depth_mm") or 0) or CART_SHELF_DEPTH_MM
                rows_deep = max(1, int(CART_SHELF_DEPTH_MM // depth))
                w += math.ceil(trays / rows_deep) * float(p.get("tray_width_mm") or 0) / 25.4
            heights.append(h)
            widths.append(round(w, 2))
        layouts.append({"Route": route, "Cart": lab, "Variation": variation, "# carts": K, "Shelves": len(heights),
                        "Height (in)": int(sum(heights)) if all(x == int(x) for x in heights) else round(sum(heights), 1),
                        "Shelf heights": " / ".join(f"{x:g}" for x in heights),
                        "Shelf widths (in)": " / ".join(f"{x:g}" for x in widths)})
    return rows, layouts, notes


MAX_BASE_CONFIGS = 50


def base_build_summary(model: dict, rows: list) -> dict:
    """Base builds of a master run: carts per base name, flex variants (distinct exact
    contents) per base, routes."""
    names = config_names(model)
    by_cart = defaultdict(list)
    for s in model["shelves"]:
        by_cart[s["cart_id"]].append(s)
    carts, variants = Counter(), defaultdict(set)
    for c in model["carts"]:
        carts[names[c["id"]]] += 1
        variants[names[c["id"]]].add(cart_signature(c, by_cart.get(c["id"], [])))
    routes = defaultdict(set)
    for r in rows:
        routes[r["KitDescription"]].add(r["Route"])
    bases = [{"name": n, "carts": carts[n], "flex_variants": len(variants[n]),
              "routes": len(routes.get(n, ()))} for n in sorted(carts, key=_config_sort_key)]
    return {"bases": bases, "carts": sum(carts.values()),
            "flex_variants_max": max((b["flex_variants"] for b in bases), default=0),
            "bases_over_4": sum(1 for b in bases if b["flex_variants"] > 4)}


def _pivot_key(k):
    if k is None:
        return (2, "")
    if isinstance(k, (date, datetime)):
        return (0, k.isoformat())
    return (1, str(k))


def build_pack_plan(model: dict, ref: dict):
    """Returns (Workbook, report). Tabs in Week 47 order; derivable tabs are generated,
    manual tabs (Changes, Availability Subs) are headed templates."""
    import openpyxl
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    rows, layouts, notes = pack_plan_rows(model, ref)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    def tree(level_fns, filt=lambda r: True):
        """Nested pivot (outline form): returns [(label, value, depth)] + grand total."""
        out = []

        def rec(rs, depth):
            keys = defaultdict(list)
            for r in rs:
                keys[level_fns[depth](r)].append(r)
            for k in sorted(keys, key=_pivot_key):
                out.append((k if k is not None else "(blank)", sum(x["Total Used"] for x in keys[k]), depth))
                if depth + 1 < len(level_fns):
                    rec(keys[k], depth + 1)
        sel = [r for r in rows if filt(r)]
        rec(sel, 0)
        return out, sum(r["Total Used"] for r in sel)

    def write_pivot(title, levels, filt=lambda r: True, width_a=46.57):
        ws = wb.create_sheet(title)
        ws.cell(3, 1, "Row Labels")
        ws.cell(3, 2, "Sum of Total Used")
        items, grand = tree(levels, filt)
        r = 4
        for label, val, depth in items:
            c = ws.cell(r, 1, xl(label) if not isinstance(label, (date, datetime)) else label)
            if isinstance(label, (date, datetime)):
                c.number_format = "mm-dd-yy"
            ws.cell(r, 2, val).number_format = "0"
            r += 1
        ws.cell(r, 1, "Grand Total")
        ws.cell(r, 2, grand).number_format = "0"
        ws.column_dimensions["A"].width = width_a
        ws.column_dimensions["B"].width = 16.43
        return grand

    is_bloom = lambda r: "BLOOMAKER" in (r["ItemDescription"] or "").upper()
    is_soil = lambda r: "SOIL" in (r["ItemDescription"] or "").upper() or "HOUSEPLANT BLEND" in (r["ItemDescription"] or "").upper()
    ship_or_blank = lambda r: r["ShipDate"]
    write_pivot("Bloomaker by Day", [ship_or_blank, lambda r: r["ItemDescription"]], is_bloom, 25.57)
    write_pivot("Soil by Day", [ship_or_blank, lambda r: r["ItemDescription"]], is_soil, 50.57)
    grand = write_pivot("Totals Used", [lambda r: r["ItemDescription"]])
    write_pivot("Routing Report", [ship_or_blank, lambda r: r["Route"], lambda r: r["ItemDescription"]],
                width_a=52.57)
    write_pivot("Deductions", [lambda r: r["AsterCode"]], width_a=12.57)

    ws = wb.create_sheet("Sheet")
    ws.append(PP_SHEET_HEADERS)
    for i, r in enumerate(rows, 2):
        ws.append([r["ShipDate"], xl(r["Route"]), xl(r["KitDescription"]), r["KitQuantity"],
                   r["Variation"], r["Layer"], r["Upgrade"], xl(r["Notes"]), xl(r["ItemDescription"]),
                   r["ItemTrays"], r["ItemEaches"], xl(r["ItemCustomerDescription"]), r["SKU"],
                   xl(r["AsterCode"]), r["Trays"], r["Eaches"], f"=D{i}*P{i}"])
        ws.cell(i, 1).number_format = "mm-dd-yy"
        for col in (4, 6, 10, 11, 15, 16, 17):
            ws.cell(i, col).number_format = "0"
    for i, w in enumerate(PP_SHEET_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:Q{max(ws.max_row, 1)}"

    for title, hdr in (("Changes", CHANGES_HEADERS), ("Availability Subs", SUBS_HEADERS)):
        w = wb.create_sheet(title)
        w.append(hdr)
        for c in w[1]:
            c.font = Font(bold=True)
        w.freeze_panes = "A2"

    masters = has_masters(model)
    base = base_build_summary(model, rows) if masters else None
    lay_hdr = LAYOUT_HEADERS[:2] + ["Variation"] + LAYOUT_HEADERS[2:] if masters else LAYOUT_HEADERS
    w = wb.create_sheet("Cart Layouts")
    w.append(lay_hdr)
    for c in w[1]:
        c.font = Font(bold=True)
    for L in layouts:
        w.append([xl(L[h]) for h in lay_hdr])
    w.freeze_panes = "A2"
    if base:
        # base builds: what the floor builds in bulk; flex variants add their two shelves
        w.append([])
        w.append(["Base build", "# carts", "Flex variants", "Routes"])
        for c in w[w.max_row]:
            c.font = Font(bold=True)
        for b in base["bases"]:
            w.append([xl(b["name"]), b["carts"], b["flex_variants"], b["routes"]])

    # Weekly check list (pack team guide 2026-11-25), worst first, on its own tab
    try:
        from services import pack_checks
    except ImportError:                      # running from backend/services directly (tests)
        import pack_checks
    _names = config_names(model)
    findings = pack_checks.run_checks(model, ref, rows, kit_name_of=_names)
    if base:
        n_b = len(base["bases"])
        findings.insert(0, {"severity": "High" if n_b > MAX_BASE_CONFIGS else "OK", "check": "I",
                            "title": f"Base cart configs <= {MAX_BASE_CONFIGS}",
                            "route": None, "kit": None, "detail": f"{n_b} base builds for {base['carts']} carts",
                            "carts": base["carts"], "file": "Pack Plan",
                            "fix": "" if n_b <= MAX_BASE_CONFIGS else "Re-run with the master + flex cart menu"})
        findings.insert(1, {"severity": "Info", "check": "I", "title": "Flex variants per base build",
                            "route": None, "kit": None,
                            "detail": f"max {base['flex_variants_max']}; {base['bases_over_4']} base build(s) "
                                      f"with more than 4 flex variants", "carts": 0, "file": "Pack Plan",
                            "fix": "Information (Jason's recommendation: at most 4 per base)"})
    w = wb.create_sheet("Checks")
    w.append(["Severity", "Check", "Finding", "Route", "Kit", "Detail", "Carts", "File", "What to do"])
    for c in w[1]:
        c.font = Font(bold=True)
    for f in findings:
        w.append([f["severity"], f["check"], f["title"], xl(f["route"]), xl(f["kit"]), xl(f["detail"]),
                  f["carts"], f["file"], f["fix"]])
    for col, wd in zip("ABCDEFGHI", (9, 7, 38, 10, 18, 52, 7, 13, 60)):
        w.column_dimensions[col].width = wd
    w.freeze_panes = "A2"

    report = {
        "checks": pack_checks.summarize(findings),
        "checks_high": sum(1 for f in findings if f["severity"] == "High"),
        "sheet_rows": len(rows), "total_used": grand, "route_config_groups": len(layouts),
        "routes": sorted({r["Route"] for r in rows}), "unrouted_rows": sum(r["Route"] == UNROUTED for r in rows),
        "mix_rows": notes["mix_rows"], "rows_without_aster_code": notes["rows_without_aster_code"],
        "handling_tag_rows": notes["handling_tag_rows"],
        "flex_rows": notes["flex_rows"],
        "base_builds": len(base["bases"]) if base else None,
        "flex_variants_max": base["flex_variants_max"] if base else None,
        "kit_names": len({r["KitDescription"] for r in rows}),
        "kit_variations": len({(r["KitDescription"], r["Variation"]) for r in rows}),
        "units_without_aster_code": sum(r["Total Used"] for r in rows if not r["AsterCode"]),
        "bloomaker_total": sum(r["Total Used"] for r in rows if is_bloom(r)),
        "soil_total": sum(r["Total Used"] for r in rows if is_soil(r)),
    }
    return wb, report


# ── 4. Aster ERP import ──────────────────────────────────────────────────────

def aster_code_for(shelf: dict, ref: dict):
    """(aster_code, kit_type_label, gap_reason|None) for one cart_shelves segment.

    3M lines already are kit codes. 1M grower-pot lines in the 12/17cm families are kitted
    per the engine's determine_kit_type choice (cart_shelves.kit_type, jute/ceramic) by
    swapping to the variety's 3M jute/ceramic code; other families ship the 1M code."""
    line = ref["avail"].get(str(shelf.get("availability_line_id"))) if shelf.get("availability_line_id") else None
    if not line or not line.get("plant_id"):
        return None, shelf.get("kit_type") or "", "no availability line (no Aster variety assigned)"
    code = str(line["plant_id"])
    if line.get("code_level") == "3M":
        return code, line.get("hard_good_type") or "", None
    fam = (ref["skus"].get(shelf["sku_id"]) or {}).get("family")
    kit = shelf.get("kit_type")
    if fam in KIT_FAMILIES and kit in KIT_TYPES:
        k3 = ref["kit3m"].get(_norm_name(ref["aster_names"].get(code, "")), {}).get(kit)
        if k3:
            return k3, kit, None
        return code, "grower_pot", f"no 3M {kit} code for 1M variety"
    return code, "grower_pot", None


def build_aster_csv(model: dict, ref: dict, include_draft: bool = False):
    """Returns (csv text or None, report). Same 5 columns as the legacy Aster export."""
    ok_status = {"approved", "exported"} | ({"draft"} if include_draft else set())
    orders = {o["id"]: o for o in model["orders"] if (o.get("status") or "") in ok_status}
    blocked = {sid for sid, s in ref["stores"].items()
               if (s.get("store_status") or "active") in BLOCK_STATUSES}
    cart_store = {c["id"]: c["store_id"] for c in model["carts"]
                  if c["order_id"] in orders and c["store_id"] not in blocked}
    qty = defaultdict(int)
    kit_of = {}
    gaps = defaultdict(lambda: {"units": 0, "stores": set(), "skus": set()})
    kit_split = Counter()
    for s in model["shelves"]:
        sid = cart_store.get(s["cart_id"])
        if sid is None or not s.get("unit_count"):
            continue
        code, kit, gap = aster_code_for(s, ref)
        if s.get("kit_type") in KIT_TYPES and (ref["skus"].get(s["sku_id"]) or {}).get("family") in KIT_FAMILIES:
            kit_split[s["kit_type"]] += int(s["unit_count"])
        if gap:
            g = gaps[(gap, code or "")]
            g["units"] += int(s["unit_count"])
            g["stores"].add(sid)
            g["skus"].add(s["sku_id"])
        if code is None:
            continue
        qty[(sid, code)] += int(s["unit_count"])
        kit_of[code] = kit
    if not qty and not gaps:
        return None, {"lines": 0, "orders_included": len(orders)}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(ASTER_HEADERS)
    for (sid, code) in sorted(qty):
        w.writerow([sid, code, qty[(sid, code)], ref["aster_names"].get(code, ""), kit_of.get(code, "")])
    unmapped = {k: v for k, v in gaps.items() if k[0].startswith("no availability")}
    if unmapped:
        w.writerow([])
        w.writerow(["# NOT IMPORTABLE — lines with no Aster variety code (excluded above)"])
        w.writerow(["# HD_SKU_ID", "Reason", "Units", "StoresAffected"])
        per_sku = defaultdict(lambda: [0, set()])
        for s in model["shelves"]:
            sid = cart_store.get(s["cart_id"])
            if sid is not None and not s.get("availability_line_id") and s.get("unit_count"):
                per_sku[s["sku_id"]][0] += int(s["unit_count"])
                per_sku[s["sku_id"]][1].add(sid)
        for sk, (u, st) in sorted(per_sku.items()):
            w.writerow([f"# {sk}", "no availability line", u, len(st)])
    tot_kit = sum(kit_split.values()) or 1
    report = {
        "orders_included": len(orders), "lines": len(qty), "units": sum(qty.values()),
        "stores": len({s for s, _ in qty}),
        "codes_3m_kits": sum(1 for c, k in kit_of.items() if k in KIT_TYPES),
        "kit_split_units_12_17cm": dict(kit_split),
        "jute_share_12_17cm": round(kit_split.get("jute_kit", 0) / tot_kit, 3),
        "gaps": [{"reason": r, "code": c, "units": g["units"], "stores": len(g["stores"]),
                  "skus": sorted(g["skus"])} for (r, c), g in sorted(gaps.items(), key=lambda kv: -kv[1]["units"])],
    }
    return buf.getvalue(), report


def workbook_bytes(wb) -> bytes:
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
