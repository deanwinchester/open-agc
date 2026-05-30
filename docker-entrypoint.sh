#!/bin/bash
# Don't use set -e — Phase 1 failures must not prevent Phase 2

# Phase 1: Auto-upgrade check (Docker deployments only)
echo "[Entrypoint] Starting..."
if [ -S /var/run/docker.sock ]; then
    python -m core.auto_upgrade 2>&1 || echo "[Entrypoint] Auto-upgrade check failed, continuing..."
else
    echo "[Entrypoint] Docker socket not found -- skipping auto-upgrade check"
fi

# Phase 2: Start the main application
echo "[Entrypoint] Starting uvicorn on port ${PORT:-8000}..."
exec xvfb-run -a -s "-screen 0 1280x800x24" \
    python -m uvicorn api.server:app --host 0.0.0.0 --port "${PORT:-8000}"
