"""Shared DB connection helper for worker scripts."""
import os
import psycopg2
import psycopg2.extras

def get_conn():
    """Return a raw psycopg2 connection using SUPABASE_DB_URL env var.
    On Railway this is injected automatically. Locally, load jim.env before calling.
    """
    dsn = os.environ.get('SUPABASE_DB_URL', '')
    if not dsn:
        raise RuntimeError(
            'SUPABASE_DB_URL is not set. '            'On Railway: check Variables tab. '            'Locally: run start.py which loads jim.env first.'
        )
    return psycopg2.connect(dsn, connect_timeout=15)
