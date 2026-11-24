"""Demand history by selling opportunity (CEO review of the IPR runs, 2026-11-24).

What the CEO asked for, and where each part lives:

  1. Measure each store x SKU only over weeks it could actually sell: from the week the product
     first reached the store, while the store was reporting, and while stock was on hand.
     A week with no delivery still counts when earlier stock was there.  -> weekly_availability()
  2. June and July are the normal-replenishment benchmark, sized on what sold repeatedly (a
     typical week, not a spike). April-May plans spring; Mother's Day is measured separately.
                                                                      -> typical_week(), benchmarks()
  3. When June-July was short of stock, use another comparable period with reliable stock,
     scaled to June-July level by a same-store, same-SKU index.        -> benchmarks()
  4. New stores / SKUs with little history: size from comparable stores (same velocity code and
     region), product family when the SKU itself is too new, then let the store's own selling
     take over as weeks accumulate.                                    -> comparable_rates(), blend
  5. Growth vs coverage: split a period-over-period change into same store x SKU growth and the
     change from lines added or lost.                                  -> growth_split()

The stock model is deliberately simple and errs toward "was available": delivered lots are
carried FIFO; a lot's sellable stock is the smaller of what is physically left and what the
calibrated lot-age curve says is still sellable at its age (~60% of a delivery ever sells, a lot is
done by ~70 days; long-life families keep a flat 60% until their freshness limit),
sold down by reported sales — a week counts only when sellable stock going in covers at least one
normal week of the line's sales (a picked-over shelf is constrained, not "reasonably available"); selling more than the record delivered marks stock unknown (available)
until the next delivery; and any week followed by a later sale with no delivery in between counts (the product
was evidently on the shelf). Erring toward "available" keeps rates from being inflated by a
missing delivery record.
"""
from collections import defaultdict, deque
from datetime import date, timedelta

HISTORY_WEEKS = 57            # 13 months of selling weeks, ending at the sales anchor
LEAD_WEEKS = 18               # earlier deliveries/sales, so stock carried into the window is known


def mothers_day(year):
    """Second Sunday of May."""
    d = date(year, 5, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7 + 7)


def week_back(anchor, d):
    """0 = the 7 days ending at the anchor, 1 = the 7 days before that, ..."""
    return (anchor - d).days // 7


def curve_share(curve, age_days):
    """Sellable share of a delivered lot at an age, from the calibrated [(days, share)] curve."""
    if age_days <= curve[0][0]:
        return curve[0][1]
    for (a0, v0), (a1, v1) in zip(curve, curve[1:]):
        if age_days <= a1:
            return v0 + (v1 - v0) * (age_days - a0) / (a1 - a0)
    return 0.0


def weekly_availability(sold, delivered, reporting, fresh_weeks, n_weeks, sellable_share=1.0,
                        min_stock=0.0, curve=None):
    """Per week (index = weeks back, 0 newest) whether the line had a selling opportunity.

    sold / delivered: {weeks_back: units}. reporting: set of weeks_back the store reported sales.
    fresh_weeks: a lot's selling life. sellable_share: the part of a delivery that realistically
    sells (~60%); once that part has sold the shelf is picked over. curve: the calibrated lot-age
    curve [(days, share)] — when given, a lot's sellable stock is the smaller of what is
    physically left and delivered x share(age), so aging plants stop counting as selling stock. min_stock: sellable units needed going into a week
    for it to count (one normal week of the line's sales) — a picked-over shelf selling its last
    few units is constrained, so neither its low nor its zero weeks are demand. A record that
    shows more sold than delivered makes stock unknown (counted available) until the next
    delivery. Returns booleans, length n_weeks, index = weeks back.
    """
    oldest = max([n_weeks - 1, *sold.keys(), *delivered.keys()])
    lots = deque()                       # [age_weeks, physical, sellable (flat), delivered] oldest first
    unknown = False                      # sold more than the record delivered: stock unknown
    avail = [False] * n_weeks
    for wb in range(oldest, -1, -1):     # oldest week first
        for lot in lots:
            lot[0] += 1
        while lots and lots[0][0] >= fresh_weeks:
            lots.popleft()               # past its selling life
        d = float(delivered.get(wb, 0) or 0)
        if d > 0:
            lots.append([0, d, d * sellable_share, d])
            unknown = False
        if curve:
            sellable = sum(min(l[1], l[3] * curve_share(curve, 7 * l[0])) for l in lots)
        else:
            sellable = sum(l[2] for l in lots)
        s = max(0.0, float(sold.get(wb, 0) or 0))
        if wb < n_weeks:
            if unknown:
                avail[wb] = s > 0 or wb in reporting
            elif min_stock > 0:
                # selling more than the model holds means the record understates stock: the
                # week was a real selling week, not a picked-over one
                avail[wb] = (s > sellable + 1e-9) or (sellable >= min_stock - 1e-9 and (s > 0 or wb in reporting))
            else:
                avail[wb] = s > 0 or (sellable > 1e-9 and wb in reporting)
        while s > 0 and lots:            # sell oldest first
            take = min(s, lots[0][1])
            lots[0][1] -= take
            lots[0][2] = max(0.0, lots[0][2] - take)
            s -= take
            if lots[0][1] <= 1e-9:
                lots.popleft()
        if s > 1e-9:
            # sales beyond recorded deliveries: the delivery record is incomplete, so a sell-out
            # cannot be inferred — count the weeks as available until the next recorded delivery
            unknown = True
    return avail


