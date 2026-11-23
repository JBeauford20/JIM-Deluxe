"""Production review — the weekly HTML for the growing team, the companion to the leadership report.

Josh 2026-11-23. It answers three questions:
  1. If we could fill store demand perfectly, what would availability look like?
  2. What do stores call for that we do not have?
  3. How can production change to fit what stores need?

Figures come from the order's saved production block (ipr_summary.production: what stores sell
per week, what they need now before any rule, what the greenhouse has, what ships). Orders built
from 2026-11-23 on carry that SKU by SKU. For an order built before then the SKU split is worked
out here from the last four weeks of sales, scaled so every family still adds up to the saved
family figures, and the document says so.

  need now      units to refill every store this week, BEFORE the sell-through review and store
                rules take anything off. For a SKU that is not on the availability sheet the
                order never sized a need, so it is estimated from that SKU's weekly sales and
                its family's need-to-sales ratio, and marked as an estimate.
  short         need now more than the greenhouse has
  left over     greenhouse has more than stores need now

The AI writes only the "how production can change" paragraphs, from these figures.
"""
from __future__ import annotations

import hashlib
import json
from html import escape as E

from services.leadership_report import CSS, _f, _n, _pct, _tile, _ul, _usd, _week

TOP = 25
SEASONAL_FAMILIES = ("Seasonal/Holiday", "Holiday")
EXTRA_CSS = """
.mix{display:flex;align-items:center;gap:6px;min-width:150px}.mix i{display:block;height:9px;border-radius:2px}
.mix .need{background:#2f6b4c}.mix .have{background:#b9c7bf}.est{color:#8a5a00;font-size:11px;font-weight:600}
.two{display:flex;gap:26px;flex-wrap:wrap}.two>div{flex:1 1 380px;min-width:0}
"""


