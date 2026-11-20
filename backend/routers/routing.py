"""Routing router — store coordinates, load map data, and the Routing Exchange
(export request -> external routing team -> import response -> truck pack plans)."""
import csv
import io
import re
import urllib.request, urllib.parse, json, time, uuid
from collections import defaultdict
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from db import get_db, cursor
from routers.auth import get_current_user, require_role
from routers.concurrency import log_change, bump_activity
from us_cities import lookup_coords
from services.v2_exports import parse_routing_workbook, plan_routes

router = APIRouter()


def _nominatim(city: str, state: str):
    q = urllib.parse.urlencode({"q": f"{city}, {state}, USA", "format": "json", "limit": 1, "countrycodes": "us"})
    url = f"https://nominatim.openstreetmap.org/search?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "JIMDeluxe/1.0 jbeauford@theplantcompany.com"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None, None


@router.get("/run/{run_id}")
def routing_run(run_id: str, user=Depends(get_current_user)):
    """Returns all loads + stores with coordinates for a run."""
    with get_db() as conn:
        cur = cursor(conn)

        # Fetch loads with their store_ids
        cur.execute("""
            SELECT l.id AS load_id, l.load_name, l.region, l.merchant,
                   l.cart_count, l.store_count,
                   array_agg(DISTINCT la.store_id) AS store_ids
            FROM loads l
            JOIN load_assignments la ON la.load_id = l.id
            WHERE l.run_id = %s
            GROUP BY l.id, l.load_name, l.region, l.merchant,
                     l.cart_count, l.store_count
            ORDER BY l.load_number
        """, (run_id,))
        load_rows = cur.fetchall()

        # Collect all store_ids across all loads
        all_store_ids = set()
        for lr in load_rows:
            for sid in (lr["store_ids"] or []):
                if sid is not None:
                    all_store_ids.add(int(sid))

        stores_dict = {}
        missing_count = 0

        if all_store_ids:
            id_list = list(all_store_ids)
            cur.execute("""
                SELECT store_id, store_name, city, state, lat, lng
                FROM stores
                WHERE store_id = ANY(%s)
            """, (id_list,))
            for row in cur.fetchall():
                has_coords = row["lat"] is not None and row["lng"] is not None
                if not has_coords:
                    missing_count += 1
                stores_dict[str(row["store_id"])] = {
                    "store_id": row["store_id"],
                    "store_name": row["store_name"],
                    "city": row["city"],
                    "state": row["state"],
                    "lat": row["lat"],
                    "lng": row["lng"],
                    "has_coords": has_coords,
                }

        loads_out = []
        for lr in load_rows:
            loads_out.append({
                "load_id": lr["load_id"],
                "load_name": lr["load_name"],
                "region": lr["region"],
                "merchant": lr["merchant"],
                "cart_count": lr["cart_count"],
                "store_count": lr["store_count"],
                "store_ids": [int(s) for s in (lr["store_ids"] or []) if s is not None],
            })

        return {
            "loads": loads_out,
            "stores": stores_dict,
            "stores_missing_coords": missing_count,
        }


