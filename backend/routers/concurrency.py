"""
Multi-user concurrency endpoints (Phases 2-4 of MultiUser_Design.md).

  Phase 2  change-feed polling   GET  /runs/{run_id}/changes?since=<iso-ts>
  Phase 3  presence heartbeat    POST /runs/{run_id}/heartbeat
                                 GET  /runs/{run_id}/presence
           store soft claims     POST /orders/{order_id}/claim
                                 POST /orders/{order_id}/release
  Phase 4  active plans          GET  /active-plans

Shared helpers (log_change, bump_activity, claim_is_held_by_other) are imported by the mutating
routers (carts/config_editor/permanent_carts/orders) so every write records a change_log row and
bumps recommendation_runs.last_activity_at. Keeping them here avoids a circular import with db.

TTLs:
  PRESENCE_TTL_SEC = 45   — a user is "present" if their heartbeat is < 45s old (heartbeat ~15s).
  CLAIM_TTL_SEC    = 120  — a store claim auto-expires after 2 min idle; NEVER trust client release.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, Query
from db import get_db, cursor
from routers.auth import get_current_user, require_role

router = APIRouter()

PRESENCE_TTL_SEC = 45
CLAIM_TTL_SEC = 120


# ── Shared write-side helpers (imported by mutating routers) ──────────────────

def log_change(cur, run_id, entity: str, entity_id, action: str, user_email):
    """Append one row to change_log + bump the run's last_activity_at. Call inside the SAME
    transaction/cursor as the mutation so it commits atomically with it. run_id may be None
    (e.g. a cart edit where only order_id is known) — then only the change row is written if a
    run_id is resolvable; callers that have the run should pass it so polling sees the change."""
    cur.execute(
        """INSERT INTO change_log (run_id, entity, entity_id, action, user_email)
           VALUES (%s,%s,%s,%s,%s)""",
        (str(run_id) if run_id else None, entity,
         str(entity_id) if entity_id is not None else None, action,
         (user_email if isinstance(user_email, str) else None)),
    )
    if run_id:
        bump_activity(cur, run_id)


def bump_activity(cur, run_id):
    """Mark the run as just-touched (drives the Active Plans dashboard ordering)."""
    if not run_id:
        return
    cur.execute(
        "UPDATE recommendation_runs SET last_activity_at = now() WHERE id = %s",
        (str(run_id),),
    )


def run_id_for_order(cur, order_id: str):
    cur.execute("SELECT run_id FROM orders WHERE id = %s", (order_id,))
    r = cur.fetchone()
    return r["run_id"] if r else None


def claim_blocker(cur, order_id: str, user_email: str):
    """Return the email of ANOTHER user who currently holds a live claim on this order, or None.
    A claim is live only if claimed_at is within CLAIM_TTL_SEC (expired claims are ignored)."""
    cur.execute(
        """SELECT claimed_by, claimed_at,
                  (claimed_at > now() - (%s || ' seconds')::interval) AS live
           FROM orders WHERE id = %s""",
        (CLAIM_TTL_SEC, order_id),
    )
    r = cur.fetchone()
    if not r or not r["claimed_by"] or not r["live"]:
        return None
    if r["claimed_by"] == user_email:
        return None
    return r["claimed_by"]


# ── Phase 2: change feed ──────────────────────────────────────────────────────

@router.get("/runs/{run_id}/changes")
def get_changes(run_id: str, since: str | None = Query(default=None),
                user=Depends(get_current_user)):
    """Changes to this run since `since` (ISO-8601 ts). Returns rows + a `now` server timestamp
    the client echoes back as the next `since` (avoids client/server clock skew). When `since` is
    omitted, returns the latest server time and no rows (used to prime the poller)."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT now() AS now")
        now_ts = cur.fetchone()["now"]
        rows = []
        if since:
            cur.execute(
                """SELECT id, entity, entity_id, action, user_email, created_at
                   FROM change_log
                   WHERE run_id = %s AND created_at > %s
                   ORDER BY created_at ASC, id ASC
                   LIMIT 500""",
                (run_id, since),
            )
            rows = cur.fetchall()
    return {"now": now_ts.isoformat() if hasattr(now_ts, "isoformat") else str(now_ts),
            "changes": rows}


