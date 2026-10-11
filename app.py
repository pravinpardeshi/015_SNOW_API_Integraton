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
import sys
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
# Auth mode: "bearer" (OAuth token) or "basic" (per-request username/password).
# Basic is unaffected by the inbound-OAuth bearer-validation outage.
DEFAULT_AUTH_MODE = os.getenv("SNOW_AUTH", "bearer").strip().lower() or "bearer"
# Bearer grant: "password" (resource-owner, needs SNOW_USERNAME/SNOW_PASSWORD)
# or "client_credentials" (client id/secret only).
DEFAULT_GRANT_TYPE = os.getenv("SNOW_GRANT_TYPE", "password").strip().lower() or "password"
DEFAULT_USERNAME = os.getenv("SNOW_USERNAME", "")
# NOTE: the password itself is never given a code default — it must come from
# .env / shell env / the UI Connect form, and is never written to disk.

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


# ---------------------------------------------------------------------------
# Observability: Datadog-like in-memory telemetry (resets on restart).
# Per-minute buckets (last 180) for charts + cumulative per-endpoint stats.
# ---------------------------------------------------------------------------
START_TIME = time.time()
_metrics_lock = threading.Lock()
_req_buckets: dict[int, dict[str, float]] = {}
_endpoint_stats: dict[str, dict[str, float]] = {}
_recent_latencies: list[float] = []  # reservoir for p50/p95 (cap 500)
_recent_snow_lat: list[float] = []
_counters: dict[str, int] = {
    "snow_calls": 0, "snow_errors": 0,
    "token_mint": 0, "token_reuse": 0,
    "rag_ask": 0, "rag_ask_err": 0,
}
_lat_sums: dict[str, float] = {"rag": 0.0}
BUCKET_SPAN_MIN = 180
SKIP_METRIC_PREFIXES = ("/static", "/favicon")


def _bucket(now_min: Optional[int] = None) -> dict[str, float]:
    now_min = now_min if now_min is not None else int(time.time() // 60)
    with _metrics_lock:
        b = _req_buckets.get(now_min)
        if b is None:
            b = {"req": 0, "err": 0, "lat": 0.0, "snow": 0, "snow_err": 0,
                 "snow_lat": 0.0, "rag": 0, "rag_err": 0, "rag_lat": 0.0}
            _req_buckets[now_min] = b
        cutoff = now_min - BUCKET_SPAN_MIN
        for k in [k for k in _req_buckets if k < cutoff]:
            del _req_buckets[k]
    return b


def _record_request(route: str, status: int, elapsed_ms: float) -> None:
    with _metrics_lock:
        e = _endpoint_stats.get(route)
        if e is None:
            e = {"req": 0, "err": 0, "lat": 0.0, "max": 0.0}
            _endpoint_stats[route] = e
        e["req"] += 1
        e["lat"] += elapsed_ms
        e["max"] = max(e["max"], elapsed_ms)
        if status >= 400:
            e["err"] += 1
        _recent_latencies.append(elapsed_ms)
        del _recent_latencies[:-500]
    b = _bucket()
    with _metrics_lock:
        b["req"] += 1
        b["lat"] += elapsed_ms
        if status >= 400:
            b["err"] += 1


def _record_snow(status: int, elapsed_ms: float, is_rag: bool = False) -> None:
    with _metrics_lock:
        _counters["snow_calls"] += 1
        _recent_snow_lat.append(elapsed_ms)
        del _recent_snow_lat[:-500]
        if status >= 400:
            _counters["snow_errors"] += 1
    b = _bucket()
    with _metrics_lock:
        b["snow"] += 1
        b["snow_lat"] += elapsed_ms
        if status >= 400:
            b["snow_err"] += 1


def _record_token(reused: bool) -> None:
    with _metrics_lock:
        _counters["token_reuse" if reused else "token_mint"] += 1


def _record_rag(status: int, elapsed_ms: float) -> None:
    with _metrics_lock:
        _counters["rag_ask"] += 1
        _lat_sums["rag"] += elapsed_ms
        if status >= 400:
            _counters["rag_ask_err"] += 1
    b = _bucket()
    with _metrics_lock:
        b["rag"] += 1
        b["rag_lat"] += elapsed_ms
        if status >= 400:
            b["rag_err"] += 1


def _pctile(sorted_vals: list[float], pct: float) -> Optional[float]:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * pct / 100))
    return round(sorted_vals[idx], 1)


@app.middleware("http")
async def metrics_middleware(request, call_next):
    if request.url.path.startswith(SKIP_METRIC_PREFIXES):
        return await call_next(request)
    start = time.time()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", None) or request.url.path
        _record_request(f"{request.method} {route}", status, (time.time() - start) * 1000)

# In-memory connection state (single-user demo; use sessions/DB for multi-user)
# Mirrored to TOKEN_FILE so the token survives restarts and can be shared
# with other ServiceNow callers on this host.
_state: dict[str, Any] = {
    "instance": DEFAULT_INSTANCE,
    "client_id": DEFAULT_CLIENT_ID,
    "client_secret": DEFAULT_CLIENT_SECRET,
    "auth_mode": DEFAULT_AUTH_MODE,      # bearer | basic
    "grant_type": DEFAULT_GRANT_TYPE,    # password | client_credentials
    "username": DEFAULT_USERNAME,
    "password": os.getenv("SNOW_PASSWORD", ""),
    "basic_paused": False,  # Disconnect() pauses basic mode until next Connect
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
    grant_type: str = "client_credentials",
    username: str = "",
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
        "grant_type": grant_type,
        "username": username,
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
            "grant_type": cached.get("grant_type") or _state.get("grant_type"),
            "username": cached.get("username") or _state.get("username"),
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
    payload: dict,
    *,
    instance: str,
    client_id: str,
    client_secret: str,
    grant_type: str = "client_credentials",
    username: str = "",
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
        grant_type=grant_type,
        username=username,
    )
    return expires_in


