#!/bin/bash
set -e

# Phase 1: Auto-upgrade check (Docker deployments only)
if [ -S /var/run/docker.sock ]; then
    python -m core.auto_upgrade
else
    echo "[Entrypoint] Docker socket not found -- skipping auto-upgrade check"
fi

# Phase 2: Start the main application
exec xvfb-run -a -s "-screen 0 1280x800x24" \
    python -m uvicorn api.server:app --host 0.0.0.0 --port "${PORT:-8000}"
