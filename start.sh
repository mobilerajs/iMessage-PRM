#!/usr/bin/env bash
# Build the dataset (if needed) and serve the CRM locally.
#
#   ./start.sh           build if out/ is missing, then serve
#   ./start.sh --rebuild force a fresh build first
#
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"

# Building needs the local model deps (mlx-lm, numpy) — always use the venv's
# Python, never the system python3 (which lacks them).
if [[ "${1:-}" == "--rebuild" || ! -f out/people.json ]]; then
  if [[ ! -x "$PY" ]]; then
    echo "No .venv found. Create it first:"
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
  fi
  echo "Building dataset..."
  "$PY" build.py
fi

echo
if [[ -x "$PY" ]] && "$PY" -c "import flask, mlx_lm" 2>/dev/null; then
  PORT="${PORT:-8001}"
  echo "Serving iMessage PRM (live model server) at  http://localhost:${PORT}"
  echo "Press Ctrl-C to stop."
  echo
  PORT="$PORT" "$PY" server.py
else
  PORT="${PORT:-8000}"
  echo "Serving iMessage PRM (static — no live filters) at  http://localhost:${PORT}"
  echo "For live filters: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  echo
  python3 -m http.server "${PORT}"
fi
