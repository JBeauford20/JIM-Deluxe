"""
TPC IPR Engine browser smoke suite (Playwright + live API).

Run:  python tests/smoke_test.py
Env:  JIM_BASE_URL  (default: production Railway URL)
      JIM_TEST_EMAIL / JIM_TEST_PASSWORD  (default: read from "JIM Deluxe/jim.env")

What it covers
  S1  /api/health responds 200
  S2  unauthenticated boot shows the login overlay
  S3  wrong password shows an inline error (and does not log in)
  S4  real login lands with the user's email in the nav
  S5  every visible nav screen activates without JavaScript errors
  S6  role gating: all manager-gated controls are hidden for order_writer
  S7  change-password modal opens
  S8  no page errors (JS exceptions) anywhere in the session
  S9  API write round-trip on a DRAFT cart: add 1 tray -> verify -> remove it
      (skipped automatically when there is no editable draft run)

The write test touches only draft orders and removes exactly what it added.
Credentials live in the git-ignored jim.env, never in this file.
"""
import os
import re
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
BASE = os.environ.get("JIM_BASE_URL", "https://jim-deluxe-production.up.railway.app").rstrip("/")

# ── credentials ──────────────────────────────────────────────────────────────
def _load_env_file():
    env_path = REPO / "JIM Deluxe" / "jim.env"
    vals = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Z_]+)\s*=\s*(.+?)\s*$", line)
            if m:
                vals[m.group(1)] = m.group(2).strip('"').strip("'")
    return vals

_ENV = _load_env_file()
EMAIL = os.environ.get("JIM_TEST_EMAIL") or _ENV.get("JIM_TEST_EMAIL")
PW    = os.environ.get("JIM_TEST_PASSWORD") or _ENV.get("JIM_TEST_PASSWORD")

results = []   # (status, name, detail)

def record(status, name, detail=""):
    results.append((status, name, detail))
    pad = {"PASS": "PASS ", "FAIL": "FAIL ", "SKIP": "SKIP ", "WARN": "WARN "}[status]
    print(f"  {pad} {name}" + (f" — {detail}" if detail else ""))

def check(name, cond, detail=""):
    record("PASS" if cond else "FAIL", name, detail if not cond else "")

# ── S1: health ───────────────────────────────────────────────────────────────
def s1_health():
    r = httpx.get(f"{BASE}/api/health", timeout=30)
    check("S1 health endpoint 200", r.status_code == 200, f"got {r.status_code}")

# ── browser session ──────────────────────────────────────────────────────────
SCREENS = [
    ("week", "Week"), ("activeplans", "Active Plans"), ("plan", "Review Plan"),
    ("editor", "Order Editor"), ("configs", "Cart Configs"), ("stores", "Stores"),
    ("promos", "Promotions"), ("scenarios", "Scenarios"), ("history", "Order History"),
]
BENIGN = ("favicon", "net::", "Failed to load resource", "geocode")

