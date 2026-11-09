"""
Packing catalog — physical tray/cart data that drives the ShelfKit-style
shelf/cart builder in the Order Editor. This is the single source of truth the
frontend geometry engine reads (tray footprints in mm, cart shelf dims in inches,
clear heights, compatibility rules, and the SKU->tray-profile map).

All dimensions are served as-is: tray width/depth in MM, cart dims in INCHES,
clear heights in INCHES — the frontend normalizes (ShelfKit is mm-native).
"""
from fastapi import APIRouter, Depends
from db import get_db, cursor
from routers.auth import get_current_user

router = APIRouter()


@router.get("/catalog")
def packing_catalog(user=Depends(get_current_user)):
    """Everything the shelf/cart builder needs to lay out trays and stack shelves."""
    with get_db() as conn:
        cur = cursor(conn)

        # Tray types (footprint mm, units/tray, clear height in)
        cur.execute("""
            SELECT id, name, short_name,
                   tray_width_mm, tray_depth_mm, units_per_tray,
                   clear_height_in, height_class, orientation, tray_color, active
            FROM packing_profiles
            ORDER BY id
        """)
        trays = cur.fetchall()

        # Cart/shipping units (shelf WxD inches, heights, caps)
        cur.execute("""
            SELECT id, code, name,
                   shelf_width_in, shelf_depth_in,
                   od_width_in, od_depth_in,
                   total_height_in, base_height_in, ceiling_in,
                   max_shelves, top_shelf_max_in, active
            FROM cart_types
            ORDER BY id
        """)
        carts = cur.fetchall()

        # Approved packing counts per (cart_type, profile): trays/shelf, units/shelf, max shelves
        cur.execute("""
            SELECT cart_type_id, profile_id, trays_per_shelf, units_per_shelf,
                   max_shelves, units_per_cart
            FROM shelf_configurations
            WHERE approved = true
        """)
        shelf_configs = cur.fetchall()

        # Which trays may share a shelf/cart
        cur.execute("SELECT * FROM packing_compat_rules")
        compat = cur.fetchall()

        # SKU -> default tray profile (so a chosen SKU resolves to a footprint)
        cur.execute("""
            SELECT spp.sku_id, spp.profile_id, spp.is_default, k.description, k.family
            FROM sku_packing_profiles spp
            JOIN skus k ON k.sku_id = spp.sku_id
            WHERE spp.is_default = true
        """)
        sku_profiles = cur.fetchall()

    return {
        "trays": trays,
        "carts": carts,
        "shelf_configs": shelf_configs,
        "compat_rules": compat,
        "sku_profiles": sku_profiles,
    }
