"""Weekly pack-plan check list (Sarah & Christa's 'Building Home Depot Pack Plans: Working
Guide', consolidated 2026-11-25; ShelfKit v3.68 physical rules). Pure Python, no DB.

run_checks(model, ref, rows) -> list of findings, worst first. Each finding:
  {severity: High|Medium|Low, check: A..H, title, detail, routes, kits, carts, file, fix}
High = wrong on the floor or the truck if nobody fixes it; Medium = will cause a question or a
mis-pull; Low = tidy-up. Findings are grouped per route x kit so one bad build reads once.

The physical model here is the pack team's, not the engine's: real tray footprints across a
50 1/2 in CC shelf (51 in with the 1/4 in overhang), shelf rungs of 10/15/21/29 in, at most 5
shelves and 95 in per CC, 4 shelves on any cart carrying 17cm.
"""
from collections import defaultdict

try:
    from workers import shelfkit as SK
except ImportError:                       # backend/workers on sys.path (engine, tests)
    import shelfkit as SK

CC_SHELF_IN, CC_FIT_IN, FILLED_STRIP_IN = SK.CC_SHELF_IN, SK.CC_FIT_IN, SK.FILLED_STRIP_IN
MIN_SHELF_FILL, CC_MAX_SHELVES, CC_MAX_HEIGHT_IN = SK.MIN_SHELF_FILL, SK.CC_MAX_SHELVES, SK.CC_MAX_HEIGHT_IN
CC_17CM_MAX_SHELVES = SK.CC_17CM_MAX_SHELVES
BOWL_SKUS = {1012142347}    # regular H2O Bowl; max 3 trays per cart, never a full shelf
BOWL_MAX_PER_CART = 3
VENTI_SKU = 1012138483
GLOBE_SKU = 1015258015      # Climb-itt Globe: always 3 per tray
LOAD_CART_CAP, LOAD_FULL_MIN = 45, 43
STOPS_SOFT, STOPS_HARD = 20, 22
TPC_TOLERANCE = 0.10        # TPC may run 10% over Balance; CG never
NEVER_SUBSTITUTE_SKUS = {1012994247}       # Anthurium Michelle Doc Block
# standing SKU overrides by Aster code (guide section 6)
ASTER_SKU_OVERRIDES = {"1000464": 1015555690, "1001666": 1015555690, "1000853": 1013310815,
                       "1001609": 1013310815, "1000614": 1012138481, "1000615": 1012138481,
                       "1001359": 1012138481, "1001429": 1012138484, "1001430": 1012138484}

PROFILE_FIT = SK.PROFILE_FIT
shelf_width = SK.shelf_width
shelf_rung = SK.shelf_rung


def cart_layers(shelves):
    """{layer: [(profile_id, trays, sku_id, units)]} for one cart's cart_shelves rows."""
    out = defaultdict(list)
    for s in shelves:
        out[int(s["shelf_position"])].append((s.get("profile_id"), int(s.get("tray_count") or 0),
                                              int(s["sku_id"]), int(s.get("unit_count") or 0)))
    return dict(out)


def check_cart(layers):
    """Physical findings for one cart: list of (severity, check, title, detail)."""
    f = []
    n = len(layers)
    rungs = {L: shelf_rung({it[0] for it in its}) for L, its in layers.items()}
    height = sum(rungs.values())
    has17 = any(it[0] in ("box17",) for its in layers.values() for it in its)
    if n > CC_MAX_SHELVES:
        f.append(("High", "B", "Cart over 5 shelves", f"{n} shelves"))
    if height > CC_MAX_HEIGHT_IN:
        f.append(("High", "B", "Cart over 95 in", f"{height} in ({' + '.join(str(rungs[L]) for L in sorted(rungs))})"))
    if has17 and n > CC_17CM_MAX_SHELVES:
        f.append(("Medium", "B", "17cm cart over 4 shelves", f"{n} shelves on a cart carrying 17cm"))
    for L, its in sorted(layers.items()):
        w = shelf_width([(p, t) for p, t, _, _ in its])
        if w > CC_FIT_IN + 1e-9:
            f.append(("High", "A", "Shelf too wide", f"layer {L}: {w:g} in across (max 51)"))
        elif not SK.shelf_is_full([(p, t) for p, t, _, _ in its]):
            # Josh 2026-11-25: every shelf full (the 80% rule is per cart, not per shelf)
            f.append(("Medium", "A", "Shelf not full", f"layer {L}: {w:g} of 50.5 in ({w / CC_SHELF_IN:.0%})"))
    bowl_t = sum(t for its in layers.values() for _, t, sk, _ in its if sk in BOWL_SKUS)
    if bowl_t > BOWL_MAX_PER_CART:
        f.append(("Medium", "C", "More than 3 H2O Bowl trays on a cart", f"{bowl_t} bowl trays"))
    for L, its in layers.items():
        if its and all(sk in BOWL_SKUS for _, _, sk, _ in its) and \
                shelf_width([(p, t) for p, t, _, _ in its]) / CC_SHELF_IN >= MIN_SHELF_FILL:
            f.append(("Medium", "C", "Full shelf of H2O Bowls", f"layer {L}"))
    # hotel floors: shelf 1 is the bottom, tallest class lowest (17cm always shelf 1)
    order = [rungs[L] for L in sorted(rungs)]
    if any(order[i] < order[i + 1] for i in range(len(order) - 1)):
        f.append(("Low", "C", "Shelf order not tallest-first from shelf 1",
                  " / ".join(f"L{L}:{rungs[L]}in" for L in sorted(rungs))))
    if sorted(layers) != list(range(1, n + 1)):
        f.append(("Low", "C", "Layer numbers have gaps", f"layers {sorted(layers)}"))
    for _, t, sk, u in (it for its in layers.values() for it in its):
        if sk == GLOBE_SKU and t and u != 3 * t:
            f.append(("Medium", "C", "Climb-itt Globe not 3 per tray", f"{t} trays / {u} units"))
        if sk == VENTI_SKU and t and u != 6 * t:
            f.append(("Medium", "C", "Venti not 6 per tray", f"{t} trays / {u} units"))
    return f


