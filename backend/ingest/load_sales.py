"""
Load a DTS "Sell Thru by Store SKU Detail" CSV into sales/deliveries/sales_coverage.

Repo version of the proven local loader (JIM Deluxe/load_sales_csv.py), adapted for
the automated weekly pipeline: pooler-aware connection, auto feed naming
(dts_wk{iso-year}{week}), overlap guard, and a sales_import_log row per run.

sales/deliveries have NO unique constraint, so double-counting is prevented here:
the loader refuses to run if the file's sale-date range overlaps dates already
present from another feed, unless --delete-feeds names the feed(s) to replace.

Run:  python backend/ingest/load_sales.py --file "path\\to\\report.csv"
      add --inspect for a no-write preview, --feed-source to override the name.
"""
import argparse
import csv
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import get_conn, Tee

COLS = {
    "store": "StoreIDNum", "sku": "SKU", "name": "ItemName",
    "sales_qty": "SalesQty", "sale_date": "LastSalesDate",
    "deliv_qty": "DeliveryQty", "deliv_date": "LastDeliveryDate",
}
# Rich columns discovered 2026-11-17 (report carries 44; all optional — the loader
# still accepts a minimal 7-column file). SalesAmount = RETAIL dollars; the $900K
# weekly objective is retail register sales (Josh, 2026-11-17).
RICH = {
    "sales_amt": "SalesAmount", "onhand_qty": "OnHandQty", "onhand_amt": "OnHandAmount",
    "dump_qty": "DumpQty", "dump_amt": "DumpAmount",
    "py_qty": "PrevYrQty", "py_amt": "PrevYrAmt",
    "wos": "WOS", "days_since": "DaysSinceLastDeliv",
}


