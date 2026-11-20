"""
Post-delivery shipment measurement (§29 + leadership 11/20: "sales increase after a
shipment has landed, 7–28 days") and the engine feedback aggregate.

For every store × family × delivery-date cohort inside the window:
    pre_rate  = family units sold in the 28 days BEFORE delivery ÷ 28
    post_rate = family units sold in days 7–28 AFTER delivery ÷ 21   (first week ignored:
                product is being merchandised and scans lag)
    lift      = post_rate ÷ pre_rate            (NULL when the store had no baseline)
    sell28    = units sold in the 28 days after ÷ units delivered

Classification (§29 vocabulary, measurable subset):
    too_recent   delivery younger than 28 days at the sales anchor — judged next week
    no_baseline  nothing sold before AND judged on sell-through alone
    strong       lift ≥ 1.25, or sell28 ≥ 0.60 (the leadership sell-through target)
    weak         lift ≤ 0.80 and sell28 < 0.40
    expected     everything in between

"Not received / not merchandised / EDI missing" cannot be told apart from weak demand
without store-visit or EDI evidence; weak cohorts at stores with a suspected EDI gap or a
DSM-reported problem are the DSM portal's job to re-classify.

The aggregate (store_family_response) is what the engine reads back — the training loop:
measure → aggregate → (observe: report | apply: bounded cadence shift) → next week's
orders → measure again.
"""
from datetime import date, timedelta

# Same first-feed-wins dedupe the engine uses (weekly DTS files repeat delivery rows);
# duplicated here because workers/ipr_engine.py is not importable from the app process.
DEDUP_DELIVERIES = """(
    SELECT store_id, sku_id, delivery_date, units_delivered FROM deliveries
    WHERE delivery_date <= DATE '2026-10-01'
    UNION ALL
    SELECT store_id, sku_id, delivery_date, units_delivered FROM (
        SELECT store_id, sku_id, delivery_date, units_delivered, feed_source,
               MIN(feed_source) OVER (PARTITION BY store_id, sku_id, delivery_date) AS _kf
        FROM deliveries WHERE delivery_date > DATE '2026-10-01') z
    WHERE feed_source = _kf)"""

MIN_COHORT_UNITS = 12          # below ~2 trays there is nothing to judge


def classify(lift, sell28, matured):
    if not matured:
        return "too_recent"
    if lift is None:
        return "no_baseline" if (sell28 or 0) < 0.6 else "strong"
    if lift >= 1.25 or (sell28 or 0) >= 0.60:
        return "strong"
    if lift <= 0.80 and (sell28 or 0) < 0.40:
        return "weak"
    return "expected"


