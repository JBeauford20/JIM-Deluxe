"""Leadership report — the one-page HTML summary of a week's orders, same layout every week.

Josh 2026-11-23: "an easy-read HTML doc that leadership can read": the sales forecast, the
order at a high level (stores, carts, trucks), an AI overview of why the orders look this way,
which exceptions were used and how fully they were followed, and how the order holds up against
the leadership rules.

Every NUMBER and every pass/fail in the document is computed here from the run's saved evidence
(recommendation_runs.ipr_summary, run_decisions, orders, scenarios). The AI writes only the
plain-language overview, from those same facts; if the AI is unavailable the document still
generates with a short computed overview in its place.

facts(cur, run_id) -> dict      everything the document shows
overview(facts)    -> dict      the AI narrative (call with no DB connection held)
render(facts, ai)  -> str       the standalone HTML document
"""
from __future__ import annotations

import hashlib
import json
import math
from html import escape as E

from services import release_report

CARTS_PER_TRUCK = 45


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _n(v):
    return "—" if v is None else f"{_f(v):,.0f}"


def _usd(v):
    return "—" if v is None else f"${_f(v):,.0f}"


def _pct(v, digits=0):
    return "—" if v is None else f"{_f(v) * 100:.{digits}f}%"


def _week(d):
    return d.strftime("%b %d, %Y").replace(" 0", " ") if hasattr(d, "strftime") else str(d)


# ── facts ────────────────────────────────────────────────────────────────────
def _exceptions(cur, run_id, week):
    """One row per scenario adjustment the run applied, with how fully it was carried out."""
    cur.execute("""SELECT reason, data FROM run_decisions
                   WHERE run_id = %s AND decision_type = 'scenario_applied' ORDER BY created_at""", (run_id,))
    applied = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT reason FROM run_decisions WHERE run_id = %s AND decision_type = 'validation'
                   AND store_id IS NULL AND reason ILIKE 'Directed shipment of%%'""", (run_id,))
    short_notes = [r["reason"] for r in cur.fetchall()]
    cur.execute("""SELECT COUNT(*) AS n FROM run_decisions WHERE run_id = %s AND decision_type = 'validation'
                   AND reason ILIKE '%%NOT sent: the store has no other product%%'""", (run_id,))
    prebuilt_alone = cur.fetchone()["n"]
    out = []
    for a in applied:
        d = a["data"] or {}
        row = {"label": None, "type": d.get("adjustment_type"), "requested_by": d.get("requested_by"),
               "approved_by": d.get("approved_by"), "stores_asked": d.get("stores"),
               "trays_requested": d.get("trays_requested"), "trays_granted": d.get("trays_granted"),
               "stores_served": None, "units_shipped": None, "request": None,
               "notes": []}
        reason = a["reason"] or ""
        if "'" in reason:
            row["label"] = reason.split("'")[1]
        adj = None
        if d.get("scenario_id"):
            cur.execute("""SELECT wa.label, wa.scope_store_ids, wa.scope_sku_id, wa.value_text,
                                  ps.description FROM weekly_adjustments wa
                           JOIN planning_scenarios ps ON ps.id = wa.scenario_id
                           WHERE wa.scenario_id = %s AND wa.active ORDER BY (wa.label = %s) DESC LIMIT 1""",
                        (d["scenario_id"], row["label"]))
            adj = cur.fetchone()
        if adj:
            row["label"] = row["label"] or adj["label"]
            row["request"] = " ".join((adj["description"] or "").split())[:220]
            ids = adj["scope_store_ids"] or []
            if ids:
                row["stores_asked"] = len(ids)
            if ids and adj["scope_sku_id"]:
                cur.execute("""SELECT COUNT(DISTINCT od.store_id) AS stores,
                                      COALESCE(SUM(COALESCE(ol.approved_qty, ol.draft_qty, ol.recommended_qty)), 0) AS units
                               FROM orders od JOIN order_lines ol ON ol.order_id = od.id
                               WHERE od.run_id = %s AND ol.sku_id = %s AND od.store_id = ANY(%s)
                                 AND COALESCE(ol.approved_qty, ol.draft_qty, ol.recommended_qty) > 0""",
                            (run_id, adj["scope_sku_id"], list(ids)))
                got = cur.fetchone()
                row["stores_served"], row["units_shipped"] = got["stores"], int(got["units"] or 0)
            name = (adj["value_text"] or "").split("|")[-1].strip()
            row["notes"] += [n for n in short_notes if name and name.lower() in n.lower()]
        asked, served = row["stores_asked"], row["stores_served"]
        if served is not None and asked:
            row["followed"] = served / asked
        elif row["trays_requested"]:
            row["followed"] = _f(row["trays_granted"]) / _f(row["trays_requested"])
        else:
            row["followed"] = None
        if (row["label"] or "").lower().find("bundle") >= 0 and prebuilt_alone:
            row["notes"].append(f"{prebuilt_alone} store(s) did not get it because they had no other product "
                                f"this week; a pre-built cart only travels with at least one cart of product.")
        out.append(row)
    cur.execute("""SELECT name, status, created_by, approved_by FROM planning_scenarios
                   WHERE shipping_week = %s AND status = 'rejected' ORDER BY created_at""", (week,))
    rejected = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT COUNT(*) AS n FROM planning_scenarios
                   WHERE shipping_week = %s AND status IN ('pending', 'proposed', 'submitted')""", (week,))
    pending = cur.fetchone()["n"]
    return out, rejected, pending


