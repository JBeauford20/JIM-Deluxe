"""
JIM Deluxe — Scenarios (natural-language engine directives)
============================================================
Users type an English scenario ("Arnie Sherman removed this week",
"Black Friday Bowls going to the South"). Claude translates it into one or
more structured `weekly_adjustments` rows — the SAME directives the engine
(playbook_engine.py) already applies. A scenario stays live until removed.

Hard principle: English -> structured spec -> human review -> engine.
Never English mutating the engine directly. The parser may ONLY emit the
adjustment types + scope values the engine actually honors (listed below).

Flow:
  POST /parse   -> Claude drafts adjustments from English (no DB write, preview)
  POST /        -> save reviewed scenario + its adjustments (goes live)
  GET  /        -> list scenarios for a week (active + inactive) with adjustments
  PATCH /{id}/toggle -> turn a scenario on/off for the run
  DELETE /{id}  -> remove a scenario from the engine for that run
"""
import os, json
from datetime import date, timedelta
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from db import get_db, cursor
from routers.auth import get_current_user, require_role
from routers.ai_explanations import _client, _call

router = APIRouter()

# ── Directive vocabulary the engine honors (playbook_engine.py) ──────────────
ADJUSTMENT_TYPES = {
    "cart_target_override": "Set stores in scope to an exact cart count. Use value_numeric=0 to "
                            "REMOVE/exclude those stores from this week entirely.",
    "demand_multiplier":    "Multiply demand for store+SKU combos in scope. >1 boosts, <1 suppresses, "
                            "0 effectively drops that SKU for those stores.",
    "exclusion":            "Hard-block the SKU(s) in scope from the stores in scope (zeroes them). "
                            "To send a product to ONLY one region, emit one exclusion per OTHER region.",
    "priority_boost":       "Add a flat bonus to the priority score of stores in scope (favor them).",
    "sku_push":             "Move more of a SKU overall by raising its availability weight (0-1).",
    "allocation_floor":     "Guarantee stores in scope at least value_numeric carts of the scoped SKU.",
    "allocation_cap":       "Limit stores in scope to at most value_numeric carts of the scoped SKU.",
}
_ALLOWED = set(ADJUSTMENT_TYPES)

# Scope fields the engine matches on (exact values only — see catalog)
_STORE_SCOPES = ["scope_merchant", "scope_region", "scope_state",
                 "scope_velocity_tier", "scope_store_ids"]
_SKU_SCOPES   = ["scope_sku_id", "scope_family", "scope_legacy_tier"]


def _catalog(cur) -> dict:
    """Valid scope values, so the parser only emits things the engine can match."""
    def col(q):
        cur.execute(q); return [r["v"] for r in cur.fetchall() if r["v"] is not None]
    cur.execute("SELECT sku_id, description FROM skus WHERE active = true ORDER BY sku_id")
    skus = [{"sku_id": r["sku_id"], "description": (r["description"] or "").strip()[:48]}
            for r in cur.fetchall()]
    return {
        "regions":        col("SELECT DISTINCT region AS v FROM stores WHERE region IS NOT NULL ORDER BY 1"),
        "merchants":      col("SELECT DISTINCT merchant AS v FROM stores WHERE merchant IS NOT NULL ORDER BY 1"),
        "states":         col("SELECT DISTINCT state AS v FROM stores WHERE state IS NOT NULL ORDER BY 1"),
        "velocity_tiers": ["AA", "A", "B", "C", "D", "P"],
        "families":       col("SELECT DISTINCT family AS v FROM skus WHERE family IS NOT NULL ORDER BY 1"),
        "legacy_tiers":   col("SELECT DISTINCT legacy_product_group AS v FROM skus WHERE legacy_product_group IS NOT NULL ORDER BY 1"),
        "skus":           skus,
    }