def browser_suite():
    from playwright.sync_api import sync_playwright

    page_errors = []     # JS exceptions — always a failure
    console_errors = []  # console.error lines — warn unless app-origin

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

        # S2 login overlay on cold boot
        page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("#loginOverlay", state="visible", timeout=20000)
            record("PASS", "S2 login overlay shown when signed out")
        except Exception:
            record("FAIL", "S2 login overlay shown when signed out", "overlay never appeared")

        # S3 wrong password -> inline error
        page.fill("#lg-email", EMAIL or "nobody@theplantcompany.com")
        page.fill("#lg-pw", "definitely-wrong-password")
        page.click("#lg-btn")
        try:
            page.wait_for_function(
                "document.getElementById('lg-err') && document.getElementById('lg-err').textContent.trim().length > 0",
                timeout=15000)
            record("PASS", "S3 bad login shows error")
        except Exception:
            record("FAIL", "S3 bad login shows error", "#lg-err stayed empty")

        if not (EMAIL and PW):
            record("SKIP", "S4-S7", "no JIM_TEST_EMAIL/JIM_TEST_PASSWORD available")
        else:
            # S4 real login (doLogin reloads the page on success)
            page.fill("#lg-email", EMAIL)
            page.fill("#lg-pw", PW)
            page.click("#lg-btn")
            try:
                page.wait_for_function(
                    f"document.getElementById('nav-user') && document.getElementById('nav-user').textContent.includes('{EMAIL}')",
                    timeout=45000)
                record("PASS", "S4 login succeeds, nav shows user")
            except Exception:
                record("FAIL", "S4 login succeeds, nav shows user", "nav-user never showed email")

            # S5 every visible screen activates; attribute console errors per screen.
            # Since the v2 shell (Nov 20), the classic screens live inside the collapsed
            # "Classic" dropdown — open it before each click so the nav link is visible.
            for sid, label in SCREENS:
                before = len(page_errors)
                try:
                    link = page.locator(f"span.nl:has-text('{label}')").first
                    if not link.is_visible():
                        page.click("#xClassicBtn", timeout=10000)
                        page.wait_for_selector("#xCMenu:not([hidden])", timeout=5000)
                    page.click(f"span.nl:has-text('{label}')", timeout=10000)
                    page.wait_for_selector(f"#screen-{sid}.active", timeout=15000)
                    time.sleep(1.2)  # let the screen's fetches land
                    new_errs = page_errors[before:]
                    check(f"S5 screen '{label}' activates cleanly", not new_errs,
                          f"JS error: {new_errs[0][:160]}" if new_errs else "")
                except Exception as e:
                    record("FAIL", f"S5 screen '{label}' activates cleanly", str(e)[:160])

            # S6 role gating: order_writer must not see manager-gated controls
            gated = page.eval_on_selector_all(
                "[data-requires-role]",
                "els => els.map(e => ({role: e.getAttribute('data-requires-role'),"
                " visible: !!(e.offsetWidth || e.offsetHeight)}))")
            vis = [g for g in gated if g["visible"]]
            check("S6 manager-gated static controls hidden for order_writer",
                  len(gated) >= 4 and not vis,
                  f"{len(gated)} gated controls, visible: {vis}")
            itc = page.eval_on_selector("#it-clear-btn",
                  "e => !!(e.offsetWidth || e.offsetHeight)") if page.query_selector("#it-clear-btn") else False
            check("S6 in-transit Clear hidden for order_writer", not itc)

            # S7 change-password modal opens (the control sits in the v2 header's user chip;
            # target the onclick precisely — text selectors also match the wrapper span)
            try:
                page.click("span[onclick='openChangePw()']", timeout=10000)
                page.wait_for_selector("#chpwM", state="visible", timeout=10000)
                record("PASS", "S7 change-password modal opens")
                page.keyboard.press("Escape")
            except Exception as e:
                record("FAIL", "S7 change-password modal opens", str(e)[:160])

        browser.close()

    # S8 page errors + console noise
    check("S8 no JavaScript page errors in session", not page_errors,
          f"{len(page_errors)}: {page_errors[0][:200]}" if page_errors else "")
    noisy = [c for c in console_errors if not any(b in c for b in BENIGN)]
    if noisy:
        record("WARN", "S8 console.error noise (non-fatal)", f"{len(noisy)} e.g. {noisy[0][:160]}")

