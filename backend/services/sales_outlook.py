"""Weekly sales outlook for a run — the numbers on the This Week tiles and the release email.

Josh 2026-11-23, replacing the engine's "sales from trusted inventory" readout, which came out
at about half of what stores actually sell:

  baseline   what we sell in a week with no new inventory = the last full week of ACTUAL
             sales before the run's sales cut-off (units x SKU wholesale price)
  added      what this plan adds in one week = order wholesale x the share we expect to sell
             (plan_sell_through_pct, default 60%) spread over the weeks it takes to sell it
             (plan_sell_through_weeks, default 4: the 60%-in-28-days target)
  expected   baseline + added
  demand     the engine's demand forecast: what stores could sell with every need filled

Computed when the run is read, from the saved summary plus the sales table, so it also
corrects runs made before this change. Read-only.
"""
from __future__ import annotations

DEFAULT_PCT = 0.60
DEFAULT_WEEKS = 4


def _setting(cur, key, default):
    cur.execute("SELECT value FROM ipr_settings WHERE key = %s", (key,))
    row = cur.fetchone()
    try:
        return float(row["value"]) if row and row["value"] is not None else default
    except (TypeError, ValueError):
        return default


def compute(cur, summary: dict) -> dict | None:
    """Outlook dict for a run's saved summary, or None when the summary has no sales cut-off."""
    summary = summary or {}
    anchor = summary.get("sales_anchor")
    if not anchor:
        return None
    o = summary.get("objective") or {}
    ce = summary.get("cart_economics") or {}
    cur.execute("""SELECT COALESCE(SUM(sa.units_sold * COALESCE(k.wholesale_price_whl, 0)), 0) AS usd,
                          COALESCE(SUM(sa.units_sold), 0) AS units
                   FROM sales sa JOIN skus k ON k.sku_id = sa.sku_id
                   WHERE sa.sale_date > %s::date - 7 AND sa.sale_date <= %s::date""", (anchor, anchor))
    wk = cur.fetchone()
    cur.execute("""SELECT COALESCE(SUM(sa.units_sold * COALESCE(k.wholesale_price_whl, 0)), 0) / 4.0 AS usd
                   FROM sales sa JOIN skus k ON k.sku_id = sa.sku_id
                   WHERE sa.sale_date > %s::date - 28 AND sa.sale_date <= %s::date""", (anchor, anchor))
    avg4 = cur.fetchone()
    pct = min(1.0, max(0.0, _setting(cur, "plan_sell_through_pct", DEFAULT_PCT)))
    weeks = max(1.0, _setting(cur, "plan_sell_through_weeks", DEFAULT_WEEKS))
    baseline = float(wk["usd"] or 0)
    plan = float(ce.get("wholesale_value") or 0)
    added = plan * pct / weeks
    goal = float(o.get("minimum_weekly_retail") or 0)
    expected = baseline + added
    return {
        "baseline": round(baseline), "baseline_units": int(wk["units"] or 0),
        "baseline_week_ending": str(anchor), "baseline_4wk_average": round(float(avg4["usd"] or 0)),
        "plan_wholesale": round(plan), "sell_through_pct": pct, "sell_through_weeks": weeks,
        "added": round(added), "expected": round(expected),
        "goal": round(goal), "gap": round(goal - expected),
        "demand_forecast": o.get("unconstrained_weekly_demand_retail"),
        "basis": "units x SKU wholesale price",
        "engine_trusted_inventory_estimate": o.get("expected_weekly_retail"),
    }
