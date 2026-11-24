"""Inbound by arrival date (workers/inbound_timing.py; Jason's review point 3). Pure — no DB.
Run: python -m pytest tests/test_inbound_timing.py -q"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "workers"))
import inbound_timing as T  # noqa: E402

R = 10.0          # 10 units/day


def old_projected(on_hand, events, arrive_day):
    return max(0.0, on_hand + sum(u for _, u in events) - R * arrive_day)


def test_truck_before_stockout_is_same_as_before():
    # 50 on hand, truck of 40 on day 3, we land on day 7: never runs out
    s, later, lost = T.simulate(50, R, 7, [(3, 40)])
    assert (s, later, lost) == (20.0, [], 0.0)
    assert s == old_projected(50, [(3, 40)], 7)


def test_lost_sales_do_not_eat_a_later_truck():
    # 10 on hand runs out on day 1; truck of 100 on day 5; we land on day 7
    s, later, lost = T.simulate(10, R, 7, [(5, 100)])
    assert lost == 40.0 and s == 80.0          # days 1-5 lost; the truck sells 2 days to arrival
    assert old_projected(10, [(5, 100)], 7) == 40.0


def test_same_truck_before_vs_after_the_stockout_changes_the_order():
    cov_days, oh = 21, 60                      # 3-week coverage; 60 on hand lasts 6 days
    # truck of 150 two days after our arrival (day 9)
    s, later, _ = T.simulate(oh, R, 7, [(9, 150)])
    need_late = max(R * cov_days - (s + sum(u for _, u in later)), T.gap_need(s, later, R, cov_days))
    # the same truck before the stockout (day 4)
    s2, later2, _ = T.simulate(oh, R, 7, [(4, 150)])
    need_early = max(R * cov_days - (s2 + sum(u for _, u in later2)), T.gap_need(s2, later2, R, cov_days))
    assert need_early == 210 - 140            # 60 + 150 - 70 sold before arrival
    assert need_late == 210 - 150             # stock at arrival 0, the later truck still covers
    assert need_late != need_early


def test_gap_before_a_late_truck_is_covered_by_our_order():
    # we land with 0; a big truck lands 10 days later: the store needs 100 to get there
    assert T.gap_need(0, [(10, 500)], R, 21) == 100.0
    # a truck after the coverage window does not count
    assert T.gap_need(0, [(30, 500)], R, 21) == 0.0
