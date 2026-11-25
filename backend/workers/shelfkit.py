"""ShelfKit v3.68 physical model for a CC cart (pack team guide, Sarah & Christa 2026-11-25).
One definition shared by the engine's cart packer and the pack-plan check list, so the engine
builds carts the pack team's checks pass. Pure Python, no DB.

CC cart: shelf 50 1/2 x 22 1/4 in, a load may overhang 1/4 in each edge (51 in across), a
leftover strip under 3 in counts as filled. Shelf rungs add to <= 95 in, at most 5 shelves,
at most 4 on any cart carrying 17cm. Shelf height = the tallest item's rung.
"""

CC_SHELF_IN = 50.5
CC_FIT_IN = 51.0
FILLED_STRIP_IN = 3.0
MIN_SHELF_FILL = 0.80
CC_MAX_SHELVES = 5
CC_MAX_HEIGHT_IN = 95
CC_17CM_MAX_SHELVES = 4
TALL_17CM_PROFILES = frozenset({"box17"})

# profile -> (footprint class, inches across per tray/box, shelf rung in)
#   std  = standard 15 3/4 x 11 tray two deep: a pair is 15.75 across, an odd tray 11
#   hb   = 12cm hanging basket 16 x 11 two deep, pair 16 (odd std + odd HB pair at 16)
#   each = fixed inches across per tray/box
PROFILE_FIT = {
    "littles": ("each", 7.09, 15), "cer9": ("std", None, 15), "canopy9": ("std", None, 15),
    "bowlEU": ("std", None, 15), "h2opp": ("std", None, 15),
    "terrEU": ("each", CC_SHELF_IN / 5, 15),          # small terrarium: rung 15, tray count open ruling
    "euro12": ("std", None, 21), "climb": ("std", None, 21), "globe3": ("std", None, 21),
    "hb12": ("hb", None, 21), "venti": ("each", 12.6, 21),
    "box17": ("each", 7.125, 29), "db12": ("std", None, 29),
    "box8x2": ("each", 9.0, 15), "soilbag1": ("each", 8.25, 15), "soilbag2": ("each", 8.25, 15),
    "spraybox": ("each", 10.1, 15),
    # Climb-itt hardware boxes (max per CC shelf: Pole 30 2, Pole 19 4, Stakes 7, Clips/Wire 4)
    "acc_pole30": ("each", CC_SHELF_IN / 2, 29), "acc_pole19": ("each", CC_SHELF_IN / 4, 21),
    "acc_stakes": ("each", CC_SHELF_IN / 7, 15), "acc_clips": ("each", CC_SHELF_IN / 4, 15),
    "acc_wire": ("each", CC_SHELF_IN / 4, 15),
    "prebuilt_cart": ("each", CC_SHELF_IN, 90),
}
DEFAULT_FIT = ("std", None, 21)


def shelf_width(items):
    """Inches across one CC shelf for [(profile_id, trays)], ShelfKit width maths."""
    n_std = n_hb = 0
    w = 0.0
    for pid, trays in items:
        kind, per, _ = PROFILE_FIT.get(pid, DEFAULT_FIT)
        if kind == "std":
            n_std += trays
        elif kind == "hb":
            n_hb += trays
        else:
            w += per * trays
    w += (n_std // 2) * 15.75 + (n_hb // 2) * 16.0
    if n_std % 2 and n_hb % 2:
        w += 16.0
    else:
        w += (n_std % 2) * 11.0 + (n_hb % 2) * 11.0
    return round(w, 2)


def fits_shelf(items):
    return shelf_width(items) <= CC_FIT_IN + 1e-9


def shelf_fill(items):
    """Share of the shelf used (0..1); a leftover strip under 3 in counts as filled."""
    w = shelf_width(items)
    if w <= 0:
        return 0.0
    if CC_SHELF_IN - w < FILLED_STRIP_IN:
        return 1.0
    return min(1.0, w / CC_SHELF_IN)


def shelf_rung(pids, default=21):
    """Shelf height = the tallest item's rung."""
    return max((PROFILE_FIT.get(p, (None, None, default))[2] for p in pids), default=0)


def max_shelves(pids):
    """Shelf cap for a cart holding these profiles: 4 with any 17cm, else 5."""
    return CC_17CM_MAX_SHELVES if TALL_17CM_PROFILES & set(pids) else CC_MAX_SHELVES
