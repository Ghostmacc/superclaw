#!/usr/bin/env bash
# SuperClaw — Start both bridges (Lazarus + Hermes)

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SUPERCLAW_DIR="$(dirname "$SCRIPT_DIR")"
BRIDGE_DIR="$SUPERCLAW_DIR/bridge"

# Source .env if it exists (needed for POSTGRES_DSN, etc.)
ENV_FILE="$SUPERCLAW_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    set -a; source "$ENV_FILE"; set +a
else
    echo "!! .env not found at $ENV_FILE"
    echo "   Run 'python3 setup.py' or copy .env.example to .env"
    exit 1
fi

if [ -f "$BRIDGE_DIR/venv/bin/python3" ]; then
    PYTHON="$BRIDGE_DIR/venv/bin/python3"
else
    PYTHON="python3"
fi

echo "Starting Lazarus Bridge (port 8888)..."
$PYTHON "$BRIDGE_DIR/lazarus_bridge.py" &
LAZARUS_PID=$!

echo "Starting Hermes Bridge (port 8787)..."
$PYTHON "$BRIDGE_DIR/hermes_bridge.py" &
HERMES_PID=$!

echo ""
echo "Bridges running:"
echo "  Lazarus: http://localhost:8888/health (PID $LAZARUS_PID)"
echo "  Hermes:  http://localhost:8787/api/v1/health (PID $HERMES_PID)"
echo ""
echo "Press Ctrl+C to stop both."
wait
