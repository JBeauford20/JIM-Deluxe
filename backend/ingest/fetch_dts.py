"""
Fetch the weekly DTS/AGS "Sell Thru by Store SKU Detail" CSV with Playwright.

STATUS: scaffold — the login/click path is NOT recorded yet. To finish it, Josh
runs ONE recorded session:

    python -m playwright codegen https://<the-DTS-report-site>

performs the normal weekly steps (log in -> open report -> set last-week range
-> export CSV), closes the window, and hands the generated script to Claude.
Those selectors get pasted into run_report() below and the pipeline is complete.

Credentials live in JIM Deluxe/jim.env (git-ignored):
    DTS_URL=...
    DTS_USERNAME=...
    DTS_PASSWORD=...

Exit codes: 0 = CSV downloaded (path printed on last line)
            2 = not configured yet (pipeline falls back to the drop folder)
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env, Tee

DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"


def last_week_range(today: date | None = None) -> tuple[date, date]:
    """Sunday-to-Saturday of the week before the run (Mon 6am run → prior full week)."""
    t = today or date.today()
    last_monday = t - timedelta(days=t.weekday() or 7)
    return last_monday - timedelta(days=1), last_monday + timedelta(days=5)


def run_report() -> Path:
    env = load_env()
    url, user, pw = env.get("DTS_URL"), env.get("DTS_USERNAME"), env.get("DTS_PASSWORD")
    log = Tee("fetch_dts")
    if not (url and user and pw):
        log("DTS fetch not configured (DTS_URL/DTS_USERNAME/DTS_PASSWORD missing from jim.env)")
        sys.exit(2)

    from playwright.sync_api import sync_playwright

    start, end = last_week_range()
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    log(f"Fetching DTS report for {start} .. {end}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.goto(url, timeout=60000)

        # ── RECORDED STEPS GO HERE ───────────────────────────────────────────
        # Paste the selectors from `playwright codegen` (login, navigate to the
        # Sell Thru by Store SKU Detail report, set the date range to start/end,
        # trigger the CSV export) and capture the download:
        #
        #   with page.expect_download() as dl:
        #       page.click(<export button selector>)
        #   out = DOWNLOAD_DIR / f"dts_sellthru_{start}_{end}.csv"
        #   dl.value.save_as(out)
        #   return out
        # ─────────────────────────────────────────────────────────────────────
        raise SystemExit(
            "fetch_dts.py: click path not recorded yet — run `python -m playwright codegen <DTS url>` "
            "once and give Claude the generated script.")


if __name__ == "__main__":
    out = run_report()
    print(out)