def _route_index(model):
    load_by_id = {l["id"]: l for l in model.get("loads", [])}
    route_of, stop_of = {}, {}
    for a in model.get("assignments", []):
        L = load_by_id.get(a["load_id"])
        if L:
            route_of[a["order_cart_id"]] = L["load_name"]
            stop_of[a["order_cart_id"]] = a.get("stop_sequence")
    return load_by_id, route_of


def run_checks(model, ref, rows, kit_name_of=None, wire_clip_unconfirmed=True):
    """The guide's weekly check list over a run's carts, routing and plan rows."""
    skus, avail, stores = ref.get("skus", {}), ref.get("avail", {}), ref.get("stores", {})
    load_by_id, route_of = _route_index(model)
    kit_name_of = kit_name_of or {}
    sh_by_cart = defaultdict(list)
    for s in model.get("shelves", []):
        sh_by_cart[s["cart_id"]].append(s)
    agg = {}                      # (sev, check, title, route, kit, detail) -> carts

    def add(sev, chk, title, detail, route=None, kit=None, carts=1, file="Pack Plan", fix=""):
        k = (sev, chk, title, route, kit, detail, file, fix)
        agg[k] = agg.get(k, 0) + carts

    # A/B/C: physical, per cart
    for c in model.get("carts", []):
        route = route_of.get(c["id"], "UNROUTED")
        kit = kit_name_of.get(c["id"], c.get("config_code"))
        for sev, chk, title, detail in check_cart(cart_layers(sh_by_cart.get(c["id"], []))):
            fix = {"A": "Rebuild the shelf: move or cut the lowest-retail tray (Sarah decides)",
                   "B": "Cut the lowest-value tray/shelf or move a shelf to the store's other cart (Sarah decides)",
                   "C": "Fix the build in the plan"}[chk]
            add(sev, chk, title, detail, route, kit, 1, fix=fix)

    # C: plan rows (SKU rules)
    for r in rows:
        code, sku = (r.get("AsterCode") or None), int(r["SKU"]) if r.get("SKU") else None
        if not code:
            add("Medium", "C", "Line with no Aster code", f"SKU {sku} {r.get('ItemCustomerDescription', '')}"[:120],
                r.get("Route"), r.get("KitDescription"), r.get("KitQuantity") or 0,
                fix="Assign a code from the restock (largest balance not already on the cart)")
        if code in ASTER_SKU_OVERRIDES and sku != ASTER_SKU_OVERRIDES[code]:
            add("Medium", "C", "Standing SKU override not applied",
                f"Aster {code} on SKU {sku}; should be {ASTER_SKU_OVERRIDES[code]}",
                r.get("Route"), r.get("KitDescription"), r.get("KitQuantity") or 0,
                fix="Ask before changing a SKU (guide section 6)")

    # D: every cart on a route; every routed store has carts
    unrouted = [c for c in model.get("carts", []) if c["id"] not in route_of]
    if unrouted and model.get("loads"):
        add("High", "D", "Carts not on any route", f"{len(unrouted)} cart(s) at "
            f"{len({c['store_id'] for c in unrouted})} store(s)", None, None, len(unrouted), "Workbook",
            "Import the routing workbook again or add these stores to a load")
    elif unrouted:
        add("High", "D", "Routing not imported", "no loads for this run", None, None, len(unrouted),
            "Workbook", "Import the routing workbook (Routing > response import)")

    # E: availability by Aster (CG never over; TPC <= 10% over); bowls/Venti by SKU
    used_code, used_sku = defaultdict(int), defaultdict(int)
    for s in model.get("shelves", []):
        line = avail.get(str(s.get("availability_line_id"))) if s.get("availability_line_id") else None
        u = int(s.get("unit_count") or 0)
        used_sku[int(s["sku_id"])] += u
        if line and line.get("plant_id"):
            used_code[(str(line["plant_id"]), line.get("shipping_origin") or "TPC")] += u
        elif int(s["sku_id"]) not in BOWL_SKUS:
            add("Medium", "E", "Units with no Aster code", f"SKU {s['sku_id']}", None, None, 0,
                "Availability", "Assign a code or confirm it ships outside Aster (soil, hardware)")
    have_code = defaultdict(int)
    for line in avail.values():
        if line.get("plant_id"):
            have_code[(str(line["plant_id"]), line.get("shipping_origin") or "TPC")] += int(line.get("units_available") or 0)
    for (code, src), u in used_code.items():
        bal = have_code.get((code, src), 0)
        if src == "CG" and u > bal:
            add("High", "E", "CG code over availability", f"Aster {code}: {u} used vs {bal}", file="Availability",
                fix="Move the overage to another code (CG has no tolerance)")
        elif src != "CG" and u > bal * (1 + TPC_TOLERANCE):
            add("High", "E", "TPC code over 10% tolerance", f"Aster {code}: {u} used vs {bal}", file="Availability",
                fix="Move the overage to another code")
    sku_have = defaultdict(int)
    for line in avail.values():
        sku_have[int(line["sku_id"])] += int(line.get("units_available") or 0)
    for sk in BOWL_SKUS | {VENTI_SKU}:
        if used_sku.get(sk, 0) > sku_have.get(sk, 0):
            add("High", "E", "H2O Bowl / Venti over stock (by SKU)", f"SKU {sk}: {used_sku[sk]} vs {sku_have[sk]}",
                file="Availability", fix="Cut bowl subs first (max 2 sub trays per cart)")

    # F: loads (45-cart cap, stops, single-cart drops, Del Dates)
    carts_by_load, stores_by_load, carts_at = defaultdict(int), defaultdict(set), defaultdict(int)
    for a in model.get("assignments", []):
        carts_by_load[a["load_id"]] += 1
        stores_by_load[a["load_id"]].add(a["store_id"])
        carts_at[(a["load_id"], a["store_id"])] += 1
    for lid, n in carts_by_load.items():
        L = load_by_id.get(lid) or {}
        name = L.get("load_name")
        if n > LOAD_CART_CAP:
            add("High", "F", "Load over 45 carts", f"{n} carts", name, None, n, "Workbook", "Move stops to another load")
        elif n < LOAD_FULL_MIN:
            add("Medium", "F", "Light load (under 43 carts)", f"{n} carts", name, None, n, "Workbook",
                "Confirm with transportation")
        st = len(stores_by_load[lid])
        if st > STOPS_HARD:
            add("Medium", "F", "Load over 22 stops", f"{st} stops", name, None, 0, "Workbook", "Split the load")
        if not L.get("delivery_date"):
            add("Medium", "F", "Load with no Del Date", "", name, None, 0, "Workbook", "Add the delivery date")
    singles = defaultdict(int)
    for (lid, st), n in carts_at.items():
        if n == 1:
            singles[(load_by_id.get(lid) or {}).get("load_name")] += 1
    for name, n in singles.items():
        add("Low", "F", "Single-cart drops", f"{n} store(s) with one cart", name, None, n, "Workbook",
            "Guide: 2 carts per store unless transportation planned it")

    # G: open rulings worth repeating every week
    acc_used = {sk for sk in used_sku if sk in (1015443111, 1015443113)}
    if wire_clip_unconfirmed and acc_used:
        add("Medium", "G", "Climb-itt Wire/Clips box size unconfirmed",
            "Jason's brief: Wire 25, Clips 50 per box; ShelfKit guide: 150 each. Ops to confirm.",
            None, None, 0, "Pack Plan", "Confirm box counts before pulling")

    # H: floor-sheet lookups (retail / UPC) present for every shipped SKU
    for sk in sorted(used_sku):
        info = skus.get(sk) or {}
        if not info.get("upc"):
            add("Medium", "H", "SKU with no UPC for the floor sheet", f"SKU {sk} {info.get('description', '')}"[:120],
                file="SKU lookup", fix="Add the UPC to the SKU master")
        if not info.get("retail_price"):
            add("Medium", "H", "SKU with no retail for the floor sheet", f"SKU {sk} {info.get('description', '')}"[:120],
                file="SKU lookup", fix="Add the HD retail to the price book")
    sev_rank = {"High": 0, "Medium": 1, "Low": 2}
    out = [{"severity": k[0], "check": k[1], "title": k[2], "route": k[3], "kit": k[4], "detail": k[5],
            "carts": v, "file": k[6], "fix": k[7]} for k, v in agg.items()]
    out.sort(key=lambda x: (sev_rank[x["severity"]], x["check"], x["title"], str(x["route"]), str(x["kit"])))
    return out


def summarize(findings):
    s = defaultdict(lambda: defaultdict(int))
    for f in findings:
        s[f["severity"]][f["title"]] += 1
    return {sev: dict(t) for sev, t in s.items()}
