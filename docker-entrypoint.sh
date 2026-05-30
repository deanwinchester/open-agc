#!/bin/bash

echo "[Entrypoint] Open-AGC container starting..."

# Phase 1: Auto-upgrade check (code-based, no Docker socket needed)
python -m core.auto_upgrade 2>&1 || echo "[Entrypoint] Auto-upgrade check failed, continuing..."

# Phase 2: Start the main application
echo "[Entrypoint] Starting uvicorn on port ${PORT:-8000}..."
exec xvfb-run -a -s "-screen 0 1280x800x24" \
    python -m uvicorn api.server:app --host 0.0.0.0 --port "${PORT:-8000}"
