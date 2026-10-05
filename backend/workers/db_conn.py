"""Shared DB connection helper for worker scripts — uses env vars, not jim.env."""
import os
import psycopg2
import psycopg2.extras

def get_conn():
    dsn = os.environ.get('SUPABASE_DB_URL', '')
    if not dsn:
        # Fallback: try loading jim.env for local dev
        from pathlib import Path
        env_file = Path(__file__).parent.parent.parent.parent / "JIM Deluxe" / "jim.env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())
            dsn = os.environ.get('SUPABASE_DB_URL', '')
    if not dsn:
        raise RuntimeError('SUPABASE_DB_URL not set')
    return psycopg2.connect(dsn, connect_timeout=15)
