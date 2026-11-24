"""Inbound counted by WHEN it arrives (Jason's review point 3; Josh 2026-11-24: day-by-day).

The engine used to add every truck arriving after the sales cutoff straight onto the shelf:
projected = on hand + all inbound - sales before our arrival. That lets a truck arriving after
the store has already run out "pay back" sales that were lost, and lets a truck arriving after
our delivery cover days it never reaches. Here stock is walked forward one event at a time:

  1. before our arrival: sell at the forecast rate; a truck adds stock on its own day; while the
     shelf is empty, sales are lost (they never consume a later truck);
  2. after our arrival: our order must also keep the store from running out before each later
     truck lands (gap_need), not just cover the total over the window.
"""


def simulate(on_hand, daily, arrive_day, events):
    """on_hand: sellable units at the sales anchor; daily: forecast units/day; arrive_day: days
    from the anchor to our delivery; events: [(day_from_anchor, units)] of other inbound.
    Returns (stock_at_arrival, later, lost_units) where later = [(days_after_arrival, units)]
    for inbound landing after our delivery."""
    s, t, lost = max(0.0, float(on_hand)), 0.0, 0.0
    later = []
    for day, units in sorted(events):
        if units <= 0:
            continue
        if day > arrive_day:
            later.append((day - arrive_day, float(units)))
            continue
        d = max(0.0, day - t)
        sold = min(s, daily * d)
        lost += daily * d - sold
        s, t = s - sold + float(units), max(t, day)
    d = max(0.0, arrive_day - t)
    sold = min(s, daily * d)
    lost += daily * d - sold
    return s - sold, later, lost


def gap_need(stock_at_arrival, later, daily, horizon_days):
    """Units our delivery must add so the store does not run out before each later truck that
    lands inside the coverage window."""
    need, cum = 0.0, float(stock_at_arrival)
    for d, u in sorted(later):
        if d > horizon_days:
            break
        need = max(need, daily * d - cum)
        cum += u
    return max(0.0, need)
