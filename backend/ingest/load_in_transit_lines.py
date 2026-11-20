"""
Load a shipped week's store x SKU in-transit units into in_transit_lines, so IPR Engine v2
counts product already on trucks as inbound (§8) instead of guessing from the manifest.

Why: the engine's only other inbound sources are (a) prior runs in status exported/
finalized/approved — WK47 was built outside the engine and sits as a v1 'draft', so it is
invisible — and (b) a manifest fallback "avg delivery per SKU the store ever got", which
overstated WK47 by ~380K units. This loader computes what actually shipped.

Sources (all inside the HD shipments week folder, e.g. ...\\shipments\\WK 47):
  1. "Week N Compiled.xlsx" [Compiled]            Store | Load | Delivery Date
  2. "Week N Stores and Carts.xlsx"               store x cart-config counts
                                                  (config columns follow 'Cart Difference')
  3. "Week N Pack Plan File - *.xlsx" [Sheet]     the FINAL per-route kit detail:
       Route | KitDescription | KitQuantity | Variation | Notes | SKU | Eaches | Total Used
     'Cart Layouts' is shelf geometry only (no contents); 'Totals Used' / 'Deductions' are
     pivots of [Sheet]; 'Changes' and 'Availability Subs' are a LOG of edits that are
     ALREADY applied in [Sheet] (no TRIAL placeholders remain, cart counts are the
     post-change counts, Total Used ties to the pivots). They are therefore NOT re-applied —
     doing so would double-count. Verified on WK47: 0 TRIAL rows, 260/282 sub targets present
     on the named route+cart, Sheet total == Totals Used grand total.

Allocation (per route, per kit; units conserve to the pack plan):
  exact        S&C carts of that config on the route == pack-plan carts -> units x n/carts
  scaled       counts differ but within 2x -> by S&C share of that config on the route
  store        kit named "... (Store NNNN)" -> all units to that store
  route-pool   config not on the route in S&C (e.g. WK47 N1 got 'Holiday AS' instead of the
               A-D carts in S&C) -> by stores' surplus S&C carts on that route
  day-pool     route has no Compiled stores / no surplus (WK47 '47-N9') -> by surplus S&C
               carts across all routes shipping that day (LOW confidence; route re-cut
               after the Compiled sheet was frozen)
  unallocated  nothing to pool against — reported, not loaded
Confidence: day-pool units are written as separate rows with confidence 'LOW' (the engine
counts them at ipr_settings.in_transit_low_factor, default 50%); everything else is 'MEDIUM'.

SKUs: [Sheet].SKU is the Home Depot SKU (= skus.sku_id). Rows with no SKU (soil) or SKUs
absent from skus (Bloomaker) are reported as unmapped and not loaded.

Run (dry-run is the default; nothing is written without --apply):
  python backend/ingest/load_in_transit_lines.py "<...\\shipments\\WK 47>"
  python backend/ingest/load_in_transit_lines.py "<...\\shipments\\WK 47>" --json-out lines.json
  python backend/ingest/load_in_transit_lines.py "<...\\shipments\\WK 47>" --apply
Apply is idempotent: replaces every row for the computed shipping_week in one transaction.
Run with `python -I` when pointing at SharePoint folders (untrusted data).
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS in_transit_lines (
    shipping_week  date    NOT NULL,
    store_id       integer NOT NULL REFERENCES stores(store_id),
    sku_id         bigint  NOT NULL,
    route          text    NOT NULL,
    delivery_date  date,
    units          numeric(10,2) NOT NULL CHECK (units >= 0),
    method         text    NOT NULL,
    confidence     text    NOT NULL DEFAULT 'MEDIUM' CHECK (confidence IN ('MEDIUM','LOW')),
    source_file    text,
    loaded_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (shipping_week, store_id, sku_id, route, confidence)
);
CREATE INDEX IF NOT EXISTS in_transit_lines_deliv ON in_transit_lines(delivery_date);
ALTER TABLE in_transit_lines ENABLE ROW LEVEL SECURITY;
"""

