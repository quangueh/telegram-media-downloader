#!/bin/sh
set -eu

if command -v node >/dev/null 2>&1 && [ -f /opt/bgutil/server/build/main.js ]; then
    BGUTIL_PORT="${BGUTIL_PORT:-4416}"
    node --max-old-space-size=64 /opt/bgutil/server/build/main.js --port "$BGUTIL_PORT" &
    BGUTIL_PID=$!
    trap 'kill "$BGUTIL_PID" 2>/dev/null || true' EXIT INT TERM
fi

set +e
python -u bot.py
STATUS=$?
set -e
if [ -n "${BGUTIL_PID:-}" ]; then
    kill "$BGUTIL_PID" 2>/dev/null || true
fi
exit "$STATUS"
