"""
§25 independent AI review + §20 cart-config consolidation for an IPR v2 run.

Reviewer: assembles the run's raw evidence (objective readout, decision counts, samples of
every gate/exception) and has the model answer the §25 question list, returning a structured
verdict. Stored in ipr_ai_reviews (kind='review').

Consolidation: finds cart-config merges and applies ONLY the demand-neutral ones —
(a) configs identical except jute/ceramic kit assignment on kit-flexible SKUs (the split is a
network-level balancing choice, so realigning it changes no store's product), and
(b) configs identical as a multiset of shelves, differing only in shelf order.
Anything that would change what a store receives is NEVER applied: it is reported as a
proposal with the §20 economics (labor saved vs. forecast at risk) for human approval —
§19 forbids replacing better-selling products merely for standardization.
Stored in ipr_ai_reviews (kind='consolidation').
"""
import json
from collections import defaultdict

from fastapi import HTTPException

from db import get_db, cursor as _cursor
from services.ipr_ai import V2_SYSTEM, call_model

RELEASED = ("exported", "released", "shipped")

REVIEW_QUESTIONS = """Answer the §25 independent-review question list against the evidence:
does each served store need product; are deliveries mature; is inventory reliable; does inbound
cover need; does assortment match store sales; stores below grade threshold or below 60%;
is availability manufacturing demand anywhere; are carts filled with slow product; H2O/Terrarium
above 13-week demand; pipeline replenishment justified; would another store be a better use of
constrained SKUs; does the plan improve sales and sell-through; what is the effect on the $900K
wholesale minimum. Then return STRICT JSON (no markdown):
{"verdict": "PASS"|"WARNING"|"BLOCK",
 "confidence": "high"|"medium"|"low",
 "summary": "<4-6 sentences for the order writer>",
 "findings": [{"severity": "BLOCK"|"WARNING"|"INFO", "area": "<short>",
               "evidence": "<numbers from the data>", "recommendation": "<action>",
               "required_approval": "<role or null>"}],
 "objective_assessment": "<2-3 sentences on the $900K readout and lost-sales breakdown>"}"""


def _review_context(cur, run_id: str) -> dict:
    cur.execute("""SELECT shipping_week, status, ipr_summary, sales_cutoff_date
                   FROM recommendation_runs WHERE id = %s""", (run_id,))
    run = cur.fetchone()
    if not run:
        raise HTTPException(404, "Run not found")
    cur.execute("""SELECT decision_type, COUNT(*) AS n FROM run_decisions
                   WHERE run_id = %s GROUP BY 1 ORDER BY 2 DESC""", (run_id,))
    counts = {r["decision_type"]: r["n"] for r in cur.fetchall()}
    samples = {}
    for kind in ("review", "low_value_cart", "deferred_economics", "pushed_min_cart",
                 "min_cart_service_review", "stopped_13wk", "reduced_13wk", "gap_fill",
                 "allocation_shortfall", "capacity_cap", "assortment_block", "validation",
                 "duplicate_run"):
        cur.execute("""SELECT store_id, sku_id, reason FROM run_decisions
                       WHERE run_id = %s AND decision_type = %s
                       ORDER BY (data->>'severity') = 'warning' DESC, store_id LIMIT 8""",
                    (run_id, kind))
        rows = cur.fetchall()
        if rows:
            samples[kind] = [dict(r) for r in rows]
    cur.execute("""SELECT o.store_id, s.store_name, o.cart_count, o.total_units
                   FROM orders o JOIN stores s ON s.store_id = o.store_id
                   WHERE o.run_id = %s ORDER BY o.total_units DESC LIMIT 10""", (run_id,))
    top_orders = [dict(r) for r in cur.fetchall()]
    return {"shipping_week": str(run["shipping_week"]), "status": run["status"],
            "sales_as_of": str(run["sales_cutoff_date"]),
            "summary": run["ipr_summary"], "decision_counts": counts,
            "decision_samples": samples, "largest_orders": top_orders}