SCALE_MAX = 2.0           # S&C vs pack-plan cart ratio still treated as the same carts
STORE_KIT = re.compile(r"^(?P<base>.+?)\s*-\s*.*\(Store\s*0*(?P<store>\d+)\)\s*$", re.I)


# ── workbook helpers ────────────────────────────────────────────────────────
def _wb(path):
    import openpyxl
    return openpyxl.load_workbook(path, read_only=True, data_only=True)


def _date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if v in (None, ""):
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(str(v).strip(), fmt).date()
        except ValueError:
            pass
    return None


def _int(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def find_sources(folder: Path, week: int):
    def pick(patterns, prefer=()):
        hits = [p for pat in patterns for p in folder.glob(pat)
                if not p.name.startswith("~$") and "backup" not in p.name.lower()]
        if not hits:
            return None
        for word in prefer:
            for p in hits:
                if word in p.name.lower():
                    return p
        return max(hits, key=lambda p: p.stat().st_mtime)
    compiled = pick([f"Week {week} Compiled.xlsx", f"Week {week}-2026 Workbook.xlsx",
                     f"WK {week}-2026 Workbook.xlsx"])
    carts = pick([f"Week {week} Stores and Carts*.xlsx"], prefer=("updated",))
    pack = pick([f"Week {week} Pack Plan File*.xlsx"], prefer=("final", "revised"))
    missing = [n for n, p in (("Compiled", compiled), ("Stores and Carts", carts),
                              ("Pack Plan File", pack)) if p is None]
    if missing:
        raise SystemExit(f"{folder}: missing {', '.join(missing)}")
    return compiled, carts, pack


def read_compiled(path):
    wb = _wb(path)
    ws = wb["Compiled"]
    it = ws.iter_rows(values_only=True)
    h = [str(x).strip() if x else "" for x in next(it)]
    si, li, di = h.index("Store"), h.index("Load"), h.index("Delivery Date")
    out = []
    for r in it:
        s = _int(r[si]) if r else None
        if s and r[li]:
            out.append((s, str(r[li]).strip(), _date(r[di])))
    wb.close()
    return out


def read_stores_and_carts(path):
    wb = _wb(path)
    rows = list(wb.worksheets[0].iter_rows(values_only=True))
    wb.close()
    h = [str(x).strip() if x else "" for x in rows[0]]
    start = h.index("Cart Difference") + 1 if "Cart Difference" in h else h.index("Holiday AA")
    cfgs = [c for c in h[start:] if c]
    counts = defaultdict(dict)
    for r in rows[1:]:
        s = _int(r[0]) if r else None
        if not s:
            continue
        for i, c in enumerate(h[start:], start):
            n = _int(r[i]) if i < len(r) else None
            if c and n:
                counts[s][c] = counts[s].get(c, 0) + n
    return counts, cfgs


def read_pack_plan(path):
    wb = _wb(path)
    ws = next((w for w in wb.worksheets if w.title == "Sheet"), None)
    if ws is None:
        raise SystemExit(f"{path.name}: no detail sheet 'Sheet' (has {wb.sheetnames})")
    it = ws.iter_rows(values_only=True)
    h = [str(x).strip() if x else "" for x in next(it)]
    need = ("ShipDate", "Route", "KitDescription", "KitQuantity", "Variation", "Notes",
            "ItemDescription", "SKU", "Eaches", "Total Used")
    miss = [c for c in need if c not in h]
    if miss:
        raise SystemExit(f"{path.name} [Sheet] missing columns {miss}")
    rows = [dict(zip(h, r)) for r in it if r and any(v is not None for v in r)]
    totals = {}
    if "Totals Used" in wb.sheetnames:
        for r in wb["Totals Used"].iter_rows(values_only=True):
            if r and r[0] not in (None, "Row Labels") and isinstance(r[1], (int, float)):
                totals[str(r[0])] = float(r[1])
    wb.close()
    return rows, totals


# ── allocation ──────────────────────────────────────────────────────────────
def allocate(compiled, sc, pack_rows):
    """Pure function: returns (lines, report). lines = {(store, sku, route): [units, deliv, method]}"""
    rep = defaultdict(list)
    store_routes = defaultdict(dict)                       # store -> {route: delivery_date}
    for s, rt, dd in compiled:
        store_routes[s][rt] = dd
    route_stores = defaultdict(set)
    for s, rts in store_routes.items():
        for rt in rts:
            route_stores[rt].add(s)

    # pack plan: units per (route, kit, sku) and carts per (route, kit)
    units = defaultdict(lambda: defaultdict(float))
    var_carts = defaultdict(int)                           # (route, kit, variation) -> carts
    route_day = {}
    family_of = {}
    unmapped = Counter()
    for r in pack_rows:
        rt, kit = str(r["Route"]).strip(), str(r["KitDescription"]).strip()
        route_day[rt] = _date(r["ShipDate"])
        k = (rt, kit, r["Variation"])
        var_carts[k] = max(var_carts[k], _int(r["KitQuantity"]) or 0)
        sku = _int(r["SKU"])
        tu = float(r["Total Used"] or 0)
        if not sku:
            unmapped[f"(no SKU) {r['ItemDescription']}"] += tu
            continue
        units[(rt, kit)][sku] += tu
        family_of[sku] = str(r["ItemDescription"])
    carts = defaultdict(int)
    for (rt, kit, _v), n in var_carts.items():
        carts[(rt, kit)] += n

    # S&C counts -> per (route, kit) per store. Single-route stores first, then split stops
    # go to the route where the pack plan still has room for that config.
    sc_rk = defaultdict(dict)
    sc_left = {s: dict(c) for s, c in sc.items()}
    for (rt, kit) in list(carts):                         # store-specific kits replace a generic cart
        m = STORE_KIT.match(kit)
        if m:
            s, base = int(m.group("store")), m.group("base").strip()
            rep["store_specific_kits"].append(f"{rt} '{kit}' -> store {s} (base '{base}')")
            routed = sum(sc.get(x, {}).get(base, 0) for x in route_stores[rt])
            surplus = max(0, routed - carts.get((rt, base), 0))
            have = sc_left.get(s, {}).get(base, 0)
            if have and surplus:
                sc_left[s][base] = have - min(have, surplus)
    multi = sorted(s for s in store_routes if len(store_routes[s]) > 1)
    for s in sorted(store_routes):
        if s in multi:
            continue
        rt = next(iter(store_routes[s]))
        for kit, n in sc_left.get(s, {}).items():
            if n:
                sc_rk[(rt, kit)][s] = sc_rk[(rt, kit)].get(s, 0) + n
    for s in multi:
        for kit, n in sc_left.get(s, {}).items():
            if not n:
                continue
            rt = max(sorted(store_routes[s]),
                     key=lambda r_: carts.get((r_, kit), 0) - sum(sc_rk[(r_, kit)].values()))
            sc_rk[(rt, kit)][s] = sc_rk[(rt, kit)].get(s, 0) + n
            rep["split_stops"].append(f"store {s}: {n} x '{kit}' -> {rt}")
    for s in store_routes:
        if s not in sc:
            rep["compiled_store_not_in_stores_and_carts"].append(s)

    # surplus weight: S&C carts on a route in configs the pack plan has fewer of
    surplus = defaultdict(float)                          # store -> carts
    for (rt, kit), per in sc_rk.items():
        tot = sum(per.values())
        short = tot - carts.get((rt, kit), 0)
        if short > 0:
            for s, n in per.items():
                surplus[(rt, s)] += n * short / tot
        if (rt, kit) not in carts:
            rep["s&c_config_not_in_pack_plan"].append(f"{rt} '{kit}' ({tot} carts)")

    lines = defaultdict(lambda: [0.0, None, None])
    unalloc = Counter()

    def put(s, rt, sku, u, method):
        rec = lines[(s, sku, rt, "LOW" if method == "day-pool" else "MEDIUM")]
        rec[0] += u
        rec[1] = store_routes[s].get(rt) or (route_day.get(rt) and route_day[rt] + timedelta(days=1))
        rec[2] = method if rec[2] in (None, method) else "mixed"

    for (rt, kit), per_sku in sorted(units.items()):
        m = STORE_KIT.match(kit)
        if m and int(m.group("store")) in store_routes:
            s = int(m.group("store"))
            for sku, u in per_sku.items():
                put(s, rt if rt in store_routes[s] else next(iter(store_routes[s])), sku, u, "store")
            continue
        per = sc_rk.get((rt, kit), {})
        tot, pc = sum(per.values()), carts[(rt, kit)]
        if tot and pc and (tot == pc or max(tot, pc) / min(tot, pc) <= SCALE_MAX):
            method = "exact" if tot == pc else "scaled"
            if method == "scaled":
                rep["scaled"].append(f"{rt} '{kit}': pack {pc} carts vs S&C {tot}")
            weights, targets = per, {s: rt for s in per}
        else:
            weights = {s: w for (r_, s), w in surplus.items() if r_ == rt and w > 0}
            targets = {s: rt for s in weights}
            method = "route-pool"
            if not weights:
                day = route_day.get(rt)
                weights = {s: w for (r_, s), w in surplus.items()
                           if w > 0 and route_day.get(r_) == day}
                targets = {s: r_ for (r_, s), w in surplus.items()
                           if w > 0 and route_day.get(r_) == day}
                method = "day-pool"
            rep[method].append(f"{rt} '{kit}': {pc} carts, {sum(per_sku.values()):.0f}u "
                               f"-> {len(weights)} stores")
        wt = sum(weights.values())
        if not wt:
            for sku, u in per_sku.items():
                unalloc[sku] += u
            rep["unallocated"].append(f"{rt} '{kit}': {sum(per_sku.values()):.0f}u")
            continue
        for s, w in weights.items():
            for sku, u in per_sku.items():
                put(s, targets[s], sku, u * w / wt, method)
    return lines, rep, unalloc, unmapped, family_of, route_day


def build(folder: Path):
    m = re.search(r"(\d{1,2})\s*$", folder.name)
    if not m:
        raise SystemExit(f"Folder name must end in the week number (got '{folder.name}')")
    week = int(m.group(1))
    p_comp, p_sc, p_pack = find_sources(folder, week)
    compiled = read_compiled(p_comp)
    sc, cfgs = read_stores_and_carts(p_sc)
    pack_rows, totals_used = read_pack_plan(p_pack)
    lines, rep, unalloc, unmapped, family_of, route_day = allocate(compiled, sc, pack_rows)
    ship_days = sorted(d for d in route_day.values() if d)
    shipping_week = ship_days[0] - timedelta(days=ship_days[0].weekday())
    return dict(week=week, shipping_week=shipping_week, files=[p_comp, p_sc, p_pack],
                compiled=compiled, sc=sc, pack_rows=pack_rows, totals_used=totals_used,
                lines=lines, rep=rep, unalloc=unalloc, unmapped=unmapped, family_of=family_of)


def reconcile(b, valid_stores=None, valid_skus=None, log=print):
    lines = b["lines"]
    pack_total = sum(float(r["Total Used"] or 0) for r in b["pack_rows"])
    alloc = sum(v[0] for v in lines.values())
    log(f"Week {b['week']} -> shipping_week {b['shipping_week']}")
    log("  sources: " + " | ".join(p.name for p in b["files"]))
    comp_stores = {s for s, _, _ in b["compiled"]}
    line_stores = {k[0] for k in lines}
    log(f"  stores: Compiled {len(comp_stores)} ({len(b['compiled'])} rows) | with in-transit lines "
        f"{len(line_stores)} | Compiled stores with no lines {sorted(comp_stores - line_stores)[:15]}")
    log(f"  units: pack plan {pack_total:,.0f} = allocated {alloc:,.0f} + unallocated "
        f"{sum(b['unalloc'].values()):,.0f} + no-SKU {sum(b['unmapped'].values()):,.0f}  "
        f"(diff {pack_total - alloc - sum(b['unalloc'].values()) - sum(b['unmapped'].values()):+.1f})")
    by_fam = defaultdict(float)
    for (s, sku, rt, _c), v in lines.items():
        by_fam[b["family_of"].get(sku, "?")] += v[0]
    for sku, u in b["unalloc"].items():
        by_fam[b["family_of"].get(sku, "?")] += u
    for d, u in b["unmapped"].items():
        by_fam[d.replace("(no SKU) ", "")] += u
    has_tu = bool(b["totals_used"])
    log("  by pack family (allocated+unallocated vs 'Totals Used'):" if has_tu else
        "  by pack family (no 'Totals Used' pivot in this pack plan; totals tie to [Sheet] above):")
    for f in sorted(set(by_fam) | set(b["totals_used"])):
        if f == "Grand Total":
            continue
        t = b["totals_used"].get(f)
        flag = "" if not has_tu or (t is not None and abs(by_fam.get(f, 0) - t) < 0.5) else "  <-- MISMATCH"
        log(f"    {f:<52} {by_fam.get(f, 0):>9,.0f}  {('%9.0f' % t) if t is not None else '      n/a'}{flag}")
    meth = Counter()
    for v in lines.values():
        meth[v[2]] += v[0]
    log("  units by method: " + ", ".join(f"{k} {u:,.0f}" for k, u in meth.most_common()))
    for k in ("store_specific_kits", "split_stops", "scaled", "route-pool", "day-pool",
              "unallocated", "s&c_config_not_in_pack_plan", "compiled_store_not_in_stores_and_carts"):
        if b["rep"].get(k):
            log(f"  {k} ({len(b['rep'][k])}):")
            for x in b["rep"][k][:25]:
                log(f"    - {x}")
    if b["unmapped"]:
        log("  unmapped (no SKU in pack plan): " +
            ", ".join(f"{d} {u:,.0f}u" for d, u in b["unmapped"].items()))
    bad_sku = bad_store = Counter()
    if valid_skus is not None:
        bad_sku = Counter()
        for (s, sku, rt, _c), v in lines.items():
            if sku not in valid_skus:
                bad_sku[(sku, b["family_of"].get(sku))] += v[0]
        if bad_sku:
            log("  SKUs not in skus master (not loaded): " +
                ", ".join(f"{k} '{f}' {u:,.0f}u" for (k, f), u in bad_sku.items()))
    if valid_stores is not None:
        bad_store = Counter()
        for (s, sku, rt, _c), v in lines.items():
            if s not in valid_stores:
                bad_store[s] += v[0]
        if bad_store:
            log("  stores not in stores master (not loaded until added): " +
                ", ".join(f"{s} {u:,.0f}u" for s, u in bad_store.items()))
    return bad_sku, bad_store


def loadable(b, valid_stores, valid_skus):
    return [(b["shipping_week"], s, sku, rt, v[1], round(v[0], 2), v[2], conf, b["files"][2].name)
            for (s, sku, rt, conf), v in sorted(b["lines"].items())
            if s in valid_stores and sku in valid_skus and v[0] > 0]


def gap_table(b, valid_stores, valid_skus, log=print):
    """Explicit bridge: pack plan = loaded + not-in-SKU-master + no-SKU + unknown store + unallocated."""
    cols = ("pack", "loaded", "low", "sku_not_in_master", "no_sku", "unknown_store", "unallocated")
    t = defaultdict(lambda: dict.fromkeys(cols, 0.0))
    fam = b["family_of"]
    for r in b["pack_rows"]:
        t[str(r["ItemDescription"])]["pack"] += float(r["Total Used"] or 0)
    for (s, sku, rt, conf), v in b["lines"].items():
        f = t[fam.get(sku, "?")]
        if sku not in valid_skus:
            f["sku_not_in_master"] += v[0]
        elif s not in valid_stores:
            f["unknown_store"] += v[0]
        else:
            f["loaded"] += v[0]
            if conf == "LOW":
                f["low"] += v[0]
    for sku, u in b["unalloc"].items():
        t[fam.get(sku, "?")]["unallocated"] += u
    for d, u in b["unmapped"].items():
        t[d.replace("(no SKU) ", "")]["no_sku"] += u
    log("  RECONCILIATION  pack plan = loaded + SKU not in master + no SKU + unknown store + unallocated")
    log(f"    {'family':<46}" + "".join(f"{c:>18}" for c in cols) + "    check")
    tot = dict.fromkeys(cols, 0.0)
    for name in sorted(t):
        row = t[name]
        for c in cols:
            tot[c] += row[c]
        diff = row["pack"] - sum(row[c] for c in cols if c not in ("pack", "low"))
        log(f"    {name[:46]:<46}" + "".join(f"{row[c]:>18,.0f}" for c in cols) +
            ("       ok" if abs(diff) < 0.5 else f"  {diff:+,.1f}"))
    diff = tot["pack"] - sum(tot[c] for c in cols if c not in ("pack", "low"))
    log(f"    {'TOTAL':<46}" + "".join(f"{tot[c]:>18,.0f}" for c in cols) +
        ("       ok" if abs(diff) < 0.5 else f"  {diff:+,.1f}"))
    log(f"    gap pack - loaded = {tot['pack'] - tot['loaded']:,.0f}u = SKU not in master "
        f"{tot['sku_not_in_master']:,.0f} + no SKU {tot['no_sku']:,.0f} + unknown store "
        f"{tot['unknown_store']:,.0f} + unallocated {tot['unallocated']:,.0f}  "
        f"| loaded at LOW confidence: {tot['low']:,.0f}u")
    return tot


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("week_folder", help=r"...\shipments\WK NN")
    ap.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    ap.add_argument("--json-out", help="write loadable rows to this JSON file (for previews)")
    args = ap.parse_args()

    b = build(Path(args.week_folder))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import get_conn
    conn = get_conn()
    if not args.apply:
        conn.set_session(readonly=True)
    cur = conn.cursor()
    cur.execute("SELECT store_id FROM stores")
    valid_stores = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT sku_id FROM skus")
    valid_skus = {r[0] for r in cur.fetchall()}
    reconcile(b, valid_stores, valid_skus)
    rows = loadable(b, valid_stores, valid_skus)
    gap_table(b, valid_stores, valid_skus)
    print(f"  loadable rows: {len(rows):,} | units {sum(r[5] for r in rows):,.0f} | "
          f"stores {len({r[1] for r in rows})} | SKUs {len({r[2] for r in rows})} | "
          f"LOW {sum(r[5] for r in rows if r[7] == 'LOW'):,.0f}u")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            [dict(shipping_week=str(r[0]), store_id=r[1], sku_id=r[2], route=r[3],
                  delivery_date=str(r[4]) if r[4] else None, units=r[5], method=r[6],
                  confidence=r[7]) for r in rows]))
        print(f"  wrote {args.json_out}")
    if not args.apply:
        cur.execute("SELECT to_regclass('public.in_transit_lines') IS NOT NULL")
        if cur.fetchone()[0]:
            cur.execute("SELECT COUNT(*), COALESCE(SUM(units),0) FROM in_transit_lines WHERE shipping_week=%s",
                        (b["shipping_week"],))
            n, u = cur.fetchone()
            print(f"  currently loaded for {b['shipping_week']}: {n} rows, {float(u):,.0f}u (would be replaced)")
        else:
            print("  table in_transit_lines does not exist yet (--apply creates it)")
        print("DRY RUN - nothing written. Re-run with --apply to load.")
        conn.close()
        return
    import psycopg2.extras
    cur.execute(DDL)
    cur.execute("""ALTER TABLE in_transit_lines ADD COLUMN IF NOT EXISTS confidence text NOT NULL
                   DEFAULT 'MEDIUM'""")
    cur.execute("DELETE FROM in_transit_lines WHERE shipping_week = %s", (b["shipping_week"],))
    deleted = cur.rowcount
    psycopg2.extras.execute_values(cur, """
        INSERT INTO in_transit_lines (shipping_week, store_id, sku_id, route, delivery_date,
                                      units, method, confidence, source_file) VALUES %s""", rows, page_size=1000)
    conn.commit()
    print(f"COMMITTED - replaced {deleted} rows with {len(rows)} for shipping_week {b['shipping_week']}")
    conn.close()


if __name__ == "__main__":
    main()
