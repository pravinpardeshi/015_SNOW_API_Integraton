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
import json
import hashlib
import threading
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

# ---------------------------------------------------------------------------
# Token cache: reuse the OAuth token for as long as possible.
# The token is persisted to a flat file so that:
#   1. it survives app restarts, and
#   2. other ServiceNow callers on this host can reuse it instead of
#      minting a new token on every call during its validity window.
# Override the location with SNOW_TOKEN_FILE if needed.
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.getenv(
    "SNOW_TOKEN_FILE", os.path.join(BASE_DIR, ".snow_token.json")
)
# Refresh slightly before real expiry so in-flight requests never use a
# token that dies mid-call.
TOKEN_EXPIRY_SKEW = int(os.getenv("SNOW_TOKEN_SKEW", "60"))

_token_lock = threading.Lock()

app = FastAPI(
    title="ServiceNow Manager",
    description="FastAPI backend + UI to interact with ServiceNow (incidents).",
    version="1.0.0",
)

# In-memory connection state (single-user demo; use sessions/DB for multi-user)
# Mirrored to TOKEN_FILE so the token survives restarts and can be shared
# with other ServiceNow callers on this host.
_state: dict[str, Any] = {
    "instance": DEFAULT_INSTANCE,
    "client_id": DEFAULT_CLIENT_ID,
    "client_secret": DEFAULT_CLIENT_SECRET,
    "access_token": None,
    "expires_at": 0.0,
    "token_type": "Bearer",
    "last_error": None,
}


# ---------------------------------------------------------------------------
# Token cache helpers (flat-file persistence + reuse)
# ---------------------------------------------------------------------------
def _hash_secret(secret: str) -> str:
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def _token_usable(expires_at: float) -> bool:
    """True if the token is still valid beyond the safety skew."""
    try:
        return bool(expires_at) and (time.time() + TOKEN_EXPIRY_SKEW) < float(expires_at)
    except (TypeError, ValueError):
        return False


def _load_token_file() -> Optional[dict]:
    """Read the cached token from disk. Returns None if missing/invalid."""
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return None
    if not isinstance(data, dict) or not data.get("access_token"):
        return None
    return data


def _save_token_file(
    *,
    access_token: str,
    expires_at: float,
    expires_in: int,
    instance: str,
    client_id: str,
    client_secret: str,
    token_type: str = "Bearer",
    scope: Optional[str] = None,
) -> None:
    """Atomically persist the token so other processes can reuse it."""
    payload = {
        "access_token": access_token,
        "expires_at": expires_at,
        "expires_in": expires_in,
        "token_type": token_type,
        "scope": scope,
        "instance": instance,
        "client_id": client_id,
        # Store only a hash — enough to detect credential changes
        # without writing the secret itself to disk.
        "client_secret_hash": _hash_secret(client_secret),
        "obtained_at": time.time(),
    }
    tmp_path = f"{TOKEN_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, TOKEN_FILE)
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _clear_token_file() -> None:
    try:
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
    except OSError:
        pass


def _promote_cached_to_state(cached: dict) -> None:
    """Copy a file-cached token into in-memory state (no secret in file)."""
    _state.update(
        {
            "instance": cached.get("instance") or _state["instance"],
            "client_id": cached.get("client_id") or _state.get("client_id"),
            "access_token": cached.get("access_token"),
            "expires_at": float(cached.get("expires_at") or 0),
            "token_type": cached.get("token_type") or "Bearer",
            "last_error": None,
        }
    )


def _sync_state_from_file() -> bool:
    """If memory has no usable token, try to pick up the file-cached one."""
    if _token_usable(_state.get("expires_at", 0)) and _state.get("access_token"):
        return True
    cached = _load_token_file()
    if cached and _token_usable(cached.get("expires_at", 0)):
        _promote_cached_to_state(cached)
        return True
    return False


def _store_token_payload(
    payload: dict, *, instance: str, client_id: str, client_secret: str
) -> int:
    """Save a fresh OAuth payload to memory + disk. Returns expires_in."""
    token = payload.get("access_token")
    if not token:
        raise HTTPException(status_code=502, detail=f"No access_token in response: {payload}")
    expires_in = int(payload.get("expires_in", 1800))
    expires_at = time.time() + expires_in  # raw expiry; skew applied on read
    token_type = payload.get("token_type", "Bearer")
    scope = payload.get("scope")
    _state.update(
        {
            "instance": instance,
            "client_id": client_id,
            "client_secret": client_secret,
            "access_token": token,
            "expires_at": expires_at,
            "token_type": token_type,
            "last_error": None,
        }
    )
    _save_token_file(
        access_token=token,
        expires_at=expires_at,
        expires_in=expires_in,
        instance=instance,
        client_id=client_id,
        client_secret=client_secret,
        token_type=token_type,
        scope=scope,
    )
    return expires_in


