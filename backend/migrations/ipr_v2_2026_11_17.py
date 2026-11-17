"""
Migration: IPR Engine v2 (AI Replenishment spec of 2026-11-17 = "the Bible").

Adds, alongside the existing JIM schema (v2 writes the same orders/carts tables so
Routing Exchange, pack plans, exports, and the editor all keep working):

- recommendation_runs.engine_version ('v1' legacy | 'v2' IPR), pipeline_status (spec §28),
  ipr_summary jsonb ($900K readout §2, allocation outputs §15, validation §5)
- ipr_settings       — every tunable the spec marks "initial, requires back-testing" (§11.2,
                        §12, §13, §30) plus explicit Josh overrides (no cart maxima), each
                        row flagged so nothing invented masquerades as approved policy
- ipr_family_map     — SKU -> spec §4.5 product family, approved=false until the business
                        signs off ("final SKU-to-family mappings must be approved")
- ipr_store_family   — latest store x family grade, velocity, mature-delivery conversion,
                        service status (§6, §11) — feeds the future DSM portal (§26)

Idempotent; pooler-safe.
"""
import os
import psycopg2

DDL = """
ALTER TABLE recommendation_runs ADD COLUMN IF NOT EXISTS engine_version text NOT NULL DEFAULT 'v1';
ALTER TABLE recommendation_runs ADD COLUMN IF NOT EXISTS pipeline_status text;
ALTER TABLE recommendation_runs ADD COLUMN IF NOT EXISTS ipr_summary jsonb;

CREATE INDEX IF NOT EXISTS run_decisions_run_type ON run_decisions(run_id, decision_type);

CREATE TABLE IF NOT EXISTS ipr_settings (
    key            text PRIMARY KEY,
    value          jsonb NOT NULL,
    spec_ref       text,
    status         text NOT NULL DEFAULT 'initial — requires back-testing',
    note           text,
    updated_at     timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE ipr_settings ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS ipr_family_map (
    sku_id         bigint PRIMARY KEY,
    ipr_family     text NOT NULL,
    approved       boolean NOT NULL DEFAULT false,
    source         text NOT NULL DEFAULT 'proposed: skus.family + legacy_product_group',
    updated_at     timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE ipr_family_map ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS ipr_store_family (
    store_id            integer NOT NULL,
    ipr_family          text NOT NULL,
    grade               text,
    weekly_velocity     numeric,
    conversion_13wk     numeric,
    conversion_confidence text,
    service_status      text,
    run_id              uuid,
    computed_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (store_id, ipr_family)
);
ALTER TABLE ipr_store_family ENABLE ROW LEVEL SECURITY;
"""

# value, spec ref, status, note
SETTINGS = {
    "grade_cutoffs_pct": ({"AA": 2, "A": 10, "B": 30, "C+": 45, "C": 60, "C-": 80, "D": 100},
        "§6", "PROPOSED — C+/C- cut lines are a business input",
        "Cumulative percentile of store x family weekly velocity among stores selling the family."),
    "conversion_thresholds": ({"AA": 0.67, "A": 0.67, "B": 0.70, "C+": 0.70, "C": 0.75, "C-": 0.80, "D": 0.80},
        "§11.2", "initial — requires back-testing", "Grade-sensitive automatic-eligibility thresholds."),
    "conversion_hard_block": (0.60, "§11.3", "spec rule", "Below this after 13 weeks: blocked; DSM + Sales Director reactivation only."),
    "conversion_delivery_lag_weeks": (2, "§11.1", "implementation choice",
        "Deliveries in the last N weeks are immature and excluded from the conversion cohort."),
    "coverage_weeks": (4, "§10", "initial — requires back-testing",
        "Target inventory = forecast weekly rate x coverage weeks (selling window)."),
    "forecast_weights": ({"w4": 0.5, "w10": 0.3, "w13": 0.2}, "§7", "implementation choice",
        "Weekly rate blend; 13-wk is the stable baseline, recent windows lead."),
    "store_priority_weights": ({"incremental_sales": 0.35, "family_velocity": 0.25, "mature_delivery_sales": 0.20,
                                "stockout_risk": 0.10, "seasonal": 0.05, "recency": 0.05},
        "§12", "spec weights", "Used only to allocate when supply < unconstrained need."),
    "sku_fit_weights": ({"sku_velocity": 0.40, "mature_delivery_sales": 0.20, "projected_need": 0.20,
                         "seasonal": 0.10, "comparable_fit": 0.10},
        "§13", "spec weights", "Availability is NOT a factor (§13)."),
    "onhand_confidence_factor": ({"HIGH": 1.0, "MEDIUM": 0.85, "LOW": 0.70},
        "§5", "initial — requires back-testing", "Approved buffer per confidence tier."),
    "h2o_terrarium_window_weeks": (13, "§17", "spec rule", "Trays may not exceed forecast 13-week demand."),
    "min_weekly_rate_units": (0.25, "§3", "implementation choice",
        "A store x SKU needs at least this sold/week (≈1 unit/4 wks) to count as having velocity."),
    "allow_comparable_product": (False, "§7", "Josh 2026-11-17",
        "Only send SKUs the store has its own velocity for; no comparable-product estimates."),
    "enforce_cart_maxima": (False, "§18", "Josh 2026-11-17 override",
        "§18's grade cart maxima are an initial framework; Josh removed cart-count caps. Physical limits still apply."),
    "weekly_objective_retail": (900000, "§2", "spec rule", "Minimum weekly HD retail register sales."),
    "freshness_weeks": (8, "§4.5–4.6", "initial — requires back-testing / product freshness limits",
        "Sellable on-hand = min(DTS reported, units delivered in this window − units sold since). "
        "DTS dumps are batch-recorded, so reported on-hand overstates sellable stock."),
}

