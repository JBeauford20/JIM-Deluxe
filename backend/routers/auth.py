"""Auth router — validates Supabase JWTs, returns user + role."""
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import os, hmac, httpx

router  = APIRouter()
bearer  = HTTPBearer()

_PROJECT_REF = "ocfmqxtkhpjkkstylypk"
_DEFAULT_URL = f"https://{_PROJECT_REF}.supabase.co"
# Trust the env URL only if it points at the right project (the Railway var historically
# held a typo'd ref), otherwise fall back to the known-correct URL.
_env_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_URL = _env_url if _PROJECT_REF in _env_url else _DEFAULT_URL
# Accept either env name — the env file historically used SUPABASE_SERVICE_ROLE_KEY
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE", "")
# The anon key (public, client-safe) validates a user JWT via /auth/v1/user.
# Pinned to the project (not read from env) — the Railway env value was stale, which
# made validation fail. This key is safe to ship; it only identifies the project.
SUPABASE_ANON = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9jZm1xeHRraHBqa2tzdHlseXBrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTA4OTczNjgsImV4cCI6MjEwNjQ3MzM2OH0.vjCWPM9os4YFppHuHeFyGi8_Q4RW3-eSK4SO5pdnl1o")

# Dev bypass is DISABLED unless a DEV_KEY env var is explicitly set (leave it unset
# in production once the Supabase login is verified). No hardcoded key. — audit, Nov 2026
DEV_KEY      = os.environ.get("DEV_KEY", "")
DEV_KEY_HDR  = "x-dev-key"

async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(HTTPBearer(auto_error=False))
):
    """
    Auth with two paths:
      1. Dev bypass: X-Dev-Key: <DEV_KEY env>  → admin (only if DEV_KEY is set)
      2. Production: Authorization: Bearer <supabase_jwt>
    """
    # ── Dev bypass (only when DEV_KEY env var is set; constant-time compare) ──
    if DEV_KEY:
        dev_key = request.headers.get(DEV_KEY_HDR, "")
        if dev_key and hmac.compare_digest(dev_key, DEV_KEY):
            return {"id": "dev", "email": "dev@theplantcompany.com", "role": "admin"}

    # ── Production JWT path ───────────────────────────────────
    if not creds:
        raise HTTPException(status_code=401, detail="Missing auth token")

    token = creds.credentials
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON}
            )
    except Exception:
        raise HTTPException(status_code=401, detail="Could not verify token")
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_data = r.json()
    # Role comes from app_metadata (only the service role can set it). user_metadata
    # is user-editable and must NOT be trusted for authorization. — audit, Nov 2026
    role = (user_data.get("app_metadata") or {}).get("role", "viewer")
    return {
        "id":    user_data.get("id"),
        "email": user_data.get("email"),
        "role":  role,
    }

def require_role(*roles: str):
    async def check(user=Depends(get_current_user)):
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail=f"Requires role: {roles}")
        return user
    return check

@router.get("/me")
async def me(user=Depends(get_current_user)):
    return user
