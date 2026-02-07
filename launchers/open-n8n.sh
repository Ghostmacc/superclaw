#!/usr/bin/env bash
# SuperClaw — Open n8n dashboard in browser

PORT="${1:-5678}"
URL="http://localhost:$PORT"

echo "Opening n8n at $URL ..."

if command -v xdg-open &>/dev/null; then
    xdg-open "$URL"
elif command -v open &>/dev/null; then
    open "$URL"
else
    echo "Could not auto-open browser. Navigate to: $URL"
fi
