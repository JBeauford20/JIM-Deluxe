"""
JIM Deluxe — FastAPI Backend
Runs on Railway. All edits go through here as single transactions.
The browser never touches Supabase directly.
"""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pathlib import Path

from db import init_pool
from routers import availability, orders, carts, engine, stores, exports, auth, ai_explanations, in_transit, store_status, routing, config_editor, promotions, scenarios, packing, manifest, permanent_carts, concurrency, ipr, dsm

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    yield

app = FastAPI(
    title="TPC - IPR Engine API",
    description="Josh Inventory Management Deluxe — TPC order recommendation system",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS: restrict to known frontend origins.
# Set ALLOWED_ORIGINS env var on Railway (comma-separated) to lock this down.
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "https://jim-deluxe-production.up.railway.app")
_allowed_origins = [o.strip() for o in _raw_origins.split(",")] if _raw_origins != "*" else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(auth.router,         prefix="/api/auth",         tags=["Auth"])
app.include_router(stores.router,       prefix="/api/stores",       tags=["Stores"])
app.include_router(availability.router, prefix="/api/availability", tags=["Availability"])
app.include_router(orders.router,       prefix="/api/orders",       tags=["Orders"])
app.include_router(carts.router,        prefix="/api/carts",        tags=["Carts"])
app.include_router(engine.router,       prefix="/api/engine",       tags=["Engine"])
app.include_router(exports.router,         prefix="/api/exports",      tags=["Exports"])
app.include_router(ai_explanations.router, prefix="/api/ai",           tags=["AI"])
app.include_router(in_transit.router,      prefix="/api/in-transit",   tags=["InTransit"])
app.include_router(store_status.router,    prefix="/api/store-status", tags=["StoreStatus"])
app.include_router(routing.router,         prefix="/api/routing",      tags=["Routing"])
app.include_router(config_editor.router,   prefix="/api/config-editor", tags=["ConfigEditor"])
app.include_router(promotions.router,       prefix="/api/promotions",   tags=["Promotions"])
app.include_router(scenarios.router,        prefix="/api/scenarios",    tags=["Scenarios"])
app.include_router(packing.router,          prefix="/api/packing",      tags=["Packing"])
app.include_router(manifest.router,         prefix="/api/manifest",     tags=["Manifest"])
app.include_router(permanent_carts.router,   prefix="/api/permanent-carts", tags=["PermanentCarts"])
app.include_router(concurrency.router,        prefix="/api/concurrency",  tags=["Concurrency"])
app.include_router(ipr.router,                prefix="/api/ipr",          tags=["IPR v2"])
app.include_router(dsm.router,                prefix="/api/dsm",          tags=["DSM Portal"])

@app.get("/api/health")
def health():
    try:
        from db import get_db, cursor
        with get_db() as conn:
            cur = cursor(conn)
            cur.execute("SELECT 1")
        return {"status": "ok", "service": "TPC - IPR Engine API", "db": "connected"}
    except Exception as e:
        from fastapi import Response
        import json
        return Response(
            content=json.dumps({"status": "degraded", "service": "TPC - IPR Engine API", "db": str(e)}),
            status_code=503,
            media_type="application/json"
        )

@app.get("/dsm")
def serve_dsm_portal():
    """DSM web portal (spec §26) — mobile-friendly store lookup for District Sales Managers."""
    html_path = Path(__file__).parent.parent / "DSM_Portal.html"
    return FileResponse(
        path=str(html_path),
        media_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/")
def serve_frontend():
    """Serve the frontend HTML. Railway reads from disk on every deploy — no embed step needed."""
    html_path = Path(__file__).parent.parent / "JIM_Deluxe_Live.html"
    return FileResponse(
        path=str(html_path),
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        }
    )