def _sku_rows(cur, run_id, s, batch_id):
    """One row per SKU: weekly demand, stores, need now, available, shipped. Returns (rows, source)."""
    prod = s.get("production") or {}
    fam = prod.get("by_family") or {}
    saved = prod.get("by_sku")
    rows = []
    if saved:
        for sku, v in saved.items():
            rows.append({"sku_id": str(sku), "description": v.get("description") or f"SKU {sku}",
                         "family": v.get("family") or "Unmapped", "weekly": _f(v.get("weekly_demand_units")),
                         "weekly_usd": _f(v.get("weekly_demand_usd")), "stores": int(v.get("stores") or 0),
                         "need": _f(v.get("need_units")), "available": _f(v.get("available_units")),
                         "shipped": _f(v.get("shipped_units")), "on_avail": bool(v.get("on_availability")),
                         "sized": bool(v.get("on_availability") and v.get("plannable"))})
        source = "saved with the order"
    else:
        anchor = s.get("sales_anchor")
        cur.execute("""SELECT sa.sku_id, SUM(sa.units_sold) / 4.0 AS weekly, COUNT(DISTINCT sa.store_id) AS stores,
                              SUM(COALESCE(sa.dollars, sa.units_sold * COALESCE(k.wholesale_price_whl, 0))) / 4.0 AS usd
                       FROM sales sa JOIN skus k ON k.sku_id = sa.sku_id
                       WHERE sa.sale_date > %s::date - 28 AND sa.sale_date <= %s::date
                       GROUP BY 1 HAVING SUM(sa.units_sold) > 0""", (anchor, anchor))
        sold = {r["sku_id"]: r for r in cur.fetchall()}
        cur.execute("""SELECT sku_id, SUM(units_available) AS units FROM availability_lines
                       WHERE batch_id = %s GROUP BY 1""", (batch_id,))
        avail = {r["sku_id"]: _f(r["units"]) for r in cur.fetchall()}
        cur.execute("""SELECT ol.sku_id, SUM(COALESCE(ol.approved_qty, ol.draft_qty, ol.recommended_qty)) AS units
                       FROM orders od JOIN order_lines ol ON ol.order_id = od.id WHERE od.run_id = %s GROUP BY 1""",
                    (run_id,))
        shipped = {r["sku_id"]: _f(r["units"]) for r in cur.fetchall()}
        ids = list(set(sold) | set(avail))
        cur.execute("""SELECT k.sku_id, k.description, COALESCE(fm.ipr_family, 'Unmapped') AS family
                       FROM skus k LEFT JOIN ipr_family_map fm ON fm.sku_id = k.sku_id
                       WHERE k.sku_id = ANY(%s)""", (ids,))
        info = {r["sku_id"]: r for r in cur.fetchall()}
        for sku in ids:
            i, sv = info.get(sku) or {}, sold.get(sku) or {}
            rows.append({"sku_id": str(sku), "description": (i.get("description") or f"SKU {sku}").strip(),
                         "family": i.get("family") or "Unmapped", "weekly": _f(sv.get("weekly")),
                         "weekly_usd": _f(sv.get("usd")), "stores": int(sv.get("stores") or 0), "need": 0.0,
                         "available": avail.get(sku, 0.0), "shipped": shipped.get(sku, 0.0),
                         "on_avail": sku in avail, "sized": False})
        # scale each family's SKU sales to the saved family demand, and split the saved family
        # need across the SKUs that are on availability in proportion to what they sell
        for f, fv in fam.items():
            grp = [r for r in rows if r["family"] == f]
            tot = sum(r["weekly"] for r in grp)
            k = (_f(fv.get("weekly_demand_units")) / tot) if tot else 0
            ku = (_f(fv.get("weekly_demand_usd")) / sum(r["weekly_usd"] for r in grp)) if sum(
                r["weekly_usd"] for r in grp) else 0
            for r in grp:
                r["weekly"], r["weekly_usd"] = r["weekly"] * k, r["weekly_usd"] * ku
            on = [r for r in grp if r["on_avail"]]
            on_tot = sum(r["weekly"] for r in on)
            for r in on:
                r["need"] = _f(fv.get("need_units")) * (r["weekly"] / on_tot) if on_tot else 0
                r["sized"] = True
        source = ("worked out from the last four weeks of sales, because this order was built before "
                  "SKU detail was saved; family totals are the order's own")
    # a SKU the order never sized: estimate need from its weekly demand and the family's ratio
    ratio = {f: (_f(v.get("need_units")) / _f(v.get("weekly_demand_units"))) if _f(v.get("weekly_demand_units")) else 0
             for f, v in fam.items()}
    sized_w = {}
    for r in rows:
        if r["sized"]:
            sized_w[r["family"]] = sized_w.get(r["family"], 0) + r["weekly"]
    all_ratio = (sum(r["need"] for r in rows if r["sized"]) / sum(sized_w.values())) if sum(sized_w.values()) else 0
    for r in rows:
        r["estimated"] = not r["sized"]
        if not r["sized"]:
            fr = (sum(x["need"] for x in rows if x["sized"] and x["family"] == r["family"]) / sized_w[r["family"]]
                  if sized_w.get(r["family"]) else (ratio.get(r["family"]) or all_ratio))
            r["need"] = r["weekly"] * fr
        r["short"] = max(0.0, r["need"] - r["available"])
        r["surplus"] = max(0.0, r["available"] - r["need"])
        r["weeks_supply"] = (r["available"] / r["weekly"]) if r["weekly"] > 0 else None
    return rows, source


