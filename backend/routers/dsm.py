"""
DSM web portal API (spec §26) — store search, store facts, SKU performance, the DSM AI
question box, store-visit input, and the DSM alert feed + email digests.

Every answer is grounded in the same tables the engine writes (sales, deliveries,
ipr_store_family, run_decisions), with the data-as-of date attached (§26.5).
"""
import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from db import get_db, cursor
from routers.auth import get_current_user, require_role
from routers.ai_explanations import _check_ai_budget
from services import emails
from services.ipr_ai import V2_SYSTEM, call_model

router = APIRouter()

CANCELLED = ("cancelled", "canceled", "failed", "rejected", "superseded", "deleted")
VISIT_ROLES = ("dsm", "sales_director", "manager", "admin")
MATURITY_DAYS = 14          # deliveries younger than this are "not mature" (§11.1 lag)


def _latest_run(cur):
    cur.execute("""SELECT id, shipping_week, run_at, sales_cutoff_date, ipr_summary
                   FROM recommendation_runs
                   WHERE engine_version = 'v2' AND COALESCE(status, '') NOT IN %s
                   ORDER BY run_at DESC LIMIT 1""", (CANCELLED,))
    return cur.fetchone()


def _sales_asof(cur):
    cur.execute("SELECT MAX(sale_date) AS mx FROM sales WHERE sale_date <= CURRENT_DATE")
    return cur.fetchone()["mx"]