def run_ai_review(run_id: str, user_email: str | None) -> dict:
    """Reads in one transaction, calls the model with NO transaction open, stores the
    verdict in a third — the LLM call never holds database locks."""
    with get_db() as conn:
        ctx = _review_context(_cursor(conn), run_id)
    raw = call_model(
        V2_SYSTEM,
        "RUN EVIDENCE (JSON):\n" + json.dumps(ctx, default=str) + "\n\n" + REVIEW_QUESTIONS,
        max_tokens=4000)
    try:
        report = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except Exception:
        report = {"verdict": "WARNING", "confidence": "low",
                  "summary": raw[:2000], "findings": [],
                  "objective_assessment": "model returned unstructured output"}
    with get_db() as conn:
        cur = _cursor(conn)
        cur.execute("""INSERT INTO ipr_ai_reviews (run_id, kind, model, verdict, report, created_by)
                       VALUES (%s, 'review', 'claude-opus-5-5', %s, %s::jsonb, %s) RETURNING id""",
                    (run_id, report.get("verdict"), json.dumps(report, default=str), user_email))
        report["id"] = str(cur.fetchone()["id"])
    return report


# ── Independent scenario audit (checks and balances, 2026-11-20) ──────────────
# A scenario passes FOUR independent hands before it is considered done:
#   1. the parser LLM turns English into directives (preview only),
#   2. a human approver who is never the submitter signs off,
#   3. the engine applies deterministically, bounded by pace (trims are logged),
#   4. THIS audit — a separate model pass, fed the raw engine evidence, not the
#      parser's or approver's reasoning — verifies the effect was justified.

SCENARIO_AUDIT_SYSTEM = """You are the INDEPENDENT scenario auditor for TPC — IPR Engine. You did
not write, parse or approve these scenarios, and you must not defend them. Approved plain-English
scenarios (promotions, exclusions, pushes) were applied to this week's run by deterministic engine
code that trims every request to the store's measured selling pace. Your job: verify from the raw
evidence that each scenario's effect is commercially justified, that the engine's pace trims were
right, and that nothing shipped a store cannot sell.

For each scenario directive, judge:
- Did it reach the stores/products the plain English intended (lines/stores touched vs scope)?
- Where the engine trimmed a request to pace, was the trim correct — or still too generous?
- Did any boost push product past what the 13-week record supports?
- Did an exclusion starve stores with real demand (lost sales)?
- Was anything approved by the same person who requested it (a control failure to flag)?

Return STRICT JSON only:
{"verdict": "PASS"|"WARNING"|"BLOCK",
 "summary": "<3-5 sentences for the order writer and the approver>",
 "per_scenario": [{"label": "...", "verdict": "PASS"|"WARNING"|"BLOCK",
                   "evidence": "<numbers from the data>",
                   "finding": "<what is right or wrong>",
                   "recommendation": "<keep / trim further / reject next week / none>"}]}"""


def run_scenario_audit(run_id: str, user_email: str | None) -> dict:
    """Audit every scenario_applied decision of a run against the affected lines' raw
    evidence. Reads in one transaction, calls the model with none open, stores the verdict."""
    with get_db() as conn:
        cur = _cursor(conn)
        cur.execute("""SELECT shipping_week, sales_cutoff_date, ipr_summary
                       FROM recommendation_runs WHERE id = %s""", (run_id,))
        run = cur.fetchone()
        if not run:
            raise HTTPException(404, "Run not found")
        summ = run["ipr_summary"] or {}
        cur.execute("""SELECT reason, data FROM run_decisions
                       WHERE run_id = %s AND decision_type = 'scenario_applied'
                       ORDER BY created_at""", (run_id,))
        applied = [dict(r) for r in cur.fetchall()]
        if not applied:
            return {"verdict": "PASS", "summary": "No scenarios were applied to this run.",
                    "per_scenario": [], "id": None}
        cur.execute("""SELECT store_id, sku_id, reason FROM run_decisions
                       WHERE run_id = %s AND decision_type = 'scenario_exclusion'
                       ORDER BY store_id LIMIT 60""", (run_id,))
        exclusions = [dict(r) for r in cur.fetchall()]
        # raw line evidence for scenario-touched lines: forecast vs 13-wk rate vs allocation
        cur.execute("""SELECT store_id, sku_id, data FROM run_decisions
                       WHERE run_id = %s AND decision_type = 'line'
                         AND (data->>'method' LIKE %s OR data->>'cap_note' LIKE %s)
                       LIMIT 120""", (run_id, "%scenario%", "%scenario%"))
        touched = [{"store_id": r["store_id"], "sku_id": r["sku_id"],
                    "forecast_wk": (r["data"] or {}).get("forecast_wk"),
                    "r13": ((r["data"] or {}).get("rates") or {}).get("r13"),
                    "projected": (r["data"] or {}).get("projected"),
                    "alloc_trays": (r["data"] or {}).get("alloc_trays"),
                    "method": (r["data"] or {}).get("method"),
                    "cap_note": (r["data"] or {}).get("cap_note")} for r in cur.fetchall()]
    ctx = {"shipping_week": str(run["shipping_week"]), "data_as_of": str(run["sales_cutoff_date"]),
           "scenarios_applied": applied, "exclusion_samples": exclusions,
           "touched_line_evidence": touched,
           "run_scenario_summary": summ.get("scenarios")}
    raw = call_model(SCENARIO_AUDIT_SYSTEM,
                     "RAW RUN EVIDENCE (JSON):\n" + json.dumps(ctx, default=str)
                     + "\n\nAudit every scenario. STRICT JSON only.",
                     max_tokens=2500)
    try:
        report = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except Exception:
        report = {"verdict": "WARNING", "summary": raw[:1500], "per_scenario": []}
    with get_db() as conn:
        cur = _cursor(conn)
        cur.execute("""INSERT INTO ipr_ai_reviews (run_id, kind, model, verdict, report, created_by)
                       VALUES (%s, 'scenario_audit', 'claude-opus-5-5', %s, %s::jsonb, %s)
                       RETURNING id""",
                    (run_id, report.get("verdict"), json.dumps(report, default=str), user_email))
        report["id"] = str(cur.fetchone()["id"])
    return report


