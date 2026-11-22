"""Home Depot store master — parse the workbook, plan the changes, apply them.

One implementation shared by the CLI (backend/ingest/load_store_master.py) and the Stores
tab upload (routers/store_status.py). ONLY the first sheet of the workbook is read: it is
the master list of how each store is handled. The other tabs are working copies.

Sheet columns (header names are matched case/spacing-insensitively):
  STORE_NUM | Status | Cross Dock? | SKU Restrictions | Merchandiser | Facility Name |
  District # | MKT_NBR | MKT_NM | DIV_NBM | Division | Merchant | Region Name |
  Street Address | City | State | Postal Code | BYO_NBR

What a load does to a store:
  HANDLING fields — the sheet is the authority, always written:
      master_status, sku_restriction, sku_rule, cross_dock, merchandiser, facility_name,
      street_address, postal_code, district, market_name, byo_nbr
  store_status —
      Status 'Do Not Ship'            -> do_not_ship (engine hard block)
      a store the master previously set do_not_ship and no longer lists as such -> active
      (a do_not_ship / paused / hold set by a person in the app is never lifted by a load)
  DESCRIPTIVE fields (state, city, region, division, market #, merchant) — filled only
      where the app has nothing. Where both have a value and they differ the app's value is
      kept and the difference is listed under `conflicts`; pass update_descriptive=True to
      take the sheet's value instead.
Nothing is deleted. Every applied load is recorded in store_master_loads with the complete
before/after change list (the rollback record).
"""
from __future__ import annotations

import io
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workers"))
import store_rules  # noqa: E402

HANDLING_FIELDS = ["master_status", "sku_restriction", "sku_rule", "cross_dock", "merchandiser",
                   "facility_name", "street_address", "postal_code", "district", "market_name",
                   "byo_nbr"]
DESCRIPTIVE_FIELDS = ["state", "city", "region_name", "market_number", "division", "region",
                      "merchant"]
KNOWN_STATUS = {"do not ship": "Do Not Ship", "with exceptions": "With Exceptions",
                "kurt weiss": "Kurt Weiss"}
NEEDS_RESTRICTION = {"With Exceptions", "Kurt Weiss"}
DIVISION = {"northern": ("North", "Northern"), "southern": ("South", "Southern"),
            "western": ("West", "Western"), "north": ("North", "Northern"),
            "south": ("South", "Southern"), "west": ("West", "Western")}
BLANKS = {"", "#n/a", "n/a", "na", "none", "null", "-", "#ref!", "#value!"}

HEADER_ALIASES = {
    "store_id": ["store_num", "store", "store #", "store number", "storenumber", "store no"],
    "master_status": ["status"],
    "cross_dock": ["cross dock?", "cross dock", "crossdock", "x-dock", "xdock"],
    "sku_restriction": ["sku restrictions", "sku restriction", "restrictions", "restriction"],
    "merchandiser": ["merchandiser", "merch partner", "merchandising partner"],
    "facility_name": ["facility name", "facility", "store name"],
    "district": ["district #", "district", "district number"],
    "market_number": ["mkt_nbr", "market number", "market #", "mkt nbr"],
    "market_name": ["mkt_nm", "market name", "mkt nm", "market"],
    "division": ["division"],
    "merchant": ["merchant"],
    "region_name": ["region name", "region"],
    "street_address": ["street address", "address"],
    "city": ["city"],
    "state": ["state", "st"],
    "postal_code": ["postal code", "zip", "zip code", "postal"],
    "byo_nbr": ["byo_nbr", "byo", "byo nbr", "byo #"],
}


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in BLANKS else s


def _int(v):
    v = _clean(v)
    if v is None:
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return int(f) if f.is_integer() else None


def _hkey(v):
    return " ".join(str(v or "").strip().lower().replace("_", " ").split()) if v is not None else ""


