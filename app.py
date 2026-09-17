"""
ServiceNow FastAPI backend.
Mirrors functionality from testServiceNow.py:
  - get_access_token (OAuth client_credentials)
  - get_incidents
  - create_incident
  - update_incident
Plus: get single incident, delete incident, connection status.

Run with:
    uvicorn app:app --host 0.0.0.0 --port 8090 --reload
or:
    python app.py
"""

import os
import time
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()  # read SNOW_* credentials from .env (never hardcoded)

# ---------------------------------------------------------------------------
# Config: everything comes from environment / .env — no secrets in code.
# ---------------------------------------------------------------------------
DEFAULT_INSTANCE = os.getenv("SNOW_INSTANCE", "https://dev389543.service-now.com")
DEFAULT_CLIENT_ID = os.getenv("SNOW_CLIENT_ID", "")
DEFAULT_CLIENT_SECRET = os.getenv("SNOW_CLIENT_SECRET", "")

REQUEST_TIMEOUT = 30

app = FastAPI(
    title="ServiceNow Manager",
    description="FastAPI backend + UI to interact with ServiceNow (incidents).",
    version="1.0.0",
)

# In-memory connection state (single-user demo; use sessions/DB for multi-user)
_state: dict[str, Any] = {
    "instance": DEFAULT_INSTANCE,
    "client_id": DEFAULT_CLIENT_ID,
    "client_secret": DEFAULT_CLIENT_SECRET,
    "access_token": None,
    "expires_at": 0.0,
    "last_error": None,
}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class ConnectRequest(BaseModel):
    instance: str = Field(default=DEFAULT_INSTANCE, description="e.g. https://devXXXXX.service-now.com")
    client_id: str = Field(default=DEFAULT_CLIENT_ID)
    client_secret: str = Field(default=DEFAULT_CLIENT_SECRET)


class CreateIncidentRequest(BaseModel):
    short_description: str = Field(..., min_length=1)
    description: Optional[str] = None
    urgency: Optional[str] = "2"
    impact: Optional[str] = None
    priority: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    caller_id: Optional[str] = None
    assignment_group: Optional[str] = None
    assigned_to: Optional[str] = None
    state: Optional[str] = None
    comments: Optional[str] = None
    work_notes: Optional[str] = None


class UpdateIncidentRequest(BaseModel):
    priority: Optional[str] = None
    assignment_group: Optional[str] = None
    description: Optional[str] = None
    comments: Optional[str] = None
    # extras beyond testServiceNow.py so UI is fully useful
    short_description: Optional[str] = None
    urgency: Optional[str] = None
    impact: Optional[str] = None
    state: Optional[str] = None
    assigned_to: Optional[str] = None
    work_notes: Optional[str] = None
    close_notes: Optional[str] = None
    close_code: Optional[str] = None


# ---------------------------------------------------------------------------
# ServiceNow helpers (ported from testServiceNow.py)
# ---------------------------------------------------------------------------
def _clean_instance(instance: str) -> str:
    instance = (instance or "").strip().rstrip("/")
    if not instance.startswith("http"):
        instance = f"https://{instance}"
    return instance


def get_access_token(instance: str, client_id: str, client_secret: str) -> dict:
    """Port of get_access_token() from testServiceNow.py. Returns full token payload."""
    instance = _clean_instance(instance)
    try:
        resp = requests.post(
            f"{instance}/oauth_token.do",
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach ServiceNow: {exc}")

    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Token request failed: {resp.text}",
        )

    try:
        return resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail=f"Non-JSON token response: {resp.text[:500]}")


def _auth_headers() -> tuple[str, dict]:
    if not _state["access_token"]:
        raise HTTPException(status_code=401, detail="Not connected. POST /api/connect first.")
    if _state["expires_at"] and time.time() > _state["expires_at"]:
        raise HTTPException(status_code=401, detail="Token expired. Reconnect via POST /api/connect.")
    instance = _clean_instance(_state["instance"])
    headers = {
        "Authorization": f"Bearer {_state['access_token']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    return instance, headers


def _raise_for_snow(resp: requests.Response, action: str):
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail={action: detail})


def _check_sys_id(sys_id: str) -> str:
    """Fail fast on malformed sys_ids (e.g. '[object Object]' from the UI)."""
    import re

    if not re.fullmatch(r"[0-9a-fA-F]{32}", sys_id or ""):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sys_id {sys_id!r}. Expected a 32-character ServiceNow sys_id. "
            "The record may not have loaded correctly — please refresh the list and retry.",
        )
    return sys_id


