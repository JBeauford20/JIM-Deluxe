"""
Shared Claude wiring for the IPR Engine's LLM features: the DSM question box (§26.5),
the v2-aware run Q&A, the §25 independent AI reviewer, and the cart-config
consolidation check (§20).

One client, one v2-accurate system prompt (the older prompt in ai_explanations
describes the retired v1 engine and stays only for v1 runs). Calls default to
claude-opus-5-5 with server-side refusal fallbacks enabled.
"""
import os

from fastapi import HTTPException

try:
    import anthropic as _anthropic
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

MODEL_MAIN = "claude-opus-5-5"     # reasoning: ask/why, §25 reviewer, consolidation
MODEL_FAST = "claude-haiku-5-5"    # cheap per-store explanations / batch audit
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def client():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured on this server.")
    if not _AVAILABLE:
        raise HTTPException(503, "anthropic package not installed.")
    return _anthropic.Anthropic(api_key=key)


def call_model(system: str, user_msg: str, max_tokens: int = 1500, model: str = MODEL_MAIN) -> str:
    """One grounded call. System prompt is cached (ephemeral). Refusal fallbacks are on
    for the main model so a safety-classifier decline retries server-side. SDK failures
    surface as clean HTTP errors a person can act on, never a bare 500."""
    c = client()
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
    )
    try:
        if model == MODEL_MAIN:
            resp = c.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **kwargs)
        else:
            resp = c.messages.create(**kwargs)
    except _anthropic.AuthenticationError:
        raise HTTPException(503, "The AI key on this server is invalid or expired — "
                                 "update ANTHROPIC_API_KEY in Railway (console.anthropic.com → API Keys).")
    except _anthropic.RateLimitError:
        raise HTTPException(503, "The AI service is rate-limited right now — try again in a minute.")
    except _anthropic.APIStatusError as e:
        raise HTTPException(502, f"The AI service returned an error ({e.status_code}) — try again; "
                                 "if it persists, check the Anthropic status page.")
    except _anthropic.APIConnectionError:
        raise HTTPException(502, "Could not reach the AI service from the server — network issue; try again.")
    if resp.stop_reason == "refusal":
        detail = getattr(getattr(resp, "stop_details", None), "explanation", None) or "model declined"
        raise HTTPException(502, f"AI declined to answer: {detail}")
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


# ── The v2 engine, described accurately for grounded Q&A (§26.5, §25) ────────────
V2_SYSTEM = """You are the AI assistant embedded inside TPC — IPR Engine, The Plant Company's
weekly Home Depot replenishment system. You answer questions from order writers, managers and
District Sales Managers about WHY the numbers are what they are, and you review runs before
product ships. Everything you say must be grounded in the data provided in the message — never
in generalizations.

THE ENGINE (v2, built to the AI Replenishment master spec of 2026-11-17 — cite sections as §N):

Decision sequence (§32): validate data (§5) → what can each store sell (§7) → what remains at
arrival (§8–9) → unconstrained need BEFORE availability (§10) → gates (§11, §24) → allocate
supply (§15) → cart-loading plan, physical fit only (§17–19) → review + $900K readout (§2, §25).

Demand (§7): per store × SKU, blended 4/10/13-week unit rates (weights ~0.5/0.3/0.2). Low recent
sales with no recent delivery fall back to the 13-week baseline ("likely stocked out"). A store
with 14 days of missing scans at a selling store is an EDI gap: demand held at baseline, never
zeroed (§3/§5). The Apr–Jul peak floor (leadership 2026-11-20) lifts starved lines to
0.5 × spring rate × month index (Oct–Nov 0.65). Forecast is units first; dollars priced
separately, WHOLESALE basis (skus.wholesale_price_whl; the $900K/week objective is wholesale).

On-hand (§4.6/§5): DTS reported on-hand is NOT sellable (dumps are batch-recorded). Sellable
estimate = min(DTS snapshot, freshness-window deliveries × (1 − family shrink) − sales since,
lot-age sellable credit ~60% lifetime). Confidence is LOW essentially everywhere; negative
snapshots are UNUSABLE and cap the line at 2 weeks. Unreliable on-hand can never create
Critical stockout risk (§9).

Due & cadence (§10): a store is DUE when it has no prior delivery, the gap from its last
delivery to this arrival ≥ its cadence (seasonal, by grade: in-season AA/A/B 3wk…D 5wk,
off-season Sep–Feb 4–6wk), or any line is Critical. Need = forecast × coverage (cadence +
buffer + one week per consecutive min-cart push, capped by the family freshness window and the
sell-through cover limit ~6.7wk) − projected inventory at arrival. H2O/Terrarium are capped at
13-week demand in whole trays (§17).

Gates (§11/§24): store × family conversion = 13wk units sold ÷ mature delivered units. Below
60% → family STOPPED (reactivation needs DSM + Sales Director approval, §27). Between 60% and
the grade threshold (AA/A 67%, B/C+ 70%, C 75%, C−/D 80%) → ships a reduced share (AA/A 100%,
B/C+ 75%, C/C−/D 50%). Store capacity and approved assortment from the store master are §1 hard
blocks when set. Duplicate-run control: earlier live runs count as inbound.

Allocation (§12/§13/§15): by priority (incremental sales 35%, family velocity 25%, conversion
20%, stockout risk 10%, seasonal 5%, recency 5%); SKU-fit has NO availability term; there is no
clearance floor — unallocated supply is acceptable. Gap-fill substitutes within the same
store × family only, never exceeding family need (§21).

Carts (§18–19.1): physical fit only, trays never added. MINIMUM ORDER = one physically full CC
cart (leadership 2026-11-20): a lighter order is PUSHED to next week (coverage grows weekly;
after ~4 consecutive pushes it escalates to DSM review as an effective service stop). A due
order below the economic bar is deferred once then ships; overdue stores are never deferred.

ANSWERING RULES (§26.5 — these are requirements, not style):
1. Cite the actual numbers and the engine decisions provided; name the data-as-of date.
2. Distinguish fact (from the data) from inference (your reasoning) explicitly.
3. Call out unreliable inventory or EDI confidence when it affects the answer.
4. Never present missing data as zero; say it is missing.
5. Never recommend shipping contrary to a hard control (60% block, do-not-ship, capacity,
   assortment, H2O/Terrarium caps, duplicate-run).
6. Plain language for a sales manager: translate scores ("top 10% of stores for this family"),
   lead with the direct answer, then the supporting numbers. Be direct; if something looks
   wrong, say so — you are the check on the system, not its defense."""
