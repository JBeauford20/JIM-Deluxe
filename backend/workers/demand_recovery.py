"""Store x SKU weekly demand and stock-at-receipt estimator (Jason's Week 49 demand method;
'Week_49_Demand_Rate_Proof_Store_628.xlsx', 2026-11-25). Pure Python, no DB.

A hierarchy of historical evidence, not an average of the last N weeks:

  W  wider recent screened pace (about the last 9 weeks)
  N  latest-month screened pace (about the last 3 weeks)
  summer  clean summer benchmark (Jul 1 - Sep 22), screened
  success typical pace of successful exact store/SKU delivery waves (68% sold within 34 days)
  dec     prior-December screened pace (winter anchor), used when no summer / success anchor

  hist      = 0.6 summer + 0.4 success when both exist, else whichever exists, else dec
  recent    = 0.55 N + 0.45 W
  light     = receipts in the last 4 weeks < max(one pack, 2 weeks of hist)    (supply-light)
  baseline  = light ? 0.2 recent + 0.8 hist : 0.55 recent + 0.45 hist   (= recent without hist)
  strongest = max of the qualified paces (W, N, hist anchors)
  recovery  = AA/A/B: 0.9 strongest + 0.1 baseline ; C/D: baseline
  two_week  = max(0.6 x last week, mean of the last two weeks)
  winter    = dec x (1 - j/35 + j/35 x clip(jan/dec, 0.6, 1.5)), j = January days in the 35
              days after receipt; overlay = 0.7 winter + 0.3 N (only when the window reaches Dec/Jan)
  selected  = max(recovery, two_week, overlay)
  final     = max(selected, exact successful-delivery pace)

  stock at receipt = receipts of the last 4 weeks (each credited once, plus any later inbound
  before receipt), depleted day by day at the baseline pace until the new shipment lands.

Price screen: a window's pace counts only when its average selling price is at least 80% of
the reference price; it is scaled by min(1, ASP / reference). A window without dollars is not
screened (Jason 11/25 flagged the historical screen for re-validation against prices effective
at the time). The weights are his starting assumptions, all overridable, pending back-testing.
"""
from datetime import date, timedelta

W = dict(recent_n=0.55, hist_summer=0.6, light_recent=0.2, adequate_recent=0.55,
         recovery_strong=0.9, overlay_winter=0.7, two_week_last=0.6, price_min=0.8,
         jan_clip=(0.6, 1.5), window_days=35)
RECOVERY_GRADES = frozenset({"AA", "A", "B"})


def screened_pace(units, days, dollars=None, ref_price=None, price_min=W["price_min"], has_supply=True):
    """Weekly pace for one window, price-screened. None when the window is not qualified."""
    if not units or units <= 0 or not days or days <= 0 or not has_supply:
        return None
    pace = units * 7.0 / days
    if dollars and ref_price:
        asp = dollars / units
        if asp < price_min * ref_price:
            return None
        pace *= min(1.0, asp / ref_price)
    return pace


def historical_anchor(summer=None, success=None, dec=None, w_summer=W["hist_summer"]):
    if summer is not None and success is not None:
        return w_summer * summer + (1 - w_summer) * success
    if summer is not None:
        return summer
    if success is not None:
        return success
    return dec


def winter_overlay(dec, jan, n_pace, receipt, window_days=W["window_days"], w=W):
    """(winter anchor, overlay) when the selling window after receipt reaches Dec/Jan; else (None, None)."""
    if dec is None or receipt is None:
        return None, None
    end = receipt + timedelta(days=window_days)
    if receipt.month not in (12, 1) and end.month not in (12, 1):
        return None, None
    jan_start = date(receipt.year + (1 if receipt.month == 12 else 0), 1, 1)
    j = max(0, min((end - jan_start).days, window_days)) if end >= jan_start else 0
    ratio = (jan / dec) if (jan is not None and dec) else 1.0
    lo, hi = w["jan_clip"]
    winter = dec * (1 - j / window_days + j / window_days * min(hi, max(lo, ratio)))
    overlay = w["overlay_winter"] * winter + (1 - w["overlay_winter"]) * (n_pace or 0.0)
    return winter, overlay