@router.post("/geocode-batch")
def geocode_batch(user=Depends(require_role("order_writer", "manager", "admin"))):
    """Geocode all stores that have no lat/lng.

    Uses a static US cities dict for instant lookups. Falls back to
    Nominatim (with 1.1s rate-limit sleep) only for cities not in the dict.
    """
    with get_db() as conn:
        cur = cursor(conn)

        # Fetch ALL stores missing coordinates (no LIMIT — static lookup is instant)
        cur.execute("""
            SELECT store_id, city, state
            FROM stores
            WHERE lat IS NULL AND city IS NOT NULL AND state IS NOT NULL
        """)
        rows = cur.fetchall()

        if not rows:
            return {"geocoded": 0, "remaining": 0}

        # Deduplicate by (city, state) — normalize key for dedup only
        seen = {}
        for row in rows:
            key = (row["city"].strip().lower(), row["state"].strip().upper())
            if key not in seen:
                seen[key] = (row["city"].strip(), row["state"].strip())

        geocoded = 0
        nominatim_queue = []  # cities that weren't in the static dict

        for _key, (city, state) in seen.items():
            lat, lng = lookup_coords(city, state)
            if lat is not None:
                cur.execute("""
                    UPDATE stores SET lat = %s, lng = %s
                    WHERE LOWER(TRIM(city)) = LOWER(%s)
                      AND UPPER(TRIM(state)) = UPPER(%s)
                      AND lat IS NULL
                """, (lat, lng, city, state))
                geocoded += cur.rowcount
            else:
                nominatim_queue.append((city, state))

        # Fall back to Nominatim for any cities not covered by the static dict
        for city, state in nominatim_queue:
            lat, lng = _nominatim(city, state)
            if lat is not None and lng is not None:
                cur.execute("""
                    UPDATE stores SET lat = %s, lng = %s
                    WHERE LOWER(TRIM(city)) = LOWER(%s)
                      AND UPPER(TRIM(state)) = UPPER(%s)
                      AND lat IS NULL
                """, (lat, lng, city, state))
                geocoded += cur.rowcount
            time.sleep(1.1)  # Nominatim rate limit: 1 request/second

        # Count remaining
        cur.execute("SELECT COUNT(*) AS cnt FROM stores WHERE lat IS NULL")
        remaining = cur.fetchone()["cnt"]

        return {"geocoded": geocoded, "remaining": remaining}


# ══ ROUTING EXCHANGE (2026-11-16) ═════════════════════════════════════════════
# The engine's loads are PROVISIONAL. The real workflow:
#   1. order writers generate + edit orders (JIM)
#   2. export the Routing Request (stores + carts) to the external routing team
#   3. the team returns Store / Route / Drop / Ship Date / Del Date
#   4. importing that file REPLACES the engine loads with the real trucks
#   5. pack plans are exported per truck (route-level kitting sheet)


