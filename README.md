<<<<<<< HEAD
# ServiceNow Incident Manager – Python + FastAPI + Web UI

A complete, hands-on integration with **ServiceNow (PDI)** using **OAuth 2.0 Client Credentials** and the **Table API (`/api/now/table/incident`)**:

1. **`testServiceNow.py`** – minimal single-file script: authenticate, list, create, and update incidents.
2. **FastAPI backend + HTML/CSS/JS web UI** (`app.py`, port **8090**) – connect, browse with search / sort / pagination, create, rich detail view with activity timeline, update, and delete incidents from the browser.

> Built for learning, demos, ITSM automation portfolios, and as a starter for Agentic AI / MCP tool-calling against ServiceNow.

![Python](https://img.shields.io/badge/Python-3.12%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)
![ServiceNow](https://img.shields.io/badge/ServiceNow-Table%20API-62D84E)
![License](https://img.shields.io/badge/License-MIT-green)

---

## 📋 Table of Contents

- [Features](#-features)
- [Tech Stack](#-tech-stack)
- [How It Works](#-how-it-works)
- [Prerequisites](#-prerequisites)
- [ServiceNow Setup (One-Time)](#-servicenow-setup-one-time)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Running the App](#-running-the-app)
- [Using the Web UI](#-using-the-web-ui)
- [API Reference](#-api-reference)
- [Key Behaviors & Gotchas](#-key-behaviors--gotchas)
- [Script Usage (testServiceNow.py)](#-script-usage-testservicenowpy)
- [Code Reference](#-code-reference)
- [Example Output](#-example-output)
- [Project Structure](#-project-structure)
- [Security Notes](#-security-notes)
- [Troubleshooting](#-troubleshooting)
- [Roadmap](#-roadmap)
- [Contributing](#-contributing)
- [License](#-license)
- [Author](#-author)

---

## ✨ Features

**Connection**
- OAuth 2.0 Client Credentials flow against `oauth_token.do` – no username/password in code
- Connect form (instance URL, Client ID/Secret), live connection status with token countdown, disconnect
- Credentials pre-filled from defaults; overridable via environment variables

**Incidents card**
- List incidents with free-text **search** (number / short description), **active-only** filter, page-size selector
- **Clickable sortable column headers** (Number, Short description, State, Priority, Urgency, Impact, Opened, Updated) with ▲/▼ indicators
- **Pagination** (Prev / Next + `Showing X–Y · Page N`), backed by `sysparm_offset`
- Dedicated **`sys_id` column** with per-row Copy button

**Detail view**
- Header with color-coded status pill, Priority/Urgency/Impact badges, opened/updated/by line
- Full description, two-column details grid (assignee, group, caller, category, resolution info, …)
- **Activity timeline** – comments & work notes parsed into entries with timestamp, author, and Comment/Work-note tags
- sys_id row with Copy button; collapsible Raw JSON; Edit / Delete / Close actions

**Create / Update / Delete**
- Create modal (short description, description, urgency, impact, priority, comments)
- Update modal pre-fills **current values** and warns when ServiceNow recalculates Priority (see [Gotchas](#-key-behaviors--gotchas))
- Delete with confirmation, from table or detail view
- Friendly error messages – nested ServiceNow errors are unwrapped for display; malformed `sys_id`s are rejected client- and server-side with a clear message

**UI polish**
- **Light / Dark themes** with toggle in the header, persisted in `localStorage`
- **Inter font** (Google Fonts) across the page, responsive layout, toast notifications

## 🛠 Tech Stack

| Layer | Technology |
|-------|------------|
| Language | Python >= 3.12 |
| Backend | FastAPI, Uvicorn (port 8090), Pydantic |
| Frontend | Plain HTML + CSS + JavaScript (no framework), Google Inter font |
| HTTP client | `requests` |
| Auth | OAuth 2.0 Client Credentials (`grant_type=client_credentials`) |
| API | ServiceNow Table API – `incident` table |
| Packaging | `uv` / `pip` + `pyproject.toml` |
| Instance type | ServiceNow PDI, e.g. `devXXXXXX.service-now.com` |

## 🧠 How It Works

```
┌──────────┐  1. POST /oauth_token.do (client_credentials)  ┌──────────────┐
│ Browser  │ ─────────────────────────────────────────────▶ │              │
│   UI     │ ◀───────────────────────────────────────────── │              │
│    │     │            2. Bearer token (kept server-side)   │  FastAPI     │
│    │     │                                                │  :8090       │
│    │     │  3. /api/connect|incidents|...                 │              │
│    │     │ ─────────────────────────────────────────────▶ │    │         │
│    │     │ ◀───────────────────────────────────────────── │    │         │
└──────────┘              JSON                               │    │ 4. Table API
                                                            │    │    + Bearer
                                                            ▼    ▼
                                                     ┌──────────────────┐
                                                     │   ServiceNow     │
                                                     │   devXXXXXX      │
                                                     │  .service-now.com│
                                                     └──────────────────┘
```

The browser never sees the Client Secret after connecting – the FastAPI backend holds the token in memory and proxies all Table API calls. Core ServiceNow logic is ported 1:1 from `testServiceNow.py` (`get_access_token`, `get_incidents`, `create_incident`, `update_incident`).

## ✅ Prerequisites

- Python **3.12+**
- A ServiceNow **PDI / Dev instance** – request one free at [developer.servicenow.com](https://developer.servicenow.com)
- An **OAuth Application Registry** entry in ServiceNow (see below)
- `pip` or [`uv`](https://docs.astral.sh/uv/) installed
- Network access to `https://<your-instance>.service-now.com`

## 🔧 ServiceNow Setup (One-Time)

1. Log in to your PDI as `admin`.
2. Go to **System OAuth > Application Registry > New > Create an OAuth API endpoint for external clients**.
3. Fill in:
   - **Name:** e.g. `Python Incident Client`
   - **Client ID:** auto-generated (or set your own)
   - **Client Secret:** auto-generated – copy it securely
4. Submit, then open the record and copy **Client ID** and **Client Secret**.
5. Ensure your user / OAuth entity has `rest_service` role and read/write access to the `incident` table (admin has it by default on a PDI).
6. Note your instance URL, e.g. `https://dev389543.service-now.com`.

> No ACL changes are needed for a default PDI admin demo. Note: the `sys_journal_field` table is typically **not** readable by the OAuth user – the app's activity timeline therefore parses journal history embedded in the incident record instead (works out of the box).

## 📦 Installation

```bash
git clone https://github.com/<your-username>/025_ServiceNow_Cloud.git
cd 025_ServiceNow_Cloud

python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# with uv
uv pip install --python venv/bin/python fastapi "uvicorn>=0.30" requests "pydantic>=2.0"

# ...or plain pip (if your environment allows it)
pip install fastapi "uvicorn>=0.30" requests "pydantic>=2.0"
```

The dependencies are also declared in `pyproject.toml`, so `pip install -e .` works too.

## ⚙️ Configuration

The app reads credentials from a **`.env` file** (loaded via `python-dotenv`) — nothing secret is hardcoded in the code:

```bash
cp .env.example .env   # then edit .env with your values
```

`.env` contents:

```bash
SNOW_INSTANCE=https://devXXXXXX.service-now.com
SNOW_CLIENT_ID=your-client-id
SNOW_CLIENT_SECRET=your-client-secret
```

| Variable | Description | Example |
|----------|-------------|---------|
| `SNOW_INSTANCE` | Base URL of your PDI, no trailing slash | `https://dev389543.service-now.com` |
| `SNOW_CLIENT_ID` | OAuth Application Registry Client ID | `35e3f36a…` |
| `SNOW_CLIENT_SECRET` | OAuth Application Registry Client Secret | `4fQYK0DZ…` |

Both `app.py` and `testServiceNow.py` call `load_dotenv()` on startup and fail with a clear message if the ID/secret are missing. Shell-exported `SNOW_*` variables also work (environment takes precedence over `.env`). The web UI's Connect form starts empty — paste the values from `.env` to connect. `.env` is git-ignored; commit only `.env.example`.

Suggested `.gitignore`:

```gitignore
venv/
.venv/
__pycache__/
*.pyc
.env
*.log
```

## 🚀 Running the App

```bash
./run.sh
# ...or directly:
venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8090
```

Then open:

- **UI:** <http://localhost:8090>
- **Interactive API docs (Swagger):** <http://localhost:8090/docs>
- **Health:** `curl http://localhost:8090/api/status`

> The token lives in server memory – restarting the server disconnects you; just hit **Connect** again in the UI.

## 🖱 Using the Web UI

1. **Connect** – check the pre-filled instance URL / Client ID / Secret (or paste your own) and click **Connect & Get Token**. The header shows green status + token lifetime.
2. **Browse incidents** – the table auto-loads. Use search, **active only**, page size, **click any column header to sort**, and **Prev/Next** to page (`Showing 1–25 · Page 1` sits centered under the table).
3. **View** – opens the detail view: status pill, P/U/I badges, description, details grid, **activity timeline**, raw JSON. Edit/Delete from here too.
4. **+ New Incident** – fill the modal and Create; the new record appears at the top with a toast showing its number (e.g. `INC0010030`).
5. **Edit** – the modal shows current Priority/Urgency/Impact/State up top. If ServiceNow recalculates your Priority, the modal stays open with a yellow explanation instead of silently closing.
6. **Theme** – moon/sun button in the header toggles Light/Dark (remembered across reloads).

## 📡 API Reference

Base URL: `http://localhost:8090`. All incident routes require a prior `POST /api/connect` (otherwise `401`).

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Web UI |
| `GET` | `/api/status` | `{connected, instance, expires_in, last_error}` |
| `POST` | `/api/connect` | `{instance, client_id, client_secret}` → fetches & caches Bearer token |
| `POST` | `/api/disconnect` | Drop the cached token |
| `GET` | `/api/incidents?limit=&offset=&active_only=&search=&order_by=` | List incidents. `order_by` e.g. `-opened_at`, `number`, `priority`. Returns `{result, has_next, offset, limit}` |
| `GET` | `/api/incidents/{sys_id}` | Single incident (with `sysparm_display_value=all`, so references resolve to names) |
| `POST` | `/api/incidents` | Create. Requires `short_description`; accepts `description, urgency, impact, priority, category, assignment_group, assigned_to, state, comments, work_notes, …` |
| `PATCH` | `/api/incidents/{sys_id}` | Update. At least one field; accepts `priority, urgency, impact, state, assignment_group, description, comments, work_notes, close_notes, close_code, …` |
| `DELETE` | `/api/incidents/{sys_id}` | Delete incident |

`sys_id` path params are validated (32 hex chars) with a clear `400` before anything reaches ServiceNow.

### curl examples

```bash
# Connect
curl -X POST http://localhost:8090/api/connect -H "Content-Type: application/json" \
  -d '{"instance":"https://devXXXXXX.service-now.com","client_id":"...","client_secret":"..."}'

# Page 2, 10 per page, newest first
curl "http://localhost:8090/api/incidents?limit=10&offset=10&order_by=-opened_at"

# Search
curl "http://localhost:8090/api/incidents?search=VPN&active_only=false"

# Create
curl -X POST http://localhost:8090/api/incidents -H "Content-Type: application/json" \
  -d '{"short_description":"VPN outage Pune","urgency":"1","impact":"2"}'

# Update (see Priority gotcha below!)
curl -X PATCH http://localhost:8090/api/incidents/<sys_id> -H "Content-Type: application/json" \
  -d '{"urgency":"1","impact":"1","comments":"Escalated"}'
```

## ⚠️ Key Behaviors & Gotchas

1. **ServiceNow recalculates Priority from Urgency × Impact on save.** Sending `priority: "1"` alone may store `4` (verified live: requested P1 with U2/I3 → stored P4). To control Priority, set **Urgency + Impact**; the UI shows current values and warns when a recalculation happens.
2. **No total count from the Table API**, so pagination is Prev/Next with server-detected `has_next` (one extra row fetched, then trimmed) – no "Page N of M".
3. **Journal via the record, not `sys_journal_field`** – the OAuth user typically can't read that table (returns `[]`), so the activity timeline parses `comments_and_work_notes` from the incident itself.
4. **Single-record fetches use `sysparm_display_value=all`** – even `sys_id`/`number` arrive as `{display_value, value}` objects; the frontend normalizes them (this once caused an `[object Object]` bug – now guarded on both sides).
5. **In-memory token** – server restarts disconnect you; tokens also expire (~30 min) and need a reconnect.

## 📜 Script Usage (testServiceNow.py)

The original minimal demo (token → list → create → list again → update):

```bash
venv/bin/python testServiceNow.py
```

```python
from testServiceNow import get_access_token, get_incidents, create_incident

token = get_access_token()
incidents = get_incidents(token)
for inc in incidents:
    print(inc["number"], inc["short_description"], inc["state"])

new_inc = create_incident(token)
print("Created:", new_inc["number"], new_inc["sys_id"])
```

Valid `urgency` / `impact`: `"1"` (High), `"2"` (Medium), `"3"` (Low). Field meanings: `state` `1`=New, `2`=In Progress, `6`=Resolved, `7`=Closed; `priority` `1`=Critical … `5`=Planning.

## 📖 Code Reference

### `get_access_token() -> str`

```http
POST {INSTANCE}/oauth_token.do
Authorization: Basic base64(CLIENT_ID:CLIENT_SECRET)

grant_type=client_credentials
```

### `get_incidents(token) -> list[dict]` → `GET /api/incidents`

```http
GET {INSTANCE}/api/now/table/incident?sysparm_query=active=true^ORDERBYDESCopened_at
    &sysparm_fields=sys_id,number,short_description,description,state,priority,urgency,impact,assigned_to,assignment_group,opened_at,updated_at
    &sysparm_limit=25&sysparm_offset=0
Authorization: Bearer <token>
```

### `create_incident(token) -> dict` → `POST /api/incidents`

```http
POST {INSTANCE}/api/now/table/incident
{
  "short_description": "Test incident created through Python",
  "description": "This incident was created using the ServiceNow Table API.",
  "urgency": "2"
}
```

### `update_incident(token, sys_id, ...) -> dict` → `PATCH /api/incidents/{sys_id}`

Only provided fields are sent (`priority`, `assignment_group`, `description`, `comments`, plus `short_description`, `urgency`, `impact`, `state`, `work_notes`, `close_notes`, `close_code` in the API).

## 🖥 Example Output

```
HTTP Response for Incident Request:  200
Create HTTP status: 201
Incident number: INC0010030
Incident sys_id: 22f5f0f247dbc7102784e454116d437e
```

## 📁 Project Structure

```
025_ServiceNow_Cloud/
├── app.py                     # FastAPI backend (port 8090) – connect + incident CRUD proxy
├── run.sh                     # Starter: uvicorn app:app on 0.0.0.0:8090
├── testServiceNow.py          # Original demo script: OAuth + list + create + update
├── pyproject.toml             # Project metadata + deps (fastapi, uvicorn, requests, pydantic)
├── README.md                  # This file
├── templates/
│   └── index.html             # Web UI: connect panel, incidents card, modals
├── static/
│   ├── style.css              # Dark/light themes, cards, table, detail view
│   └── app.js                 # UI logic: paging, sorting, detail render, journal parse
├── src/
│   └── 025_servicenow_cloud/
│       └── __init__.py        # Package scaffold
└── venv/                      # Local virtualenv (do not commit)
```

## 🔒 Security Notes

> ⚠️ **Important before pushing to GitHub:** this repo's history/code contains a demo `CLIENT_SECRET` and instance URL. Rotate/revoke that secret in **System OAuth > Application Registry** and switch to environment variables before making the repo public.

Checklist:

- [ ] Regenerate Client Secret in ServiceNow after this demo
- [ ] Remove hardcoded secrets, use `SNOW_*` env vars / `.env` (add `.env` to `.gitignore`)
- [ ] Enable GitHub **secret scanning / push protection**
- [ ] Restrict OAuth scope / roles to minimum needed (`rest_service`, table ACLs)
- [ ] Use short-lived tokens; never log full tokens

## 🐛 Troubleshooting

| Symptom | Likely cause / fix |
|---------|-------------------|
| `401 Unauthorized` on `oauth_token.do` | Wrong Client ID/Secret or extra whitespace. Regenerate secret and retry. |
| `401 Not connected. POST /api/connect first` | Server restarted (in-memory token lost) or token expired – hit **Connect** again. |
| Priority change "doesn't stick" | ServiceNow recalculates Priority from Urgency × Impact – set those two instead (see Gotchas). |
| `No Record found — Record doesn't exist or ACL…` | Bad `sys_id` (e.g. record deleted, or a stale ID). Refresh the list and retry; the UI now validates IDs first. |
| `Invalid sys_id '[object Object]'` | Stale frontend – hard-refresh (Ctrl+Shift+R) so the fixed JS loads. |
| `403 Forbidden` | OAuth user lacks `rest_service` role or table ACL denies access. Test as admin first. |
| Empty table / `result: []` | `active=true` filter excludes closed incidents, or search text matches nothing – uncheck active-only / clear search. |
| Next button never enables | You're on the last page (`has_next: false`) – correct behavior, not a bug. |
| Port `8090` already in use | Another uvicorn instance is running – stop it (`pkill -f 'uvicorn app:app'`) and restart. |
| Light theme looks unstyled | Hard-refresh to bypass cached CSS. |
| PDI connection errors / SSL | PDI hibernated – wake it at developer.servicenow.com; check corporate proxy. |
| `400 invalid_grant` | Grant type not enabled for the OAuth registry entry – allow **Client Credentials**. |

## 🗺 Roadmap

- [x] OAuth client-credentials connect + proxied Table API backend (FastAPI, port 8090)
- [x] Web UI: list, search, **sort**, **pagination**, `sys_id` column with copy
- [x] Rich detail view with activity timeline, Edit/Delete actions
- [x] Update with Priority-recalculation warning; friendly error unwrapping; `sys_id` validation
- [x] Light/Dark themes, Inter font
- [ ] Load config from `.env` via `python-dotenv`
- [ ] Persistent token cache / multi-user sessions (currently in-memory, single user)
- [ ] `argparse` CLI for the script (`list`, `create`, `get`, `update`, `delete`)
- [ ] Wrap in a reusable `ServiceNowClient` class with auto token-refresh
- [ ] `pytest` + mock tests and GitHub Actions CI
- [ ] MCP server wrapper for Agentic AI tool-calling
- [ ] Type hints + `ruff` / `black` formatting

## 🤝 Contributing

1. Fork the repo
2. Create a feature branch: `git checkout -b feat/my-change`
3. Commit: `git commit -m "feat: my change"`
4. Push and open a Pull Request

Please do not include credentials, PDI URLs, or customer data in issues/PRs.

## 📄 License

Add a license before public release. Suggested for demos/portfolios:

- MIT – permissive, simple. Add a `LICENSE` file with the MIT text.

If this stays private/educational, you may leave it unlicensed, but GitHub will treat it as all-rights-reserved by default.

## 👤 Author

**Pravin Pardeshi** – pravin.pardeshi@gmail.com

Part of a hands-on Cloud + ITSM integration series (alongside JIRA Cloud, AWS, Google Cloud experiments). Built with Python (`requests`, FastAPI) against a ServiceNow PDI.

---

⭐ If this starter helped you, give it a star and use it as a base for Incident automation, SRE bots, or LLM tool integrations with ServiceNow.
=======
# 015_SNOW_API_Integraton
>>>>>>> fcc122b24307b59624ee5ceaf5109c3c61b472f8
