import os
import requests
from dotenv import load_dotenv

load_dotenv()  # read SNOW_* credentials from .env (never hardcoded)

INSTANCE = os.getenv("SNOW_INSTANCE", "https://dev389543.service-now.com")

CLIENT_ID = os.getenv("SNOW_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SNOW_CLIENT_SECRET", "")

if not CLIENT_ID or not CLIENT_SECRET:
    raise SystemExit(
        "Missing SNOW_CLIENT_ID / SNOW_CLIENT_SECRET. "
        "Copy .env.example to .env and fill in your ServiceNow OAuth credentials."
    )

# Get Token
def get_access_token():
    response = requests.post(
        f"{INSTANCE}/oauth_token.do",
        data={
            "grant_type": "client_credentials",
        },
        auth=(CLIENT_ID, CLIENT_SECRET),
    )

    response.raise_for_status()

    return response.json()["access_token"]


# Get Incidents
def get_incidents(token):
    ACCESS_TOKEN = token
    headers = { "Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json", }
    params = {
        "sysparm_query": "active=true",
        "sysparm_fields": "sys_id,number,short_description,state,priority,opened_at",
    }

    params = { 
              "sysparm_query": "active=true", 
              "sysparm_fields": "sys_id,number,short_description,description,state,priority,urgency,impact,assigned_to,assignment_group,opened_at,updated_at", 
             },

    response = requests.get( f"{INSTANCE}/api/now/table/incident", headers=headers, ) #params=params, ) 
    response.raise_for_status()

    print("HTTP Response for Incident Request: ", response.status_code)
    # print('Response.text: ', response.text)

    response.raise_for_status()
    incidents = response.json()['result']

    return incidents


def create_incident(token):
    response = requests.post(
        f"{INSTANCE}/api/now/table/incident",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={
            "short_description": "Test incident created through Python",
            "description": "This incident was created using the ServiceNow Table API.",
            "urgency": "2",
        },
    )

    print("Create HTTP status:", response.status_code)
    print("Create response:", response.text)

    response.raise_for_status()

    return response.json()["result"]


import requests


def update_incident( token, sys_id, priority=None, assignment_group=None, description=None, comments=None,):
    """
    Update an existing ServiceNow Incident.

    Parameters:
        token: OAuth access token
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

    response = requests.patch(
        f"{INSTANCE}/api/now/table/incident/{sys_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=payload,
    )

    print("Update HTTP status:", response.status_code)
    print("Update response:", response.text)

    response.raise_for_status()

    return response.json()["result"]



# Get Token
token = get_access_token()

# Get and print Incidents
available_incidents = get_incidents(token)

# Create Incidents
incident = create_incident(token)

print("Incident number:", incident["number"], "Incident sys_id:", incident["sys_id"])

# Get again the incidents and print them
available_incidents = get_incidents(token)
for incident in available_incidents:
    print( 'Incident: ', incident["number"], "|", incident["sys_id"], "|", incident["state"], "|", incident["short_description"], "| ",  incident["description"] , "|", incident['priority'],  "|", incident['urgency'],  "|", incident['impact'], "|",  )


# Update Priority
incident = update_incident( token, sys_id="ff4c21c4735123002728660c4cf6a758", priority="5",)
print("Updated  the Incident number:", incident["number"], "Incident sys_id:", incident["sys_id"])