def get_valid_token(
    instance: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    force_refresh: bool = False,
    grant_type: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> tuple[str, bool]:
    """Return a usable (token, reused) pair, minting a new one only if needed.

    Reuse order: valid in-memory token -> valid flat-file token ->
    fresh OAuth request. Other ServiceNow callers can import this function
    (or just read TOKEN_FILE) instead of requesting a token per call.
    Pass force_refresh=True to skip the caches (e.g. after a 401, where
    ServiceNow rejected the cached token server-side despite it looking
    time-valid).
    """
    with _token_lock:
        instance = _clean_instance(instance or _state.get("instance") or DEFAULT_INSTANCE)
        client_id = client_id or _state.get("client_id") or DEFAULT_CLIENT_ID
        client_secret = client_secret or _state.get("client_secret") or DEFAULT_CLIENT_SECRET
        grant_type = (grant_type or _state.get("grant_type") or DEFAULT_GRANT_TYPE).strip().lower()
        username = username or _state.get("username") or DEFAULT_USERNAME
        # Password is never defaulted from code — .env / env / Connect form only.
        password = password or _state.get("password") or os.getenv("SNOW_PASSWORD", "")

        # 1. Reuse in-memory token if it matches these credentials
        # (instance + client_id + secret + grant + username must all match).
        if (
            not force_refresh
            and _state.get("access_token")
            and _token_usable(_state.get("expires_at", 0))
            and _clean_instance(_state.get("instance", "")) == instance
            and (_state.get("client_id") or "") == (client_id or "")
            and _hash_secret(_state.get("client_secret") or "") == _hash_secret(client_secret or "")
            and (_state.get("grant_type") or "") == grant_type
            and (_state.get("username") or "") == (username or "")
        ):
            _record_token(True)
            return _state["access_token"], True

        # 2. Reuse flat-file token (covers restarts + other processes that
        #    already refreshed it) when instance/client/secret/grant still match.
        if not force_refresh:
            cached = _load_token_file()
            if cached and _token_usable(cached.get("expires_at", 0)):
                same_instance = _clean_instance(cached.get("instance", "")) == instance
                same_id = (cached.get("client_id") or "") == (client_id or "")
                same_secret = (cached.get("client_secret_hash") or "") == _hash_secret(client_secret or "")
                # Old cache files predate grant tracking: only match when the
                # stored grant agrees (or is absent and we want client_credentials).
                stored_grant = (cached.get("grant_type") or "client_credentials")
                same_grant = stored_grant == grant_type
                same_user = (cached.get("username") or "") == (username or "")
                if same_instance and same_id and same_secret and same_grant and same_user:
                    _promote_cached_to_state(cached)
                    # Keep the secrets in memory for future auto-refreshes.
                    _state["client_secret"] = client_secret
                    _state["password"] = password
                    _record_token(True)
                    return cached["access_token"], True

        # 3. No usable token — mint exactly one fresh token.
        if not client_id or not client_secret:
            raise HTTPException(
                status_code=401, detail="Not connected. POST /api/connect first."
            )
        if grant_type == "password" and (not username or not password):
            raise HTTPException(
                status_code=401,
                detail="Password grant needs SNOW_USERNAME/SNOW_PASSWORD — add them to .env or the Connect form.",
            )
        payload = get_access_token(
            instance, client_id, client_secret,
            grant_type=grant_type, username=username, password=password,
        )
        _store_token_payload(
            payload, instance=instance, client_id=client_id, client_secret=client_secret,
            grant_type=grant_type, username=username,
        )
        # Remember password only in memory (never on disk) for auto-refresh.
        _state["password"] = password
        _record_token(False)
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
    auth_mode: str = Field(default=DEFAULT_AUTH_MODE, description="bearer (OAuth token) or basic (username/password per request)")
    grant_type: str = Field(default=DEFAULT_GRANT_TYPE, description="bearer-mode grant: password or client_credentials")
    username: str = Field(default=DEFAULT_USERNAME, description="password grant + basic auth user")
    password: str = Field(default="", description="password grant + basic auth password (never stored on disk)")


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


def get_access_token(
    instance: str,
    client_id: str,
    client_secret: str,
    grant_type: str = "client_credentials",
    username: str = "",
    password: str = "",
) -> dict:
    """Mint an OAuth token. Returns full token payload.

    grant_type "password" uses the resource-owner password grant (needs a
    username + password and an entity that allows it); anything else uses
    the client-credentials grant.
    """
    instance = _clean_instance(instance)
    grant_type = (grant_type or "client_credentials").strip().lower()
    if grant_type == "password":
        data = {
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password,
        }
        auth = None
    else:
        data = {"grant_type": "client_credentials"}
        auth = (client_id, client_secret)
    try:
        resp = requests.post(
            f"{instance}/oauth_token.do",
            data=data,
            auth=auth,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
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


def _current_auth_mode() -> str:
    return (_state.get("auth_mode") or DEFAULT_AUTH_MODE).strip().lower() or "bearer"


def _basic_creds() -> tuple[str, str]:
    if _state.get("basic_paused"):
        raise HTTPException(
            status_code=401, detail="Not connected. POST /api/connect first."
        )
    username = _state.get("username") or DEFAULT_USERNAME
    password = _state.get("password") or os.getenv("SNOW_PASSWORD", "")
    if not username or not password:
        raise HTTPException(
            status_code=401,
            detail="Basic mode needs SNOW_USERNAME/SNOW_PASSWORD — add them to .env or the Connect form, then Connect.",
        )
    return username, password


def _auth_headers() -> tuple[str, dict]:
    """Build auth headers for the active mode (bearer token or basic).

    Basic mode needs no token — returns headers without Authorization;
    the (username, password) pair travels via _basic_auth().
    """
    if _current_auth_mode() == "basic":
        _basic_creds()  # fail fast with a clear message when creds are missing
        instance = _clean_instance(_state["instance"])
        return instance, {"Accept": "application/json", "Content-Type": "application/json"}
    token, _reused = get_valid_token()
    instance = _clean_instance(_state["instance"])
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    return instance, headers


def _basic_auth() -> Optional[tuple[str, str]]:
    """requests auth=(user, pass) pair in basic mode, else None."""
    if _current_auth_mode() == "basic":
        return _basic_creds()
    return None


def _raise_for_snow(resp: requests.Response, action: str):
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail={action: detail})


def _invalidate_token() -> None:
    """Drop the cached token (memory + file) so the next call mints a fresh one."""
    with _token_lock:
        _state["access_token"] = None
        _state["expires_at"] = 0.0
        _clear_token_file()


def _snow_request(
    method: str,
    path: str,
    *,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
    include_content_type: bool = False,
) -> requests.Response:
    """Send one Table API request, retrying once with a fresh token on 401.

    Bearer mode: the time-based cache can't know when ServiceNow
    revokes/invalidates a token server-side, so a 401 triggers exactly one
    forced refresh + retry. If the retry is also 401, the 401 is raised with
    a pointer to the server-side checks.
    Basic mode: credentials go on every request; a 401 means the username /
    password itself was rejected — raised directly with a credential hint.
    """
    instance, headers = _auth_headers()
    basic = _basic_auth()
    if not include_content_type:
        headers = {k: v for k, v in headers.items() if k != "Content-Type"}
    url = f"{instance}{path}"
    t0 = time.time()
    try:
        resp = requests.request(
            method, url, headers=headers, auth=basic, params=params, json=json_body,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")
    _record_snow(resp.status_code, (time.time() - t0) * 1000)
    if resp.status_code != 401:
        return resp
    if basic is not None:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise HTTPException(
            status_code=401,
            detail=(
                f"ServiceNow rejected the basic-auth credentials: {detail}. "
                "Check SNOW_USERNAME/SNOW_PASSWORD (typo? expired? locked?), "
                "then Disconnect + Connect again."
            ),
        )
    # Cached token rejected server-side — mint one fresh token and retry once.
    _invalidate_token()
    # Re-auth with forced refresh (bypasses both caches).
    token, _reused = get_valid_token(force_refresh=True)
    instance = _clean_instance(_state["instance"])
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if not include_content_type:
        headers = {k: v for k, v in headers.items() if k != "Content-Type"}
    url = f"{instance}{path}"
    t0 = time.time()
    try:
        resp = requests.request(
            method, url, headers=headers, auth=None, params=params, json=json_body,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")
    _record_snow(resp.status_code, (time.time() - t0) * 1000)
    if resp.status_code == 401:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise HTTPException(
            status_code=401,
            detail=(
                f"ServiceNow rejected even a freshly minted token: {detail}. "
                "This is server-side, not a stale cache. As admin check: "
                "1) System OAuth > Application Registry > your app: Active, "
                "Client Credentials grant enabled, OAuth Application User set to an "
                "active/unlocked user (e.g. python_api). "
                "2) That user: Active=true, Locked out=false, has "
                "snc_platform_rest_api_access (rest_service is deprecated). "
                "3) If the app enforces token restriction / REST API Auth Scope, "
                "allow Table API for this client. Then POST /api/disconnect + "
                "POST /api/connect and retry."
            ),
        )
    return resp


def _snow_total(table: str, sysparm_query: Optional[str]) -> Optional[int]:
    """Exact record count via the Stats API (/api/now/stats/{table}).

    Same filter as the list call, minus any ORDERBY clauses (ordering is
    meaningless for counts). Returns None on any failure so callers can
    fall back to next-page-only pagination.
    """
    _check_table_name(table)
    query = sysparm_query or ""
    # Strip ORDERBY... segments: they sort, they don't filter.
    kept = [p for p in query.split("^") if p and not p.startswith("ORDERBY")]
    params: dict[str, Any] = {"sysparm_count": "true"}
    if kept:
        params["sysparm_query"] = "^".join(kept)
    try:
        resp = _snow_request("GET", f"/api/now/stats/{table}", params=params)
    except HTTPException:
        return None
    if resp.status_code != 200:
        return None
    try:
        count = resp.json().get("result", {}).get("stats", {}).get("count")
        return int(count) if count is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


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
    auth_mode = _current_auth_mode()
    if auth_mode == "basic":
        if _state.get("basic_paused"):
            connected = False
        else:
            username = _state.get("username") or DEFAULT_USERNAME
            password = _state.get("password") or os.getenv("SNOW_PASSWORD", "")
            connected = bool(username and password)
    else:
        connected = bool(_state.get("access_token")) and _token_usable(_state.get("expires_at", 0))
    return {
        "connected": connected,
        "instance": _state["instance"],
        "auth_mode": auth_mode,
        "grant_type": _state.get("grant_type") or DEFAULT_GRANT_TYPE,
        "username": _state.get("username") or DEFAULT_USERNAME,
        "expires_in": max(0, int(_state["expires_at"] - time.time())) if _state["expires_at"] else None,
        "last_error": _state.get("last_error"),
    }


@app.post("/api/connect")
def api_connect(body: ConnectRequest):
    instance = _clean_instance(body.instance or _state.get("instance") or DEFAULT_INSTANCE)
    auth_mode = (body.auth_mode or _state.get("auth_mode") or DEFAULT_AUTH_MODE).strip().lower() or "bearer"
    if auth_mode not in ("bearer", "basic"):
        raise HTTPException(status_code=400, detail="auth_mode must be 'bearer' or 'basic'.")
    grant_type = (body.grant_type or _state.get("grant_type") or DEFAULT_GRANT_TYPE).strip().lower() or "password"
    if grant_type not in ("password", "client_credentials"):
        raise HTTPException(status_code=400, detail="grant_type must be 'password' or 'client_credentials'.")
    username = body.username or _state.get("username") or DEFAULT_USERNAME
    # Password: form value wins, else keep memory, else env. Never logged/stored on disk.
    password = body.password or _state.get("password") or os.getenv("SNOW_PASSWORD", "")

    with _token_lock:
        _state["instance"] = instance
        _state["auth_mode"] = auth_mode
        _state["basic_paused"] = False
        _state["grant_type"] = grant_type
        _state["username"] = username
        _state["password"] = password

    if auth_mode == "basic":
        if not username or not password:
            raise HTTPException(
                status_code=400,
                detail="Basic mode needs a username and password — fill them in the Connect form or .env.",
            )
        return {
            "ok": True,
            "instance": instance,
            "auth_mode": "basic",
            "grant_type": grant_type,
            "username": username,
            "expires_in": None,
            "reused": False,
        }

    client_id = body.client_id or _state.get("client_id") or DEFAULT_CLIENT_ID
    client_secret = body.client_secret or _state.get("client_secret") or DEFAULT_CLIENT_SECRET
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="client_id and client_secret are required.")
    if grant_type == "password" and (not username or not password):
        raise HTTPException(
            status_code=400,
            detail="Password grant needs a username and password — fill them in the Connect form or .env.",
        )
    # Reuse the cached token when credentials are unchanged and it is
    # still valid — this avoids minting a new token on every Connect click.
    token, reused = get_valid_token(
        instance, client_id, client_secret,
        grant_type=grant_type, username=username, password=password,
    )
    expires_in = max(0, int(_state["expires_at"] - time.time()))
    return {
        "ok": True,
        "instance": _state["instance"],
        "auth_mode": "bearer",
        "grant_type": grant_type,
        "username": username,
        "expires_in": expires_in,
        "token_type": _state.get("token_type", "Bearer"),
        "reused": reused,
    }


@app.post("/api/disconnect")
def api_disconnect():
    with _token_lock:
        _state["access_token"] = None
        _state["expires_at"] = 0.0
        _state["password"] = ""  # memory-only secret; env value still applies on next connect
        _state["basic_paused"] = True  # basic mode stays off until next Connect
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
        resp = _snow_request("GET", "/api/now/table/incident", params=params)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")

    _raise_for_snow(resp, "list incidents failed")

    rows = resp.json().get("result", [])
    # Table API returns no total count, so detect "next page" via the extra row.
    has_next = len(rows) > limit
    # Exact total via the Stats API (same filter, no ordering). None -> UI falls back.
    total = _snow_total("incident", sysparm_query)
    pages = ((total + limit - 1) // limit) if total else None
    return {"result": rows[:limit], "has_next": has_next, "offset": offset, "limit": limit,
            "total": total, "pages": pages}


@app.get("/api/incidents/{sys_id}")
def api_get_incident(sys_id: str):
    _check_sys_id(sys_id)
    try:
        resp = _snow_request(
            "GET",
            f"/api/now/table/incident/{sys_id}",
            params={"sysparm_display_value": "all"},
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")

    _raise_for_snow(resp, "get incident failed")

    return {"result": resp.json().get("result")}


@app.post("/api/incidents", status_code=201)
def api_create_incident(body: CreateIncidentRequest):
    """Port of create_incident() from testServiceNow.py."""
    payload = {k: v for k, v in body.model_dump().items() if v is not None}

    if "short_description" not in payload:
        raise HTTPException(status_code=400, detail="short_description is required.")

    try:
        resp = _snow_request(
            "POST", "/api/now/table/incident",
            json_body=payload, include_content_type=True,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")

    _raise_for_snow(resp, "create incident failed")

    return {"result": resp.json().get("result")}


@app.patch("/api/incidents/{sys_id}")
def api_update_incident(sys_id: str, body: UpdateIncidentRequest):
    """Port of update_incident() from testServiceNow.py."""
    _check_sys_id(sys_id)
    payload = {k: v for k, v in body.model_dump().items() if v is not None}

    if not payload:
        raise HTTPException(status_code=400, detail="At least one field must be provided for update.")

    try:
        resp = _snow_request(
            "PATCH", f"/api/now/table/incident/{sys_id}",
            json_body=payload, include_content_type=True,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")

    _raise_for_snow(resp, "update incident failed")

    return {"result": resp.json().get("result")}


@app.delete("/api/incidents/{sys_id}")
def api_delete_incident(sys_id: str):
    _check_sys_id(sys_id)
    try:
        resp = _snow_request("DELETE", f"/api/now/table/incident/{sys_id}")
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")

    if resp.status_code not in (200, 204):
        _raise_for_snow(resp, "delete incident failed")

    return {"ok": True, "sys_id": sys_id}


def _check_table_name(table: str) -> str:
    """Validate a ServiceNow table name to prevent URL injection."""
    import re

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table or ""):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid table name {table!r}. Expected e.g. u_snow_incident_table.",
        )
    return table


# Candidate free-text fields for custom tables. Invalid fields are
# silently ignored by ServiceNow (verified live), so it is safe to
# OR across both stock and u_-prefixed names without knowing the schema.
CUSTOM_SEARCH_FIELDS = [
    "number",
    "short_description",
    "description",
    "name",
    "u_number",
    "u_short_description",
    "u_description",
    "u_name",
    "u_ticket_id",
    "u_summary",
]

# Table name -> (unix timestamp, [field names]). Free-text search is built
# ONLY from fields that actually exist: ServiceNow matches LIKE terms on
# missing fields against everything, so a blind OR across candidates
# silently returns the whole table (verified live on u_snow_incident_table).
_custom_field_cache: dict[str, tuple[float, list[str]]] = {}
_CUSTOM_FIELD_CACHE_TTL = 300


def _custom_table_fields(table: str) -> list[str]:
    """Field names actually present on a table (1-row probe, briefly cached)."""
    now = time.time()
    hit = _custom_field_cache.get(table)
    if hit and (now - hit[0]) < _CUSTOM_FIELD_CACHE_TTL:
        return hit[1]
    try:
        resp = _snow_request(
            "GET", f"/api/now/table/{table}",
            params={"sysparm_limit": 1, "sysparm_display_value": "false"},
        )
        fields = list((resp.json().get("result") or [{}])[0].keys()) if resp.status_code == 200 else []
    except HTTPException:
        fields = []
    if fields:
        _custom_field_cache[table] = (now, fields)
    return fields or list(CUSTOM_SEARCH_FIELDS)

DEFAULT_CUSTOM_TABLE = os.getenv("SNOW_CUSTOM_TABLE", "u_snow_incident_table")


def _build_custom_query(
    search: Optional[str], raw_query: Optional[str], active_only: bool,
    table: Optional[str] = None,
) -> Optional[str]:
    """Combine active filter + free-text search + raw encoded query."""
    parts: list[str] = []
    if active_only:
        parts.append("active=true")
    if search:
        s = search.strip().replace("'", "").replace("^", "")
        if not s:
            pass
        elif len(s) == 32 and all(c in "0123456789abcdefABCDEF" for c in s):
            parts.append(f"sys_id={s}")
        else:
            fields = CUSTOM_SEARCH_FIELDS
            if table:
                actual = set(_custom_table_fields(table))
                fields = [f for f in CUSTOM_SEARCH_FIELDS if f in actual] or CUSTOM_SEARCH_FIELDS
            or_clause = "^OR".join(f"{f}LIKE{s}" for f in fields)
            parts.append(or_clause)
    if raw_query:
        rq = raw_query.strip()
        if rq:
            parts.append(rq)
    return "^".join(parts) if parts else None


# ---------------------------------------------------------------------------
# API: generic custom tables (e.g. u_snow_incident_table)
# Schema-agnostic: no sysparm_fields by default so ALL columns come back
# and the UI renders dynamically.
# ---------------------------------------------------------------------------
@app.get("/api/custom/{table}")
def api_custom_list(
    table: str,
    limit: int = Query(25, ge=1, le=250),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None, description="Free text or 32-char sys_id"),
    query: Optional[str] = Query(
        None, description="Raw ServiceNow encoded query, e.g. u_priority=1^ORDERBYDESCsys_created_on"
    ),
    order_by: str = Query("", description="Shorthand sort, e.g. -sys_created_on"),
    fields: Optional[str] = Query(None, description="Comma-separated sysparm_fields; omit for all fields"),
    active_only: bool = Query(False),
    display_value: str = Query("all"),
):
    """List/query any table — built for u_snow_incident_table."""
    _check_table_name(table)

    filter_query = _build_custom_query(search, query, active_only, table=table)
    sysparm_query = filter_query
    if order_by:
        direction = "ORDERBYDESC" if order_by.startswith("-") else "ORDERBY"
        field = order_by.lstrip("-")
        if field:
            order_clause = f"{direction}{field}"
            sysparm_query = f"{sysparm_query}^{order_clause}" if sysparm_query else order_clause

    params: dict[str, Any] = {
        "sysparm_limit": limit + 1,
        "sysparm_offset": offset,
        "sysparm_display_value": display_value,
    }
    if sysparm_query:
        params["sysparm_query"] = sysparm_query
    if fields:
        params["sysparm_fields"] = fields

    try:
        resp = _snow_request("GET", f"/api/now/table/{table}", params=params)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")

    if resp.status_code == 403:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise HTTPException(
            status_code=403,
            detail=(
                f"ServiceNow denied access to table '{table}': {detail}. "
                "Fix in ServiceNow as admin: give the OAuth user read access — "
                "e.g. System Security > Access Control (ACL) > create/read ACLs for the table "
                "(requires role such as rest_service), or Table > Application Access / 'Allow access to this "
                "table via web services', or grant the OAuth app user an admin/custom role covering the table. "
                "Then retry from this UI."
            ),
        )
    _raise_for_snow(resp, f"list {table} failed")

    rows = resp.json().get("result", [])
    has_next = len(rows) > limit
    total = _snow_total(table, filter_query)
    pages = ((total + limit - 1) // limit) if total else None
    return {"result": rows[:limit], "has_next": has_next, "offset": offset, "limit": limit, "table": table,
            "total": total, "pages": pages}


@app.get("/api/custom/{table}/{sys_id}")
def api_custom_get(table: str, sys_id: str):
    _check_table_name(table)
    _check_sys_id(sys_id)
    try:
        resp = _snow_request(
            "GET", f"/api/now/table/{table}/{sys_id}",
            params={"sysparm_display_value": "all"},
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")
    if resp.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail=f"ServiceNow denied access to table '{table}'. Grant the OAuth user read access (ACL / Application Access) and retry.",
        )
    _raise_for_snow(resp, f"get {table} record failed")
    return {"result": resp.json().get("result"), "table": table}


@app.post("/api/custom/{table}", status_code=201)
def api_custom_create(table: str, body: dict):
    _check_table_name(table)
    if not isinstance(body, dict) or not body:
        raise HTTPException(status_code=400, detail="Request body must be a non-empty JSON object of field->value.")
    try:
        resp = _snow_request(
            "POST", f"/api/now/table/{table}",
            json_body=body, include_content_type=True,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")
    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail=f"ServiceNow denied write access to table '{table}'. Grant create/write ACLs and retry.")
    _raise_for_snow(resp, f"create {table} record failed")
    return {"result": resp.json().get("result"), "table": table}


@app.patch("/api/custom/{table}/{sys_id}")
def api_custom_update(table: str, sys_id: str, body: dict):
    _check_table_name(table)
    _check_sys_id(sys_id)
    if not isinstance(body, dict) or not body:
        raise HTTPException(status_code=400, detail="At least one field must be provided for update.")
    try:
        resp = _snow_request(
            "PATCH", f"/api/now/table/{table}/{sys_id}",
            json_body=body, include_content_type=True,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")
    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail=f"ServiceNow denied write access to table '{table}'. Grant write ACLs and retry.")
    _raise_for_snow(resp, f"update {table} record failed")
    return {"result": resp.json().get("result"), "table": table}


@app.delete("/api/custom/{table}/{sys_id}")
def api_custom_delete(table: str, sys_id: str):
    _check_table_name(table)
    _check_sys_id(sys_id)
    try:
        resp = _snow_request("DELETE", f"/api/now/table/{table}/{sys_id}")
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"ServiceNow request failed: {exc}")
    if resp.status_code not in (200, 204):
        if resp.status_code == 403:
            raise HTTPException(status_code=403, detail=f"ServiceNow denied delete on table '{table}'. Grant delete ACLs and retry.")
        _raise_for_snow(resp, f"delete {table} record failed")
    return {"ok": True, "sys_id": sys_id, "table": table}


@app.get("/api/custom-tables/default")
def api_custom_default():
    return {"table": DEFAULT_CUSTOM_TABLE}


@app.get("/api/sync/stats")
def api_sync_stats():
    """Local mirror stats: snow_incident rows + sync watermark (no ServiceNow call)."""
    import psycopg

    try:
        with psycopg.connect(_rag_pg_conninfo()) as conn:
            total = conn.execute("SELECT count(*) FROM snow_incident").fetchone()[0]
            wm = conn.execute(
                "SELECT value FROM sync_state WHERE key = 'incident_sys_updated_on'"
            ).fetchone()
            latest = conn.execute("SELECT max(synced_at) FROM snow_incident").fetchone()[0]
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Sync DB unreachable (ran sync_to_pg.py yet?): {str(exc)[:200]}"
        )
    return {
        "rows": total,
        "watermark": wm[0] if wm else None,
        "latest_sync": str(latest) if latest else None,
    }


# ---------------------------------------------------------------------------
# API: observability — Datadog-like service metrics (in-memory, reset on restart).
# ---------------------------------------------------------------------------
def _host_stats() -> dict:
    """Lightweight host telemetry via /proc (Linux); degrades gracefully."""
    stats: dict = {"load_1m": None, "mem_total_mb": None, "mem_avail_mb": None}
    try:
        with open("/proc/loadavg") as f:
            stats["load_1m"] = float(f.read().split()[0])
    except OSError:
        pass
    try:
        mem: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].rstrip(":") in ("MemTotal", "MemAvailable"):
                    mem[parts[0].rstrip(":")] = int(parts[1])
        if mem:
            stats["mem_total_mb"] = mem.get("MemTotal", 0) // 1024
            stats["mem_avail_mb"] = mem.get("MemAvailable", 0) // 1024
    except OSError:
        pass
    return stats


@app.get("/api/metrics/summary")
def api_metrics_summary():
    """Service overview: uptime, request/error/latency stats, SNOW/token/RAG/pipeline health."""
    with _metrics_lock:
        total_req = sum(e["req"] for e in _endpoint_stats.values())
        total_err = sum(e["err"] for e in _endpoint_stats.values())
        lat_sorted = sorted(_recent_latencies)
        snow_sorted = sorted(_recent_snow_lat)
        counters = dict(_counters)
        endpoints = {
            route: {
                "req": int(e["req"]),
                "err": int(e["err"]),
                "avg_ms": round(e["lat"] / e["req"], 1) if e["req"] else None,
                "max_ms": round(e["max"], 1),
            }
            for route, e in sorted(_endpoint_stats.items(), key=lambda kv: -kv[1]["req"])
        }
    snow_calls = counters["snow_calls"]
    rag_asks = counters["rag_ask"]
    try:
        ollama_up = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5).status_code == 200
    except requests.RequestException:
        ollama_up = False
    mirror: dict = {}
    try:
        mirror = api_sync_stats()
    except HTTPException as exc:
        mirror = {"error": str(exc.detail)[:120]}
    rag_db: dict = {}
    try:
        rag_db = api_rag_stats()
    except HTTPException as exc:
        rag_db = {"error": str(exc.detail)[:120]}
    return {
        "uptime_s": int(time.time() - START_TIME),
        "requests": total_req,
        "errors": total_err,
        "error_pct": round(total_err / total_req * 100, 2) if total_req else 0.0,
        "latency_ms": {
            "p50": _pctile(lat_sorted, 50),
            "p95": _pctile(lat_sorted, 95),
        },
        "snow": {
            "calls": snow_calls,
            "errors": counters["snow_errors"],
            "error_pct": round(counters["snow_errors"] / snow_calls * 100, 2) if snow_calls else 0.0,
            "p50_ms": _pctile(snow_sorted, 50),
            "p95_ms": _pctile(snow_sorted, 95),
        },
        "tokens": {"minted": counters["token_mint"], "reused": counters["token_reuse"]},
        "rag": {
            "asks": rag_asks,
            "errors": counters["rag_ask_err"],
            "avg_ms": round(_lat_sums["rag"] / rag_asks, 0) if rag_asks else None,
        },
        "mirror": mirror,
        "rag_db": rag_db,
        "ollama_up": ollama_up,
        "host": _host_stats(),
        "endpoints": endpoints,
    }