def estimate_rate(grade, w_pace=None, n_pace=None, summer=None, success=None, dec=None, jan=None,
                  receipts_4wk=0.0, pack=1, last_week=None, prev_week=None, receipt=None,
                  exact_success_pace=None, w=W):
    """Weekly demand with a full trace (dict). rate is None when there is no evidence at all."""
    hist = historical_anchor(summer, success, dec, w["hist_summer"])
    if w_pace is None and n_pace is None:
        recent = None
    else:
        recent = (w["recent_n"] * (n_pace if n_pace is not None else w_pace)
                  + (1 - w["recent_n"]) * (w_pace if w_pace is not None else n_pace))
    light = None
    if hist is not None and recent is not None:
        light = float(receipts_4wk or 0) < max(float(pack), 2.0 * hist)
        a = w["light_recent"] if light else w["adequate_recent"]
        baseline = a * recent + (1 - a) * hist
    else:
        baseline = recent if recent is not None else hist
    if baseline is None:
        return {"rate": exact_success_pace, "baseline": None, "source": "exact successful delivery only"
                if exact_success_pace else "no evidence"}
    quals = [x for x in (w_pace, n_pace, summer, success, hist if (summer is None and success is None) else None)
             if x is not None]
    strongest = max(quals) if quals else baseline
    recovery = (w["recovery_strong"] * strongest + (1 - w["recovery_strong"]) * baseline
                if str(grade or "").upper() in RECOVERY_GRADES else baseline)
    two_week = None
    if last_week is not None:
        two_week = max(w["two_week_last"] * last_week,
                       (last_week + (prev_week if prev_week is not None else last_week)) / 2.0)
    winter, overlay = winter_overlay(dec, jan, n_pace, receipt, w["window_days"], w)
    selected = max(x for x in (recovery, two_week, overlay) if x is not None)
    final = max(selected, exact_success_pace or 0.0)
    src = ("exact successful delivery pace" if exact_success_pace and exact_success_pace > selected else
           "recovery (strongest qualified pace)" if selected == recovery and recovery != baseline else
           "two-week replenishment pace" if two_week is not None and selected == two_week else
           "winter overlay" if overlay is not None and selected == overlay else "restored baseline")
    return {"rate": final, "baseline": baseline, "recent": recent, "hist": hist, "light": light,
            "strongest": strongest, "recovery": recovery, "two_week": two_week, "winter": winter,
            "overlay": overlay, "selected": selected, "source": src}


def stock_at_receipt(receipts, baseline_pace, receipt_day):
    """Credited sellable stock when the new shipment lands. receipts: [(day, units)] on one
    day axis (e.g. days after the sales anchor; negative = already received), each credited
    once; stock before the first receipt is taken as sold through. Depletes at baseline/7 a day."""
    rs = sorted((d, float(u)) for d, u in receipts if u and d <= receipt_day)
    if not rs:
        return 0.0
    per_day = max(0.0, float(baseline_pace or 0)) / 7.0
    stock, day = 0.0, rs[0][0]
    for d, u in rs:
        stock = max(0.0, stock - (d - day) * per_day) + u
        day = d
    return max(0.0, stock - (receipt_day - day) * per_day)


# ── DB loader (read-only) ────────────────────────────────────────────────────

def _windows(anchor):
    """Calendar windows relative to the sales anchor."""
    a = anchor
    ys = a.year if a > date(a.year, 9, 22) else a.year - 1
    yd = a.year - 1 if a < date(a.year, 12, 31) else a.year
    return {"w": (a - timedelta(days=63), a), "n": (a - timedelta(days=21), a),
            "wk0": (a - timedelta(days=7), a), "wk1": (a - timedelta(days=14), a - timedelta(days=7)),
            "summer": (date(ys, 6, 30), date(ys, 9, 22)), "dec": (date(yd, 11, 30), date(yd, 12, 31)),
            "jan": (date(yd + 1, 1, 1) - timedelta(days=1), date(yd + 1, 1, 14))}


