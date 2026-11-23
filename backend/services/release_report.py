"""Release report — the email the team gets once a week's orders are final.

build(cur, run_id) gathers everything from what the run already saved (ipr_summary,
run_decisions, orders) plus the changes people made since the previous run (policy edits,
store status changes, store master loads, approved scenarios). render() turns it into a
subject, an HTML body and a plain-text body. Nothing here sends: routers/ipr.py previews it
and sends it to the person who asks; automatic sending on release is switched on later.

Dollars follow the run's own basis (summary.objective.dollar_basis) — never mixed.
"""
from __future__ import annotations

from html import escape as E

BASIS_LABEL = {"wholesale": "units x SKU wholesale price", "sales": "sales-feed dollars"}


def _n(v):
    return "—" if v is None else f"{float(v):,.0f}"


def _usd(v):
    return "—" if v is None else f"${float(v):,.0f}"


def _pct(v):
    return "—" if v is None else f"{float(v) * 100:.0f}%"


def build(cur, run_id: str) -> dict | None:
    cur.execute("""SELECT id, shipping_week, status, pipeline_status, run_at, ipr_summary, batch_id
                   FROM recommendation_runs WHERE id = %s""", (run_id,))
    run = cur.fetchone()
    if not run:
        return None
    s = run["ipr_summary"] or {}
    o, ce, a, c = (s.get("objective") or {}, s.get("cart_economics") or {},
                   s.get("allocation") or {}, s.get("counts") or {})
    cur.execute("""SELECT COALESCE(fm.ipr_family, 'Unmapped') AS family, COUNT(DISTINCT ol.store_id) AS stores,
                          SUM(COALESCE(ol.approved_qty, ol.draft_qty, ol.recommended_qty)) AS units,
                          SUM(COALESCE(ol.approved_qty, ol.draft_qty, ol.recommended_qty)
                              * COALESCE(k.wholesale_price_whl, 0)) AS wholesale
                   FROM orders od JOIN order_lines ol ON ol.order_id = od.id
                   JOIN skus k ON k.sku_id = ol.sku_id
                   LEFT JOIN ipr_family_map fm ON fm.sku_id = ol.sku_id
                   WHERE od.run_id = %s GROUP BY 1 ORDER BY 4 DESC NULLS LAST""", (run_id,))
    families = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT COALESCE(s.division, 'No division') AS division, COUNT(*) AS stores,
                          SUM(od.cart_count) AS carts, SUM(od.total_units) AS units
                   FROM orders od JOIN stores s ON s.store_id = od.store_id
                   WHERE od.run_id = %s GROUP BY 1 ORDER BY 1""", (run_id,))
    divisions = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT decision_type, COUNT(*) AS n, COUNT(DISTINCT store_id) AS stores
                   FROM run_decisions WHERE run_id = %s GROUP BY 1""", (run_id,))
    dec = {r["decision_type"]: dict(r) for r in cur.fetchall()}
    cur.execute("""SELECT d.store_id, s.store_name, s.state, d.reason FROM run_decisions d
                   LEFT JOIN stores s ON s.store_id = d.store_id
                   WHERE d.run_id = %s AND d.decision_type = 'directed_shipment' ORDER BY d.store_id""",
                (run_id,))
    directed = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT reason FROM run_decisions WHERE run_id = %s AND decision_type = 'validation'
                   AND store_id IS NULL AND sku_id IS NULL ORDER BY created_at LIMIT 12""", (run_id,))
    flags = [r["reason"] for r in cur.fetchall()]

    # what people changed since the previous run (or in the last 7 days when there is none)
    cur.execute("""SELECT MAX(run_at) AS t FROM recommendation_runs
                   WHERE run_at < %s AND COALESCE(status, '') NOT IN ('cancelled', 'canceled', 'failed')""",
                (run["run_at"],))
    since = (cur.fetchone() or {}).get("t")
    cur.execute("""SELECT entity_id AS key, action, user_email, created_at FROM change_log
                   WHERE entity = 'ipr_setting' AND created_at >= COALESCE(%s, now() - interval '7 days')
                   ORDER BY created_at""", (since,))
    import json
    policy = []
    for r in cur.fetchall():
        try:
            act = json.loads(r["action"])
        except Exception:
            act = {}
        policy.append({"key": r["key"], "by": r["user_email"], "at": r["created_at"],
                       "old": act.get("old"), "new": act.get("new"), "reason": act.get("reason")})
    cur.execute("""SELECT store_id, store_name, state, store_status, status_note, status_updated_by,
                          status_updated_at FROM stores
                   WHERE status_updated_at >= COALESCE(%s, now() - interval '7 days')
                   ORDER BY status_updated_at""", (since,))
    status_changes = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT COUNT(*) AS n FROM change_log
                   WHERE run_id = %s AND entity <> 'ipr_setting'""", (run_id,))
    manual_edits = (cur.fetchone() or {}).get("n") or 0
    cur.execute("""SELECT user_email, COUNT(*) AS n FROM change_log
                   WHERE run_id = %s AND entity <> 'ipr_setting' GROUP BY 1 ORDER BY 2 DESC""", (run_id,))
    edits_by = [dict(r) for r in cur.fetchall()]
    # last full week of actual sales, both ways it gets quoted, so nobody compares across bases
    actual = None
    if s.get("sales_anchor"):
        cur.execute("""SELECT SUM(sa.units_sold) AS units, SUM(sa.dollars) AS feed,
                              SUM(sa.units_sold * COALESCE(k.wholesale_price_whl, 0)) AS wholesale
                       FROM sales sa JOIN skus k ON k.sku_id = sa.sku_id
                       WHERE sa.sale_date > %s::date - 7 AND sa.sale_date <= %s::date""",
                    (s["sales_anchor"], s["sales_anchor"]))
        actual = dict(cur.fetchone() or {})
    from services import sales_outlook
    outlook = sales_outlook.compute(cur, s)
    return {
        "outlook": outlook,
        "actual_last_week": actual,
        "run": {"id": str(run["id"]), "shipping_week": run["shipping_week"], "status": run["status"],
                "pipeline_status": run["pipeline_status"], "run_at": run["run_at"]},
        "basis": (o.get("dollar_basis") or "wholesale"),
        "headline": {"stores": c.get("stores_shipped"), "carts": ce.get("carts"),
                     "units": a.get("allocated_units"), "value": ce.get("wholesale_value"),
                     "per_cart": ce.get("avg_wholesale_per_cart"), "freight_pct": ce.get("freight_pct"),
                     "cart_fill": ce.get("avg_tray_fill_shipping")},
        "sales": {"goal": o.get("minimum_weekly_retail"), "demand": o.get("unconstrained_weekly_demand_retail"),
                  "baseline": o.get("baseline_weekly_retail"), "added": o.get("incremental_weekly_retail"),
                  "expected": o.get("expected_weekly_retail"), "gap": o.get("gap_to_minimum")},
        "not_shipped": [
            ("Pushed under the full-cart minimum", (ce.get("pushed_min_cart") or {}).get("stores"),
             (ce.get("pushed_min_cart") or {}).get("units"), o.get("lost_minimum_cart_pushes_one_week")),
            ("Stopped at the 13-week sell-through review", (dec.get("stopped_13wk") or {}).get("stores"),
             a.get("stopped_13wk_units"), None),
            ("Reduced at the 13-week review", (dec.get("reduced_13wk") or {}).get("stores"),
             a.get("reduced_13wk_units_removed"), None),
            ("No inventory available", None, a.get("unfilled_demand_units"), o.get("lost_unavailable_inventory")),
            ("Do not ship and store-master rules", None, a.get("blocked_units"), o.get("lost_blocks_and_holds")),
            ("Not due a delivery yet", (s.get("delivery_cadence") or {}).get("stores_not_due"), None, None),
        ],
        "families": families, "divisions": divisions,
        "production": s.get("production"),
        "store_rules": s.get("store_rules") or {},
        "grades": s.get("availability_grades"),
        "scenarios": (s.get("scenarios") or {}).get("by_directive") or [], "directed": directed,
        "policy_changes": policy, "status_changes": status_changes,
        "manual_edits": manual_edits, "edits_by": edits_by, "since": since,
        "data": {"sales_through": s.get("sales_anchor"), "arrival": s.get("arrival"),
                 "arrival_basis": (s.get("arrival_basis") or {}).get("mode"),
                 "sales_age_days": (s.get("data_quality") or {}).get("sales_age_days"),
                 "edi_gap_stores": c.get("stores_edi_gap"), "review_warnings": c.get("review_warning"),
                 "flags": flags},
    }


