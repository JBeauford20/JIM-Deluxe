"""
TPC - IPR Engine — Scenarios (natural-language engine directives)
============================================================
Users type an English scenario ("Arnie Sherman removed this week",
"Black Friday Bowls going to the South"). Claude translates it into one or
more structured `weekly_adjustments` rows — the SAME directives the engine
(playbook_engine.py) already applies. A scenario stays live until removed.

Hard principle: English -> structured spec -> human review -> engine.
Never English mutating the engine directly. The parser may ONLY emit the
adjustment types + scope values the engine actually honors (listed below).

Flow (checks and balances, 2026-11-20 — no one approves their own work):
  POST /parse        -> Claude drafts adjustments from English (no DB write, preview)
  POST /             -> submit the reviewed scenario: status pending_approval, directives
                        saved INACTIVE — the engine cannot see them yet
  GET  /pending      -> the approval queue (Mike's dashboard)
  POST /{id}/approve -> approver (manager/sales_director/admin, NEVER the submitter)
                        activates it; the engine applies it deterministically + bounded
  POST /{id}/reject  -> approver declines with a reason
  GET  /             -> list scenarios for a week with status + adjustments
  PATCH /{id}/toggle -> turn an APPROVED scenario on/off for the run
  DELETE /{id}  -> remove a scenario from the engine for that run
After the run, the independent scenario audit (services/ipr_reviewer.run_scenario_audit)
verifies every applied scenario against store pace — a different model pass, fed raw
evidence, never the parser's or approver's reasoning.
"""
import os, json, re
from datetime import date, timedelta
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from db import get_db, cursor
from routers.auth import get_current_user, require_role
from routers.ai_explanations import _client, _call

router = APIRouter()

# ── Directive vocabulary the engine honors (playbook_engine.py) ──────────────
# What the v2 engine (workers/ipr_engine.py) actually applies. The legacy types 'sku_push' and a
# non-zero 'cart_target_override' are NOT applied by v2 (availability may not drive demand;
# cart counts are demand-driven), so the parser is no longer offered them.
ADJUSTMENT_TYPES = {
    "cart_target_override": "ONLY with value_numeric = 0: remove the stores in scope from this "
                            "week entirely. Never any other number.",
    "demand_multiplier":    "Multiply forecast demand for the store + product in scope. 1.25 = 25% "
                            "more, 0.8 = 20% less. Capped by the engine (default 3x). Needs a "
                            "product scope (family, legacy tier or SKU) unless it is truly everything.",
    "exclusion":            "Block the product in scope from the stores in scope this week. To send "
                            "a product to ONLY one region, emit one exclusion per OTHER region.",
    "priority_boost":       "Move the stores in scope up the allocation order when supply is short. "
                            "value_numeric between 0.05 and 0.2.",
    "allocation_floor":     "Give the stores in scope at least value_numeric SHELVES of the product "
                            "in scope. The engine trims it to what each store sells, never more.",
    "allocation_cap":       "Limit the stores in scope to at most value_numeric SHELVES of the "
                            "product in scope.",
    "directed_shipment":    "Ship EXACTLY value_numeric SHELVES of the product in scope to the "
                            "stores in scope, whether or not they have sold it before. Use this "
                            "ONLY when the user clearly orders product to be sent ('send', 'ship', "
                            "'put', 'they get', 'make sure they receive' + a shelf count) to named "
                            "stores, a merchant, a state, a region or a grade. Needs BOTH a store "
                            "scope and a product scope. For 'at least' / 'guarantee' use "
                            "allocation_floor instead. If no shelf count is given, use 1 and say so "
                            "in warnings.",
}
_ALLOWED = set(ADJUSTMENT_TYPES)
DIRECTED_MAX_SHELVES = 10        # mirrors workers/ipr_engine.py
_VOCAB = "\n".join(f"- {k}: {v}" for k, v in ADJUSTMENT_TYPES.items())

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
    # Combined product names people actually say ("12cm Collector", "17cm Rare Collector"):
    # each maps to a size family AND a tier, which the engine matches together.
    cur.execute("""SELECT fm.ipr_family AS name, MIN(k.family) AS family,
                          MIN(k.legacy_product_group) AS tier
                   FROM skus k JOIN ipr_family_map fm ON fm.sku_id = k.sku_id
                   WHERE k.active AND fm.ipr_family IS NOT NULL AND k.family IS NOT NULL
                     AND k.legacy_product_group IS NOT NULL
                   GROUP BY fm.ipr_family
                   HAVING COUNT(DISTINCT k.family) = 1 AND COUNT(DISTINCT k.legacy_product_group) = 1""")
    groups = {r["name"]: {"scope_family": r["family"], "scope_legacy_tier": r["tier"]} for r in cur.fetchall()}
    return {
        "product_groups": groups,
        "regions":        col("SELECT DISTINCT region AS v FROM stores WHERE region IS NOT NULL ORDER BY 1"),
        "merchants":      col("SELECT DISTINCT merchant AS v FROM stores WHERE merchant IS NOT NULL ORDER BY 1"),
        "states":         col("SELECT DISTINCT UPPER(TRIM(state)) AS v FROM stores "
                              "WHERE state IS NOT NULL AND TRIM(state) <> '' ORDER BY 1"),
        "velocity_tiers": ["AA", "A", "B", "C", "D", "P"],
        "families":       col("SELECT DISTINCT family AS v FROM skus WHERE family IS NOT NULL ORDER BY 1"),
        "legacy_tiers":   col("SELECT DISTINCT legacy_product_group AS v FROM skus WHERE legacy_product_group IS NOT NULL ORDER BY 1"),
        "skus":           skus,
    }


