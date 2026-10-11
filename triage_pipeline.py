"""Auto-ingestion pipeline: open / in-progress tickets -> current_incident.

Fetches open tickets (u_status NOT IN RAG_CLOSED_STATUSES) from CUR_TABLE,
upserts ticket fields into the local current_incident table, then runs
grounded triage + RCA + solution recommendation per ticket using ONLY the
historic RAG corpus (rag_ticket). The LLM is forbidden from using its own
parametric knowledge; unsupported cases store an explicit no-solution
statement instead of an invented fix.

Usage:
  venv/bin/python triage_pipeline.py --once                # ingest + triage untriaged
  venv/bin/python triage_pipeline.py --once --limit 5      # bound the run (LLM calls are slow on CPU)
  venv/bin/python triage_pipeline.py --once --retriage     # re-analyse even triaged rows
  venv/bin/python triage_pipeline.py --loop --interval 600 # daemon: repeat every 10 min

Config (environment / .env — never hardcoded):
  SNOW_*            instance + auth, same as testServiceNow.py / app.py.
                    Basic mode (SNOW_AUTH=basic) is recommended while inbound
                    OAuth bearer validation is unreliable server-side.
  CUR_TABLE         source table (default: SNOW_CUSTOM_TABLE value).
  RAG_CLOSED_STATUSES  statuses treated as closed (default: Resolved).
  CUR_TOP_K         similar tickets per analysis (default 5).
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

import psycopg
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as backend
import testServiceNow as snow
from sync_to_pg import ensure_database


def run_once(limit: int, top_k: int, retriage: bool) -> dict:
    """One ingest + triage cycle. Returns stats."""
    ensure_database()
    # Honour the configured SNOW_AUTH mode inside the shared backend state.
    backend._state["auth_mode"] = snow.AUTH_MODE
    stats = {"fetched": 0, "upserted": 0, "triaged": 0,
             "skipped": 0, "unsupported": 0, "errors": []}
    recs = backend._current_fetch_open(limit)
    stats["fetched"] = len(recs)
    with psycopg.connect(backend._rag_pg_conninfo()) as conn:
        backend._ensure_current(conn)
        with conn.cursor() as cur:
            for rec in recs:
                row = backend._current_row_from_rec(rec)
                if not row["sys_id"]:
                    continue
                cur.execute(backend.CURRENT_UPSERT_SQL, row)
                stats["upserted"] += 1
            conn.commit()
            targets = conn.execute(
                "SELECT sys_id FROM current_incident "
                + ("" if retriage else "WHERE triage IS NULL ")
                + "ORDER BY ticket_id LIMIT %s",
                (limit,),
            ).fetchall()
        for (sys_id,) in targets:
            try:
                rec = next(
                    (r for r in recs if backend._dv_sys(r) == sys_id), None
                )
                if rec is None:
                    stored = conn.execute(
                        "SELECT raw FROM current_incident WHERE sys_id = %s",
                        (sys_id,),
                    ).fetchone()
                    rec = backend._raw_to_rec(stored[0]) if stored else None
                if not rec:
                    stats["skipped"] += 1
                    continue
                _q, analysis = backend._current_analyze_text(rec, top_k)
                backend._current_store_analysis(conn, sys_id, analysis)
                conn.commit()
                stats["triaged"] += 1
                if not analysis["supported"]:
                    stats["unsupported"] += 1
                print(
                    f"  triaged {sys_id[:8]} confidence={analysis['confidence']} "
                    f"supported={analysis['supported']}",
                    flush=True,
                )
            except Exception as exc:  # one bad ticket must not kill the run
                conn.rollback()
                stats["errors"].append(f"{sys_id[:8]}: {str(exc)[:120]}")
                print(f"  ERROR {sys_id[:8]}: {str(exc)[:150]}", flush=True)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ingest open tickets + grounded triage into current_incident.")
    ap.add_argument("--once", action="store_true", help="single cycle, then exit")
    ap.add_argument("--loop", action="store_true", help="repeat every --interval seconds")
    ap.add_argument("--interval", type=int, default=600, help="seconds between --loop cycles (default 600)")
    ap.add_argument("--limit", type=int, default=10, help="max tickets per cycle (default 10)")
    ap.add_argument("--top-k", type=int, default=5, help="similar tickets per analysis (default 5)")
    ap.add_argument("--retriage", action="store_true", help="re-analyse even already-triaged rows")
    args = ap.parse_args(argv)

    if bool(args.once) == bool(args.loop):
        ap.error("pass exactly one of --once or --loop")
    if args.interval < 30:
        ap.error("--interval must be >= 30 seconds")
    if args.limit < 1:
        ap.error("--limit must be >= 1")

    print(
        f"Current-incident pipeline | table={backend.CUR_TABLE} "
        f"open=NOT {backend._cur_closed_statuses()} auth={snow.AUTH_MODE}"
    )
    if args.once:
        stats = run_once(args.limit, args.top_k, args.retriage)
        print(f"Done: {stats}")
        return 0 if not stats["errors"] else 1
    print(f"Loop mode: every {args.interval}s. Ctrl+C to stop.")
    while True:
        started = datetime.now(timezone.utc).strftime("%H:%M:%S")
        try:
            stats = run_once(args.limit, args.top_k, args.retriage)
            print(f"[{started}] {stats}")
        except Exception as exc:  # keep daemon alive across transient errors
            print(f"[{started}] cycle failed (will retry): {str(exc)[:300]}")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