FAMILY_RULES = """
INSERT INTO ipr_family_map (sku_id, ipr_family)
SELECT sku_id,
  CASE
    WHEN family = '9cm Littles'                                   THEN '9cm Standard'
    WHEN family = '9cm Canopy'                                    THEN '9cm Canopy/Hanging Basket'
    WHEN family LIKE '9cm%%'                                      THEN '9cm Other'
    WHEN family = '12cm HB'                                       THEN '12cm Canopy/Hanging Basket'
    WHEN family = '12cm' AND legacy_product_group = 'Collectors'  THEN '12cm Collector'
    WHEN family = '12cm' AND legacy_product_group IN ('Rare Collectors','Boutique') THEN '12cm Rare Collector'
    WHEN family = '12cm' AND legacy_product_group = 'Premium'     THEN '12cm Standard'
    WHEN family LIKE '12cm%%'                                     THEN '12cm Other'
    WHEN family = '17cm' AND legacy_product_group = 'Collectors'  THEN '17cm Collector'
    WHEN family = '17cm' AND legacy_product_group IN ('Rare Collectors','Boutique') THEN '17cm Rare Collector'
    WHEN family = '17cm' AND legacy_product_group = 'Premium'     THEN '17cm Standard'
    WHEN family LIKE '17cm%%'                                     THEN '17cm Other'
    WHEN family LIKE 'H2O%%'                                      THEN 'H2O'
    WHEN family LIKE '%%Terrarium%%'                              THEN 'Terrarium'
    WHEN family = 'Dish Garden'                                   THEN 'Dish Garden'
    WHEN family = 'Climb-itt'                                     THEN 'Climb-ITT'
    WHEN family = 'Holiday' OR is_holiday                         THEN 'Holiday'
    ELSE 'Unmapped'
  END
FROM skus
ON CONFLICT (sku_id) DO NOTHING;
"""


def main():
    import json
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    cur = conn.cursor()
    cur.execute("SET TRANSACTION READ WRITE")
    cur.execute(DDL)
    for k, (v, ref, status, note) in SETTINGS.items():
        cur.execute("""INSERT INTO ipr_settings (key, value, spec_ref, status, note)
                       VALUES (%s, %s::jsonb, %s, %s, %s)
                       ON CONFLICT (key) DO NOTHING""", (k, json.dumps(v), ref, status, note))
    cur.execute(FAMILY_RULES)
    conn.commit()
    cur.execute("SELECT ipr_family, COUNT(*) FROM ipr_family_map GROUP BY 1 ORDER BY 1")
    print("ipr_v2: applied. family map:", cur.fetchall())
    conn.close()


if __name__ == "__main__":
    main()
