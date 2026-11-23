"""IPR v2 read API — the run's §2 objective readout, §15 allocation outputs, and the
§25 evidence trail (run_decisions) behind every shipped line, block, and hold."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from db import get_db, cursor
from routers.auth import get_current_user, require_role
from routers.concurrency import log_change

router = APIRouter()

DECISION_TYPES = ("block", "review_hold", "allocation_shortfall", "validation", "review", "line",
                  "duplicate_run", "stopped_13wk", "reduced_13wk", "gap_fill", "low_value_cart", "deferred_economics", "not_due",
                  "deferral", "reactivation", "pushed_min_cart", "min_cart_service_review",
                  "assortment_block", "capacity_cap", "response_adjustment",
                  "scenario_applied", "scenario_exclusion",
                  "store_rule_block", "sku_restriction_block", "variety_restriction_block",
                  "variety_restriction", "directed_shipment", "store_rule_override",
                  "cart_completion")
# Spec §11.3 / §27: commercial reactivation is approved by the assigned DSM and then the
# Home Depot Sales Director — never by the IPR/development team. 'admin' may act as either
# role (Josh), but one person can never sign both steps. 'manager' joined the director step
# 2026-11-20: all approvals run through the IPR Manager/Approver (Mike Treiber).
DSM_ROLES = ("dsm", "admin")
DIRECTOR_ROLES = ("sales_director", "manager", "admin")


# Mirrors workers/ipr_engine.py CANCELLED_RUN_STATUSES: runs in these statuses are ignored by
# the duplicate-run control, so a discarded test run no longer counts as inbound supply.
CANCELLED_RUN_STATUSES = ("cancelled", "canceled", "failed", "rejected", "superseded", "deleted")


@router.get("/runs")
def list_runs(limit: int = Query(40, ge=1, le=200), include_cancelled: bool = False,
              user=Depends(get_current_user)):
    """Every run (draft, released, optionally discarded), newest first, for History and the
    This Week 'earlier runs for this week' check."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT rr.id, rr.shipping_week, rr.run_at, rr.status, rr.pipeline_status, rr.engine_version,
                   rr.stores_served, rr.total_carts, rr.total_units, rr.finalized_at, rr.created_by,
                   ab.source_filename,
                   (rr.ipr_summary->'objective'->>'expected_weekly_retail')::numeric AS expected_weekly_retail
            FROM recommendation_runs rr
            LEFT JOIN availability_batches ab ON ab.id = rr.batch_id
            WHERE %s OR COALESCE(rr.status, '') NOT IN %s
            ORDER BY rr.run_at DESC NULLS LAST
            LIMIT %s
        """, (include_cancelled, CANCELLED_RUN_STATUSES, limit))
        return cur.fetchall()


class DiscardRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/runs/{run_id}/discard")
def discard_run(run_id: str, body: DiscardRequest,
                user=Depends(require_role("order_writer", "manager", "admin"))):
    """Mark a run cancelled (test runs, bad uploads). Its orders stay on record but the engine's
    duplicate-run control stops counting them as inbound. A released run can only be discarded
    by a manager or admin."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT status FROM recommendation_runs WHERE id = %s FOR UPDATE", (run_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, "Run not found")
        if (r["status"] or "") in CANCELLED_RUN_STATUSES:
            raise HTTPException(409, "Run is already discarded")
        if r["status"] == "exported" and user.get("role") not in ("manager", "admin"):
            raise HTTPException(403, "Only a manager can discard a released run")
        cur.execute("UPDATE recommendation_runs SET status = 'cancelled', last_activity_at = NOW() "
                    "WHERE id = %s", (run_id,))
        log_change(cur, run_id, "run", run_id,
                   f"discarded (was {r['status']}): {body.reason.strip()}", user.get("email"))
        return {"discarded": True, "previous_status": r["status"]}


