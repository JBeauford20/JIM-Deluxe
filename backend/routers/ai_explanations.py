"""
JIM Deluxe — AI Router
Generates plain-English explanations, run audits, and Q&A for order decisions using Claude.

Endpoints:
  POST /orders/{order_id}/explain    → store-level "Why?" explanation  (Haiku, cached)
  POST /runs/{run_id}/summarize      → full run executive summary       (Sonnet, cached)
  POST /runs/{run_id}/audit          → structured flag audit of all orders (Haiku, cached)
  POST /ask                          → free-text Q&A about any run/store  (Sonnet)

Prompt caching: the full TPC/JIM system prompt (_FULL_SYSTEM) is marked ephemeral
and cached by Anthropic for 5 minutes — all calls in a run audit share one cache hit.
"""
import os, json
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from db import get_db, cursor
from routers.auth import get_current_user

router = APIRouter()

# ── Tier targets (kept in sync with recommendation_engine.py) ────────────────
_TIER_CART_TARGETS = {'AA': 6, 'A': 4, 'B': 3, 'C': 2, 'D': 1, 'P': 1}

# ── Full TPC/JIM system prompt — cached by Anthropic (ephemeral, 5-min TTL) ──
_FULL_SYSTEM = """You are the AI audit and Q&A assistant embedded inside JIM Deluxe — The Plant Company's
weekly Home Depot order recommendation system. Your job is to audit generated orders for
problems, answer questions from order writers about why specific stores and SKUs ended up
on (or off) a given week's orders, and flag anything that doesn't pass the sniff test
before product ships.

You have deep knowledge of TPC's business, JIM's engine logic, and the historical
performance data that drives store scoring. You are direct, specific, and always ground
your answers in actual numbers from the data provided — not generalizations. When
something looks wrong, say so clearly. When something looks right, explain why briefly
so the order writer can trust it and move on.

---

THE PLANT COMPANY (TPC) — BUSINESS CONTEXT

The Plant Company is a large commercial houseplant grower. Its primary retail customer is
Home Depot, with approximately 2,000 store locations served across the United States.
TPC ships weekly — order writers use JIM to determine which stores get product, what
product they get, how many carts to build, and how those carts group onto truck loads.

Key operational facts:
- Shipping cycle: weekly. Every run covers one week's worth of store deliveries.
- Scale: up to ~2,000 stores eligible; a typical run services 200-400 stores.
- On-hand tracking at stores is NOT used by JIM — it is unreliable (third-party merch
  discards product without logging it). JIM is purely demand-driven: scans at the HD
  register are the ground truth.
- TPC has no visibility into what is physically on the shelf. The DUE gate prevents oversupply.

---

PRODUCT FAMILIES

- 9cm Littles: Standard (jute kit), Ceramic, Hanging Basket (HB). Highest volume SKU group.
- 12cm: Standard, Collectors Edition, Rare Collectors, Climb-itt.
- 17cm: Standard, Collectors Series, Rare Collectors, Climb-itt.
- H2O: Bowl (.804L), OPP, Venti (1.9L). Water-feature kits.
- Terrarium: Small, Medium, Large. Kit products.
- Climb-itt Accessories: Climbing Pole 19", Climbing Pole 30", Support Clips, Support Stakes.
- Dish Garden: Fall 8in. Seasonal.

Aster item hierarchy:
- 1M level: Bare plant in grower pot. Kit decided at order time. 50/50 jute/ceramic split.
- 3M level: Plant already kitted by contract grower.

---

PRICING TIERS — CRITICAL RULE

Tier ladder (lowest to highest): Premium < Collectors < Specialty < Boutique < Rare Collectors

For HD: Boutique and Rare Collectors are treated as equivalent.

HARD RULE — NEVER VIOLATE: A plant with Legacy Product Group of Collectors, Specialty,
Boutique, or Rare Collectors must NEVER be downgraded to Premium. If you see this,
flag it as FLAG severity requiring immediate human review. This is non-negotiable.

---

STORE TIERS — PRECISE DEFINITIONS

JIM uses dynamic_velocity_tier (recalculated weekly) for all scoring. Static volume_code
is the legacy field — may differ from dynamic tier.

  AA = Top 0.5% nationally (~9 stores)
  A  = Top 10% (below AA, ~185 stores)
  B  = Top 30% (below A, ~395 stores)
  C  = Top 60% (below B, ~595 stores)
  D  = Bottom 40% (~795 stores)
  P  = Provisional (<6 weeks of data)

A store's static and dynamic tiers can differ. Dynamic is the truth. A static-AA store
scoring dynamically as B has been declining and may have been historically over-served.

---

JIM ENGINE LOGIC

Stage 0 — Store Clock DUE Gate (hard binary filter, not a scoring penalty):
  Static clock  = ROUNDUP((2 carts x $2,534/cart x 0.70) / dollar_scans_per_week_40wk)
  Dynamic clock = CEIL(last_delivery_wholesale_value x 0.70 / dollar_scans_per_week_6wk)
  Store is DUE when: weeks_since_last_delivery >= dynamic_clock
  Clock capped between 2 and 13 weeks.
  Overshipped flag: dynamic_clock - static_clock > 7 weeks.

Stage 1 — Priority Score:
  Priority = (Velocity x 0.40) + (Recency x 0.30) + (Pace x 0.20) + (Breadth x 0.10)

  Velocity (40%): EWMA alpha=0.10. Half-life ~6.6 weeks. Slow and sticky by design.
  Recency (30%): 0-7 days since delivery = very low priority. 28+ days = max priority.
  Pace (20%): Sell-through rate = scans / deliveries over last 6 weeks.
              Target >= 70%. Below 40% is a serious oversupply signal.
  Breadth (10%): How many SKU groups the store actively sells. Wider = small boost.

Stage 2 — SKU Scoring:
  SKU Score = (Tier-avg Group Velocity x 0.65) + (Availability Weight x 0.18)
  Availability weight was reduced from 0.35 to 0.18 in Oct 2026 to let velocity dominate.

Stage 3 — Cart Building:
  Greedy fill. Max 2 shelves per SKU. 5 shelves per CC cart.
  Cart flagged if fewer than 4 shelves used (<80% fill rate).

  Standard capacities (CC cart):
    9cm Littles: 504 units/cart | 12cm: 144 units/cart | 17cm: 63 units/cart
    H2O Bowl: 216 units/cart | Climb-itt Accessories: varies

Stage 4.5 — Config consolidation to <=25 unique CC codes per run.
Stage 5   — Load building: 45 carts/truck, grouped by merchant/region/state.

Cart targets by tier: AA=6 | A=4 | B=3 | C=2 | D=1

---

AUDIT RED FLAGS

FLAG (stop and investigate):
- Collectors/Specialty/Boutique/Rare Collectors SKU coded or priced as Premium anywhere
- Store on the order but weeks_since_last_delivery < dynamic_clock (DUE gate failure)
- Store has OVER (overshipped) badge AND is on this week's order
- AA or A store completely absent with no clear reason (DUE gate or inventory depletion)
- Cart with fewer than 3 shelves used

WARNING (review before shipping):
- D-tier store receiving 2+ carts (target is 1 cart as of Oct 2026)
- Store pace_ratio < 0.25 (sell-through below 25% — high oversupply risk)
- SKU group on order where this store has very low group velocity for that family
- Store received fewer carts than its tier target without DUE gate or inventory explaining it
- Priority score below 0.35 for an allocated store
- Dynamic clock minus static clock gap of 5-7 weeks (approaching overshipped)

INFO (note for order writer):
- Static tier differs from dynamic tier by 2+ levels
- Provisional (P-tier) store on the order
- Store has not received a delivery in 60+ days
- Single SKU group across a multi-cart order

---

RESPONSE GUIDELINES

For audit flags: be brief and specific. Name the exact metric. Say what to check.
Do not over-flag — correct-but-unusual orders should be INFO at most.

For Q&A: answer the question directly first, then provide supporting numbers.
Use plain language — order writers are not data scientists.
Translate scores: "top 10% nationally" is more useful than "velocity score 0.847".

For "why was this store included?": lead with DUE gate status, then priority rank,
then the components that drove it. If everything checks out, say so clearly.

For "why was this store NOT on the order?": check DUE gate first (most common),
then inventory depletion, then confirm the store is active.

Tone: direct, confident, grounded in numbers. Not defensive of the engine.
If something looks wrong, say it looks wrong. You are the check on the system."""

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


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER AUDIT — batch flag check across every store in a run
# ═══════════════════════════════════════════════════════════════════════════════

