"""Monitor ServiceNow incidents and mirror them into local PostgreSQL.

How it works: polls /api/now/table/incident for records with
sys_updated_on newer than the stored watermark (ORDERBY sys_updated_on),
upserts each row into snow_incident, advances the watermark. New incidents
are just rows whose sys_updated_on is fresh — they get picked up on the
next poll, so this both detects new incidents and tracks updates.

Usage:
  venv/bin/python sync_to_pg.py --once                 # single poll (cron-friendly)
  venv/bin/python sync_to_pg.py --once --since 2026-10-01   # bound the first backfill
  venv/bin/python sync_to_pg.py --loop --interval 60   # daemon: poll every 60s

Config (environment / .env — never hardcoded):
  SNOW_*            instance + auth, same as testServiceNow.py.
                    Basic mode (SNOW_AUTH=basic) is recommended: no token
                    expiry to manage; proven working during OAuth outages.
  PG_CONN           full libpq conninfo, e.g.
                    "host=localhost dbname=snowdb user=postgres". If empty,
                    built from PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD
                    (defaults: localhost/5432/snowdb/postgres/no password).
                    The database is created automatically on first run.

Tables created automatically (schema ensured on every run):
  snow_incident   one row per incident, PK sys_id (number UNIQUE).
  sync_state      single-row watermark: last max(sys_updated_on) synced.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import psycopg
import requests
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testServiceNow as snow

PG_CONN = os.getenv("PG_CONN", "")
PGHOST = os.getenv("PGHOST", "localhost")
PGPORT = os.getenv("PGPORT", "5432")
PGDATABASE = os.getenv("PGDATABASE", "snowdb")
PGUSER = os.getenv("PGUSER", "postgres")
PGPASSWORD = os.getenv("PGPASSWORD", "")

SYNC_FIELDS = (
    "sys_id,number,short_description,description,state,priority,urgency,"
    "impact,assigned_to,assignment_group,caller_id,category,subcategory,"
    "opened_at,sys_created_on,sys_updated_on,sys_created_by,sys_updated_by"
)
PAGE_SIZE = 100
WATERMARK_KEY = "incident_sys_updated_on"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS snow_incident (
    sys_id            TEXT PRIMARY KEY,
    number            TEXT UNIQUE,
    short_description TEXT,
    description       TEXT,
    state             TEXT,
    priority          TEXT,
    urgency           TEXT,
    impact            TEXT,
    assigned_to       TEXT,
    assignment_group  TEXT,
    caller_id         TEXT,
    category          TEXT,
    subcategory       TEXT,
    opened_at         TEXT,
    sys_created_on    TEXT,
    sys_updated_on    TEXT,
    sys_created_by    TEXT,
    sys_updated_by    TEXT,
    raw               JSONB,
    synced_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_snow_incident_number ON snow_incident(number);
CREATE INDEX IF NOT EXISTS idx_snow_incident_updated ON snow_incident(sys_updated_on);
CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

UPSERT_SQL = """
INSERT INTO snow_incident (
    sys_id, number, short_description, description, state, priority,
    urgency, impact, assigned_to, assignment_group, caller_id, category,
    subcategory, opened_at, sys_created_on, sys_updated_on,
    sys_created_by, sys_updated_by, raw, synced_at
) VALUES (
    %(sys_id)s, %(number)s, %(short_description)s, %(description)s, %(state)s,
    %(priority)s, %(urgency)s, %(impact)s, %(assigned_to)s, %(assignment_group)s,
    %(caller_id)s, %(category)s, %(subcategory)s, %(opened_at)s,
    %(sys_created_on)s, %(sys_updated_on)s, %(sys_created_by)s,
    %(sys_updated_by)s, %(raw)s, now()
)
ON CONFLICT (sys_id) DO UPDATE SET
    number = EXCLUDED.number,
    short_description = EXCLUDED.short_description,
    description = EXCLUDED.description,
    state = EXCLUDED.state,
    priority = EXCLUDED.priority,
    urgency = EXCLUDED.urgency,
    impact = EXCLUDED.impact,
    assigned_to = EXCLUDED.assigned_to,
    assignment_group = EXCLUDED.assignment_group,
    caller_id = EXCLUDED.caller_id,
    category = EXCLUDED.category,
    subcategory = EXCLUDED.subcategory,
    opened_at = EXCLUDED.opened_at,
    sys_created_on = EXCLUDED.sys_created_on,
    sys_updated_on = EXCLUDED.sys_updated_on,
    sys_created_by = EXCLUDED.sys_created_by,
    sys_updated_by = EXCLUDED.sys_updated_by,
    raw = EXCLUDED.raw,
    synced_at = now()