@router.get("/runs/{run_id}/exchange-status")
def exchange_status(run_id: str, user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT routing_status, routing_submitted_at, routing_imported_at, routing_filename
            FROM recommendation_runs WHERE id = %s
        """, (run_id,))
        run = cur.fetchone()
        if not run:
            raise HTTPException(404, "Run not found")
        cur.execute("""
            SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE source = 'external_routing') AS ext
            FROM loads WHERE run_id = %s
        """, (run_id,))
        l = cur.fetchone()
        return {**dict(run), "load_count": l["n"], "external_loads": l["ext"]}


@router.get("/runs/{run_id}/request-export")
def routing_request_export(run_id: str,
                           user=Depends(require_role("order_writer", "manager", "admin"))):
    """CSV the routing team plans from: one row per store with cart/unit totals."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT shipping_week FROM recommendation_runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
        if not run:
            raise HTTPException(404, "Run not found")
        cur.execute("""
            SELECT o.store_id, s.store_name, s.city, s.state, s.region, s.merchant,
                   o.cart_count, o.total_units
            FROM orders o JOIN stores s ON s.store_id = o.store_id
            WHERE o.run_id = %s AND COALESCE(o.cart_count, 0) > 0
            ORDER BY s.merchant, s.region, s.state, o.store_id
        """, (run_id,))
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(400, "Run has no orders with carts")

        cur.execute("""
            UPDATE recommendation_runs
            SET routing_status = 'submitted', routing_submitted_at = now(),
                pipeline_status = CASE WHEN engine_version = 'v2'
                                       THEN 'Sent to Pin Routes' ELSE pipeline_status END
            WHERE id = %s AND routing_status <> 'routed'
        """, (run_id,))
        log_change(cur, run_id, "run", run_id, "routing_request_export",
                   user.get("email") if isinstance(user, dict) else None)
        bump_activity(cur, run_id)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Store", "Store Name", "City", "State", "Region", "Merchant", "Carts", "Units"])
    for r in rows:
        w.writerow([r["store_id"], r["store_name"], r["city"], r["state"],
                    r["region"], r["merchant"], r["cart_count"], r["total_units"]])
    wk = run["shipping_week"].isocalendar()[1] if run["shipping_week"] else 0
    fname = f"Routing_Request_WK{wk:02d}.csv"
    return StreamingResponse(io.BytesIO(buf.getvalue().encode("utf-8-sig")),
                             media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


def _parse_date_cell(v: str):
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


@router.post("/runs/{run_id}/response-import")
async def routing_response_import(run_id: str, file: UploadFile = File(...),
                                  user=Depends(require_role("order_writer", "manager", "admin"))):
    """Import the routing team's file (Store, Route, Drop, Ship Date, Del Date).

    Replaces the engine's provisional loads for this run with the real trucks,
    reassigns every cart, and upserts the week's delivery_manifest rows so the
    DUE gate sees the shipments as in-transit immediately."""
    # Read with a hard cap BEFORE buffering the whole body (CVE-2024-47874 class)
    MAX = 5 * 1024 * 1024
    raw = await file.read(MAX + 1)
    if len(raw) > MAX:
        raise HTTPException(400, "File too large (5MB max)")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    rdr = csv.DictReader(io.StringIO(text))
    headers = { (h or "").strip().lower(): h for h in (rdr.fieldnames or []) }

    def col(*names):
        for n in names:
            if n in headers:
                return headers[n]
        return None

    c_store = col("store", "store #", "storeid", "store id")
    c_route = col("route", "load")
    c_drop  = col("drop", "stop", "stop sequence")
    c_ship  = col("ship date", "shipdate")
    c_del   = col("del date", "delivery date", "del. date", "deldate")
    if not (c_store and c_route):
        raise HTTPException(400, f"Need Store and Route columns; found {list(headers)}")

    # Route names come from an EXTERNAL party's file and are rendered in the UI and
    # written into the pack-plan xlsx — whitelist them (also kills formula injection).
    ROUTE_RE = re.compile(r"^[\w\s\-./#]{1,40}$")

    entries, bad, bad_routes = [], 0, set()
    for row in rdr:
        try:
            store = int(float(str(row[c_store]).strip()))
        except (TypeError, ValueError):
            bad += 1
            continue
        route = str(row[c_route]).strip()
        if not route:
            bad += 1
            continue
        if not ROUTE_RE.match(route):
            bad += 1
            bad_routes.add(route[:60])
            continue
        try:
            drop = int(float(str(row[c_drop]).strip())) if c_drop and str(row[c_drop]).strip() else None
        except (TypeError, ValueError):
            drop = None
        entries.append({
            "store_id": store, "route": route, "drop": drop,
            "ship": _parse_date_cell(row[c_ship]) if c_ship else None,
            "deliv": _parse_date_cell(row[c_del]) if c_del else None,
        })
    if not entries:
        raise HTTPException(400, "No parseable rows in the file")

    result = _apply_route_plan(run_id, entries, file.filename, user, allow_split=False)
    return {**result, "rows_bad": bad, "bad_route_names": sorted(bad_routes)}


def _apply_route_plan(run_id: str, entries: list, filename: str | None, user,
                      allow_split: bool, action: str = "routing_response_import") -> dict:
    """Shared by the CSV response import and the routing Workbook import.

    Replaces the engine's provisional loads for this run with the routed trucks, assigns
    every cart to exactly one load (split stops divide a store's carts; the legacy CSV path
    keeps 'first route wins'), and upserts this week's delivery_manifest rows so the DUE
    gate sees the shipments as in-transit immediately."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT shipping_week FROM recommendation_runs WHERE id = %s FOR UPDATE",
                    (run_id,))
        run = cur.fetchone()
        if not run:
            raise HTTPException(404, "Run not found")
        shipping_week = run["shipping_week"]
        week_number = shipping_week.isocalendar()[1] if shipping_week else None

        # Stores in this run and their carts (cart_number order: split stops take carts in order)
        cur.execute("""
            SELECT o.store_id, oc.id AS cart_id
            FROM orders o JOIN order_carts oc ON oc.order_id = o.id
            WHERE o.run_id = %s
            ORDER BY o.store_id, oc.cart_number, oc.id
        """, (run_id,))
        carts_by_store = defaultdict(list)
        for r in cur.fetchall():
            carts_by_store[r["store_id"]].append(r["cart_id"])
        run_stores = set(carts_by_store)

        plan = plan_routes(entries, carts_by_store, allow_split=allow_split)
        if not plan["loads"]:
            raise HTTPException(400, "No stores in the file belong to this run")

        # Replace provisional loads wholesale
        cur.execute("DELETE FROM load_assignments WHERE load_id IN "
                    "(SELECT id FROM loads WHERE run_id = %s)", (run_id,))
        cur.execute("DELETE FROM loads WHERE run_id = %s", (run_id,))

        # Store metadata for load region/merchant stamping
        cur.execute("SELECT store_id, region, merchant FROM stores WHERE store_id = ANY(%s)",
                    (list(run_stores),))
        smeta = {r["store_id"]: r for r in cur.fetchall()}

        load_count = 0
        stops_all = []
        for i, (route, L) in enumerate(sorted(plan["loads"].items()), start=1):
            stops = L["stops"]
            stops_all.extend((route, st) for st in stops)
            load_id = str(uuid.uuid4())
            cart_ids = [c for st in stops for c in st["cart_ids"]]
            regions = [smeta.get(st["store_id"], {}).get("region") for st in stops]
            merchants = [smeta.get(st["store_id"], {}).get("merchant") for st in stops]
            # sorted() makes the majority tie-break deterministic across processes
            region = max(sorted(set(filter(None, regions))), key=regions.count) if any(regions) else None
            merchant = max(sorted(set(filter(None, merchants))), key=merchants.count) if any(merchants) else None
            util = round(len(cart_ids) / 45 * 100, 1)
            cur.execute("""
                INSERT INTO loads (id, run_id, load_number, load_name, merchant, region,
                                   primary_state, cart_count, store_count, capacity,
                                   utilization_pct, is_flagged, flag_reason,
                                   ship_date, delivery_date, source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,45,%s,%s,%s,%s,%s,'external_routing')
            """, (load_id, run_id, i, route, merchant, region,
                  smeta.get(stops[0]["store_id"], {}).get("region"),
                  len(cart_ids), len(stops), util,
                  util < 70, f"Under-utilized ({util:.0f}%)" if util < 70 else None,
                  L["ship"], L["deliv"]))
            rows = [(str(uuid.uuid4()), load_id, cid, st["store_id"], st["drop"])
                    for st in stops for cid in st["cart_ids"]]
            cur.executemany("""
                INSERT INTO load_assignments (id, load_id, order_cart_id, store_id, stop_sequence)
                VALUES (%s,%s,%s,%s,%s)
            """, rows)
            load_count += 1

        # Manifest upsert: the routed shipments become this week's in-transit truth.
        # First clear THIS import's prior rows for the run's stores — a re-import with
        # different routes must not leave stale (store, old_route) rows that would
        # wrongly keep a store "in transit" and suppress it from the next DUE gate.
        manifest_new = manifest_upd = 0
        if week_number is not None:
            cur.execute("""DELETE FROM delivery_manifest
                           WHERE week_number = %s AND source = 'routing_import'
                             AND store_id = ANY(%s)""", (week_number, list(run_stores)))
            cur.execute("""SELECT store_id, load_code, ship_date, delivery_date
                           FROM delivery_manifest WHERE week_number = %s""", (week_number,))
            existing = {(r["store_id"], r["load_code"]): (r["ship_date"], r["delivery_date"])
                        for r in cur.fetchall()}
            for route, st in stops_all:
                ship, deliv = st["ship"], st["deliv"]
                if not (ship or deliv):
                    continue  # no dates = nothing useful for the DUE gate
                key = (st["store_id"], route)
                if key not in existing:
                    cur.execute("""
                        INSERT INTO delivery_manifest (id, store_id, ship_date, delivery_date,
                                                       week_number, load_code, source, uploaded_at)
                        VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, 'routing_import', now())
                    """, (st["store_id"], ship, deliv, week_number, route))
                    existing[key] = (ship, deliv)
                    manifest_new += 1
                elif existing[key] != (ship, deliv):
                    cur.execute("""
                        UPDATE delivery_manifest
                        SET ship_date = COALESCE(%s, ship_date),
                            delivery_date = COALESCE(%s, delivery_date),
                            source = 'routing_import', uploaded_at = now()
                        WHERE week_number = %s AND store_id = %s AND load_code = %s
                    """, (ship, deliv, week_number, st["store_id"], route))
                    manifest_upd += 1

        cur.execute("""
            UPDATE recommendation_runs
            SET routing_status = 'routed', routing_imported_at = now(), routing_filename = %s,
                pipeline_status = CASE WHEN engine_version = 'v2'
                                       THEN 'Routing Response Received' ELSE pipeline_status END
            WHERE id = %s
        """, ((filename or "")[:200], run_id))
        log_change(cur, run_id, "run", run_id, action,
                   user.get("email") if isinstance(user, dict) else None)
        bump_activity(cur, run_id)

    return {
        "loads": load_count,
        "stores_routed": len({st["store_id"] for _, st in stops_all}),
        "carts_assigned": plan["carts_assigned"],
        "stores_on_multiple_routes": plan["stores_on_multiple_routes"],
        "split_stops": plan["split_stops"],
        "split_stops_uneven": plan["split_stops_uneven"],
        "stores_in_file_not_in_run": plan["stores_in_file_not_in_run"],
        "stores_in_run_not_routed": plan["stores_in_run_not_routed"],
        "manifest_rows_added": manifest_new,
        "manifest_rows_updated": manifest_upd,
    }


WORKBOOK_MAX_BYTES = 15 * 1024 * 1024


@router.post("/runs/{run_id}/workbook")
async def routing_workbook_import(run_id: str, file: UploadFile = File(...),
                                  user=Depends(require_role("order_writer", "manager", "admin"))):
    """Import the routing team's 'Week NN-YYYY Workbook.xlsx' (the file routing returns today).

    'Compiled' gives Store -> Load + Ship/Delivery Date; each per-load sheet adds the Drop
    (stop sequence) and split-stop cart counts. Same replace-loads/manifest semantics as the
    CSV response import, but split stops divide a store's carts across their trucks."""
    raw = await file.read(WORKBOOK_MAX_BYTES + 1)
    if len(raw) > WORKBOOK_MAX_BYTES:
        raise HTTPException(400, "File too large (15MB max)")
    if not raw.startswith(b"PK"):
        raise HTTPException(400, "Not an .xlsx workbook (save the routing Workbook as .xlsx)")
    try:
        parsed = parse_routing_workbook(raw)
    except Exception as ex:   # corrupt / non-workbook upload — a 400, never a 500
        raise HTTPException(400, f"Could not read workbook: {type(ex).__name__}")
    if not parsed["entries"]:
        raise HTTPException(400, "No Store/Load rows found (expected a 'Compiled' sheet or per-load sheets)")
    result = _apply_route_plan(run_id, parsed["entries"], file.filename, user, allow_split=True,
                               action="routing_workbook_import")
    return {**result, "loads_in_file": parsed["loads"], "load_sheets": parsed["load_sheets"],
            "non_store_rows": parsed["non_store_rows"], "bad_route_names": parsed["bad_route_names"],
            "rows_bad": parsed["rows_bad"], "warnings": parsed["warnings"][:50]}


