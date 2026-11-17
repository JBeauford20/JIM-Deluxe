"""IPR v2 read API — the run's §2 objective readout, §15 allocation outputs, and the
§25 evidence trail (run_decisions) behind every shipped line, block, and hold."""
from fastapi import APIRouter, Depends, HTTPException, Query
from db import get_db, cursor
from routers.auth import get_current_user

router = APIRouter()

DECISION_TYPES = ("block", "review_hold", "allocation_shortfall", "validation", "review", "line")


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


@router.get("/family-map")
def family_map(user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""SELECT fm.sku_id, k.description, k.family, k.legacy_product_group,
                              fm.ipr_family, fm.approved
                       FROM ipr_family_map fm LEFT JOIN skus k ON k.sku_id = fm.sku_id
                       ORDER BY fm.ipr_family, fm.sku_id""")
        return cur.fetchall()
