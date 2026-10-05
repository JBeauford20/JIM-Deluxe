"""Auth router — validates Supabase JWTs, returns user + role."""
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import os, httpx

router  = APIRouter()
bearer  = HTTPBearer()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE", "")

DEV_KEY      = "jim-dev-2026"
DEV_KEY_HDR  = "x-dev-key"

async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(HTTPBearer(auto_error=False))
):
    """
    Auth with two paths:
      1. Dev bypass: X-Dev-Key: jim-dev-2026  → admin user, no Supabase call
      2. Production: Authorization: Bearer <supabase_jwt>
    """
    # ── Dev bypass (internal / local testing) ────────────────
    dev_key = request.headers.get(DEV_KEY_HDR, "")
    if dev_key == DEV_KEY:
        return {"id": "dev", "email": "dev@theplantcompany.com", "role": "admin"}

    # ── Production JWT path ───────────────────────────────────
    if not creds:
        raise HTTPException(status_code=401, detail="Missing auth token or dev key")

    token = creds.credentials
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_KEY}
        )
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_data = r.json()
    return {
        "id":    user_data.get("id"),
        "email": user_data.get("email"),
        "role":  user_data.get("user_metadata", {}).get("role", "viewer"),
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
