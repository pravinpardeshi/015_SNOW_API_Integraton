# Windows Setup — SNow Manager (ServiceNow + RAG + triage UI)

Moves this app to another Windows machine that already has PostgreSQL and the Python libraries. Automated path: `setup_windows.ps1`. Manual fallback commands are below.

## 1. Prerequisites

| Need | Notes |
|---|---|
| Python 3.12+ | From python.org, tick **Add to PATH**. Verify: `python --version` |
| PostgreSQL (running) | Verify: service `postgresql-x64-*` is Started in `services.msc` |
| pgvector extension | **Not bundled** with stock EDB Windows installers — see §2 |
| Ollama | From https://ollama.com/download/windows |
| ServiceNow access | PDI/dev instance awake at developer.servicenow.com + OAuth app credentials |
| This repo folder | Copy the whole `025_ServiceNow_Cloud` directory to the new machine |

## 2. Install pgvector (Windows gotcha)

`setup_windows.ps1` stops with guidance if this is missing. Options:

- **Manual:** https://github.com/pgvector/pgvector/releases → download the Windows zip matching your PostgreSQL major version → copy `bin/vector.dll` → `<PostgreSQL>\lib`, `share\extension\vector*` → `<PostgreSQL>\share\extension` → restart PostgreSQL.
- **Docker instead:** `docker run -d --name pgvector -p 5432:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16` (then set `PGPASSWORD=postgres` in `.env`).

## 3. Parameters (`.env`)

Easiest: **copy the `.env` file from the old machine** into the repo folder. Otherwise `copy .env.example .env` and fill in:

```
SNOW_INSTANCE=https://devXXXXXX.service-now.com
SNOW_CLIENT_ID=... / SNOW_CLIENT_SECRET=...
SNOW_USERNAME=... / SNOW_PASSWORD=...     # required for SNOW_AUTH=basic (recommended)
```

Full parameter reference (`.env` value → default if unset):

| Variable | Default | Used for |
|---|---|---|
| `SNOW_AUTH` | `bearer` | `basic` recommended — per-request username/password, unaffected by OAuth bearer-validation outages |
| `SNOW_GRANT_TYPE` | `password` | Bearer-mode grant: `password` (needs username/password) or `client_credentials` |
| `SNOW_CUSTOM_TABLE` | `u_snow_incident_table` | Custom-table explorer default; RAG + triage source |
| `PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` | `localhost` / `5432` / `postgres` / *(empty)* / `snowdb` | Postgres connection (or set `PG_CONN` full conninfo to override) |
| `OLLAMA_HOST` | `http://localhost:11434` | Embeddings + chat server |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Must match between ingest and query (768 dims) |
| `OLLAMA_CHAT_MODEL` | `llama3.1:8b` | RCA prose generation |
| `RAG_CLOSED_STATUSES` | `Resolved` | `u_status` values forming the RAG corpus; everything else counts as open |
| `RAG_TOP_K` / `CUR_TOP_K` | `5` | Similar tickets per ask / per triage |
| `CUR_RUN_LIMIT` | `10` | Max tickets per triage run |

## 4. Automated setup

```powershell
cd <repo>\025_ServiceNow_Cloud
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
```

What it does, in order (order matters):

1. Checks `python` on PATH.
2. Requires `.env` (copies from `.env.example` on first run, then stops so you can fill secrets).
3. `pip install -r requirements.txt`.
4. Creates database `snowdb` + `CREATE EXTENSION vector` + all UI schemas (`snow_incident`, `sync_state`, `rag_ticket`, `current_incident`, `rag_thread`, `rag_message` — imported from `sync_to_pg.py` / `rag_ingest.py` / `app.py`, so they always match the code).
5. `ollama pull nomic-embed-text` + `ollama pull llama3.1:8b`.
6. `python sync_to_pg.py --once` → fills `snow_incident` (Sync tab, mirror stats).
7. `python rag_ingest.py --once` → fills `rag_ticket` (~488 rows, embedding takes a while; Agent chat tab).
8. `python triage_pipeline.py --once --limit 10` → fills `current_incident` (Current tab; each ticket costs 1+ slow CPU LLM calls).
9. Prints row counts for verification.

Variants:

```powershell
.\setup_windows.ps1 -RagLimit 20     # smoke test: embed 20 tickets only
.\setup_windows.ps1 -SkipTriage       # skip LLM triage; run it later manually
.\setup_windows.ps1 -TriageLimit 5 -SkipOllamaPull
```

## 5. Manual fallback (same steps by hand)

```powershell
copy .env.example .env          # fill in secrets (or copy .env from old machine)
python -m pip install -r requirements.txt
ollama pull nomic-embed-text
ollama pull llama3.1:8b
python sync_to_pg.py --once
python rag_ingest.py --once
python triage_pipeline.py --once --limit 10
```

## 6. Verify — UI tab ↔ database mapping

```powershell
python -c "import psycopg; from sync_to_pg import _pg_conninfo; conn = psycopg.connect(_pg_conninfo()); [print(f'{t}:', conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0]) for t in ['snow_incident','rag_ticket','current_incident','rag_thread']]"
```

Healthy values (reference machine): `snow_incident=16`, `rag_ticket=488`, `current_incident=2`.

| UI tab | Source | Empty because… |
|---|---|---|
| Incidents, Custom Table | Live ServiceNow Table API | Not connected (press Connect) or PDI asleep |
| Sync | `snow_incident` / `sync_state` | `sync_to_pg.py --once` not run / PG unreachable |
| Agent chat | `rag_ticket` (+ Ollama) | `rag_ingest.py --once` not run, pgvector missing, or Ollama down |
| Current | `current_incident` (+ `rag_ticket` + Ollama) | triage pipeline not run yet |
| Early Warn | `AIML\models\*.joblib` + `baseline.json` (files, not DB) | Copy them from the old machine, or retrain: `python AIML\train.py` + `python AIML\ml_train.py` |
| Observability, Dashboard metrics | In-memory (resets on restart) | Normal after a restart |

API cross-checks once the app runs: `/api/sync/stats`, `/api/rag/stats`, `/api/status`.

## 7. Start the app

```powershell
python -m uvicorn app:app --host 0.0.0.0 --port 8090
```

Open http://localhost:8090 → press **Connect**. Swagger: `/docs`. Health: `/api/status`.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not reach PostgreSQL` | Start the service in `services.msc`; check `PGHOST/PGPORT/PGUSER/PGPASSWORD` in `.env` |
| `PGVECTOR MISSING` / `vector extension` error | §2 — install pgvector for your PG major version, restart Postgres, re-run |
| `401` / `invalid_grant` on `oauth_token.do` | Wrong secret or grant disabled on the Application Registry entry |
| Bearer rejected by Table API | Known ServiceNow-side flakiness — set `SNOW_AUTH=basic` in `.env` |
| `403` on tables | Grant the OAuth user table ACLs / Application Access in ServiceNow |
| RAG `502` / empty Agent replies | Run `rag_ingest.py --once`; check `ollama list` and pgvector |
| Agent/triage very slow | CPU LLM ≈ minutes per call — use ticket ids + symptoms, `-TriageLimit 5`, `-SkipTriage` first |
| PDI errors | Wake the instance at developer.servicenow.com |
