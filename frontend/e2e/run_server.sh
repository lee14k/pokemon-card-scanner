#!/usr/bin/env bash
# Builds the frontend, starts the PokéWallet stub, then the backend serving statics.
set -euo pipefail
cd "$(dirname "$0")/../.."

(cd frontend && npm run build)

.venv/bin/uvicorn tests.pokewallet_stub:app --host 127.0.0.1 --port 8901 &
STUB_PID=$!
trap 'kill $STUB_PID' EXIT

POKEWALLET_BASE_URL=http://127.0.0.1:8901 \
POKEWALLET_API_KEY=test-key \
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8900
