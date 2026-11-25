"""Jason's Week 49 forecast regression cases (Week_49_Agent_Logic_Comparison_and_Regression.xlsx,
'Forecast regression' tab): F(q,h) = min(q*f, max(0, h*a*r - s)) and next-pack marginal units.
Run: python -m pytest tests/test_demand_forecast.py -q"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "workers"))
import demand_forecast as DF  # noqa: E402

# store, p, r, s, h, a, f, josh_q, rec_q, exp_josh, exp_rec, exp_next
CASES = [
    (628, 12, 40.413619257, 51.963171571, 5, .9, .83, 588, 168, 129.898115086, 129.898115086, 0),
    (419, 12, 40.333333333, 119.950700661, 5, .9, .83, 396, 84, 61.549299339, 61.549299339, 0),
    (2679, 12, 62.937777778, 69.841380705, 5, .9, .83, 588, 252, 213.378619295, 209.16, 4.218619295),
    (2575, 12, 40.95, 124.623433106, 5, .9, .83, 252, 84, 59.651566894, 59.651566894, 0),
    (8445, 8, 8.4, 0, 5, .9, .83, 184, 40, 37.8, 33.2, 4.6),
    (504, 25, 4.327272727, 0, 5, .9, 1, 50, 25, 19.472727273, 19.472727273, 0),
    (509, 25, 17, 0, 5, .9, 1, 25, 50, 25, 50, 25),
    (1053, 6, 9.545454545, 0, 5, .9, .83, 42, 120, 34.86, 42.954545455, 0),
    (1053, 6, 9.545454545, 0, 10, .9, .83, 42, 120, 34.86, 85.909090909, 0),
    (1084, 28, 4.299354839, 0, 10, .9, 1, 28, 28, 28, 28, 10.694193548),
]


@pytest.mark.parametrize("st,p,r,s,h,a,f,jq,rq,ej,er,en", CASES)
def test_forecast_regression_cases(st, p, r, s, h, a, f, jq, rq, ej, er, en):
    assert DF.sold_units(jq, r, s, h, a, f) == pytest.approx(ej, abs=1e-6)
    assert DF.sold_units(rq, r, s, h, a, f) == pytest.approx(er, abs=1e-6)
    assert DF.marginal_units(rq, p, r, s, h, a, f) == pytest.approx(en, abs=1e-6)


def test_ceiling_packs_store_628_and_accessory_boxes():
    # 628 Littles: demand 129.9 sellable units, 9.96 per tray -> 13 trays (the 14th adds < half a tray)
    assert DF.ceiling_packs(40.413619257, 51.963171571, 5, 12) == 13
    assert DF.ceiling_packs(4.327272727, 0, 5, 25, f=1.0) == 1      # store 504: one wire box
    assert DF.ceiling_packs(17, 0, 5, 25, f=1.0) == 3               # store 509: 2nd box adds 25
    assert DF.ceiling_packs(0, 10, 5, 12) == 0


def test_horizon_by_grade_and_family():
    assert DF.horizon_weeks("D", "9cm Standard") == 10
    assert DF.horizon_weeks("D", "H2O") == 5
    assert DF.horizon_weeks("AA", "12cm Standard") == 5
