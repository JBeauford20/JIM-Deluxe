"""§29 shipment-response classification boundaries."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from services.post_delivery import classify  # noqa: E402


def test_too_recent_wins_regardless_of_numbers():
    assert classify(2.0, 0.9, matured=False) == "too_recent"


def test_strong_on_lift_or_sell_through():
    assert classify(1.25, 0.1, True) == "strong"          # lift threshold inclusive
    assert classify(0.5, 0.60, True) == "strong"          # 60% sold = leadership target
    assert classify(1.24, 0.59, True) == "expected"


def test_weak_needs_both_low_lift_and_low_sell_through():
    assert classify(0.80, 0.39, True) == "weak"
    assert classify(0.80, 0.45, True) == "expected"       # sold enough despite low lift
    assert classify(0.9, 0.1, True) == "expected"         # lift not low enough


def test_no_baseline_judged_on_sell_through_alone():
    assert classify(None, 0.2, True) == "no_baseline"
    assert classify(None, 0.65, True) == "strong"
