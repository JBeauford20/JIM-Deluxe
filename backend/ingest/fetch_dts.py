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
    """Monday-to-Sunday of the week before the run (Mon 6am run → prior full week).
    Aligned to sales_coverage week_start (Mondays) and to the manual feeds, which
    end on Sundays — so consecutive weekly runs tile with no gap or overlap."""
    t = today or date.today()
    this_monday = t - timedelta(days=t.weekday())
    return this_monday - timedelta(days=7), this_monday - timedelta(days=1)


def run_report(start: date | None = None, end: date | None = None) -> Path:
    """Fetch the Sell-Thru CSV for [start, end] (default: prior Mon–Sun week)."""
    env = load_env()
    url, user, pw = env.get("DTS_URL"), env.get("DTS_USERNAME"), env.get("DTS_PASSWORD")
    log = Tee("fetch_dts")
    if not (url and user and pw):
        log("DTS fetch not configured (DTS_URL/DTS_USERNAME/DTS_PASSWORD missing from jim.env)")
        sys.exit(2)

    from playwright.sync_api import sync_playwright

    if start is None or end is None:
        start, end = last_week_range()
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    log(f"Fetching DTS Sell Thru by Store/SKU (All) for {start} .. {end}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()

        # Login (recorded 2026-11-16; ASPX app at dts2.advgrower.com/LoadTrack)
        page.goto(url, timeout=60000)
        page.locator("#tbUsername").fill(user)
        page.locator("#tbPassword").fill(pw)
        page.get_by_role("button", name="Login").click()
        page.wait_for_load_state("domcontentloaded")
        if "Login" in page.url:
            raise SystemExit("DTS login failed — check DTS_USERNAME/DTS_PASSWORD in jim.env")

        # Navigate: Reports -> DTS Sales Store Item Sales -> Sell Thru by Store/SKU (All)
        page.get_by_role("link", name="Reports").click()
        page.get_by_text("DTS Sales Store Item Sales").click()
        page.get_by_role("link", name="Sell Thru by Store/SKU (All)").click()
        page.wait_for_load_state("domcontentloaded")

        # Set the date range EXPLICITLY. The page defaults to a wide window
        # (e.g. Oct 1 -> today), which would overlap previously loaded feeds.
        # The two Infragistics date pickers render as the only text inputs
        # holding M/D/YYYY values: first = start, second = end.
        date_boxes = page.eval_on_selector_all(
            "input[type='text']",
            "els => els.map((e,i) => ({i, v: e.value}))")
        date_idx = [b["i"] for b in date_boxes
                    if __import__("re").match(r"^\d{1,2}/\d{1,2}/\d{4}$", b["v"] or "")]
        if len(date_idx) < 2:
            raise SystemExit(f"Could not locate the two date inputs (found {date_boxes})")
        for idx, d in ((date_idx[0], start), (date_idx[1], end)):
            box = page.locator("input[type='text']").nth(idx)
            box.click()
            box.press("Control+a")
            box.type(f"{d.month}/{d.day}/{d.year}", delay=20)
            box.press("Tab")

        # Verify the pickers took the values before downloading
        after = page.eval_on_selector_all(
            "input[type='text']", "els => els.map(e => e.value)")
        want = {f"{start.month}/{start.day}/{start.year}", f"{end.month}/{end.day}/{end.year}"}
        if not want.issubset(set(after)):
            raise SystemExit(f"Date fields did not accept values: wanted {want}, page has {after}")

        # Download (bExportData = the 'Download Report' button)
        with page.expect_download(timeout=180000) as dl:
            page.locator("#bExportData").click()
        suggested = dl.value.suggested_filename or "report.csv"
        suffix = Path(suggested).suffix or ".csv"
        out = DOWNLOAD_DIR / f"dts_sellthru_{start}_{end}{suffix}"
        dl.value.save_as(out)
        browser.close()
        log(f"Downloaded {suggested} -> {out.name} ({out.stat().st_size:,} bytes)")
        return out


if __name__ == "__main__":
    # optional explicit window: fetch_dts.py 2026-10-05 2026-10-11 (backfills/repairs)
    if len(sys.argv) == 3:
        out = run_report(date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2]))
    else:
        out = run_report()
    print(out)
