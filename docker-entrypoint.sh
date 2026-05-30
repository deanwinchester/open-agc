#!/bin/bash

echo "[Entrypoint] Open-AGC container starting..."

# Phase 1: Auto-upgrade check
python -m core.auto_upgrade 2>&1 || echo "[Entrypoint] Auto-upgrade check failed, continuing..."

# Phase 2: Pre-flight checks
echo "[Entrypoint] Python: $(python --version)"
echo "[Entrypoint] xvfb-run: $(which xvfb-run)"

if ! python -c "import api.server" 2>&1; then
    echo "[Entrypoint] FATAL: api.server import failed"
    while true; do sleep 3600; done
fi

PORT="${PORT:-8000}"
echo "[Entrypoint] Starting uvicorn on port $PORT..."

# Start Xvfb in background if available (for headless GUI tools)
if command -v Xvfb >/dev/null 2>&1; then
    echo "[Entrypoint] Starting Xvfb display :99..."
    touch /root/.Xauthority
    Xvfb :99 -ac -screen 0 1280x800x24 &
    export DISPLAY=:99
    sleep 1
fi

# Start uvicorn in background so we can check if it's alive
python -m uvicorn api.server:app --host 0.0.0.0 --port "$PORT" --log-level info &
UVICORN_PID=$!
echo "[Entrypoint] uvicorn PID: $UVICORN_PID"

# Wait and check
for i in $(seq 1 30); do
    sleep 2
    if curl -sf http://localhost:$PORT/api/plugins > /dev/null 2>&1; then
        echo "[Entrypoint] ✅ uvicorn ready on port $PORT"
        wait $UVICORN_PID
        exit $?
    fi
    if ! kill -0 $UVICORN_PID 2>/dev/null; then
        echo "[Entrypoint] ❌ uvicorn (PID $UVICORN_PID) died"
        wait $UVICORN_PID
        echo "[Entrypoint] exit code: $?"
        exit 1
    fi
done

echo "[Entrypoint] ❌ uvicorn not ready after 60s"
wait $UVICORN_PID
exit 1