_PARSE_SYSTEM = """You translate an order writer's plain-English scenario into structured TPC - IPR Engine
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
- A SPECIFIC PLANT VARIETY (e.g. "Monstera Bulbasaur 17cm", "Thai Constellation 12cm", "Hedera
  Glacier") is not in the catalog: the catalog lists product groups. When the user names one,
  put its name INCLUDING THE SIZE if given in "variety" and leave scope_family, scope_legacy_tier
  and scope_sku_id null. The server finds the variety. Only directed_shipment may carry a variety.
- "[STORE LIST]" in the scenario stands for a list of store numbers the user pasted. To use it
  set "scope_store_ids": ["LIST"] exactly. Never invent or copy store numbers for it.
- Product scopes COMBINE: scope_family and scope_legacy_tier set together mean "this size AND
  this tier". "12cm collectors" -> scope_family "12cm" + scope_legacy_tier "Collectors".
  "17cm rare collectors" -> "17cm" + "Rare Collectors". "collectors" alone -> tier only (all
  sizes). CATALOG.product_groups lists the combined names and the family + tier each one means.
- Specific stores: put their numbers in scope_store_ids as a JSON list of integers, e.g. [608, 664].
  Store numbers are checked after you answer, so do not warn that you cannot verify them.
- "More" / "less" / a percentage of a product -> demand_multiplier. "At least N shelves" ->
  allocation_floor. "No more than N shelves" -> allocation_cap. "Take X first" -> priority_boost.
  "Send / ship N shelves of X to <stores>" -> directed_shipment (exact, ignores sales history).
- If the request cannot be expressed with the types below, return no adjustment and say why in
  warnings, in one plain sentence.

Return STRICT JSON only, no prose:
{
  "interpretation": "one sentence restating what you understood, in plain English",
  "adjustments": [
    {"adjustment_type": "...", "label": "...", "value_numeric": number-or-null, "value_text": null,
     "variety": null,
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


def _pkey(v) -> str:
    """'12cm Collectors' / '12 cm collector' -> '12cmcollector' (case, spaces, plural)."""
    k = "".join(ch for ch in str(v or "").lower() if ch.isalnum())
    return k[:-1] if k.endswith("s") else k


def _validate(adjustments: list, cat: dict) -> tuple:
    """Return (clean_adjustments, warnings). Drops invalid types; nulls unknown scope values."""
    clean, warns = [], []
    valid = {"scope_merchant": set(cat["merchants"]), "scope_region": set(cat["regions"]),
             "scope_state": set(cat["states"]), "scope_velocity_tier": set(cat["velocity_tiers"]),
             "scope_family": set(cat["families"]), "scope_legacy_tier": set(cat["legacy_tiers"])}
    sku_ids = {s["sku_id"] for s in cat["skus"]}
    groups = {_pkey(k): v for k, v in (cat.get("product_groups") or {}).items()}
    for a in adjustments:
        if not isinstance(a, dict):
            continue
        atype = a.get("adjustment_type")
        if atype not in _ALLOWED:
            warns.append(f"Dropped unsupported directive '{atype}'.")
            continue
        if atype == "cart_target_override" and float(a.get("value_numeric") or 0) != 0:
            warns.append("Setting an exact cart count is not supported; cart counts follow demand. "
                         "Dropped.")
            continue
        if isinstance(a.get("scope_state"), str):
            a["scope_state"] = a["scope_state"].strip().upper()
        ids = a.get("scope_store_ids")
        if ids is not None:                       # "608", ["608"], 608 -> [608]
            if not isinstance(ids, (list, tuple)):
                ids = [ids]
            good = []
            for x in ids:
                try:
                    good.append(int(str(x).strip()))
                except (TypeError, ValueError):
                    warns.append(f"'{x}' is not a store number — ignored.")
            a["scope_store_ids"] = sorted(set(good)) or None
        try:
            if a.get("scope_sku_id") is not None:
                a["scope_sku_id"] = int(a["scope_sku_id"])
        except (TypeError, ValueError):
            a["scope_sku_id"] = None
        # a combined name in either product field ("12cm Collectors") -> its family + tier
        if isinstance(a, dict):
            for f in ("scope_family", "scope_legacy_tier"):
                g = groups.get(_pkey(a.get(f))) if a.get(f) and a.get(f) not in valid[f] else None
                if g:
                    a["scope_family"], a["scope_legacy_tier"] = g["scope_family"], g["scope_legacy_tier"]
                    break
        for f, allowed in valid.items():
            v = a.get(f)
            if v and v not in allowed:
                warns.append(f"{f}='{v}' isn't a known value — ignored.")
                a[f] = None
        if a.get("scope_sku_id") and a["scope_sku_id"] not in sku_ids:
            warns.append(f"scope_sku_id={a['scope_sku_id']} not found — ignored.")
            a["scope_sku_id"] = None
        # checked LAST, after unknown scope values were cleared: a directed shipment whose
        # product name was not recognised must not survive as "send to this store, no product"
        if atype == "directed_shipment":
            shelves = float(a.get("value_numeric") or 0)
            if not any(a.get(f) for f in _STORE_SCOPES) or not any(a.get(f) for f in _SKU_SCOPES):
                warns.append("A directed shipment must name both the stores and the product. Dropped.")
                continue
            if shelves <= 0 or shelves != int(shelves) or shelves > DIRECTED_MAX_SHELVES:
                warns.append(f"A directed shipment must be a whole number of shelves from 1 to "
                             f"{DIRECTED_MAX_SHELVES} per store. Dropped.")
                continue
            warns.append("Directed shipment: ships regardless of the stores' sales history and skips "
                         "the full-cart minimum. Once approved it also overrides a store's product "
                         "restriction. Do-not-ship and variety bans (No Hedera) still apply.")
        clean.append(a)
    return clean, warns


def _check_stores(cur, adjustments: list) -> tuple:
    """Drop store numbers that are not in the store master and tell the reviewer what each
    named store's handling means FOR THIS REQUEST: blocked stores, and a store-master product
    rule that an approved directed shipment would override. A rule that does not touch the
    product (a No Hedera store receiving a Monstera) is not mentioned."""
    ids = sorted({i for a in adjustments for i in (a.get("scope_store_ids") or [])})
    if not ids:
        return adjustments, []
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_name = 'stores' AND column_name = 'sku_rule'""")
    extra = ", sku_restriction, sku_rule" if cur.fetchone() else ""
    cur.execute(f"SELECT store_id, store_name, store_status{extra} FROM stores WHERE store_id = ANY(%s)", (ids,))
    known = {r["store_id"]: r for r in cur.fetchall()}
    warns = [f"Store {i} is not in the store master — ignored." for i in ids if i not in known]
    blocked = sorted(i for i, r in known.items()
                     if (r.get("store_status") or "active") in ("do_not_ship", "paused", "inactive", "hold"))
    for i in blocked:
        warns.append(f"Store {i} is {known[i]['store_status'].replace('_', ' ')}: nothing ships there "
                     f"whatever this scenario says.")
    ruled = {i: r for i, r in known.items() if r.get("sku_rule") and i not in blocked}
    if ruled:
        from services.store_master import store_rules
        cur.execute("SELECT value FROM ipr_settings WHERE key = 'store_sku_rules'")
        row = cur.fetchone()
        rules = store_rules.merged_rules(row["value"] if row else None)
        cur.execute("""SELECT k.sku_id, k.family, k.program, k.legacy_product_group, k.is_holiday,
                              fm.ipr_family FROM skus k LEFT JOIN ipr_family_map fm ON fm.sku_id = k.sku_id
                       WHERE k.active""")
        skus = [dict(r) for r in cur.fetchall()]
        over, limited = {}, {}
        for a in adjustments:
            if a.get("adjustment_type") in ("exclusion", "cart_target_override", "allocation_cap"):
                continue                          # taking product away never conflicts with a rule
            scoped = [k for k in skus
                      if (not a.get("scope_sku_id") or k["sku_id"] == a["scope_sku_id"])
                      and (not a.get("scope_family") or k["family"] == a["scope_family"])
                      and (not a.get("scope_legacy_tier") or k["legacy_product_group"] == a["scope_legacy_tier"])]
            for i in (a.get("scope_store_ids") or []):
                if i not in ruled:
                    continue
                rule = store_rules.rule_for(ruled[i], rules)
                if not scoped or all(store_rules.sku_allowed(rule, k)[0] for k in scoped):
                    continue                      # the rule does not keep this product out
                if a["adjustment_type"] == "directed_shipment" and store_rules.directed_overrides(rule):
                    over.setdefault(ruled[i].get("sku_restriction") or rule.get("label"), []).append(i)
                else:
                    limited.setdefault(ruled[i].get("sku_restriction") or rule.get("label"), []).append(i)
        for label, st in sorted(over.items()):
            st = sorted(set(st))
            warns.append(f"OVERRIDE: {len(st)} store(s) normally restricted to '{label}' will receive this "
                         f"product if it is approved: {', '.join(map(str, st[:40]))}"
                         + (f" and {len(st) - 40} more" if len(st) > 40 else "") + ".")
        for label, st in sorted(limited.items()):
            st = sorted(set(st))
            warns.append(f"{len(st)} store(s) are restricted to '{label}', so this will not ship there: "
                         f"{', '.join(map(str, st[:40]))}" + (f" and {len(st) - 40} more" if len(st) > 40 else "") + ".")
    out = []
    for a in adjustments:
        if a.get("scope_store_ids"):
            a["scope_store_ids"] = [i for i in a["scope_store_ids"] if i in known] or None
            if a["scope_store_ids"] is None and not any(a.get(f) for f in _STORE_SCOPES):
                warns.append(f"'{a.get('label') or a['adjustment_type']}' named no valid store — dropped.")
                continue
        out.append(a)
    return out, warns


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
        where.append("UPPER(TRIM(state)) = %s"); params.append(a["scope_state"])
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
    interpretation: Optional[str] = None     # the parser's restatement, shown to the approver
    adjustments: List[AdjItem]