@router.get("/runs/{run_id}/summary")
def run_summary(run_id: str, user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""SELECT engine_version, pipeline_status, ipr_summary, sales_cutoff_date, shipping_week
                       FROM recommendation_runs WHERE id = %s""", (run_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, "Run not found")
        cur.execute("""SELECT decision_type, COUNT(*) AS n FROM run_decisions
                       WHERE run_id = %s GROUP BY 1""", (run_id,))
        r = dict(r)
        r["decision_counts"] = {x["decision_type"]: x["n"] for x in cur.fetchall()}
        from services import sales_outlook
        r["sales_outlook"] = sales_outlook.compute(cur, r.get("ipr_summary"))
        return r


# Mirrors workers/ipr_engine.py STALE_SALES_WARN_DAYS / STALE_SALES_BLOCK_DAYS (Sec 5).
FRESH_WARN_DAYS = 9
FRESH_BLOCK_DAYS = 21


@router.get("/store-grades")
def store_grades(user=Depends(get_current_user)):
    """One row per store: family grades (Sec 6), weekly demand and 13-week conversion (Sec 11)
    from ipr_store_family, for the Stores screen."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT store_id,
                   jsonb_object_agg(ipr_family, grade) FILTER (WHERE grade IS NOT NULL) AS grades,
                   ROUND(SUM(weekly_velocity)::numeric, 1) AS weekly_units,
                   ROUND(AVG(conversion_13wk)::numeric, 3) AS conversion_13wk,
                   COUNT(*) FILTER (WHERE conversion_13wk IS NOT NULL AND conversion_13wk < 0.6) AS families_below_60,
                   array_remove(array_agg(DISTINCT service_status), NULL) AS service_statuses
            FROM ipr_store_family
            GROUP BY store_id
        """)
        return cur.fetchall()


@router.get("/freshness")
def data_freshness(user=Depends(get_current_user)):
    """Sales-through date for the This Week strip, read before any run exists."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT MAX(sale_date) AS mx FROM sales WHERE sale_date <= CURRENT_DATE")
        mx = cur.fetchone()["mx"]
        age = (date.today() - mx).days if mx else None
        return {"sales_through": str(mx) if mx else None, "age_days": age,
                "warn_days": FRESH_WARN_DAYS, "block_days": FRESH_BLOCK_DAYS}


@router.get("/runs/{run_id}/decisions")
def run_decisions(run_id: str, type: str = Query("block"), limit: int = Query(500, le=5000),
                  store_id: int | None = None, user=Depends(get_current_user)):
    if type not in DECISION_TYPES:
        raise HTTPException(400, f"type must be one of {DECISION_TYPES}")
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute(f"""
            SELECT d.decision_type, d.store_id, s.store_name, s.state, d.sku_id, k.description,
                   d.reason, d.data
            FROM run_decisions d
            LEFT JOIN stores s ON s.store_id = d.store_id
            LEFT JOIN skus k ON k.sku_id = d.sku_id
            WHERE d.run_id = %s AND d.decision_type = %s
              {"AND d.store_id = %s" if store_id is not None else ""}
            ORDER BY d.store_id NULLS FIRST, d.sku_id
            LIMIT %s
        """, (run_id, type, store_id, limit) if store_id is not None else (run_id, type, limit))
        return cur.fetchall()


@router.get("/runs/{run_id}/stores/{store_id}/evidence")
def store_evidence(run_id: str, store_id: int, user=Depends(get_current_user)):
    """Everything the engine decided about one store this run (lines, holds, review)."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT d.decision_type, d.sku_id, k.description, d.reason, d.data
            FROM run_decisions d LEFT JOIN skus k ON k.sku_id = d.sku_id
            WHERE d.run_id = %s AND d.store_id = %s
            ORDER BY d.decision_type, d.sku_id
        """, (run_id, store_id))
        rows = cur.fetchall()
        cur.execute("""SELECT ipr_family, grade, weekly_velocity, conversion_13wk,
                              conversion_confidence, service_status
                       FROM ipr_store_family WHERE store_id = %s ORDER BY weekly_velocity DESC""",
                    (store_id,))
        return {"decisions": rows, "families": cur.fetchall()}