@router.get("/runs/{run_id}/pack-plan-export")
def pack_plan_export(run_id: str,
                     user=Depends(require_role("order_writer", "manager", "admin"))):
    """Truck-level pack plan (xlsx): per Route x kit(config) x variation x shelf x SKU,
    with Aster variety split — the sheet packing builds trucks from."""
    try:
        import openpyxl
        from openpyxl.styles import Font
    except ImportError:
        raise HTTPException(503, "openpyxl not installed")
    from routers.exports import _distribute_trays

    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT batch_id, shipping_week FROM recommendation_runs WHERE id = %s",
                    (run_id,))
        run = cur.fetchone()
        if not run:
            raise HTTPException(404, "Run not found")

        cur.execute("""
            SELECT l.load_name, l.ship_date, oc.id AS cart_id, oc.config_code,
                   cs.shelf_position, cs.sku_id, cs.profile_id, cs.kit_type,
                   cs.tray_count, cs.unit_count
            FROM loads l
            JOIN load_assignments la ON la.load_id = l.id
            JOIN order_carts oc ON oc.id = la.order_cart_id
            JOIN cart_shelves cs ON cs.cart_id = oc.id
            WHERE l.run_id = %s
            ORDER BY l.load_name, oc.config_code, oc.id, cs.shelf_position, cs.sku_id
        """, (run_id,))
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(400, "Run has no loads — import the routing file first")

        cur.execute("SELECT sku_id, description, family, size_category FROM skus")
        sku_ref = {r["sku_id"]: r for r in cur.fetchall()}
        cur.execute("""SELECT sku_id, plant_id AS aster_code, units_available
                       FROM availability_lines
                       WHERE batch_id = %s AND plant_id IS NOT NULL AND units_available > 0
                       ORDER BY sku_id, units_available DESC""", (run["batch_id"],))
        aster_pool = defaultdict(list)
        for r in cur.fetchall():
            aster_pool[r["sku_id"]].append((r["aster_code"], r["units_available"]))
        cur.execute("SELECT aster_code, product_name FROM variety_parsed_reference")
        aster_name = {str(r["aster_code"]): r["product_name"] for r in cur.fetchall()}

    # Build cart content signatures: route -> config -> signature group
    carts = defaultdict(list)   # (route, ship, config, cart_id) rows
    for r in rows:
        carts[(r["load_name"], r["ship_date"], r["config_code"], r["cart_id"])].append(r)

    groups = defaultdict(list)  # (route, ship, config, signature) -> [cart_id ...]
    sig_contents = {}
    for (route, ship, cfg, cart_id), shelves in carts.items():
        sig = tuple(sorted((s["shelf_position"], s["sku_id"], s["profile_id"],
                            s["kit_type"], s["tray_count"], s["unit_count"]) for s in shelves))
        groups[(route, ship, cfg, sig)].append(cart_id)
        sig_contents[(route, ship, cfg, sig)] = shelves

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pack Plan"
    hdr = ["ShipDate", "Route", "KitDescription", "KitQuantity", "Variation", "Layer",
           "Upgrade", "Notes", "ItemDescription", "ItemTrays", "ItemEaches",
           "ItemCustomerDescription", "SKU", "AsterCode", "Trays", "Eaches",
           "Total Used", "Message"]
    ws.append(hdr)
    for c in ws[1]:
        c.font = Font(bold=True)

    def xl(v):
        """Excel formula-injection guard for string cells (= + - @ prefixes)."""
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
            return "'" + v
        return v

    var_counter = defaultdict(int)
    for (route, ship, cfg, sig), cart_ids in sorted(
            groups.items(), key=lambda kv: (kv[0][0], kv[0][2], -len(kv[1]))):
        var_counter[(route, cfg)] += 1
        variation = var_counter[(route, cfg)]
        kit_qty = len(cart_ids)
        shelves = sig_contents[(route, ship, cfg, sig)]
        for s in sorted(shelves, key=lambda x: (x["shelf_position"], x["sku_id"])):
            sk = sku_ref.get(s["sku_id"], {})
            item_desc = sk.get("size_category") or sk.get("family") or ""
            total_trays = (s["tray_count"] or 0) * kit_qty
            total_units = (s["unit_count"] or 0) * kit_qty
            split = _distribute_trays(total_trays, aster_pool.get(s["sku_id"], []))
            if not split:
                split = [(None, total_trays)]
            upt = (s["unit_count"] // s["tray_count"]) if s["tray_count"] else 0
            if not s["tray_count"]:
                # unit-only shelf (possible via manual cart edits): emit one
                # un-split row rather than dropping the line entirely
                split = [(None, 0)]
            for code, trays in split:
                if trays <= 0 and s["tray_count"]:
                    continue
                eaches = trays * upt if upt else total_units
                ws.append([
                    ship, xl(route), xl(cfg or ""), kit_qty, variation, s["shelf_position"],
                    "", "", xl(item_desc), s["tray_count"], s["unit_count"],
                    xl(aster_name.get(str(code), sk.get("description") or "")),
                    str(s["sku_id"]), xl(str(code) if code else ""), trays, eaches,
                    eaches, "",
                ])

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    wk = run["shipping_week"].isocalendar()[1] if run["shipping_week"] else 0
    fname = f"Pack_Plan_WK{wk:02d}.xlsx"
    return StreamingResponse(
        out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