class RejectReq(BaseModel):
    reason: str


_NUM = re.compile(r"(?<![\w.$#/-])(\d{3,5})(?![\w.%/-]|\s*(?:cm|units?|shelves|shelf|carts?|trays?)\b)")


def _extract_store_list(text: str) -> tuple:
    """A pasted list of store numbers is lifted out of the text deterministically, so the
    model never has to copy 100 numbers (and cannot drop or invent one). Returns
    (text with the list replaced by a marker, [store numbers]). Under 8 numbers the text is
    left alone and the model reads them itself."""
    found = _NUM.findall(text or "")
    if len(found) < 8:
        return text, []
    ids, seen = [], set()
    for x in found:
        if int(x) not in seen:
            seen.add(int(x))
            ids.append(int(x))
    out = _NUM.sub("\x00", text)
    out = re.sub(r"(?:\x00[\s,;|/&]*(?:and\s+)?)+", " [STORE LIST] ", out)
    return " ".join(out.split()), ids


def _resolve_variety(cur, a: dict) -> list:
    """'Monstera Bulbasaur 17cm' -> scope_sku_id (the assorted HD SKU it ships under) and
    value_text 'aster:<codes>|<name>' so the engine ships ONLY that variety. Mutates `a`;
    returns warnings. Ambiguous or unknown names leave the adjustment without a product, and
    validation then drops it."""
    name = str(a.pop("variety", "") or "").strip()
    words = [w for w in re.findall(r"[a-z0-9']+", name.lower()) if len(w) >= 3 and w not in ("the", "and", "plant")]
    if a.get("adjustment_type") != "directed_shipment":
        a["adjustment_type"] = "named variety on a non-shipment"
        return [f"A named variety ('{name}') can only be used to send product (a directed shipment). "
                f"To block or limit, name the product group instead."]
    a["scope_family"] = a["scope_legacy_tier"] = a["scope_sku_id"] = None
    a["value_text"] = None
    if not words:
        return []
    cur.execute("""SELECT v.aster_code::text AS code, v.product_name, m.hd_sku_id
                   FROM variety_parsed_reference v
                   JOIN aster_product_mapping m ON m.aster_code::text = v.aster_code::text
                   WHERE m.active AND m.hd_sku_id IS NOT NULL AND """
                + " AND ".join(["v.product_name ILIKE %s"] * len(words)),
                [f"%{w}%" for w in words])
    rows = cur.fetchall()
    if not rows:
        return [f"No plant variety matches '{name}'. Check the spelling, or name the product group instead."]
    skus = sorted({r["hd_sku_id"] for r in rows})
    if len(skus) > 1:
        ex = sorted({r["product_name"] for r in rows})[:4]
        return [f"'{name}' matches more than one product ({'; '.join(ex)}). Add the size, e.g. '{name} 17cm'."]
    if a.get("adjustment_type") != "directed_shipment":
        # never let it fall through with no product (an exclusion with no product would block
        # everything for those stores): make it an unknown type so validation drops it
        a["adjustment_type"] = "named variety on a non-shipment"
        return [f"A named variety ('{name}') can only be used to send product (a directed shipment). "
                f"To block or limit, name the product group instead."]
    codes = sorted({r["code"] for r in rows})
    label = min((r["product_name"] for r in rows), key=len)
    label = re.sub(r"\s+(Everyday|Jute Kit|Ceramic Kit|Canopy)$", "", label, flags=re.I)
    a["scope_sku_id"] = int(skus[0])
    a["value_text"] = f"aster:{','.join(codes)}|{label}"
    return [f"Only {label} will be sent (Aster {', '.join(codes)}). It ships only if this week's "
            f"availability has that variety; nothing is substituted if it is short."]


