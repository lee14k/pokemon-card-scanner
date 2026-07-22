#!/bin/bash
# Runs the live-scan full local E2E acceptance check (live_scan_e2e_check.py):
# starts uvicorn, waits for /health, runs the driver, tails the server log,
# then tears the server down (before AND after, per repo machine-care rules).
set -uo pipefail
cd "$(dirname "$0")/../.."   # repo root

pkill -f "uvicorn app.main" 2>/dev/null
sleep 1

export PYTHONPATH=. DATABASE_URL=postgresql://pcs:pcs@localhost:5432/pcs \
  AUTH_SECRET=dev-secret-not-for-prod-pad-0123456789 \
  PHOTO_STORAGE_DIR=./var/pulls COOKIE_SECURE=false

nohup .venv/bin/uvicorn app.main:app --port 8000 >/tmp/live_scan_e2e_uvicorn.log 2>&1 &
echo "uvicorn pid=$!, waiting for /health..."

for i in $(seq 1 30); do
  if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/health | grep -q 200; then
    echo "health OK after ${i}s"
    break
  fi
  sleep 1
done

.venv/bin/python docs/acceptance/live_scan_e2e_check.py
DRIVER_EXIT=$?

echo "--- uvicorn log tail ---"
tail -n 40 /tmp/live_scan_e2e_uvicorn.log

pkill -f "uvicorn app.main" 2>/dev/null
sleep 1
echo "driver exit code: $DRIVER_EXIT"
exit $DRIVER_EXIT