@app.get("/api/metrics/series")
def api_metrics_series(minutes: int = Query(60, ge=5, le=180)):
    """Per-minute buckets for charts: oldest -> newest."""
    now_min = int(time.time() // 60)
    labels, req, err, lat, snow, snow_lat, rag = [], [], [], [], [], [], []
    with _metrics_lock:
        for m in range(now_min - minutes + 1, now_min + 1):
            b = _req_buckets.get(m)
            labels.append(time.strftime("%H:%M", time.localtime(m * 60)))
            req.append(b["req"] if b else 0)
            err.append(b["err"] if b else 0)
            lat.append(round(b["lat"] / b["req"], 1) if b and b["req"] else 0)
            snow.append(b["snow"] if b else 0)
            snow_lat.append(round(b["snow_lat"] / b["snow"], 1) if b and b["snow"] else 0)
            rag.append(b["rag"] if b else 0)
    return {
        "labels": labels, "req_per_min": req, "err_per_min": err,
        "avg_latency_ms": lat, "snow_per_min": snow,
        "snow_latency_ms": snow_lat, "rag_per_min": rag,
    }


# ---------------------------------------------------------------------------
# API: RAG — root-cause analysis over CLOSED custom-table tickets.
# Corpus: rag_ticket (Postgres + pgvector), built by rag_ingest.py from
# u_snow_incident_table rows with Resolved status. Embeddings + generation
# are 100% local via Ollama (nomic-embed-text + llama3.1:8b by default).
# ---------------------------------------------------------------------------
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "llama3.1:8b")
RAG_TOP_K_DEFAULT = int(os.getenv("RAG_TOP_K", "5"))
RAG_CHAT_TIMEOUT = int(os.getenv("RAG_CHAT_TIMEOUT", "600"))


def _rag_pg_conninfo() -> str:
    """Libpq conninfo for the snowdb database (mirrors sync_to_pg.py)."""
    pg_conn = os.getenv("PG_CONN", "")
    if pg_conn:
        return pg_conn
    parts = [
        f"host={os.getenv('PGHOST', 'localhost')}",
        f"port={os.getenv('PGPORT', '5432')}",
        f"user={os.getenv('PGUSER', 'postgres')}",
        f"dbname={os.getenv('PGDATABASE', 'snowdb')}",
    ]
    if os.getenv("PGPASSWORD", ""):
        parts.append(f"password={os.getenv('PGPASSWORD')}")
    return " ".join(parts)


def _rag_embed(text: str) -> list[float]:
    """Embed one text with the SAME model rag_ingest.py used (else similarity is garbage)."""
    import rag_ingest

    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/embed",
            json={"model": rag_ingest.EMBED_MODEL, "input": text},
            timeout=int(os.getenv("RAG_EMBED_TIMEOUT", "120")),
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Ollama embed failed (is Ollama up at {OLLAMA_HOST}?): {exc}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Ollama embed failed: {resp.text[:300]}")
    try:
        return resp.json()["embeddings"][0]
    except (ValueError, KeyError, IndexError):
        raise HTTPException(status_code=502, detail="Ollama embed returned no vector.")


def _rag_resolve_ticket(user_input: str) -> tuple[dict, str]:
    """Resolve user input to (ticket_record, query_text).

    Ticket id (e.g. J2EE-0003) or 32-char sys_id -> fetched live from
    ServiceNow. Anything else -> treated as a free-text new-incident
    description and analyzed directly.
    """
    import rag_ingest

    s = (user_input or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail="Provide a ticket id (e.g. J2EE-0003), sys_id, or incident description.")
    is_sys_id = len(s) == 32 and all(c in "0123456789abcdefABCDEF" for c in s)
    if not is_sys_id and not any(c.isdigit() for c in s):
        return {"ticket_id": "", "summary": s[:120]}, s  # free text, not a ticket
    table = os.getenv("SNOW_CUSTOM_TABLE", "u_snow_incident_table")
    _check_table_name(table)
    if is_sys_id:
        resp = _snow_request("GET", f"/api/now/table/{table}/{s}", params={"sysparm_display_value": "all"})
    else:
        resp = _snow_request(
            "GET", f"/api/now/table/{table}",
            params={"sysparm_query": f"u_ticket_id={s}", "sysparm_limit": 2, "sysparm_display_value": "all"},
        )
        _raise_for_snow(resp, "ticket lookup failed")
        rows = resp.json().get("result", [])
        if not rows:
            raise HTTPException(status_code=404, detail=f"No ticket {s!r} in {table}. Try its full text instead.")
        return rows[0], rag_ingest.ticket_text(rows[0])
    _raise_for_snow(resp, "ticket lookup failed")
    rec = resp.json().get("result")
    if not rec:
        raise HTTPException(status_code=404, detail=f"No ticket {s!r} in {table}.")
    return rec, rag_ingest.ticket_text(rec)


