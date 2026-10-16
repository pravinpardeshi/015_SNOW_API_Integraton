"""ServiceNow Change Management script: verify access + list / create / update change_request.

Mirrors testServiceNow.py auth patterns, but targets the Change tables
(default: change_request) instead of incident.

ServiceNow side one-time enable (PDI / dev instance):
  1. Wake the instance at developer.servicenow.com.
  2. Confirm All > Change > All Changes exists (stock on PDI).
  3. Same OAuth registry as incidents: System OAuth > Application Registry >
     "OAuth API endpoint for external clients" (copy Client ID/Secret).
  4. OAuth user must be Active/unlocked with REST access
     (snc_platform_rest_api_access) + itil / change_manager + read/create/write
     ACLs on change_request. On 403 see System Security > ACLs or Table >
     Application Access > "Allow access via web services".

Config (see .env.example, never commit .env):
  SNOW_INSTANCE, SNOW_CLIENT_ID, SNOW_CLIENT_SECRET,
  SNOW_AUTH=bearer|basic, SNOW_GRANT_TYPE=password|client_credentials,
  SNOW_USERNAME, SNOW_PASSWORD, SNOW_CHANGE_TABLE=change_request

Examples:
  python manage_change.py check
  python manage_change.py list --limit 5
  python manage_change.py create --short-description "Upgrade DB pool" --description "..." --type Normal
  python manage_change.py create --short-description "..." --type Standard --category Software --risk 3 --impact 2
  python manage_change.py get --sys-id <32-char-sys_id>
  python manage_change.py update --sys-id <sys_id> --state "In Progress" --work-notes "started"
"""

import argparse
import os
import re
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

INSTANCE = os.getenv("SNOW_INSTANCE", "https://dev389543.service-now.com").rstrip("/")
CLIENT_ID = os.getenv("SNOW_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SNOW_CLIENT_SECRET", "")
USERNAME = os.getenv("SNOW_USERNAME", "")
PASSWORD = os.getenv("SNOW_PASSWORD", "")
AUTH_MODE = os.getenv("SNOW_AUTH", "bearer").strip().lower()  # bearer | basic
GRANT_TYPE = os.getenv("SNOW_GRANT_TYPE", "password").strip().lower()
CHANGE_TABLE = os.getenv("SNOW_CHANGE_TABLE", "change_request")

REQUEST_TIMEOUT = 30
SYS_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _redact(token: str) -> str:
    return f"{token[:6]}... (len {len(token)})" if token else "<empty>"


def _require_basic_creds() -> tuple[str, str]:
    if not USERNAME or not PASSWORD:
        raise SystemExit(
            "Missing SNOW_USERNAME / SNOW_PASSWORD. Add them to .env and retry."
        )
    return USERNAME, PASSWORD


def _require_client_creds() -> tuple[str, str]:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit(
            "Missing SNOW_CLIENT_ID / SNOW_CLIENT_SECRET. Copy .env.example to .env first."
        )
    return CLIENT_ID, CLIENT_SECRET


def _check_table_name(table: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table or ""):
        raise SystemExit(f"Invalid table name {table!r}. Expected e.g. change_request.")
    return table


def _check_sys_id(sys_id: str) -> str:
    if not sys_id or not SYS_ID_RE.fullmatch(sys_id):
        raise SystemExit(
            f"Invalid sys_id {sys_id!r}. Expected a 32-character ServiceNow sys_id."
        )
    return sys_id