def parse_workbook(data: bytes, filename: str = "") -> dict:
    """First sheet -> {"sheet", "sheets_ignored", "rows": [...], "problems": [...]}"""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    raw = [r for r in ws.iter_rows(values_only=True)]
    out = {"file": filename, "sheet": ws.title, "sheets_ignored": [w.title for w in wb.worksheets[1:]],
           "rows": [], "problems": []}
    wb.close()
    alias = {_hkey(a): f for f, names in HEADER_ALIASES.items() for a in names}
    hdr_i, cols = None, {}
    for i, r in enumerate(raw[:15]):
        m = {}
        for j, v in enumerate(r or ()):
            f = alias.get(_hkey(v))
            if f and f not in m:
                m[f] = j
        if "store_id" in m and len(m) >= 3:
            hdr_i, cols = i, m
            break
    if hdr_i is None:
        out["problems"].append("No header row found on the first sheet (need a STORE_NUM column "
                               "plus Status / Cross Dock? / SKU Restrictions)")
        return out
    missing = [f for f in ("master_status", "cross_dock", "sku_restriction") if f not in cols]
    if missing:
        out["problems"].append("First sheet is missing handling column(s): " + ", ".join(missing)
                               + " — refusing to load (a missing column would clear every "
                                 "store's restriction)")
        return out
    out["columns"] = sorted(cols)

    def cell(r, f):
        j = cols.get(f)
        return r[j] if j is not None and j < len(r) else None

    seen = {}
    for n, r in enumerate(raw[hdr_i + 1:], hdr_i + 2):
        if not r or all(v is None or str(v).strip() == "" for v in r):
            continue
        sid = _int(cell(r, "store_id"))
        if sid is None or sid <= 0:
            out["problems"].append(f"row {n}: store number '{cell(r, 'store_id')}' is not a store")
            continue
        if sid in seen:
            out["problems"].append(f"row {n}: store {sid} is listed twice (first on row {seen[sid]})")
            continue
        seen[sid] = n
        st_raw = _clean(cell(r, "master_status"))
        status = KNOWN_STATUS.get((st_raw or "").lower()) if st_raw else None
        if st_raw and not status:
            out["problems"].append(f"row {n}: store {sid} has an unknown Status '{st_raw}' "
                                   f"(known: Do Not Ship, With Exceptions, Kurt Weiss)")
            continue
        restr = _clean(cell(r, "sku_restriction"))
        code = store_rules.rule_code_for_text(restr)
        if code and code.startswith("unknown:"):
            out["problems"].append(f"row {n}: store {sid} has an unrecognised SKU restriction "
                                   f"'{restr}' — define it in workers/store_rules.py first")
            continue
        if status in NEEDS_RESTRICTION and not code:
            out["problems"].append(f"row {n}: store {sid} is '{status}' but has no SKU restriction")
            continue
        cd = _clean(cell(r, "cross_dock"))
        if cd and cd.lower() in ("no", "n", "false", "0"):
            cd = None
        div = DIVISION.get((_clean(cell(r, "division")) or "").lower(), (None, None))
        dist = _clean(cell(r, "district"))
        if dist and dist.replace(".0", "").isdigit():
            dist = dist.replace(".0", "").zfill(4)
        st = _clean(cell(r, "state"))
        out["rows"].append({
            "store_id": sid, "row": n,
            "master_status": status, "sku_restriction": restr, "sku_rule": code, "cross_dock": cd,
            "merchandiser": _clean(cell(r, "merchandiser")),
            "facility_name": _clean(cell(r, "facility_name")),
            "street_address": _clean(cell(r, "street_address")),
            "postal_code": _clean(cell(r, "postal_code")),
            "district": dist, "market_name": _clean(cell(r, "market_name")),
            "byo_nbr": _int(cell(r, "byo_nbr")),
            "state": st.upper() if st else None, "city": _clean(cell(r, "city")),
            "region_name": _clean(cell(r, "region_name")),
            "market_number": _int(cell(r, "market_number")),
            "division": div[0], "region": div[1], "merchant": _clean(cell(r, "merchant")),
        })
    return out


def _same(a, b):
    na = "" if a is None else str(a).strip()
    nb = "" if b is None else str(b).strip()
    return na == nb


def _same_loose(a, b):
    return store_rules.norm_text(a) == store_rules.norm_text(b)


