"""Database connection — single psycopg2 pool for all requests."""
import os
import psycopg2
import psycopg2.pool
import psycopg2.extras
import threading
from contextlib import contextmanager

POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))
POOL_WAIT_SECONDS = 30
_pool: psycopg2.pool.ThreadedConnectionPool | None = None
# psycopg2's pool raises PoolError the instant every connection is out. A burst of parallel
# reads (the Exceptions tab loads ~10 decision kinds at once) or a running engine job plus two
# users then 500s. Gate checkouts with a semaphore so callers queue briefly instead.
_slots = threading.BoundedSemaphore(POOL_MAX)

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
        maxconn=POOL_MAX,
        dsn=dsn + extras,
        connect_timeout=15,
    )

@contextmanager
def get_db():
    if _pool is None:
        raise RuntimeError("Database pool not initialised — check SUPABASE_DB_URL")
    if not _slots.acquire(timeout=POOL_WAIT_SECONDS):
        raise RuntimeError(f"Database busy: no connection free after {POOL_WAIT_SECONDS}s")
    try:
        conn = _pool.getconn()
    except Exception:
        _slots.release()
        raise
    try:
        conn.autocommit = False
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)
        _slots.release()

def cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