def _build_audit_batch(stores: list) -> str:
    lines = [
        f"Audit the following {len(stores)} store orders from this JIM Deluxe run.",
        "Apply your red flag criteria. Return ONLY a valid JSON array.",
        "Each element must be: {\"store_id\": int, \"severity\": \"FLAG|WARNING|INFO\", \"category\": str, \"message\": str}",
        "Omit stores with no issues. Return [] if everything is clean.",
        "",
    ]
    for o in stores:
        tier      = (o.get("dynamic_velocity_tier") or "?").strip()
        s_tier    = (o.get("volume_code") or "?").strip()
        pace_pct  = (o.get("pace_ratio") or 0) * 100
        dc        = o.get("dynamic_clock_weeks")
        sc        = o.get("static_clock_weeks")
        gap       = o.get("clock_gap")
        days      = o.get("days_since_last_delivery")
        carts     = o.get("cart_count", 0)
        target    = _TIER_CART_TARGETS.get(tier, "?")
        over      = o.get("is_overshipped", False)
        grps      = ", ".join(o.get("sku_groups") or []) or "none"

        lines += [
            f"STORE {o.get('store_id')} — {o.get('store_name','')}, {o.get('state','')} | Merchant: {o.get('merchant','')}",
            f"  Dynamic tier: {tier} | Static tier: {s_tier}{'  [MISMATCH]' if tier != s_tier else ''}",
            f"  Priority: {o.get('priority_score',0):.4f}  (vel={o.get('velocity_component',0):.3f} rec={o.get('recency_component',0):.3f} pace={o.get('pace_component',0):.3f} brd={o.get('breadth_component',0):.3f})",
            f"  Pace (6wk sell-thru): {pace_pct:.1f}%",
            f"  Days since last delivery: {days if days is not None else 'unknown'}",
            f"  Static clock: {sc}wk | Dynamic clock: {dc}wk | Gap: {gap}wk | Overshipped: {'YES' if over else 'no'}",
            f"  Carts this order: {carts} (tier target: {target}) | Units: {o.get('total_units',0):,}",
            f"  SKU groups: {grps}",
            "",
        ]
    return "\n".join(lines)


