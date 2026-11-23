"""Weekly sales outlook for a run — the numbers on the This Week tiles and the release email.

Josh 2026-11-23, replacing the engine's "sales from trusted inventory" readout, which came out
at about half of what stores actually sell:

  baseline   what we expect to sell NEXT week with no new inventory = the last full week of
             ACTUAL sales before the run's sales cut-off (units x SKU wholesale price) moved
             by the recent trend and last year's same-week change (see _forecast_factor)
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
DEFAULT_GROWTH = 1200000        # CEO review 2026-11-23: $1.2M growth objective; $900K is the floor
# Sales dollars as the AGS feed reports them (what the team sees on the sales report); where a
# row has no feed dollars (history before 2026) fall back to units x SKU wholesale price. Units x
# price alone dropped SKUs with no wholesale price on file (three holiday SKUs, ~$50K a week).
PRICE = "COALESCE(sa.dollars, sa.units_sold * COALESCE(k.wholesale_price_whl, 0))"


def _setting(cur, key, default):
    cur.execute("SELECT value FROM ipr_settings WHERE key = %s", (key,))
    row = cur.fetchone()
    try:
        return float(row["value"]) if row and row["value"] is not None else default
    except (TypeError, ValueError):
        return default


FACTOR_MIN, FACTOR_MAX = 0.80, 1.10


def _forecast_factor(cur, anchor) -> dict:
    """Next week's sales as a multiple of the last full week (Josh 2026-11-23: the baseline tile
    is a forecast, not last week's actual).

    Half the recent trend (average week-over-week change across the last 4 weeks), half what the
    same week did last year (sales per selling store, so store-count growth does not distort it).
    Back-tested one week ahead over Aug 31 - Nov 16 2026: 7.2% average miss, 6.9% over the last
    five weeks, against 7.2% / 10.0% for "same as last week". Limited to -20% / +10% a week.
    """
    price = PRICE
    cur.execute(f"""SELECT (%s::date - sa.sale_date) / 7 AS b, SUM({price}) AS usd
                    FROM sales sa JOIN skus k ON k.sku_id = sa.sku_id
                    WHERE sa.sale_date > %s::date - 35 AND sa.sale_date <= %s::date
                    GROUP BY 1""", (anchor, anchor, anchor))
    w = {int(r["b"]): float(r["usd"] or 0) for r in cur.fetchall()}
    trend = None
    if w.get(0, 0) > 0 and w.get(4, 0) > 0:
        trend = (w[0] / w[4]) ** 0.25
    cur.execute(f"""SELECT (%s::date - 357 - sa.sale_date) / 7 AS b, SUM({price}) AS usd,
                           COUNT(DISTINCT sa.store_id) AS stores
                    FROM sales sa JOIN skus k ON k.sku_id = sa.sku_id
                    WHERE sa.sale_date > %s::date - 371 AND sa.sale_date <= %s::date - 357
                    GROUP BY 1""", (anchor, anchor, anchor))
    ly = {int(r["b"]): (float(r["usd"] or 0) / r["stores"] if r["stores"] else 0) for r in cur.fetchall()}
    last_year = ly[0] / ly[1] if ly.get(0, 0) > 0 and ly.get(1, 0) > 0 else None
    if trend and last_year:
        factor, method = (trend * last_year) ** 0.5, "recent 4-week trend blended with the same week last year"
    elif trend:
        factor, method = trend, "recent 4-week trend"
    elif last_year:
        factor, method = last_year, "same week last year"
    else:
        factor, method = 1.0, "last week, unchanged (not enough history)"
    return {"factor": min(FACTOR_MAX, max(FACTOR_MIN, factor)), "method": method,
            "trend": round(trend, 4) if trend else None,
            "last_year": round(last_year, 4) if last_year else None}


def compute(cur, summary: dict) -> dict | None:
    """Outlook dict for a run's saved summary, or None when the summary has no sales cut-off."""
    summary = summary or {}
    anchor = summary.get("sales_anchor")
    if not anchor:
        return None
    o = summary.get("objective") or {}
    ce = summary.get("cart_economics") or {}
    cur.execute(f"""SELECT COALESCE(SUM({PRICE}), 0) AS usd,
                          COALESCE(SUM(sa.units_sold), 0) AS units
                   FROM sales sa JOIN skus k ON k.sku_id = sa.sku_id
                   WHERE sa.sale_date > %s::date - 7 AND sa.sale_date <= %s::date""", (anchor, anchor))
    wk = cur.fetchone()
    cur.execute(f"""SELECT COALESCE(SUM({PRICE}), 0) / 4.0 AS usd
                   FROM sales sa JOIN skus k ON k.sku_id = sa.sku_id
                   WHERE sa.sale_date > %s::date - 28 AND sa.sale_date <= %s::date""", (anchor, anchor))
    avg4 = cur.fetchone()
    pct = min(1.0, max(0.0, _setting(cur, "plan_sell_through_pct", DEFAULT_PCT)))
    weeks = max(1.0, _setting(cur, "plan_sell_through_weeks", DEFAULT_WEEKS))
    last_week = float(wk["usd"] or 0)
    fc = _forecast_factor(cur, anchor)
    baseline = last_week * fc["factor"]
    plan = float(ce.get("wholesale_value") or 0)
    added = plan * pct / weeks
    goal = float(o.get("minimum_weekly_retail") or 0)
    growth = _setting(cur, "weekly_growth_objective", DEFAULT_GROWTH)
    expected = baseline + added
    return {
        "baseline": round(baseline), "baseline_units": int(wk["units"] or 0),
        "last_week_actual": round(last_week), "baseline_is_forecast": True,
        "baseline_factor": round(fc["factor"], 4), "baseline_trend_factor": fc["trend"],
        "baseline_last_year_factor": fc["last_year"], "baseline_method": fc["method"],
        "baseline_week_ending": str(anchor), "baseline_4wk_average": round(float(avg4["usd"] or 0)),
        "plan_wholesale": round(plan), "sell_through_pct": pct, "sell_through_weeks": weeks,
        "added": round(added), "expected": round(expected),
        "goal": round(goal), "gap": round(goal - expected),
        "growth_goal": round(growth), "gap_to_growth": round(growth - expected),
        "demand_forecast": o.get("unconstrained_weekly_demand_retail"),
        "basis": "sales dollars from the AGS feed",
        "engine_trusted_inventory_estimate": o.get("expected_weekly_retail"),
    }
