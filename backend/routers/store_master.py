"""Store master — the Home Depot Store List (first sheet) as the app's store handling rules.

  GET  /summary   counts by rule / cross dock / do-not-ship, rule definitions, last load
  GET  /stores    the stores that carry any special handling (rule, cross dock, blocked)
  GET  /loads     load history
  POST /preview   upload the workbook -> what WOULD change (nothing written)
  POST /apply     upload the workbook -> write it (manager / admin; logged with full change list)

Parsing, change planning and writing live in services/store_master.py (shared with the
CLI loader). What each restriction means lives in workers/store_rules.py.
"""
import pathlib

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from db import cursor, get_db
from routers.auth import get_current_user, require_role
from services import store_master as sm

router = APIRouter()
store_rules = sm.store_rules
BLOCK_STATUSES = ("do_not_ship", "paused", "inactive", "hold")
MAX_BYTES = 20 * 1024 * 1024


def _email(user):
    return user.get("email", "system") if isinstance(user, dict) else "system"


def _has_table(cur, name):
    cur.execute("SELECT to_regclass(%s) AS t", (f"public.{name}",))
    return bool(cur.fetchone()["t"])


def _rules(cur):
    cur.execute("SELECT value FROM ipr_settings WHERE key = 'store_sku_rules'")
    row = cur.fetchone()
    return store_rules.merged_rules(row["value"] if row else None)


def _describe(code, rule):
    allow = rule.get("allow_only") or {}
    parts = []
    if not rule.get("routine", True):
        parts.append("No routine replenishment orders.")
    if rule.get("directed_only"):
        parts.append("Holiday / promo product only when the store is explicitly added to that program.")
    if allow and rule.get("routine", True):
        ok = (allow.get("families") or []) + (allow.get("legacy_groups") or []) + (allow.get("ipr_families") or [])
        if allow.get("holiday"):
            ok.append("Holiday")
        parts.append("Routine orders limited to: " + ", ".join(ok) + ".")
    if rule.get("deny_genus"):
        parts.append("Never ships: " + ", ".join(rule["deny_genus"]) + " (checked on the Aster variety of "
                     "every availability line; these carts get their own cart config tagged "
                     f"'{rule.get('tag') or rule.get('label')}').")
    return {"code": code, "label": rule.get("label", code), "effect": " ".join(parts) or "No effect."}


