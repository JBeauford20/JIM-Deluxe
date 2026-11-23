# TPC IPR Engine

Weekly Home Depot order-recommendation system for The Plant Company. Scores ~2,000 HD
stores from 2+ years of sales history, turns a weekly greenhouse availability upload into
shelf-by-shelf cart plans, exchanges store lists with the external routing team, and
exports pick/ship files for the Aster ERP.

**Live:** https://jim-deluxe-production.up.railway.app (push to `main` = deploy)
**Stack:** FastAPI (`backend/`) + single-file frontend (`TPC_IPR_Engine.html`, served via
FileResponse — edit it, push, done; there is NO embed step) + Supabase Postgres
(project `ocfmqxtkhpjkkstylypk`, pooler connection) + Railway.

## Weekly cycle
1. **Monday 06:00** — scheduled task "JIM Weekly Ingest" (`scripts/run_weekly_ingest.ps1`):
   fetches the DTS Sell-Thru CSV (Playwright), loads sales/deliveries (overlap-guarded),
   loads the week's Compiled manifest, recomputes EWMA velocity. Failures auto-launch a
   Claude triage session (`/jim-ingest-triage`). Logs: `backend/ingest/logs/`.
2. Order writers upload availability, generate a plan (engine: Store Clock DUE gate →
   priority scoring → shelf-by-shelf cart build → CC consolidation → provisional loads),
   apply Scenarios (natural language → directives), and edit orders.
3. **Routing Exchange** (Review Plan screen): export the routing request → external team
   returns Store/Route/Drop/Ship/Del → import replaces provisional loads with real trucks
   and writes the delivery manifest → export truck-level Pack Plans.
4. Approve / finalize → Aster import CSV.

## Development
- Creds in `JIM Deluxe/jim.env` (git-ignored, never commit).
- Local run: `python backend/start.py` (loads jim.env; DB must use the pooler host —
  the direct db.* host is IPv6-only).
- Smoke suite: `python tests/smoke_test.py` — run it against prod after every deploy.
- Deps: `requirements.txt` = runtime (Railway), `requirements-dev.txt` = local tooling.
- Hygiene rules: never `git add .`; never edit UTF-8 content files with PowerShell
  Get-/Set-Content (mojibake); `.ps1` files stay ASCII-only.

## Key invariants
- Demand-driven only: no OnHand tracking by design.
- Pricing tiers: Collector/Specialty/Boutique/Rare Collectors must NEVER map down to
  Premium — flag for human review instead.
- `sales`/`deliveries` have no unique constraints — the loaders' feed_source + overlap
  guard are the only dedupe; never bypass them casually.
- Ledger-style tables (order_line_allocations, change_log) are append-only truth.