def _rag_search(vec: list[float], top_k: int, exclude_sys_id: str = "") -> list[dict]:
    import psycopg

    try:
        with psycopg.connect(_rag_pg_conninfo()) as conn:
            rows = conn.execute(
                "SELECT sys_id, ticket_id, status, summary, text, solution, rca, "
                "1 - (embedding <=> %s::vector) AS similarity "
                "FROM rag_ticket WHERE sys_id != %s "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (vec, exclude_sys_id or "", vec, top_k),
            ).fetchall()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Vector search failed (ran rag_ingest.py yet? Is pgvector installed?): {str(exc)[:300]}",
        )
    return [
        {"sys_id": r[0], "ticket_id": r[1], "status": r[2], "summary": r[3],
         "text": r[4], "solution": r[5], "rca": r[6], "similarity": round(float(r[7]), 4)}
        for r in rows
    ]


def _rag_generate(query_text: str, similar: list[dict]) -> str:
    """Ask the local chat model for RCA + proposed solution, grounded ONLY in retrieved tickets."""
    context = "\n\n".join(
        f"[{t['ticket_id'] or t['sys_id']}] (similarity {t['similarity']})\n"
        f"Summary: {t['summary']}\nDetail: {(t['text'] or '')[:900]}\n"
        f"Historic root cause: {t['rca'] or 'n/a'}\nHistoric solution: {t['solution'] or 'n/a'}"
        for t in similar
    )
    prompt = (
        "You are a ServiceNow major-incident analyst. Using ONLY the historic resolved tickets below, "
        "write: 1) Likely Root Cause (2-4 sentences), 2) Proposed Solution (concrete numbered steps), "
        "3) Confidence (High/Medium/Low with one-line reason). "
        "Cite ticket ids like [J2EE-0003] for every claim. "
        "If the historic tickets do not support an answer, say so instead of inventing one.\n\n"
        f"NEW INCIDENT:\n{query_text[:2000]}\n\nHISTORIC RESOLVED TICKETS:\n{context}"
    )
    last_err: Optional[str] = None
    for attempt in (1, 2):  # CPU-local LLM occasionally stalls: one retry
        try:
            resp = requests.post(
                f"{OLLAMA_HOST}/api/chat",
                json={
                    "model": OLLAMA_CHAT_MODEL,
                    "stream": False,
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0.2, "num_predict": 500},
                    "keep_alive": "30m",
                },
                timeout=RAG_CHAT_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_err = f"Ollama chat failed (is Ollama up at {OLLAMA_HOST}?): {exc}"
            continue
        break
    else:
        raise HTTPException(status_code=502, detail=last_err or "Ollama chat failed.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Ollama chat failed: {resp.text[:300]}")
    try:
        return resp.json()["message"]["content"]
    except (ValueError, KeyError):
        raise HTTPException(status_code=502, detail="Ollama chat returned no content.")


class RagAskRequest(BaseModel):
    input: str = Field(..., min_length=1, description="Ticket id (J2EE-0003), sys_id, or free-text incident description")
    top_k: int = Field(default=RAG_TOP_K_DEFAULT, ge=1, le=20)


class RagChatMessage(BaseModel):
    role: str = Field(..., description="'user' or 'assistant'")
    content: str = Field(..., min_length=1, max_length=4000)


class RagChatRequest(BaseModel):
    messages: list[RagChatMessage] = Field(..., min_length=1, max_length=20)
    top_k: int = Field(default=RAG_TOP_K_DEFAULT, ge=1, le=20)
    thread_id: Optional[int] = Field(default=None, description="Existing thread to continue; omitted starts a new one")


RAG_THREAD_SQL = """
CREATE TABLE IF NOT EXISTS rag_thread (
    id          SERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS rag_message (
    id          SERIAL PRIMARY KEY,
    thread_id   INTEGER NOT NULL REFERENCES rag_thread(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    cited       JSONB,
    model       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Upgrades for pre-existing tables (IF NOT EXISTS = safe reruns):
ALTER TABLE rag_message ADD COLUMN IF NOT EXISTS cited JSONB;
ALTER TABLE rag_message ADD COLUMN IF NOT EXISTS agent_trace JSONB;
CREATE INDEX IF NOT EXISTS idx_rag_message_thread ON rag_message(thread_id, id);
"""


def _ensure_rag_threads(conn) -> None:
    conn.execute(RAG_THREAD_SQL)


def _thread_title(first_question: str) -> str:
    """Title = first question, single line, ~60 chars (chatgpt-style)."""
    one_line = " ".join((first_question or "").strip().split())
    return (one_line[:60] + "…") if len(one_line) > 60 else (one_line or "Untitled chat")


def _rag_chat_answer(history: list[dict], query_text: str, similar: list[dict]) -> str:
    """Multi-turn answer: conversation history + freshly retrieved tickets."""
    context = "\n\n".join(
        f"[{t['ticket_id'] or t['sys_id']}] (similarity {t['similarity']})\n"
        f"Summary: {t['summary']}\nDetail: {(t['text'] or '')[:900]}\n"
        f"Historic root cause: {t['rca'] or 'n/a'}\nHistoric solution: {t['solution'] or 'n/a'}"
        for t in similar
    )
    convo = "\n".join(
        f"{'Investigator' if m['role'] == 'user' else 'Analyst'}: {m['content'][:1500]}"
        for m in history[-8:]
    )
    prompt = (
        "You are a ServiceNow major-incident analyst chatting with an investigator. "
        "Continue the conversation helpfully. For factual claims about past incidents use ONLY the "
        "historic resolved tickets below and cite ticket ids like [J2EE-0003]. "
        "When asked for root cause or a fix, give: 1) Likely Root Cause, 2) Proposed Solution "
        "(concrete steps), 3) Confidence (High/Medium/Low + reason). "
        "Answer directly — do not repeat or restate the conversation transcript. "
        "If history is insufficient, say what extra detail would help instead of inventing facts.\n\n"
        f"CONVERSATION SO FAR:\n{convo}\n\n"
        f"HISTORIC RESOLVED TICKETS RELEVANT TO THE LATEST QUESTION:\n{context}"
        if similar else
        "You are a ServiceNow major-incident analyst chatting with an investigator. "
        "No historic tickets matched (empty store?) — say so and ask for details.\n\n"
        f"CONVERSATION SO FAR:\n{convo}"
    )
    _ = query_text  # retrieval already used it; history carries the question
    last_err: Optional[str] = None
    for _attempt in (1, 2):  # CPU-local LLM occasionally stalls: one retry
        try:
            resp = requests.post(
                f"{OLLAMA_HOST}/api/chat",
                json={
                    "model": OLLAMA_CHAT_MODEL,
                    "stream": False,
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0.2, "num_predict": 500},
                    "keep_alive": "30m",
                },
                timeout=RAG_CHAT_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_err = f"Ollama chat failed (is Ollama up at {OLLAMA_HOST}?): {exc}"
            continue
        break
    else:
        raise HTTPException(status_code=502, detail=last_err or "Ollama chat failed.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Ollama chat failed: {resp.text[:300]}")
    try:
        return resp.json()["message"]["content"]
    except (ValueError, KeyError):
        raise HTTPException(status_code=502, detail="Ollama chat returned no content.")


@app.get("/api/rag/threads")
def api_rag_threads(limit: int = Query(30, ge=1, le=100)):
    """Chat history list, newest first (title = first question)."""
    import psycopg

    try:
        with psycopg.connect(_rag_pg_conninfo()) as conn:
            _ensure_rag_threads(conn)
            rows = conn.execute(
                "SELECT t.id, t.title, t.updated_at, "
                "(SELECT count(*) FROM rag_message m WHERE m.thread_id = t.id) "
                "FROM rag_thread t ORDER BY t.updated_at DESC LIMIT %s",
                (limit,),
            ).fetchall()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Thread list failed: {str(exc)[:200]}")
    return {
        "threads": [
            {"id": r[0], "title": r[1], "updated_at": str(r[2]), "messages": r[3]}
            for r in rows
        ]
    }


@app.get("/api/rag/threads/{thread_id}")
def api_rag_thread(thread_id: int):
    """Full transcript of one thread (for reloading into the UI)."""
    import psycopg

    try:
        with psycopg.connect(_rag_pg_conninfo()) as conn:
            _ensure_rag_threads(conn)
            head = conn.execute(
                "SELECT id, title FROM rag_thread WHERE id = %s", (thread_id,)
            ).fetchone()
            if not head:
                raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found.")
            msgs = conn.execute(
                "SELECT role, content, cited, model FROM rag_message "
                "WHERE thread_id = %s ORDER BY id",
                (thread_id,),
            ).fetchall()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Thread load failed: {str(exc)[:200]}")
    return {
        "id": head[0],
        "title": head[1],
        "messages": [
            {"role": m[0], "content": m[1], "similar": m[2], "model": m[3]} for m in msgs
        ],
    }


@app.delete("/api/rag/threads")
def api_rag_threads_delete_all():
    """Delete ALL chat sessions (bulk clear)."""
    import psycopg

    try:
        with psycopg.connect(_rag_pg_conninfo()) as conn:
            _ensure_rag_threads(conn)
            cur = conn.execute("DELETE FROM rag_thread")
            conn.commit()
            return {"ok": True, "deleted": cur.rowcount}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Clear chats failed: {str(exc)[:200]}")


@app.delete("/api/rag/threads/{thread_id}")
def api_rag_thread_delete(thread_id: int):
    import psycopg

    try:
        with psycopg.connect(_rag_pg_conninfo()) as conn:
            _ensure_rag_threads(conn)
            cur = conn.execute("DELETE FROM rag_thread WHERE id = %s", (thread_id,))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Thread delete failed: {str(exc)[:200]}")
    return {"ok": True, "id": thread_id}


RAG_MAX_ITERS_DEFAULT = int(os.getenv("RAG_MAX_ITERS", "2"))

_AGENT_TOOL_NAMES = ("search_tickets", "fetch_ticket", "file_triage_note", "create_incident")

AGENT_TOOLS_SPEC = """You have these tools (one call per step, JSON only):
- search_tickets {"query": "...", "top_k": 3-8}: vector search over Resolved tickets. USE when confidence is Low/Medium and a refined query could help.
- fetch_ticket {"ticket_id": "J2EE-0003"}: full record of one historic ticket for deeper evidence.
- file_triage_note {"ticket_id": "J2EE-0003", "note": "..."}: APPENDS (never overwrites) a timestamped analyst note to that ticket's Triage field. USE only when the user asks to record/file something.
- create_incident {"short_description": "...", "description": "...", "urgency": "1|2|3"}: raises a NEW incident ticket. USE only when the user explicitly asks to raise/create one."""


def _agent_tool_call(tool: str, args: dict) -> dict:
    """Execute one agent tool call server-side. Returns JSON-serializable observation."""
    import rag_ingest

    if tool == "search_tickets":
        vec = _rag_embed(str(args.get("query", "")))
        hits = _rag_search(vec, max(1, min(8, int(args.get("top_k", 5)))))
        return {"tickets": [
            {"ticket_id": h["ticket_id"], "similarity": h["similarity"],
             "summary": (h["summary"] or "")[:300],
             "rca": (h["rca"] or "")[:500], "solution": (h["solution"] or "")[:500]}
            for h in hits
        ]}
    if tool == "fetch_ticket":
        table = os.getenv("SNOW_CUSTOM_TABLE", "u_snow_incident_table")
        tid = str(args.get("ticket_id", "")).strip()
        if not tid:
            return {"error": "ticket_id is required"}
        resp = _snow_request(
            "GET", f"/api/now/table/{table}",
            params={"sysparm_query": f"u_ticket_id={tid}", "sysparm_limit": 1,
                    "sysparm_display_value": "all"},
        )
        _raise_for_snow(resp, "fetch_ticket failed")
        rows = resp.json().get("result", [])
        if not rows:
            return {"error": f"ticket {tid} not found"}
        rec = rows[0]
        return {"ticket": {
            "ticket_id": rag_ingest.dv(rec.get("u_ticket_id")),
            "status": rag_ingest.dv(rec.get("u_status")),
            "summary": rag_ingest.dv(rec.get("u_summary")),
            "description": rag_ingest.dv(rec.get("u_description"))[:1500],
            "technology_stack": rag_ingest.dv(rec.get("u_technology_stack")),
            "environment": rag_ingest.dv(rec.get("u_environment")),
            "rca": rag_ingest.dv(rec.get("u_root_cause_analysis"))[:1000],
            "solution": rag_ingest.dv(rec.get("u_solution"))[:1000],
        }}
    if tool == "file_triage_note":
        import datetime

        table = os.getenv("SNOW_CUSTOM_TABLE", "u_snow_incident_table")
        tid = str(args.get("ticket_id", "")).strip()
        note = str(args.get("note", "")).strip()[:1000]
        if not tid or not note:
            return {"error": "ticket_id and note are required"}
        resp = _snow_request(
            "GET", f"/api/now/table/{table}",
            params={"sysparm_query": f"u_ticket_id={tid}", "sysparm_limit": 1,
                    "sysparm_fields": "sys_id,u_ticket_id,u_triage",
                    "sysparm_display_value": "all"},
        )
        _raise_for_snow(resp, "triage read failed")
        rows = resp.json().get("result", [])
        if not rows:
            return {"error": f"ticket {tid} not found"}
        import rag_ingest as _ri

        sys_id = _ri.dv(rows[0].get("sys_id"))
        _check_sys_id(sys_id)
        current = _ri.dv(rows[0].get("u_triage"))
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        updated = f"{current}\n\n[Knowledge Agent {stamp}] {note}".strip()[:4000]
        resp = _snow_request(
            "PATCH", f"/api/now/table/{table}/{sys_id}",
            json_body={"u_triage": updated}, include_content_type=True,
        )
        _raise_for_snow(resp, "triage write failed")
        return {"filed_on": tid, "note_chars": len(note)}
    if tool == "create_incident":
        short = str(args.get("short_description", "")).strip()
        if not short:
            return {"error": "short_description is required"}
        body = {"short_description": short[:160]}
        if args.get("description"):
            body["description"] = str(args["description"])[:2000]
        if str(args.get("urgency", "")) in ("1", "2", "3"):
            body["urgency"] = str(args["urgency"])
        resp = _snow_request("POST", "/api/now/table/incident",
                             json_body=body, include_content_type=True)
        _raise_for_snow(resp, "create_incident failed")
        created = resp.json().get("result", {})
        return {"number": created.get("number"), "sys_id": created.get("sys_id")}
    return {"error": f"unknown tool {tool!r}"}


