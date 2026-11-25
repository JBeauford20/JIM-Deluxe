"""Jason's demand method (Week_49_Demand_Rate_Proof_Store_628.xlsx). No DB.
Run: python -m pytest tests/test_demand_recovery.py -q"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "workers"))
import demand_recovery as DR  # noqa: E402


def test_store_628_rate_reproduces_proof_exactly():
    w = DR.screened_pace(394, 61, 1952.8, 5.35)            # wider recent, price-screened
    n = DR.screened_pace(115, 22, 614.15, 5.35)
    dec = DR.screened_pace(63, 31, 302.4, 5.35)
    assert w == pytest.approx(41.886318369848325)
    assert n == pytest.approx(36.52548853016143)
    assert dec == pytest.approx(12.76334036780223)
    assert DR.screened_pace(625, 84, 2668.2, 5.35) is None  # Jul-Sep failed the 80% price screen
    t = DR.estimate_rate("AA", w_pace=w, n_pace=n, dec=dec, jan=9.5 * 12.76334036780223 / 14.2258,
                         receipts_4wk=168, pack=12, last_week=27, prev_week=49, receipt=date(2026, 12, 10))
    assert t["light"] is False
    assert t["baseline"] == pytest.approx(27.1593272424223)
    assert t["strongest"] == pytest.approx(41.886318369848325)
    assert t["two_week"] == pytest.approx(38)
    assert t["rate"] == pytest.approx(40.41361925710572)


def test_store_628_stock_credit_at_receipt():
    # Nov 7 receipt 168, Nov 28 receipt 12, new shipment lands Dec 10; deplete at the baseline
    s = DR.stock_at_receipt([(0, 168), (21, 12)], 27.1593272424223, 33)
    assert s == pytest.approx(51.96317157143774)


def test_store_1601_older_stocked_periods_lead_when_supply_light():
    t = DR.estimate_rate("B", w_pace=1.147540984, n_pace=0.318181818, summer=8.666666667,
                         success=17.575384615, receipts_4wk=0, pack=8, exact_success_pace=17.575384615)
    assert t["light"] is True
    assert t["baseline"] == pytest.approx(9.922401765, abs=1e-6)
    assert t["recovery"] == pytest.approx(16.81008633, abs=1e-6)
    assert t["rate"] == pytest.approx(17.575384615, abs=1e-6)       # final candidate weekly demand


def test_store_1053_d_store_no_recovery_and_exact_history_floor():
    t = DR.estimate_rate("D", w_pace=5.008827238, n_pace=4.695804196, exact_success_pace=9.545454545)
    assert t["baseline"] == pytest.approx(4.836664565, abs=1e-6)
    assert t["recovery"] == pytest.approx(4.836664565, abs=1e-6)    # no AA/A/B recovery for D
    assert t["rate"] == pytest.approx(9.545454545, abs=1e-6)


def test_no_evidence_is_not_zero_demand():
    assert DR.estimate_rate("B")["rate"] is None
