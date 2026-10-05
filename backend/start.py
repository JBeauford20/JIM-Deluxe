"""
Start the JIM Deluxe backend.

Local dev:  reads jim.env from the JIM Deluxe folder
Railway:    env vars already injected by Railway — jim.env not needed
"""
import os, sys
from pathlib import Path

ENV_FILE = Path(__file__).parent.parent.parent / "JIM Deluxe" / "jim.env"

if ENV_FILE.exists():
    # Local development — load from file
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())  # don't overwrite Railway vars
    print(f"Local mode — loaded env from {ENV_FILE}")
else:
    # Railway (or any server) — vars already set in environment
    print("Production mode — using environment variables from Railway")

# Confirm the critical variable is present
db_url = os.environ.get('SUPABASE_DB_URL', '')
if not db_url:
    raise SystemExit("SUPABASE_DB_URL is not set. Add it in Railway Variables tab.")

print(f"DB: {db_url[:55]}...")
print("Dev auth header: X-Dev-Key: jim-dev-2026")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