MIN_RATE_WEEKS = 4            # a rate is never one or two lucky weeks


def window_rate(sold, avail, weeks, min_weeks=MIN_RATE_WEEKS):
    """Units per AVAILABLE week over `weeks` (iterable of weeks back), dividing by at least
    `min_weeks` (or the window length if shorter). (rate, n_available)."""
    weeks = list(weeks)
    n = sum(1 for w in weeks if avail[w])
    if not n:
        return None, 0
    den = max(n, min(min_weeks, len(weeks)))
    return sum(max(0.0, float(sold.get(w, 0) or 0)) for w in weeks if avail[w]) / den, n


def typical_week(values):
    """What sold repeatedly: the mean of available weeks with the single biggest week capped at
    the second biggest, so one spike (an ad, a holiday) never sets the routine rate."""
    v = sorted(max(0.0, x) for x in values)
    if not v:
        return None
    if len(v) >= 3:
        v[-1] = v[-2]
    return sum(v) / len(v)


def period_weeks(anchor, start, end, exclude=()):
    """Weeks back whose 7-day span ends inside [start, end], minus excluded dates' weeks."""
    out = []
    for wb in range(0, HISTORY_WEEKS + LEAD_WEEKS):
        wk_end = anchor - timedelta(days=7 * wb)
        if start <= wk_end <= end:
            out.append(wb)
    ex = {week_back(anchor, d) for d in exclude}
    return [w for w in out if w not in ex]


def last_occurrence(anchor, m0, d0, m1, d1):
    """Most recent [m0/d0, m1/d1] span that ended on or before the anchor."""
    y = anchor.year
    end = date(y, m1, d1)
    if end > anchor:
        y -= 1
    return date(y, m0, d0), date(y, m1, d1)


def benchmark_periods(anchor, ship_month):
    """Ordered (label, weeks-back list). Spring ship months plan from April-May; otherwise June-
    July is primary. Mother's Day week is never inside a routine benchmark."""
    jj = last_occurrence(anchor, 6, 1, 7, 31)
    aso = last_occurrence(anchor, 8, 1, 9, 30)
    am = last_occurrence(anchor, 4, 1, 5, 31)
    md = mothers_day(am[0].year)
    p_jj = ("Jun-Jul " + str(jj[0].year), period_weeks(anchor, *jj))
    p_as = ("Aug-Sep " + str(aso[0].year), period_weeks(anchor, *aso))
    p_am = ("Apr-May " + str(am[0].year) + " ex Mother's Day", period_weeks(anchor, *am, exclude=[md]))
    if ship_month in (3, 4, 5):
        return [p_am, p_jj, p_as]
    return [p_jj, p_as, p_am]


def period_benchmark(sold, avail, weeks, min_weeks, min_share):
    """(typical rate, n available) when the period had reliable stock, else (None, n)."""
    if not weeks:
        return None, 0
    vals = [float(sold.get(w, 0) or 0) for w in weeks if w < len(avail) and avail[w]]
    n = len(vals)
    if n < min_weeks or n / len(weeks) < min_share:
        return None, n
    return typical_week(vals), n


