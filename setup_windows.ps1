# setup_windows.ps1 — fresh-machine setup for SNow Manager (ServiceNow + RAG + triage UI).
#
# Run on the NEW Windows machine (PowerShell 5.1+):
#   cd <repo>\025_ServiceNow_Cloud
#   powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
#
# Options:
#   .\setup_windows.ps1 -RagLimit 20            # smoke test: embed only 20 tickets
#   .\setup_windows.ps1 -SkipTriage             # skip LLM triage (slow on CPU)
#   .\setup_windows.ps1 -TriageLimit 5          # triage only 5 open tickets
#   .\setup_windows.ps1 -SkipOllamaPull         # you already pulled the models
#
# What it does (in order — order matters):
#   1. checks python + pip
#   2. ensures .env exists (copies .env.example; YOU fill in SNOW_* secrets,
#      easiest: copy the .env file from the old machine)
#   3. pip installs requirements.txt
#   4. creates PostgreSQL database + pgvector extension + ALL UI schemas
#      (snow_incident, sync_state, rag_ticket, current_incident, rag_thread,
#       rag_message) — schemas are imported from the app code so they match
#   5. pulls Ollama models (embed + chat)
#   6. sync_to_pg.py --once        -> fills snow_incident  (Sync tab, mirror stats)
#   7. rag_ingest.py --once        -> fills rag_ticket     (Agent chat / RAG tab)
#   8. triage_pipeline.py --once   -> fills current_incident (Current tab)
#   9. verifies row counts so you know the UI will show data
#
# Env vars it honours (same names as .env / Linux):
#   PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE  (defaults: localhost 5432 postgres <empty> snowdb)
#   PG_CONN          full libpq conninfo — overrides the PG* parts above
#   SNOW_AUTH        bearer|basic (basic recommended — unaffected by OAuth outages)
#   SNOW_INSTANCE SNOW_CLIENT_ID SNOW_CLIENT_SECRET SNOW_USERNAME SNOW_PASSWORD
#   OLLAMA_HOST (default http://localhost:11434), OLLAMA_EMBED_MODEL, OLLAMA_CHAT_MODEL
#   RAG_CLOSED_STATUSES (default Resolved), SNOW_CUSTOM_TABLE (default u_snow_incident_table)

param(
  [int]$RagLimit = 0,          # 0 = all closed tickets (~488 rows, slow embed); e.g. 20 for smoke test
  [int]$TriageLimit = 10,       # max open tickets triaged per run (each = 1+ slow CPU LLM calls)
  [switch]$SkipTriage,          # skip triage_pipeline (Current tab stays empty until you run it)
  [switch]$SkipOllamaPull       # skip `ollama pull` (models already present)
)

$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $AppDir

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "OK: $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "WARN: $msg" -ForegroundColor Yellow }

# ---------------------------------------------------------------- 1. python
Step "1/9  Checking Python"
$py = $null
foreach ($c in @("python", "py -3", "python3")) {
  try { & $c.Split(" ")[0] @($c.Split(" ")[1..99] | Where-Object { $_ }) --version 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch { }
}
if (-not $py) { throw "No Python found. Install Python 3.12+ from python.org (tick 'Add to PATH'), then re-run." }
Write-Host "Using python command: $py"
Invoke-Expression "$py --version"

# ---------------------------------------------------------------- 2. .env
Step "2/9  Checking .env (ServiceNow + Postgres parameters)"
if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Warn ".env was missing — copied from .env.example."
  Write-Host ""
  Write-Host "  EASIEST: copy the .env file from your old machine into this folder," -ForegroundColor Yellow
  Write-Host "  then re-run this script. It holds:" -ForegroundColor Yellow
  Write-Host "    SNOW_INSTANCE / SNOW_CLIENT_ID / SNOW_CLIENT_SECRET / SNOW_USERNAME / SNOW_PASSWORD" -ForegroundColor Yellow
  Write-Host "  Optional (defaults shown): SNOW_AUTH=bearer PGHOST=localhost PGPORT=5432" -ForegroundColor Yellow
  Write-Host "    PGUSER=postgres PGPASSWORD= PGDATABASE=snowdb OLLAMA_HOST=http://localhost:11434" -ForegroundColor Yellow
  throw "Fill in .env first, then re-run setup_windows.ps1."
}
# Load .env into this session so PG*/SNOW* are visible to child python processes
Get-Content ".env" | ForEach-Object {
  if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
  $k, $v = $_.Split('=', 2)
  $k = $k.Trim(); $v = $v.Trim()
  if ($k -and -not (Test-Path "env:$k")) { Set-Item "env:$k" $v }
}
foreach ($k in @("SNOW_INSTANCE","SNOW_CLIENT_ID","SNOW_CLIENT_SECRET")) {
  if (-not (Get-Item "env:$k" -ErrorAction SilentlyContinue).Value) { throw "Missing $k in .env — copy .env from the old machine or fill it in." }
}
if (($env:SNOW_AUTH -eq "basic" -or -not $env:SNOW_AUTH) -and ((-not $env:SNOW_USERNAME) -or (-not $env:SNOW_PASSWORD))) {
  Warn "SNOW_USERNAME/SNOW_PASSWORD empty — Basic auth and password-grant bearer need them."
}
Ok ".env present (SNOW_INSTANCE=$env:SNOW_INSTANCE SNOW_AUTH=$($env:SNOW_AUTH))"

# ---------------------------------------------------------------- 3. pip deps
Step "3/9  Installing Python libraries"
Invoke-Expression "$py -m pip install -r requirements.txt"
Ok "requirements installed"