def _counts(stores):
    """Handling counts over an iterable of store dicts (before/after QA totals)."""
    c = {"do_not_ship": 0, "by_master_status": Counter(), "by_rule": Counter(), "by_cross_dock": Counter()}
    for s in stores:
        if (s.get("store_status") or "active") == "do_not_ship":
            c["do_not_ship"] += 1
        if s.get("master_status"):
            c["by_master_status"][s["master_status"]] += 1
        if s.get("sku_rule"):
            c["by_rule"][s["sku_rule"]] += 1
        if s.get("cross_dock"):
            c["by_cross_dock"][s["cross_dock"]] += 1
    return {k: (dict(v) if isinstance(v, Counter) else v) for k, v in c.items()}


def plan(parsed: dict, db_stores: dict, update_descriptive: bool = False) -> dict:
    """Compare the parsed sheet with the stores table -> the change plan + QA report.
    db_stores: {store_id: row dict}. Pure; nothing is written."""
    rows = parsed.get("rows") or []
    changes, conflicts, not_in_app = [], [], []
    status_changes = []
    after = {sid: dict(s) for sid, s in db_stores.items()}
    for r in rows:
        sid = r["store_id"]
        cur_ = db_stores.get(sid)
        if cur_ is None:
            not_in_app.append({"store_id": sid, "facility_name": r["facility_name"], "state": r["state"]})
            continue
        for f in HANDLING_FIELDS:
            if not _same(cur_.get(f), r[f]):
                changes.append({"store_id": sid, "field": f, "old": cur_.get(f), "new": r[f]})
                after[sid][f] = r[f]
        db_status = (cur_.get("store_status") or "active")
        if r["master_status"] == "Do Not Ship":
            if db_status != "do_not_ship":
                changes.append({"store_id": sid, "field": "store_status", "old": db_status, "new": "do_not_ship"})
                status_changes.append({"store_id": sid, "from": db_status, "to": "do_not_ship",
                                       "store": cur_.get("store_name") or r["facility_name"], "state": r["state"]})
                after[sid]["store_status"] = "do_not_ship"
        elif db_status == "do_not_ship" and cur_.get("master_status") == "Do Not Ship":
            changes.append({"store_id": sid, "field": "store_status", "old": "do_not_ship", "new": "active"})
            status_changes.append({"store_id": sid, "from": "do_not_ship", "to": "active",
                                   "store": cur_.get("store_name") or r["facility_name"], "state": r["state"]})
            after[sid]["store_status"] = "active"
        for f in DESCRIPTIVE_FIELDS:
            new, old = r.get(f), cur_.get(f)
            if new is None or _same(old, new):
                continue
            blank = old is None or str(old).strip() == ""
            if blank or (update_descriptive and not _same_loose(old, new)):
                changes.append({"store_id": sid, "field": f, "old": old, "new": new})
                after[sid][f] = new
            elif not _same_loose(old, new):
                conflicts.append({"store_id": sid, "field": f, "app": old, "sheet": new})
    sheet_ids = {r["store_id"] for r in rows}
    not_in_sheet = [{"store_id": sid, "store_name": s.get("store_name"), "state": s.get("state"),
                     "store_status": s.get("store_status"), "sku_rule": s.get("sku_rule")}
                    for sid, s in sorted(db_stores.items()) if sid not in sheet_ids]
    conf_by = Counter((c["field"], str(c["app"]).strip(), str(c["sheet"]).strip()) for c in conflicts)
    by_field = Counter(c["field"] for c in changes)
    return {
        "file": parsed.get("file"), "sheet": parsed.get("sheet"),
        "sheets_ignored": parsed.get("sheets_ignored") or [],
        "problems": parsed.get("problems") or [],
        "qa": {
            "input_rows": len(rows) + len([p for p in parsed.get("problems") or [] if p.startswith("row ")]),
            "rows_accepted": len(rows),
            "rows_rejected": len([p for p in parsed.get("problems") or [] if p.startswith("row ")]),
            "matched": len(rows) - len(not_in_app),
            "unmatched_sheet_not_in_app": len(not_in_app),
            "app_stores_not_in_sheet": len(not_in_sheet),
            "duplicate_keys": len([p for p in parsed.get("problems") or [] if "listed twice" in p]),
            "stores_changed": len({c["store_id"] for c in changes}),
            "field_changes": len(changes),
            "changes_by_field": dict(by_field),
            "before": _counts(db_stores.values()),
            "after": _counts(after.values()),
        },
        "status_changes": status_changes,
        "not_in_app": not_in_app, "not_in_sheet": not_in_sheet,
        "conflicts_summary": [{"field": f, "app": a, "sheet": s, "stores": n}
                              for (f, a, s), n in conf_by.most_common(60)],
        "conflicts_total": len(conflicts),
        "update_descriptive": update_descriptive,
        "changes": changes,
        "sheet_store_ids": sorted(sheet_ids & set(db_stores)),
    }