@router.get("/settings")
def settings(user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT key, value, spec_ref, status, note, updated_at FROM ipr_settings ORDER BY key")
        return cur.fetchall()


# ── Editable policies (Policies screen) ───────────────────────────────────────
# Leadership 2026-11-20: rule and setting changes run through the IPR Manager. Managers and
# admins edit; everyone reads. Each change is validated against the catalog bounds, applies
# to the NEXT engine run (past runs are never rewritten), and is logged in change_log
# (entity 'ipr_setting') with who, when, old value, new value and the reason given.
import json as _json
from typing import Any

from services import settings_catalog as _cat


class SettingChange(BaseModel):
    value: Any = None
    reason: str = Field(min_length=5, max_length=500)


def _setting_history(cur, limit=60, key=None):
    cur.execute("""SELECT entity_id AS key, action, user_email, created_at FROM change_log
                   WHERE entity = 'ipr_setting' AND (%s::text IS NULL OR entity_id = %s)
                   ORDER BY created_at DESC LIMIT %s""", (key, key, limit))
    out = []
    for r in cur.fetchall():
        try:
            a = _json.loads(r["action"])
        except Exception:
            a = {"note": r["action"]}
        e = _cat.BY_KEY.get(r["key"]) or {}
        out.append({"key": r["key"], "label": e.get("label", r["key"]), "kind": e.get("kind"),
                    "by": r["user_email"], "at": r["created_at"], "old": a.get("old"),
                    "new": a.get("new"), "reason": a.get("reason"), "was_default": a.get("was_default")})
    return out


@router.get("/settings/catalog")
def settings_catalog(user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT key, value, spec_ref, status, note, updated_at FROM ipr_settings")
        rows = {r["key"]: dict(r) for r in cur.fetchall()}
        last = {}
        for h in _setting_history(cur, 400):
            last.setdefault(h["key"], {"by": h["by"], "at": h["at"], "reason": h["reason"]})
        out = _cat.describe(rows, last)
        out["can_edit"] = _role(user) in ("manager", "admin")
        return out


@router.get("/settings/history")
def settings_history(limit: int = Query(60, ge=1, le=400), user=Depends(get_current_user)):
    with get_db() as conn:
        return _setting_history(cursor(conn), limit)


@router.put("/settings/{key}")
def update_setting(key: str, body: SettingChange, user=Depends(require_role("manager", "admin"))):
    """Change one policy. Manager / admin only."""
    try:
        new = _cat.validate(key, body.value)
    except ValueError as e:
        raise HTTPException(422, str(e))
    entry = _cat.BY_KEY[key]
    email = user.get("email") if isinstance(user, dict) else None
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT value FROM ipr_settings WHERE key = %s FOR UPDATE", (key,))
        row = cur.fetchone()
        old = row["value"] if row else entry["default"]
        if row is not None and old == new:
            raise HTTPException(409, "That is already the current value. Nothing was changed.")
        stamp = f"edited by {email or 'manager'} {date.today().isoformat()}"
        cur.execute("""INSERT INTO ipr_settings (key, value, spec_ref, status, note)
                       VALUES (%s, %s::jsonb, %s, %s, %s)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                           status = EXCLUDED.status, updated_at = now()""",
                    (key, _json.dumps(new), "Policies screen", stamp, entry["help"]))
        log_change(cur, None, "ipr_setting", key,
                   _json.dumps({"old": old, "new": new, "reason": body.reason.strip(),
                                "was_default": row is None}, default=str), email)
        return {"key": key, "value": new, "old": old, "status": stamp,
                "applies": "next engine run"}


# ── Release report (the email the team gets when a week's orders are final) ───
# Preview for anyone signed in. "Send to me" mails it to the person asking, marked TEST, so
# the report can be checked on a real run before the team list is switched on. Without SMTP
# settings the message is queued in email_outbox, never lost.
@router.get("/runs/{run_id}/release-report")
def release_report(run_id: str, user=Depends(get_current_user)):
    from services import release_report as rr
    with get_db() as conn:
        d = rr.build(cursor(conn), run_id)
        if not d:
            raise HTTPException(404, "Run not found")
        out = rr.render(d, test=True)
        from services import emails
        out["smtp_configured"] = emails.smtp_configured()
        out["your_email"] = user.get("email") if isinstance(user, dict) else None
        return out


@router.post("/runs/{run_id}/release-report/send-to-me")
def release_report_send_to_me(run_id: str, user=Depends(require_role("order_writer", "manager", "admin"))):
    from services import emails, release_report as rr
    to = user.get("email") if isinstance(user, dict) else None
    if not to:
        raise HTTPException(400, "Your login has no email address")
    with get_db() as conn:
        cur = cursor(conn)
        d = rr.build(cur, run_id)
        if not d:
            raise HTTPException(404, "Run not found")
        out = rr.render(d, test=True)
        status = emails.send_or_queue(cur, to, out["subject"], out["text"], "release_report_test",
                                      {"run_id": run_id}, html=out["html"])
        return {"to": to, "status": status, "smtp_configured": emails.smtp_configured()}


# ── Leadership report: the weekly one-page HTML summary, same layout every week ──
# Numbers and rule results are computed from the saved order; the AI writes only the overview.
# The overview is kept per order (ipr_ai_reviews, kind 'leadership_overview') and rewritten
# only when the order's figures change or refresh=true, so the document reads the same each
# time it is downloaded.
@router.get("/runs/{run_id}/leadership-report")
def leadership_report(run_id: str, refresh: bool = False, user=Depends(get_current_user)):
    import json
    from fastapi.responses import Response
    from services import leadership_report as lr
    with get_db() as conn:
        cur = cursor(conn)
        f = lr.facts(cur, run_id)
        if not f:
            raise HTTPException(404, "Run not found")
        fp = lr.fingerprint(f)
        cur.execute("""SELECT report FROM ipr_ai_reviews WHERE run_id = %s AND kind = 'leadership_overview'
                       ORDER BY created_at DESC LIMIT 1""", (run_id,))
        row = cur.fetchone()
    ai = None
    if row and not refresh and (row["report"] or {}).get("fingerprint") == fp:
        ai = row["report"].get("overview")
    if ai is None:
        try:
            ai = lr.overview(f)          # no DB connection is held during the model call
        except HTTPException:
            ai = None                    # the document still generates, with computed text
        if ai:
            from services import ipr_ai
            with get_db() as conn:
                cursor(conn).execute(
                    """INSERT INTO ipr_ai_reviews (run_id, kind, model, verdict, report, created_by)
                       VALUES (%s, 'leadership_overview', %s, NULL, %s::jsonb, %s)""",
                    (run_id, ipr_ai.MODEL_MAIN, json.dumps({"fingerprint": fp, "overview": ai}, default=str),
                     user.get("email") if isinstance(user, dict) else None))
    html = lr.render(f, ai, generated_at=date.today())
    name = f"TPC_Home_Depot_Orders_Week_{f['week']}.html"
    return Response(html, media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "X-Overview": "ai" if ai else "computed"})


# ── Production review: the growing team's companion to the leadership report ──
# What stores need against this week's availability, product by product. Same pattern: figures
# computed from the saved order, AI writes only the recommendations (kind 'production_overview').
@router.get("/runs/{run_id}/production-review")
def production_review(run_id: str, refresh: bool = False, user=Depends(get_current_user)):
    import json
    from fastapi.responses import Response
    from services import production_review as pr
    with get_db() as conn:
        cur = cursor(conn)
        f = pr.facts(cur, run_id)
        if not f:
            raise HTTPException(404, "Run not found")
        fp = pr.fingerprint(f)
        cur.execute("""SELECT report FROM ipr_ai_reviews WHERE run_id = %s AND kind = 'production_overview'
                       ORDER BY created_at DESC LIMIT 1""", (run_id,))
        row = cur.fetchone()
    ai = None
    if not f.get("missing"):
        if row and not refresh and (row["report"] or {}).get("fingerprint") == fp:
            ai = row["report"].get("overview")
        if ai is None:
            try:
                ai = pr.overview(f)      # no DB connection is held during the model call
            except HTTPException:
                ai = None
            if ai:
                from services import ipr_ai
                with get_db() as conn:
                    cursor(conn).execute(
                        """INSERT INTO ipr_ai_reviews (run_id, kind, model, verdict, report, created_by)
                           VALUES (%s, 'production_overview', %s, NULL, %s::jsonb, %s)""",
                        (run_id, ipr_ai.MODEL_MAIN, json.dumps({"fingerprint": fp, "overview": ai}, default=str),
                         user.get("email") if isinstance(user, dict) else None))
    html = pr.render(f, ai, generated_at=date.today())
    name = f"TPC_Production_Review_Week_{f['week']}.html"
    return Response(html, media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "X-Overview": "ai" if ai else "computed"})


