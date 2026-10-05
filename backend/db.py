"""Database connection — single psycopg2 pool for all requests."""
import os
import psycopg2
import psycopg2.pool
import psycopg2.extras
from contextlib import contextmanager

_pool: psycopg2.pool.ThreadedConnectionPool | None = None

def init_pool():
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=10,
        dsn=os.environ["SUPABASE_DB_URL"],
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