def benchmarks(line_hist, periods, min_weeks, min_share):
    """Per line: (rate at the PRIMARY period's level, label, n_weeks). A fallback period is scaled
    by the same-store, same-SKU index between it and the primary (lines reliable in both)."""
    per = {}          # key -> [rate or None per period]
    for k, (sold, avail) in line_hist.items():
        per[k] = [period_benchmark(sold, avail, w, min_weeks, min_share) for _, w in periods]
    index = [1.0]
    for i in range(1, len(periods)):
        num = den = 0.0
        for r in per.values():
            if r[0][0] is not None and r[i][0] is not None:
                num += r[0][0]
                den += r[i][0]
        index.append(num / den if den > 0 and num > 0 else 1.0)
    out = {}
    for k, r in per.items():
        for i, (rate, n) in enumerate(r):
            if rate is not None:
                out[k] = (rate * index[i], periods[i][0] + ("" if i == 0 else f" x {index[i]:.2f}"), n)
                break
    return out, index


def comparable_rates(rates, store_group, sku_family, min_stores=5):
    """Median adjusted weekly rate by (store group, SKU), and per-SKU median by (group, family)
    for products too new to have comparables of their own. rates: {(store, sku): rate}."""
    by_sku, by_fam = defaultdict(list), defaultdict(list)
    for (st, sk), r in rates.items():
        g = store_group.get(st)
        if g is None:
            continue
        by_sku[(g, sk)].append(r)
        by_fam[(g, sku_family.get(sk))].append(r)
    med = lambda v: sorted(v)[len(v) // 2]
    return ({k: med(v) for k, v in by_sku.items() if len(v) >= min_stores},
            {k: med(v) for k, v in by_fam.items() if len(v) >= min_stores})


def growth_split(now, before):
    """Split total change into same-line growth and coverage (lines added / lost).
    now / before: {(store, sku): units}."""
    same = set(now) & set(before)
    s_now = sum(now[k] for k in same)
    s_before = sum(before[k] for k in same)
    added = sum(v for k, v in now.items() if k not in before)
    lost = sum(v for k, v in before.items() if k not in now)
    tot_now, tot_before = sum(now.values()), sum(before.values())
    return {
        "units_now": round(tot_now), "units_before": round(tot_before),
        "total_change_pct": round(100 * (tot_now / tot_before - 1), 1) if tot_before else None,
        "same_line_growth_pct": round(100 * (s_now / s_before - 1), 1) if s_before else None,
        "same_lines": len(same), "lines_added": len(set(now) - set(before)),
        "lines_lost": len(set(before) - set(now)),
        "units_from_added_lines": round(added), "units_from_lost_lines": round(lost),
        "stores_now": len({k[0] for k in now}), "stores_before": len({k[0] for k in before}),
    }


# ── loader ────────────────────────────────────────────────────────────────────

LONG_LIFE_DAYS = 120


def load(cur, anchor, fresh_of, fresh_default_days, dedup_deliveries_sql, stores, sku_family,
         ship_month, S, log=print, lot_curve=None):
    """Read 13 months (+ lead-in) of weekly sales and deliveries; return everything the engine
    needs. One pass, read-only."""
    n_all = HISTORY_WEEKS + LEAD_WEEKS
    start = anchor - timedelta(days=7 * n_all)
    cur.execute("""SELECT store_id, sku_id, ((%s::date - sale_date) / 7)::int AS wb,
                          SUM(units_sold)::float AS u
                   FROM sales WHERE sale_date > %s AND sale_date <= %s
                   GROUP BY 1, 2, 3""", (anchor, start, anchor))
    sold = defaultdict(dict)
    reporting = defaultdict(set)
    for st, sk, wb, u in cur.fetchall():
        if u:
            sold[(st, sk)][wb] = u
            if u > 0:
                reporting[st].add(wb)
    cur.execute("""SELECT store_id, sku_id, ((%s::date - delivery_date) / 7)::int AS wb,
                          SUM(units_delivered)::float AS u
                   FROM {dd} dl WHERE delivery_date > %s AND delivery_date <= %s
                   GROUP BY 1, 2, 3""".format(dd=dedup_deliveries_sql), (anchor, start, anchor))
    deliv = defaultdict(dict)
    for st, sk, wb, u in cur.fetchall():
        if u and u > 0:
            deliv[(st, sk)][wb] = u

    min_w = int(S.get("benchmark_min_avail_weeks", 4) or 4)
    min_share = float(S.get("benchmark_min_avail_share", 0.5) or 0.5)
    limited = int(S.get("limited_history_weeks", 4) or 4)
    reasonable_weeks = float(S.get("reasonable_stock_weeks", 1.0) or 0)
    lot_curve = lot_curve or [(0, 0.60), (28, 0.25), (42, 0.14), (56, 0.07), (63, 0.05), (70, 0.0)]
    lot_life_days, sellable_share = lot_curve[-1][0], lot_curve[0][1]

    hist, lines = {}, {}
    for k in set(sold) | set(deliv):
        st, sk = k
        # a lot's selling life: the calibrated lot-age curve (plants are done by ~70 days), or the
        # family freshness limit for long-life product (terrariums, glass, soil: 120+ days)
        fd = float(fresh_of.get(sk, fresh_default_days))
        long_life = fd >= LONG_LIFE_DAYS
        life = fd if long_life else min(fd, lot_life_days)
        fw = max(2, int(round(life / 7.0)))
        cv = None if long_life else lot_curve
        rep_st = reporting.get(st, set())
        av = weekly_availability(sold.get(k, {}), deliv.get(k, {}), rep_st, fw, n_all, sellable_share,
                                 curve=cv)
        # second pass: "reasonably available" = sellable stock for at least `reasonable_weeks`
        # of this line's normal week (typical week over the last 26 selling weeks)
        s0 = sold.get(k, {})
        norm = typical_week([float(s0.get(w, 0) or 0) for w in range(0, 26) if av[w]])
        if norm and reasonable_weeks > 0:
            av = weekly_availability(s0, deliv.get(k, {}), rep_st, fw, n_all, sellable_share,
                                     min_stock=norm * reasonable_weeks, curve=cv)
        s = sold.get(k, {})
        hist[k] = (s, av)
        r4, a4 = window_rate(s, av, range(0, 4))
        r10, a10 = window_rate(s, av, range(0, 10))
        r13, a13 = window_rate(s, av, range(0, 13))
        a_total = sum(av[:HISTORY_WEEKS])
        first = next((wb for wb in range(n_all - 1, -1, -1) if av[wb]), None)
        lines[k] = dict(r4=r4, a4=a4, r10=r10, a10=a10, r13=r13, a13=a13, a_total=a_total,
                        first_avail_wb=first)

    periods = benchmark_periods(anchor, ship_month)
    bench, index = benchmarks(hist, periods, min_w, min_share)

    # Mother's Day, measured on its own (never part of a routine benchmark)
    md = mothers_day(anchor.year if anchor >= mothers_day(anchor.year) else anchor.year - 1)
    md_wb = week_back(anchor, md)
    md_rate = {k: float(s.get(md_wb, 0) or 0) for k, (s, av) in hist.items()
               if md_wb < len(av) and av[md_wb]}

    # comparables (velocity code + region) from lines with an established 13-week rate
    group = {sid: (st.get("volume_code"), st.get("region")) for sid, st in stores.items()}
    established = {k: v["r13"] for k, v in lines.items() if v["r13"] is not None and v["a13"] >= limited}
    cmp_sku, cmp_fam = comparable_rates(established, group, sku_family)

    # growth vs coverage: last 13 weeks vs the same weeks a year earlier, and Jun-Jul vs Jun-Jul
    def units(weeks):
        out = {}
        for k, s in sold.items():
            u = sum(max(0.0, s.get(w, 0) or 0) for w in weeks)
            if u > 0:
                out[k] = u
        return out
    growth = {"last_13_weeks_vs_year_ago": growth_split(units(range(0, 13)), units(range(52, 65)))}
    jj_now = last_occurrence(anchor, 6, 1, 7, 31)
    jj_prev = (jj_now[0].replace(year=jj_now[0].year - 1), jj_now[1].replace(year=jj_now[1].year - 1))
    w_prev = period_weeks(anchor, *jj_prev)
    if w_prev and max(w_prev) < n_all:
        growth[f"jun_jul_{jj_now[0].year}_vs_{jj_prev[0].year}"] = growth_split(
            units(period_weeks(anchor, *jj_now)), units(w_prev))

    n_short = sum(1 for k, v in lines.items() if v["a13"] < 13 and v["r13"] is not None)
    log(f"Demand history (13 mo, by selling opportunity): {len(lines):,} store x SKU lines · "
        f"{n_short:,} with fewer than 13 selling weeks in the last 13 · benchmark {len(bench):,} lines "
        f"({', '.join(f'{p[0]} x {i:.2f}' for p, i in zip(periods, index))}) · "
        f"comparables {len(cmp_sku):,} group x SKU, {len(cmp_fam):,} group x family")
    return dict(lines=lines, bench=bench, bench_index=dict(zip([p[0] for p in periods], index)),
                periods=[p[0] for p in periods], md_rate=md_rate, md_date=md, group=group,
                cmp_sku=cmp_sku, cmp_fam=cmp_fam, growth=growth, limited_weeks=limited)
