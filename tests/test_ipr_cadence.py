"""Unit tests for IPR v2 delivery cadence + shipping efficiency helpers (pure, no DB).

Run:  python -m pytest tests/test_ipr_cadence.py -q
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "workers"))
from ipr_engine import (DEFAULT_CADENCE_WEEKS as CAD, cadence_grade, coverage_weeks,  # noqa: E402
                        due_status, shipping_efficiency)

ARRIVAL = date(2026, 11, 25)


def test_cadence_grade_prefers_store_volume_grade():
    assert cadence_grade("B", "AA", CAD) == "B"


def test_cadence_grade_falls_back_to_best_family_grade_then_c():
    assert cadence_grade(None, "A", CAD) == "A"
    assert cadence_grade("", None, CAD) == "C"
    assert cadence_grade("ZZ", "nope", CAD) == "C"


def test_due_when_no_prior_delivery():
    assert due_status(None, ARRIVAL, 3) == (True, ARRIVAL)


def test_due_boundary_is_cadence_times_7_minus_3():
    # 2-week cadence: due when the gap to arrival is >= 11 days
    assert due_status(date(2026, 11, 14), ARRIVAL, 2)[0] is True       # 11 days
    assert due_status(date(2026, 11, 15), ARRIVAL, 2)[0] is False      # 10 days
    assert due_status(date(2026, 11, 15), ARRIVAL, 2)[1] == date(2026, 11, 26)


def test_weekly_store_is_due_after_four_days():
    assert due_status(date(2026, 11, 21), ARRIVAL, 1)[0] is True       # 4 days
    assert due_status(date(2026, 11, 22), ARRIVAL, 1)[0] is False      # 3 days


def test_critical_line_makes_store_due():
    assert due_status(date(2026, 11, 24), ARRIVAL, 4, any_critical=True)[0] is True


def test_coverage_is_cadence_plus_buffer_capped_by_freshness():
    assert coverage_weeks(3, 1, 90) == 4.0
    assert coverage_weeks(4, 1, 28) == 4.0          # 28-day freshness caps 5 weeks to 4
    assert coverage_weeks(1, 1, 90) == 2.0


def test_shipping_efficiency():
    assert shipping_efficiency(67, 100) == 0.67
    assert shipping_efficiency(5, 0) is None


# ── Josh option B: defer a light due order once, then ship regardless ────────
from ipr_engine import economic_action, prior_economic_deferrals  # noqa: E402


def test_prior_deferrals_skip_not_due_weeks_and_stop_at_a_shipped_cycle():
    assert prior_economic_deferrals([]) == 0
    assert prior_economic_deferrals([{"deferred_economics"}]) == 1
    assert prior_economic_deferrals([{"not_due"}, {"deferred_economics"}, {"review"}]) == 1
    assert prior_economic_deferrals([{"review"}, {"deferred_economics"}]) == 0
    assert prior_economic_deferrals([set(), {"deferred_economics"}]) == 0


def test_economic_action_passes_full_carts():
    assert economic_action(1800, 1, 0, 240, 1700, False, 0, 1) == "pass"


def test_economic_action_releases_on_lost_sales_or_critical():
    assert economic_action(500, 1, 240, 240, 1700, False, 0, 1) == "release"
    assert economic_action(500, 2, 300, 240, 1700, True, 0, 1) == "release"


def test_light_due_order_is_deferred_once_then_ships():
    assert economic_action(500, 1, 50, 240, 1700, False, 0, 1) == "defer"
    assert economic_action(500, 1, 50, 240, 1700, False, 1, 1) == "release_after_deferral"
    assert economic_action(500, 1, 50, 240, 1700, False, 0, 0) == "release_after_deferral"


# ── §8 shrink + week-1 deferral seed ─────────────────────────────────────────
from ipr_engine import is_overdue, shrink_rate  # noqa: E402


def test_shrink_rate_formula_clamp_and_fallback():
    assert shrink_rate(1000, 700, onhand_change=100) == (0.2, "sold + Δ DTS on-hand vs delivered")
    assert shrink_rate(1000, 100, onhand_change=0)[0] == 0.6                  # clamped
    assert shrink_rate(1000, 1200, onhand_change=0)[0] == 0.0                 # clamped
    assert shrink_rate(1000, 700, onhand_change=None, efficiency=0.75)[0] == 0.25
    assert shrink_rate(0, 0) == (None, "no data")


def test_overdue_store_is_released_not_deferred():
    arrival = date(2026, 11, 25)
    assert is_overdue(date(2026, 11, 3), arrival, 2) is True                 # 22 days > 21
    assert is_overdue(date(2026, 11, 4), arrival, 2) is False                # 21 days
    assert is_overdue(None, arrival, 1) is True
    assert economic_action(500, 1, 50, 240, 1700, False, 0, 1, overdue=True) == "release_overdue"
    assert economic_action(500, 1, 50, 240, 1700, False, 0, 1, overdue=False) == "defer"