def _agent_llm(messages: list[dict], num_predict: int = 300) -> str:
    """One chat-model call for the agent loop (short drafts, low temperature)."""
    last_err: Optional[str] = None
    for _attempt in (1, 2):  # cold/loaded model occasionally stalls: one retry
        try:
            resp = requests.post(
                f"{OLLAMA_HOST}/api/chat",
                json={
                    "model": OLLAMA_CHAT_MODEL,
                    "stream": False,
                    "messages": messages,
                    "options": {"temperature": 0.2, "num_predict": num_predict},
                    "keep_alive": "30m",
                },
                timeout=RAG_CHAT_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_err = f"Ollama chat failed: {exc}"
            continue
        break
    else:
        raise HTTPException(status_code=502, detail=last_err or "Ollama chat failed.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Ollama chat failed: {resp.text[:300]}")
    try:
        return resp.json()["message"]["content"]
    except (ValueError, KeyError):
        raise HTTPException(status_code=502, detail="Ollama chat returned no content.")


def _agent_parse_step(text: str) -> dict:
    """Parse the model's step: a JSON {type: answer|tool, ...} object.

    Uses balanced-brace scanning (regex can't handle the nested "args"
    object) over fenced blocks first, then the whole text. Falls back to a
    final Medium-confidence answer when unparseable.
    """
    candidates: list[dict] = []
    blocks = [text]
    import re

    fenced = re.findall(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        blocks = fenced
    decoder = json.JSONDecoder()
    for block in blocks:
        for match in re.finditer(r"\{", block):
            try:
                obj, _end = decoder.raw_decode(block[match.start():])
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") in ("answer", "tool"):
                candidates.append(obj)
                continue
            # Model shorthand: {"type": "<toolname>", ...rest as args} or
            # a bare {"answer": "..."} without type.
            if obj.get("type") in _AGENT_TOOL_NAMES:
                tool = obj.pop("type")
                thought = obj.pop("thought", "")
                conf = obj.pop("confidence", "Medium")
                candidates.append({"type": "tool", "thought": thought,
                                   "confidence": conf, "tool": tool, "args": obj})
            elif isinstance(obj.get("answer"), str):
                candidates.append({"type": "answer",
                                   "confidence": obj.get("confidence", "Medium"),
                                   "answer": obj["answer"]})
    if candidates:
        return candidates[-1]
    return {"type": "answer", "confidence": "Medium",
            "answer": text.strip() or "(empty answer)"}


def _detect_write_intent(text: str) -> str:
    """Deterministic intent route so explicit write requests can't be ignored.

    Returns "create_incident" | "file_triage_note" | "". The ReAct prompt
    then ORDERS the matching tool call — models otherwise tend to keep
    researching instead of acting.
    """
    import re

    t = (text or "").lower()
    if re.search(r"\b(raise|create|open|file|log)\b.{0,30}\bincident\b|\bincident\b.{0,30}\b(raise|create|open)\b", t):
        return "create_incident"
    if re.search(r"\b(file|record|add|save|note|triage)\b.{0,40}\b(note|triage|rca|finding)", t) or \
       re.search(r"\b(note|triage|rca|finding)\b.{0,40}\b(file|record|add|save|on the ticket|to the ticket)\b", t):
        return "file_triage_note"
    return ""


def _detect_chat_intent(text: str) -> str:
    """Classify a chat input before any retrieval/LLM work.

    Returns "greeting" | "help" | "thanks" | "analyze". Smalltalk must NEVER
    reach the RAG/agent loop: with no real question, retrieval returns random
    tickets and the model confabulates an investigation (hallucination).
    """
    import re

    t = (text or "").strip().lower()
    if not t:
        return "greeting"
    if re.fullmatch(
        r"(hi+|hii+|hello+|hey+|yo|sup|good\s?(morning|afternoon|evening|day)"
        r"|namaste|howdy|greetings)[.!,\s]*", t
    ):
        return "greeting"
    if re.search(
        r"\b(thank(s| you)?|thx|bye|goodbye|see you)\b", t
    ) and not any(c.isdigit() for c in t) and len(t) < 40:
        return "thanks"
    if re.search(
        r"\b(help|what can you do|how (do|does|can) (you|this|it)|capabilit|"
        r"what is this|who are you|how to use)\b", t
    ):
        return "help"
    return "analyze"


_INTENT_REPLIES = {
    "greeting": (
        "Hello! I'm your ServiceNow analyst. Give me a ticket id "
        "(e.g. J2EE-0003), paste an incident description, and I'll compare it "
        "against similar resolved tickets — or ask what I can do."
    ),
    "help": (
        "Here's what I can do:\n"
        "1. Analyse a ticket — send a ticket id (e.g. J2EE-0003) or describe symptoms; "
        "I return likely root cause + proposed fix, citing similar resolved tickets.\n"
        "2. File a triage note — ask me to record findings on a ticket.\n"
        "3. Raise an incident — ask me to create one with a short description.\n"
        "My answers come only from historic resolved tickets — if they don't cover "
        "your case, I'll say so explicitly instead of guessing."
    ),
    "thanks": "You're welcome! Send another ticket id or incident description any time.",
    "clarify": (
        "To recommend anything I need something concrete to match against — "
        "send a ticket id (e.g. J2EE-0003) or describe the symptoms "
        "(error text, component, environment). What incident are you looking at?"
    ),
}


def _needs_clarification(text: str) -> bool:
    """True for vague asks with nothing matchable (no ticket ref, no details).

    Such inputs retrieve near-random tickets and burn several slow CPU LLM
    calls ending in a confabulated answer — ask for specifics instead.
    """
    import re

    t = (text or "").strip()
    if len(t) >= 120:
        return False  # detailed description: analyse it
    if re.search(r"J2EE-\d+|[0-9a-fA-F]{32}", t):
        return False  # ticket reference: analyse it
    if any(c.isdigit() for c in t):
        return False  # versions/counts/ids: concrete enough
    if re.search(
        r"\b(solution|fix|root cause|rca|triage|analys|investig|recommend|suggest)\b",
        t, re.IGNORECASE,
    ):
        return True  # generic "suggest a solution" with no specifics
    return len(t) < 30


def _rag_agent_run(history: list[dict], top_k: int, max_iters: int) -> dict:
    """ReAct loop: retrieve -> draft -> self-check -> (tool | re-retrieve) -> answer.

    Returns {reply, similar(last retrieval), trace, actions}.
    """
    import rag_ingest

    query = history[-1]["content"]
    intent = _detect_chat_intent(query)
    if intent != "analyze":
        # Smalltalk/help: answer directly with zero retrieval and zero LLM
        # calls, so there is nothing to hallucinate from.
        return {
            "reply": _INTENT_REPLIES[intent],
            "similar": [],
            "trace": [{"iter": 0, "intent": intent, "action": None,
                       "confidence": "High", "retrieved": 0}],
            "actions": [],
        }
    if _needs_clarification(query):
        # Vague ask with nothing matchable: instant clarifier instead of
        # several minutes of CPU LLM calls over near-random tickets.
        return {
            "reply": _INTENT_REPLIES["clarify"],
            "similar": [],
            "trace": [{"iter": 0, "intent": "clarify", "action": None,
                       "confidence": "High", "retrieved": 0}],
            "actions": [],
        }
    trace: list[dict] = []
    actions: list[dict] = []
    evidence: list[dict] = []  # accumulated retrieved tickets
    seen_ids: set[str] = set()
    done_writes: dict = {}  # idempotency: write-tool signature -> observation
    write_intent = _detect_write_intent(query)
    final_answer = ""
    last_similar: list[dict] = []

    for it in range(1, max_iters + 1):
        vec = _rag_embed(query)
        fresh = [h for h in _rag_search(vec, top_k) if h["sys_id"] not in seen_ids]
        for h in fresh:
            seen_ids.add(h["sys_id"])
        evidence.extend(fresh)
        last_similar = fresh or last_similar
        ctx = "\n\n".join(
            f"[{t['ticket_id'] or t['sys_id']}] (sim {t['similarity']})\n"
            f"Summary: {t['summary']}\nDetail: {(t['text'] or '')[:450]}\n"
            f"RCA: {t['rca'] or 'n/a'}\nSolution: {t['solution'] or 'n/a'}"
            for t in evidence[-4:]
        )
        convo = "\n".join(
            f"{'Investigator' if m['role'] == 'user' else 'Analyst'}: {m['content'][:800]}"
            for m in history[-4:]
        )
        step_text = _agent_llm([{"role": "user", "content": (
            "You are a ServiceNow incident agent. Be terse: at most 2 sentences of "
            "reasoning, then EITHER answer "
            "OR call exactly one tool. End your reply with ONE fenced block, EXACTLY like this:\n"
            '```json {"type": "answer", "confidence": "High|Medium|Low", "answer": "..."}\n```\n'
            "or\n"
            '```json {"type": "tool", "thought": "...", "confidence": "High|Medium|Low", '
            '"tool": "search_tickets", "args": {"query": "...", "top_k": 3}}\n```\n'
            'Use "type": "tool" plus a separate "tool" name — never "type": "search_tickets".\n'
            + (f"ORDER: the user explicitly requested a write action — you MUST call the "
                f'"{write_intent}" tool (with complete args) before giving any final answer. '
                f"Do it in this step if the target is already clear.\n"
                if write_intent else "")
            + "Cite ticket ids like [J2EE-0003]. Never invent facts. "
            "Answer directly when evidence suffices; call a tool only when it adds value "
            "(refine search on Low confidence, fetch a ticket for depth, file/record only if asked).\n\n"
            f"{AGENT_TOOLS_SPEC}\n\nCONVERSATION:\n{convo}\n\n"
            f"EVIDENCE ({len(evidence)} tickets so far):\n{ctx or '(none yet)'}"
        )}])
        step = _agent_parse_step(step_text)
        entry: dict = {"iter": it, "thought": step.get("thought", ""),
                       "confidence": step.get("confidence", "Medium"),
                       "retrieved": len(fresh)}
        if step["type"] == "answer":
            final_answer = step.get("answer") or step_text
            entry["action"] = None
            trace.append(entry)
            break
        if it == max_iters:
            # Budget spent without an answer: force a closing answer-only call
            # over the accumulated evidence (never leak a tool-call block).
            done = "; ".join(
                f"{a.get('tool')} -> {a.get('number') or a.get('filed_on') or 'ok'}"
                for a in actions
            )
            closing = _agent_llm([{"role": "user", "content": (
                "Write the final incident analysis now. No tools, no JSON, no preamble about "
                "process — just: 1) Likely Root Cause (2-4 sentences), 2) Proposed Solution "
                "(concrete numbered steps), 3) Confidence (High/Medium/Low + one-line reason). "
                "Use ONLY the tickets below, cite ids like [J2EE-0003]."
                + (f" Close by confirming completed actions: {done}." if done else "")
                + f"\n\nQUESTION: {history[-1]['content'][:800]}\n\nEVIDENCE:\n{ctx or '(none)'}"
            )}], num_predict=500)
            parsed = _agent_parse_step(closing)
            final_answer = parsed.get("answer") or closing
            entry["action"] = None
            entry["forced_close"] = True
            trace.append(entry)
            break
        tool, args = step.get("tool", ""), step.get("args", {}) or {}
        entry["action"] = {"tool": tool, "args": args}
        # Idempotency: never execute the same write twice in one run. The
        # model sometimes re-issues create/file calls it already completed.
        sig = None
        if tool == "create_incident":
            sig = ("create", str(args.get("short_description", ""))[:120])
        elif tool == "file_triage_note":
            sig = ("triage", str(args.get("ticket_id", "")), str(args.get("note", ""))[:80])
        if sig is not None and sig in done_writes:
            obs = {"already_completed": True, **done_writes[sig]}
            entry["observation"] = json.dumps(obs)[:800] + " (deduped: not re-executed)"
            entry["deduped"] = True
            trace.append(entry)
            history = history + [{"role": "assistant",
                                  "content": f"[internal] {tool} already completed this run: "
                                             f"{json.dumps(obs)[:400]}. Answer now."}]
            continue
        try:
            obs = _agent_tool_call(tool, args)
        except HTTPException as exc:
            obs = {"error": str(exc.detail)[:300]}
        entry["observation"] = json.dumps(obs)[:800]
        trace.append(entry)
        if tool in ("file_triage_note", "create_incident") and "error" not in obs:
            actions.append({"tool": tool, **obs})
            if sig is not None:
                done_writes[sig] = {k: v for k, v in obs.items() if k != "error"}
        # Next iteration reasons over the observation; refined query when supplied.
        history = history + [{"role": "assistant",
                              "content": f"[internal thought] {step.get('thought','')}\n"
                                         f"[tool {tool} observation] {json.dumps(obs)[:1200]}"}]
        if tool == "search_tickets" and isinstance(obs.get("tickets"), list):
            query = history[-1]["content"]  # observation already in context; keep digging
    return {"reply": final_answer, "similar": last_similar, "trace": trace, "actions": actions}


class RagAgentRequest(BaseModel):
    messages: list[RagChatMessage] = Field(..., min_length=1, max_length=20)
    top_k: int = Field(default=RAG_TOP_K_DEFAULT, ge=1, le=20)
    max_iters: int = Field(default=RAG_MAX_ITERS_DEFAULT, ge=1, le=5)
    thread_id: Optional[int] = Field(default=None)


@app.post("/api/rag/agent")
def api_rag_agent(body: RagAgentRequest):
    """Agentic RCA: ReAct loop with tools (search/fetch/file-triage/create-incident).

    Same persistence as /chat (thread titled by first question + agent_trace).
    """
    t0 = time.time()
    status = 200
    try:
        history = [{"role": m.role, "content": m.content} for m in body.messages]
        if any(m["role"] not in ("user", "assistant") for m in history):
            raise HTTPException(status_code=400, detail="roles must be 'user' or 'assistant'.")
        if not history or history[-1]["role"] != "user" or not history[-1]["content"].strip():
            raise HTTPException(status_code=400, detail="The last message must be a non-empty user message.")
        out = _rag_agent_run(history, body.top_k, body.max_iters)
        similar_out = [
            {k: t[k] for k in ("ticket_id", "status", "summary", "similarity", "solution", "rca")}
            for t in out["similar"]
        ]
        import psycopg

        try:
            with psycopg.connect(_rag_pg_conninfo()) as conn:
                _ensure_rag_threads(conn)
                with conn.cursor() as cur:
                    if body.thread_id:
                        exists = cur.execute(
                            "SELECT id FROM rag_thread WHERE id = %s", (body.thread_id,)
                        ).fetchone()
                        if not exists:
                            raise HTTPException(status_code=404, detail=f"Thread {body.thread_id} not found.")
                        thread_id = body.thread_id
                        first_q = cur.execute(
                            "SELECT content FROM rag_message WHERE thread_id = %s AND role = 'user' "
                            "ORDER BY id LIMIT 1", (thread_id,),
                        ).fetchone()
                        title = _thread_title(first_q[0] if first_q else history[-1]["content"])
                    else:
                        first_user = next((m["content"] for m in history if m["role"] == "user"),
                                          history[-1]["content"])
                        title = _thread_title(first_user)
                        thread_id = cur.execute(
                            "INSERT INTO rag_thread (title) VALUES (%s) RETURNING id", (title,),
                        ).fetchone()[0]
                    cur.execute(
                        "INSERT INTO rag_message (thread_id, role, content) VALUES (%s,'user',%s)",
                        (thread_id, history[-1]["content"]),
                    )
                    cur.execute(
                        "INSERT INTO rag_message (thread_id, role, content, cited, model, agent_trace) "
                        "VALUES (%s,'assistant',%s,%s,%s,%s)",
                        (thread_id, out["reply"], json.dumps(similar_out),
                         OLLAMA_CHAT_MODEL, json.dumps(out["trace"])),
                    )
                    cur.execute(
                        "UPDATE rag_thread SET updated_at = now(), title = %s WHERE id = %s",
                        (title, thread_id),
                    )
                conn.commit()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Agent history save failed: {str(exc)[:200]}")
        return {
            "reply": out["reply"], "similar": similar_out, "trace": out["trace"],
            "actions": out["actions"], "model": OLLAMA_CHAT_MODEL,
            "thread_id": thread_id, "thread_title": title,
        }
    except HTTPException as exc:
        status = exc.status_code
        raise
    finally:
        _record_rag(status, (time.time() - t0) * 1000)


@app.post("/api/rag/chat")
def api_rag_chat(body: RagChatRequest):
    """Conversational RAG: each user turn re-retrieves from the vector DB.

    The latest user message drives retrieval (embedded + cosine search over
    closed tickets); the model sees the recent conversation plus the fresh
    evidence, so follow-ups ("what did J2EE-0008 do differently?", "dig into
    the GlassFish ones") stay grounded in historic data.
    """
    t0 = time.time()
    status = 200
    try:
        history = [{"role": m.role, "content": m.content} for m in body.messages]
        if any(m["role"] not in ("user", "assistant") for m in history):
            raise HTTPException(status_code=400, detail="roles must be 'user' or 'assistant'.")
        if history[-1]["role"] != "user":
            raise HTTPException(status_code=400, detail="The last message must be from the user.")
        latest = history[-1]["content"].strip()
        if not latest:
            raise HTTPException(status_code=400, detail="Latest user message is empty.")
        # Intent gate: smalltalk/help never reaches retrieval or the LLM.
        intent = _detect_chat_intent(latest)
        if intent != "analyze":
            reply = _INTENT_REPLIES[intent]
            similar = []
        elif _needs_clarification(latest):
            reply = _INTENT_REPLIES["clarify"]
            similar = []
        else:
            # Ticket ids / sys_ids resolve to full ticket text (same as /ask);
            # anything else is embedded as-is. Unknown ids fall back to raw text.
            import rag_ingest

            try:
                _rec, embed_text = _rag_resolve_ticket(latest)
            except HTTPException as exc:
                if exc.status_code == 404:
                    embed_text = latest
                else:
                    raise
            vec = _rag_embed(embed_text)
            similar = _rag_search(vec, body.top_k)
            reply = _rag_chat_answer(history, latest, similar)
        similar_out = [
            {k: t[k] for k in ("ticket_id", "status", "summary", "similarity", "solution", "rca")}
            for t in similar
        ]
        # Persist: new thread titled by the FIRST question, else append.
        import psycopg

        try:
            with psycopg.connect(_rag_pg_conninfo()) as conn:
                _ensure_rag_threads(conn)
                with conn.cursor() as cur:
                    if body.thread_id:
                        exists = cur.execute(
                            "SELECT id FROM rag_thread WHERE id = %s", (body.thread_id,)
                        ).fetchone()
                        if not exists:
                            raise HTTPException(status_code=404, detail=f"Thread {body.thread_id} not found.")
                        thread_id = body.thread_id
                        first_q = cur.execute(
                            "SELECT content FROM rag_message WHERE thread_id = %s AND role = 'user' "
                            "ORDER BY id LIMIT 1",
                            (thread_id,),
                        ).fetchone()
                        title = _thread_title(first_q[0] if first_q else latest)
                    else:
                        first_user = next((m["content"] for m in history if m["role"] == "user"), latest)
                        title = _thread_title(first_user)
                        thread_id = cur.execute(
                            "INSERT INTO rag_thread (title) VALUES (%s) RETURNING id",
                            (title,),
                        ).fetchone()[0]
                    cur.execute(
                        "INSERT INTO rag_message (thread_id, role, content) VALUES (%s,'user',%s)",
                        (thread_id, latest),
                    )
                    cur.execute(
                        "INSERT INTO rag_message (thread_id, role, content, cited, model) "
                        "VALUES (%s,'assistant',%s,%s,%s)",
                        (thread_id, reply, json.dumps(similar_out), OLLAMA_CHAT_MODEL),
                    )
                    cur.execute(
                        "UPDATE rag_thread SET updated_at = now(), title = %s WHERE id = %s",
                        (title, thread_id),
                    )
                conn.commit()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Chat history save failed: {str(exc)[:200]}")
        return {
            "reply": reply,
            "similar": similar_out,
            "model": OLLAMA_CHAT_MODEL,
            "thread_id": thread_id,
            "thread_title": title,
        }
    except HTTPException as exc:
        status = exc.status_code
        raise
    finally:
        _record_rag(status, (time.time() - t0) * 1000)


@app.post("/api/rag/ask")
def api_rag_ask(body: RagAskRequest):
    """Root-cause analysis for one incident from historic closed tickets."""
    import rag_ingest

    t0 = time.time()
    status = 200
    try:
        intent = _detect_chat_intent(body.input)
        if intent != "analyze" or _needs_clarification(body.input):
            return {
                "query": {
                    "input": body.input,
                    "ticket_id": "",
                    "summary": body.input[:120],
                },
                "similar": [],
                "answer": _INTENT_REPLIES[intent if intent != "analyze" else "clarify"],
                "model": OLLAMA_CHAT_MODEL,
            }
        rec, query_text = _rag_resolve_ticket(body.input)
        rec_sys_id = rag_ingest.dv(rec.get("sys_id")) if isinstance(rec, dict) else ""
        vec = _rag_embed(query_text)
        similar = _rag_search(vec, body.top_k, exclude_sys_id=rec_sys_id)
        if not similar:
            status = 404
            raise HTTPException(
                status_code=404,
                detail="Vector DB is empty — run venv/bin/python rag_ingest.py --once first.",
            )
        answer = _rag_generate(query_text, similar)
        return {
            "query": {
                "input": body.input,
                "ticket_id": rag_ingest.dv(rec.get("u_ticket_id")) if isinstance(rec, dict) else "",
                "summary": rag_ingest.dv(rec.get("u_summary")) if isinstance(rec, dict) else body.input[:120],
            },
            "similar": [
                {k: t[k] for k in ("ticket_id", "status", "summary", "similarity", "solution", "rca")}
                for t in similar
            ],
            "answer": answer,
            "model": OLLAMA_CHAT_MODEL,
        }
    except HTTPException as exc:
        status = exc.status_code
        raise
    finally:
        _record_rag(status, (time.time() - t0) * 1000)


@app.get("/api/rag/stats")
def api_rag_stats():
    import psycopg

    try:
        with psycopg.connect(_rag_pg_conninfo()) as conn:
            total = conn.execute("SELECT count(*) FROM rag_ticket").fetchone()[0]
            by_status = conn.execute(
                "SELECT status, count(*) FROM rag_ticket GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()
            dim = conn.execute("SELECT vector_dims(embedding) FROM rag_ticket LIMIT 1").fetchone()
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"RAG DB unreachable (ran rag_ingest.py yet?): {str(exc)[:200]}"
        )
    reachable = True
    try:
        requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5).raise_for_status()
    except requests.RequestException:
        reachable = False
    return {
        "rows": total,
        "by_status": [{"status": s, "count": n} for s, n in by_status],
        "embedding_dims": dim[0] if dim else None,
        "ollama_up": reachable,
        "chat_model": OLLAMA_CHAT_MODEL,
    }


# ---------------------------------------------------------------------------
# API: Current incidents — open / in-progress tickets + grounded triage.
# Table: current_incident (Postgres; IF NOT EXISTS so reruns are safe).
# Source: CUR_TABLE (default u_snow_incident_table); open means u_status NOT
# IN the RAG closed statuses. Analysis is grounded ONLY in rag_ticket history:
# the model may not use its own parametric knowledge, and unsupported cases
# store an explicit no-solution statement instead of an invented fix.
# ---------------------------------------------------------------------------
CUR_TABLE = os.getenv("SNOW_CUSTOM_TABLE", "u_snow_incident_table")
CUR_FIELDS = (
    "sys_id,u_ticket_id,u_summary,u_description,u_status,u_category,u_subcategory,"
    "u_priority,u_severity,u_environment,u_technology_stack,u_affected_component,"
    "sys_updated_on"
)
CUR_RUN_LIMIT_DEFAULT = int(os.getenv("CUR_RUN_LIMIT", "10"))
CUR_TOP_K_DEFAULT = int(os.getenv("CUR_TOP_K", "5"))

NO_SOLUTION_TEXT = (
    "No supported solution — the historic tickets do not cover this incident."
)

CURRENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS current_incident (
    sys_id              TEXT PRIMARY KEY,
    ticket_id           TEXT,
    summary             TEXT,
    description         TEXT,
    status              TEXT,
    category            TEXT,
    subcategory         TEXT,
    priority            TEXT,
    severity            TEXT,
    environment         TEXT,
    technology_stack    TEXT,
    affected_component  TEXT,
    triage              TEXT,
    rca                 TEXT,
    solution            TEXT,
    confidence          TEXT,
    cited               JSONB,
    agent_trace         JSONB,
    triaged_at          TIMESTAMPTZ,
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw                 JSONB
);
CREATE INDEX IF NOT EXISTS idx_current_incident_ticket ON current_incident(ticket_id);
CREATE INDEX IF NOT EXISTS idx_current_incident_status ON current_incident(status);
"""


def _ensure_current(conn) -> None:
    conn.execute(CURRENT_SCHEMA_SQL)


def _cur_closed_statuses() -> list[str]:
    return [
        s.strip()
        for s in os.getenv("RAG_CLOSED_STATUSES", "Resolved").split(",")
        if s.strip()
    ]


def _cur_open_query() -> Optional[str]:
    """Encoded query matching open tickets (status NOT IN closed statuses)."""
    parts = [f"u_status!={s}" for s in _cur_closed_statuses()]
    return "^".join(parts) if parts else None


def _triage_analyze(query_text: str, similar: list[dict]) -> dict:
    """Structured triage/RCA/solution grounded ONLY in retrieved tickets.

    Never calls the LLM on its own knowledge: the prompt forbids parametric
    answers and demands ticket-id citations; empty evidence (or an
    unsupported verdict) yields the explicit NO_SOLUTION_TEXT, never a guess.
    Returns {triage, rca, solution, confidence, supported, cited, trace}.
    """
    import rag_ingest

    cited = [
        {k: t[k] for k in ("ticket_id", "status", "summary", "similarity", "solution", "rca")}
        for t in (similar or [])
    ]
    trace = [{"model": OLLAMA_CHAT_MODEL, "retrieved": len(cited)}]
    if not similar:
        return {
            "triage": "No historic tickets retrieved for this incident — triage deferred.",
            "rca": "Unknown — no historic evidence retrieved.",
            "solution": NO_SOLUTION_TEXT,
            "confidence": "Low",
            "supported": False,
            "cited": cited,
            "trace": trace,
        }
    ctx = "\n\n".join(
        f"[{t['ticket_id'] or t['sys_id']}] (similarity {t['similarity']})\n"
        f"Summary: {t['summary']}\nDetail: {(t['text'] or '')[:900]}\n"
        f"Historic root cause: {t['rca'] or 'n/a'}\nHistoric solution: {t['solution'] or 'n/a'}"
        for t in similar
    )
    prompt = (
        "You are a ServiceNow triage analyst. You have NO knowledge of this incident "
        "beyond the historic resolved tickets below. Use ONLY those tickets — do NOT use "
        "your own parametric knowledge, do NOT invent causes, fixes, commands, or ticket ids. "
        "Every factual claim must cite a ticket id like [J2EE-0003]. "
        "Reply with EXACTLY one fenced JSON block and nothing else:\n"
        '```json {"triage": "<2-3 sentence assessment of severity/area and what to check first>", '
        '"rca": "<likely root cause in 2-4 sentences, or \'Unknown — insufficient historic evidence.\'>", '
        '"solution": "<concrete numbered steps, or the exact string: '
        + NO_SOLUTION_TEXT + '>", '
        '"confidence": "<High|Medium|Low>", '
        '"supported": <true if the historic tickets support the RCA/solution, else false>}\n```\n'
        "If the historic tickets do not support a root cause or fix, set supported=false, "
        "confidence=Low, and solution to that exact no-solution string.\n\n"
        f"CURRENT INCIDENT:\n{query_text[:2000]}\n\nHISTORIC RESOLVED TICKETS:\n{ctx}"
    )
    try:
        raw_out = _agent_llm(
            [{"role": "user", "content": prompt}], num_predict=600
        )
    except HTTPException as exc:
        return {
            "triage": "Triage failed — local model unreachable.",
            "rca": "Unknown — analysis did not run.",
            "solution": NO_SOLUTION_TEXT,
            "confidence": "Low",
            "supported": False,
            "cited": cited,
            "trace": trace + [{"error": str(exc.detail)[:200]}],
        }
    obj: Optional[dict] = None
    decoder = json.JSONDecoder()
    import re

    blocks = re.findall(r"```json\s*(.*?)\s*```", raw_out, re.DOTALL) or [raw_out]
    for block in blocks:
        for match in re.finditer(r"\{", block):
            try:
                cand, _end = decoder.raw_decode(block[match.start():])
            except ValueError:
                continue
            if isinstance(cand, dict) and any(k in cand for k in ("triage", "rca", "solution")):
                obj = cand
                break
        if obj is not None:
            break
    if obj is None:
        return {
            "triage": (raw_out.strip()[:1500] or "(empty model output)"),
            "rca": "Unknown — model output was not parseable.",
            "solution": NO_SOLUTION_TEXT,
            "confidence": "Low",
            "supported": False,
            "cited": cited,
            "trace": trace + [{"unparseable": True}],
        }
    supported = bool(obj.get("supported", False))
    conf = str(obj.get("confidence", "Low")).strip().capitalize()
    if conf not in ("High", "Medium", "Low"):
        conf = "Low"
    _sol = obj.get("solution", "")
    if isinstance(_sol, list):  # model sometimes returns steps as a JSON array
        _sol = "\n".join(
            f"{i + 1}. {str(s).strip()}" for i, s in enumerate(_sol) if str(s).strip()
        )
    solution = str(_sol or "").strip() or NO_SOLUTION_TEXT
    if not supported:
        # Enforce explicitness: an "unsupported" verdict must not smuggle in a fix.
        solution = NO_SOLUTION_TEXT
        conf = "Low"
    return {
        "triage": str(obj.get("triage", "")).strip()[:2000] or "Triage not provided.",
        "rca": str(obj.get("rca", "")).strip()[:2000] or "Unknown — insufficient historic evidence.",
        "solution": solution[:2000],
        "confidence": conf,
        "supported": supported,
        "cited": cited,
        "trace": trace,
    }


def _current_row_from_rec(rec: dict) -> dict:
    """Map a ServiceNow custom-table record onto current_incident columns."""
    import rag_ingest

    dv = rag_ingest.dv
    return {
        "sys_id": dv(rec.get("sys_id")),
        "ticket_id": dv(rec.get("u_ticket_id")),
        "summary": dv(rec.get("u_summary")),
        "description": dv(rec.get("u_description")),
        "status": dv(rec.get("u_status")),
        "category": dv(rec.get("u_category")),
        "subcategory": dv(rec.get("u_subcategory")),
        "priority": dv(rec.get("u_priority")),
        "severity": dv(rec.get("u_severity")),
        "environment": dv(rec.get("u_environment")),
        "technology_stack": dv(rec.get("u_technology_stack")),
        "affected_component": dv(rec.get("u_affected_component")),
        "raw": json.dumps(rec),
    }


CURRENT_UPSERT_SQL = """
INSERT INTO current_incident (
    sys_id, ticket_id, summary, description, status, category, subcategory,
    priority, severity, environment, technology_stack, affected_component,
    raw, synced_at
) VALUES (
    %(sys_id)s, %(ticket_id)s, %(summary)s, %(description)s, %(status)s,
    %(category)s, %(subcategory)s, %(priority)s, %(severity)s, %(environment)s,
    %(technology_stack)s, %(affected_component)s, %(raw)s, now()
)
ON CONFLICT (sys_id) DO UPDATE SET
    ticket_id = EXCLUDED.ticket_id,
    summary = EXCLUDED.summary,
    description = EXCLUDED.description,
    status = EXCLUDED.status,
    category = EXCLUDED.category,
    subcategory = EXCLUDED.subcategory,
    priority = EXCLUDED.priority,
    severity = EXCLUDED.severity,
    environment = EXCLUDED.environment,
    technology_stack = EXCLUDED.technology_stack,
    affected_component = EXCLUDED.affected_component,
    raw = EXCLUDED.raw,
    synced_at = now()
"""


def _current_fetch_open(limit: int) -> list[dict]:
    """Fetch up to `limit` open tickets live from ServiceNow (oldest first)."""
    _check_table_name(CUR_TABLE)
    params: dict[str, Any] = {
        "sysparm_fields": CUR_FIELDS,
        "sysparm_limit": max(1, limit),
        "sysparm_offset": 0,
        "sysparm_display_value": "all",
    }
    open_q = _cur_open_query()
    params["sysparm_query"] = f"{open_q}^ORDERBYsys_created_on" if open_q else "ORDERBYsys_created_on"
    resp = _snow_request("GET", f"/api/now/table/{CUR_TABLE}", params=params)
    _raise_for_snow(resp, "fetch open tickets failed")
    return resp.json().get("result", [])


def _current_analyze_text(rec: dict, top_k: int) -> tuple[str, dict]:
    """Build query text + grounded analysis for one ticket record."""
    import rag_ingest

    query_text = rag_ingest.ticket_text(rec)
    rec_sys_id = rag_ingest.dv(rec.get("sys_id"))
    vec = _rag_embed(query_text)
    similar = _rag_search(vec, top_k, exclude_sys_id=rec_sys_id)
    return query_text, _triage_analyze(query_text, similar)


@app.get("/api/current")
def api_current_list(
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None, description="Filter by status, e.g. In Progress"),
):
    """Local current-incident rows (ticket + triage + RCA + solution). No ServiceNow call."""
    import psycopg

    try:
        with psycopg.connect(_rag_pg_conninfo()) as conn:
            _ensure_current(conn)
            params: list[Any] = []
            where = ""
            if status:
                where = "WHERE status = %s"
                params.append(status)
            total = conn.execute(
                f"SELECT count(*) FROM current_incident {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT sys_id, ticket_id, summary, status, priority, severity, "
                f"confidence, triaged_at, synced_at FROM current_incident "
                f"{where} ORDER BY ticket_id LIMIT %s OFFSET %s",
                (*params, limit, offset),
            ).fetchall()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Current DB unreachable: {str(exc)[:200]}")
    return {
        "result": [
            {"sys_id": r[0], "ticket_id": r[1], "summary": r[2], "status": r[3],
             "priority": r[4], "severity": r[5], "confidence": r[6],
             "triaged_at": str(r[7]) if r[7] else None,
             "synced_at": str(r[8]) if r[8] else None}
            for r in rows
        ],
        "total": total, "offset": offset, "limit": limit,
    }


@app.get("/api/current/{sys_id}")
def api_current_get(sys_id: str):
    """One current ticket with full triage + RCA + solution + cited tickets."""
    import psycopg

    _check_sys_id(sys_id)
    try:
        with psycopg.connect(_rag_pg_conninfo()) as conn:
            _ensure_current(conn)
            r = conn.execute(
                "SELECT sys_id, ticket_id, summary, description, status, category, "
                "subcategory, priority, severity, environment, technology_stack, "
                "affected_component, triage, rca, solution, confidence, cited, "
                "agent_trace, triaged_at, synced_at "
                "FROM current_incident WHERE sys_id = %s",
                (sys_id,),
            ).fetchone()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Current DB unreachable: {str(exc)[:200]}")
    if not r:
        raise HTTPException(status_code=404, detail=f"No current incident {sys_id}.")
    keys = ["sys_id", "ticket_id", "summary", "description", "status", "category",
            "subcategory", "priority", "severity", "environment", "technology_stack",
            "affected_component", "triage", "rca", "solution", "confidence", "cited",
            "agent_trace", "triaged_at", "synced_at"]
    out = dict(zip(keys, r))
    out["triaged_at"] = str(out["triaged_at"]) if out["triaged_at"] else None
    out["synced_at"] = str(out["synced_at"]) if out["synced_at"] else None
    return {"result": out}


class CurrentRunRequest(BaseModel):
    limit: int = Field(default=CUR_RUN_LIMIT_DEFAULT, ge=1, le=50)
    top_k: int = Field(default=CUR_TOP_K_DEFAULT, ge=1, le=20)
    retriage: bool = Field(default=False, description="Re-analyse even already-triaged rows")


def _current_store_analysis(conn, sys_id: str, analysis: dict) -> None:
    conn.execute(
        "UPDATE current_incident SET triage = %s, rca = %s, solution = %s, "
        "confidence = %s, cited = %s, agent_trace = %s, triaged_at = now() "
        "WHERE sys_id = %s",
        (analysis["triage"], analysis["rca"], analysis["solution"],
         analysis["confidence"], json.dumps(analysis["cited"]),
         json.dumps(analysis["trace"]), sys_id),
    )


@app.post("/api/current/run")
def api_current_run(body: CurrentRunRequest):
    """Ingest open tickets into current_incident + grounded triage/RCA/solution.

    Upserts ticket fields for up to `limit` open tickets, then analyses rows
    that have no triage yet (or all, with retriage=true). Each analysis costs
    one embed + one local-LLM call — keep `limit` small on CPU boxes.
    """
    import psycopg

    t0 = time.time()
    stats: dict[str, Any] = {"fetched": 0, "upserted": 0, "triaged": 0,
                             "skipped": 0, "unsupported": 0, "errors": []}
    try:
        recs = _current_fetch_open(body.limit)
    except HTTPException as exc:
        raise exc
    stats["fetched"] = len(recs)
    try:
        with psycopg.connect(_rag_pg_conninfo()) as conn:
            _ensure_current(conn)
            with conn.cursor() as cur:
                for rec in recs:
                    row = _current_row_from_rec(rec)
                    if not row["sys_id"]:
                        continue
                    cur.execute(CURRENT_UPSERT_SQL, row)
                    stats["upserted"] += 1
                conn.commit()
                targets = conn.execute(
                    "SELECT sys_id FROM current_incident "
                    + ("" if body.retriage else "WHERE triage IS NULL ")
                    + "ORDER BY ticket_id LIMIT %s",
                    (body.limit,),
                ).fetchall()
            for (sys_id,) in targets:
                try:
                    rec = next(
                        (r for r in recs
                         if _dv_sys(r) == sys_id),
                        None,
                    )
                    if rec is None:  # already-stored row: rebuild from raw
                        stored = conn.execute(
                            "SELECT raw FROM current_incident WHERE sys_id = %s",
                            (sys_id,),
                        ).fetchone()
                        rec = _raw_to_rec(stored[0]) if stored else None
                    if not rec:
                        stats["skipped"] += 1
                        continue
                    _q, analysis = _current_analyze_text(rec, body.top_k)
                    _current_store_analysis(conn, sys_id, analysis)
                    conn.commit()
                    stats["triaged"] += 1
                    if not analysis["supported"]:
                        stats["unsupported"] += 1
                except Exception as exc:  # one bad ticket must not kill the run
                    conn.rollback()
                    stats["errors"].append(f"{sys_id[:8]}: {str(exc)[:120]}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Current run failed: {str(exc)[:200]}")
    stats["elapsed_s"] = round(time.time() - t0, 1)
    return stats


def _dv_sys(rec: dict) -> str:
    v = rec.get("sys_id")
    if isinstance(v, dict):
        return str(v.get("display_value", v.get("value", "")) or "")
    return "" if v is None else str(v)


def _raw_to_rec(raw) -> Optional[dict]:
    """Normalise a stored raw column (psycopg returns JSONB as dict)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            obj = json.loads(raw)
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None
    return None


@app.post("/api/current/retriage/{sys_id}")
def api_current_retriage(sys_id: str, top_k: int = Query(CUR_TOP_K_DEFAULT, ge=1, le=20)):
    """Re-run grounded triage/RCA/solution for one stored current ticket."""
    import psycopg

    _check_sys_id(sys_id)
    try:
        with psycopg.connect(_rag_pg_conninfo()) as conn:
            _ensure_current(conn)
            stored = conn.execute(
                "SELECT raw FROM current_incident WHERE sys_id = %s", (sys_id,)
            ).fetchone()
            if not stored:
                raise HTTPException(status_code=404, detail=f"No current incident {sys_id}.")
            rec = _raw_to_rec(stored[0])
            if not rec:
                # Fall back to a live fetch from ServiceNow.
                _check_table_name(CUR_TABLE)
                resp = _snow_request(
                    "GET", f"/api/now/table/{CUR_TABLE}/{sys_id}",
                    params={"sysparm_display_value": "all"},
                )
                _raise_for_snow(resp, "ticket lookup failed")
                rec = resp.json().get("result")
            _q, analysis = _current_analyze_text(rec, top_k)
            _current_store_analysis(conn, sys_id, analysis)
            conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Re-triage failed: {str(exc)[:200]}")
    return {"ok": True, "sys_id": sys_id, **{k: analysis[k] for k in ("confidence", "supported")},
            "analysis": analysis}


@app.get("/api/dashboard")
def api_dashboard():
    """Landing-page aggregate: connection, live open count + local metrics.

    Never fails hard: unreachable backends degrade to nulls/errors so the
    dashboard always renders.
    """
    import psycopg

    st = api_status()
    try:
        open_total = _snow_total(CUR_TABLE, _cur_open_query())
    except Exception:
        open_total = None
    cur: dict[str, Any] = {
        "total": None, "triaged": None, "unsupported": None,
        "untriaged": [], "recent": [],
    }
    try:
        with psycopg.connect(_rag_pg_conninfo()) as conn:
            _ensure_current(conn)
            cur["total"] = conn.execute("SELECT count(*) FROM current_incident").fetchone()[0]
            cur["triaged"] = conn.execute(
                "SELECT count(*) FROM current_incident WHERE triage IS NOT NULL"
            ).fetchone()[0]
            cur["unsupported"] = conn.execute(
                "SELECT count(*) FROM current_incident WHERE solution = %s",
                (NO_SOLUTION_TEXT,),
            ).fetchone()[0]
            cur["untriaged"] = [
                {"ticket_id": r[0], "summary": r[1]}
                for r in conn.execute(
                    "SELECT ticket_id, summary FROM current_incident "
                    "WHERE triage IS NULL ORDER BY ticket_id LIMIT 5"
                ).fetchall()
            ]
            cur["recent"] = [
                {"ticket_id": r[0], "confidence": r[1],
                 "triaged_at": str(r[2])[:16] if r[2] else None}
                for r in conn.execute(
                    "SELECT ticket_id, confidence, triaged_at FROM current_incident "
                    "WHERE triage IS NOT NULL ORDER BY triaged_at DESC LIMIT 5"
                ).fetchall()
            ]
    except Exception as exc:
        cur["error"] = str(exc)[:150]
    try:
        mirror = api_sync_stats()
    except HTTPException as exc:
        mirror = {"error": str(exc.detail)[:120]}
    try:
        rag = api_rag_stats()
    except HTTPException as exc:
        rag = {"error": str(exc.detail)[:120]}
    with _metrics_lock:
        snow_calls = _counters["snow_calls"]
        snow_errors = _counters["snow_errors"]
        tok_mint = _counters["token_mint"]
        tok_reuse = _counters["token_reuse"]
    return {
        "connected": st["connected"], "instance": st["instance"],
        "auth_mode": st["auth_mode"], "expires_in": st["expires_in"],
        "open_total": open_total, "open_table": CUR_TABLE,
        "current": cur, "mirror": mirror, "rag": rag,
        "uptime_s": int(time.time() - START_TIME),
        "snow": {"calls": snow_calls, "errors": snow_errors},
        "tokens": {"minted": tok_mint, "reused": tok_reuse},
    }


# ---------------------------------------------------------------------------
# API: Early Warning (AIML) — model details + single-entry scoring.
# Everything lives in AIML/ (no ServiceNow calls here).
# ---------------------------------------------------------------------------
EW_DIR = os.path.join(BASE_DIR, "AIML")
_ew_backend = None  # lazy MLBackend singleton (needs sklearn)


def _ew_ensure_path() -> None:
    if EW_DIR not in sys.path:
        sys.path.insert(0, EW_DIR)


def _ew_meta() -> dict:
    _ew_ensure_path()
    meta_path = os.path.join(EW_DIR, "models", "ml_meta.json")
    if not os.path.exists(meta_path):
        raise HTTPException(
            status_code=503,
            detail="ML models not trained yet — run: cd AIML && python3 ml_train.py",
        )
    with open(meta_path, encoding="utf-8") as f:
        return json.load(f)


def _ew_get_backend():
    global _ew_backend
    if _ew_backend is None:
        _ew_ensure_path()
        try:
            from ml_model import MLBackend
        except ImportError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"ML backend needs scikit-learn in the app venv: {exc}",
            )
        _ew_meta()  # 503 with guidance when untrained
        _ew_backend = MLBackend()
    return _ew_backend


