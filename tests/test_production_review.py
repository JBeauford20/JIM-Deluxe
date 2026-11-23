"""Production review (services/production_review.py). Pure: a fake cursor, no DB, no AI.
Run: python -m pytest tests/test_production_review.py -q"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from services import production_review as pr  # noqa: E402


class Cur:
    """Answers the two queries facts() makes when the order carries SKU detail."""
    def __init__(self, run, skus):
        self.run, self.skus, self.rows = run, skus, []

    def execute(self, sql, params=None):
        self.rows = [self.run] if "FROM recommendation_runs" in sql else self.skus

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def sku(desc, fam, weekly, need, avail, on=True, plannable=True):
    return {"description": desc, "family": fam, "weekly_demand_units": weekly, "weekly_demand_usd": weekly * 10,
            "stores": 5, "need_units": need, "shipped_units": 0, "available_units": avail,
            "on_availability": on, "plannable": plannable}


RUN = {"id": "r1", "shipping_week": date(2026, 11, 30), "status": "draft", "batch_id": "b1", "ipr_summary": {
    "sales_anchor": "2026-11-22",
    "production": {
        "by_family": {"9cm": {"weekly_demand_units": 150, "need_units": 500, "weekly_demand_usd": 1500}},
        "by_sku": {"1": sku("Littles", "9cm", 100, 500, 200), "2": sku("Ceramic <9cm>", "9cm", 10, 40, 300),
                   "3": sku("Alocasia", "9cm", 40, 0, 0, on=False, plannable=False),
                   "4": sku("Clips", "Climb-ITT", 5, 20, 9000), "5": sku("Gnome", "Holiday", 7, 0, 60)}}}}
SKUS = [{"sku_id": 1, "program": "9cm", "is_holiday": False}, {"sku_id": 2, "program": "9cm", "is_holiday": False},
        {"sku_id": 3, "program": "9cm", "is_holiday": False}, {"sku_id": 4, "program": None, "is_holiday": False},
        {"sku_id": 5, "program": "Holiday", "is_holiday": True}]


def test_facts_split_plants_from_seasonal_and_hard_goods():
    f = pr.facts(Cur(RUN, SKUS), "r1")
    assert [x["description"] for x in f["hard_goods"]] == ["Clips"]
    assert [x["description"] for x in f["seasonal"]["top"]] == ["Gnome"]
    # Alocasia is not on availability: need estimated from the family's need-to-sales ratio
    alo = f["not_on_availability"]["top"][0]
    assert alo["description"] == "Alocasia" and alo["estimated"] and round(alo["need"]) == round(40 * 540 / 110)
    assert f["short_on_availability"]["top"][0]["short"] == 300 and f["surplus"]["top"][0]["surplus"] == 260
    t = f["totals"]
    assert t["available"] == 500 and t["covered"] == 240          # hardware and seasonal left out
    assert abs(f["fit"] - 240 / t["need"]) < 0.01


def test_render_with_and_without_ai():
    f = pr.facts(Cur(RUN, SKUS), "r1")
    html = pr.render(f, None, generated_at=date(2026, 11, 23))
    for text in ("Production review: ship week of Nov 30, 2026", "Ceramic &lt;9cm&gt;", "Hard goods",
                 "Seasonal and holiday items", "est.</span>", "Grow more 9cm", "could not be generated"):
        assert text in html, text
    html = pr.render(f, {"headline": "Fit is low", "changes": ["Grow more Littles"]})
    assert "Fit is low" in html and "Grow more Littles" in html and "could not be generated" not in html


def test_order_without_production_figures_says_so():
    run = {**RUN, "ipr_summary": {}}
    f = pr.facts(Cur(run, []), "r1")
    assert f["missing"] and "Build the orders again" in pr.render(f, None)