_PARSE_SYSTEM = """You translate an order writer's plain-English scenario into structured JIM Deluxe
engine directives (weekly_adjustments). You are the review step's input — a human will confirm your
output before it touches the engine, so be precise and conservative.

RULES
- Output ONLY the adjustment types listed. Never invent types or scope fields.
- Use ONLY scope values that appear in the provided CATALOG, spelled exactly. If the user names
  something not in the catalog, add a warning and do your best / leave that scope null.
- Region names in this system are "Northern", "Southern", "Western" (map "south"->"Southern", etc.).
- Leave every unused scope field null. Store scopes: merchant, region, state, velocity_tier, store_ids.
  Product scopes: sku_id, family, legacy_tier.
- To REMOVE/EXCLUDE a set of stores from the week: cart_target_override with value_numeric = 0.
- To send a product to ONLY one region: emit one `exclusion` per OTHER region (scope that region +
  the product family/sku). Do NOT exclude the target region.
- Prefer scope_family for product groups (e.g. "bowls" -> family "H2O Bowl"); use scope_sku_id only
  when the user clearly means one specific SKU.
- Give each adjustment a short human label.

Return STRICT JSON only, no prose:
{
  "interpretation": "one sentence restating what you understood, in plain English",
  "adjustments": [
    {"adjustment_type": "...", "label": "...", "value_numeric": number-or-null, "value_text": null,
     "scope_merchant": null, "scope_region": null, "scope_state": null, "scope_velocity_tier": null,
     "scope_sku_id": null, "scope_family": null, "scope_legacy_tier": null, "scope_store_ids": null}
  ],
  "warnings": ["..."]
}

EXAMPLES
Scenario: "Arnie Sherman removed from this week's consideration"
-> {"interpretation":"Exclude all stores in Arnie Sherman's merchant territory from this week.",
    "adjustments":[{"adjustment_type":"cart_target_override","label":"Exclude Arnie Sherman territory",
      "value_numeric":0,"value_text":null,"scope_merchant":"Arnie Sherman","scope_region":null,
      "scope_state":null,"scope_velocity_tier":null,"scope_sku_id":null,"scope_family":null,
      "scope_legacy_tier":null,"scope_store_ids":null}],"warnings":[]}

Scenario: "Black Friday Bowls going to the South"
-> {"interpretation":"Send H2O Bowls only to Southern-region stores by blocking them elsewhere.",
    "adjustments":[
      {"adjustment_type":"exclusion","label":"Bowls blocked from Northern","value_numeric":null,
       "value_text":null,"scope_merchant":null,"scope_region":"Northern","scope_state":null,
       "scope_velocity_tier":null,"scope_sku_id":null,"scope_family":"H2O Bowl","scope_legacy_tier":null,
       "scope_store_ids":null},
      {"adjustment_type":"exclusion","label":"Bowls blocked from Western","value_numeric":null,
       "value_text":null,"scope_merchant":null,"scope_region":"Western","scope_state":null,
       "scope_velocity_tier":null,"scope_sku_id":null,"scope_family":"H2O Bowl","scope_legacy_tier":null,
       "scope_store_ids":null}],"warnings":[]}"""


def _validate(adjustments: list, cat: dict) -> tuple:
    """Return (clean_adjustments, warnings). Drops invalid types; nulls unknown scope values."""
    clean, warns = [], []
    valid = {"scope_merchant": set(cat["merchants"]), "scope_region": set(cat["regions"]),
             "scope_state": set(cat["states"]), "scope_velocity_tier": set(cat["velocity_tiers"]),
             "scope_family": set(cat["families"]), "scope_legacy_tier": set(cat["legacy_tiers"])}
    sku_ids = {s["sku_id"] for s in cat["skus"]}
    for a in adjustments:
        atype = a.get("adjustment_type")
        if atype not in _ALLOWED:
            warns.append(f"Dropped unsupported directive '{atype}'.")
            continue
        for f, allowed in valid.items():
            v = a.get(f)
            if v and v not in allowed:
                warns.append(f"{f}='{v}' isn't a known value — ignored.")
                a[f] = None
        if a.get("scope_sku_id") and a["scope_sku_id"] not in sku_ids:
            warns.append(f"scope_sku_id={a['scope_sku_id']} not found — ignored.")
            a["scope_sku_id"] = None
        clean.append(a)
    return clean, warns