def facts(cur, run_id: str) -> dict | None:
    base = release_report.build(cur, run_id)
    if not base:
        return None
    cur.execute("SELECT ipr_summary, shipping_week FROM recommendation_runs WHERE id = %s", (run_id,))
    run = cur.fetchone()
    s, week = run["ipr_summary"] or {}, run["shipping_week"]
    ce, a, c, o = (s.get("cart_economics") or {}, s.get("allocation") or {}, s.get("counts") or {},
                   s.get("objective") or {})
    cad, sr, g = s.get("delivery_cadence") or {}, s.get("store_rules") or {}, s.get("availability_grades") or {}

    cur.execute("""SELECT COUNT(*) FILTER (WHERE cart_count > 0) AS stores, COALESCE(SUM(cart_count), 0) AS carts,
                          COALESCE(SUM(total_units), 0) AS units FROM orders WHERE run_id = %s""", (run_id,))
    live = cur.fetchone()
    stores, carts, units = int(live["stores"] or 0), int(live["carts"] or 0), int(live["units"] or 0)
    cur.execute("SELECT COUNT(*) AS n, MIN(source) AS source FROM loads WHERE run_id = %s", (run_id,))
    ld = cur.fetchone()
    if ld["n"]:
        trucks = {"count": int(ld["n"]), "routed": True,
                  "note": "routed loads on this run" + (f" ({ld['source']})" if ld["source"] else "")}
    else:
        trucks = {"count": math.ceil(carts / CARTS_PER_TRUCK) if carts else 0, "routed": False,
                  "note": f"estimate at {CARTS_PER_TRUCK} carts per truck; routing sets the final count"}

    # stores shipping, split by what put them on the order
    cur.execute("""SELECT COUNT(*) FILTER (WHERE dir) AS directed, COUNT(*) FILTER (WHERE NOT dir) AS routine
                   FROM (SELECT od.store_id, EXISTS (SELECT 1 FROM run_decisions d WHERE d.run_id = od.run_id
                                AND d.store_id = od.store_id AND d.decision_type = 'directed_shipment') AS dir
                         FROM orders od WHERE od.run_id = %s AND od.cart_count > 0) x""", (run_id,))
    split = cur.fetchone()
    cur.execute("""SELECT COUNT(*) AS n FROM orders od JOIN stores st ON st.store_id = od.store_id
                   WHERE od.run_id = %s AND od.cart_count > 0 AND st.store_status = 'do_not_ship'""", (run_id,))
    dns_shipping = cur.fetchone()["n"]
    cur.execute("""SELECT COUNT(*) AS n FROM run_decisions
                   WHERE run_id = %s AND decision_type = 'store_rule_override'""", (run_id,))
    overrides = cur.fetchone()["n"]

    exceptions, rejected, pending = _exceptions(cur, run_id, week)
    ol = base.get("outlook") or {}
    goal = _f(ol.get("goal") or o.get("minimum_weekly_retail"))
    low, high = (ce.get("assumptions") or {}).get("normal_wholesale_range") or [1700, 3000]
    per_cart = ce.get("avg_wholesale_per_cart")
    pushed = ce.get("pushed_min_cart") or {}
    by_grade = g.get("by_grade") or {}
    g3, g2 = by_grade.get("3") or {}, by_grade.get("2") or {}

    def share(x):
        return (_f(x.get("shipping_units")) / _f(x.get("available_units"))) if _f(x.get("available_units")) else None

    checks = []

    def check(rule, status, result, detail="", tier="supporting"):
        checks.append({"rule": rule, "status": status, "result": result, "detail": detail, "tier": tier})

    # plants only: hardware and pre-built carts are counted as "units" on the availability
    # sheet but are not plants (CEO review: 283,730 "available" against 135,687 on his summary)
    cur.execute("""SELECT COALESCE(SUM(al.units_available), 0) AS total,
                          COALESCE(SUM(al.units_available) FILTER (WHERE COALESCE(fm.ipr_family, '') = 'Prebuilt Cart'
                              OR (COALESCE(fm.ipr_family, '') = 'Climb-ITT' AND COALESCE(k.program, '') = '')), 0) AS hard
                   FROM availability_lines al JOIN skus k ON k.sku_id = al.sku_id
                   LEFT JOIN ipr_family_map fm ON fm.sku_id = al.sku_id
                   WHERE al.batch_id = (SELECT batch_id FROM recommendation_runs WHERE id = %s)""", (run_id,))
    av = cur.fetchone()
    cc = s.get("cart_completion") or {}

    if ol:
        check(f"Weekly sales of at least {_usd(goal)} (recovery floor)",
              "met" if _f(ol.get("gap")) <= 0 else "not_met",
              f"Expected {_usd(ol.get('expected'))}, {_usd(abs(_f(ol.get('gap'))))} "
              f"{'above' if _f(ol.get('gap')) <= 0 else 'short of'} the floor",
              f"Baseline forecast {_usd(ol.get('baseline'))} plus {_usd(ol.get('added'))} added by this order.",
              tier="primary")
        if ol.get("growth_goal"):
            check(f"Weekly sales of {_usd(ol.get('growth_goal'))} (growth objective)",
                  "met" if _f(ol.get("gap_to_growth")) <= 0 else "not_met",
                  f"{_usd(abs(_f(ol.get('gap_to_growth'))))} "
                  f"{'above' if _f(ol.get('gap_to_growth')) <= 0 else 'short of'} the objective",
                  f"Store demand the system can identify is {_usd(ol.get('demand_forecast'))} a week; the "
                  f"rest of the gap needs more demand found, not only more product shipped.", tier="primary")
        _dem = _f(ol.get("demand_forecast"))
        if _dem:
            check("Identified store demand is being served",
                  "met" if _f(ol.get("expected")) >= 0.9 * _dem else
                  "partly" if _f(ol.get("expected")) >= 0.75 * _dem else "not_met",
                  f"Expected sales are {_pct(_f(ol.get('expected')) / _dem)} of the {_usd(_dem)} demand forecast",
                  f"Sales at risk this week: {_usd(o.get('lost_minimum_cart_pushes_one_week'))} from orders held "
                  f"under the full-cart rule, {_usd(o.get('lost_unavailable_inventory'))} from product not "
                  f"available, {_usd(o.get('lost_blocks_and_holds'))} from the sell-through review and store "
                  f"rules.", tier="primary")
        check("Estimated margin dollars", "info", "Not reported yet",
              "Needs approved category margin estimates; none have been supplied.", tier="primary")
    check("No order ships without one full cart",
          "met" if int(_f(ce.get("min_order_full_carts"), 1)) > 0 else "not_met",
          f"{_n(split['routine'])} routine store order(s) ship, each with a full cart; "
          f"{_n(pushed.get('stores'))} lighter orders were pushed to next week",
          f"{_n(split['directed'])} more store(s) ship under an approved directed shipment, which is exempt "
          f"from the minimum. A full cart means at least {_pct(ce.get('min_cart_fill_pct'))} of its shelf space is filled."
          + (f" Qualified gap-fill completed a cart at {_n(cc.get('stores'))} of {_n(cc.get('stores_tried'))} "
             f"short stores, adding {_n(cc.get('units'))} units beyond their own need; "
             f"{_n(cc.get('stores_short'))} had no qualified product to finish a cart."
             if cc.get("enabled") else " Gap-fill to complete carts was not in use for this order."))
    if per_cart is not None:
        check(f"Cart value between {_usd(low)} and {_usd(high)} at wholesale",
              ("met" if _f(ce.get("low_value_carts")) <= 0.25 * max(1.0, _f(ce.get("carts"))) else "partly")
              if low <= _f(per_cart) <= high else "not_met",
              f"Average {_usd(per_cart)} per cart; {_n(ce.get('low_value_carts'))} of {_n(ce.get('carts'))} "
              f"carts are under {_usd(low)}",
              "Directed-shipment carts and pre-built carts are counted in the average.")
    if ce.get("freight_pct") is not None:
        check("Delivery cost in proportion to the order",
              "info", f"Freight is {_pct(ce.get('freight_pct'), 1)} of order value "
                      f"({_usd(ce.get('delivered_cost'))} for {_n(ce.get('carts'))} carts)",
              f"Costed at {_usd((ce.get('assumptions') or {}).get('cart_delivered_cost'))} per delivered cart.")
    check("Products that do not sell through are held back",
          "met", f"{_n(c.get('lines_stopped_13wk'))} store-product lines stopped and "
                 f"{_n(c.get('lines_reduced_13wk'))} reduced at the 13-week sell-through review",
          f"{_n(a.get('stopped_13wk_units'))} units held; {_n(c.get('lines_reactivated'))} line(s) were "
          f"reactivated by approval.")
    check("Do-not-ship stores and store-master rules are respected",
          "met" if not dns_shipping else "not_met",
          f"{_n(dns_shipping)} do-not-ship store(s) on the order; {_n(sr.get('units_not_shipped_by_rule'))} units "
          f"kept off orders by store rules",
          (f"{_n(overrides)} store rule(s) were set aside for a manager-approved directed shipment."
           if overrides else "No store rule was set aside."))
    # only a manager can approve a scenario (routers/scenarios.py), so a named approver is the test
    unapproved = [x for x in exceptions if not x["approved_by"]]
    check("Exceptions and rule changes go through the IPR Manager",
          "met" if not unapproved else "not_met",
          (f"All {len(exceptions)} exception(s) in this order were approved by a manager" if exceptions
           else "No exceptions were used") + f"; {len(base['policy_changes'])} policy change(s) since the last run, all logged",
          f"{len(rejected)} request(s) were rejected and are not in the order."
          + (f" {pending} request(s) are still waiting for approval." if pending else ""))
    if g3 and g2:
        s3, s2 = share(g3), share(g2)
        check("Grade 3 product ships before grade 2",
              "met" if (s3 or 0) >= (s2 or 0) else "partly",
              f"{_pct(s3)} of grade 3 availability is shipping against {_pct(s2)} of grade 2",
              f"Grade 3: {_n(g3.get('shipping_units'))} of {_n(g3.get('available_units'))} units. "
              f"Grade 2: {_n(g2.get('shipping_units'))} of {_n(g2.get('available_units'))}.")
    if cad:
        check("Stores are shipped on their delivery cycle",
              "met", f"{_n(cad.get('stores_due'))} stores were due; {_n(cad.get('stores_not_due'))} not yet due "
                     f"were left off",
              "Cycle length by store grade: " + ", ".join(
                  f"{k} {v}wk" for k, v in sorted(((cad.get('settings') or {}).get('delivery_cadence_weeks') or {}).items())))

    return {
        "run": base["run"], "week": week, "headline": {"stores": stores, "carts": carts, "units": units,
                                                       "value": ce.get("wholesale_value"), "per_cart": per_cart,
                                                       "freight_pct": ce.get("freight_pct")},
        "trucks": trucks, "split": {"routine": split["routine"], "directed": split["directed"]},
        "outlook": ol, "not_shipped": base["not_shipped"], "divisions": base["divisions"],
        "families": base["families"], "exceptions": exceptions, "rejected": rejected, "pending": pending,
        "overrides": overrides, "checks": checks, "manual_edits": base["manual_edits"],
        "policy_changes": len(base["policy_changes"]), "status_changes": len(base["status_changes"]),
        "cart_completion": cc,
        "availability": {"plant_units": int(_f(av["total"]) - _f(av["hard"])), "hard_goods_units": int(_f(av["hard"])),
                         "note": "hard goods are Climb-itt hardware and pre-built carts, not plants"},
        "data": base["data"], "allocation": {"available_units": a.get("available_units"),
                                             "allocated_units": a.get("allocated_units"),
                                             "unfilled_demand_units": a.get("unfilled_demand_units")},
        "cadence_by_grade": cad.get("by_grade"),
    }


