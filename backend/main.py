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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten to Railway frontend URL in production
    allow_methods=["*"],
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

# Serve the frontend HTML at the root URL
# When deployed to Railway, hitting https://jim-deluxe.up.railway.app/ returns the app
HTML_FILE = Path(__file__).parent.parent / "JIM_Deluxe_Live.html"

@app.get("/")
def serve_frontend():
    if HTML_FILE.exists():
        return FileResponse(str(HTML_FILE), media_type="text/html")
    return {"error": "Frontend not found — ensure JIM_Deluxe_Live.html is in the project root"}