@app.get("/api/earlywarning/model")
def api_ew_model():
    """ML model details for the UI (works without sklearn)."""
    _ew_ensure_path()
    import early_warning as ew

    meta = _ew_meta()
    cfg = ew.CONFIG
    return {
        "ml_trained": True,
        "services": cfg["services"],
        "features": meta.get("base_features", []),
        "window": meta.get("window", 3),
        "levels": "per-service calibrated (WATCH=p95, WARNING=train max, CRITICAL=1.5x max)",
        "isolation_forest": {
            "n_estimators": 200,
            "contamination": meta.get("contamination"),
            "trained_on": "normal minutes of 5 seeded logs (test: unseen seed 42)",
            "calibration": meta.get("calibration", {}),
        },
        "random_forest": {
            "n_estimators": 300,
            "oob_score": (meta.get("rf") or {}).get("oob_score"),
            "train_precision": (meta.get("rf") or {}).get("train_precision"),
            "train_recall": (meta.get("rf") or {}).get("train_recall"),
            "samples": (meta.get("rf") or {}).get("samples"),
            "positives": (meta.get("rf") or {}).get("positives"),
            "horizon_min": meta.get("horizon"),
            "proba_threshold": meta.get("proba_threshold"),
        },
        "train_window": cfg["train_window"],
        "failure_window": cfg["failure_window"],
    }