def facts(cur, run_id: str) -> dict | None:
    cur.execute("""SELECT id, shipping_week, status, ipr_summary, batch_id FROM recommendation_runs
                   WHERE id = %s""", (run_id,))
    run = cur.fetchone()
    if not run:
        return None
    s = run["ipr_summary"] or {}
    prod = s.get("production")
    if not prod:
        return {"run": {"id": str(run["id"]), "status": run["status"]}, "week": run["shipping_week"],
                "missing": True}
    all_rows, source = _sku_rows(cur, run_id, s, run["batch_id"])
    # Growers act on plants. Seasonal programs are planned by event, and Climb-itt hardware and
    # pre-built carts are bought in, so both are shown on their own and kept out of the fit.
    cur.execute("SELECT sku_id, program, is_holiday FROM skus WHERE sku_id::text = ANY(%s)",
                ([r["sku_id"] for r in all_rows],))
    meta = {str(r["sku_id"]): r for r in cur.fetchall()}
    for r in all_rows:
        m = meta.get(r["sku_id"]) or {}
        if r["family"] in SEASONAL_FAMILIES or m.get("is_holiday"):
            r["kind"] = "seasonal"
        elif r["family"] == "Prebuilt Cart" or (r["family"] == "Climb-ITT" and not (m.get("program") or "").strip()):
            r["kind"] = "hard_good"
        else:
            r["kind"] = "plant"
    rows = [r for r in all_rows if r["kind"] == "plant"]
    seasonal = sorted((r for r in all_rows if r["kind"] == "seasonal" and (r["weekly"] > 0 or r["available"] > 0)),
                      key=lambda r: -r["weekly_usd"])
    hard = sorted((r for r in all_rows if r["kind"] == "hard_good"), key=lambda r: -r["available"])
    fams = {}
    for r in rows:
        f = fams.setdefault(r["family"], {"family": r["family"], "weekly": 0.0, "weekly_usd": 0.0, "need": 0.0,
                                          "available": 0.0, "shipped": 0.0, "short": 0.0, "surplus": 0.0,
                                          "covered": 0.0, "skus_called": 0, "skus_missing": 0})
        for k in ("weekly", "weekly_usd", "need", "available", "shipped", "short", "surplus"):
            f[k] += r[k]
        f["covered"] += min(r["need"], r["available"])
        f["skus_called"] += 1 if r["weekly"] > 0 else 0
        f["skus_missing"] += 1 if (r["weekly"] > 0 and not r["on_avail"]) else 0
    tot = {k: sum(f[k] for f in fams.values()) for k in
           ("weekly", "weekly_usd", "need", "available", "shipped", "short", "surplus", "covered")}
    for f in fams.values():
        f["ideal_share"] = f["need"] / tot["need"] if tot["need"] else 0
        f["have_share"] = f["available"] / tot["available"] if tot["available"] else 0
        f["fit"] = f["covered"] / f["need"] if f["need"] else None

    def slim(r):
        return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in r.items() if k != "sized"}

    missing = sorted((r for r in rows if r["weekly"] > 0 and not r["on_avail"]), key=lambda r: -r["weekly_usd"])
    short = sorted((r for r in rows if r["on_avail"] and r["short"] > 0), key=lambda r: -r["short"])
    surplus = sorted((r for r in rows if r["surplus"] > 0), key=lambda r: -r["surplus"])
    g = s.get("availability_grades") or {}
    return {
        "run": {"id": str(run["id"]), "status": run["status"]}, "week": run["shipping_week"],
        "sales_through": s.get("sales_anchor"), "sku_source": source,
        "totals": {k: round(v) for k, v in tot.items()},
        "fit": (tot["covered"] / tot["need"]) if tot["need"] else None,
        "families": [slim(f) for f in sorted(fams.values(), key=lambda f: -f["need"])],
        "missing": False,
        "not_on_availability": {"skus": len(missing), "weekly_units": round(sum(r["weekly"] for r in missing)),
                                "weekly_usd": round(sum(r["weekly_usd"] for r in missing)),
                                "need_units": round(sum(r["need"] for r in missing)),
                                "top": [slim(r) for r in missing[:TOP]]},
        "short_on_availability": {"skus": len(short), "short_units": round(sum(r["short"] for r in short)),
                                  "top": [slim(r) for r in short[:TOP]]},
        "surplus": {"skus": len(surplus), "units": round(sum(r["surplus"] for r in surplus)),
                    "no_demand_skus": sum(1 for r in surplus if r["weekly"] <= 0),
                    "top": [slim(r) for r in surplus[:TOP]]},
        "grades": g.get("by_grade"),
        "seasonal": {"skus": len(seasonal), "weekly_units": round(sum(r["weekly"] for r in seasonal)),
                     "weekly_usd": round(sum(r["weekly_usd"] for r in seasonal)),
                     "available_units": round(sum(r["available"] for r in seasonal)),
                     "top": [slim(r) for r in seasonal[:15]]},
        "hard_goods": [slim(r) for r in hard],
    }


def fingerprint(f: dict) -> str:
    return hashlib.sha1(json.dumps(f, sort_keys=True, default=str).encode()).hexdigest()


