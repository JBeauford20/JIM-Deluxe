"""Routing router — store coordinates, load map data, and the Routing Exchange
(export request -> external routing team -> import response -> truck pack plans)."""
import csv
import io
import urllib.request, urllib.parse, json, time, uuid
from collections import defaultdict
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from db import get_db, cursor
from routers.auth import get_current_user, require_role
from routers.concurrency import log_change, bump_activity
from us_cities import lookup_coords

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
            SET routing_status = 'submitted', routing_submitted_at = now()
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
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(400, "File too large")
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

    entries, bad = [], 0
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

    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT shipping_week FROM recommendation_runs WHERE id = %s FOR UPDATE",
                    (run_id,))
        run = cur.fetchone()
        if not run:
            raise HTTPException(404, "Run not found")
        shipping_week = run["shipping_week"]
        week_number = shipping_week.isocalendar()[1] if shipping_week else None

        # Stores in this run and their carts
        cur.execute("""
            SELECT o.store_id, oc.id AS cart_id
            FROM orders o JOIN order_carts oc ON oc.order_id = o.id
            WHERE o.run_id = %s
        """, (run_id,))
        carts_by_store = defaultdict(list)
        for r in cur.fetchall():
            carts_by_store[r["store_id"]].append(r["cart_id"])
        run_stores = set(carts_by_store)

        file_stores = {e["store_id"] for e in entries}
        unmatched = sorted(file_stores - run_stores)          # in file, not in run
        unrouted = sorted(run_stores - file_stores)           # in run, not in file
        matched = [e for e in entries if e["store_id"] in run_stores]
        if not matched:
            raise HTTPException(400, "No stores in the file belong to this run")

        # Replace provisional loads wholesale
        cur.execute("DELETE FROM load_assignments WHERE load_id IN "
                    "(SELECT id FROM loads WHERE run_id = %s)", (run_id,))
        cur.execute("DELETE FROM loads WHERE run_id = %s", (run_id,))

        # Group by route, keep file order for stop sequence fallback
        routes = defaultdict(list)
        for e in matched:
            routes[e["route"]].append(e)

        # Store metadata for load region/merchant stamping
        cur.execute("SELECT store_id, region, merchant FROM stores WHERE store_id = ANY(%s)",
                    (list(run_stores),))
        smeta = {r["store_id"]: r for r in cur.fetchall()}

        load_count = 0
        for i, (route, stops) in enumerate(sorted(routes.items()), start=1):
            load_id = str(uuid.uuid4())
            cart_ids = [c for e in stops for c in carts_by_store[e["store_id"]]]
            regions = [smeta.get(e["store_id"], {}).get("region") for e in stops]
            merchants = [smeta.get(e["store_id"], {}).get("merchant") for e in stops]
            region = max(set(filter(None, regions)), key=regions.count) if any(regions) else None
            merchant = max(set(filter(None, merchants)), key=merchants.count) if any(merchants) else None
            ship = next((e["ship"] for e in stops if e["ship"]), None)
            deliv = next((e["deliv"] for e in stops if e["deliv"]), None)
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
                  ship, deliv))
            rows = []
            for e in stops:
                seq = e["drop"] if e["drop"] is not None else (stops.index(e) + 1)
                for cid in carts_by_store[e["store_id"]]:
                    rows.append((str(uuid.uuid4()), load_id, cid, e["store_id"], seq))
            cur.executemany("""
                INSERT INTO load_assignments (id, load_id, order_cart_id, store_id, stop_sequence)
                VALUES (%s,%s,%s,%s,%s)
            """, rows)
            load_count += 1

        # Manifest upsert: the routed shipments become this week's in-transit truth
        manifest_new = manifest_upd = 0
        if week_number is not None:
            cur.execute("""SELECT store_id, load_code, ship_date, delivery_date
                           FROM delivery_manifest WHERE week_number = %s""", (week_number,))
            existing = {(r["store_id"], r["load_code"]): (r["ship_date"], r["delivery_date"])
                        for r in cur.fetchall()}
            for e in matched:
                if not (e["ship"] or e["deliv"]):
                    continue  # no dates = nothing useful for the DUE gate
                key = (e["store_id"], e["route"])
                if key not in existing:
                    cur.execute("""
                        INSERT INTO delivery_manifest (id, store_id, ship_date, delivery_date,
                                                       week_number, load_code, source, uploaded_at)
                        VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, 'routing_import', now())
                    """, (e["store_id"], e["ship"], e["deliv"], week_number, e["route"]))
                    manifest_new += 1
                elif existing[key] != (e["ship"], e["deliv"]) and (e["ship"] or e["deliv"]):
                    cur.execute("""
                        UPDATE delivery_manifest
                        SET ship_date = COALESCE(%s, ship_date),
                            delivery_date = COALESCE(%s, delivery_date),
                            source = 'routing_import', uploaded_at = now()
                        WHERE week_number = %s AND store_id = %s AND load_code = %s
                    """, (e["ship"], e["deliv"], week_number, e["store_id"], e["route"]))
                    manifest_upd += 1

        cur.execute("""
            UPDATE recommendation_runs
            SET routing_status = 'routed', routing_imported_at = now(), routing_filename = %s
            WHERE id = %s
        """, (file.filename, run_id))
        log_change(cur, run_id, "run", run_id, "routing_response_import",
                   user.get("email") if isinstance(user, dict) else None)
        bump_activity(cur, run_id)

    return {
        "loads": load_count,
        "stores_routed": len({e['store_id'] for e in matched}),
        "rows_bad": bad,
        "stores_in_file_not_in_run": unmatched,
        "stores_in_run_not_routed": unrouted,
        "manifest_rows_added": manifest_new,
        "manifest_rows_updated": manifest_upd,
    }


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
                       FROM availability_lines WHERE batch_id = %s AND plant_id IS NOT NULL
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
            for code, trays in split:
                if trays <= 0:
                    continue
                eaches = trays * upt if upt else total_units
                ws.append([
                    ship, route, cfg or "", kit_qty, variation, s["shelf_position"],
                    "", "", item_desc, s["tray_count"], s["unit_count"],
                    aster_name.get(str(code), sk.get("description") or ""),
                    str(s["sku_id"]), code or "", trays, eaches, eaches, "",
                ])

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    wk = run["shipping_week"].isocalendar()[1] if run["shipping_week"] else 0
    fname = f"Pack_Plan_WK{wk:02d}.xlsx"
    return StreamingResponse(
        out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