# ── Cart-config consolidation (§20) ──────────────────────────────────────────

def _load_carts(cur, run_id: str):
    cur.execute("""
        SELECT oc.id AS cart_id, oc.order_id, oc.store_id, oc.config_code, oc.template_id,
               cs.id AS shelf_id, cs.shelf_position, cs.sku_id, cs.profile_id, cs.kit_type,
               cs.tray_count, cs.unit_count
        FROM order_carts oc JOIN orders o ON o.id = oc.order_id
        LEFT JOIN cart_shelves cs ON cs.cart_id = oc.id
        WHERE o.run_id = %s
        ORDER BY oc.id, cs.shelf_position
    """, (run_id,))
    carts = {}
    for r in cur.fetchall():
        c = carts.setdefault(r["cart_id"], {"cart_id": str(r["cart_id"]), "store_id": r["store_id"],
                                            "config_code": r["config_code"],
                                            "template_id": r["template_id"], "shelves": []})
        if r["shelf_id"]:
            c["shelves"].append({"shelf_id": str(r["shelf_id"]), "pos": r["shelf_position"],
                                 "sku_id": r["sku_id"], "profile_id": r["profile_id"],
                                 "kit": r["kit_type"], "trays": r["tray_count"],
                                 "units": r["unit_count"]})
    return list(carts.values())


def _sig_full(c):      # exact signature incl. kit + position
    return tuple(sorted((s["pos"], s["sku_id"], s["trays"], s["kit"] or "") for s in c["shelves"]))


def _sig_nokit(c):     # position + sku + trays, kit ignored
    return tuple(sorted((s["pos"], s["sku_id"], s["trays"]) for s in c["shelves"]))


def _sig_nopos(c):     # multiset of (sku, trays, kit), shelf order ignored
    return tuple(sorted((s["sku_id"], s["trays"], s["kit"] or "") for s in c["shelves"]))


def _kit_flexible_skus(cur, run_id: str) -> set:
    """SKUs whose kit is decided at order time (a 1M / grower-pot availability line in the
    run's batch). Only these may have kit_type realigned: a 3M line is a finished product
    whose jute vs ceramic are physically different goods tied to different availability
    lines — flipping those is NOT demand-neutral."""
    cur.execute("""SELECT DISTINCT al.sku_id
                   FROM recommendation_runs rr
                   JOIN availability_lines al ON al.batch_id = rr.batch_id
                   WHERE rr.id = %s AND COALESCE(al.code_level, '3M') <> '3M'""", (run_id,))
    return {r["sku_id"] for r in cur.fetchall()}


