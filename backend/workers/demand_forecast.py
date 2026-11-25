"""Shipment-specific sales forecast and the marginal value of each pack (Jason's Week 49 agent
direction, 2026-11-25, section 4). Pure Python, no DB.

  r = stocked weekly unit pace for the store x SKU
  s = projected sellable stock at receipt (existing + qualified inbound, credited once)
  h = weeks after receipt (35 days = 5; D-store core plants / accessories 70 days = 10;
      H2O / Terrarium always 5)
  q = cumulative planned shipment units for the store x SKU (across all its carts)
  a = pace factor (base 0.90, slower 0.80)
  f = sellable fraction (plants base 0.83 = 17% loss / shelf allowance, slower 0.80;
      non-plant hard goods 1.00)
  p = units per approved tray / box

  F(q, h)          = min(q * f, max(0, h * a * r - s))      units sold within h weeks
  marginal(q, p)   = F(q + p, h) - F(q, h)                   what the next pack adds

F is linear in q until q * f reaches the sellable demand D = max(0, h*a*r - s) and flat after,
so the last pack worth sending is found in closed form (ceiling_packs).
"""
import math

BASE = {"a": 0.90, "f_plant": 0.83, "f_hard": 1.00}
SLOWER = {"a": 0.80, "f_plant": 0.80, "f_hard": 1.00}
HORIZON_WEEKS = 5          # 35 days after receipt
D_HORIZON_WEEKS = 10       # 70 days: D-store core plant / accessory replenishment


def sellable_demand(r, s, h, a):
    """D = max(0, h * a * r - s): units the store can sell in h weeks beyond what it holds."""
    return max(0.0, h * a * float(r or 0) - max(0.0, float(s or 0)))


def sold_units(q, r, s, h, a=BASE["a"], f=BASE["f_plant"]):
    """F(q, h) = min(q * f, max(0, h * a * r - s))."""
    return min(max(0.0, float(q)) * f, sellable_demand(r, s, h, a))


def marginal_units(q, p, r, s, h, a=BASE["a"], f=BASE["f_plant"]):
    """F(q + p, h) - F(q, h): projected units the next pack adds."""
    return sold_units(q + p, r, s, h, a, f) - sold_units(q, r, s, h, a, f)


def ceiling_packs(r, s, h, p, a=BASE["a"], f=BASE["f_plant"], min_share=0.5):
    """Most packs worth sending: every pack up to and including the last one whose marginal
    units are at least min_share of a pack's sellable units (p * f). Closed form of
    max k with D - (k - 1) * p * f >= min_share * p * f."""
    D = sellable_demand(r, s, h, a)
    unit = float(p) * f
    if unit <= 0 or D <= 0:
        return 0
    return max(0, int(math.floor(D / unit + 1.0 - min_share + 1e-9)))


def horizon_weeks(store_grade, family, d_weeks=D_HORIZON_WEEKS, h2o_terr=frozenset({"H2O", "Terrarium"})):
    """35 days for every grade; 70 for D-store core plants and accessories (never H2O/Terrarium)."""
    if str(store_grade or "").upper() == "D" and family not in h2o_terr:
        return d_weeks
    return HORIZON_WEEKS


def sellable_fraction(is_hard_good, case=BASE):
    return case["f_hard"] if is_hard_good else case["f_plant"]