# ---------------------------------------------------------------------------
# Static / UI
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    index_path = os.path.join(TEMPLATES_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>templates/index.html not found</h1>", status_code=500)


# ---------------------------------------------------------------------------
# API: connection
# ---------------------------------------------------------------------------
@app.get("/api/status")
def api_status():
    connected = bool(_state["access_token"]) and not (
        _state["expires_at"] and time.time() > _state["expires_at"]
    )
    return {
        "connected": connected,
        "instance": _state["instance"],
        "expires_in": max(0, int(_state["expires_at"] - time.time())) if _state["expires_at"] else None,
        "last_error": _state.get("last_error"),
    }


@app.post("/api/connect")
def api_connect(body: ConnectRequest):
    instance = _clean_instance(body.instance)
    if not body.client_id or not body.client_secret:
        raise HTTPException(status_code=400, detail="client_id and client_secret are required.")
    payload = get_access_token(instance, body.client_id, body.client_secret)
    token = payload.get("access_token")
    if not token:
        raise HTTPException(status_code=502, detail=f"No access_token in response: {payload}")
    expires_in = int(payload.get("expires_in", 1800))
    _state.update(
        {
            "instance": instance,
            "client_id": body.client_id,
            "client_secret": body.client_secret,
            "access_token": token,
            "expires_at": time.time() + expires_in - 30,
            "last_error": None,
        }
    )
    return {
        "ok": True,
        "instance": instance,
        "expires_in": expires_in,
        "token_type": payload.get("token_type", "Bearer"),
    }


@app.post("/api/disconnect")
def api_disconnect():
    _state["access_token"] = None
    _state["expires_at"] = 0.0
    return {"ok": True}


# ---------------------------------------------------------------------------
# API: incidents
# ---------------------------------------------------------------------------
@app.get("/api/incidents")
def api_list_incidents(
    limit: int = Query(25, ge=1, le=250),
    offset: int = Query(0, ge=0),
    active_only: bool = Query(True),
    search: Optional[str] = Query(None, description="Free text searched in number/short_description"),
    order_by: str = Query("-opened_at"),
):
    """Port of get_incidents() from testServiceNow.py. Supports pagination via limit/offset."""
    instance, headers = _auth_headers()

    # Build sysparm_query. Original used active=true.
    query_parts: list[str] = []
    if active_only:
        query_parts.append("active=true")

    if search:
        s = search.replace("'", "")
        query_parts.append(f"numberLIKE{s}^ORshort_descriptionLIKE{s}")
    sysparm_query = "^".join(query_parts) if query_parts else None

    params: dict[str, Any] = {
        "sysparm_limit": limit + 1,  # fetch one extra to detect a next page
        "sysparm_offset": offset,
        "sysparm_fields": "sys_id,number,short_description,description,state,priority,urgency,impact,assigned_to,assignment_group,opened_at,updated_at",
    }
    if sysparm_query:
        params["sysparm_query"] = sysparm_query
    # order: Table API uses sysparm_query=ORDERBY... ; accept simple order_by param
    if order_by:
        direction = "ORDERBYDESC" if order_by.startswith("-") else "ORDERBY"
        field = order_by.lstrip("-")
        if sysparm_query:
            params["sysparm_query"] = f"{sysparm_query}^{direction}{field}"
        else:
            params["sysparm_query"] = f"{direction}{field}"

    try:
        resp = requests.get(
            f"{instance}/api/now/table/incident",
            headers={k: v for k, v in headers.items() if k != "Content-Type"},
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")

    _raise_for_snow(resp, "list incidents failed")

    rows = resp.json().get("result", [])
    # Table API returns no total count, so detect "next page" via the extra row.
    has_next = len(rows) > limit
    return {"result": rows[:limit], "has_next": has_next, "offset": offset, "limit": limit}


@app.get("/api/incidents/{sys_id}")
def api_get_incident(sys_id: str):
    _check_sys_id(sys_id)
    instance, headers = _auth_headers()
    try:
        resp = requests.get(
            f"{instance}/api/now/table/incident/{sys_id}",
            headers={k: v for k, v in headers.items() if k != "Content-Type"},
            params={"sysparm_display_value": "all"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")

    _raise_for_snow(resp, "get incident failed")

    return {"result": resp.json().get("result")}


@app.post("/api/incidents", status_code=201)
def api_create_incident(body: CreateIncidentRequest):
    """Port of create_incident() from testServiceNow.py."""
    instance, headers = _auth_headers()
    payload = {k: v for k, v in body.model_dump().items() if v is not None}

    if "short_description" not in payload:
        raise HTTPException(status_code=400, detail="short_description is required.")

    try:
        resp = requests.post(
            f"{instance}/api/now/table/incident",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")

    _raise_for_snow(resp, "create incident failed")

    return {"result": resp.json().get("result")}


@app.patch("/api/incidents/{sys_id}")
def api_update_incident(sys_id: str, body: UpdateIncidentRequest):
    """Port of update_incident() from testServiceNow.py."""
    _check_sys_id(sys_id)
    instance, headers = _auth_headers()
    payload = {k: v for k, v in body.model_dump().items() if v is not None}

    if not payload:
        raise HTTPException(status_code=400, detail="At least one field must be provided for update.")

    try:
        resp = requests.patch(
            f"{instance}/api/now/table/incident/{sys_id}",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")

    _raise_for_snow(resp, "update incident failed")

    return {"result": resp.json().get("result")}


@app.delete("/api/incidents/{sys_id}")
def api_delete_incident(sys_id: str):
    _check_sys_id(sys_id)
    instance, headers = _auth_headers()
    try:
        resp = requests.delete(
            f"{instance}/api/now/table/incident/{sys_id}",
            headers={k: v for k, v in headers.items() if k != "Content-Type"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")

    if resp.status_code not in (200, 204):
        _raise_for_snow(resp, "delete incident failed")

    return {"ok": True, "sys_id": sys_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8090, reload=True)
