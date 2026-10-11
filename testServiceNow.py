"""ServiceNow demo script: OAuth token (password or client-credentials grant)
or HTTP Basic auth, then list / create / update incidents via Table API.

Auth selection (environment, see .env.example):
  SNOW_AUTH=basic           -> per-request username/password (no token).
                               Proven working even during the OAuth bearer
                               outage. Requires SNOW_USERNAME/SNOW_PASSWORD.
  SNOW_AUTH=bearer (default) -> OAuth access token in Authorization header.
      SNOW_GRANT_TYPE=password (default) -> resource-owner password grant.
                               Requires SNOW_USERNAME/SNOW_PASSWORD plus the
                               OAuth client id/secret.
      SNOW_GRANT_TYPE=client_credentials  -> machine-to-machine grant.
                               Requires only the OAuth client id/secret.

Flow when run directly: token (bearer mode) -> list -> create -> list ->
update the just-created incident. Importing this module has no side effects.
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()  # read SNOW_* credentials from .env (never hardcoded)

INSTANCE = os.getenv("SNOW_INSTANCE", "https://dev389543.service-now.com").rstrip("/")

CLIENT_ID = os.getenv("SNOW_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SNOW_CLIENT_SECRET", "")

USERNAME = os.getenv("SNOW_USERNAME", "")
PASSWORD = os.getenv("SNOW_PASSWORD", "")

AUTH_MODE = os.getenv("SNOW_AUTH", "bearer").strip().lower()  # bearer | basic
GRANT_TYPE = os.getenv("SNOW_GRANT_TYPE", "password").strip().lower()  # password | client_credentials

CUSTOM_TABLE = os.getenv("SNOW_CUSTOM_TABLE", "u_snow_incident_table")

REQUEST_TIMEOUT = 30


def _redact(token: str) -> str:
    """Never print full tokens into logs (first 6 chars + length only)."""
    if not token:
        return "<empty>"
    return f"{token[:6]}... (len {len(token)})"


def _require_basic_creds() -> tuple[str, str]:
    if not USERNAME or not PASSWORD:
        raise SystemExit(
            "Missing SNOW_USERNAME / SNOW_PASSWORD. "
            "Add them to your local .env (never commit it) and retry."
        )
    return USERNAME, PASSWORD


def _require_client_creds() -> tuple[str, str]:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit(
            "Missing SNOW_CLIENT_ID / SNOW_CLIENT_SECRET. "
            "Copy .env.example to .env and fill in your ServiceNow OAuth credentials."
        )
    return CLIENT_ID, CLIENT_SECRET


# Get Token (password grant by default; client_credentials on request)
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
        payload = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    else:
        raise SystemExit(
            f"Unsupported SNOW_GRANT_TYPE {grant_type!r}. Use 'password' or 'client_credentials'."
        )

    response = requests.post(
        f"{INSTANCE}/oauth_token.do",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def make_auth(token: str = "") -> tuple[dict, tuple | None]:
    """Return (headers, basic_auth_tuple) for the configured SNOW_AUTH mode."""
    if AUTH_MODE == "basic":
        username, password = _require_basic_creds()
        return {"Accept": "application/json"}, (username, password)
    if AUTH_MODE != "bearer":
        raise SystemExit(f"Unsupported SNOW_AUTH {AUTH_MODE!r}. Use 'bearer' or 'basic'.")
    if not token:
        raise SystemExit("Bearer mode needs a token: call get_access_token() first.")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}, None


# Get Incidents
def get_incidents(token: str = ""):
    headers, basic = make_auth(token)
    params = {
        "sysparm_query": "active=true",
        "sysparm_fields": "sys_id,number,short_description,description,state,priority,urgency,impact,assigned_to,assignment_group,opened_at,updated_at",
    }

    response = requests.get(
        f"{INSTANCE}/api/now/table/incident",
        headers=headers,
        auth=basic,
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    print("HTTP Response for Incident Request: ", response.status_code)
    # print('Response.text: ', response.text)

    response.raise_for_status()
    incidents = response.json()['result']

    return incidents


def create_incident(token: str = ""):
    headers, basic = make_auth(token)
    headers["Content-Type"] = "application/json"
    response = requests.post(
        f"{INSTANCE}/api/now/table/incident",
        headers=headers,
        auth=basic,
        json={
            "short_description": "Test incident created through Python",
            "description": "This incident was created using the ServiceNow Table API.",
            "urgency": "2",
        },
        timeout=REQUEST_TIMEOUT,
    )

    print("Create HTTP status:", response.status_code)
    print("Create response:", response.text)

    response.raise_for_status()

    return response.json()["result"]


def _check_table_name(table: str) -> str:
    """Validate a ServiceNow table name to prevent URL injection."""
    import re

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table or ""):
        raise SystemExit(
            f"Invalid table name {table!r}. Expected e.g. u_snow_incident_table."
        )
    return table


def _display_value(value):
    """Unwrap {display_value, value} objects from sysparm_display_value=all."""
    if isinstance(value, dict):
        return str(value.get("display_value", value.get("value", "")))
    return "" if value is None else str(value)


# Get records from a custom table (default: u_snow_incident_table).
# Schema-agnostic: fetches all fields and summarizes each row from
# whichever id-like columns exist.
def get_custom_records(token: str = "", table: str = CUSTOM_TABLE, limit: int = 5):
    table = _check_table_name(table)
    headers, basic = make_auth(token)
    params = {
        "sysparm_limit": max(1, limit),
        "sysparm_display_value": "all",
    }

    response = requests.get(
        f"{INSTANCE}/api/now/table/{table}",
        headers=headers,
        auth=basic,
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    print(f"HTTP Response for {table} Request: ", response.status_code)

    response.raise_for_status()
    return response.json()["result"]


def summarize_custom_record(record: dict) -> str:
    """One-line summary using whichever id-like columns the row has."""
    ident = ""
    for key in ("number", "u_number", "u_ticket_id", "name", "u_name"):
        ident = _display_value(record.get(key)).strip()
        if ident:
            break
    desc = ""
    for key in ("short_description", "u_short_description", "u_summary", "description", "u_description"):
        desc = _display_value(record.get(key)).strip()
        if desc:
            break
    sys_id = _display_value(record.get("sys_id"))
    label = ident or "(no number/name)"
    if desc:
        label += f" | {desc[:80]}"
    return f"{label} | sys_id {sys_id}"


def update_incident( token: str = "", sys_id=None, priority=None, assignment_group=None, description=None, comments=None,):
    """
    Update an existing ServiceNow Incident.

    Parameters:
        token: OAuth access token (bearer mode; ignored for basic auth)

        sys_id: ServiceNow sys_id of the Incident

        priority:
            Incident priority.
            Examples: "1", "2", "3", "4", "5"

        assignment_group:
            ServiceNow Assignment Group sys_id.
            Example: "287ebd7da9fe198100f92cc8d1d2154e"

        description:
            New value for the Incident Description.

        comments:
            Additional comment to add to the Incident.

    Returns:
        ServiceNow Incident record returned by the API.
    """

    # Build only the fields that the caller wants to change.
    payload = {}

    if priority is not None:
        payload["priority"] = str(priority)

    if assignment_group is not None:
        payload["assignment_group"] = assignment_group

    if description is not None:
        payload["description"] = description

    if comments is not None:
        payload["comments"] = comments

    # Don't make an API request if there is nothing to update.
    if not payload:
        raise ValueError("At least one field must be provided for update.")

    headers, basic = make_auth(token)
    headers["Content-Type"] = "application/json"
    response = requests.patch(
        f"{INSTANCE}/api/now/table/incident/{sys_id}",
        headers=headers,
        auth=basic,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )

    print("Update HTTP status:", response.status_code)
    print("Update response:", response.text)

    response.raise_for_status()

    return response.json()["result"]


def main() -> None:
    print(f"Auth mode: {AUTH_MODE}", end="")
    if AUTH_MODE == "bearer":
        print(f" (grant: {GRANT_TYPE})")
    else:
        print(f" (user: {USERNAME or '<missing>'})")

    # Get Token (bearer mode only; basic mode authenticates per request)
    token = get_access_token() if AUTH_MODE == "bearer" else ""
    if token:
        print(f'Token: {_redact(token)}')

    # Get and print Incidents
    available_incidents = get_incidents(token)

    # Create Incidents
    created = create_incident(token)

    print("Incident number:", created["number"], "Incident sys_id:", created["sys_id"])

    # Get again the incidents and print them
    available_incidents = get_incidents(token)
    for row in available_incidents:
        print( 'Incident: ', row["number"], "|", row["sys_id"], "|", row["state"], "|", row["short_description"], "| ",  row["description"] , "|", row['priority'],  "|", row['urgency'],  "|", row['impact'], "|",  )


    # Update Priority on the incident we just created (never a stale hardcoded id)
    incident = update_incident( token, sys_id=created["sys_id"], priority="5",)
    print("Updated  the Incident number:", incident["number"], "Incident sys_id:", incident["sys_id"])

    # Query the custom table (default: u_snow_incident_table)
    print(f"--- Custom table: {CUSTOM_TABLE} ---")
    custom_rows = get_custom_records(token)
    print(f"Custom table rows returned: {len(custom_rows)}")
    for row in custom_rows:
        print("Custom record:", summarize_custom_record(row))


if __name__ == "__main__":
    main()
