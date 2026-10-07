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
from routers import availability, orders, carts, engine, stores, exports, auth

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    yield

app = FastAPI(
    title="JIM Deluxe API",
    description="Josh Inventory Management Deluxe — TPC order recommendation system",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS: restrict to known frontend origins.
# Set ALLOWED_ORIGINS env var on Railway (comma-separated) to lock this down.
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "*")
_allowed_origins = [o.strip() for o in _raw_origins.split(",")] if _raw_origins != "*" else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(auth.router,         prefix="/api/auth",         tags=["Auth"])
app.include_router(stores.router,       prefix="/api/stores",       tags=["Stores"])
app.include_router(availability.router, prefix="/api/availability", tags=["Availability"])
app.include_router(orders.router,       prefix="/api/orders",       tags=["Orders"])
app.include_router(carts.router,        prefix="/api/carts",        tags=["Carts"])
app.include_router(engine.router,       prefix="/api/engine",       tags=["Engine"])
app.include_router(exports.router,      prefix="/api/exports",      tags=["Exports"])

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "JIM Deluxe API"}

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