def interpret(cur, text: str) -> dict:
    """English -> {interpretation, adjustments, warnings}. Nothing is written. Shared by the
    /parse endpoint and tests/scenario_battery.py so the battery exercises the real path."""
    cat = _catalog(cur)
    text, store_list = _extract_store_list(text)
    user_msg = (f"ADJUSTMENT TYPES (the only ones you may use):\n\n"
                f"{_VOCAB}\n\n"
                f"CATALOG (valid scope values):\n{json.dumps(cat)}\n\n"
                f"SCENARIO: {text.strip()}")
    from services.ipr_ai import call_model
    raw = call_model(_PARSE_SYSTEM, user_msg, max_tokens=1500)
    try:
        s_, e_ = raw.find("{"), raw.rfind("}") + 1
        parsed = json.loads(raw[s_:e_])
    except Exception:
        raise HTTPException(502, "Could not parse the scenario — try rephrasing.")
    if not isinstance(parsed, dict):
        raise HTTPException(502, "Could not parse the scenario — try rephrasing.")
    raw_adj = parsed.get("adjustments")
    raw_adj = raw_adj if isinstance(raw_adj, list) else []
    pre = []
    for a in raw_adj:
        if not isinstance(a, dict):
            continue
        if a.get("scope_store_ids") in (["LIST"], "LIST"):
            a["scope_store_ids"] = list(store_list)
        if a.get("variety"):
            pre += _resolve_variety(cur, a)
    adjustments, warns = _validate(raw_adj, cat)
    warns = pre + warns
    if store_list:
        warns.insert(0, f"Read {len(store_list)} store numbers from your list. Check that count is right.")
    adjustments, w2 = _check_stores(cur, adjustments)
    warns = [str(w) for w in (parsed.get("warnings") or [])
             if isinstance(w, str) and not w.lstrip().startswith(("{", "["))] + warns + w2
    for a in adjustments:
        a["stores_affected"] = _store_count(cur, a)
    return {"interpretation": parsed.get("interpretation", ""),
            "adjustments": adjustments, "warnings": warns}


