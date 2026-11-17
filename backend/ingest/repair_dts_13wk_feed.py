"""
One-time repair (2026-11-17): replace the time-smeared 'dts_13wk_nov09' feed.

That feed was a 13-week CUMULATIVE export loaded as one row per store x SKU stamped on its
last-sale date: ~13 weeks of units piled onto single days (Oct 5–26 looked near-empty, Nov
weeks ~2x), and its window (from ~Aug 10) double-counted Aug 10–Oct 1 already covered by the
daily ledger. Every velocity window (v1 EWMA, Store Clock, v2 rates) read it wrong.

Procedure (safe order):
  1. back up affected feed rows into *_backup_20261117 tables (reversible)
  2. fetch ALL clean windows from DTS first — any failure aborts with NO DB changes
  3. delete the bad feeds (sales + deliveries)
  4. load each clean weekly window (also backfills retail dollars + on-hand snapshot)
  5. print week-by-week units before/after
"""
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import get_conn
from fetch_dts import run_report

BAD_FEEDS = ("dts_13wk_nov09", "dts_2026-11-10_to_11-15")
WINDOWS = [(date(2026, 10, 2), date(2026, 10, 4), "dts_wk202640p")] + [
    (date.fromordinal(date(2026, 10, 5).toordinal() + 7 * i),
     date.fromordinal(date(2026, 10, 11).toordinal() + 7 * i), None) for i in range(6)]


def weekly(cur):
    cur.execute("""SELECT date_trunc('week', sale_date)::date, SUM(units_sold)
                   FROM sales WHERE sale_date >= '2026-08-03' GROUP BY 1 ORDER BY 1""")
    return dict(cur.fetchall())


def main():
    conn = get_conn()
    cur = conn.cursor()
    before = weekly(cur)
    conn.commit()   # SET TRANSACTION below must open a fresh transaction

    # 1. backup (idempotent)
    cur.execute("SET TRANSACTION READ WRITE")
    cur.execute("""CREATE TABLE IF NOT EXISTS sales_backup_20261117 AS
                   SELECT * FROM sales WHERE feed_source = ANY(%s)""", (list(BAD_FEEDS),))
    cur.execute("""CREATE TABLE IF NOT EXISTS deliveries_backup_20261117 AS
                   SELECT * FROM deliveries WHERE feed_source = ANY(%s)""", (list(BAD_FEEDS),))
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM sales_backup_20261117")
    print(f"backup: {cur.fetchone()[0]:,} sales rows saved")

    # 2. fetch everything first
    files = []
    for s, e, feed in WINDOWS:
        print(f"fetching {s} .. {e}")
        f = run_report(s, e)
        iso = e.isocalendar()
        files.append((f, feed or f"dts_wk{iso[0]}{iso[1]:02d}", e))
    print(f"all {len(files)} windows fetched — swapping feeds")

    # 3. delete bad feeds
    cur.execute("SET TRANSACTION READ WRITE")
    cur.execute("DELETE FROM sales WHERE feed_source = ANY(%s)", (list(BAD_FEEDS),))
    print(f"deleted {cur.rowcount:,} smeared sales rows")
    cur.execute("DELETE FROM deliveries WHERE feed_source = ANY(%s)", (list(BAD_FEEDS),))
    print(f"deleted {cur.rowcount:,} smeared delivery rows")
    conn.commit()

    # 4. load clean windows
    for f, feed, e in files:
        r = subprocess.run([sys.executable, str(HERE / "load_sales.py"), "--file", str(f),
                            "--feed-source", feed, "--report-date", e.isoformat()],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        tail = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()][-3:]
        print(f"load {feed}: exit {r.returncode} | " + " / ".join(tail))
        if r.returncode != 0:
            print("STOPPING — fix and re-run the remaining windows; backup tables hold the old rows")
            sys.exit(1)

    # 5. reconcile
    after = weekly(cur)
    print("\nweek        before      after")
    for wk in sorted(set(before) | set(after)):
        print(f"{wk}  {before.get(wk, 0):>9,}  {after.get(wk, 0):>9,}")
    conn.close()


if __name__ == "__main__":
    main()