# ---------------------------------------------------------------- 4. Postgres DB + schemas
Step "4/9  Creating database + pgvector + UI schemas"
Invoke-Expression "$py -c `"import sync_to_pg as s; s.ensure_database(); print('database ready:', s.PGDATABASE)`""
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Write-Host "Could not reach PostgreSQL. Check:" -ForegroundColor Red
  Write-Host "  - PostgreSQL is installed and running (services.msc -> postgresql-x64-xx -> Start)" -ForegroundColor Red
  Write-Host "  - PGHOST/PGPORT/PGUSER/PGPASSWORD in .env match (default: localhost/5432/postgres/<no password>)" -ForegroundColor Red
  throw "Postgres connection failed."
}
# Schemas are imported from the real code so they always match app.py.
Invoke-Expression "$py -c `"`nimport psycopg;`nfrom sync_to_pg import ensure_database, _pg_conninfo, SCHEMA_SQL as SYNC_SCHEMA;`nimport rag_ingest;`nimport app as backend;`nensure_database();`nwith psycopg.connect(_pg_conninfo(), autocommit=True) as conn:`n    try:`n        conn.execute('CREATE EXTENSION IF NOT EXISTS vector')`n        print('pgvector extension: ok')`n    except Exception as e:`n        print('PGVECTOR MISSING:', str(e)[:300])`n        raise SystemExit(10)`nwith psycopg.connect(_pg_conninfo()) as conn:`n    conn.execute(SYNC_SCHEMA)`n    conn.execute(rag_ingest.SCHEMA_SQL)`n    backend._ensure_current(conn)`n    backend._ensure_rag_threads(conn)`n    conn.commit()`nprint('schemas ready: snow_incident, sync_state, rag_ticket, current_incident, rag_thread, rag_message')`""
if ($LASTEXITCODE -eq 10 -or $LASTEXITCODE -ne 0) {
  Write-Host ""
  Write-Host "pgvector extension is missing. Stock PostgreSQL Windows installers do NOT bundle it." -ForegroundColor Red
  Write-Host "Install it for your PostgreSQL major version, then re-run this script:" -ForegroundColor Yellow
  Write-Host "  1. https://github.com/pgvector/pgvector/releases -> download pgvector windows zip" -ForegroundColor Yellow
  Write-Host "  2. Copy bin/vector.dll -> <PostgreSQL>\lib, share\extension\vector* -> <PostgreSQL>\share\extension" -ForegroundColor Yellow
  Write-Host "  3. Restart PostgreSQL, re-run setup_windows.ps1" -ForegroundColor Yellow
  Write-Host "  Alternative: run Postgres via Docker: docker run -d --name pgvector -p 5432:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16" -ForegroundColor Yellow
  throw "pgvector setup required."
}
Ok "database + schemas ready"

# ---------------------------------------------------------------- 5. Ollama
Step "5/9  Ollama models (embed + chat, all local)"
if (-not $SkipOllamaPull) {
  try { ollama --version | Out-Null } catch { throw "Ollama not found. Install from https://ollama.com/download/windows, then re-run." }
  $embed = if ($env:OLLAMA_EMBED_MODEL) { $env:OLLAMA_EMBED_MODEL } else { "nomic-embed-text" }
  $chat  = if ($env:OLLAMA_CHAT_MODEL)  { $env:OLLAMA_CHAT_MODEL }  else { "llama3.1:8b" }
  ollama pull $embed
  ollama pull $chat
  Ok "ollama models pulled ($embed, $chat)"
} else { Warn "skipped ollama pull (flag -SkipOllamaPull)" }

# ---------------------------------------------------------------- 6. mirror incidents
Step "6/9  Mirroring ServiceNow incidents -> snow_incident (Sync tab)"
Invoke-Expression "$py sync_to_pg.py --once"
Ok "incident mirror done"

# ---------------------------------------------------------------- 7. RAG ingest
Step "7/9  Building RAG vectors -> rag_ticket (Agent chat tab)"
if ($RagLimit -gt 0) { Invoke-Expression "$py rag_ingest.py --once --limit $RagLimit" }
else { Invoke-Expression "$py rag_ingest.py --once" }
Ok "RAG ingest done"

# ---------------------------------------------------------------- 8. triage
Step "8/9  Triaging open tickets -> current_incident (Current tab)"
if ($SkipTriage) {
  Warn "skipped triage_pipeline (run later: python triage_pipeline.py --once --limit $TriageLimit)"
} else {
  Invoke-Expression "$py triage_pipeline.py --once --limit $TriageLimit"
  Ok "triage done"
}

# ---------------------------------------------------------------- 9. verify
Step "9/9  Verifying — these counts back the UI tabs"
Invoke-Expression "$py -c `"`nimport psycopg;`nfrom sync_to_pg import _pg_conninfo;`nwith psycopg.connect(_pg_conninfo()) as c:`n    for t in ['snow_incident','rag_ticket','current_incident','rag_thread']:`n        print(f'{t}:', c.execute(f'SELECT count(*) FROM {t}').fetchone()[0])`""

Write-Host ""
Write-Host "Expected on a healthy setup (your old machine): snow_incident=16  rag_ticket=488  current_incident=2" -ForegroundColor Cyan
Write-Host ""
Write-Host "Start the app:  $py -m uvicorn app:app --host 0.0.0.0 --port 8090" -ForegroundColor Green
Write-Host "Then open http://localhost:8090 and press Connect. Tabs backed by live ServiceNow" -ForegroundColor Green
Write-Host "(Incidents, Custom Table) need no DB; Sync/Current/Agent tabs need the counts above." -ForegroundColor Green
Write-Host ""
Write-Host "Early Warn tab: copy AIML\models\*.joblib + baseline.json from the old machine," -ForegroundColor Yellow
Write-Host "  or retrain:  $py AIML\train.py  ;  $py AIML\ml_train.py" -ForegroundColor Yellow