# ── Endpoints ─────────────────────────────────────────────────────────────────
@router.post("/parse")
def parse_scenario(body: ParseReq,
                   user=Depends(require_role("order_writer", "manager", "admin"))):
    """English -> draft directives (preview only, nothing written)."""
    if not body.text.strip():
        raise HTTPException(400, "Enter a scenario to interpret.")
    with get_db() as conn:
        return interpret(cursor(conn), body.text)


@router.post("/", status_code=201)
def create_scenario(body: CreateReq,
                    user=Depends(require_role("order_writer", "manager", "sales_director", "admin"))):
    """Submit a reviewed scenario for approval. Directives are saved INACTIVE and the
    scenario is pending_approval — the engine cannot see it until a DIFFERENT person
    with approval rights approves it (checks and balances, 2026-11-20)."""
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
            INSERT INTO planning_scenarios (name, description, shipping_week, is_active,
                                            created_by, status, interpretation)
            VALUES (%s, %s, %s, false, %s, 'pending_approval', %s) RETURNING id
        """, (name, body.text.strip(), week, email, (body.interpretation or "").strip()[:600] or None))
        sid = cur.fetchone()["id"]
        for a in clean:
            cur.execute("""
                INSERT INTO weekly_adjustments
                    (shipping_week, label, description, adjustment_type,
                     scope_merchant, scope_region, scope_state, scope_velocity_tier,
                     scope_store_ids, scope_sku_id, scope_family, scope_legacy_tier,
                     value_numeric, value_text, effective_start, effective_end,
                     active, source, created_by, stores_affected, scenario_id)
                VALUES (%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, false,'scenario',%s,%s,%s)
            """, (week, a.get("label", name)[:120], body.text.strip()[:500], a["adjustment_type"],
                  a.get("scope_merchant"), a.get("scope_region"), a.get("scope_state"),
                  a.get("scope_velocity_tier"), a.get("scope_store_ids"), a.get("scope_sku_id"),
                  a.get("scope_family"), a.get("scope_legacy_tier"),
                  a.get("value_numeric"), a.get("value_text"), week, week,
                  email, _store_count(cur, a), sid))
    return {"id": str(sid), "shipping_week": str(week), "adjustments_saved": len(clean),
            "status": "pending_approval"}


APPROVER_ROLES = ("manager", "sales_director", "admin")


@router.get("/pending")
def pending_scenarios(user=Depends(get_current_user)):
    """The approval queue, newest first — Mike's dashboard feed."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT id, name, description, interpretation, shipping_week, created_by, created_at
            FROM planning_scenarios WHERE status = 'pending_approval'
            ORDER BY created_at DESC LIMIT 100
        """)
        rows = cur.fetchall()
        for s in rows:
            cur.execute("""SELECT adjustment_type, label, value_numeric, scope_merchant, scope_region,
                                  scope_state, scope_velocity_tier, scope_sku_id, scope_family,
                                  scope_legacy_tier, scope_store_ids, stores_affected, value_text
                           FROM weekly_adjustments WHERE scenario_id = %s ORDER BY created_at""",
                        (s["id"],))
            s["adjustments"] = cur.fetchall()
            # What the approver must know before saying yes: store conflicts as they stand NOW
            # (a store may have gone do-not-ship since this was submitted) and overrides.
            _adj, notes = _check_stores(cur, [dict(a) for a in s["adjustments"]])
            if any(a["adjustment_type"] == "directed_shipment" for a in s["adjustments"]):
                notes.insert(0, "Directed shipment: ships regardless of the stores' sales history and "
                                "skips the due test, the 13-week review and the full-cart minimum. "
                                "Approving it also OVERRIDES a store's product restriction for this "
                                "shipment. Do-not-ship and variety bans (No Hedera) still apply.")
            s["notes"] = notes
    return {"pending": rows, "can_approve": user.get("role") in APPROVER_ROLES}


@router.post("/{scenario_id}/approve")
def approve_scenario(scenario_id: str, user=Depends(require_role(*APPROVER_ROLES))):
    """Activate a pending scenario. The approver can never be the submitter."""
    email = user.get("email", "")
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""SELECT created_by, status FROM planning_scenarios
                       WHERE id = %s FOR UPDATE""", (scenario_id,))
        s = cur.fetchone()
        if not s:
            raise HTTPException(404, "Scenario not found")
        if s["status"] != "pending_approval":
            raise HTTPException(409, f"Scenario is already {s['status']}")
        if (s["created_by"] or "").lower() == email.lower():
            raise HTTPException(403, "You cannot approve your own scenario — a second person signs off")
        cur.execute("""UPDATE planning_scenarios
                       SET status = 'approved', is_active = true, approved_by = %s, approved_at = now()
                       WHERE id = %s""", (email, scenario_id))
        cur.execute("UPDATE weekly_adjustments SET active = true WHERE scenario_id = %s", (scenario_id,))
    return {"id": scenario_id, "status": "approved", "approved_by": email}