def _store_count(cur, a: dict) -> Optional[int]:
    """Rough preview of how many active stores a store-scoped directive touches."""
    where, params = ["active = true"], []
    if a.get("scope_store_ids"):
        where.append("store_id = ANY(%s)"); params.append(a["scope_store_ids"])
    if a.get("scope_merchant"):
        where.append("merchant = %s"); params.append(a["scope_merchant"])
    if a.get("scope_region"):
        where.append("region = %s"); params.append(a["scope_region"])
    if a.get("scope_state"):
        where.append("state = %s"); params.append(a["scope_state"])
    if a.get("scope_velocity_tier"):
        where.append("TRIM(dynamic_velocity_tier) = %s"); params.append(a["scope_velocity_tier"])
    if len(where) == 1:
        return None  # no store scope (SKU-only directive)
    cur.execute(f"SELECT COUNT(*) AS n FROM stores WHERE {' AND '.join(where)}", params)
    return cur.fetchone()["n"]


def _resolve_week(shipping_week: Optional[str]) -> date:
    if shipping_week:
        try:
            return date.fromisoformat(shipping_week)
        except ValueError:
            raise HTTPException(400, "shipping_week must be YYYY-MM-DD")
    today = date.today()
    days_ahead = (7 - today.weekday()) % 7 or 7
    return today + timedelta(days=days_ahead)


# ── Models ───────────────────────────────────────────────────────────────────
class AdjItem(BaseModel):
    adjustment_type: str
    label: str
    value_numeric: Optional[float] = None
    value_text: Optional[str] = None
    scope_merchant: Optional[str] = None
    scope_region: Optional[str] = None
    scope_state: Optional[str] = None
    scope_velocity_tier: Optional[str] = None
    scope_sku_id: Optional[int] = None
    scope_family: Optional[str] = None
    scope_legacy_tier: Optional[str] = None
    scope_store_ids: Optional[List[int]] = None


class ParseReq(BaseModel):
    text: str
    shipping_week: Optional[str] = None


class CreateReq(BaseModel):
    name: Optional[str] = None
    text: str
    shipping_week: str
    adjustments: List[AdjItem]


# ── Endpoints ─────────────────────────────────────────────────────────────────
@router.post("/parse")
def parse_scenario(body: ParseReq,
                   user=Depends(require_role("order_writer", "manager", "admin"))):
    """English -> draft directives (preview only, nothing written)."""
    if not body.text.strip():
        raise HTTPException(400, "Enter a scenario to interpret.")
    with get_db() as conn:
        cur = cursor(conn)
        cat = _catalog(cur)
        user_msg = (f"CATALOG (valid scope values):\n{json.dumps(cat)}\n\n"
                    f"SCENARIO: {body.text.strip()}")
        raw = _call(_client(), model="claude-sonnet-4-6",
                    system=_PARSE_SYSTEM, user_msg=user_msg, max_tokens=1500)
        try:
            s, e = raw.find("{"), raw.rfind("}") + 1
            parsed = json.loads(raw[s:e])
        except Exception:
            raise HTTPException(502, "Could not parse the scenario — try rephrasing.")
        adjustments, warns = _validate(parsed.get("adjustments", []), cat)
        warns = (parsed.get("warnings") or []) + warns
        for a in adjustments:
            a["stores_affected"] = _store_count(cur, a)
    return {"interpretation": parsed.get("interpretation", ""),
            "adjustments": adjustments, "warnings": warns}