OVERVIEW_PROMPT = """You are writing for the growing (production) team at The Plant Company, a
greenhouse that supplies Home Depot stores with houseplants. They decide what to grow and how
much. This weekly review compares what stores need with what is on this week's availability.

Use ONLY the facts in the JSON below. Every number you write must appear in the facts or be a
simple sum, difference, ratio or share of numbers that do. Do not invent causes, lead times,
crop cycles or costs. "need" is what stores need now to be fully stocked, before any ordering
rule; rows marked estimated are estimates and should be called that. The main figures cover
grown plants only. "seasonal" items are planned by event, not by this review: mention them only
as a note. "hard_goods" (Climb-itt hardware, pre-built carts) are bought in, not grown: never
recommend growing more or less of them. One week is a snapshot:
say so where a recommendation would need more than one week of evidence.
Plain English for growers: name product families and products, no system words.

Return ONLY a JSON object with these keys:
  "headline"  one sentence: how well this week's availability fits what stores need
  "ideal"     3 to 4 short bullets: what a perfect-fit availability would look like, and where
              the real one differs most
  "missing"   2 to 4 short bullets: what stores call for that is short or not available at all
  "changes"   4 to 6 short bullets: specific changes production could make, each naming the
              family or product and the size of the gap (grow more, grow less, shift mix)
  "caveats"   1 to 3 short bullets: what in these figures the growers should be careful with

Each bullet is one or two sentences with its numbers."""


def overview(f: dict) -> dict:
    from services import ipr_ai
    slim = {k: f[k] for k in ("week", "totals", "fit", "families", "grades", "sku_source", "seasonal",
                              "hard_goods")}
    slim["seasonal"] = {**f["seasonal"], "top": f["seasonal"]["top"][:6]}
    for k in ("not_on_availability", "short_on_availability", "surplus"):
        slim[k] = {**f[k], "top": f[k]["top"][:12]}
    raw = ipr_ai.call_model(OVERVIEW_PROMPT, "FACTS (JSON):\n" + json.dumps(slim, default=str), max_tokens=6000)
    try:
        out = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except Exception:
        out = {"headline": "", "changes": [raw[:1500]]}
    return {k: (str(out.get(k) or "") if k == "headline" else [str(x) for x in (out.get(k) or [])][:6])
            for k in ("headline", "ideal", "missing", "changes", "caveats")}


