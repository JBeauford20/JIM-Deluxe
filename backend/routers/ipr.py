"""IPR v2 read API — the run's §2 objective readout, §15 allocation outputs, and the
§25 evidence trail (run_decisions) behind every shipped line, block, and hold."""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from db import get_db, cursor
from routers.auth import get_current_user

router = APIRouter()

DECISION_TYPES = ("block", "review_hold", "allocation_shortfall", "validation", "review", "line",
                  "duplicate_run", "stopped_13wk", "reduced_13wk", "gap_fill", "low_value_cart", "deferred_economics",
                  "deferral", "reactivation")
# Spec §11.3 / §27: commercial reactivation is approved by the assigned DSM and then the
# Home Depot Sales Director — never by the IPR/development team. 'admin' may act as either
# role (Josh), but one person can never sign both steps.
DSM_ROLES = ("dsm", "admin")
DIRECTOR_ROLES = ("sales_director", "admin")


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
        return r


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


@router.get("/family-map")
def family_map(user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""SELECT fm.sku_id, k.description, k.family, k.legacy_product_group,
                              fm.ipr_family, fm.approved
                       FROM ipr_family_map fm LEFT JOIN skus k ON k.sku_id = fm.sku_id
                       ORDER BY fm.ipr_family, fm.sku_id""")
        return cur.fetchall()