@router.post("/{scenario_id}/reject")
def reject_scenario(scenario_id: str, body: RejectReq, user=Depends(require_role(*APPROVER_ROLES))):
    if not body.reason.strip():
        raise HTTPException(400, "Give the submitter a reason")
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""UPDATE planning_scenarios
                       SET status = 'rejected', is_active = false, approved_by = %s,
                           approved_at = now(), reject_reason = %s
                       WHERE id = %s AND status = 'pending_approval' RETURNING id""",
                    (user.get("email"), body.reason.strip()[:500], scenario_id))
        if not cur.fetchone():
            raise HTTPException(404, "Scenario not found or no longer pending")
        cur.execute("UPDATE weekly_adjustments SET active = false WHERE scenario_id = %s", (scenario_id,))
    return {"id": scenario_id, "status": "rejected"}


@router.get("/")
def list_scenarios(shipping_week: Optional[str] = None, user=Depends(get_current_user)):
    """All scenarios for a week (active + inactive) with their directives."""
    week = _resolve_week(shipping_week)
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""
            SELECT id, name, description, interpretation, shipping_week, is_active, created_by,
                   created_at, status, approved_by, approved_at, reject_reason
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
    """Turn an APPROVED scenario on/off for the run (its directives follow).
    A pending or rejected scenario cannot be switched on this way."""
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("""UPDATE planning_scenarios SET is_active = NOT is_active
                       WHERE id = %s AND status = 'approved' RETURNING is_active""",
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