@router.post("/runs/{run_id}/audit")
def audit_run(run_id: str, force: bool = False, user=Depends(get_current_user)):
    """
    Batch-audit every order in a run using Claude Haiku.
    Results are cached in order_audits — pass ?force=true to re-run.
    Returns flags grouped by severity: FLAG > WARNING > INFO.
    """
    with get_db() as conn:
        cur = cursor(conn)

        # Return cached audit unless force-refresh requested
        if not force:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM order_audits WHERE run_id = %s",
                (run_id,)
            )
            if (cur.fetchone() or {}).get("cnt", 0) > 0:
                cur.execute("""
                    SELECT store_id, severity, category, message, created_at
                    FROM order_audits WHERE run_id = %s
                    ORDER BY
                        CASE severity WHEN 'FLAG' THEN 1 WHEN 'WARNING' THEN 2 ELSE 3 END,
                        store_id
                """, (run_id,))
                flags = cur.fetchall()
                return {"flags": flags, "cached": True,
                        "total_flags": len(flags),
                        "flag_count":    sum(1 for f in flags if f["severity"] == "FLAG"),
                        "warning_count": sum(1 for f in flags if f["severity"] == "WARNING"),
                        "info_count":    sum(1 for f in flags if f["severity"] == "INFO")}

        # Pull all orders with full scoring context
        cur.execute("""
            SELECT o.id AS order_id, o.store_id, o.priority_score,
                   o.velocity_component, o.recency_component,
                   o.pace_component, o.breadth_component, o.pace_ratio,
                   o.days_since_last_delivery, o.static_clock_weeks,
                   o.dynamic_clock_weeks, o.clock_gap, o.is_overshipped,
                   o.cart_count, o.total_units,
                   s.store_name, s.city, s.state, s.merchant,
                   s.dynamic_velocity_tier, s.volume_code
            FROM orders o
            JOIN stores s ON s.store_id = o.store_id
            WHERE o.run_id = %s
            ORDER BY o.priority_score DESC
        """, (run_id,))
        orders = cur.fetchall()
        if not orders:
            raise HTTPException(404, "No orders found for this run.")

        # Pull SKU families per order
        cur.execute("""
            SELECT ol.order_id, sk.family
            FROM order_lines ol
            JOIN skus sk ON sk.sku_id = ol.sku_id
            JOIN orders o ON o.id = ol.order_id
            WHERE o.run_id = %s AND ol.draft_qty > 0
        """, (run_id,))
        order_families: dict = {}
        for row in cur.fetchall():
            order_families.setdefault(row["order_id"], set()).add(row["family"])

    # Attach families and convert to plain dicts
    orders_ctx = []
    for o in orders:
        od = dict(o)
        od["sku_groups"] = sorted(order_families.get(o["order_id"], set()))
        orders_ctx.append(od)

    # Batch through Claude Haiku (30 stores per call)
    client = _client()
    all_flags = []
    parse_failures = 0
    BATCH = 30
    for i in range(0, len(orders_ctx), BATCH):
        batch   = orders_ctx[i:i + BATCH]
        prompt  = _build_audit_batch(batch)
        raw     = _call(client,
                        model="claude-haiku-4-5-20251001",
                        system=_FULL_SYSTEM,
                        user_msg=prompt,
                        max_tokens=2000)
        try:
            start = raw.find("[")
            end   = raw.rfind("]") + 1
            if start >= 0 and end > start:
                batch_flags = json.loads(raw[start:end])
                if isinstance(batch_flags, list):
                    all_flags.extend(batch_flags)
        except Exception:
            parse_failures += 1

    # Clear any prior audit for this run, then write fresh flags
    with get_db() as conn:
        cur = cursor(conn)
        cur.execute("DELETE FROM order_audits WHERE run_id = %s", (run_id,))
        for flag in all_flags:
            if not isinstance(flag, dict) or "store_id" not in flag:
                continue
            sev = flag.get("severity", "INFO")
            if sev not in ("FLAG", "WARNING", "INFO"):
                sev = "INFO"
            cur.execute("""
                INSERT INTO order_audits (run_id, store_id, severity, category, message)
                VALUES (%s, %s, %s, %s, %s)
            """, (run_id,
                  flag.get("store_id"),
                  sev,
                  flag.get("category", "General")[:100],
                  flag.get("message", "")[:500]))

    return {
        "flags":         all_flags,
        "cached":        False,
        "total_flags":   len(all_flags),
        "flag_count":    sum(1 for f in all_flags if f.get("severity") == "FLAG"),
        "warning_count": sum(1 for f in all_flags if f.get("severity") == "WARNING"),
        "info_count":    sum(1 for f in all_flags if f.get("severity") == "INFO"),
        "stores_audited": len(orders_ctx),
        "parse_failures": parse_failures,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Q&A — free-text questions about any run or specific store
# ═══════════════════════════════════════════════════════════════════════════════

class AskRequest(BaseModel):
    run_id:   str
    store_id: Optional[int] = None
    question: str


@router.post("/ask")
def ask(body: AskRequest, user=Depends(get_current_user)):
    """
    Answer a free-text question about a run or a specific store's order.
    Pulls full context from DB before calling Claude Sonnet.
    """
    with get_db() as conn:
        cur = cursor(conn)

        # Run-level context
        cur.execute("""
            SELECT rr.shipping_week, rr.stores_served, rr.total_carts,
                   rr.total_units, rr.inventory_clearance, ab.source_filename
            FROM recommendation_runs rr
            JOIN availability_batches ab ON ab.id = rr.batch_id
            WHERE rr.id = %s
        """, (body.run_id,))
        run = cur.fetchone()
        if not run:
            raise HTTPException(404, "Run not found.")

        ctx = [
            f"RUN WEEK: {run['shipping_week']}",
            f"RUN STATS: {run['stores_served']} stores | {run['total_carts']} carts | "
            f"{run['total_units']:,} units | {run['inventory_clearance']:.1f}% clearance",
        ]

        if body.store_id:
            # Pull the order for this store in this run
            cur.execute("""
                SELECT o.id AS order_id, o.store_id, o.priority_score,
                       o.velocity_component, o.recency_component,
                       o.pace_component, o.breadth_component, o.pace_ratio,
                       o.days_since_last_delivery, o.static_clock_weeks,
                       o.dynamic_clock_weeks, o.clock_gap, o.is_overshipped,
                       o.cart_count, o.total_units, o.static_grade,
                       s.store_name, s.city, s.state, s.merchant,
                       s.dynamic_velocity_tier, s.volume_code
                FROM orders o
                JOIN stores s ON s.store_id = o.store_id
                WHERE o.run_id = %s AND o.store_id = %s
            """, (body.run_id, body.store_id))
            order = cur.fetchone()

            if order:
                pace_pct = (order["pace_ratio"] or 0) * 100
                ctx.append(
                    f"\nSTORE {order['store_id']} — {order['store_name']}, "
                    f"{order['city']}, {order['state']}"
                )
                ctx.append(f"Merchant territory: {order['merchant']}")
                ctx.append(
                    f"Dynamic tier: {order['dynamic_velocity_tier']} | "
                    f"Static tier (volume_code): {order['volume_code']}"
                )
                ctx.append(
                    f"Priority score: {order['priority_score']:.4f}  "
                    f"(velocity={order['velocity_component']:.3f}  "
                    f"recency={order['recency_component']:.3f}  "
                    f"pace={order['pace_component']:.3f}  "
                    f"breadth={order['breadth_component']:.3f})"
                )
                ctx.append(f"Pace — 6wk sell-through: {pace_pct:.1f}%  (target ≥70%)")
                ctx.append(
                    f"Days since last delivery: {order['days_since_last_delivery']}"
                )
                ctx.append(
                    f"Static clock: {order['static_clock_weeks']}wk  "
                    f"Dynamic clock: {order['dynamic_clock_weeks']}wk  "
                    f"Gap: {order['clock_gap']}wk  "
                    f"Overshipped: {'YES' if order['is_overshipped'] else 'No'}"
                )
                ctx.append(
                    f"Order: {order['cart_count']} carts  "
                    f"(tier target: {_TIER_CART_TARGETS.get(order['dynamic_velocity_tier'], '?')}), "
                    f"{order['total_units']:,} units"
                )

                # SKUs on the order
                cur.execute("""
                    SELECT sk.description, sk.family, sk.legacy_product_group, ol.draft_qty
                    FROM order_lines ol
                    JOIN skus sk ON sk.sku_id = ol.sku_id
                    WHERE ol.order_id = %s
                    ORDER BY ol.draft_qty DESC
                """, (order["order_id"],))
                lines = cur.fetchall()
                if lines:
                    sku_text = "\n".join(
                        f"  {r['description'][:40]} ({r['family']}, {r['legacy_product_group']}) — {r['draft_qty']} units"
                        for r in lines
                    )
                    ctx.append(f"SKUs ordered:\n{sku_text}")

                # Engine run_decisions for this store
                cur.execute("""
                    SELECT decision_type, message
                    FROM run_decisions
                    WHERE run_id = %s AND store_id = %s
                    ORDER BY created_at
                """, (body.run_id, body.store_id))
                decisions = cur.fetchall()
                if decisions:
                    dec_text = "\n".join(
                        f"  [{r['decision_type']}] {r['message']}" for r in decisions
                    )
                    ctx.append(f"Engine decisions logged:\n{dec_text}")

                # Velocity history (8 weeks)
                cur.execute("""
                    SELECT week_start, ewma_score, dynamic_tier
                    FROM velocity_history
                    WHERE store_id = %s
                    ORDER BY week_start DESC LIMIT 8
                """, (body.store_id,))
                hist = list(reversed(cur.fetchall()))
                if hist:
                    hist_str = " | ".join(
                        f"{r['week_start']}: {r['ewma_score']:.2f} ({r['dynamic_tier']})"
                        for r in hist
                    )
                    ctx.append(f"Velocity history (oldest→newest): {hist_str}")

            else:
                # Store not on the order — pull profile to help explain why
                ctx.append(f"\nSTORE {body.store_id} was NOT included in this run.")
                cur.execute("""
                    SELECT s.store_name, s.city, s.state, s.merchant,
                           s.dynamic_velocity_tier, s.volume_code,
                           s.dynamic_velocity_score,
                           (SELECT MAX(delivery_date) FROM deliveries
                            WHERE store_id = s.store_id) AS last_delivery
                    FROM stores s WHERE s.store_id = %s
                """, (body.store_id,))
                store = cur.fetchone()
                if store:
                    ctx.append(
                        f"Store profile: {store['store_name']}, {store['city']}, {store['state']}"
                    )
                    ctx.append(
                        f"Dynamic tier: {store['dynamic_velocity_tier']} | "
                        f"Static tier: {store['volume_code']} | "
                        f"Velocity score: {store['dynamic_velocity_score']}"
                    )
                    ctx.append(f"Last recorded delivery: {store['last_delivery']}")
                else:
                    ctx.append(f"Store {body.store_id} not found in the stores table.")

    full_context = "\n".join(ctx)
    user_msg     = f"{full_context}\n\nQUESTION FROM ORDER WRITER: {body.question}"

    answer = _call(
        _client(),
        model="claude-sonnet-4-6",
        system=_FULL_SYSTEM,
        user_msg=user_msg,
        max_tokens=600,
    )

    return {"answer": answer, "store_id": body.store_id, "run_id": body.run_id}