def fingerprint(f: dict) -> str:
    return hashlib.sha1(json.dumps(f, sort_keys=True, default=str).encode()).hexdigest()


# ── the AI overview ──────────────────────────────────────────────────────────
OVERVIEW_PROMPT = """You are writing the overview section of a weekly one-page report that goes to
the leadership team of The Plant Company about this week's Home Depot orders. They are senior,
busy, and not technical. They want to know why the order looks the way it does.

Use ONLY the facts in the JSON below. Every number you write must appear in the facts or be a
simple sum, difference or share of numbers that do. Never invent a cause the facts do not show.
Revenue and sell-through are the controlling results; cart and operating rules support them. Lead
with revenue against the floor and the growth objective. When you compare what shipped with what
was available, use availability.plant_units (allocation.available_units also counts hardware).
Plain business English: no section numbers, no system names, no words like "gate", "run",
"directive", "allocation", "engine" or "pipeline" (say "the system" if you must). Be direct, and
say plainly when something is weak or needs a decision: do not defend the order.

Return ONLY a JSON object with these keys:
  "headline"   one sentence: the single most important thing about this week's order
  "why"        4 to 6 short bullets: why the order is this size and shape (what drove stores on or
               off the order, what limited it, how it compares with what stores could sell)
  "exceptions" 1 to 3 short bullets: what the approved exceptions did to the order and how fully
               they were carried out
  "watch"      2 to 4 short bullets: what leadership should watch or decide

Each bullet is one or two sentences, with the numbers that support it."""


