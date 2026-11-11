"""
Permanent Cart Config builder (§6D) — standalone, reusable cart templates that exist
independent of any order/run. Users design a cart (named, shelves, SKUs, split shelves)
and save it to cart_templates / cart_template_shelves for use in future orders. The
finite-hardgood sandbox ("this cart × N stores = M units per SKU") is computed client-side
from the saved config, so no batch is required to design one.
"""
from typing import List, Optional
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from db import get_db, cursor
from routers.auth import get_current_user, require_role

router = APIRouter()


# ── Models ────────────────────────────────────────────────────
class ShelfItem(BaseModel):
    sku_id: int
    tray_count: int = 1
    kit_type: Optional[str] = None
    profile_id: Optional[str] = None   # each item carries its own tray profile (mixed shelves)


class ShelfIn(BaseModel):
    shelf_position: int
    profile_id: Optional[str] = None   # legacy fallback when an item has none
    items: List[ShelfItem]


class PermanentCartIn(BaseModel):
    name: str
    cart_type_id: str = "cc"
    notes: Optional[str] = None
    shelves: List[ShelfIn]


# ── Catalog: the SKU menu + shelf-profile specs for the builder ──
@router.get("/catalog")
def catalog(user=Depends(get_current_user)):
    """Everything the builder needs: every active SKU with its default tray profile, and
    the CC shelf profiles (trays/units per shelf, max shelves)."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT s.sku_id, s.description, s.family, s.legacy_product_group,
                   spp.profile_id, pp.units_per_tray, pp.height_class, pp.short_name
            FROM skus s
            LEFT JOIN sku_packing_profiles spp ON spp.sku_id = s.sku_id AND spp.is_default = true
            LEFT JOIN packing_profiles pp ON pp.id = spp.profile_id
            WHERE s.active = true
            ORDER BY s.family, s.description
        """)
        skus = cur.fetchall()
        cur.execute("""
            SELECT sc.profile_id, sc.trays_per_shelf, sc.units_per_shelf, sc.max_shelves,
                   pp.units_per_tray, pp.height_class, pp.short_name, pp.clear_height_in,
                   pp.tray_width_mm, pp.tray_depth_mm, pp.tray_color
            FROM shelf_configurations sc
            JOIN packing_profiles pp ON pp.id = sc.profile_id
            WHERE sc.cart_type_id = 'cc' AND sc.approved = true
            ORDER BY sc.profile_id
        """)
        profiles = cur.fetchall()
        cur.execute("""
            SELECT id, name, max_shelves, ceiling_in, shelf_width_in, shelf_depth_in,
                   base_height_in, total_height_in
            FROM cart_types WHERE id = 'cc'
        """)
        cart = cur.fetchone()
    return {"skus": skus, "profiles": profiles, "cart": cart}


# ── List / detail ──────────────────────────────────────────────
@router.get("")
@router.get("/")
def list_carts(user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT id, name, cart_type_id, total_shelves, total_units,
                   created_by, created_at, notes
            FROM cart_templates
            ORDER BY created_at DESC
        """)
        return cur.fetchall()


@router.get("/{template_id}")
def get_cart(template_id: str, user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT * FROM cart_templates WHERE id = %s", (template_id,))
        t = cur.fetchone()
        if not t:
            raise HTTPException(404, "Permanent cart not found")
        cur.execute("""
            SELECT cts.shelf_position, cts.profile_id, cts.sku_id, cts.tray_count,
                   cts.unit_count, cts.kit_type, k.description, k.family,
                   pp.short_name, pp.height_class
            FROM cart_template_shelves cts
            JOIN skus k ON k.sku_id = cts.sku_id
            LEFT JOIN packing_profiles pp ON pp.id = cts.profile_id
            WHERE cts.template_id = %s
            ORDER BY cts.shelf_position, cts.unit_count DESC
        """, (template_id,))
        rows = cur.fetchall()
    shelves: dict = {}
    order: list = []
    for r in rows:
        pos = r["shelf_position"]
        if pos not in shelves:
            shelves[pos] = {"shelf_position": pos, "profile_id": r["profile_id"],
                            "short_name": r["short_name"], "height_class": r["height_class"],
                            "items": []}
            order.append(pos)
        shelves[pos]["items"].append({
            "sku_id": r["sku_id"], "description": r["description"], "family": r["family"],
            "tray_count": r["tray_count"], "unit_count": r["unit_count"], "kit_type": r["kit_type"],
            "profile_id": r["profile_id"],
        })
    return {**t, "shelves": [shelves[p] for p in order]}


# ── Create / delete ────────────────────────────────────────────
@router.post("")
@router.post("/")
def create_cart(req: PermanentCartIn, user=Depends(require_role("order_writer", "manager", "admin"))):
    if not req.name.strip():
        raise HTTPException(400, "Name is required")
    if not req.shelves:
        raise HTTPException(400, "Add at least one shelf")
    with get_db() as conn:
        cur = cursor(conn)
        # units_per_tray per profile (so unit_count is authoritative server-side)
        cur.execute("SELECT id, units_per_tray FROM packing_profiles")
        upt = {r["id"]: (r["units_per_tray"] or 0) for r in cur.fetchall()}

        import uuid
        tid = str(uuid.uuid4())
        total_units = 0
        positions = set()
        shelf_rows = []
        for sh in req.shelves:
            for it in sh.items:
                prof = it.profile_id or sh.profile_id
                units = (it.tray_count or 0) * upt.get(prof, 0)
                total_units += units
                positions.add(sh.shelf_position)
                shelf_rows.append((tid, sh.shelf_position, it.sku_id, prof,
                                   it.kit_type, it.tray_count, units))
        cur.execute("""
            INSERT INTO cart_templates (id, name, cart_type_id, created_by, is_approved,
                                        total_shelves, total_units, notes)
            VALUES (%s,%s,%s,%s,false,%s,%s,%s)
        """, (tid, req.name.strip(), req.cart_type_id,
              (user.get("email") if isinstance(user, dict) else None),
              len(positions), total_units, req.notes))
        from psycopg2.extras import execute_batch
        execute_batch(cur, """
            INSERT INTO cart_template_shelves
                (template_id, shelf_position, sku_id, profile_id, kit_type, tray_count, unit_count)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, shelf_rows)
    return {"id": tid, "total_shelves": len(positions), "total_units": total_units}