def get_valid_token(
    instance: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> tuple[str, bool]:
    """Return a usable (token, reused) pair, minting a new one only if needed.

    Reuse order: valid in-memory token -> valid flat-file token ->
    fresh OAuth request. Other ServiceNow callers can import this function
    (or just read TOKEN_FILE) instead of requesting a token per call.
    """
    with _token_lock:
        instance = _clean_instance(instance or _state.get("instance") or DEFAULT_INSTANCE)
        client_id = client_id or _state.get("client_id") or DEFAULT_CLIENT_ID
        client_secret = client_secret or _state.get("client_secret") or DEFAULT_CLIENT_SECRET

        # 1. Reuse in-memory token if it matches these credentials
        # (instance + client_id + secret must all match).
        if (
            _state.get("access_token")
            and _token_usable(_state.get("expires_at", 0))
            and _clean_instance(_state.get("instance", "")) == instance
            and (_state.get("client_id") or "") == (client_id or "")
            and _hash_secret(_state.get("client_secret") or "") == _hash_secret(client_secret or "")
        ):
            return _state["access_token"], True

        # 2. Reuse flat-file token (covers restarts + other processes that
        #    already refreshed it) when instance/client/secret still match.
        cached = _load_token_file()
        if cached and _token_usable(cached.get("expires_at", 0)):
            same_instance = _clean_instance(cached.get("instance", "")) == instance
            same_id = (cached.get("client_id") or "") == (client_id or "")
            same_secret = (cached.get("client_secret_hash") or "") == _hash_secret(client_secret or "")
            if same_instance and same_id and same_secret:
                _promote_cached_to_state(cached)
                # Keep the secret in memory for future auto-refreshes.
                _state["client_secret"] = client_secret
                return cached["access_token"], True

        # 3. No usable token — mint exactly one fresh token.
        if not client_id or not client_secret:
            raise HTTPException(
                status_code=401, detail="Not connected. POST /api/connect first."
            )
        payload = get_access_token(instance, client_id, client_secret)
        _store_token_payload(
            payload, instance=instance, client_id=client_id, client_secret=client_secret
        )
        return _state["access_token"], False


# Pick up a still-valid token persisted by a previous run at import time.
_sync_state_from_file()


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
    """Build auth headers, reusing the cached token and refreshing only if needed."""
    token, _reused = get_valid_token()
    instance = _clean_instance(_state["instance"])
    headers = {
        "Authorization": f"Bearer {token}",
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
    # Pick up a token persisted by another process/previous run, if any.
    _sync_state_from_file()
    connected = bool(_state.get("access_token")) and _token_usable(_state.get("expires_at", 0))
    return {
        "connected": connected,
        "instance": _state["instance"],
        "expires_in": max(0, int(_state["expires_at"] - time.time())) if _state["expires_at"] else None,
        "last_error": _state.get("last_error"),
    }


@app.post("/api/connect")
def api_connect(body: ConnectRequest):
    instance = _clean_instance(body.instance or _state.get("instance") or DEFAULT_INSTANCE)
    client_id = body.client_id or _state.get("client_id") or DEFAULT_CLIENT_ID
    client_secret = body.client_secret or _state.get("client_secret") or DEFAULT_CLIENT_SECRET
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="client_id and client_secret are required.")
    # Reuse the cached token when credentials are unchanged and it is
    # still valid — this avoids minting a new token on every Connect click.
    token, reused = get_valid_token(instance, client_id, client_secret)
    expires_in = max(0, int(_state["expires_at"] - time.time()))
    return {
        "ok": True,
        "instance": _state["instance"],
        "expires_in": expires_in,
        "token_type": _state.get("token_type", "Bearer"),
        "reused": reused,
    }


@app.post("/api/disconnect")
def api_disconnect():
    with _token_lock:
        _state["access_token"] = None
        _state["expires_at"] = 0.0
        _clear_token_file()
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