STORE_SELECT = ("store_id, store_name, city, state, region, region_name, market_number, division, "
                "merchant, store_status, status_note")


def load_db_stores(cur) -> dict:
    """{store_id: row} with whichever store-master columns exist (dict-style cursor)."""
    cur.execute("""SELECT column_name::text AS c FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'stores' AND column_name = ANY(%s)""",
                (HANDLING_FIELDS,))
    have = sorted(r["c"] for r in cur.fetchall())
    cur.execute(f"SELECT {STORE_SELECT}{''.join(', ' + c for c in have)} FROM stores")
    return {r["store_id"]: dict(r) for r in cur.fetchall()}, have


def migration_applied(have_cols) -> bool:
    return set(HANDLING_FIELDS) <= set(have_cols)


def apply(cur, pl: dict, by: str) -> dict:
    """Write a plan (same transaction as the caller's cursor). Returns counts."""
    if pl.get("problems"):
        raise ValueError("Refusing to apply a store master with problems: " + "; ".join(pl["problems"][:5]))
    per_store = {}
    for c in pl["changes"]:
        per_store.setdefault(c["store_id"], {})[c["field"]] = c["new"]
    src = pl.get("file") or "store master"
    for sid, fields in per_store.items():
        sets, vals = [], []
        for f, v in fields.items():
            sets.append(f"{f} = %s")
            vals.append(v)
        if "store_status" in fields:
            note = ("Store master: Do Not Ship" if fields["store_status"] == "do_not_ship"
                    else "Store master: no longer listed Do Not Ship")
            sets += ["status_note = %s", "status_updated_at = now()", "status_updated_by = %s"]
            vals += [f"{note} ({src})", f"store_master:{by}"]
        cur.execute(f"UPDATE stores SET {', '.join(sets)}, updated_at = now() WHERE store_id = %s",
                    (*vals, sid))
    if pl.get("sheet_store_ids"):
        cur.execute("UPDATE stores SET master_loaded_at = now(), master_source = %s WHERE store_id = ANY(%s)",
                    (src, pl["sheet_store_ids"]))
    summary = {k: pl[k] for k in ("qa", "status_changes", "not_in_app", "not_in_sheet",
                                  "conflicts_summary", "conflicts_total", "update_descriptive")}
    cur.execute("""INSERT INTO store_master_loads (loaded_by, source_file, sheet_name, rows_read,
                                                   stores_changed, summary, changes)
                   VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb) RETURNING id""",
                (by, src, pl.get("sheet"), pl["qa"]["rows_accepted"], len(per_store),
                 json.dumps(summary, default=str), json.dumps(pl["changes"], default=str)))
    row = cur.fetchone()
    load_id = row["id"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
    return {"load_id": str(load_id), "stores_changed": len(per_store), "field_changes": len(pl["changes"])}


def public_plan(pl: dict, sample: int = 200) -> dict:
    """The plan without the full change list (API response / CLI print)."""
    out = {k: v for k, v in pl.items() if k not in ("changes", "sheet_store_ids")}
    out["changes_sample"] = [c for c in pl["changes"] if c["field"] in
                             ("store_status", "sku_rule", "cross_dock", "master_status")][:sample]
    return out
