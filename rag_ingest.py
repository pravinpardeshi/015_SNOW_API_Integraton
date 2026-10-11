"""Build the RAG vector DB from CLOSED custom-table tickets.

Corpus: u_snow_incident_table rows whose u_status is in RAG_CLOSED_STATUSES
(default "Resolved" — verified live: 488/503 rows). Each ticket contributes
summary + description + tech context as the retrievable text, with its
u_root_cause_analysis + u_solution stored as the historic answer.

Storage: local PostgreSQL + pgvector (CREATE EXTENSION vector).
Embeddings: local Ollama (OLLAMA_EMBED_MODEL, default nomic-embed-text,
768 dims — verified). No API keys, no GPU needed.

Usage:
  venv/bin/python rag_ingest.py --once            # full rebuild of closed tickets
  venv/bin/python rag_ingest.py --once --limit 20 # smoke test on 20 tickets
  venv/bin/python rag_ingest.py --stats           # corpus stats only

Re-running is idempotent (upsert by ticket sys_id); re-embed only changed
rows with --changed-only (compares sys_updated_on).
"""

import argparse
import json
import os
import sys

import psycopg
import requests
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testServiceNow as snow
from sync_to_pg import ensure_database, _pg_conninfo

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
RAG_TABLE = os.getenv("SNOW_CUSTOM_TABLE", "u_snow_incident_table")
CLOSED_STATUSES = [
    s.strip() for s in os.getenv("RAG_CLOSED_STATUSES", "Resolved").split(",") if s.strip()
]
EMBED_BATCH = int(os.getenv("RAG_EMBED_BATCH", "16"))
EMBED_TIMEOUT = int(os.getenv("RAG_EMBED_TIMEOUT", "120"))

