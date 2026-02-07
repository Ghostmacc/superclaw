#!/usr/bin/env bash
# SuperClaw — Serve the Mission Control dashboard

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASH_DIR="$(dirname "$SCRIPT_DIR")/dashboard"

PORT="${1:-8000}"

echo "Serving Mission Control on http://localhost:$PORT/mission-control.html"
echo "(Also accessible from LAN via your machine IP)"
echo "Press Ctrl+C to stop."
echo ""

cd "$DASH_DIR"
python3 -m http.server "$PORT" --bind 0.0.0.0
