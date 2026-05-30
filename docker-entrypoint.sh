#!/bin/bash

echo "[Entrypoint] Open-AGC container starting..."

# Phase 1: Auto-upgrade check
python -m core.auto_upgrade 2>&1 || echo "[Entrypoint] Auto-upgrade check failed, continuing..."

# Phase 2: Pre-flight checks
echo "[Entrypoint] Python: $(python --version)"
echo "[Entrypoint] xvfb-run: $(which xvfb-run)"

# Verify the app can import before starting
if ! python -c "import api.server" 2>&1; then
    echo "[Entrypoint] FATAL: api.server import failed"
    while true; do sleep 3600; done
fi

echo "[Entrypoint] Starting uvicorn on port ${PORT:-8000}..."
exec xvfb-run -a -s "-screen 0 1280x800x24" \
    python -m uvicorn api.server:app --host 0.0.0.0 --port "${PORT:-8000}"