# ── rendering ────────────────────────────────────────────────────────────────
TH = 'style="text-align:left;padding:6px 10px;border-bottom:2px solid #cfd8d3;font-size:12px;color:#52605a"'
TD = 'style="padding:6px 10px;border-bottom:1px solid #e6ece8;font-size:13px"'
TDR = 'style="padding:6px 10px;border-bottom:1px solid #e6ece8;font-size:13px;text-align:right"'


def _table(headers, rows, right=()):
    if not rows:
        return '<p style="color:#6b7a73;font-size:13px;margin:4px 0 0">None.</p>'
    h = "".join(f"<th {TH}{' align=right' if i in right else ''}>{E(str(x))}</th>" for i, x in enumerate(headers))
    b = "".join("<tr>" + "".join(f"<td {TDR if i in right else TD}>{E(str(x))}</td>" for i, x in enumerate(r))
                + "</tr>" for r in rows)
    return f'<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%">{h and "<tr>" + h + "</tr>"}{b}</table>'


def _section(title, inner, note=""):
    return (f'<h2 style="font-size:15px;margin:22px 0 6px;color:#1f3a2e">{E(title)}</h2>'
            + (f'<p style="color:#6b7a73;font-size:12px;margin:0 0 6px">{E(note)}</p>' if note else "") + inner)