def overview(f: dict) -> dict:
    """Ask the model for the narrative. Raises HTTPException when the AI cannot be reached."""
    from services import ipr_ai
    slim = {k: f[k] for k in ("week", "headline", "trucks", "split", "outlook", "not_shipped", "divisions",
                              "exceptions", "rejected", "pending", "overrides", "checks", "manual_edits",
                              "allocation", "cadence_by_grade", "data")}
    slim["availability"], slim["cart_completion"] = f.get("availability"), f.get("cart_completion")
    slim["families"] = f["families"][:10]
    raw = ipr_ai.call_model(OVERVIEW_PROMPT, "FACTS (JSON):\n" + json.dumps(slim, default=str), max_tokens=6000)
    try:
        out = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except Exception:
        out = {"headline": "", "why": [raw[:1500]], "exceptions": [], "watch": []}
    return {k: (out.get(k) if k == "headline" else [str(x) for x in (out.get(k) or [])][:6])
            for k in ("headline", "why", "exceptions", "watch")}


def computed_overview(f: dict) -> dict:
    """What the document says when the AI is unavailable: the same facts, in fixed sentences."""
    h, ol = f["headline"], f["outlook"] or {}
    why = [f"{_n(h['stores'])} stores ship on {_n(h['carts'])} carts: {_n(f['split']['routine'])} on routine "
           f"replenishment and {_n(f['split']['directed'])} carrying an approved directed shipment."]
    for r, st, u, v in f["not_shipped"]:
        if st or u:
            why.append(f"{r}: {_n(st)} stores, {_n(u)} units.")
    return {"headline": (f"Expected weekly sales are {_usd(ol.get('expected'))} against a goal of "
                         f"{_usd(ol.get('goal'))}." if ol else ""),
            "why": why[:6], "exceptions": [], "watch": [], "computed": True}