class ReactivationRequest(BaseModel):
    store_id: int
    ipr_family: str = Field(min_length=1, max_length=60)
    diagnosis: str = Field(min_length=5, max_length=2000)
    corrective_action: str = Field(min_length=5, max_length=2000)
    test_units: int = Field(gt=0, le=2000)
    expires_at: date | None = None
    review_date: date | None = None


class Rejection(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


def _role(user):
    return (user or {}).get("role") if isinstance(user, dict) else None


@router.post("/reactivations", status_code=201)
def request_reactivation(req: ReactivationRequest, user=Depends(get_current_user)):
    """Structured reactivation request for a blocked/held store x family (§11.5)."""
    if _role(user) in (None, "viewer"):
        raise HTTPException(403, "Viewers cannot request reactivation")
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""SELECT grade, conversion_13wk, service_status FROM ipr_store_family
                       WHERE store_id = %s AND ipr_family = %s""", (req.store_id, req.ipr_family))
        sf = cur.fetchone()
        if not sf:
            raise HTTPException(404, "No graded store x family record — run the engine first")
        if sf["service_status"] not in ("BLOCKED", "REVIEW"):
            raise HTTPException(400, f"This family is {sf['service_status']} — nothing to reactivate")
        cur.execute("""SELECT id FROM ipr_reactivations WHERE store_id = %s AND ipr_family = %s
                       AND status IN ('pending_dsm','pending_director')""", (req.store_id, req.ipr_family))
        if cur.fetchone():
            raise HTTPException(409, "A reactivation request for this store and family is already pending")
        today = date.today()
        cur.execute("""
            INSERT INTO ipr_reactivations (store_id, ipr_family, conversion_13wk, grade, diagnosis,
                corrective_action, test_units, expires_at, review_date, requested_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (req.store_id, req.ipr_family, sf["conversion_13wk"], sf["grade"], req.diagnosis.strip(),
              req.corrective_action.strip(), req.test_units,
              req.expires_at or today + timedelta(days=30), req.review_date or today + timedelta(days=21),
              user.get("email")))
        return {"id": str(cur.fetchone()["id"]), "status": "pending_dsm"}


@router.get("/reactivations")
def list_reactivations(status: str | None = None, user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute(f"""
            SELECT r.*, s.store_name, s.state FROM ipr_reactivations r
            LEFT JOIN stores s ON s.store_id = r.store_id
            {"WHERE r.status = %s" if status else ""}
            ORDER BY r.requested_at DESC LIMIT 500
        """, (status,) if status else ())
        return cur.fetchall()


@router.post("/reactivations/{rid}/approve")
def approve_reactivation(rid: str, user=Depends(get_current_user)):
    role, email = _role(user), user.get("email")
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT * FROM ipr_reactivations WHERE id = %s FOR UPDATE", (rid,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, "Request not found")
        if r["status"] == "pending_dsm":
            if role not in DSM_ROLES:
                raise HTTPException(403, "Step 1 requires the District Sales Manager")
            cur.execute("""UPDATE ipr_reactivations SET status='pending_director', dsm_by=%s, dsm_at=now()
                           WHERE id=%s""", (email, rid))
            return {"status": "pending_director"}
        if r["status"] == "pending_director":
            if role not in DIRECTOR_ROLES:
                raise HTTPException(403, "Step 2 requires the Home Depot Sales Director")
            if email == r["dsm_by"]:
                raise HTTPException(403, "The same person cannot sign both approval steps")
            cur.execute("""UPDATE ipr_reactivations SET status='approved', director_by=%s, director_at=now()
                           WHERE id=%s""", (email, rid))
            return {"status": "approved"}
        raise HTTPException(400, f"Request is already {r['status']}")


@router.post("/reactivations/{rid}/reject")
def reject_reactivation(rid: str, body: Rejection, user=Depends(get_current_user)):
    if _role(user) not in set(DSM_ROLES) | set(DIRECTOR_ROLES):
        raise HTTPException(403, "Only the DSM or Sales Director can reject")
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""UPDATE ipr_reactivations SET status='rejected', rejected_by=%s, rejected_at=now(),
                              reject_reason=%s
                       WHERE id=%s AND status IN ('pending_dsm','pending_director') RETURNING id""",
                    (user.get("email"), body.reason.strip(), rid))
        if not cur.fetchone():
            raise HTTPException(400, "Request not found or no longer pending")
        return {"status": "rejected"}


# ── §25 independent AI review + §20 cart-config consolidation ────────────────

@router.post("/runs/{run_id}/ai-review")
def ai_review(run_id: str, user=Depends(require_role("order_writer", "manager", "admin"))):
    """Run the §25 LLM reviewer on this run's evidence and store the structured verdict.
    The service manages its own transactions so no DB locks are held during the model call."""
    from routers.ai_explanations import _check_ai_budget
    from services.ipr_reviewer import run_ai_review
    with get_db() as conn:
        _check_ai_budget(cursor(conn), user, "audit")
    report = run_ai_review(run_id, user.get("email"))
    with get_db() as conn:
        log_change(cursor(conn), run_id, "run", run_id,
                   f"AI review: {report.get('verdict')}", user.get("email"))
    return report


@router.post("/runs/{run_id}/consolidate-configs")
def consolidate_configs(run_id: str, apply: bool = False,
                        user=Depends(require_role("order_writer", "manager", "admin"))):
    """Cart-config consolidation (§20). apply=false previews; apply=true applies ONLY the
    demand-neutral merges (kit alignment on kit-flexible 1M SKUs / shelf order — same SKUs,
    trays and units per store). Anything else — including finished-product kit differences —
    comes back as a proposal for human approval, never applied. The merge transaction holds
    the run row FOR UPDATE so a concurrent release serializes against it."""
    from routers.ai_explanations import _check_ai_budget
    from services.ipr_reviewer import run_consolidation
    with get_db() as conn:
        _check_ai_budget(cursor(conn), user, "audit")
    result = run_consolidation(run_id, user.get("email"), apply)
    if result.get("applied"):
        with get_db() as conn:
            log_change(cursor(conn), run_id, "run", run_id,
                       f"config consolidation applied: {result['verdict']}", user.get("email"))
    return result


@router.post("/runs/{run_id}/scenario-audit")
def scenario_audit(run_id: str, user=Depends(require_role("order_writer", "manager",
                                                          "sales_director", "admin"))):
    """Independent audit of every scenario applied to a run (check 4 of the chain:
    parser LLM → human approver ≠ submitter → bounded deterministic apply → this audit)."""
    from routers.ai_explanations import _check_ai_budget
    from services.ipr_reviewer import run_scenario_audit
    with get_db() as conn:
        _check_ai_budget(cursor(conn), user, "audit")
    report = run_scenario_audit(run_id, user.get("email"))
    if report.get("id"):
        with get_db() as conn:
            log_change(cursor(conn), run_id, "run", run_id,
                       f"scenario audit: {report.get('verdict')}", user.get("email"))
    return report


@router.get("/runs/{run_id}/ai-reviews")
def list_ai_reviews(run_id: str, user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""SELECT id, kind, model, verdict, report, created_by, created_at
                       FROM ipr_ai_reviews WHERE run_id = %s
                       ORDER BY created_at DESC LIMIT 20""", (run_id,))
        return cur.fetchall()


# ── §29 post-delivery measurement (7–28 day shipment response; leadership 11/20) ──

@router.post("/post-delivery/compute")
def post_delivery_compute(user=Depends(require_role("order_writer", "manager", "admin"))):
    """Rebuild the shipment-response measurement from the sales/delivery record. Runs
    automatically after the Monday ingest; this endpoint is the manual trigger."""
    from services.post_delivery import compute
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT to_regclass('public.delivery_performance') IS NOT NULL AS ok")
        if not cur.fetchone()["ok"]:
            raise HTTPException(503, "Run the post_delivery_2026_11_21 migration first")
        cur.execute("SELECT value FROM ipr_settings WHERE key = 'post_delivery_window_weeks'")
        r = cur.fetchone()
        window = int(r["value"]) if r else 26
        result = compute(cur, window)
        log_change(cur, None, "analysis", "post_delivery",
                   f"recomputed: {result.get('cohorts', 0)} cohorts", user.get("email"))
        return result


@router.get("/post-delivery/summary")
def post_delivery_summary(user=Depends(get_current_user)):
    from services.post_delivery import network_summary
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT to_regclass('public.delivery_performance') IS NOT NULL AS ok")
        if not cur.fetchone()["ok"]:
            raise HTTPException(503, "Post-delivery measurement is not set up yet (migration pending)")
        return network_summary(cur)


@router.get("/family-map")
def family_map(user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""SELECT fm.sku_id, k.description, k.family, k.legacy_product_group,
                              fm.ipr_family, fm.approved
                       FROM ipr_family_map fm LEFT JOIN skus k ON k.sku_id = fm.sku_id
                       ORDER BY fm.ipr_family, fm.sku_id""")
        return cur.fetchall()
