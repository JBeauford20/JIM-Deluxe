"""Demand by selling opportunity (workers/demand_history.py, CEO review 2026-11-24).
Pure — no DB. Run: python -m pytest tests/test_demand_history.py -q"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend", "workers"))
import demand_history as D  # noqa: E402

ALL = set(range(80))          # store reported every week


def test_weeks_before_first_delivery_are_not_selling_weeks():
    # delivered 5 weeks ago, sold 2/wk since: 13-wk rate is 2/wk, not 10/13
    sold = {w: 2 for w in range(0, 5)}
    av = D.weekly_availability(sold, {4: 40}, ALL, fresh_weeks=12, n_weeks=20)
    rate, n = D.window_rate(sold, av, range(0, 13))
    assert n == 5 and rate == 2
    # a launch week alone is never the rate: at least 4 weeks in the divisor
    av1 = D.weekly_availability({0: 16}, {0: 24}, ALL, fresh_weeks=12, n_weeks=13)
    assert D.window_rate({0: 16}, av1, range(0, 13)) == (4.0, 1)


def test_zero_week_counts_as_demand_when_earlier_stock_was_on_hand():
    # one delivery 6 weeks ago, no delivery since; zero-sale weeks with stock still count
    sold = {5: 3, 3: 3}
    av = D.weekly_availability(sold, {5: 30}, ALL, fresh_weeks=12, n_weeks=10)
    assert all(av[w] for w in range(0, 6))
    assert D.window_rate(sold, av, range(0, 6)) == (1.0, 6)


def test_sold_out_weeks_are_not_selling_weeks():
    sold = {3: 6}                              # 6 delivered, 6 sold, nothing left
    av = D.weekly_availability(sold, {3: 6}, ALL, fresh_weeks=12, n_weeks=6)
    assert av[3] and not any(av[w] for w in (0, 1, 2))


def test_stock_past_freshness_is_not_on_hand():
    av = D.weekly_availability({}, {10: 50}, ALL, fresh_weeks=4, n_weeks=12)
    assert [w for w in range(12) if av[w]] == [7, 8, 9, 10]


def test_store_not_reporting_is_not_a_selling_week():
    av = D.weekly_availability({}, {3: 50}, ALL - {1}, fresh_weeks=12, n_weeks=5)
    assert av[3] and av[2] and not av[1] and av[0]


def test_typical_week_caps_one_spike():
    assert D.typical_week([4, 4, 4, 40]) == 4
    assert D.typical_week([2, 4]) == 3
    assert D.typical_week([]) is None


def test_mothers_day_and_benchmark_periods():
    assert D.mothers_day(2026) == date(2026, 5, 10)
    anchor = date(2026, 11, 22)
    labels = [p[0] for p in D.benchmark_periods(anchor, 12)]
    assert labels[0] == "Jun-Jul 2026" and labels[2].startswith("Apr-May 2026")
    am = D.benchmark_periods(anchor, 4)[0]
    assert am[0].startswith("Apr-May") and D.week_back(anchor, date(2026, 5, 10)) not in am[1]


def test_short_primary_period_falls_back_scaled_to_primary_level():
    p = [("JJ", [0, 1, 2, 3]), ("AS", [4, 5, 6, 7])]
    full = [True] * 8
    hist = {
        ("a", 1): ({0: 4, 1: 4, 2: 4, 3: 4, 4: 8, 5: 8, 6: 8, 7: 8}, full),       # sets the index 0.5
        ("b", 1): ({4: 6, 5: 6, 6: 6, 7: 6}, [False] * 4 + [True] * 4),          # JJ out of stock
    }
    out, idx = D.benchmarks(hist, p, min_weeks=4, min_share=0.5)
    assert idx == [1.0, 0.5]
    assert out[("a", 1)][0] == 4
    assert out[("b", 1)][0] == 3 and "x 0.50" in out[("b", 1)][1]


def test_comparables_need_enough_stores():
    rates = {(s, 9): float(s) for s in range(1, 6)}
    grp = {s: ("A", "South") for s in range(1, 7)}
    by_sku, by_fam = D.comparable_rates(rates, grp, {9: "Bowls"}, min_stores=5)
    assert by_sku[(("A", "South"), 9)] == 3.0 and by_fam[(("A", "South"), "Bowls")] == 3.0
    assert D.comparable_rates(rates, grp, {9: "Bowls"}, min_stores=6) == ({}, {})


def test_growth_split_separates_added_coverage():
    g = D.growth_split({(1, 1): 110, (2, 1): 50}, {(1, 1): 100, (3, 1): 20})
    assert g["same_line_growth_pct"] == 10.0
    assert g["units_from_added_lines"] == 50 and g["units_from_lost_lines"] == 20
    assert g["total_change_pct"] == round(100 * (160 / 120 - 1), 1)


def test_no_delivery_record_counts_weeks_from_first_sale():
    # no delivery on record at all (feed gap / SKU mismatch): sells 1 every 3rd week
    sold = {0: 1, 3: 1, 6: 1, 9: 1, 12: 1}
    av = D.weekly_availability(sold, {}, ALL, fresh_weeks=8, n_weeks=20)
    assert all(av[w] for w in range(0, 13)) and not any(av[w] for w in range(13, 20))
    assert D.window_rate(sold, av, range(0, 13)) == (5 / 13, 13)


def test_delivery_breaks_the_later_sale_evidence():
    # weeks before a delivery are not proven by sales after it
    av = D.weekly_availability({0: 5}, {1: 10}, ALL, fresh_weeks=8, n_weeks=4)
    assert av[0] and av[1] and not av[2] and not av[3]


def test_selling_past_the_delivery_record_means_stock_unknown_not_sold_out():
    # sold 11 with nothing delivered on record, then zeros: those zeros are demand, not sell-out
    sold = {12: 11}
    av = D.weekly_availability(sold, {}, ALL, fresh_weeks=8, n_weeks=13)
    assert all(av[w] for w in range(0, 13))
    # a real sell-out on a consistent record still drops the zero weeks
    av2 = D.weekly_availability({12: 24}, {12: 24}, ALL, fresh_weeks=8, n_weeks=13)
    assert av2[12] and not any(av2[w] for w in range(0, 12))


def test_picked_over_shelf_is_not_reasonably_available():
    # 100 delivered (60 sellable); sells 20, 20, 15, then 4 and 1 off the dregs
    sold = {6: 20, 5: 20, 4: 15, 3: 4, 2: 1}
    av = D.weekly_availability(sold, {6: 100}, ALL, fresh_weeks=10, n_weeks=8,
                               sellable_share=0.6, min_stock=18)
    assert av[6] and av[5] and av[4] and not av[3] and not av[2] and not av[1]
    assert D.window_rate(sold, av, range(0, 7)) == (55 / 4, 3)


CURVE = [(0, 0.60), (28, 0.25), (42, 0.14), (56, 0.07), (63, 0.05), (70, 0.0)]


def test_aging_stock_stops_counting_on_the_lot_age_curve():
    # 400 delivered 9 weeks ago, sold slowly: plenty physically left, but once the lot is old the
    # curve leaves less than one normal week (40u) sellable by 8 weeks — those weeks are not demand
    sold = {9: 40, 8: 40, 7: 30, 6: 20, 5: 10, 4: 5, 3: 3, 2: 2, 1: 1}
    av = D.weekly_availability(sold, {9: 400}, ALL, fresh_weeks=10, n_weeks=10, sellable_share=0.6,
                               min_stock=40, curve=CURVE)
    assert [w for w in range(10) if av[w]] == [2, 3, 4, 5, 6, 7, 8, 9]   # 49 days: 42u left
    assert D.curve_share(CURVE, 35) == 0.25 + (0.14 - 0.25) * 7 / 14


def test_selling_more_than_the_model_holds_is_a_real_selling_week():
    # a new store sells 13 with no delivery on record: that week counts (stock was there)
    av = D.weekly_availability({0: 13}, {}, ALL, fresh_weeks=10, n_weeks=4, sellable_share=0.6,
                               min_stock=5, curve=CURVE)
    assert av[0] and not av[1]
