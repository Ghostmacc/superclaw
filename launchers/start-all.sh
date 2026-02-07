#!/usr/bin/env bash
# SuperClaw — Start Everything
# Usage: ./launchers/start-all.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== SUPERCLAW START ==="
echo ""

# 0. Check for .env
ENV_FILE="$PROJECT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "!! .env file not found at $ENV_FILE"
    echo "   Run 'python3 setup.py' or copy .env.example to .env and fill in your values."
    echo "   At minimum, set POSTGRES_PASSWORD and POSTGRES_DSN."
    exit 1
fi
set -a; source "$ENV_FILE"; set +a

# 1. Docker stack
echo "[1/5] Starting Docker stack..."
if command -v docker &>/dev/null; then
    docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d
    echo "  Postgres:5432 Qdrant:6333 n8n:5678"

    # Wait for Docker services to be healthy before starting bridges
    echo "  Waiting for services..."
    for i in $(seq 1 30); do
        PG_OK=$(docker exec superclaw-postgres pg_isready -q 2>/dev/null && echo "1" || echo "0")
        QD_OK=$(curl -sf http://localhost:6333/collections >/dev/null 2>&1 && echo "1" || echo "0")
        if [ "$PG_OK" = "1" ] && [ "$QD_OK" = "1" ]; then
            echo "  Postgres + Qdrant healthy"
            break
        fi
        sleep 2
    done
else
    echo "  WARNING: Docker not found — skipping"
fi

# 2. Lazarus Bridge
echo "[2/5] Starting Lazarus Bridge..."
BRIDGE_DIR="$PROJECT_DIR/bridge"
if [ -f "$BRIDGE_DIR/venv/bin/python3" ]; then
    PYTHON="$BRIDGE_DIR/venv/bin/python3"
else
    PYTHON="python3"
fi

if [ -f "$BRIDGE_DIR/lazarus_bridge.py" ]; then
    $PYTHON "$BRIDGE_DIR/lazarus_bridge.py" &
    echo "  Lazarus Bridge → port 8888"
fi

# 3. Hermes Bridge
echo "[3/5] Starting Hermes Bridge..."
if [ -f "$BRIDGE_DIR/hermes_bridge.py" ]; then
    $PYTHON "$BRIDGE_DIR/hermes_bridge.py" &
    echo "  Hermes Bridge → port 8787"
fi

# 4. Dashboard
echo "[4/5] Starting Dashboard..."
DASH_DIR="$PROJECT_DIR/dashboard"
if [ -f "$DASH_DIR/mission-control.html" ]; then
    cd "$DASH_DIR"
    $PYTHON -m http.server 8000 --bind 0.0.0.0 &
    echo "  Dashboard → http://localhost:8000/mission-control.html"
    cd "$PROJECT_DIR"
fi

# 5. Dashboard data sync
echo "[5/5] Starting dashboard data sync..."
SYNC_SCRIPT="$DASH_DIR/sync-mission-data.py"
if [ -f "$SYNC_SCRIPT" ]; then
    $PYTHON "$SYNC_SCRIPT" --watch &
    echo "  Data sync → watching TASKS.json + sessions"
else
    echo "  WARNING: sync-mission-data.py not found — dashboard will show no data"
fi

echo ""
echo "=== ALL SERVICES STARTED ==="
echo ""
echo "  n8n:       http://localhost:5678"
echo "  Lazarus:   http://localhost:8888/health"
echo "  Hermes:    http://localhost:8787/api/v1/health"
echo "  Dashboard: http://localhost:8000/mission-control.html"
echo ""
echo "Press Ctrl+C to stop bridges and dashboard."
echo "(Docker services stay running — use 'docker compose down' to stop)"
wait
