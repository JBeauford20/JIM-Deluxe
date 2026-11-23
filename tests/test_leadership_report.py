"""Leadership report rendering (services/leadership_report.py). Pure: no DB, no AI.
Run: python -m pytest tests/test_leadership_report.py -q"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from services import leadership_report as lr  # noqa: E402

FACTS = {
    "run": {"id": "43dfdefd-0000", "status": "draft"}, "week": date(2026, 11, 30),
    "headline": {"stores": 103, "carts": 260, "units": 20700, "value": 448427, "per_cart": 1725, "freight_pct": 0.139},
    "trucks": {"count": 6, "routed": False, "note": "estimate at 45 carts per truck"},
    "split": {"routine": 10, "directed": 93},
    "outlook": {"goal": 900000, "baseline": 510401, "added": 67264, "expected": 577665, "gap": 322335,
                "baseline_factor": 0.9677, "last_week_actual": 527430, "plan_wholesale": 448427,
                "sell_through_pct": 0.6, "sell_through_weeks": 4, "demand_forecast": 892230,
                "baseline_method": "recent 4-week trend"},
    "not_shipped": [("Pushed under the full-cart minimum", 1562, 111880, 193061), ("Nothing", None, None, None)],
    "divisions": [{"division": "North", "stores": 40, "carts": 100, "units": 8000}],
    "families": [{"family": "17cm <Rare>", "stores": 80, "units": 5040, "wholesale": 293678}],
    "exceptions": [{"label": "Bulbasaur", "requested_by": "a@x", "approved_by": "m@x", "stores_asked": 114,
                    "stores_served": 80, "units_shipped": 5040, "trays_requested": 2394, "trays_granted": 1680,
                    "request": "Send one cart", "notes": ["34 stores short"], "followed": 80 / 114}],
    "rejected": [{"name": "x"}], "pending": 0, "overrides": 5, "manual_edits": 0,
    "checks": [{"rule": "Goal", "status": "not_met", "result": "short", "detail": "d", "tier": "primary"},
               {"rule": "Full cart", "status": "met", "result": "ok", "detail": ""},
               {"rule": "Freight", "status": "info", "result": "14%", "detail": ""}],
    "data": {"sales_through": "2026-11-22"},
}


def test_renders_every_section_without_ai():
    html = lr.render(FACTS, None, generated_at=date(2026, 11, 23))
    for text in ("ship week of Nov 30, 2026", "Draft, not yet released", "about 6", "$577,665",
                 "Short of goal", "80 of 114 stores", "70%", "1 of 1 supporting rules met", "0 of 1 controlling requirements met", "17cm &lt;Rare&gt;",
                 "could not be generated"):
        assert text in html, text
    assert "Nothing" not in html                      # empty not-shipped rows are left out


def test_ai_text_is_escaped_and_marked():
    ai = {"headline": "Order <b>short</b>", "why": ["one"], "exceptions": ["two"], "watch": ["three"]}
    html = lr.render(FACTS, ai)
    assert "Order &lt;b&gt;short&lt;/b&gt;" in html and "What to watch" in html
    assert "is AI-written from those figures" in html and "could not be generated" not in html


def test_fingerprint_changes_with_the_figures():
    other = {**FACTS, "headline": {**FACTS["headline"], "carts": 261}}
    assert lr.fingerprint(FACTS) == lr.fingerprint(dict(FACTS)) != lr.fingerprint(other)