def parse_date(s):
    if not s or not str(s).strip():
        return None
    s = str(s).strip()
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def safe_int(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def safe_num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def read_csv(path: Path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rdr = csv.DictReader(fh)
        missing = [c for c in COLS.values() if c not in (rdr.fieldnames or [])]
        if missing:
            raise SystemExit(f"Missing expected columns: {missing}\nFound: {rdr.fieldnames}")
        return list(rdr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--feed-source")
    ap.add_argument("--report-date")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--delete-feeds", default="",
                    help="comma-separated feed_source values to delete before load")
    ap.add_argument("--allow-overlap", action="store_true",
                    help="load even when the date range overlaps existing feeds")
    args = ap.parse_args()

    log = Tee("sales")
    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"File not found: {path}")
    report_date = (datetime.strptime(args.report_date, "%Y-%m-%d").date()
                   if args.report_date else date.today())

    log(f"Reading {path.name} ...")
    rows = read_csv(path)

    has_rich = RICH["sales_amt"] in (rows[0].keys() if rows else [])
    sales, delivs, new_skus, snaps = [], [], {}, []
    sd_list, dd_list = [], []
    skip_bad = skip_noact = 0
    for r in rows:
        store = safe_int(r[COLS["store"]])
        sku = safe_int(r[COLS["sku"]])
        sq = safe_int(r[COLS["sales_qty"]]) or 0
        dq = safe_int(r[COLS["deliv_qty"]]) or 0
        if not store or not sku:
            skip_bad += 1
            continue
        amt = safe_num(r.get(RICH["sales_amt"])) if has_rich else None
        # snapshot rows capture EVERY store×SKU line (incl. zero-sales rows —
        # on-hand/dumps/prior-year matter even when nothing sold this period)
        if has_rich:
            snaps.append((store, sku, sq,
                          amt,
                          round(amt / sq, 4) if amt and sq else None,
                          safe_int(r.get(RICH["onhand_qty"])),
                          safe_num(r.get(RICH["onhand_amt"])),
                          safe_int(r.get(RICH["dump_qty"])),
                          safe_num(r.get(RICH["dump_amt"])),
                          safe_int(r.get(RICH["py_qty"])),
                          safe_num(r.get(RICH["py_amt"])),
                          safe_num(r.get(RICH["wos"])),
                          safe_int(r.get(RICH["days_since"]))))
        if sq == 0 and dq == 0:
            skip_noact += 1
            continue
        if sku not in new_skus:
            new_skus[sku] = (str(r[COLS["name"]]).strip() if r[COLS["name"]] else None)
        if sq > 0:
            sd = parse_date(r[COLS["sale_date"]]) or report_date
            sales.append((store, sku, sd, sq, amt))
            sd_list.append(sd)
        if dq > 0:
            dd = parse_date(r[COLS["deliv_date"]]) or report_date
            delivs.append((store, sku, dd, dq))
            dd_list.append(dd)

    rng = (min(sd_list) if sd_list else None, max(sd_list) if sd_list else None)
    retail_total = sum(s[4] or 0 for s in sales)
    log(f"  rows {len(rows):,} | sales {len(sales):,} ({len({s[0] for s in sales}):,} stores)"
        f" | deliveries {len(delivs):,} | skus {len(new_skus):,}"
        f" | skipped bad {skip_bad:,} / no-activity {skip_noact:,}")
    log(f"  sale dates {rng[0]} -> {rng[1]}"
        + (f" | retail ${retail_total:,.0f}" if has_rich else " | (minimal file: no dollar columns)")
        + (f" | snapshot rows {len(snaps):,}" if snaps else ""))
    for w, n in sorted(Counter((s[2] - timedelta(days=s[2].weekday())) for s in sales).items()):
        log(f"    week {w}: {n:,}")

    if args.inspect:
        log("  INSPECT ONLY — no DB changes")
        return

    iso = (rng[1] or report_date).isocalendar()
    feed = args.feed_source or f"dts_wk{iso[0]}{iso[1]:02d}"

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Idempotent re-run guard: the overlap check below excludes our OWN feed name,
    # and sales/deliveries have no unique constraints — re-running the same weekly
    # file would silently double-count. Replace our own prior rows instead.
    cur.execute("SELECT COUNT(*) AS n FROM sales WHERE feed_source = %s", (feed,))
    if cur.fetchone()["n"]:
        cur.execute("DELETE FROM sales WHERE feed_source = %s", (feed,))
        sdel = cur.rowcount
        cur.execute("DELETE FROM deliveries WHERE feed_source = %s", (feed,))
        log(f"  re-run detected: replacing existing feed '{feed}' "
            f"({sdel:,} sales + {cur.rowcount:,} deliveries removed)")

    # Overlap guard: refuse to double-count a window that already has data.
    if rng[0] and not args.allow_overlap:
        cur.execute("""SELECT feed_source, COUNT(*) n FROM sales
                       WHERE sale_date BETWEEN %s AND %s AND feed_source <> %s
                       GROUP BY feed_source""", (rng[0], rng[1], feed))
        overlap = cur.fetchall()
        deletes = {f.strip() for f in args.delete_feeds.split(",") if f.strip()}
        blocking = [o for o in overlap if o["feed_source"] not in deletes]
        if blocking:
            names = ", ".join(f"{o['feed_source']}({o['n']:,})" for o in blocking)
            conn.close()
            raise SystemExit(
                f"OVERLAP: existing sales in {rng[0]}..{rng[1]} from {names}. "
                f"Re-run with --delete-feeds to replace them, or --allow-overlap.")

    cur.execute("SELECT store_id FROM stores")
    valid_stores = {r["store_id"] for r in cur.fetchall()}
    cur.execute("SELECT sku_id FROM skus")
    valid_skus = {r["sku_id"] for r in cur.fetchall()}

    seed = [(sid, desc, True) for sid, desc in new_skus.items() if sid not in valid_skus]
    if seed:
        log(f"  seeding {len(seed)} new SKUs (description only)")
        psycopg2.extras.execute_batch(cur,
            "INSERT INTO skus (sku_id, description, active) VALUES (%s,%s,%s) ON CONFLICT (sku_id) DO NOTHING",
            seed, page_size=500)
        cur.execute("SELECT sku_id FROM skus")
        valid_skus = {r["sku_id"] for r in cur.fetchall()}

    for f in (x.strip() for x in args.delete_feeds.split(",") if x.strip()):
        cur.execute("DELETE FROM sales WHERE feed_source=%s", (f,))
        sdel = cur.rowcount
        cur.execute("DELETE FROM deliveries WHERE feed_source=%s", (f,))
        log(f"  deleted feed '{f}': {sdel:,} sales + {cur.rowcount:,} deliveries")

    sclean = [(s[0], s[1], s[2], s[3], s[4], feed) for s in sales if s[0] in valid_stores and s[1] in valid_skus]
    dclean = [(d[0], d[1], d[2], d[3], feed) for d in delivs if d[0] in valid_stores and d[1] in valid_skus]

    # Cross-feed delivery dedupe (2026-11-20): weekly DTS files repeat delivery rows from other
    # weeks (a delivery dated 11/5 showed up in the wk42, wk44 and wk45 files — 25.5K duplicate
    # units since 10/1). The sales overlap guard tiles by SALE date and never sees this. First
    # report wins: skip any delivery whose (store, sku, delivery_date) another feed already loaded.
    if dclean:
        lo, hi = min(d[2] for d in dclean), max(d[2] for d in dclean)
        cur.execute("""SELECT DISTINCT store_id, sku_id, delivery_date FROM deliveries
                       WHERE delivery_date BETWEEN %s AND %s AND feed_source <> %s""",
                    (lo, hi, feed))
        seen = {(r["store_id"], r["sku_id"], r["delivery_date"]) for r in cur.fetchall()}
        before = len(dclean)
        dclean = [d for d in dclean if (d[0], d[1], d[2]) not in seen]
        if before - len(dclean):
            log(f"  cross-feed dedupe: skipped {before - len(dclean):,} delivery rows already "
                f"loaded by another feed")

    log(f"  inserting {len(sclean):,} sales + {len(dclean):,} deliveries (feed={feed})")
    psycopg2.extras.execute_batch(cur,
        "INSERT INTO sales (store_id, sku_id, sale_date, units_sold, dollars, feed_source) VALUES (%s,%s,%s,%s,%s,%s)",
        sclean, page_size=500)
    psycopg2.extras.execute_batch(cur,
        "INSERT INTO deliveries (store_id, sku_id, delivery_date, units_delivered, feed_source) VALUES (%s,%s,%s,%s,%s)",
        dclean, page_size=500)

    cov = {}
    for s in sclean:
        cov[s[0]] = s[2] - timedelta(days=s[2].weekday())
    psycopg2.extras.execute_batch(cur, """
        INSERT INTO sales_coverage (store_id, week_start, data_received, feed_source)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT (store_id, week_start) DO UPDATE SET
            data_received=EXCLUDED.data_received, feed_source=EXCLUDED.feed_source
    """, [(sid, wk, True, feed) for sid, wk in cov.items()], page_size=500)

    # Rich snapshot (on-hand, dumps, prior-year, WOS) — one row per store×SKU per pull.
    # Period = the fetch window when the filename carries it, else the sale-date range.
    if snaps:
        m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})", path.name)
        p_start = datetime.strptime(m.group(1), "%Y-%m-%d").date() if m else rng[0]
        p_end = datetime.strptime(m.group(2), "%Y-%m-%d").date() if m else (rng[1] or report_date)
        cur.execute("DELETE FROM dts_weekly_snapshot WHERE feed_source = %s", (feed,))
        if cur.rowcount:
            log(f"  replaced {cur.rowcount:,} prior snapshot rows for feed '{feed}'")
        psycopg2.extras.execute_batch(cur, """
            INSERT INTO dts_weekly_snapshot
                (store_id, sku_id, sales_qty, sales_amount, realized_price,
                 onhand_qty, onhand_amount, dump_qty, dump_amount,
                 prev_yr_qty, prev_yr_amount, weeks_of_supply, days_since_last_delivery,
                 period_start, period_end, feed_source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, [s + (p_start, p_end, feed) for s in snaps], page_size=500)
        log(f"  snapshot: {len(snaps):,} store×SKU rows for {p_start} -> {p_end}")

    cur.execute("""
        INSERT INTO sales_import_log
            (id, imported_at, source_filename, feed_source, date_range_start, date_range_end,
             rows_attempted, rows_inserted, rows_skipped, rows_errored, duplicate_risk, status, notes)
        VALUES (gen_random_uuid(), now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, 'loaded', %s)
    """, (path.name, feed, rng[0], rng[1], len(rows), len(sclean) + len(dclean),
          skip_noact, skip_bad, bool(args.allow_overlap),
          f"weekly ingest; deliveries={len(dclean)}"))
    conn.commit()

    cur.execute("SELECT MAX(sale_date) FROM sales")
    log(f"  COMMITTED — max sale_date now {cur.fetchone()[0]}")
    conn.close()


if __name__ == "__main__":
    main()
