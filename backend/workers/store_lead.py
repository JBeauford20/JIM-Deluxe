"""Store delivery profiles — how many days it usually takes a truck to reach each store.

Built from the routed history in delivery_manifest (one row per store per load: ship date,
delivery date, week number), which the weekly ingest keeps current and
backend/ingest/load_delivery_history.py backfills from the weekly shipping workbooks.

For every store:
  days        usual days from the MONDAY OF THE SHIP WEEK to delivery (median) — the number
              the engine needs: orders are written against a ship week, not a ship date
  p80         8 deliveries in 10 arrive within this many days
  transit     usual days from ship date to delivery date (median)
  n           deliveries on record
  basis       'store' (3+ deliveries of its own), else 'state', else 'division', else 'network'
Pure Python, no DB — the engine, the API and the tests all call build_profiles().
"""
from collections import Counter, defaultdict
from datetime import date

MIN_OWN = 3            # deliveries a store needs before its own history is used
MIN_GROUP = 30         # deliveries a state/division needs before it is used as a fallback
MAX_DAYS = 14          # Monday -> delivery beyond this is a typo / re-route, not a lead time
DEFAULT_DAYS = 7       # network median, 2026 history (16,089 deliveries)
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def week_monday(week_number, near):
    """Monday of ISO `week_number` in whichever year puts it closest to the date `near`."""
    best = None
    for y in (near.year - 1, near.year, near.year + 1):
        try:
            m = date.fromisocalendar(y, int(week_number), 1)
        except ValueError:
            continue
        if best is None or abs((near - m).days) < abs((near - best).days):
            best = m
    return best


def _median(v):
    v = sorted(v)
    return v[len(v) // 2] if len(v) % 2 else v[len(v) // 2 - 1]      # lower median: whole days


def _p(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))]


def clean(rows):
    """[(store_id, days Monday->delivery, transit days|None, ship date|None, delivery date)]
    from manifest rows (dicts: store_id, week_number, ship_date, delivery_date). Rows with no
    delivery date, no week, or an implausible gap are dropped."""
    out = []
    for r in rows:
        dl, wk = r.get("delivery_date"), r.get("week_number")
        if not dl or not wk:
            continue
        mon = week_monday(wk, dl)
        if mon is None:
            continue
        d = (dl - mon).days
        if d < -1 or d > MAX_DAYS:
            continue
        ship = r.get("ship_date")
        tr = (dl - ship).days if ship and 0 <= (dl - ship).days <= 10 else None
        out.append((int(r["store_id"]), d, tr, ship, dl))
    return out


def build_profiles(rows, stores):
    """rows: manifest dict rows. stores: {store_id: {state, division}}.
    Returns ({store_id: profile}, summary). Every store in `stores` gets a profile."""
    obs = clean(rows)
    per, per_tr, per_dl, per_last = defaultdict(list), defaultdict(list), defaultdict(list), {}
    for sid, d, tr, _ship, dl in obs:
        per[sid].append(d)
        per_dl[sid].append(dl.weekday())
        if tr is not None:
            per_tr[sid].append(tr)
        if sid not in per_last or dl > per_last[sid]:
            per_last[sid] = dl
    by_state, by_div = defaultdict(list), defaultdict(list)
    for sid, d, _tr, _s, _dl in obs:
        st = stores.get(sid) or {}
        if (st.get("state") or "").strip():
            by_state[st["state"].strip().upper()].append(d)
        if st.get("division"):
            by_div[st["division"]].append(d)
    state_days = {k: _median(v) for k, v in by_state.items() if len(v) >= MIN_GROUP}
    div_days = {k: _median(v) for k, v in by_div.items() if len(v) >= MIN_GROUP}
    network = _median([d for _s, d, *_ in obs]) if obs else DEFAULT_DAYS
    prof = {}
    for sid, st in stores.items():
        v = per.get(sid) or []
        state = (st.get("state") or "").strip().upper()
        if len(v) >= MIN_OWN:
            days, basis = _median(v), "store"
        elif state in state_days:
            days, basis = state_days[state], "state"
        elif st.get("division") in div_days:
            days, basis = div_days[st["division"]], "division"
        else:
            days, basis = network, "network"
        tr = per_tr.get(sid) or []
        wd = Counter(per_dl.get(sid) or [])
        prof[sid] = {
            "days": int(days), "basis": basis, "n": len(v),
            "p80": int(_p(v, .8)) if len(v) >= MIN_OWN else None,
            "fastest": min(v) if v else None, "slowest": max(v) if v else None,
            "transit": int(_median(tr)) if tr else None,
            "usual_delivery_day": WEEKDAYS[wd.most_common(1)[0][0]] if wd else None,
            "last_delivery": per_last.get(sid),
        }
    summary = {
        "deliveries": len(obs), "stores_with_history": sum(1 for v in per.values() if len(v) >= MIN_OWN),
        "network_days": int(network),
        "by_basis": dict(Counter(p["basis"] for p in prof.values())),
        "by_division": {k: {"days": int(_median(v)), "deliveries": len(v)} for k, v in sorted(by_div.items())},
        "by_state": {k: {"days": int(_median(v)), "deliveries": len(v)} for k, v in sorted(by_state.items())
                     if len(v) >= MIN_GROUP},
        "days_distribution": dict(sorted(Counter(p["days"] for p in prof.values()).items())),
    }
    return prof, summary


MANIFEST_SQL = """SELECT store_id, week_number, ship_date, delivery_date FROM delivery_manifest
                  WHERE delivery_date IS NOT NULL AND week_number IS NOT NULL"""
STORES_SQL = "SELECT store_id, state, division FROM stores"


def load_profiles(cur):
    """Profiles straight from the database (any cursor whose rows support ['col'])."""
    cur.execute(MANIFEST_SQL)
    rows = [dict(r) for r in cur.fetchall()]
    cur.execute(STORES_SQL)
    stores = {r["store_id"]: {"state": r["state"], "division": r["division"]} for r in cur.fetchall()}
    return build_profiles(rows, stores)