def analyze_consolidation(cur, run_id: str, sku_families: dict,
                          kit_flexible: set | None = None) -> dict:
    """Group the run's carts into configs and find merges. Returns the analysis dict;
    nothing is written here."""
    carts = _load_carts(cur, run_id)
    by_code = defaultdict(list)
    for c in carts:
        by_code[c["config_code"]].append(c)
    configs = {code: {"carts": lst, "n": len(lst),
                      "sig_full": _sig_full(lst[0]), "sig_nokit": _sig_nokit(lst[0]),
                      "sig_nopos": _sig_nopos(lst[0])}
               for code, lst in by_code.items()}

    neutral, proposals = [], []
    codes = sorted(configs, key=lambda k: -configs[k]["n"])
    merged = set()
    for i, a in enumerate(codes):
        if a in merged:
            continue
        for b in codes[i + 1:]:
            if b in merged:
                continue
            A, B = configs[a], configs[b]
            if A["sig_nokit"] == B["sig_nokit"] and A["sig_full"] != B["sig_full"]:
                # kit-only difference. Neutral ONLY when every differing SKU is kit-flexible
                # (1M grower-pot line in this batch): a 3M jute vs ceramic kit is a different
                # physical product on a different availability line — that goes to proposals.
                pairs = list(zip(sorted(A["carts"][0]["shelves"], key=lambda s: (s["pos"], s["sku_id"])),
                                 sorted(B["carts"][0]["shelves"], key=lambda s: (s["pos"], s["sku_id"]))))
                diff_skus = {s2["sku_id"] for s1, s2 in pairs if (s1["kit"] or "") != (s2["kit"] or "")}
                if kit_flexible is not None and diff_skus <= kit_flexible:
                    neutral.append({"type": "kit_alignment", "survivor": a, "absorbed": b,
                                    "carts_moved": B["n"], "kit_flips_per_cart": len(diff_skus)})
                    merged.add(b)
                else:
                    proposals.append({
                        "config_a": a, "config_b": b, "carts_a": A["n"], "carts_b": B["n"],
                        "differs": {"kit_skus": sorted(diff_skus)},
                        "note": "kit differs on finished (3M) product — jute and ceramic are "
                                "different goods here; merging needs a human call"})
            elif A["sig_nopos"] == B["sig_nopos"] and A["sig_nokit"] != B["sig_nokit"]:
                neutral.append({"type": "shelf_order", "survivor": a, "absorbed": b,
                                "carts_moved": B["n"]})
                merged.add(b)
            else:
                # near-miss: same shelf count, exactly one differing (sku, trays) line,
                # same family + profile -> propose, never apply (§19: no standardization swaps)
                sa = set((s["sku_id"], s["trays"], s["profile_id"]) for s in A["carts"][0]["shelves"])
                sb = set((s["sku_id"], s["trays"], s["profile_id"]) for s in B["carts"][0]["shelves"])
                da, db_ = sa - sb, sb - sa
                if len(da) == 1 and len(db_) == 1:
                    (ska, tra, pa), (skb, trb, pb) = next(iter(da)), next(iter(db_))
                    if pa == pb and tra == trb and \
                       sku_families.get(ska) and sku_families.get(ska) == sku_families.get(skb):
                        proposals.append({
                            "config_a": a, "config_b": b, "carts_a": A["n"], "carts_b": B["n"],
                            "differs": {"sku_a": ska, "sku_b": skb, "trays": tra,
                                        "family": sku_families.get(ska)},
                            "note": "swapping either SKU to merge changes what stores receive — "
                                    "requires human approval (§19/§20)"})
    return {"configs": len(configs), "carts": len(carts),
            "neutral_merges": neutral, "proposals": proposals,
            "config_sizes": {k: configs[k]["n"] for k in codes}}