# ── the document ─────────────────────────────────────────────────────────────
CSS = """
*{box-sizing:border-box}body{margin:0;background:#eef2ef;color:#1c2a24;font:15px/1.5 'Segoe UI',Arial,sans-serif}
.page{max-width:940px;margin:24px auto;background:#fff;border:1px solid #dbe4de;border-radius:10px;padding:34px 40px}
h1{font-size:25px;margin:0 0 4px;color:#173326}h2{font-size:17px;margin:34px 0 10px;color:#173326;
border-bottom:2px solid #e3ebe6;padding-bottom:6px}.sub{color:#5a6a62;font-size:13px;margin:0}
.draft{display:inline-block;background:#fff4d6;color:#7a5600;border:1px solid #ecd58f;border-radius:4px;
padding:1px 8px;font-size:12px;font-weight:600}.final{background:#e3f3e8;color:#1d6a3a;border-color:#b5dcc1}
.tiles{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.tile{flex:1 1 150px;background:#f4f8f5;
border:1px solid #dfe8e2;border-radius:8px;padding:12px 14px}.tile b{display:block;font-size:26px;color:#173326;
line-height:1.15}.tile span{display:block;font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:#52605a;
margin-top:2px}.tile small{display:block;font-size:12px;color:#6b7a73;margin-top:3px}
.tile.key{background:#173326;border-color:#173326}.tile.key b,.tile.key span,.tile.key small{color:#fff}
.bar{position:relative;height:26px;background:#e9efeb;border-radius:5px;margin:16px 0 4px;overflow:hidden}
.bar i{position:absolute;top:0;bottom:0;display:block}.bar .b1{background:#2f6b4c}.bar .b2{background:#7fb894}
.legend{font-size:12px;color:#5a6a62}.legend span{display:inline-block;margin-right:16px}
.dot{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
table{border-collapse:collapse;width:100%;font-size:14px}th{text-align:left;font-size:12px;color:#52605a;
padding:7px 10px;border-bottom:2px solid #cfd8d3}td{padding:8px 10px;border-bottom:1px solid #e6ece8;vertical-align:top}
td.r,th.r{text-align:right;white-space:nowrap}.muted{color:#6b7a73;font-size:13px}
.pill{display:inline-block;border-radius:10px;padding:1px 10px;font-size:12px;font-weight:600;white-space:nowrap}
.met{background:#e3f3e8;color:#1d6a3a}.partly{background:#fff4d6;color:#7a5600}.not_met{background:#fde5e2;color:#9b2c1f}
.info{background:#e8eef5;color:#33506e}.lead{font-size:17px;font-weight:600;color:#173326;margin:0 0 10px}
ul{margin:6px 0 0;padding-left:20px}li{margin:0 0 7px}h3{font-size:14px;margin:18px 0 2px;color:#2f4a3c}
.meter{height:8px;background:#e9efeb;border-radius:4px;overflow:hidden;margin-top:5px;min-width:90px}
.meter i{display:block;height:100%;background:#2f6b4c}.foot{font-size:12px;color:#8b9892;margin-top:34px;
border-top:1px solid #e3ebe6;padding-top:12px}.note{background:#f7f9f8;border-left:3px solid #c9d6ce;padding:8px 12px;
font-size:13px;color:#4c5b54;margin-top:10px}
@media print{body{background:#fff}.page{border:0;margin:0;padding:0;max-width:none}h2{break-after:avoid}
tr,.tile,li{break-inside:avoid}}
@media(max-width:640px){.page{padding:20px 16px;margin:0;border-radius:0}}
"""
PILL = {"met": "Met", "partly": "Partly met", "not_met": "Not met", "info": "For information"}


