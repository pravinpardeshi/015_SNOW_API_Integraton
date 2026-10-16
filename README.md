# SNow Manager — ServiceNow + Agentic RCA + ML Early Warning

ServiceNow integration with an incident web UI, a **Knowledge Agent** (local RAG + ReAct tools over resolved tickets), an **auto-triage pipeline** for open tickets, and an **ML early-warning system** (IsolationForest + RandomForest, CloudWatch-ready) for microservices logs. FastAPI backend, plain HTML/CSS/JS frontend, local Ollama — no cloud AI keys needed.

![Python](https://img.shields.io/badge/Python-3.12%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)
![ServiceNow](https://img.shields.io/badge/ServiceNow-Table%20API-62D84E)
![RAG](https://img.shields.io/badge/RAG-pgvector%20%2B%20Ollama-orange)
![ML](https://img.shields.io/badge/ML-IsolationForest%20%2B%20RandomForest-purple)
![License](https://img.shields.io/badge/License-MIT-green)

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [ServiceNow Setup](#servicenow-setup-one-time)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the App](#running-the-app)
- [Using the Web UI](#using-the-web-ui)
- [Change Management](#change-management)
- [Knowledge Agent](#knowledge-agent)
- [Current Incidents Pipeline](#current-incidents-pipeline)
- [Early Warning (AIML)](#early-warning-aiml)
- [API Reference](#api-reference)
- [Project Structure](#project-structure)
- [Security Notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

---

## Features

**ServiceNow core**
- Bearer (OAuth `password` / `client_credentials`) and Basic auth; token reused while valid (memory + `.snow_token.json`, `0600`), refreshed with safety skew; one forced refresh + retry on 401
- Incidents: search, active filter, sorting, pagination with Stats-API totals, detail view with activity timeline, create/update/delete, `sys_id` validation
- Custom-table explorer (default `u_snow_incident_table`): dynamic columns, free-text + encoded queries, full CRUD
- Change Management (`change_request` via `manage_change.py` + generic `/api/custom/{table}`): enable check, create/get/list/update
- Postgres mirror (`sync_to_pg.py`, incremental on `sys_updated_on`)

**Knowledge Agent (RAG + ReAct)**
- Local RAG over resolved tickets (pgvector + Ollama `nomic-embed-text` / `llama3.1:8b`): single-shot ask, multi-turn chat, and agentic ReAct loop with 4 tools (search/fetch/file-triage/create-incident)
- Grounded-only answers with ticket citations; explicit no-solution statement when history doesn't cover the case
- Intent gate: greeting/help/vague asks answered instantly with zero retrieval (no hallucinated investigations, no wasted 10-min CPU calls)

**Current incidents**
- `current_incident` table for open/in-progress tickets; `triage_pipeline.py` ingests + writes grounded triage/RCA/solution/confidence per ticket; dedicated Current view (ticket + triage + RCA + solution + cited-ticket links)

**ML early warning (`AIML/`)**
- 1-hour synthetic microservices log (normal → peak-hour pool-exhaustion failure → restart → recovery)
- Stdlib robust-z baseline + streak predictor; full ML path (IsolationForest + RandomForest, multi-seed training); CloudWatch adapter (logs + metrics); Early Warn UI tab with model details + single-entry scoring

**UI polish**
- Dashboard landing page (connection card with quick Connect, open-ticket counts, triage/RAG/mirror metrics), collapsible icon sidebar, ChatGPT-style agent chat, light/dark themes, Inter font

## Architecture

```
Browser UI (:8090) ──► FastAPI (app.py) ──┬──► ServiceNow Table API
        │                                  ├── .snow_token.json (OAuth cache)
        │                                  ├── Postgres: snow_incident, rag_ticket,
        │                                  │   current_incident, threads
        │                                  └── Ollama :11434 (embed + chat)
        └── AIML/ ──► file / CloudWatch logs ──► features ──► IF + RF ──► alerts
```

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.12+, FastAPI, Uvicorn (`:8090`), Pydantic |
| Frontend | Plain HTML/CSS/JS, Inter font, no framework |
| ServiceNow | Table API + Stats API, `requests` |
| Data | PostgreSQL + pgvector (`snow_incident`, `rag_ticket`, `current_incident`, threads) |
| Local AI | Ollama `nomic-embed-text` (768d) + `llama3.1:8b` |
| ML | scikit-learn (IsolationForest, RandomForest), NumPy; boto3 for CloudWatch |

## Prerequisites

- Python **3.12+**, PostgreSQL with **pgvector**, local **Ollama**
- A ServiceNow **PDI / dev instance** ([developer.servicenow.com](https://developer.servicenow.com)) + OAuth Application Registry entry
- `pip` or [`uv`](https://docs.astral.sh/uv/); AWS creds only for the CloudWatch source

## ServiceNow Setup (One-Time)

1. PDI → **System OAuth > Application Registry > New > OAuth API endpoint for external clients**; copy Client ID/Secret.
2. OAuth user needs REST access (`rest_service` / `snc_platform_rest_api_access`) + table ACLs (admin has them on a PDI).

## Installation

```bash
git clone <your-repo-url>
cd 025_ServiceNow_Cloud
python -m venv venv && source venv/bin/activate
pip install -e .                        # fastapi, uvicorn, requests, pydantic,
                                        # dotenv, psycopg, scikit-learn, boto3
cp .env.example .env                    # then fill in your values
```

```bash
docker run -d --name pgvector -p 5432:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16
ollama serve & ollama pull nomic-embed-text && ollama pull llama3.1:8b
```

## Configuration

All secrets from `.env` (never commit it). Key variables — see `.env.example` for the full list:

```bash
SNOW_INSTANCE=https://devXXXXXX.service-now.com
SNOW_CLIENT_ID=... / SNOW_CLIENT_SECRET=...
# SNOW_AUTH=bearer|basic   SNOW_GRANT_TYPE=password|client_credentials
# SNOW_USERNAME / SNOW_PASSWORD   SNOW_CUSTOM_TABLE=u_snow_incident_table
# SNOW_CHANGE_TABLE=change_request (used by manage_change.py; --table overrides)
# RAG_*: OLLAMA_HOST, OLLAMA_EMBED_MODEL, OLLAMA_CHAT_MODEL, RAG_CLOSED_STATUSES, RAG_TOP_K, RAG_MAX_ITERS
# CUR_RUN_LIMIT / CUR_TOP_K       PG_CONN or PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD
```

## Running the App

```bash
./run.sh   # uvicorn app:app on 0.0.0.0:8090
```

- **UI:** <http://localhost:8090> · **Swagger:** `/docs` · **Health:** `/api/status`

```bash
venv/bin/python sync_to_pg.py --once        # mirror incidents
venv/bin/python rag_ingest.py --once        # build RAG vectors
venv/bin/python triage_pipeline.py --once --limit 10   # triage open tickets
```

## Using the Web UI

Views: **Dashboard** (landing) · **Connect** · **Incidents** · **Custom Table** · **Current** · **Sync** · **Agent chat** · **Observability** · **Early Warn**.

- **Dashboard** — connection card (Connect/Disconnect/Advanced), live open-ticket count, triage/RAG/mirror stats, needs-triage + recently-triaged shortcuts.
- **Incidents** — search/sort/page; Number + sys_id in one column; double-click a row to open; `?` on State explains values 1/2/3/6/7.
- **Current** — open tickets with triage, RCA + confidence, solution, supporting-ticket links; Run pipeline / Re-analyse.
- **Agent chat** — ticket id or symptoms in; agentic ReAct with trace, or single-shot; thread history with Clear.
- **Early Warn** — model details (IF calibration, RF scores) + one-click sample scoring (NORMAL / ANOMALOUS / FAILURE LIKELY).

## Change Management

Stock `change_request` table — no plugin install on a PDI. The OAuth/basic user needs `snc_platform_rest_api_access` + `itil`/`change_manager` + read/create/write ACLs on `change_request` (same setup as incidents, plus Change roles). `403` → Table > Application Access > allow web services / fix ACLs.

`manage_change.py` mirrors `testServiceNow.py` auth (`SNOW_AUTH=bearer|basic`, `SNOW_GRANT_TYPE=password|client_credentials`) and targets `$SNOW_CHANGE_TABLE` (default `change_request`):

```bash
venv/bin/python manage_change.py check                          # probe access (enable verification)
venv/bin/python manage_change.py list --limit 5                 # recent changes
venv/bin/python manage_change.py create --short-description "Upgrade DB pool" \
  --description "Increase max connections ahead of peak" \
  --type Normal --category Software --risk 3 --impact 2          # only --short-description required
venv/bin/python manage_change.py get --sys-id <32-char-sys_id>  # retrieve one change
venv/bin/python manage_change.py update --sys-id <sys_id> --state "In Progress" --work-notes "started"
venv/bin/python manage_change.py <cmd> --table change_task     # override table per call
```

Same via the running backend (generic custom-table routes, `./run.sh` + `POST /api/connect` first):

```bash
curl -X POST http://localhost:8090/api/custom/change_request -H 'Content-Type: application/json' \
  -d '{"short_description":"Upgrade DB pool","type":"Normal"}'
curl "http://localhost:8090/api/custom/change_request/<sys_id>"
curl "http://localhost:8090/api/custom/change_request?limit=5&order_by=-sys_created_on"
# or in the UI: Custom Table tab -> table name `change_request`
```

## Knowledge Agent

`POST /api/rag/agent` runs retrieve → draft → self-check → act (tools: `search_tickets`, `fetch_ticket`, `file_triage_note`, `create_incident`), with write-intent routing, write idempotency, and a forced grounded close on budget exhaustion. Intent gate handles greeting/help/vague inputs instantly. `/api/rag/chat` (multi-turn) and `/api/rag/ask` (single-shot) share the same grounding.

## Current Incidents Pipeline

Open tickets (`u_status` NOT IN closed) → `current_incident` → per-ticket grounded triage/RCA/solution via historic RAG data. LLM may not use parametric knowledge; unsupported cases store *"No supported solution — …"* explicitly. Script (`triage_pipeline.py --once/--loop`) and API (`POST /api/current/run`, `/retriage/{sys_id}`) share the engine.

## Early Warning (AIML)

See `AIML/README.md`. Synthetic 1-hour log (normal → peak failure → restart recovery); stdlib robust-z + streak predictor; ML path (IF per-service models trained on 5 seeded logs, global RF on 27 windowed features, tested on unseen seed 42 — first alert min 35, prediction min 35 = 10-min lead, zero false alarms); CloudWatch source (logs + metrics → same features); `monitor.py --replay/--live/--ml/--source cloudwatch`; `evaluate.py` gates releases.

## API Reference

Base `http://localhost:8090` (incident/custom routes need `POST /api/connect` first).

| Area | Endpoints |
|------|-----------|
| Core | `GET /` · `GET /api/status` · `POST /api/connect` · `POST /api/disconnect` |
| Incidents | `GET /api/incidents?…` · `GET/POST /api/incidents[/{sys_id}]` · `PATCH/DELETE /api/incidents/{sys_id}` |
| Custom | `GET/POST /api/custom/{table}[​/{sys_id}]` · `PATCH/DELETE` · `GET /api/custom-tables/default` |
| Current | `GET /api/current[​/{sys_id}]` · `POST /api/current/run` · `POST /api/current/retriage/{sys_id}` |
| RAG/Agent | `POST /api/rag/ask|chat|agent` · `GET/DELETE /api/rag/threads[​/{id}]` · `GET /api/rag/stats` |
| Early Warn | `GET /api/earlywarning/model` · `GET /api/earlywarning/sample` · `POST /api/earlywarning/score` |
| Ops | `GET /api/sync/stats` · `GET /api/dashboard` · `GET /api/metrics/summary|series` |

## Project Structure

```
├── app.py                 # FastAPI backend (:8090)
├── testServiceNow.py      # Minimal incident demo script
├── manage_change.py       # Change Management: check/list/create/get/update change_request
├── sync_to_pg.py          # Incident → Postgres mirror
├── rag_ingest.py          # Closed tickets → pgvector
├── triage_pipeline.py     # Open tickets → triage/RCA/solution
├── AIML/                  # Early warning: logs, detector, ML, CloudWatch
├── templates/index.html   # Web UI
├── static/                # style.css, app.js
└── run.sh / pyproject.toml / .env.example
```

## Security Notes

- Rotate any demo Client Secret committed in history before going public.
- Keep secrets in local `.env` only; never commit `.env` or `.snow_token.json` (both git-ignored).
- Least privilege on the OAuth user; short-lived tokens; no full tokens in logs.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `401` on `oauth_token.do` / `invalid_grant` | Wrong secret or grant disabled on the registry entry |
| Bearer rejected by Table API | Known ServiceNow-side bearer-validation flakiness — use **Basic** mode |
| Priority won't stick | Recalculated from Urgency × Impact — set those |
| `403` on tables | Grant the OAuth user table ACLs / Application Access (`change_request` also needs `itil`/`change_manager`) |
| RAG `502` / empty | Run `rag_ingest.py --once`; check Ollama + pgvector |
| Agent slow (CPU LLM ~10 min/call) | Vague asks get instant clarifiers; use ticket ids + symptoms; quiet hours help |
| PDI errors | Wake the instance at developer.servicenow.com |

## Roadmap

- [x] Auth, incidents, custom tables, mirror, RAG + agent, triage pipeline, early warning
- [ ] `pytest` + CI
- [ ] MCP server wrapper
- [ ] Multi-user sessions

## Contributing

Fork → feature branch → PR. No credentials, PDI URLs, or customer data in issues/PRs.

## License

MIT — add a `LICENSE` file before public release.

---

Built by **Pravin Pardeshi** — Cloud + ITSM integration series.
