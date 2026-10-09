"""
JIM Deluxe — AI Explanation Router
Generates plain-English explanations for order decisions using Claude.
Results are cached in the DB after first generation.

Models:
  Run summary  → claude-sonnet-4-6  (one per run, quality matters)
  Store "Why?" → claude-haiku-4-5-20251001  (per-store volume, speed matters)
"""
import os
from fastapi import APIRouter, HTTPException, Depends
from db import get_db, cursor
from routers.auth import get_current_user

router = APIRouter()

_ANTHROPIC_AVAILABLE = False
try:
    import anthropic as _anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    pass


def _client():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured on this server.")
    if not _ANTHROPIC_AVAILABLE:
        raise HTTPException(503, "anthropic package not installed.")
    return _anthropic.Anthropic(api_key=key)


def _call(client, model: str, system: str, user_msg: str, max_tokens: int) -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
    )
    return resp.content[0].text.strip()


# ── Store-level explanation ───────────────────────────────────

_STORE_SYSTEM = """You are an AI assistant for TPC (The Plant Company), a wholesale plant \
grower shipping to ~2,000 Home Depot stores weekly. You help order writers understand \
why JIM Deluxe — TPC's AI ordering engine — made specific decisions.

Write in clear, direct prose. No bullet points, no headers. \
Reference the actual numbers provided. 3-4 sentences maximum. \
Speak from the perspective of explaining the algorithm's logic to \
a knowledgeable order writer who understands plant retail."""

def _build_store_prompt(o: dict, lines: list, carts: list) -> str:
    tier  = (o.get("dynamic_velocity_tier") or "?").strip()
    pace  = o.get("pace_ratio")
    pace_str = f"{pace*100:.1f}%" if pace is not None else "unknown"

    pri   = o.get("priority_score") or 0
    vel_c = o.get("velocity_component") or 0
    rec_c = o.get("recency_component") or 0
    pac_c = o.get("pace_component") or 0
    brd_c = o.get("breadth_component") or 0

    sc    = o.get("static_clock_weeks")
    dc    = o.get("dynamic_clock_weeks")
    grade = o.get("static_grade") or "?"
    days  = o.get("days_since_last_delivery")
    over  = o.get("is_overshipped", False)

    sku_lines = []
    for ln in (lines or [])[:6]:
        desc = (ln.get("description") or "")[:35]
        fam  = ln.get("family") or ""
        qty  = ln.get("draft_qty") or 0
        tier_prod = ln.get("legacy_product_group") or ""
        sku_lines.append(f"  {desc} ({fam}, {tier_prod}) — {qty} units")
    sku_text = "\n".join(sku_lines) or "  (no lines)"

    overstock_line = (
        "\nNOTE: Overshipped flag — dynamic clock exceeds static by more than 7 weeks, "
        "suggesting last delivery may not have fully cleared."
    ) if over else ""

    return f"""Explain why this Home Depot store received its order from JIM Deluxe this week.

STORE: {o.get('store_id')} — {o.get('store_name','')}, {o.get('city','')}, {o.get('state','')}
MERCHANT TERRITORY: {o.get('merchant','unknown')}
DYNAMIC VELOCITY TIER: {tier}
STORE CLOCK GRADE: {grade} | Static clock: {sc}wk | Dynamic clock: {dc}wk
DAYS SINCE LAST DELIVERY: {days if days is not None else 'no prior delivery'} days
SELL-THROUGH PACE: {pace_str}
PRIORITY SCORE: {pri:.4f}  \
(velocity {vel_c:.3f} + recency {rec_c:.3f} + pace {pac_c:.3f} + breadth {brd_c:.3f})
ORDER: {o.get('cart_count',0)} carts, {o.get('total_units',0):,} units
TOP SKUs SENT:
{sku_text}{overstock_line}"""


@router.post("/orders/{order_id}/explain")
def explain_order(order_id: str, user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)

        # Return cached result if available
        cur.execute("SELECT ai_explanation FROM orders WHERE id = %s", (order_id,))
        row = cur.fetchone()
        if row and row["ai_explanation"]:
            return {"explanation": row["ai_explanation"], "cached": True}

        # Gather order data
        cur.execute("""
            SELECT o.*, s.store_name, s.city, s.state, s.merchant,
                   s.dynamic_velocity_tier
            FROM orders o
            JOIN stores s ON s.store_id = o.store_id
            WHERE o.id = %s
        """, (order_id,))
        order = cur.fetchone()
        if not order:
            raise HTTPException(404, "Order not found")

        cur.execute("""
            SELECT ol.draft_qty, k.description, k.family, k.legacy_product_group
            FROM order_lines ol
            JOIN skus k ON k.sku_id = ol.sku_id
            WHERE ol.order_id = %s
            ORDER BY ol.draft_qty DESC
            LIMIT 8
        """, (order_id,))
        lines = cur.fetchall()

        cur.execute("""
            SELECT cart_number, config_code, total_units, shelves_used
            FROM order_carts WHERE order_id = %s ORDER BY cart_number
        """, (order_id,))
        carts = cur.fetchall()

    prompt = _build_store_prompt(order, lines, carts)
    explanation = _call(
        _client(),
        model="claude-haiku-4-5-20251001",
        system=_STORE_SYSTEM,
        user_msg=prompt,
        max_tokens=300,
    )

    # Cache
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute(
            "UPDATE orders SET ai_explanation = %s WHERE id = %s",
            (explanation, order_id)
        )

    return {"explanation": explanation, "cached": False}