def load_inputs(cur, anchor, receipt_lag_days, dedup_deliveries_sql):
    """{(store, sku): {window: (units, dollars, units_with_dollars)}, '_receipts': [(day, units)]}.
    Windows are (start exclusive, end inclusive]. Receipt day = delivery date + lag - anchor."""
    win = _windows(anchor)
    cols, params = [], []
    for k, (s, e) in win.items():
        cols.append(f"SUM(units_sold) FILTER (WHERE sale_date > %s AND sale_date <= %s) AS {k}_u")
        cols.append(f"SUM(dollars) FILTER (WHERE sale_date > %s AND sale_date <= %s AND dollars IS NOT NULL) AS {k}_d")
        cols.append(f"SUM(units_sold) FILTER (WHERE sale_date > %s AND sale_date <= %s AND dollars IS NOT NULL) AS {k}_du")
        params += [s, e, s, e, s, e]
    lo = min(s for s, _ in win.values())
    cur.execute(f"SELECT store_id, sku_id, {', '.join(cols)} FROM sales WHERE sale_date > %s AND sale_date <= %s "
                f"GROUP BY 1, 2", (*params, lo, anchor))
    out = {}
    for r in cur.fetchall():
        r = dict(r)
        out[(r["store_id"], r["sku_id"])] = {k: (float(r[f"{k}_u"] or 0), float(r[f"{k}_d"] or 0),
                                                 float(r[f"{k}_du"] or 0)) for k in win}
    cur.execute(f"""SELECT store_id, sku_id, (delivery_date + %s - %s::date) AS d, SUM(units_delivered) AS u
                    FROM {dedup_deliveries_sql} dl
                    WHERE delivery_date + %s > %s::date - 28 GROUP BY 1, 2, 3""",
                (int(receipt_lag_days), anchor, int(receipt_lag_days), anchor))
    for r in cur.fetchall():
        r = dict(r)
        if r["u"] and float(r["u"]) > 0:
            out.setdefault((r["store_id"], r["sku_id"]), {}).setdefault("_receipts", []).append(
                (int(r["d"]), float(r["u"])))
    return out, win


def line_estimate(inp, win, grade, pack, ref_price, receipt_day, receipt_date, exact_success_pace=None,
                  success=None, w=W):
    """Rate + stock at receipt for one store x SKU from load_inputs() data. None without evidence."""
    if not inp:
        return None

    def pace(k):
        u, d, du = inp.get(k, (0, 0, 0))
        days = (win[k][1] - win[k][0]).days
        asp_d = d if du and du >= 0.8 * u else None          # screen only when dollars cover the window
        return screened_pace(u, days, (asp_d / du * u) if asp_d else None, ref_price if asp_d else None,
                             w["price_min"])

    rec = inp.get("_receipts", [])
    r4 = sum(u for d, u in rec if -28 < d <= 0)
    wk0 = inp.get("wk0", (0, 0, 0))[0]
    wk1 = inp.get("wk1", (0, 0, 0))[0]
    t = estimate_rate(grade, w_pace=pace("w"), n_pace=pace("n"), summer=pace("summer"), success=success,
                      dec=pace("dec"), jan=pace("jan"), receipts_4wk=r4, pack=pack,
                      last_week=wk0 if (wk0 or wk1) else None, prev_week=wk1 if (wk0 or wk1) else None,
                      receipt=receipt_date, exact_success_pace=exact_success_pace, w=w)
    if t.get("rate") is None:
        return None
    t["stock"] = stock_at_receipt(rec, t.get("baseline") or t["rate"], receipt_day)
    t["receipts_4wk"] = r4
    return t
