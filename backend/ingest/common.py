"""Shared helpers for the weekly ingest pipeline (DB connection, env, logging)."""
import os
import re
from datetime import datetime
from pathlib import Path

import psycopg2

REPO = Path(__file__).resolve().parents[2]
ENV_FILE = REPO / "JIM Deluxe" / "jim.env"
LOG_DIR = REPO / "backend" / "ingest" / "logs"


def load_env() -> dict:
    """jim.env key=value pairs (git-ignored file; holds all credentials)."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def get_conn():
    """Connect to Supabase. The direct db.<ref> host is IPv6-only from most
    Windows networks, so prefer the IPv4 pooler derived from the same URL."""
    dsn = os.environ.get("SUPABASE_DB_URL") or load_env().get("SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit("SUPABASE_DB_URL not set (env var or JIM Deluxe/jim.env)")
    m = re.match(r"postgresql://postgres:([^@]+)@db\.([a-z0-9]+)\.supabase\.co:5432/postgres", dsn)
    if m:
        pooler = (f"postgresql://postgres.{m.group(2)}:{m.group(1)}"
                  f"@aws-0-us-west-2.pooler.supabase.com:6543/postgres?sslmode=require")
        try:
            return psycopg2.connect(pooler, connect_timeout=15)
        except psycopg2.OperationalError:
            pass
    return psycopg2.connect(dsn, connect_timeout=15)


class Tee:
    """Print to stdout and append to the weekly ingest log file."""
    def __init__(self, name: str):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOG_DIR / f"ingest_{datetime.now():%Y%m%d}.log"
        self.name = name

    def __call__(self, *parts):
        line = " ".join(str(p) for p in parts)
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            # Windows consoles (cp1252) cannot print every character a child script logs.
            # A log line must never crash the ingest: 2026-11-22 the sales load committed and
            # the wrapper then died here, skipping the manifest / velocity / response stages.
            enc = getattr(__import__("sys").stdout, "encoding", None) or "ascii"
            print(line.encode(enc, "replace").decode(enc), flush=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%H:%M:%S}] [{self.name}] {line}\n")