# ── Run-level summary ─────────────────────────────────────────

_RUN_SYSTEM = """You are an AI assistant for TPC (The Plant Company), a wholesale plant \
grower shipping to ~2,000 Home Depot stores weekly. You help managers understand \
JIM Deluxe's weekly recommendation run before approving it for shipment.

Write a clear executive summary. No bullet points, no headers. \
4-6 sentences. Cover the overall plan health, key exclusions, grade distribution, \
notable findings, and inventory utilization. \
Write for a manager who will use this to decide whether to approve or adjust the plan."""

def _build_run_prompt(run: dict, grades: list, configs: list,
                      top_stores: list, overcount: int,
                      stores_excluded: int) -> str:
    grade_str = "  ".join(
        f"{r['static_grade']}: {r['cnt']} stores" for r in (grades or [])
    ) or "no grade data"

    cfg_str = ", ".join(
        f"{r['config_code']} ({r['cart_count']} carts)" for r in (configs or [])
    ) or "no config data"

    top_str = "; ".join(
        f"Store {r['store_id']} {r.get('store_name','')[:20].strip()} "
        f"({(r.get('dynamic_velocity_tier') or '?').strip()}, "
        f"{r.get('cart_count',0)} carts)"
        for r in (top_stores or [])
    ) or "none"

    return f"""Summarize this week's JIM Deluxe recommendation run for a manager review.

WEEK OF: {run.get('shipping_week','')}
STORES SERVED: {run.get('stores_served',0):,} of ~1,995 active HD locations
STORES EXCLUDED BY STORE CLOCK (not yet due): {stores_excluded:,}
TOTAL CARTS: {run.get('total_carts',0):,} across truck loads
TOTAL UNITS ALLOCATED: {run.get('total_units',0):,}
INVENTORY CLEARANCE: {run.get('inventory_clearance',0):.1f}%
GRADE DISTRIBUTION (served stores): {grade_str}
OVERSHIPPED FLAGS: {overcount} stores (dynamic clock >7wk above static)
TOP CART CONFIGS: {cfg_str}
TOP 5 STORES BY PRIORITY: {top_str}"""


@router.post("/runs/{run_id}/summarize")
def summarize_run(run_id: str, user=Depends(get_current_user)):
    with get_db() as conn:
        cur = cursor(conn)

        # Return cached result
        cur.execute(
            "SELECT ai_summary, stores_served, total_carts, total_units, "
            "inventory_clearance, shipping_week FROM recommendation_runs WHERE id = %s",
            (run_id,)
        )
        run = cur.fetchone()
        if not run:
            raise HTTPException(404, "Run not found")
        if run["ai_summary"]:
            return {"summary": run["ai_summary"], "cached": True}

        # Grade distribution of served stores
        cur.execute("""
            SELECT static_grade, COUNT(*) AS cnt
            FROM orders
            WHERE run_id = %s AND static_grade IS NOT NULL
            GROUP BY static_grade ORDER BY static_grade
        """, (run_id,))
        grades = cur.fetchall()

        # Cart config distribution
        cur.execute("""
            SELECT oc.config_code, COUNT(oc.id) AS cart_count
            FROM order_carts oc
            JOIN orders o ON o.id = oc.order_id
            WHERE o.run_id = %s
            GROUP BY oc.config_code
            ORDER BY cart_count DESC
            LIMIT 6
        """, (run_id,))
        configs = cur.fetchall()

        # Overshipped count
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM orders WHERE run_id = %s AND is_overshipped = true",
            (run_id,)
        )
        overcount = (cur.fetchone() or {}).get("cnt", 0)

        # Stores excluded (approximate: total active minus served)
        cur.execute("SELECT COUNT(*) AS cnt FROM stores WHERE active = true")
        total_active = (cur.fetchone() or {}).get("cnt", 1995)
        stores_excluded = total_active - (run.get("stores_served") or 0)

        # Top stores
        cur.execute("""
            SELECT o.store_id, o.cart_count, o.priority_score,
                   s.store_name, s.dynamic_velocity_tier
            FROM orders o
            JOIN stores s ON s.store_id = o.store_id
            WHERE o.run_id = %s
            ORDER BY o.priority_score DESC
            LIMIT 5
        """, (run_id,))
        top_stores = cur.fetchall()

    prompt = _build_run_prompt(run, grades, configs, top_stores,
                               overcount, stores_excluded)
    summary = _call(
        _client(),
        model="claude-sonnet-4-6",
        system=_RUN_SYSTEM,
        user_msg=prompt,
        max_tokens=500,
    )

    # Cache
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute(
            "UPDATE recommendation_runs SET ai_summary = %s WHERE id = %s",
            (summary, run_id)
        )

    return {"summary": summary, "cached": False}