# ── S9: API write round-trip on a draft cart ────────────────────────────────
def s9_write_roundtrip():
    service_key = _ENV.get("SUPABASE_SERVICE_ROLE_KEY")
    db_url = _ENV.get("SUPABASE_DB_URL", "")
    ref_m = re.search(r"@db\.([a-z0-9]+)\.supabase\.co", db_url)
    if not (EMAIL and PW and service_key and ref_m):
        record("SKIP", "S9 write round-trip", "missing creds/keys in jim.env")
        return
    sb_url = f"https://{ref_m.group(1)}.supabase.co"

    tok = httpx.post(f"{sb_url}/auth/v1/token?grant_type=password",
                     headers={"apikey": service_key, "Content-Type": "application/json"},
                     json={"email": EMAIL, "password": PW}, timeout=30)
    if tok.status_code != 200:
        record("FAIL", "S9 write round-trip", f"token grant {tok.status_code}")
        return
    hdr = {"Authorization": f"Bearer {tok.json()['access_token']}"}

    # Find a draft cart with shelf room + a reusable supply line, directly in the DB.
    import psycopg2, psycopg2.extras
    pw_m = re.match(r"postgresql://postgres:([^@]+)@db\.([a-z0-9]+)\.supabase\.co", db_url)
    dsn = (f"postgresql://postgres.{pw_m.group(2)}:{pw_m.group(1)}"
           f"@aws-0-us-west-2.pooler.supabase.com:6543/postgres?sslmode=require")
    conn = psycopg2.connect(dsn, connect_timeout=15)
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("""
        SELECT oc.id AS cart_id, oc.version, oc.shelves_used,
               cs.sku_id, cs.kit_type, cs.availability_line_id
        FROM order_carts oc
        JOIN orders o  ON o.id = oc.order_id AND o.status = 'draft'
        JOIN recommendation_runs r ON r.id = o.run_id AND r.status = 'draft'
        JOIN cart_shelves cs ON cs.cart_id = oc.id AND cs.availability_line_id IS NOT NULL
        WHERE oc.shelves_used < COALESCE(oc.effective_max_shelves, oc.shelves_capacity, 5)
        ORDER BY o.created_at DESC, oc.id
        LIMIT 8
    """)
    rows = cur.fetchall()
    conn.close()
    if not rows:
        record("SKIP", "S9 write round-trip", "no editable draft cart found")
        return

    add = row = None
    for cand in rows:  # some supply lines are fully drawn — try a few
        resp = httpx.post(f"{BASE}/api/carts/{cand['cart_id']}/shelves", headers=hdr, timeout=60, json={
            "sku_id": cand["sku_id"], "tray_count": 1, "kit_type": cand["kit_type"],
            "availability_line_id": str(cand["availability_line_id"]), "version": cand["version"],
        })
        if resp.status_code == 400 and "availability" in resp.text.lower():
            continue
        add, row = resp, cand
        break
    if add is None:
        record("SKIP", "S9 write round-trip",
               f"all {len(rows)} candidate supply lines fully drawn")
        return
    cart_id = str(row["cart_id"])
    if add.status_code != 201:
        record("FAIL", "S9 add trays", f"{add.status_code} {add.text[:160]}")
        return
    pos = add.json()["shelf_position"]
    record("PASS", "S9 add trays (201)", "")

    got = httpx.get(f"{BASE}/api/carts/{cart_id}", headers=hdr, timeout=30)
    shelves = [s for s in got.json().get("shelves", []) if s["shelf_position"] == pos]
    check("S9 added shelf visible on cart", bool(shelves))

    new_version = got.json().get("version")
    rem = httpx.delete(f"{BASE}/api/carts/{cart_id}/shelves/{pos}",
                       headers=hdr, timeout=60,
                       params={"version": new_version} if new_version else None)
    check("S9 cleanup: remove added shelf", rem.status_code in (200, 204),
          f"{rem.status_code} {rem.text[:160]}")

    got2 = httpx.get(f"{BASE}/api/carts/{cart_id}", headers=hdr, timeout=30)
    check("S9 cart back to original shelf count",
          got2.json().get("shelves_used") == row["shelves_used"],
          f"before {row['shelves_used']}, after {got2.json().get('shelves_used')}")

# ── main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"JIM smoke suite → {BASE}\n")
    s1_health()
    browser_suite()
    s9_write_roundtrip()

    fails = [r for r in results if r[0] == "FAIL"]
    print(f"\n{'='*60}")
    print(f"TOTAL: {len(results)}  PASS: {sum(1 for r in results if r[0]=='PASS')}  "
          f"FAIL: {len(fails)}  SKIP: {sum(1 for r in results if r[0]=='SKIP')}  "
          f"WARN: {sum(1 for r in results if r[0]=='WARN')}")
    for s, n, d in fails:
        print(f"  FAILED: {n} — {d}")
    sys.exit(1 if fails else 0)