@router.get("/summary")
def summary(user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        db, have = sm.load_db_stores(cur)
        rules = _rules(cur)
        loaded = sm.migration_applied(have)
        by_rule, by_cd, by_status = {}, {}, {}
        for s in db.values():
            if s.get("sku_rule"):
                by_rule[s["sku_rule"]] = by_rule.get(s["sku_rule"], 0) + 1
            if s.get("cross_dock"):
                by_cd[s["cross_dock"]] = by_cd.get(s["cross_dock"], 0) + 1
            st = s.get("store_status") or "active"
            if st != "active":
                by_status[st] = by_status.get(st, 0) + 1
        last = None
        if _has_table(cur, "store_master_loads"):
            cur.execute("""SELECT id, loaded_at, loaded_by, source_file, sheet_name, rows_read,
                                  stores_changed FROM store_master_loads
                           ORDER BY loaded_at DESC LIMIT 1""")
            last = cur.fetchone()
        return {
            "migration_applied": loaded,
            "master_loaded": bool(last),
            "last_load": last,
            "stores_total": len(db),
            "stores_with_rule": sum(by_rule.values()),
            "stores_cross_dock": sum(by_cd.values()),
            "stores_blocked": sum(n for k, n in by_status.items() if k in BLOCK_STATUSES),
            "by_rule": [{**_describe(code, rules.get(code) or store_rules.UNKNOWN_RULE), "stores": n}
                        for code, n in sorted(by_rule.items(), key=lambda kv: -kv[1])],
            "by_cross_dock": [{"cross_dock": k, "stores": n} for k, n in sorted(by_cd.items(), key=lambda kv: -kv[1])],
            "by_status": [{"status": k, "stores": n} for k, n in sorted(by_status.items(), key=lambda kv: -kv[1])],
            "rule_definitions": [_describe(code, r) for code, r in sorted(rules.items())],
        }


@router.get("/stores")
def handled_stores(user=Depends(get_current_user)):
    """Every store with special handling: a rule, a cross dock, or a non-active status."""
    with get_db() as conn:
        cur = cursor(conn)
        db, have = sm.load_db_stores(cur)
        rules = _rules(cur)
        out = []
        for sid, s in sorted(db.items()):
            st = s.get("store_status") or "active"
            if not (s.get("sku_rule") or s.get("cross_dock") or st != "active"):
                continue
            r = store_rules.rule_for(s, rules)
            out.append({
                "store_id": sid, "store_name": s.get("store_name"), "city": s.get("city"),
                "state": s.get("state"), "division": s.get("division"), "merchant": s.get("merchant"),
                "store_status": st, "status_note": s.get("status_note"),
                "master_status": s.get("master_status"), "sku_rule": s.get("sku_rule"),
                "rule_label": (r or {}).get("label"), "sku_restriction": s.get("sku_restriction"),
                "cross_dock": s.get("cross_dock"), "merchandiser": s.get("merchandiser"),
                "routine_orders": ("blocked" if st in BLOCK_STATUSES or store_rules.routine_blocked(r)
                                   else "limited" if r else "normal"),
            })
        return out


@router.get("/loads")
def loads(user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)
        if not _has_table(cur, "store_master_loads"):
            return []
        cur.execute("""SELECT id, loaded_at, loaded_by, source_file, sheet_name, rows_read,
                              stores_changed, summary FROM store_master_loads
                       ORDER BY loaded_at DESC LIMIT 25""")
        return cur.fetchall()


async def _parse_upload(file: UploadFile):
    ext = pathlib.Path(file.filename or "").suffix.lower()
    if ext not in (".xlsx", ".xlsm"):
        raise HTTPException(400, "Upload the store master as an Excel workbook (.xlsx)")
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File is larger than 20 MB")
    try:
        return sm.parse_workbook(data, pathlib.Path(file.filename or "store master").name)
    except Exception as e:                       # not a readable workbook
        raise HTTPException(400, f"Could not read the workbook: {type(e).__name__}")


@router.post("/preview")
async def preview(file: UploadFile = File(...), update_descriptive: bool = Form(False),
                  user=Depends(require_role("order_writer", "manager", "admin"))):
    """What loading this workbook would change. Nothing is written."""
    parsed = await _parse_upload(file)
    with get_db() as conn:
        cur = cursor(conn)
        db, have = sm.load_db_stores(cur)
        pl = sm.plan(parsed, db, update_descriptive=update_descriptive)
        out = sm.public_plan(pl)
        out["migration_applied"] = sm.migration_applied(have)
        out["can_apply"] = out["migration_applied"] and not pl["problems"] and bool(pl["qa"]["rows_accepted"])
        return out


@router.post("/apply")
async def apply(file: UploadFile = File(...), update_descriptive: bool = Form(False),
                user=Depends(require_role("manager", "admin"))):
    """Load the workbook. Manager / admin only — store handling rules are hard blocks for the
    order writer, so changing them runs through the manager. Logged with every change."""
    parsed = await _parse_upload(file)
    with get_db() as conn:
        cur = cursor(conn)
        db, have = sm.load_db_stores(cur)
        if not sm.migration_applied(have):
            raise HTTPException(409, "The store-master database migration has not been applied yet "
                                     "(backend/migrations/store_master_2026_11_22.py)")
        pl = sm.plan(parsed, db, update_descriptive=update_descriptive)
        if pl["problems"]:
            raise HTTPException(422, "The workbook has problems, nothing was loaded: "
                                     + "; ".join(pl["problems"][:5]))
        if not pl["qa"]["rows_accepted"]:
            raise HTTPException(422, "No store rows found on the first sheet")
        res = sm.apply(cur, pl, _email(user))
        out = sm.public_plan(pl)
        out.update(res)
        return out
