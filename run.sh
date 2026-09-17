#!/bin/bash
# Start the ServiceNow FastAPI service on port 8090
cd "$(dirname "$0")"
exec venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8090 --reload
