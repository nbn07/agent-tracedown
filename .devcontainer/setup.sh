#!/usr/bin/env bash
# Runs once when the dev container is created. Installs the Python SDK and server in
# editable mode, the TypeScript SDK and the web app, then builds the bundled web app
# so `tracedown up` serves the full UI.
set -euo pipefail
cd "$(dirname "$0")/.."

python -m pip install --upgrade pip
pip install -e ./sdk/python -e "./server[dev]"
(cd sdk/typescript && npm ci)
(cd web && npm ci && node scripts/build-static.mjs)

cat <<'MSG'

Tracedown dev environment is ready.
  pytest -q                         Python SDK and server tests (about 20 seconds)
  tracedown up --no-open            server, web app and demo runs on port 7070
  cd sdk/typescript && npm test     TypeScript SDK tests
  cd web && npm run dev             web app with live reload on port 3000 (needs the server on 7070)
Pick an issue: https://github.com/nbn07/agent-tracedown/labels/good%20first%20issue
MSG
