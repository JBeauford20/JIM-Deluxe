"""Start the JIM Deluxe backend — loads jim.env then launches uvicorn."""
import os, sys
from pathlib import Path

ENV_FILE = Path(__file__).parent.parent.parent / "JIM Deluxe" / "jim.env"

if not ENV_FILE.exists():
    raise SystemExit(f"jim.env not found at {ENV_FILE}")

for line in ENV_FILE.read_text().splitlines():
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        os.environ[k.strip()] = v.strip()

print(f"Loaded env from {ENV_FILE}")
print(f"DB: {os.environ.get('SUPABASE_DB_URL','')[:50]}...")
print("Starting JIM Deluxe API on http://localhost:8000")
print("Dev auth: add header  X-Dev-Key: jim-dev-2026")
print()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))  # Railway sets PORT automatically
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
