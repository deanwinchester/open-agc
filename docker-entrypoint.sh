#!/bin/bash

echo "[Entrypoint] Open-AGC container starting..."

# ── Auto-upgrade: download latest release from GitHub ──
CURRENT_VER=$(cat /app/VERSION | tr -d 'vV \n')
echo "[Entrypoint] Current version: $CURRENT_VER"

LATEST_VER=$(curl -sf https://api.github.com/repos/deanwinchester/open-agc/releases/latest \
    2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('tag_name','').lstrip('v'))" 2>/dev/null)

if [ -n "$LATEST_VER" ] && [ "$LATEST_VER" != "$CURRENT_VER" ]; then
    echo "[Entrypoint] ⬆️ Upgrade available: v$CURRENT_VER -> v$LATEST_VER"
    echo "[Entrypoint] Downloading v$LATEST_VER source code..."

    TARBALL_URL="https://github.com/deanwinchester/open-agc/archive/refs/tags/v$LATEST_VER.tar.gz"
    curl -sL "$TARBALL_URL" -o /tmp/upgrade.tar.gz
    if [ $? -eq 0 ] && [ -s /tmp/upgrade.tar.gz ]; then
        cd /tmp
        tar xzf upgrade.tar.gz
        EXTRACTED=$(find /tmp -maxdepth 1 -name "open-agc-*" -type d | head -1)

        if [ -n "$EXTRACTED" ]; then
            echo "[Entrypoint] Applying v$LATEST_VER..."
            # Copy code files (merge, not replace)
            for dir in core tools agent api plugins static skills; do
                if [ -d "$EXTRACTED/$dir" ]; then
                    cp -r "$EXTRACTED/$dir/" "/app/$dir/" 2>/dev/null || true
                fi
            done
            for file in main.py launcher.py gui_app.py package.json vite.config.mjs requirements.txt docker-entrypoint.sh; do
                if [ -f "$EXTRACTED/$file" ]; then
                    cp "$EXTRACTED/$file" "/app/$file" 2>/dev/null || true
                fi
            done
            cp "$EXTRACTED/VERSION" /app/VERSION 2>/dev/null || true

            # Update pip dependencies if requirements changed
            if [ -f "$EXTRACTED/requirements.txt" ]; then
                echo "[Entrypoint] Updating Python dependencies..."
                pip install --no-cache-dir -r "$EXTRACTED/requirements.txt" 2>&1 || true
            fi

            # Rebuild frontend (npm run build)
            if command -v npm >/dev/null 2>&1 && [ -f "/app/package.json" ]; then
                echo "[Entrypoint] Rebuilding frontend..."
                cd /app && npm install --no-audit --no-fund 2>&1 && npm run build 2>&1 || \
                    echo "[Entrypoint] ⚠️ Frontend build failed (npm may be outdated)"
            else
                echo "[Entrypoint] npm not available, frontend may be stale"
            fi

            echo "[Entrypoint] ✅ Upgrade to v$LATEST_VER applied successfully"
        else
            echo "[Entrypoint] ⚠️ Could not find extracted source"
        fi
        rm -rf /tmp/upgrade.tar.gz /tmp/open-agc-*
    else
        echo "[Entrypoint] ⚠️ Failed to download upgrade tarball"
    fi
else
    echo "[Entrypoint] ✅ Up to date (v$CURRENT_VER)"
fi

# ── Pre-flight checks ──
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

# Start uvicorn in background and wait for it
python -m uvicorn api.server:app --host 0.0.0.0 --port "$PORT" --log-level info &
UVICORN_PID=$!
echo "[Entrypoint] uvicorn PID: $UVICORN_PID"

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