@router.post("/{template_id}/assign/{order_id}")
def assign_to_order(template_id: str, order_id: str,
                    user=Depends(require_role("order_writer", "manager", "admin"))):
    """Assign a saved permanent cart to a store's order as a new one-off cart. Blocks if the
    cart's SKUs aren't available this week (lists the shortfall); otherwise resolves each SKU
    to specific Aster varieties from what's REMAINING, allocates through the ledger (never
    negative), and updates order_lines + totals."""
    import uuid
    from psycopg2.extras import execute_batch
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT id, store_id, run_id FROM orders WHERE id = %s FOR UPDATE", (order_id,))
        order = cur.fetchone()
        if not order:
            raise HTTPException(404, "Order not found")
        cur.execute("SELECT batch_id FROM recommendation_runs WHERE id = %s", (order["run_id"],))
        br = cur.fetchone()
        batch_id = br["batch_id"] if br else None
        if not batch_id:
            raise HTTPException(400, "Run has no availability batch")

        cur.execute("SELECT name FROM cart_templates WHERE id = %s", (template_id,))
        tmpl = cur.fetchone()
        if not tmpl:
            raise HTTPException(404, "Permanent cart not found")
        cur.execute("""
            SELECT shelf_position, sku_id, profile_id, tray_count, unit_count
            FROM cart_template_shelves WHERE template_id = %s ORDER BY shelf_position
        """, (template_id,))
        tshelves = cur.fetchall()
        if not tshelves:
            raise HTTPException(400, "Permanent cart has no shelves")

        req_by_sku: dict = defaultdict(int)
        for s in tshelves:
            req_by_sku[s["sku_id"]] += s["unit_count"]
        skus = list(req_by_sku)

        # Lock the availability rows for every SKU this cart draws, then compute remaining
        # per Aster line = units_available − ledger allocations − live reservations.
        cur.execute("SELECT id FROM availability_lines WHERE batch_id = %s AND sku_id = ANY(%s) FOR UPDATE",
                    (batch_id, skus))
        cur.execute("""
            WITH alloc AS (SELECT availability_line_id, SUM(qty) q
                           FROM order_line_allocations WHERE batch_id = %s GROUP BY 1),
                 res AS (SELECT availability_line_id, SUM(reserved_qty) q
                         FROM allocation_reservations WHERE reservation_type <> 'released' GROUP BY 1)
            SELECT al.id AS line_id, al.sku_id,
                   (al.units_available - COALESCE(alloc.q,0) - COALESCE(res.q,0)) AS remaining
            FROM availability_lines al
            LEFT JOIN alloc ON alloc.availability_line_id = al.id
            LEFT JOIN res   ON res.availability_line_id = al.id
            WHERE al.batch_id = %s AND al.sku_id = ANY(%s)
        """, (batch_id, batch_id, skus))
        pool: dict = defaultdict(list)
        avail_by_sku: dict = defaultdict(int)
        for r in cur.fetchall():
            rem = int(r["remaining"] or 0)
            if rem > 0:
                pool[r["sku_id"]].append([str(r["line_id"]), rem])
                avail_by_sku[r["sku_id"]] += rem

        # BLOCK on shortfall — list exactly what's short.
        short = [{"sku_id": sku, "need": need, "have": avail_by_sku.get(sku, 0)}
                 for sku, need in req_by_sku.items() if avail_by_sku.get(sku, 0) < need]
        if short:
            cur.execute("SELECT sku_id, description FROM skus WHERE sku_id = ANY(%s)",
                        ([s["sku_id"] for s in short],))
            desc = {r["sku_id"]: r["description"] for r in cur.fetchall()}
            detail = "; ".join(f"{desc.get(s['sku_id'], s['sku_id'])}: need {s['need']}, have {s['have']}"
                               for s in short)
            raise HTTPException(400, f"Not enough availability this week — {detail}")

        for sku in pool:
            pool[sku].sort(key=lambda x: -x[1])   # draw from the most-available line first

        # New one-off cart on this store.
        cur.execute("SELECT COALESCE(MAX(cart_number),0)+1 AS n FROM order_carts WHERE order_id = %s",
                    (order_id,))
        cart_num = cur.fetchone()["n"]
        cart_id = str(uuid.uuid4())
        total_units = sum(req_by_sku.values())
        positions = len(set(s["shelf_position"] for s in tshelves))
        label = ("PC " + (tmpl["name"] or ""))[:40]
        cur.execute("""
            INSERT INTO order_carts (id, order_id, store_id, cart_number, cart_type_id,
                 shelves_used, shelves_capacity, total_units, is_full, is_flagged,
                 flag_reason, effective_max_shelves, cart_key, config_code)
            VALUES (%s,%s,%s,%s,'cc',%s,5,%s,%s,false,'permanent cart',5,%s,%s)
        """, (cart_id, order_id, order["store_id"], cart_num, positions, total_units,
              positions >= 5, f"PC-{cart_num:04d}", label))

        shelf_rows = []
        ledger_pending = []   # (sku_id, cart_shelf_id, availability_line_id, qty)
        for s in tshelves:
            sku, need, prof, trays, item_units = (s["sku_id"], s["unit_count"], s["profile_id"],
                                                  s["tray_count"], s["unit_count"])
            for line in pool[sku]:
                if need <= 0:
                    break
                take = min(need, line[1])
                if take <= 0:
                    continue
                cs_id = str(uuid.uuid4())
                seg_trays = round(trays * take / item_units) if item_units else trays
                shelf_rows.append((cs_id, cart_id, s["shelf_position"], sku, prof, None,
                                   seg_trays, take, line[0]))
                ledger_pending.append((sku, cs_id, line[0], take))
                line[1] -= take
                need -= take

        execute_batch(cur, """
            INSERT INTO cart_shelves (id, cart_id, shelf_position, sku_id, profile_id,
                 kit_type, tray_count, unit_count, availability_line_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, shelf_rows)

        # order_lines: add this cart's units per SKU (create if missing).
        for sku, units in req_by_sku.items():
            cur.execute("""
                UPDATE order_lines SET draft_qty = COALESCE(draft_qty,0) + %s, is_manual_edit = true
                WHERE order_id = %s AND sku_id = %s
            """, (units, order_id, sku))
            if cur.rowcount == 0:
                cur.execute("""
                    INSERT INTO order_lines (order_id, store_id, sku_id, recommended_qty, draft_qty, is_manual_edit)
                    VALUES (%s,%s,%s,0,%s,true)
                """, (order_id, order["store_id"], sku, units))

        # Ledger rows (link each segment to its order_line + physical shelf).
        cur.execute("SELECT id, sku_id FROM order_lines WHERE order_id = %s AND sku_id = ANY(%s)",
                    (order_id, skus))
        ol_map = {r["sku_id"]: r["id"] for r in cur.fetchall()}
        ledger_rows = [(ol_map.get(sku), lid, cs_id, str(order["run_id"]), batch_id, sku, qty, "permanent_cart")
                       for (sku, cs_id, lid, qty) in ledger_pending]
        execute_batch(cur, """
            INSERT INTO order_line_allocations
                (order_line_id, availability_line_id, cart_shelf_id, run_id, batch_id, sku_id, qty, source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, ledger_rows)

        # Order totals.
        cur.execute("""
            UPDATE orders SET
                total_units = (SELECT COALESCE(SUM(total_units),0) FROM order_carts WHERE order_id = %s),
                cart_count  = (SELECT COUNT(*) FROM order_carts WHERE order_id = %s),
                version     = version + 1
            WHERE id = %s
        """, (order_id, order_id, order_id))

    return {"assigned": True, "cart_id": cart_id, "cart_key": f"PC-{cart_num:04d}",
            "units": total_units, "shelves": positions}


@router.delete("/{template_id}")
def delete_cart(template_id: str, user=Depends(require_role("order_writer", "manager", "admin"))):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("DELETE FROM cart_template_shelves WHERE template_id = %s", (template_id,))
        cur.execute("DELETE FROM cart_templates WHERE id = %s RETURNING id", (template_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Permanent cart not found")
    return {"deleted": True}