def apply_neutral_merges(cur, run_id: str, analysis: dict) -> dict:
    """Apply only the demand-neutral merges: update absorbed carts' shelf kits/positions to
    the survivor's exact layout, repoint config_code + template, refresh template counts.
    Units, trays and SKUs per store are untouched."""
    carts = _load_carts(cur, run_id)
    by_code = defaultdict(list)
    for c in carts:
        by_code[c["config_code"]].append(c)
    applied = []
    for m in analysis["neutral_merges"]:
        surv, absd = m["survivor"], m["absorbed"]
        if not by_code.get(surv) or not by_code.get(absd):
            continue
        ref = sorted(by_code[surv][0]["shelves"], key=lambda s: (s["sku_id"], s["trays"], s["pos"]))
        surv_tid = by_code[surv][0]["template_id"]
        for cart in by_code[absd]:
            mine = sorted(cart["shelves"], key=lambda s: (s["sku_id"], s["trays"], s["pos"]))
            # verify the whole cart matches the survivor layout BEFORE touching any row
            if len(mine) != len(ref) or any(
                    s_m["sku_id"] != s_r["sku_id"] or s_m["trays"] != s_r["trays"]
                    for s_m, s_r in zip(mine, ref)):
                continue
            for s_m, s_r in zip(mine, ref):
                cur.execute("""UPDATE cart_shelves SET kit_type = %s, shelf_position = %s
                               WHERE id = %s""", (s_r["kit"], s_r["pos"], s_m["shelf_id"]))
            cur.execute("""UPDATE order_carts SET config_code = %s, template_id = %s
                           WHERE id = %s""", (surv, surv_tid, cart["cart_id"]))
        applied.append(m)
    # refresh template counts; remove emptied templates
    cur.execute("""
        WITH live AS (
            SELECT oc.config_code, COUNT(*) AS carts, COUNT(DISTINCT oc.store_id) AS stores,
                   SUM(oc.total_units) AS units
            FROM order_carts oc JOIN orders o ON o.id = oc.order_id
            WHERE o.run_id = %s GROUP BY 1)
        UPDATE cart_config_templates t
        SET cart_count = live.carts, store_count = live.stores, total_units = live.units
        FROM live WHERE t.run_id = %s AND t.config_code = live.config_code
    """, (run_id, run_id))
    cur.execute("""
        SELECT t.id, t.config_code FROM cart_config_templates t
        WHERE t.run_id = %s AND NOT EXISTS (
            SELECT 1 FROM order_carts oc JOIN orders o ON o.id = oc.order_id
            WHERE o.run_id = %s AND oc.config_code = t.config_code)
    """, (run_id, run_id))
    emptied = cur.fetchall()
    for t in emptied:
        cur.execute("""DELETE FROM cart_config_template_varieties WHERE shelf_id IN
                       (SELECT id FROM cart_config_template_shelves WHERE template_id = %s)""",
                    (t["id"],))
        cur.execute("DELETE FROM cart_config_template_shelves WHERE template_id = %s", (t["id"],))
        cur.execute("DELETE FROM cart_config_templates WHERE id = %s", (t["id"],))
    return {"applied": applied, "templates_removed": [t["config_code"] for t in emptied]}


def run_consolidation(run_id: str, user_email: str | None, apply: bool) -> dict:
    """Analysis + apply run in ONE short transaction holding the run row FOR UPDATE (so a
    concurrent release/export serializes against the merge). The AI assessment runs with no
    transaction open; the report insert is its own transaction."""
    with get_db() as conn:
        cur = _cursor(conn)
        cur.execute("SELECT status FROM recommendation_runs WHERE id = %s FOR UPDATE", (run_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, "Run not found")
        if apply and (r["status"] or "") in RELEASED:
            raise HTTPException(409, "Run is already released — consolidation must happen before export")
        cur.execute("SELECT fm.sku_id, fm.ipr_family FROM ipr_family_map fm")
        fams = {x["sku_id"]: x["ipr_family"] for x in cur.fetchall()}
        analysis = analyze_consolidation(cur, run_id, fams, _kit_flexible_skus(cur, run_id))
        result = {"analysis": analysis, "applied": None}
        if apply and analysis["neutral_merges"]:
            result["applied"] = apply_neutral_merges(cur, run_id, analysis)

    # AI integrity check (§25 lens on §20): does any merge or proposal threaten order
    # integrity, and are the remaining proposals commercially worth a human look?
    assessment = call_model(
        V2_SYSTEM,
        "CART-CONFIG CONSOLIDATION RESULT (JSON). Demand-neutral merges "
        f"{'WERE applied' if result['applied'] else 'were NOT applied (preview)'}; proposals are "
        "never auto-applied because they change what a store receives.\n"
        + json.dumps(result, default=str)
        + "\n\nAssess in 4-8 sentences: (1) confirm or dispute that the applied merges are "
          "demand-neutral (same SKUs, trays and units per store); (2) for each proposal, weigh "
          "the §20 economics — configs saved vs. the risk of swapping a better-selling SKU — and "
          "say which, if any, deserve human approval; (3) flag anything that threatens order "
          "integrity. Ground every claim in the numbers above.",
        max_tokens=1200)
    result["ai_assessment"] = assessment
    verdict = (f"{len(analysis['neutral_merges'])} neutral merge(s)"
               f"{' applied' if result['applied'] else ' found'}, "
               f"{len(analysis['proposals'])} proposal(s) for approval")
    with get_db() as conn:
        cur = _cursor(conn)
        cur.execute("""INSERT INTO ipr_ai_reviews (run_id, kind, model, verdict, report, created_by)
                       VALUES (%s, 'consolidation', 'claude-opus-5-5', %s, %s::jsonb, %s) RETURNING id""",
                    (run_id, verdict, json.dumps(result, default=str), user_email))
        result["id"], result["verdict"] = str(cur.fetchone()["id"]), verdict
    return result
