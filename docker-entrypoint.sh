#!/bin/bash

echo "[Entrypoint] Open-AGC container starting..."

# Phase 1: Auto-upgrade — restore persisted upgrade or check for new version
UPGRADE_DIR="/app/data/upgrade"
UPGRADE_VERSION_FILE="$UPGRADE_DIR/VERSION"

if [ -f "$UPGRADE_VERSION_FILE" ]; then
    PERSISTED_VER=$(cat "$UPGRADE_VERSION_FILE" | tr -d 'vV \n')
    IMAGE_VER=$(cat /app/VERSION | tr -d 'vV \n')
    echo "[Entrypoint] Found persisted upgrade: v$PERSISTED_VER (image: v$IMAGE_VER)"
    if [ "$PERSISTED_VER" != "$IMAGE_VER" ]; then
        echo "[Entrypoint] Restoring v$PERSISTED_VER from persistent storage..."
        for dir in core tools agent api skills plugins static; do
            if [ -d "$UPGRADE_DIR/$dir" ]; then
                cp -r "$UPGRADE_DIR/$dir/" "/app/$dir/" 2>/dev/null || true
            fi
        done
        for file in main.py launcher.py gui_app.py requirements.txt docker-entrypoint.sh; do
            if [ -f "$UPGRADE_DIR/$file" ]; then
                cp "$UPGRADE_DIR/$file" "/app/$file" 2>/dev/null || true
            fi
        done
        cp "$UPGRADE_VERSION_FILE" /app/VERSION
        echo "[Entrypoint] ✅ Restored v$PERSISTED_VER from persistent storage"
    else
        echo "[Entrypoint] Persisted version matches image, skipping restore"
    fi
fi

# Phase 2: Auto-upgrade check (GitHub)
echo "[Entrypoint] Checking for upgrades..."
python -m core.auto_upgrade 2>&1
UPGRADE_EXIT=$?
if [ $UPGRADE_EXIT -eq 0 ]; then
    echo "[Entrypoint] ✅ Upgrade check complete (up to date or not applicable)"
elif [ $UPGRADE_EXIT -eq 42 ]; then
    echo "[Entrypoint] ✅ Upgrade applied — code persisted to data/upgrade/"
    echo "[Entrypoint] Container will restart..."
fi

# Phase 3: Pre-flight checks
echo "[Entrypoint] Python: $(python --version)"
echo "[Entrypoint] xvfb-run: $(which xvfb-run)"

if ! python -c "import api.server" 2>&1; then
    echo "[Entrypoint] FATAL: api.server import failed"
    while true; do sleep 3600; done
fi

PORT="${PORT:-8000}"
echo "[Entrypoint] Starting uvicorn on port $PORT..."

# Clean Xvfb lock files from previous runs
rm -f /tmp/.X*-lock /tmp/.X11-unix/X*

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