RAG_FIELDS = (
    "sys_id,u_ticket_id,u_summary,u_description,u_solution,u_root_cause_analysis,"
    "u_status,u_category,u_subcategory,u_priority,u_severity,u_environment,"
    "u_technology_stack,u_affected_component,sys_updated_on"
)

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS rag_ticket (
    sys_id      TEXT PRIMARY KEY,
    ticket_id   TEXT,
    status      TEXT,
    summary     TEXT,
    text        TEXT NOT NULL,
    solution    TEXT,
    rca         TEXT,
    meta        JSONB,
    embedding   vector(768),
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rag_ticket_ticket ON rag_ticket(ticket_id);
"""


def dv(value) -> str:
    if isinstance(value, dict):
        return str(value.get("display_value", value.get("value", "")) or "")
    return "" if value is None else str(value)


def ticket_text(rec: dict) -> str:
    """Retrievable text: what the incident IS (symptoms, stack, context)."""
    parts = [
        f"Ticket {dv(rec.get('u_ticket_id'))}: {dv(rec.get('u_summary'))}".strip(),
        f"Description: {dv(rec.get('u_description'))}".strip(),
        f"Category: {dv(rec.get('u_category'))} / {dv(rec.get('u_subcategory'))}".strip(" /"),
        f"Environment: {dv(rec.get('u_environment'))}".strip(),
        f"Technology: {dv(rec.get('u_technology_stack'))}".strip(),
        f"Affected component: {dv(rec.get('u_affected_component'))}".strip(),
        f"Priority {dv(rec.get('u_priority'))} Severity {dv(rec.get('u_severity'))}".strip(),
    ]
    return "\n".join(p for p in parts if p and not p.endswith(": "))


def embed_batch(texts: list[str]) -> list[list[float]]:
    resp = requests.post(
        f"{OLLAMA_HOST}/api/embed",
        json={"model": EMBED_MODEL, "input": texts},
        timeout=EMBED_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["embeddings"]


def fetch_closed(headers, basic, limit: int = 0):
    """Yield closed-ticket rows, paged. limit=0 means all."""
    status_q = "^OR".join(f"u_status={s}" for s in CLOSED_STATUSES)
    offset, got = 0, 0
    while True:
        params = {
            "sysparm_query": f"{status_q}^ORDERBYsys_created_on",
            "sysparm_fields": RAG_FIELDS,
            "sysparm_limit": 100,
            "sysparm_offset": offset,
            "sysparm_display_value": "all",
        }
        resp = requests.get(
            f"{snow.INSTANCE}/api/now/table/{RAG_TABLE}",
            headers=headers, auth=basic, params=params, timeout=snow.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json().get("result", [])
        if not rows:
            return
        for row in rows:
            yield row
            got += 1
            if limit and got >= limit:
                return
        if len(rows) < 100:
            return
        offset += 100


def ingest(limit: int = 0, changed_only: bool = False) -> dict:
    ensure_database()
    if snow.AUTH_MODE == "basic":
        headers, basic = snow.make_auth("")
    else:
        headers, basic = snow.make_auth(snow.get_access_token())
    stats = {"fetched": 0, "embedded": 0, "upserted": 0, "skipped": 0}
    with psycopg.connect(_pg_conninfo()) as conn:
        conn.execute(SCHEMA_SQL)
        known = {}
        if changed_only:
            rows = conn.execute("SELECT sys_id, (meta->>'sys_updated_on') FROM rag_ticket").fetchall()
            known = {sys_id: upd for sys_id, upd in rows}
        batch, metas = [], []
        def flush():
            if not batch:
                return
            vecs = embed_batch([t for t, _ in batch])
            with conn.cursor() as cur:
                for (text, rec), vec in zip(batch, vecs):
                    cur.execute(
                        "INSERT INTO rag_ticket (sys_id, ticket_id, status, summary, text, solution, rca, meta, embedding, embedded_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,now()) "
                        "ON CONFLICT (sys_id) DO UPDATE SET ticket_id=EXCLUDED.ticket_id, status=EXCLUDED.status, "
                        "summary=EXCLUDED.summary, text=EXCLUDED.text, solution=EXCLUDED.solution, rca=EXCLUDED.rca, "
                        "meta=EXCLUDED.meta, embedding=EXCLUDED.embedding, embedded_at=now()",
                        (rec["sys_id"], rec["ticket_id"], rec["status"], rec["summary"],
                         text, rec["solution"], rec["rca"], json.dumps(rec["meta"]), vec),
                    )
                    stats["upserted"] += 1
            conn.commit()
            stats["embedded"] += len(batch)
            print(f"  embedded+stored {stats['embedded']}...", flush=True)
            batch.clear()
        for rec in fetch_closed(headers, basic, limit):
            stats["fetched"] += 1
            sys_id = dv(rec.get("sys_id"))
            if not sys_id:
                continue
            if changed_only and known.get(sys_id) == dv(rec.get("sys_updated_on")):
                stats["skipped"] += 1
                continue
            meta = {
                "ticket_id": dv(rec.get("u_ticket_id")),
                "status": dv(rec.get("u_status")),
                "category": dv(rec.get("u_category")),
                "technology_stack": dv(rec.get("u_technology_stack")),
                "environment": dv(rec.get("u_environment")),
                "sys_updated_on": dv(rec.get("sys_updated_on")),
            }
            batch.append((ticket_text(rec), {
                "sys_id": sys_id,
                "ticket_id": meta["ticket_id"],
                "status": meta["status"],
                "summary": dv(rec.get("u_summary")),
                "solution": dv(rec.get("u_solution")),
                "rca": dv(rec.get("u_root_cause_analysis")),
                "meta": meta,
            }))
            if len(batch) >= EMBED_BATCH:
                flush()
        flush()
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rag_ticket_hnsw "
            "ON rag_ticket USING hnsw (embedding vector_cosine_ops)"
        )
        conn.commit()
        total = conn.execute("SELECT count(*) FROM rag_ticket").fetchone()[0]
    stats["total"] = total
    print(f"Ingest done: fetched={stats['fetched']} embedded={stats['embedded']} "
          f"upserted={stats['upserted']} skipped={stats['skipped']} total={total}")
    return stats


def show_stats() -> None:
    ensure_database()
    with psycopg.connect(_pg_conninfo()) as conn:
        conn.execute(SCHEMA_SQL)
        total = conn.execute("SELECT count(*) FROM rag_ticket").fetchone()[0]
        by_status = conn.execute(
            "SELECT status, count(*) FROM rag_ticket GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        dim = conn.execute(
            "SELECT vector_dims(embedding) FROM rag_ticket LIMIT 1"
        ).fetchone()
    print(f"rag_ticket rows: {total}")
    for status, n in by_status:
        print(f"  {status or '<empty>'}: {n}")
    print(f"embedding dims: {dim[0] if dim else '<none embedded>'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build RAG vector DB from closed ServiceNow tickets.")
    ap.add_argument("--once", action="store_true", help="run full ingest")
    ap.add_argument("--limit", type=int, default=0, help="smoke test on N tickets")
    ap.add_argument("--changed-only", action="store_true", help="re-embed only rows changed since last ingest")
    ap.add_argument("--stats", action="store_true", help="show corpus stats")
    args = ap.parse_args()
    if args.stats:
        show_stats()
    elif args.once or args.limit:
        print(f"RAG ingest | table={RAG_TABLE} closed={CLOSED_STATUSES} model={EMBED_MODEL}")
        ingest(limit=args.limit, changed_only=args.changed_only)
    else:
        ap.error("pass --once [--limit N] [--changed-only] or --stats")
