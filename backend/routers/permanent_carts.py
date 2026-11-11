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


@router.delete("/{template_id}")
def delete_cart(template_id: str, user=Depends(require_role("order_writer", "manager", "admin"))):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("DELETE FROM cart_template_shelves WHERE template_id = %s", (template_id,))
        cur.execute("DELETE FROM cart_templates WHERE id = %s RETURNING id", (template_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Permanent cart not found")
    return {"deleted": True}