@app.get("/api/earlywarning/sample")
def api_ew_sample(
    service: str = Query("payments"),
    kind: str = Query("normal", description="normal | peak | failure"),
):
    """Prefilled feature sets: normal = learned medians; peak/failure = real
    minutes (40 / 47) from the log, with two prior minutes as RF history."""
    _ew_ensure_path()
    import early_warning as ew

    if service not in ew.CONFIG["services"]:
        raise HTTPException(status_code=400, detail=f"Unknown service {service!r}.")
    if kind not in ("normal", "peak", "failure"):
        raise HTTPException(status_code=400, detail="kind must be normal|peak|failure.")
    feats = ["count", "err_rate", "p95", "avg_lat", "cpu", "mem", "pool"]
    if kind == "normal":
        bl_path = os.path.join(EW_DIR, "models", "baseline.json")
        if not os.path.exists(bl_path):
            raise HTTPException(
                status_code=503, detail="Baseline not trained — run: cd AIML && python3 train.py"
            )
        with open(bl_path, encoding="utf-8") as f:
            stats = json.load(f)
        row = {f: round(stats[service][f][0], 3) for f in feats}
        return {"service": service, "kind": kind, "features": row,
                "history": [row, row], "note": "history = medians (steady state)"}
    import train as train_mod

    buckets = train_mod.load_minutes(os.path.join(EW_DIR, ew.CONFIG["log_file"]))
    mins = {"peak": [38, 39, 40], "failure": [45, 46, 47]}[kind]
    rows = []
    for m in mins:
        fmap = ew.minute_features(buckets.get(m, [])).get(service)
        if not fmap:
            raise HTTPException(status_code=502, detail=f"No {service} data at min {m}.")
        rows.append({f: round(fmap[f], 3) for f in feats})
    return {"service": service, "kind": kind, "features": rows[-1],
            "history": rows[:-1], "note": f"real log minutes {mins[0]}-{mins[-1]}"}