def compute(cur, window_weeks: int = 26) -> dict:
    """Rebuild delivery_performance + store_family_response for the window. Idempotent:
    the window's rows are replaced. Returns a summary dict."""
    cur.execute("SELECT MAX(sale_date) AS mx FROM sales WHERE sale_date <= CURRENT_DATE")
    anchor = cur.fetchone()["mx"]
    if not anchor:
        return {"error": "no sales data"}
    w0 = anchor - timedelta(weeks=window_weeks)

    # One pass in SQL: cohorts (store × family × delivery date) with their sales windows.
    cur.execute(f"""
        WITH fam AS (
            SELECT sku_id, COALESCE(ipr_family, 'Unmapped') AS f FROM ipr_family_map),
        dl AS (
            SELECT d.store_id, fam.f AS family, d.delivery_date,
                   SUM(d.units_delivered)::int AS units
            FROM {DEDUP_DELIVERIES} d JOIN fam ON fam.sku_id = d.sku_id
            WHERE d.delivery_date > %(w0)s AND d.delivery_date <= %(anchor)s
            GROUP BY 1, 2, 3
            HAVING SUM(d.units_delivered) >= %(minu)s),
        sa AS (
            SELECT s.store_id, fam.f AS family, s.sale_date, SUM(s.units_sold)::float AS u
            FROM sales s JOIN fam ON fam.sku_id = s.sku_id
            WHERE s.sale_date > %(w0)s - 28 AND s.sale_date <= %(anchor)s
            GROUP BY 1, 2, 3)
        SELECT dl.store_id, dl.family, dl.delivery_date, dl.units,
               COALESCE(SUM(sa.u) FILTER (WHERE sa.sale_date >  dl.delivery_date - 28
                                            AND sa.sale_date <= dl.delivery_date), 0) AS pre28,
               COALESCE(SUM(sa.u) FILTER (WHERE sa.sale_date >  dl.delivery_date + 7
                                            AND sa.sale_date <= dl.delivery_date + 28), 0) AS post7_28,
               COALESCE(SUM(sa.u) FILTER (WHERE sa.sale_date >  dl.delivery_date
                                            AND sa.sale_date <= dl.delivery_date + 28), 0) AS sold28
        FROM dl LEFT JOIN sa ON sa.store_id = dl.store_id AND sa.family = dl.family
        GROUP BY dl.store_id, dl.family, dl.delivery_date, dl.units
    """, {"w0": w0, "anchor": anchor, "minu": MIN_COHORT_UNITS})
    rows = cur.fetchall()

    counts = {"strong": 0, "expected": 0, "weak": 0, "no_baseline": 0, "too_recent": 0}
    out = []
    for r in rows:
        matured = (anchor - r["delivery_date"]).days >= 28
        pre_rate = r["pre28"] / 28.0
        post_rate = r["post7_28"] / 21.0
        lift = (post_rate / pre_rate) if pre_rate > 0 else None
        sell28 = min(1.5, r["sold28"] / r["units"]) if r["units"] else None
        cls = classify(lift, sell28, matured)
        counts[cls] += 1
        out.append((r["store_id"], r["family"], r["delivery_date"], r["units"],
                    round(r["pre28"]), round(r["post7_28"]),
                    round(pre_rate, 4), round(post_rate, 4),
                    round(lift, 4) if lift is not None else None,
                    round(sell28, 4) if sell28 is not None else None, cls))

    cur.execute("DELETE FROM delivery_performance WHERE delivery_date > %s", (w0,))
    if out:
        from psycopg2.extras import execute_batch
        execute_batch(cur, """
            INSERT INTO delivery_performance (store_id, ipr_family, delivery_date, cohort_units,
                pre28_units, post7_28_units, pre_rate, post_rate, lift, sell_through_28,
                classification)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (store_id, ipr_family, delivery_date) DO UPDATE SET
                cohort_units=EXCLUDED.cohort_units, pre28_units=EXCLUDED.pre28_units,
                post7_28_units=EXCLUDED.post7_28_units, pre_rate=EXCLUDED.pre_rate,
                post_rate=EXCLUDED.post_rate, lift=EXCLUDED.lift,
                sell_through_28=EXCLUDED.sell_through_28,
                classification=EXCLUDED.classification, computed_at=now()
        """, out, page_size=500)

    # Engine-readable aggregate: matured cohorts only.
    cur.execute("""
        DELETE FROM store_family_response;
        INSERT INTO store_family_response (store_id, ipr_family, cohorts, avg_lift, avg_sell28,
            strong_share, weak_share, last_delivery, response_class)
        SELECT store_id, ipr_family, COUNT(*),
               ROUND(AVG(lift)::numeric, 3), ROUND(AVG(sell_through_28)::numeric, 3),
               ROUND(AVG((classification = 'strong')::int)::numeric, 3),
               ROUND(AVG((classification = 'weak')::int)::numeric, 3),
               MAX(delivery_date),
               CASE WHEN AVG((classification = 'strong')::int) >= 0.5 THEN 'strong'
                    WHEN AVG((classification = 'weak')::int)   >= 0.5 THEN 'weak'
                    ELSE 'expected' END
        FROM delivery_performance
        WHERE classification NOT IN ('too_recent')
        GROUP BY store_id, ipr_family
    """)
    cur.execute("SELECT COUNT(*) AS n FROM store_family_response")
    agg_n = cur.fetchone()["n"]
    return {"anchor": str(anchor), "window_weeks": window_weeks,
            "cohorts": len(out), "by_class": counts, "store_families_aggregated": agg_n,
            "method": "post-rate = days 7-28 after delivery / 21; pre-rate = 28 days before / 28; "
                      "strong = lift >= 1.25 or 60% sold in 28 days; weak = lift <= 0.8 and < 40% sold"}


def network_summary(cur) -> dict:
    """Read-side rollup for the Reports tab and the DSM portal."""
    cur.execute("""SELECT classification, COUNT(*) AS n, SUM(cohort_units)::int AS units,
                          ROUND(AVG(lift)::numeric, 3) AS avg_lift,
                          ROUND(AVG(sell_through_28)::numeric, 3) AS avg_sell28
                   FROM delivery_performance GROUP BY 1 ORDER BY 2 DESC""")
    by_class = cur.fetchall()
    cur.execute("""SELECT ipr_family, COUNT(*) AS cohorts,
                          ROUND(AVG(lift)::numeric, 3) AS avg_lift,
                          ROUND(AVG(sell_through_28)::numeric, 3) AS avg_sell28,
                          ROUND(AVG((classification = 'strong')::int)::numeric, 3) AS strong_share
                   FROM delivery_performance WHERE classification <> 'too_recent'
                   GROUP BY 1 ORDER BY 2 DESC""")
    by_family = cur.fetchall()
    cur.execute("""SELECT r.store_id, s.store_name, s.state, r.ipr_family, r.cohorts,
                          r.avg_lift, r.avg_sell28, r.response_class
                   FROM store_family_response r JOIN stores s ON s.store_id = r.store_id
                   WHERE r.cohorts >= 2 AND r.response_class = 'strong'
                   ORDER BY r.avg_lift DESC NULLS LAST LIMIT 15""")
    best = cur.fetchall()
    cur.execute("""SELECT r.store_id, s.store_name, s.state, r.ipr_family, r.cohorts,
                          r.avg_lift, r.avg_sell28, r.response_class
                   FROM store_family_response r JOIN stores s ON s.store_id = r.store_id
                   WHERE r.cohorts >= 2 AND r.response_class = 'weak'
                   ORDER BY r.avg_sell28 ASC NULLS LAST LIMIT 15""")
    worst = cur.fetchall()
    cur.execute("SELECT MAX(computed_at) AS at, COUNT(*) AS n FROM delivery_performance")
    meta = cur.fetchone()
    return {"computed_at": str(meta["at"]) if meta["at"] else None, "cohorts": meta["n"],
            "by_class": by_class, "by_family": by_family,
            "strongest_store_families": best, "weakest_store_families": worst}