def _tile(label, value, foot="", key=False):
    return (f'<div class="tile{" key" if key else ""}"><b>{E(str(value))}</b><span>{E(label)}</span>'
            + (f"<small>{E(foot)}</small>" if foot else "") + "</div>")


def _ul(items):
    return "<ul>" + "".join(f"<li>{E(str(x))}</li>" for x in items) + "</ul>" if items else ""


def render(f: dict, ai: dict | None, generated_at=None) -> str:
    h, ol, wk = f["headline"], f["outlook"] or {}, _week(f["week"])
    final = f["run"]["status"] in ("exported", "released", "shipped")
    ai = ai or computed_overview(f)
    p = [f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
         f'<meta name="viewport" content="width=device-width,initial-scale=1">'
         f'<title>Home Depot orders, week of {E(wk)}</title><style>{CSS}</style></head><body><div class="page">'
         f'<h1>Home Depot orders: ship week of {E(wk)}</h1>'
         f'<p class="sub"><span class="draft{" final" if final else ""}">'
         f'{"Released" if final else "Draft, not yet released"}</span> &nbsp;Sales data through '
         f'{E(str(f["data"]["sales_through"]))} · dollars at wholesale'
         + (f' · prepared {E(_week(generated_at))}' if generated_at else '') + '</p>']

    # 1. the order
    t = f["trucks"]
    p.append('<h2>This week\'s order</h2><div class="tiles">'
             + _tile("Stores", _n(h["stores"]), f"{_n(f['split']['routine'])} routine, "
                                                f"{_n(f['split']['directed'])} with a directed shipment")
             + _tile("Carts", _n(h["carts"]), f"{_usd(h['per_cart'])} per cart")
             + _tile("Trucks", ("" if t["routed"] else "about ") + _n(t["count"]), t["note"])
             + _tile("Units", _n(h["units"]))
             + _tile("Order value", _usd(h["value"]), f"freight {_pct(h['freight_pct'], 1)} of value")
             + "</div>")

    # 2. the forecast
    if ol:
        goal, base, added = _f(ol.get("goal")), _f(ol.get("baseline")), _f(ol.get("added"))
        scale = max(goal, base + added, 1) * 1.02
        gap = _f(ol.get("gap"))
        bf = (_f(ol.get("baseline_factor"), 1) - 1) * 100
        p.append('<h2>Sales forecast</h2><div class="tiles">'
                 + _tile("Baseline next week", _usd(base),
                         f"no new product; last week sold {_usd(ol.get('last_week_actual'))} ({bf:+.1f}%)")
                 + _tile("Added by this order", "+" + _usd(added),
                         f"{_usd(ol.get('plan_wholesale'))} shipped, {_pct(ol.get('sell_through_pct'))} sold over "
                         f"{_f(ol.get('sell_through_weeks')):g} weeks")
                 + _tile("Expected weekly sales", _usd(ol.get("expected")), "baseline plus this order", key=True)
                 + _tile("Short of goal" if gap > 0 else "Above goal", _usd(abs(gap)), f"goal {_usd(goal)} a week")
                 + _tile("Demand forecast", _usd(ol.get("demand_forecast")), "if every store need were filled")
                 + "</div>"
                 + f'<div class="bar"><i class="b1" style="left:0;width:{base / scale * 100:.1f}%"></i>'
                   f'<i class="b2" style="left:{base / scale * 100:.1f}%;width:{added / scale * 100:.1f}%"></i>'
                   f'<i style="left:{goal / scale * 100:.1f}%;width:2px;background:#9b2c1f"></i></div>'
                   '<p class="legend"><span><i class="dot" style="background:#2f6b4c"></i>Baseline</span>'
                   '<span><i class="dot" style="background:#7fb894"></i>Added by this order</span>'
                   '<span><i class="dot" style="background:#9b2c1f"></i>Weekly goal</span></p>'
                 + f'<p class="muted">The baseline is a forecast from the {E(str(ol.get("baseline_method") or ""))}. '
                   f'An order covers several weeks of selling, so it adds less to one week than its full value.</p>')

    # 3. why
    p.append("<h2>Why the order looks this way</h2>"
             + (f'<p class="lead">{E(ai.get("headline") or "")}</p>' if ai.get("headline") else "")
             + _ul(ai.get("why")))
    rows = [(r, st, u, v) for r, st, u, v in f["not_shipped"] if st or u or v]
    if rows:
        p.append('<h3>What held product back</h3><table><tr><th>Reason</th><th class="r">Stores</th>'
                 '<th class="r">Units</th><th class="r">One week of sales at risk</th></tr>'
                 + "".join(f'<tr><td>{E(r)}</td><td class="r">{_n(st)}</td><td class="r">{_n(u)}</td>'
                           f'<td class="r">{_usd(v)}</td></tr>' for r, st, u, v in rows) + "</table>")
    if ai.get("computed"):
        p.append('<p class="note">The written overview could not be generated this time, so this section '
                 'shows the computed figures only.</p>')

    # 4. exceptions
    p.append("<h2>Exceptions used this week</h2>")
    if f["exceptions"]:
        body = ""
        for x in f["exceptions"]:
            fol = x.get("followed")
            asked, served = x.get("stores_asked"), x.get("stores_served")
            how = (f"{_n(served)} of {_n(asked)} stores" if served is not None and asked
                   else (f"{_n(x.get('trays_granted'))} of {_n(x.get('trays_requested'))} trays"
                         if x.get("trays_requested") else "applied"))
            meter = (f'<div class="meter"><i style="width:{min(100, fol * 100):.0f}%"></i></div>' if fol is not None else "")
            notes = "".join(f'<div class="muted">{E(n)}</div>' for n in x["notes"])
            body += (f'<tr><td><b>{E(x["label"] or "Exception")}</b>'
                     + (f'<div class="muted">Request: {E(x["request"])}{"…" if len(x["request"]) >= 220 else ""}</div>'
                        if x.get("request") else "") + notes + "</td>"
                     f'<td>{E(x["requested_by"] or "—")}</td><td>{E(x["approved_by"] or "not approved")}</td>'
                     f'<td><b>{_pct(fol) if fol is not None else "—"}</b> · {E(how)}'
                     + (f'<div class="muted">{_n(x["units_shipped"])} units shipping</div>'
                        if x.get("units_shipped") is not None else "") + meter + "</td></tr>")
        p.append('<table><tr><th>Exception</th><th>Asked by</th><th>Approved by</th><th>Carried out</th></tr>'
                 + body + "</table>")
    else:
        p.append('<p class="muted">No exceptions were applied to this order.</p>')
    p.append(_ul(ai.get("exceptions")))
    extra = []
    if f["overrides"]:
        extra.append(f"{_n(f['overrides'])} store rule(s) were set aside for an approved directed shipment.")
    if f["rejected"]:
        extra.append(f"{len(f['rejected'])} request(s) were rejected and are not in the order.")
    if f["pending"]:
        extra.append(f"{_n(f['pending'])} request(s) are still waiting for approval and are not in the order.")
    extra.append(f"{_n(f['manual_edits'])} manual change(s) were made to the orders after they were built.")
    p.append(f'<p class="note">{E(" ".join(extra))}</p>')

    # 5. leadership rules
    def _rules(rows):
        return ('<table><tr><th>Requirement</th><th>Result</th><th>Status</th></tr>'
                + "".join(f'<tr><td><b>{E(c["rule"])}</b></td><td>{E(c["result"])}'
                          + (f'<div class="muted">{E(c["detail"])}</div>' if c["detail"] else "")
                          + f'</td><td><span class="pill {c["status"]}">{PILL[c["status"]]}</span></td></tr>'
                          for c in rows) + "</table>")

    prim = [c for c in f["checks"] if c.get("tier") == "primary"]
    supp = [c for c in f["checks"] if c.get("tier") != "primary"]
    p.append('<h2>Does the order meet the business requirements?</h2>'
             '<p class="muted">Revenue, sell-through and margin are the controlling requirements. The order '
             'is judged on these first.</p>' + _rules(prim))
    pm = sum(1 for c in prim if c["status"] == "met")
    ps = sum(1 for c in prim if c["status"] != "info")
    p.append(f'<p class="lead" style="margin-top:10px">{pm} of {ps} controlling requirements met.</p>')
    p.append('<h3>Supporting rules: how the order was built</h3>'
             '<p class="muted">These support the requirements above. Meeting them does not make up for '
             'missing a controlling requirement.</p>' + _rules(supp))
    met = sum(1 for c in supp if c["status"] == "met")
    scored = sum(1 for c in supp if c["status"] != "info")
    p.append(f'<p class="muted">{met} of {scored} supporting rules met. Results are computed from the order '
             f'itself, not written by the AI.</p>')

    # 6. watch + breakdown
    if ai.get("watch"):
        p.append("<h2>What to watch</h2>" + _ul(ai["watch"]))
    if f["divisions"]:
        p.append('<h2>Where it is going</h2><table><tr><th>Division</th><th class="r">Stores</th>'
                 '<th class="r">Carts</th><th class="r">Units</th></tr>'
                 + "".join(f'<tr><td>{E(str(d["division"]))}</td><td class="r">{_n(d["stores"])}</td>'
                           f'<td class="r">{_n(d["carts"])}</td><td class="r">{_n(d["units"])}</td></tr>'
                           for d in f["divisions"]) + "</table>")
    if f["families"]:
        p.append('<h2>What is going</h2><table><tr><th>Product family</th><th class="r">Stores</th>'
                 '<th class="r">Units</th><th class="r">Value at wholesale</th></tr>'
                 + "".join(f'<tr><td>{E(str(d["family"]))}</td><td class="r">{_n(d["stores"])}</td>'
                           f'<td class="r">{_n(d["units"])}</td><td class="r">{_usd(d["wholesale"])}</td></tr>'
                           for d in f["families"][:12]) + "</table>")
    p.append(f'<p class="foot">TPC IPR Engine · order {E(f["run"]["id"][:8])} · figures come from the saved '
             f'order and its decision record; the written overview '
             f'{"was computed without the AI" if ai.get("computed") else "is AI-written from those figures"}.</p>'
             '</div></body></html>')
    return "".join(p)
