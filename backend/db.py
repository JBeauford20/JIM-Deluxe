"""Database connection — single psycopg2 pool for all requests."""
import os
import psycopg2
import psycopg2.pool
import psycopg2.extras
from contextlib import contextmanager

_pool: psycopg2.pool.ThreadedConnectionPool | None = None

def init_pool():
    global _pool
    dsn = os.environ["SUPABASE_DB_URL"]
    # Supabase connection pooler (port 6543) requires sslmode and
    # fewer connections than direct (port 5432)
    extras = "?sslmode=require" if "pooler.supabase.com" in dsn and "sslmode" not in dsn else ""
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=5,
        dsn=dsn + extras,
        connect_timeout=15,
    )

@contextmanager
def get_db():
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