def get_access_token(
    grant_type: str = GRANT_TYPE,
    username: str = USERNAME,
    password: str = PASSWORD,
) -> str:
    grant_type = (grant_type or "password").strip().lower()
    if grant_type == "password":
        _require_basic_creds()
        client_id, client_secret = _require_client_creds()
        payload = {
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password,
        }
    elif grant_type == "client_credentials":
        client_id, client_secret = _require_client_creds()
        payload = {"grant_type": "client_credentials"}
        # client_credentials uses HTTP Basic with id/secret, like app.py
        resp = requests.post(
            f"{INSTANCE}/oauth_token.do",
            data=payload,
            auth=(client_id, client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    else:
        raise SystemExit(f"Unsupported SNOW_GRANT_TYPE {grant_type!r}.")

    resp = requests.post(
        f"{INSTANCE}/oauth_token.do",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def make_auth(token: str = "") -> tuple[dict, tuple | None]:
    if AUTH_MODE == "basic":
        return {"Accept": "application/json"}, _require_basic_creds()
    if AUTH_MODE != "bearer":
        raise SystemExit(f"Unsupported SNOW_AUTH {AUTH_MODE!r}. Use 'bearer' or 'basic'.")
    if not token:
        raise SystemExit("Bearer mode needs a token: call get_access_token() first.")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}, None


def _friendly_error(action: str, resp: requests.Response) -> str:
    try:
        detail = resp.json()
    except ValueError:
        detail = resp.text[:500]
    hint = ""
    if resp.status_code == 403:
        hint = (
            " ServiceNow denied access: grant the OAuth/basic user read/create/write "
            "ACLs on change_request (itil/change_manager role, Table > Application "
            "Access > allow web services) and retry."
        )
    if resp.status_code == 401 and AUTH_MODE == "basic":
        hint = " Basic credentials rejected: check SNOW_USERNAME/SNOW_PASSWORD (locked/expired?)."
    return f"{action} failed [{resp.status_code}]: {detail}.{hint}"


def check_change_enabled(token: str = "", table: str = CHANGE_TABLE) -> bool:
    """Probe GET 1 row. Returns True when the table is reachable; explains 403/404."""
    table = _check_table_name(table)
    headers, basic = make_auth(token)
    resp = requests.get(
        f"{INSTANCE}/api/now/table/{table}",
        headers=headers,
        auth=basic,
        params={"sysparm_limit": 1, "sysparm_display_value": "false"},
        timeout=REQUEST_TIMEOUT,
    )
    print(f"Probe {table}: HTTP {resp.status_code}")
    if resp.status_code == 200:
        print(f"OK: table '{table}' is reachable. Change Management is enabled for this user.")
        return True
    if resp.status_code == 404:
        print(f"Table '{table}' not found. On a PDI change_request/change_task are stock — "
              "check for a typo or a scoped custom table name.")
        return False
    print(_friendly_error(f"Probe {table}", resp))
    return False


def list_changes(token: str = "", table: str = CHANGE_TABLE, limit: int = 10) -> list:
    table = _check_table_name(table)
    headers, basic = make_auth(token)
    resp = requests.get(
        f"{INSTANCE}/api/now/table/{table}",
        headers=headers,
        auth=basic,
        params={
            "sysparm_limit": max(1, limit),
            "sysparm_display_value": "all",
            "sysparm_query": "ORDERBYDESCsys_created_on",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise SystemExit(_friendly_error(f"List {table}", resp))
    return resp.json().get("result", [])


def get_change(token: str = "", sys_id: str = "", table: str = CHANGE_TABLE) -> dict:
    table = _check_table_name(table)
    _check_sys_id(sys_id)
    headers, basic = make_auth(token)
    resp = requests.get(
        f"{INSTANCE}/api/now/table/{table}/{sys_id}",
        headers=headers,
        auth=basic,
        params={"sysparm_display_value": "all"},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise SystemExit(_friendly_error(f"Get {table}/{sys_id}", resp))
    return resp.json().get("result", {})


def create_change(token: str = "", table: str = CHANGE_TABLE, **fields) -> dict:
    """Create a change_request. Only short_description is strictly required."""
    table = _check_table_name(table)
    payload = {k: v for k, v in fields.items() if v is not None}
    if not payload.get("short_description"):
        raise SystemExit("--short-description is required.")
    headers, basic = make_auth(token)
    headers["Content-Type"] = "application/json"
    resp = requests.post(
        f"{INSTANCE}/api/now/table/{table}",
        headers=headers,
        auth=basic,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    print("Create HTTP status:", resp.status_code)
    if resp.status_code not in (200, 201):
        raise SystemExit(_friendly_error(f"Create {table}", resp))
    return resp.json().get("result", {})


def update_change(token: str = "", sys_id: str = "", table: str = CHANGE_TABLE, **fields) -> dict:
    table = _check_table_name(table)
    _check_sys_id(sys_id)
    payload = {k: v for k, v in fields.items() if v is not None}
    if not payload:
        raise SystemExit("Nothing to update: pass at least one field (e.g. --state).")
    headers, basic = make_auth(token)
    headers["Content-Type"] = "application/json"
    resp = requests.patch(
        f"{INSTANCE}/api/now/table/{table}/{sys_id}",
        headers=headers,
        auth=basic,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    print("Update HTTP status:", resp.status_code)
    if resp.status_code != 200:
        raise SystemExit(_friendly_error(f"Update {table}/{sys_id}", resp))
    return resp.json().get("result", {})


def _summarize(row: dict) -> str:
    def dv(v):
        return str(v.get("display_value", v.get("value", ""))) if isinstance(v, dict) else ("" if v is None else str(v))

    num = dv(row.get("number"))
    desc = dv(row.get("short_description"))[:70]
    state = dv(row.get("state"))
    return f"{num or '(no number)'} | {state} | {desc} | sys_id {dv(row.get('sys_id'))}"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="ServiceNow Change Management via Table API")
    ap.add_argument("--table", default=CHANGE_TABLE, help="default: %(default)s")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="probe table access (enable verification)")
    p_list = sub.add_parser("list", help="list recent changes")
    p_list.add_argument("--limit", type=int, default=10)

    p_get = sub.add_parser("get", help="fetch one change")
    p_get.add_argument("--sys-id", required=True)

    p_create = sub.add_parser("create", help="create a change request")
    p_create.add_argument("--short-description", required=True)
    p_create.add_argument("--description", default=None)
    p_create.add_argument("--type", default="Normal",
                          help="Standard | Normal | Emergency (default: Normal)")
    p_create.add_argument("--category", default=None, help="e.g. Software, Hardware")
    p_create.add_argument("--risk", default=None, help="1-4")
    p_create.add_argument("--impact", default=None, help="1-3")
    p_create.add_argument("--urgency", default=None, help="1-3")
    p_create.add_argument("--state", default=None)
    p_create.add_argument("--assignment-group", default=None)
    p_create.add_argument("--assigned-to", default=None)
    p_create.add_argument("--justification", default=None)
    p_create.add_argument("--work-notes", default=None)

    p_up = sub.add_parser("update", help="update a change request")
    p_up.add_argument("--sys-id", required=True)
    p_up.add_argument("--state", default=None)
    p_up.add_argument("--short-description", default=None)
    p_up.add_argument("--description", default=None)
    p_up.add_argument("--work-notes", default=None)
    p_up.add_argument("--comments", default=None)
    p_up.add_argument("--assignment-group", default=None)
    p_up.add_argument("--assigned-to", default=None)
    return ap


def main(argv: list | None = None) -> None:
    args = build_parser().parse_args(argv)
    print(f"Auth mode: {AUTH_MODE}" + (f" (grant: {GRANT_TYPE})" if AUTH_MODE == "bearer" else f" (user: {USERNAME or '<missing>'})"))
    token = get_access_token() if AUTH_MODE == "bearer" else ""
    if token:
        print(f"Token: {_redact(token)}")

    if args.cmd == "check":
        ok = check_change_enabled(token, table=args.table)
        sys.exit(0 if ok else 1)
    if args.cmd == "list":
        for row in list_changes(token, table=args.table, limit=args.limit):
            print(_summarize(row))
    elif args.cmd == "get":
        print(_summarize(get_change(token, sys_id=args.sys_id, table=args.table)))
    elif args.cmd == "create":
        rec = create_change(
            token, table=args.table,
            short_description=args.short_description,
            description=args.description,
            type=args.type,
            category=args.category,
            risk=args.risk,
            impact=args.impact,
            urgency=args.urgency,
            state=args.state,
            assignment_group=args.assignment_group,
            assigned_to=args.assigned_to,
            justification=args.justification,
            work_notes=args.work_notes,
        )
        print("Created:", _summarize(rec))
    elif args.cmd == "update":
        rec = update_change(
            token, sys_id=args.sys_id, table=args.table,
            state=args.state,
            short_description=args.short_description,
            description=args.description,
            work_notes=args.work_notes,
            comments=args.comments,
            assignment_group=args.assignment_group,
            assigned_to=args.assigned_to,
        )
        print("Updated:", _summarize(rec))


if __name__ == "__main__":
    main()