"""


def _pg_base_conninfo() -> str:
    if PG_CONN:
        return PG_CONN
    parts = [f"host={PGHOST}", f"port={PGPORT}", f"user={PGUSER}", f"dbname=postgres"]
    if PGPASSWORD:
        parts.append(f"password={PGPASSWORD}")
    return " ".join(parts)


def _pg_conninfo() -> str:
    if PG_CONN:
        return PG_CONN
    parts = [f"host={PGHOST}", f"port={PGPORT}", f"user={PGUSER}", f"dbname={PGDATABASE}"]
    if PGPASSWORD:
        parts.append(f"password={PGPASSWORD}")
    return " ".join(parts)


def ensure_database() -> None:
    """Create PGDATABASE if missing (connects to the maintenance db)."""
    with psycopg.connect(_pg_base_conninfo(), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (PGDATABASE,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{PGDATABASE}"')
            print(f"Created database {PGDATABASE}")


def ensure_schema(conn) -> None:
    conn.execute(SCHEMA_SQL)


def get_watermark(conn) -> str:
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key = %s", (WATERMARK_KEY,)
    ).fetchone()
    return row[0] if row else ""


def set_watermark(conn, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (WATERMARK_KEY, value),
    )


def _auth():
    """Authenticate per SNOW_AUTH. Returns (headers, basic_tuple, token_or_empty)."""
    if snow.AUTH_MODE == "basic":
        headers, basic = snow.make_auth("")
        return headers, basic, ""
    token = snow.get_access_token()
    headers, basic = snow.make_auth(token)
    return headers, basic, token


def fetch_updated_since(headers, basic, watermark: str):
    """Yield incident rows with sys_updated_on > watermark, oldest first.

    First run (no watermark): full backfill, paged. Later runs: incremental.
    """
    offset = 0
    query = f"sys_updated_on>{watermark}^ORDERBYsys_updated_on" if watermark else "ORDERBYsys_updated_on"
    while True:
        resp = requests.get(
            f"{snow.INSTANCE}/api/now/table/incident",
            headers=headers,
            auth=basic,
            params={
                "sysparm_query": query,
                "sysparm_fields": SYNC_FIELDS,
                "sysparm_limit": PAGE_SIZE,
                "sysparm_offset": offset,
                "sysparm_display_value": "false",  # raw values: stable for storage
            },
            timeout=snow.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json().get("result", [])
        if not rows:
            return
        yield from rows
        if len(rows) < PAGE_SIZE:
            return
        offset += PAGE_SIZE


def _ref(value) -> str:
    """Reference fields arrive as {link, value} — store the sys_id value."""
    if isinstance(value, dict):
        return str(value.get("value", ""))
    return "" if value is None else str(value)


def to_row(rec: dict) -> dict:
    get = lambda k: "" if rec.get(k) is None else str(rec.get(k))
    return {
        "sys_id": get("sys_id"),
        "number": get("number"),
        "short_description": get("short_description"),
        "description": get("description"),
        "state": get("state"),
        "priority": get("priority"),
        "urgency": get("urgency"),
        "impact": get("impact"),
        "assigned_to": _ref(rec.get("assigned_to")),
        "assignment_group": _ref(rec.get("assignment_group")),
        "caller_id": _ref(rec.get("caller_id")),
        "category": get("category"),
        "subcategory": get("subcategory"),
        "opened_at": get("opened_at"),
        "sys_created_on": get("sys_created_on"),
        "sys_updated_on": get("sys_updated_on"),
        "sys_created_by": get("sys_created_by"),
        "sys_updated_by": get("sys_updated_by"),
        "raw": json.dumps(rec),
    }


def sync_once(since: str = "") -> dict:
    """One poll cycle. Returns stats. `since` seeds the watermark (YYYY-MM-DD)."""
    ensure_database()
    headers, basic, _token = _auth()
    stats = {"fetched": 0, "upserted": 0, "max_updated": ""}
    with psycopg.connect(_pg_conninfo()) as conn:
        ensure_schema(conn)
        watermark = get_watermark(conn)
        if not watermark and since:
            watermark = since
            print(f"No watermark yet — starting from {since}")
        elif not watermark:
            print("No watermark yet — full backfill (paged).")
        with conn.cursor() as cur:
            for rec in fetch_updated_since(headers, basic, watermark):
                if not rec.get("sys_id"):
                    continue
                cur.execute(UPSERT_SQL, to_row(rec))
                stats["fetched"] += 1
                stats["upserted"] += 1
                updated = str(rec.get("sys_updated_on", ""))
                if updated > stats["max_updated"]:
                    stats["max_updated"] = updated
            if stats["max_updated"]:
                set_watermark(conn, stats["max_updated"])
        conn.commit()
    print(
        f"Sync done: fetched={stats['fetched']} upserted={stats['upserted']} "
        f"watermark={stats['max_updated'] or '(unchanged)'}"
    )
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Mirror ServiceNow incidents into PostgreSQL.")
    ap.add_argument("--once", action="store_true", help="single poll, then exit")
    ap.add_argument("--loop", action="store_true", help="poll forever every --interval seconds")
    ap.add_argument("--interval", type=int, default=60, help="seconds between polls in --loop (default 60)")
    ap.add_argument("--since", default="", help="seed watermark YYYY-MM-DD on first run (bounds backfill)")
    args = ap.parse_args(argv)

    if bool(args.once) == bool(args.loop):
        ap.error("pass exactly one of --once or --loop")
    if args.interval < 5:
        ap.error("--interval must be >= 5 seconds")

    print(f"SNOW -> PG sync | instance={snow.INSTANCE} auth={snow.AUTH_MODE} db={PGDATABASE}")
    if args.once:
        sync_once(since=args.since)
        return 0
    print(f"Loop mode: polling every {args.interval}s. Ctrl+C to stop.")
    while True:
        started = datetime.now(timezone.utc).strftime("%H:%M:%S")
        try:
            stats = sync_once()
            print(f"[{started}] fetched={stats['fetched']} upserted={stats['upserted']}")
        except Exception as exc:  # keep daemon alive across transient errors
            print(f"[{started}] poll failed (will retry): {str(exc)[:300]}")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
