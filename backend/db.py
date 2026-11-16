"""Database connection — single psycopg2 pool for all requests."""
import os
import psycopg2
import psycopg2.pool
import psycopg2.extras
from contextlib import contextmanager

_pool: psycopg2.pool.ThreadedConnectionPool | None = None

def init_pool():
    global _pool
    dsn = os.environ.get("SUPABASE_DB_URL", "NOT SET")
    # Log only the host/port — any raw slice of the DSN can include password chars
    try:
        from urllib.parse import urlsplit
        p = urlsplit(dsn)
        safe = f"{p.hostname}:{p.port or 5432}/{(p.path or '').lstrip('/')}"
    except Exception:
        safe = "(unparseable DSN)"
    print(f"[DB] Connecting to: {safe}", flush=True)
    print(f"[DB] Using pooler: {'pooler.supabase.com' in dsn}", flush=True)
    extras = "?sslmode=require" if "pooler.supabase.com" in dsn and "sslmode" not in dsn else ""
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=5,
        dsn=dsn + extras,
        connect_timeout=15,
    )

@contextmanager
def get_db():
    if _pool is None:
        raise RuntimeError("Database pool not initialised — check SUPABASE_DB_URL")
    conn = _pool.getconn()
    try:
        conn.autocommit = False
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)

def cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
