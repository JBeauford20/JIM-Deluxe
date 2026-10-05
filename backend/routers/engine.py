"""
Engine router — queues velocity + recommendation jobs.
The worker runs them independently. Closing the browser doesn't stop a job.
"""
import uuid
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel
from db import get_db, cursor
from routers.auth import require_role
import sys, os

# Workers live in backend/workers/ — deployed with the repo
sys.path.insert(0, str(os.path.join(os.path.dirname(__file__), '..', 'workers')))

router = APIRouter()

class GenerateRequest(BaseModel):
    batch_id:     str
    shipping_week: str
    scenario_id:  str | None = None

@router.post("/generate")
def queue_recommendation_run(
    req: GenerateRequest,
    background: BackgroundTasks,
    user=Depends(require_role("order_writer", "manager", "admin"))
):
    """Queue a recommendation run. Returns job_id immediately."""
    with get_db() as conn:
        cur = cursor(conn)

        # Check for duplicate active job
        cur.execute("""
            SELECT id FROM engine_jobs
            WHERE batch_id = %s AND status IN ('queued','running')
        """, (req.batch_id,))
        if cur.fetchone():
            raise HTTPException(409, "A recommendation run is already in progress for this batch")

        job_id = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO engine_jobs
                (id, job_type, status, batch_id, shipping_week, created_by)
            VALUES (%s,'recommendations','queued',%s,%s,%s)
        """, (job_id, req.batch_id, req.shipping_week, user["email"]))

    background.add_task(_run_recommendations, job_id, req.batch_id,
                        req.shipping_week, req.scenario_id)
    return {"job_id": job_id, "status": "queued"}


@router.get("/jobs/{job_id}")
def job_status(job_id: str, user=Depends(require_role("viewer","order_writer","manager","admin"))):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT * FROM engine_jobs WHERE id = %s", (job_id,))
        job = cur.fetchone()
        if not job:
            raise HTTPException(404, "Job not found")
        return job


@router.post("/velocity")
def queue_velocity(
    background: BackgroundTasks,
    user=Depends(require_role("manager", "admin"))
):
    """Queue a full velocity scoring run."""
    with get_db() as conn:
        cur = cursor(conn)
        job_id = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO engine_jobs (id, job_type, status, created_by)
            VALUES (%s,'velocity_scoring','queued',%s)
        """, (job_id, user["email"]))
    background.add_task(_run_velocity, job_id)
    return {"job_id": job_id, "status": "queued"}


# ── Background workers ────────────────────────────────────────

def _update_job(job_id, status, progress_pct=None, progress_msg=None, error=None, run_id=None):
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            UPDATE engine_jobs SET
                status       = %s,
                progress_pct = COALESCE(%s, progress_pct),
                progress_msg = COALESCE(%s, progress_msg),
                error_text   = %s,
                run_id       = COALESCE(%s, run_id),
                started_at   = CASE WHEN %s = 'running' THEN now() ELSE started_at END,
                completed_at = CASE WHEN %s IN ('complete','failed') THEN now() ELSE completed_at END
            WHERE id = %s
        """, (status, progress_pct, progress_msg, error, run_id, status, status, job_id))


def _run_recommendations(job_id, batch_id, shipping_week, scenario_id):
    _update_job(job_id, 'running', 0, 'Starting recommendation engine...')
    try:
        from recommendation_engine import run as engine_run
        from datetime import date
        _update_job(job_id, 'running', 10, 'Loading data...')
        run_id = engine_run(batch_id, shipping_week=date.fromisoformat(shipping_week))
        _update_job(job_id, 'complete', 100, 'Done', run_id=run_id)
    except Exception as e:
        _update_job(job_id, 'failed', error=str(e))


def _run_velocity(job_id):
    _update_job(job_id, 'running', 0, 'Running velocity engine...')
    try:
        from velocity_engine import run_velocity_scoring
        from group_velocity_engine import run_group_scoring
        _update_job(job_id, 'running', 40, 'Overall velocity...')
        run_velocity_scoring(verbose=False)
        _update_job(job_id, 'running', 80, 'SKU group velocity...')
        run_group_scoring(verbose=False)
        _update_job(job_id, 'complete', 100, 'Done')
    except Exception as e:
        _update_job(job_id, 'failed', error=str(e))
