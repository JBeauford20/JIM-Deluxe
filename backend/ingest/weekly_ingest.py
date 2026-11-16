"""
JIM weekly ingest — one command that feeds the engine its week of data.

Stages (each skippable):
  1. SALES     fetch the DTS CSV with Playwright (fetch_dts.py) — or, until that
               click-path is recorded, pick the newest CSV from the drop folder
               backend/ingest/drop/ — then load via load_sales.py.
  2. MANIFEST  load the newest week's Compiled sheet into delivery_manifest
               (in-transit + last-delivery truth for the DUE gate).
  3. VELOCITY  recompute store + group velocity (EWMA) on the fresh sales.

Availability is NOT ingested here: it arrives with the weekly greenhouse upload
through the app (or a future Aster pull) and is a different cadence.

Run:  python backend/ingest/weekly_ingest.py
      flags: --skip-sales --skip-manifest --skip-velocity --sales-file <csv>
Exit code 0 = every attempted stage succeeded; 1 = something failed (see log).
Scheduled by scripts/register_weekly_ingest.ps1 (Mondays 06:00).
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import Tee, LOG_DIR, load_env


def export_db_env():
    """Expose SUPABASE_DB_URL (pooler form — direct host is IPv6-only) so every
    child script (loaders, velocity engines via db_conn) can connect."""
    if os.environ.get("SUPABASE_DB_URL") and "pooler" in os.environ["SUPABASE_DB_URL"]:
        return
    dsn = load_env().get("SUPABASE_DB_URL", "")
    m = re.match(r"postgresql://postgres:([^@]+)@db\.([a-z0-9]+)\.supabase\.co:5432/postgres", dsn)
    if m:
        dsn = (f"postgresql://postgres.{m.group(2)}:{m.group(1)}"
               f"@aws-0-us-west-2.pooler.supabase.com:6543/postgres?sslmode=require")
    if dsn:
        os.environ["SUPABASE_DB_URL"] = dsn

DROP = HERE / "drop"
PY = sys.executable


def run(cmd: list[str], log) -> bool:
    log(f"$ {' '.join(str(c) for c in cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    for stream in (r.stdout, r.stderr):
        for line in (stream or "").splitlines():
            log(f"    {line}")
    return r.returncode == 0


def stage_sales(args, log) -> bool:
    csv_path = args.sales_file
    if not csv_path:
        # Try the automated fetch first
        fetch = subprocess.run([PY, str(HERE / "fetch_dts.py")],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
        if fetch.returncode == 0:
            csv_path = fetch.stdout.strip().splitlines()[-1]
            log(f"  fetched: {csv_path}")
        elif fetch.returncode == 2:
            DROP.mkdir(exist_ok=True)
            drops = sorted(DROP.glob("*.csv"), key=lambda p: p.stat().st_mtime)
            if not drops:
                log("  SALES: fetch not configured and drop folder empty "
                    f"({DROP}) — put the DTS CSV there (or record the fetch). SKIPPED.")
                return True  # not a failure: nothing to do
            csv_path = str(drops[-1])
            log(f"  using newest drop-folder CSV: {Path(csv_path).name}")
        else:
            log("  SALES FETCH FAILED:")
            for line in (fetch.stderr or "").splitlines()[-5:]:
                log(f"    {line}")
            return False
    ok = run([PY, str(HERE / "load_sales.py"), "--file", str(csv_path)], log)
    if ok and not args.sales_file:
        done = DROP / "loaded"
        done.mkdir(exist_ok=True)
        try:
            Path(csv_path).rename(done / Path(csv_path).name)
        except OSError:
            pass
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-sales", action="store_true")
    ap.add_argument("--skip-manifest", action="store_true")
    ap.add_argument("--skip-velocity", action="store_true")
    ap.add_argument("--sales-file")
    args = ap.parse_args()

    log = Tee("weekly")
    log("=" * 60)
    log("JIM WEEKLY INGEST")
    export_db_env()
    failures = []

    if not args.skip_sales:
        if not stage_sales(args, log):
            failures.append("sales")
    if not args.skip_manifest:
        if not run([PY, str(HERE / "load_manifest.py")], log):
            failures.append("manifest")
    if not args.skip_velocity and "sales" not in failures:
        workers = HERE.parent / "workers"
        for script in ("velocity_engine.py", "group_velocity_engine.py"):
            if not run([PY, str(workers / script)], log):
                failures.append(script)

    if failures:
        log(f"INGEST FAILED: {failures} — see {LOG_DIR}")
        sys.exit(1)
    log("INGEST COMPLETE")


if __name__ == "__main__":
    main()