def _tile(label, value, foot=""):
    return (f'<td style="padding:10px 14px;background:#f3f7f4;border:1px solid #dfe8e2;border-radius:6px">'
            f'<div style="font-size:20px;font-weight:700;color:#1f3a2e">{E(value)}</div>'
            f'<div style="font-size:11px;text-transform:uppercase;color:#52605a">{E(label)}</div>'
            + (f'<div style="font-size:11px;color:#6b7a73">{E(foot)}</div>' if foot else "") + "</td>")


def render(d: dict, test: bool = True) -> dict:
    wk = d["run"]["shipping_week"]
    wk_s = wk.strftime("%b %d, %Y").replace(" 0", " ") if hasattr(wk, "strftime") else str(wk)
    h, sa, basis = d["headline"], d["sales"], BASIS_LABEL.get(d["basis"], d["basis"])
    final = d["run"]["status"] in ("exported", "released", "shipped")
    state = "released" if final else "DRAFT — not released"
    subject = (("[TEST] " if test else "") + f"Home Depot orders {'released' if final else 'draft'}: "
               f"ship week of {wk_s} — {_n(h['stores'])} stores, {_n(h['carts'])} carts, {_usd(h['value'])}")
    parts = [f'<div style="font-family:Segoe UI,Arial,sans-serif;color:#1c2a24;max-width:760px">'
             f'<h1 style="font-size:19px;margin:0 0 2px">Home Depot orders — ship week of {E(wk_s)}</h1>'
             f'<p style="margin:0 0 12px;font-size:13px;color:#52605a">Status: <b>{E(state)}</b> · '
             f'sales through {E(str(d["data"]["sales_through"]))} · dollars are {E(basis)}'
             + (' · <b>TEST — not for distribution</b>' if test else '') + '</p>']
    parts.append('<table cellspacing="8" cellpadding="0"><tr>'
                 + _tile("Stores shipping", _n(h["stores"]))
                 + _tile("Carts", _n(h["carts"]), f"{_pct(h['cart_fill'])} full on average")
                 + _tile("Units", _n(h["units"]))
                 + _tile("Order value", _usd(h["value"]), f"{_usd(h['per_cart'])} per cart")
                 + _tile("Freight", _pct(h["freight_pct"]), "of order value") + "</tr></table>")
    ol = d.get("outlook")
    if ol:
        sa = {"demand": ol["demand_forecast"], "baseline": ol["baseline"], "added": ol["added"],
              "expected": ol["expected"], "goal": ol["goal"], "gap": ol["gap"]}
        _b = f"Baseline, no new inventory (actual sales, week ending {ol['baseline_week_ending']})"
        _a = (f"Added by this plan ({_usd(ol['plan_wholesale'])} shipped x {ol['sell_through_pct'] * 100:.0f}% "
              f"sold over {ol['sell_through_weeks']:g} weeks)")
    else:
        _b, _a = "Sold from stock already in stores or on the way", "Added by this week's orders"
    parts.append(_section("Expected weekly sales", _table(
        ["", "Dollars"],
        [("Demand forecast (what stores could sell with every need filled)", _usd(sa["demand"])),
         (_b, _usd(sa["baseline"])),
         (_a, _usd(sa["added"])),
         ("Expected weekly sales with these orders", _usd(sa["expected"])),
         ("Weekly goal", _usd(sa["goal"])),
         ("Gap to goal", _usd(sa["gap"]))], right=(1,)),
        "An order covers several weeks of selling, so order value is larger than the sales it adds in one week."
        + ((f" Last week's actual sales: {_usd(d['actual_last_week'].get('feed'))} in the sales report's own "
            f"dollars, {_usd(d['actual_last_week'].get('wholesale'))} at SKU wholesale price "
            f"({_n(d['actual_last_week'].get('units'))} units). Figures in this email use the wholesale-price basis.")
           if d.get("actual_last_week") and d["actual_last_week"].get("units") else "")))
    parts.append(_section("What did not ship, and why", _table(
        ["Reason", "Stores", "Units", "One week of sales at risk"],
        [(r, _n(st), _n(u), _usd(v)) for r, st, u, v in d["not_shipped"] if st or u or v], right=(1, 2, 3))))
    parts.append(_section("Orders by product family", _table(
        ["Family", "Stores", "Units", "Value at wholesale"],
        [(f["family"], _n(f["stores"]), _n(f["units"]), _usd(f["wholesale"])) for f in d["families"]], right=(1, 2, 3))))
    parts.append(_section("Orders by division", _table(
        ["Division", "Stores", "Carts", "Units"],
        [(x["division"], _n(x["stores"]), _n(x["carts"]), _n(x["units"])) for x in d["divisions"]], right=(1, 2, 3))))
    p = d.get("production")
    if p:
        rows = [(f, _n(v["weekly_demand_units"]), _n(v["need_units"]), _n(v["available_units"]),
                 _n(v["shipped_units"]), _n(v["short_units"]), _n(v["surplus_units"]))
                for f, v in sorted(p["by_family"].items(), key=lambda kv: -kv[1]["need_units"])]
        t = p["totals"]
        rows.append(("TOTAL", _n(t["weekly_demand_units"]), _n(t["need_units"]), _n(t["available_units"]),
                     _n(t["shipped_units"]), _n(t["short_units"]), _n(t["surplus_units"])))
        parts.append(_section("Ideal production", _table(
            ["Family", "Stores sell / week", "Needed now", "Greenhouse has", "Shipping", "Short", "Left over"],
            rows, right=(1, 2, 3, 4, 5, 6)),
            "Stores sell / week = steady weekly production that would keep every store supplied. Needed now = "
            "units to fill every store's need this week before any gate. Short = needed more than the "
            "greenhouse has. Left over = greenhouse has more than stores need."))
    else:
        parts.append(_section("Ideal production", '<p style="font-size:13px;color:#6b7a73">Not available for '
                                                  'this run (it was made before production figures were saved).</p>'))
    g = d.get("grades")
    if g and g.get("by_grade"):
        parts.append(_section("Availability by grade", _table(
            ["Grade", "Greenhouse has", "Shipping", "Left after this week"],
            [(f"Grade {k}" + (" (picked first)" if str(g.get("picked_first")) == k else ""),
              _n(v["available_units"]), _n(v["shipping_units"]), _n(v["left_units"]))
             for k, v in g["by_grade"].items()], right=(1, 2, 3)),
            "Grade 3 is more mature plants and contract-grower stock. It is picked before grade 2 "
            "within each product."))
    sr = d["store_rules"]
    if sr:
        parts.append(_section("Store handling rules applied", _table(
            ["Rule", "Count"],
            [("Do-not-ship stores", _n(sr.get("stores_do_not_ship"))),
             ("Stores with no routine order (store master)", _n(sr.get("stores_no_routine_order"))),
             ("Stores with products left off (store master)", _n(sr.get("stores_products_limited"))),
             ("Units kept off orders by store rules", _n(sr.get("units_not_shipped_by_rule"))),
             ("Carts built to a variety restriction", _n(sr.get("carts_with_handling_tag"))),
             ("Cross-dock stores shipping", ", ".join(f"{k} {v}" for k, v in (sr.get("cross_dock_stores_shipping") or {}).items()) or "0")],
            right=(1,))))
    parts.append(_section("Scenarios live this week", _table(
        ["Scenario", "What it does", "Stores", "Asked by", "Approved by"],
        [(x.get("label"), str(x.get("type", "")).replace("_", " "), _n(x.get("stores")),
          x.get("requested_by") or "—", x.get("approved_by") or "—") for x in d["scenarios"]], right=(2,))))
    if d["directed"]:
        parts.append(_section("Directed shipments", _table(
            ["Store", "What was sent"],
            [(f"{x['store_id']} {x.get('store_name') or ''} {x.get('state') or ''}".strip(), x["reason"][:160])
             for x in d["directed"][:40]])))
    ch = [(str(x["at"])[:16], x["by"] or "—", f"Policy: {x['key'].replace('_', ' ')}",
           f"{x['old']} → {x['new']}", x["reason"] or "") for x in d["policy_changes"]]
    ch += [(str(x["status_updated_at"])[:16], x["status_updated_by"] or "—",
            f"Store {x['store_id']} {x.get('store_name') or ''}".strip(),
            str(x["store_status"]).replace("_", " "), x.get("status_note") or "")
           for x in d["status_changes"][:60]]
    parts.append(_section("Approvals and changes since the last run", _table(
        ["When", "Who", "What", "Change", "Why"], sorted(ch)),
        f"{_n(d['manual_edits'])} manual order edit(s) on this run"
        + (": " + ", ".join(f"{e['user_email'] or 'unknown'} {e['n']}" for e in d["edits_by"]) if d["edits_by"] else "")
        + (f"; {len(d['status_changes']) - 60} more store changes not listed" if len(d["status_changes"]) > 60 else "")))
    dt = d["data"]
    parts.append(_section("Data health", _table(
        ["Check", "Value"],
        [("Sales data through", f"{dt['sales_through']} ({_n(dt['sales_age_days'])} days old at run time)"),
         ("Expected arrival used", f"{dt['arrival']} — {dt['arrival_basis'] or 'flat assumption'}"),
         ("Stores with a suspected reporting gap", _n(dt["edi_gap_stores"])),
         ("Orders the reviewer flagged (they still ship)", _n(dt["review_warnings"]))])
        + ("".join(f'<p style="font-size:12px;color:#8a5a00;margin:4px 0 0">• {E(f)}</p>' for f in dt["flags"]))))
    parts.append(f'<p style="font-size:11px;color:#8b9892;margin-top:22px">TPC IPR Engine · run {E(d["run"]["id"][:8])} · '
                 f'generated from the run\'s saved evidence</p></div>')
    html = "".join(parts)
    text = "\n".join([
        f"Home Depot orders - ship week of {wk_s} ({state})",
        f"Stores {_n(h['stores'])} | carts {_n(h['carts'])} | units {_n(h['units'])} | value {_usd(h['value'])} "
        f"| {_usd(h['per_cart'])} per cart | freight {_pct(h['freight_pct'])}",
        f"Expected weekly sales {_usd(sa['expected'])} of a {_usd(sa['goal'])} goal (gap {_usd(sa['gap'])}); "
        f"demand {_usd(sa['demand'])}. Dollars are {basis}.",
        "Not shipped: " + "; ".join(f"{r}: {_n(st)} stores, {_n(u)} units" for r, st, u, _v in d["not_shipped"] if st or u),
        f"Scenarios live: {len(d['scenarios'])} | policy changes: {len(d['policy_changes'])} | "
        f"store status changes: {len(d['status_changes'])} | manual order edits: {_n(d['manual_edits'])}",
        "Open the HTML version of this message for the full report.",
    ])
    return {"subject": subject, "html": html, "text": text}