# ── Phase 3: presence ─────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/heartbeat")
def heartbeat(run_id: str, user=Depends(get_current_user)):
    """Upsert the caller's presence for this run. Called by the frontend ~every 15s while a plan
    is open. Also returns the current live-presence list so one call both announces and reads."""
    email = user.get("email") if isinstance(user, dict) else None
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT 1 FROM recommendation_runs WHERE id = %s", (run_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Run not found")
        cur.execute(
            """INSERT INTO plan_presence (run_id, user_email, last_seen)
               VALUES (%s,%s, now())
               ON CONFLICT (run_id, user_email)
               DO UPDATE SET last_seen = now()""",
            (run_id, email),
        )
        present = _live_presence(cur, run_id, exclude=email)
    return {"ok": True, "present": present}


@router.get("/runs/{run_id}/presence")
def presence(run_id: str, user=Depends(get_current_user)):
    """Live presence list for a run (excludes stale heartbeats > PRESENCE_TTL_SEC)."""
    with get_db() as conn:
        cur = cursor(conn)
        return {"present": _live_presence(cur, run_id)}


def _live_presence(cur, run_id: str, exclude: str | None = None):
    cur.execute(
        """SELECT user_email, last_seen
           FROM plan_presence
           WHERE run_id = %s
             AND last_seen > now() - (%s || ' seconds')::interval
           ORDER BY user_email""",
        (run_id, PRESENCE_TTL_SEC),
    )
    out = []
    for r in cur.fetchall():
        if exclude and r["user_email"] == exclude:
            continue
        out.append({"user_email": r["user_email"],
                    "last_seen": r["last_seen"].isoformat() if hasattr(r["last_seen"], "isoformat") else str(r["last_seen"])})
    return out


# ── Phase 3: store soft claims ────────────────────────────────────────────────

@router.post("/orders/{order_id}/claim")
def claim_order(order_id: str, user=Depends(require_role("order_writer", "manager", "admin"))):
    """Claim a store for editing. Soft + auto-expiring: if someone else holds a LIVE claim
    (within CLAIM_TTL_SEC) this returns 409 with who holds it; an EXPIRED claim is taken over.
    Taking your own claim again just refreshes the timestamp (heartbeat for the claim)."""
    email = user.get("email") if isinstance(user, dict) else None
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT run_id FROM orders WHERE id = %s FOR UPDATE", (order_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Order not found")
        holder = claim_blocker(cur, order_id, email)
        if holder:
            raise HTTPException(409, f"{holder} is editing this store")
        cur.execute(
            "UPDATE orders SET claimed_by = %s, claimed_at = now() WHERE id = %s",
            (email, order_id),
        )
        log_change(cur, row["run_id"], "order", order_id, "claim", email)
    return {"claimed": True, "by": email}


@router.post("/orders/{order_id}/release")
def release_order(order_id: str, user=Depends(require_role("order_writer", "manager", "admin"))):
    """Release a store you hold (on editor close). Only clears the claim if YOU hold it; a claim
    you no longer hold (someone took over after TTL) is left alone. TTL expiry is the real
    safety net — this is just the courteous fast-path."""
    email = user.get("email") if isinstance(user, dict) else None
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("SELECT run_id, claimed_by FROM orders WHERE id = %s FOR UPDATE", (order_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Order not found")
        if row["claimed_by"] == email:
            cur.execute(
                "UPDATE orders SET claimed_by = NULL, claimed_at = NULL WHERE id = %s",
                (order_id,),
            )
            log_change(cur, row["run_id"], "order", order_id, "release", email)
            return {"released": True}
    return {"released": False}


# ── Phase 4: active plans dashboard ───────────────────────────────────────────

@router.get("/active-plans")
def active_plans(user=Depends(get_current_user)):
    """Draft (in-progress) runs with creator, last activity, #stores/#carts, and who is currently
    in each (live presence). Lets writers join an existing plan instead of starting a duplicate."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute(
            """SELECT rr.id, rr.batch_id, rr.shipping_week, rr.status, rr.created_by,
                      rr.last_activity_at, rr.stores_served, rr.total_carts, rr.total_units,
                      ab.source_filename
               FROM recommendation_runs rr
               LEFT JOIN availability_batches ab ON ab.id = rr.batch_id
               WHERE rr.status = 'draft'
               ORDER BY rr.last_activity_at DESC NULLS LAST, rr.run_at DESC
               LIMIT 50"""
        )
        runs = cur.fetchall()
        for r in runs:
            r["last_activity_at"] = (r["last_activity_at"].isoformat()
                                     if r.get("last_activity_at") and hasattr(r["last_activity_at"], "isoformat")
                                     else r.get("last_activity_at"))
            r["present"] = _live_presence(cur, r["id"])
    return {"plans": runs}
