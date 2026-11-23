"""
Pre-built carts as a pseudo product (additive, idempotent). 2026-11-23.

A pre-built cart (first one: the Climb-itt Bundle of hard goods) is a whole cart that is
already built and is not made of availability lines. To send one through the order writer it
is given a pseudo SKU that can be listed on the availability file with the number on hand:

    SKU 1099000001   "Climb-itt Bundle"   family 'Prebuilt Cart'

  packing_profiles      'prebuilt_cart'   1 per tray, 90 in tall (nothing else fits on the cart)
  shelf_configurations  cc / prebuilt_cart: 1 tray per shelf, 1 shelf per cart
  skus                  the pseudo SKU (no price: it is counted in units, not dollars)
  sku_packing_profiles  the pseudo SKU -> prebuilt_cart
  ipr_family_map        the pseudo SKU -> 'Prebuilt Cart' (approved)

The engine never orders it on its own (it has no sales). It ships only through an approved
directed shipment ("send 1 Climb-itt Bundle to stores ..."), one whole cart per store, limited
by the quantity on the availability line. To add another pre-built cart later, add a row to
BUNDLES and re-run.

Reads SUPABASE_DB_URL from env. Safe to re-run.
"""
import os

import psycopg2

BUNDLES = [(1099000001, "Climb-itt Bundle")]


def main():
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"], connect_timeout=25)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO packing_profiles (id, name, short_name, tray_width_mm, tray_depth_mm, units_per_tray,
                                      clear_height_in, orientation, height_class, notes, active)
        VALUES ('prebuilt_cart', 'Pre-built cart (whole cart)', 'Pre-built', 1300, 565, 1, 90, 'horizontal',
                'T', 'One unit = one whole pre-built cart. 90 in tall so nothing else shares the cart.', true)
        ON CONFLICT (id) DO NOTHING""")
    cur.execute("""
        INSERT INTO shelf_configurations (cart_type_id, profile_id, trays_per_shelf, units_per_shelf,
                                          max_shelves, units_per_cart, approved, notes)
        VALUES ('cc', 'prebuilt_cart', 1, 1, 1, 1, true, 'A pre-built cart is the whole cart.')
        ON CONFLICT (cart_type_id, profile_id) DO NOTHING""")
    for sku_id, name in BUNDLES:
        cur.execute("""
            INSERT INTO skus (sku_id, description, family, legacy_product_group, units_per_tray, active, is_holiday)
            VALUES (%s, %s, 'Prebuilt Cart', 'Premium', 1, true, false)
            ON CONFLICT (sku_id) DO NOTHING""", (sku_id, name))
        cur.execute("""
            INSERT INTO sku_packing_profiles (sku_id, profile_id, is_default)
            VALUES (%s, 'prebuilt_cart', true) ON CONFLICT (sku_id, profile_id) DO NOTHING""", (sku_id,))
        cur.execute("""
            INSERT INTO ipr_family_map (sku_id, ipr_family, approved, source)
            VALUES (%s, 'Prebuilt Cart', true, 'pre-built cart pseudo SKU - Josh 2026-11-23')
            ON CONFLICT (sku_id) DO NOTHING""", (sku_id,))
    conn.commit()
    cur.execute("""SELECT k.sku_id, k.description, k.family, fm.ipr_family, spp.profile_id, sc.trays_per_shelf, pp.units_per_tray
                   FROM skus k JOIN ipr_family_map fm ON fm.sku_id = k.sku_id
                   JOIN sku_packing_profiles spp ON spp.sku_id = k.sku_id
                   JOIN packing_profiles pp ON pp.id = spp.profile_id
                   JOIN shelf_configurations sc ON sc.profile_id = spp.profile_id AND sc.cart_type_id = 'cc'
                   WHERE k.family = 'Prebuilt Cart' ORDER BY 1""")
    for r in cur.fetchall():
        print("  pre-built cart ready:", r)
    conn.close()


if __name__ == "__main__":
    main()