# ── §26.2 store search ────────────────────────────────────────────────────────
@router.get("/stores/search")
def search_stores(q: str = Query(min_length=1, max_length=80), limit: int = Query(20, ge=1, le=100),
                  user=Depends(get_current_user)):
    like = f"%{q.strip()}%"
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT s.store_id, s.store_name, s.city, s.state, s.merchant, s.region,
                   s.volume_code, s.dsm, s.active, s.store_status
            FROM stores s
            WHERE s.store_id::text = %s
               OR s.store_name ILIKE %s OR s.city ILIKE %s OR s.state ILIKE %s
               OR s.merchant ILIKE %s OR s.region ILIKE %s
            ORDER BY (s.store_id::text = %s) DESC, s.store_name
            LIMIT %s
        """, (q.strip(), like, like, like, like, like, q.strip(), limit))
        return cur.fetchall()


# ── §26.3 store facts ─────────────────────────────────────────────────────────
@router.get("/stores/{store_id}/profile")
def store_profile(store_id: int, user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT * FROM stores WHERE store_id = %s", (store_id,))
        store = cur.fetchone()
        if not store:
            raise HTTPException(404, "Store not found")
        asof = _sales_asof(cur)
        run = _latest_run(cur)

        # Family grades + service status (engine-written, §6/§11)
        cur.execute("""SELECT ipr_family, grade, weekly_velocity, conversion_13wk,
                              conversion_confidence, service_status, computed_at
                       FROM ipr_store_family WHERE store_id = %s
                       ORDER BY weekly_velocity DESC NULLS LAST""", (store_id,))
        families = cur.fetchall()

        # Weekly sales, last 13 weeks + YTD (§26.3: recent weekly sales, YTD, units)
        cur.execute("""
            SELECT date_trunc('week', sale_date)::date AS week,
                   SUM(units_sold)::int AS units, ROUND(SUM(dollars)::numeric, 0) AS dollars
            FROM sales WHERE store_id = %s AND sale_date > %s
            GROUP BY 1 ORDER BY 1
        """, (store_id, (asof or date.today()) - timedelta(days=91)))
        weekly = cur.fetchall()
        cur.execute("""SELECT SUM(units_sold)::int AS units, ROUND(SUM(dollars)::numeric, 0) AS dollars
                       FROM sales WHERE store_id = %s AND sale_date >= date_trunc('year', CURRENT_DATE)""",
                    (store_id,))
        ytd = cur.fetchone()

        # Rank within nation / region / merchant by 13-wk dollars (§26.4)
        cur.execute("""
            WITH s13 AS (
                SELECT st.store_id, st.region, st.merchant, COALESCE(SUM(sa.dollars), 0) AS d
                FROM stores st LEFT JOIN sales sa
                  ON sa.store_id = st.store_id AND sa.sale_date > CURRENT_DATE - 91
                WHERE st.active IS NOT FALSE
                GROUP BY st.store_id, st.region, st.merchant),
            ranked AS (
                SELECT store_id,
                       RANK() OVER (ORDER BY d DESC) AS national_rank,
                       COUNT(*) OVER () AS national_of,
                       RANK() OVER (PARTITION BY region ORDER BY d DESC) AS region_rank,
                       COUNT(*) OVER (PARTITION BY region) AS region_of,
                       RANK() OVER (PARTITION BY merchant ORDER BY d DESC) AS merchant_rank,
                       COUNT(*) OVER (PARTITION BY merchant) AS merchant_of
                FROM s13)
            SELECT * FROM ranked WHERE store_id = %s
        """, (store_id,))
        ranks = cur.fetchone()

        # Last delivery + inbound (§26.3: last delivery, inbound orders)
        cur.execute("SELECT MAX(delivery_date) AS last_delivery FROM deliveries WHERE store_id = %s",
                    (store_id,))
        last_delivery = cur.fetchone()["last_delivery"]
        inbound = {"loaded_units": 0, "manifest_delivery_after_sales": None}
        cur.execute("SELECT to_regclass('public.in_transit_lines') IS NOT NULL AS ok")
        if cur.fetchone()["ok"] and asof:
            cur.execute("""SELECT COALESCE(SUM(units), 0)::int AS u FROM in_transit_lines
                           WHERE store_id = %s AND delivery_date > %s""", (store_id, asof))
            inbound["loaded_units"] = cur.fetchone()["u"]
        if asof:
            cur.execute("""SELECT MAX(delivery_date) AS mx FROM delivery_manifest
                           WHERE store_id = %s AND delivery_date > %s""", (store_id, asof))
            inbound["manifest_delivery_after_sales"] = cur.fetchone()["mx"]

        # Latest-run decisions: risks, warnings, service events for this store (§26.3)
        decisions, risks = [], []
        if run:
            cur.execute("""SELECT decision_type, sku_id, reason, data FROM run_decisions
                           WHERE run_id = %s AND store_id = %s
                           ORDER BY decision_type, sku_id""", (run["id"], store_id))
            for r in cur.fetchall():
                d = dict(r)
                if r["decision_type"] == "line" and (r["data"] or {}).get("risk") in ("Critical", "High"):
                    risks.append({"sku_id": r["sku_id"], "risk": r["data"]["risk"],
                                  "projected": r["data"].get("projected"),
                                  "forecast_wk": r["data"].get("forecast_wk")})
                if r["decision_type"] != "line":
                    decisions.append(d)

        # Pending approvals (§26.3)
        cur.execute("""SELECT id, ipr_family, status, test_units, requested_at FROM ipr_reactivations
                       WHERE store_id = %s AND status IN ('pending_dsm', 'pending_director')""",
                    (store_id,))
        pending = cur.fetchall()

        # Recent store visits (§26.6)
        cur.execute("""SELECT visited_at, recorded_by, merchandising_status, discarded_units, comments
                       FROM dsm_store_visits WHERE store_id = %s
                       ORDER BY visited_at DESC LIMIT 5""", (store_id,))
        visits = cur.fetchall()

        # EDI hint: selling store silent for 14+ days
        cur.execute("""SELECT MAX(sale_date) AS last_sale,
                              COALESCE(SUM(units_sold) FILTER (WHERE sale_date > CURRENT_DATE - 91), 0)::int AS u13
                       FROM sales WHERE store_id = %s""", (store_id,))
        edi = cur.fetchone()
        edi_gap = bool(edi["u13"] and edi["last_sale"] and (date.today() - edi["last_sale"]).days >= 14)

        return {
            "data_as_of": str(asof) if asof else None,
            "store": store,
            "families": families,
            "weekly_sales_13wk": weekly,
            "ytd": ytd,
            "ranks": ranks,
            "last_delivery": last_delivery,
            "inbound": inbound,
            "edi": {"last_sale": edi["last_sale"], "suspected_gap": edi_gap},
            "latest_run": {"id": str(run["id"]), "shipping_week": str(run["shipping_week"])} if run else None,
            "stockout_risks": sorted(risks, key=lambda x: (x["risk"] != "Critical", x["sku_id"]))[:15],
            "engine_decisions": decisions[:40],
            "pending_approvals": pending,
            "recent_visits": visits,
        }


# ── §26.4 SKU performance ─────────────────────────────────────────────────────
@router.get("/stores/{store_id}/skus")
def store_skus(store_id: int, user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        asof = _sales_asof(cur)
        if not asof:
            raise HTTPException(503, "No sales data loaded")
        run = _latest_run(cur)
        cur.execute("""
            WITH sa AS (
                SELECT sku_id,
                       COALESCE(SUM(units_sold) FILTER (WHERE sale_date > %(d91)s), 0)::float AS u13,
                       COALESCE(SUM(dollars)    FILTER (WHERE sale_date > %(d91)s), 0)::float AS d13,
                       COALESCE(SUM(units_sold) FILTER (WHERE sale_date > %(d28)s), 0)::float AS u4,
                       MAX(sale_date) AS last_sale
                FROM sales WHERE store_id = %(st)s AND sale_date > %(d182)s GROUP BY sku_id),
            dl AS (
                SELECT sku_id,
                       COALESCE(SUM(units_delivered) FILTER (WHERE delivery_date > %(d91)s), 0)::float AS del13,
                       MAX(delivery_date) AS last_del
                FROM deliveries WHERE store_id = %(st)s AND delivery_date > %(d182)s GROUP BY sku_id)
            SELECT COALESCE(sa.sku_id, dl.sku_id) AS sku_id, k.description,
                   COALESCE(fm.ipr_family, 'Unmapped') AS family,
                   COALESCE(sa.u13, 0) AS u13, COALESCE(sa.d13, 0) AS d13, COALESCE(sa.u4, 0) AS u4,
                   sa.last_sale, COALESCE(dl.del13, 0) AS del13, dl.last_del
            FROM sa FULL OUTER JOIN dl ON dl.sku_id = sa.sku_id
            LEFT JOIN skus k ON k.sku_id = COALESCE(sa.sku_id, dl.sku_id)
            LEFT JOIN ipr_family_map fm ON fm.sku_id = COALESCE(sa.sku_id, dl.sku_id)
        """, {"st": store_id, "d91": asof - timedelta(days=91), "d28": asof - timedelta(days=28),
              "d182": asof - timedelta(days=182)})
        rows = [dict(r) for r in cur.fetchall()]

        # Latest-run line evidence for risk / projected stock (probable stockouts, excess)
        line_ev = {}
        if run:
            cur.execute("""SELECT sku_id, data FROM run_decisions
                           WHERE run_id = %s AND store_id = %s AND decision_type = 'line'""",
                        (run["id"], store_id))
            line_ev = {r["sku_id"]: r["data"] for r in cur.fetchall()}

        today = date.today()
        for r in rows:
            r["r13"], r["r4"] = r["u13"] / 13.0, r["u4"] / 4.0
            r["trend"] = (r["r4"] / r["r13"]) if r["r13"] else None
            ev = line_ev.get(r["sku_id"]) or {}
            r["risk"], r["projected"] = ev.get("risk"), ev.get("projected")
            # §26.4: bottom-SKU reporting must distinguish the cause
            if r["u13"] < 3:
                if not r["del13"]:
                    r["cause"] = "no inventory (nothing delivered in 13 weeks)"
                elif r["last_del"] and (today - r["last_del"]).days < MATURITY_DAYS:
                    r["cause"] = "recent delivery not mature yet"
                elif r["last_del"] and (today - r["last_del"]).days > 70:
                    r["cause"] = "stale inventory (last delivery > 70 days)"
                else:
                    r["cause"] = "weak demand (delivered, not selling)"
            else:
                r["cause"] = None

        sellers = [r for r in rows if r["u13"] > 0]
        by_u13 = sorted(sellers, key=lambda r: -r["u13"])
        growing = sorted((r for r in sellers if r["trend"] is not None and r["u13"] >= 5),
                         key=lambda r: -(r["trend"] or 0))
        no_recent = [r for r in sellers
                     if r["last_sale"] and (asof - r["last_sale"]).days >= 14]
        stockouts = [r for r in rows if r["risk"] in ("Critical", "High")]
        excess = sorted((r for r in rows if (r["projected"] or 0) > 8 * max(r["r13"], 0.1)),
                        key=lambda r: -(r["projected"] or 0))
        fam = {}
        for r in rows:
            f = fam.setdefault(r["family"], {"family": r["family"], "u13": 0, "d13": 0.0})
            f["u13"] += r["u13"]
            f["d13"] += r["d13"]
        fam_rank = sorted(fam.values(), key=lambda f: -f["u13"])

        slim = lambda rs: [{k: r.get(k) for k in
                            ("sku_id", "description", "family", "u13", "u4", "d13", "trend",
                             "last_sale", "del13", "last_del", "risk", "projected", "cause")}
                           for r in rs]
        return {
            "data_as_of": str(asof),
            "top5": slim(by_u13[:5]),
            "bottom5": slim(sorted(rows, key=lambda r: (r["u13"], -(r["del13"] or 0)))[:5]),
            "fastest_growing": slim(growing[:5]),
            "declining": slim(list(reversed(growing))[:5] if growing else []),
            "no_recent_sales": slim(sorted(no_recent, key=lambda r: -r["u13"])[:10]),
            "probable_stockouts": slim(stockouts[:10]),
            "excess_inventory": slim(excess[:10]),
            "best_families": fam_rank[:5],
            "weakest_families": fam_rank[-5:][::-1] if len(fam_rank) > 5 else [],
            "sku_count": len(rows),
        }


# ── §26.5 DSM AI question box ─────────────────────────────────────────────────
class AskStore(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


@router.post("/stores/{store_id}/ask")
def ask_store(store_id: int, body: AskStore,
              user=Depends(require_role("dsm", "sales_director", "manager", "admin", "order_writer"))):
    """Grounded AI answer about one store: profile + SKU performance + the engine's own
    decisions are assembled server-side so the model answers from data, not memory."""
    profile = store_profile(store_id, user)                 # grounded context, same auth
    skus = store_skus(store_id, user)
    with get_db() as conn:
        _check_ai_budget(cursor(conn), user, "ask")
    ctx = {
        "data_as_of": profile["data_as_of"],
        "store": {k: profile["store"].get(k) for k in
                  ("store_id", "store_name", "city", "state", "merchant", "region", "volume_code",
                   "dsm", "active", "store_status", "capacity_carts", "approved_assortment",
                   "receiving_restrictions")},
        "families": profile["families"],
        "weekly_sales_13wk": profile["weekly_sales_13wk"],
        "ytd": profile["ytd"], "ranks": profile["ranks"],
        "last_delivery": profile["last_delivery"], "inbound": profile["inbound"],
        "edi": profile["edi"], "stockout_risks": profile["stockout_risks"],
        "engine_decisions": profile["engine_decisions"],
        "pending_approvals": profile["pending_approvals"],
        "recent_visits": profile["recent_visits"],
        "sku_performance": {k: skus[k] for k in
                            ("top5", "bottom5", "fastest_growing", "no_recent_sales",
                             "probable_stockouts", "excess_inventory", "best_families",
                             "weakest_families")},
    }
    answer = call_model(
        V2_SYSTEM,
        "STORE DATA (JSON, as of the data_as_of date — cite it):\n"
        + json.dumps(ctx, default=str)
        + f"\n\nDSM QUESTION: {body.question.strip()}",
        max_tokens=1200,
    )
    return {"answer": answer, "store_id": store_id, "data_as_of": profile["data_as_of"]}


# ── §29 shipment response for one store (feeds the DSM visit conversation) ────
@router.get("/stores/{store_id}/response")
def store_response(store_id: int, user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT to_regclass('public.delivery_performance') IS NOT NULL AS ok")
        if not cur.fetchone()["ok"]:
            return {"available": False, "note": "Post-delivery measurement is not set up yet"}
        cur.execute("""SELECT ipr_family, delivery_date, cohort_units, pre28_units, post7_28_units,
                              lift, sell_through_28, classification
                       FROM delivery_performance WHERE store_id = %s
                       ORDER BY delivery_date DESC LIMIT 40""", (store_id,))
        cohorts = cur.fetchall()
        cur.execute("""SELECT ipr_family, cohorts, avg_lift, avg_sell28, response_class
                       FROM store_family_response WHERE store_id = %s
                       ORDER BY cohorts DESC""", (store_id,))
        return {"available": True, "cohorts": cohorts, "by_family": cur.fetchall()}


# ── §26.6 store-visit input ───────────────────────────────────────────────────
class VisitIn(BaseModel):
    visited_at: date | None = None
    verified_counts: dict[str, int] | None = None     # sku_id -> units seen on the floor
    merchandising_status: str | None = Field(None, max_length=60)
    product_condition: str | None = Field(None, max_length=500)
    discarded_units: int | None = Field(None, ge=0, le=100000)
    display_issues: str | None = Field(None, max_length=2000)
    comments: str | None = Field(None, max_length=4000)
    recommended_changes: str | None = Field(None, max_length=2000)
    follow_up: str | None = Field(None, max_length=2000)


@router.post("/stores/{store_id}/visits", status_code=201)
def record_visit(store_id: int, body: VisitIn, user=Depends(require_role(*VISIT_ROLES))):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT 1 FROM stores WHERE store_id = %s", (store_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Store not found")
        cur.execute("""
            INSERT INTO dsm_store_visits (store_id, visited_at, recorded_by, verified_counts,
                merchandising_status, product_condition, discarded_units, display_issues,
                comments, recommended_changes, follow_up)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (store_id, body.visited_at or date.today(), user.get("email"),
              json.dumps(body.verified_counts) if body.verified_counts else None,
              body.merchandising_status, body.product_condition, body.discarded_units,
              body.display_issues, body.comments, body.recommended_changes, body.follow_up))
        return {"id": str(cur.fetchone()["id"])}


