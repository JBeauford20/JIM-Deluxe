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