@router.post("/", status_code=201)
def create_scenario(body: CreateReq,
                    user=Depends(require_role("order_writer", "manager", "admin"))):
    """Save a reviewed scenario + its adjustments — goes live for the week's run."""
    week = _resolve_week(body.shipping_week)
    if not body.adjustments:
        raise HTTPException(400, "A scenario needs at least one directive.")
    email = user.get("email", "system") if isinstance(user, dict) else "system"
    name = (body.name or body.text).strip()[:120]
    with get_db() as conn:
        cur = cursor(conn)
        cat = _catalog(cur)
        clean, _ = _validate([a.dict() for a in body.adjustments], cat)
        if not clean:
            raise HTTPException(400, "No valid directives to save.")
        cur.execute("""
            INSERT INTO planning_scenarios (name, description, shipping_week, is_active, created_by)
            VALUES (%s, %s, %s, true, %s) RETURNING id
        """, (name, body.text.strip(), week, email))
        sid = cur.fetchone()["id"]
        for a in clean:
            cur.execute("""
                INSERT INTO weekly_adjustments
                    (shipping_week, label, description, adjustment_type,
                     scope_merchant, scope_region, scope_state, scope_velocity_tier,
                     scope_store_ids, scope_sku_id, scope_family, scope_legacy_tier,
                     value_numeric, value_text, effective_start, effective_end,
                     active, source, created_by, stores_affected, scenario_id)
                VALUES (%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, true,'scenario',%s,%s,%s)
            """, (week, a.get("label", name)[:120], body.text.strip()[:500], a["adjustment_type"],
                  a.get("scope_merchant"), a.get("scope_region"), a.get("scope_state"),
                  a.get("scope_velocity_tier"), a.get("scope_store_ids"), a.get("scope_sku_id"),
                  a.get("scope_family"), a.get("scope_legacy_tier"),
                  a.get("value_numeric"), a.get("value_text"), week, week,
                  email, _store_count(cur, a), sid))
    return {"id": str(sid), "shipping_week": str(week), "adjustments_saved": len(clean)}


@router.get("/")
def list_scenarios(shipping_week: Optional[str] = None, user=Depends(get_current_user)):
    """All scenarios for a week (active + inactive) with their directives."""
    week = _resolve_week(shipping_week)
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT id, name, description, shipping_week, is_active, created_by, created_at
            FROM planning_scenarios WHERE shipping_week = %s
            ORDER BY is_active DESC, created_at DESC
        """, (week,))
        scenarios = cur.fetchall()
        for s in scenarios:
            cur.execute("""
                SELECT adjustment_type, label, value_numeric,
                       scope_merchant, scope_region, scope_state, scope_velocity_tier,
                       scope_sku_id, scope_family, scope_legacy_tier,
                       stores_affected, active
                FROM weekly_adjustments WHERE scenario_id = %s ORDER BY created_at
            """, (s["id"],))
            s["adjustments"] = cur.fetchall()
    return {"shipping_week": str(week), "scenarios": scenarios}


@router.patch("/{scenario_id}/toggle")
def toggle_scenario(scenario_id: str,
                    user=Depends(require_role("order_writer", "manager", "admin"))):
    """Turn a scenario on/off for the run (its directives follow)."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("UPDATE planning_scenarios SET is_active = NOT is_active WHERE id = %s RETURNING is_active",
                    (scenario_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Scenario not found")
        cur.execute("UPDATE weekly_adjustments SET active = %s WHERE scenario_id = %s",
                    (row["is_active"], scenario_id))
    return {"id": scenario_id, "is_active": row["is_active"]}


@router.delete("/{scenario_id}")
def delete_scenario(scenario_id: str,
                    user=Depends(require_role("order_writer", "manager", "admin"))):
    """Remove a scenario from the engine for that run (deletes it + its directives)."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("DELETE FROM weekly_adjustments WHERE scenario_id = %s", (scenario_id,))
        removed = cur.rowcount
        cur.execute("DELETE FROM planning_scenarios WHERE id = %s", (scenario_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Scenario not found")
    return {"deleted": scenario_id, "adjustments_removed": removed}