@router.get("/stores/{store_id}/visits")
def list_visits(store_id: int, user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""SELECT * FROM dsm_store_visits WHERE store_id = %s
                       ORDER BY visited_at DESC, created_at DESC LIMIT 50""", (store_id,))
        return cur.fetchall()


# ── Alert feed + email digests ────────────────────────────────────────────────
ALERT_TYPES = ("min_cart_service_review", "stopped_13wk", "capacity_cap")


@router.get("/alerts")
def alerts(mine: bool = False, user=Depends(get_current_user)):
    """DSM alert feed from the latest run: service stops, min-cart review escalations,
    capacity cuts, plus pending reactivations. mine=true filters to stores assigned to
    the signed-in DSM (stores.dsm_email)."""
    with get_db() as conn:
        cur = cursor(conn)
        run = _latest_run(cur)
        out = {"run": {"id": str(run["id"]), "shipping_week": str(run["shipping_week"])} if run else None,
               "alerts": [], "pending_reactivations": []}
        if run:
            cur.execute(f"""
                SELECT d.decision_type, d.store_id, s.store_name, s.state, s.dsm, s.dsm_email,
                       s.volume_code, d.reason, d.data
                FROM run_decisions d JOIN stores s ON s.store_id = d.store_id
                WHERE d.run_id = %s AND d.decision_type = ANY(%s)
                  {"AND s.dsm_email = %s" if mine else ""}
                ORDER BY d.decision_type, d.store_id
            """, (run["id"], list(ALERT_TYPES), user.get("email")) if mine
                 else (run["id"], list(ALERT_TYPES)))
            rows = cur.fetchall()
            # §11.4: an AA/A store stopped on conversion is an URGENT operational alert.
            # dsm_email is staff contact data — only managers/admins see it in the feed.
            show_email = user.get("role") in ("manager", "admin")
            for r in rows:
                r = dict(r)
                if not show_email:
                    r.pop("dsm_email", None)
                r["urgent"] = (r["decision_type"] == "stopped_13wk"
                               and (r.get("volume_code") or "") in ("AA", "A"))
                out["alerts"].append(r)
            out["alerts"].sort(key=lambda r: (not r["urgent"], r["store_id"]))
        cur.execute("""SELECT r.id, r.store_id, s.store_name, r.ipr_family, r.status, r.requested_at
                       FROM ipr_reactivations r JOIN stores s ON s.store_id = r.store_id
                       WHERE r.status IN ('pending_dsm', 'pending_director')
                       ORDER BY r.requested_at""")
        out["pending_reactivations"] = cur.fetchall()
        return out


@router.post("/alerts/notify")
def notify_dsms(user=Depends(require_role("manager", "admin"))):
    """Compose one digest email per DSM (stores.dsm_email) covering their stores' alerts
    from the latest run. Sends over SMTP when configured, otherwise queues in email_outbox."""
    feed = alerts(mine=False, user=user)
    if not feed["run"]:
        raise HTTPException(409, "No v2 run to notify about")
    by_dsm = {}
    for a in feed["alerts"]:
        if a.get("dsm_email"):
            by_dsm.setdefault(a["dsm_email"], []).append(a)
    if not by_dsm:
        return {"sent": 0, "note": "No alert is attached to a store with a dsm_email set — "
                                   "load DSM assignments on the stores table first."}
    results = {}
    with get_db() as conn:
        cur = cursor(conn)
        for email_addr, items in sorted(by_dsm.items()):
            urgent = [a for a in items if a["urgent"]]
            lines = [f"TPC — IPR Engine · DSM alerts for ship week {feed['run']['shipping_week']}", ""]
            if urgent:
                lines.append("URGENT (AA/A store stopped on 13-week conversion — §11.4):")
                lines += [f"  • Store {a['store_id']} {a['store_name']}, {a['state']}: {a['reason']}"
                          for a in urgent]
                lines.append("")
            rest = [a for a in items if not a["urgent"]]
            if rest:
                lines.append("Needs review:")
                lines += [f"  • Store {a['store_id']} {a['store_name']}, {a['state']} "
                          f"[{a['decision_type']}]: {a['reason']}" for a in rest]
            lines += ["", "Open the DSM portal for store detail and to record your review: /dsm"]
            results[email_addr] = emails.send_or_queue(
                cur, email_addr,
                f"IPR alerts — {len(items)} store(s), week {feed['run']['shipping_week']}",
                "\n".join(lines), "dsm_digest",
                {"run_id": feed["run"]["id"], "stores": [a["store_id"] for a in items]})
    return {"recipients": results, "smtp_configured": emails.smtp_configured()}


@router.get("/outbox")
def outbox(user=Depends(require_role("manager", "admin"))):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""SELECT id, to_email, subject, kind, status, error, created_at, sent_at
                       FROM email_outbox ORDER BY created_at DESC LIMIT 200""")
        return {"smtp_configured": emails.smtp_configured(), "messages": cur.fetchall()}


@router.post("/outbox/flush")
def outbox_flush(user=Depends(require_role("manager", "admin"))):
    with get_db() as conn:
        return emails.flush_outbox(cursor(conn))