def _sku_table(rows, cols):
    """cols: list of (header, right-aligned?, fn(row) -> html)."""
    if not rows:
        return '<p class="muted">None this week.</p>'
    head = "".join(f'<th{" class=r" if r else ""}>{E(h)}</th>' for h, r, _ in cols)
    body = "".join("<tr>" + "".join(f'<td{" class=r" if r else ""}>{fn(x)}</td>' for _, r, fn in cols) + "</tr>"
                   for x in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def _name(r):
    return (f'<b>{E(r["description"])}</b><div class="muted">{E(r["sku_id"])} · {E(r["family"])}</div>')


def _need(r):
    return _n(r["need"]) + (' <span class="est">est.</span>' if r.get("estimated") else "")


def render(f: dict, ai: dict | None, generated_at=None) -> str:
    wk = _week(f["week"])
    head = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Production review, week of {E(wk)}</title><style>{CSS}{EXTRA_CSS}</style></head><body>'
            f'<div class="page"><h1>Production review: ship week of {E(wk)}</h1>')
    if f.get("missing"):
        return (head + '<p class="note">This order was built before production figures were saved, so there is '
                       'nothing to review. Build the orders again to get this document.</p></div></body></html>')
    ai = ai or {}
    t = f["totals"]
    p = [head + f'<p class="sub">For the growing team · what stores need against this week\'s availability · '
                f'sales data through {E(str(f["sales_through"]))}'
         + (f' · prepared {E(_week(generated_at))}' if generated_at else '') + '</p>']
    p.append('<div class="tiles">'
             + _tile("Stores sell a week", _n(t["weekly"]), f"{_usd(t['weekly_usd'])} at wholesale")
             + _tile("Needed now", _n(t["need"]), "units to fully stock every store")
             + _tile("On availability", _n(t["available"]), "units this week")
             + _tile("Fit", _pct(f["fit"]), "of the need this availability could cover", key=True)
             + _tile("Short", _n(t["short"]), "needed, not there")
             + _tile("Left over", _n(t["surplus"]), "there, not needed now") + "</div>")
    if ai.get("headline"):
        p.append(f'<p class="lead" style="margin-top:18px">{E(ai["headline"])}</p>')

    # 1. perfect availability
    mx = max([x["ideal_share"] for x in f["families"]] + [x["have_share"] for x in f["families"]] + [0.01])
    rows = ""
    for x in f["families"]:
        if not (x["need"] or x["available"] or x["weekly"]):
            continue
        diff = x["available"] - x["need"]
        rows += (f'<tr><td><b>{E(x["family"])}</b></td><td class="r">{_n(x["weekly"])}</td>'
                 f'<td class="r">{_n(x["need"])}</td><td class="r">{_n(x["available"])}</td>'
                 f'<td class="r" style="color:{"#9b2c1f" if diff < 0 else "#52605a"}">{"+" if diff > 0 else ""}{_n(diff)}</td>'
                 f'<td><div class="mix"><i class="need" style="width:{x["ideal_share"] / mx * 110:.0f}px"></i>'
                 f'{_pct(x["ideal_share"])}</div><div class="mix"><i class="have" '
                 f'style="width:{x["have_share"] / mx * 110:.0f}px"></i>{_pct(x["have_share"])}</div></td>'
                 f'<td class="r">{_pct(x["fit"]) if x["fit"] is not None else "—"}</td></tr>')
    p.append('<h2>If we filled demand perfectly, what would availability look like?</h2>'
             '<p class="muted">A perfect availability would match "Needed now" line for line. The bars compare '
             'each family\'s share of what stores need (dark) with its share of what is available (grey).</p>'
             + _ul(ai.get("ideal"))
             + '<table><tr><th>Family</th><th class="r">Stores sell a week</th><th class="r">Needed now</th>'
               '<th class="r">On availability</th><th class="r">Over / short</th><th>Share of need vs share of '
               'availability</th><th class="r">Fit</th></tr>' + rows + "</table>"
             + '<p class="muted">Fit = how much of the family\'s need could be covered by the right products '
               'on this availability. A family can show units left over and still have a low fit when the '
               'units are in the wrong products.</p>')

    # 2. what stores call for that we do not have
    na, so = f["not_on_availability"], f["short_on_availability"]
    p.append('<h2>What stores call for that we do not have</h2>' + _ul(ai.get("missing")))
    p.append(f'<h3>Not on this week\'s availability at all</h3><p class="muted">{_n(na["skus"])} products that '
             f'stores are selling, worth {_usd(na["weekly_usd"])} a week ({_n(na["weekly_units"])} units a week). '
             f'Top {min(TOP, na["skus"])} by weekly sales value.</p>'
             + _sku_table(na["top"], [
                 ("Product", False, _name), ("Stores selling it", True, lambda r: _n(r["stores"])),
                 ("Sells a week", True, lambda r: _n(r["weekly"])),
                 ("Worth a week", True, lambda r: _usd(r["weekly_usd"])),
                 ("Needed now", True, _need)]))
    p.append(f'<h3>On availability, but not enough</h3><p class="muted">{_n(so["skus"])} products are short by '
             f'{_n(so["short_units"])} units in total. Top {min(TOP, so["skus"])} by units short.</p>'
             + _sku_table(so["top"], [
                 ("Product", False, _name), ("Sells a week", True, lambda r: _n(r["weekly"])),
                 ("Needed now", True, _need), ("On availability", True, lambda r: _n(r["available"])),
                 ("Short", True, lambda r: f'<b style="color:#9b2c1f">{_n(r["short"])}</b>'),
                 ("Weeks of sales on hand", True,
                  lambda r: f'{r["weeks_supply"]:.1f}' if r.get("weeks_supply") is not None else "—")]))

    # 3. what we have that stores are not calling for
    su = f["surplus"]
    p.append(f'<h2>What we have that stores are not calling for</h2><p class="muted">{_n(su["skus"])} products '
             f'have {_n(su["units"])} more units than stores need now; {_n(su["no_demand_skus"])} of them have no '
             f'store sales at all. Top {min(TOP, su["skus"])} by units left over.</p>'
             + _sku_table(su["top"], [
                 ("Product", False, _name), ("Sells a week", True, lambda r: _n(r["weekly"])),
                 ("Needed now", True, _need), ("On availability", True, lambda r: _n(r["available"])),
                 ("Left over", True, lambda r: f'<b>{_n(r["surplus"])}</b>'),
                 ("Weeks of sales on hand", True,
                  lambda r: f'{r["weeks_supply"]:.0f}' if r.get("weeks_supply") is not None else "no sales")]))

    se, hg = f.get("seasonal") or {}, f.get("hard_goods") or []
    if se.get("skus") or hg:
        p.append('<h2>Kept out of the figures above</h2>')
    if se.get("skus"):
        p.append(f'<h3>Seasonal and holiday items</h3><p class="muted">Planned by event, so they are not part of '
                 f'the fit. {_n(se["skus"])} items sold {_usd(se["weekly_usd"])} a week over the last four weeks; '
                 f'{_n(se["available_units"])} units are on this availability. Past-season items still selling '
                 f'down show here too.</p>'
                 + _sku_table(se["top"], [
                     ("Product", False, _name), ("Stores selling it", True, lambda r: _n(r["stores"])),
                     ("Sells a week", True, lambda r: _n(r["weekly"])),
                     ("Worth a week", True, lambda r: _usd(r["weekly_usd"])),
                     ("On availability", True, lambda r: _n(r["available"]))]))
    if hg:
        p.append('<h3>Hard goods</h3><p class="muted">Bought in, not grown: Climb-itt hardware and pre-built carts. '
                 'Stock on hand against what stores sell.</p>'
                 + _sku_table(hg, [
                     ("Product", False, _name), ("Sells a week", True, lambda r: _n(r["weekly"])),
                     ("On availability", True, lambda r: _n(r["available"])),
                     ("Weeks of sales on hand", True,
                      lambda r: f'{r["weeks_supply"]:.0f}' if r.get("weeks_supply") is not None else "—")]))

    # 4. how production can change
    p.append('<h2>How production can change to fit store needs</h2>')
    if ai.get("changes"):
        p.append(_ul(ai["changes"]))
    else:
        grow = sorted((x for x in f["families"] if x["short"] > 0), key=lambda x: -x["short"])[:5]
        less = sorted((x for x in f["families"] if x["surplus"] > 0), key=lambda x: -x["surplus"])[:5]
        p.append(_ul([f"Grow more {x['family']}: short by {_n(x['short'])} units against a need of {_n(x['need'])}."
                      for x in grow]
                     + [f"Grow less {x['family']}: {_n(x['surplus'])} units more than stores need now."
                        for x in less])
                 + '<p class="note">The written recommendations could not be generated this time, so this '
                   'section shows the computed gaps only.</p>')
    g = f.get("grades")
    if g:
        p.append('<h3>Availability by grade</h3><table><tr><th>Grade</th><th class="r">On availability</th>'
                 '<th class="r">Shipping this week</th><th class="r">Left after this week</th></tr>'
                 + "".join(f'<tr><td>Grade {E(str(k))}</td><td class="r">{_n(v.get("available_units"))}</td>'
                           f'<td class="r">{_n(v.get("shipping_units"))}</td><td class="r">{_n(v.get("left_units"))}</td></tr>'
                           for k, v in sorted(g.items(), reverse=True)) + "</table>")
    notes = ['"Needed now" is what it takes to fully stock every store this week, before the sell-through '
             'review, store rules or the full-cart minimum take anything off, so it is larger than what ships '
             f'({_n(t["shipped"])} units this week).',
             'Figures marked est. are estimates: the product is not on availability, so its need is worked out '
             'from its weekly sales and its family\'s need-to-sales ratio.',
             'This is one week. A gap that repeats over several weeks is a production signal; a one-week gap '
             'may be timing.',
             f'Product-level figures were {f["sku_source"]}.']
    p.append("<h2>How to read these figures</h2>" + _ul(list(ai.get("caveats") or []) + notes))
    p.append(f'<p class="foot">TPC IPR Engine · order {E(f["run"]["id"][:8])} · figures come from the saved order, '
             f'sales and this week\'s availability; the written recommendations '
             f'{"are AI-written from those figures" if ai.get("changes") else "were computed without the AI"}.</p>'
             '</div></body></html>')
    return "".join(p)