class EWScoreRequest(BaseModel):
    service: str = Field(..., description="one of the 6 services")
    features: dict = Field(..., description="{count, err_rate, p95, avg_lat, cpu, mem, pool}")
    history: list[dict] = Field(default=[], description="0-2 prior-minute feature dicts, oldest first")


@app.post("/api/earlywarning/score")
def api_ew_score(body: EWScoreRequest):
    """Score one entry: IsolationForest anomaly level + RandomForest failure
    probability -> verdict NORMAL / ANOMALOUS / FAILURE LIKELY."""
    backend = _ew_get_backend()
    import early_warning as ew

    if body.service not in ew.CONFIG["services"]:
        raise HTTPException(status_code=400, detail=f"Unknown service {body.service!r}.")
    feats = ["count", "err_rate", "p95", "avg_lat", "cpu", "mem", "pool"]
    try:
        cur = {f: float(body.features[f]) for f in feats}
    except (KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail=f"features must carry numbers for {feats}."
        )
    hist = []
    for h in (body.history or [])[-2:]:
        try:
            hist.append({f: float(h[f]) for f in feats})
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="history rows need the same 7 numbers.")
    degraded = len(hist) < 2
    full_hist = (hist + [cur])[-3:]
    while len(full_hist) < 3:
        full_hist.insert(0, dict(cur))

    scores = backend.score_minute({body.service: cur})
    _detail, if_score = scores[body.service]
    level = backend.level_for(body.service, if_score)

    import numpy as _np
    from ml_model import ONE_HOT, window_features

    x = _np.array([window_features(full_hist) + ONE_HOT[body.service]], dtype=float)
    proba = float(backend.rf.predict_proba(x)[0][1])
    thr = backend.meta["proba_threshold"]
    if proba >= thr:
        verdict = "FAILURE LIKELY"
    elif level in ("WARNING", "CRITICAL"):
        verdict = "ANOMALOUS"
    else:
        verdict = "NORMAL"

    # Explainability: stdlib z-scores on the same minute (no sklearn needed).
    bl_path = os.path.join(EW_DIR, "models", "baseline.json")
    top = []
    try:
        from early_warning import Baseline

        base = Baseline.load(bl_path)
        zs, _s = base.zscores({body.service: cur})[body.service]
        top = sorted(
            ({"feature": k, "z": round(v, 2)} for k, v in zs.items()),
            key=lambda d: -abs(d["z"]),
        )[:3]
    except Exception:
        pass
    return {
        "service": body.service,
        "if_score": round(if_score, 4),
        "level": level,
        "failure_proba": round(proba, 4),
        "proba_threshold": thr,
        "verdict": verdict,
        "top_features": top,
        "history_degraded": degraded,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8090, reload=True)
