# ============================================================
# Build args
# ============================================================
ARG BUILD_REGISTRY=
ARG PIP_INDEX_URL=
ARG APP_VERSION=0.0.0

# ============================================================
# Single-stage runtime image
# ============================================================
FROM ${BUILD_REGISTRY}python:3.10-slim

# ── Environment ──
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=8000 \
    APP_VERSION=${APP_VERSION}

# ── OCI labels ──
LABEL org.opencontainers.image.title="Open-AGC" \
      org.opencontainers.image.description="AI Agent Framework" \
      org.opencontainers.image.source="https://github.com/deanwinchester/open-agc" \
      org.opencontainers.image.version="${APP_VERSION}"

# ── System deps (xvfb for headless PyAutoGUI, browser deps, Docker CLI) ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    xvfb \
    libgl1 \
    libglib2.0-0t64 \
    libnss3 \
    libatk1.0-0t64 \
    libatk-bridge2.0-0t64 \
    libcups2t64 \
    libdbus-1-3 \
    libxcomposite1 \
    libxrandr2 \
    libgbm1 \
    libasound2t64 \
    x11-utils \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── VERSION file (baked into image) ──
COPY VERSION .

# ── Python dependencies (layer cache friendly) ──
COPY requirements.txt .
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url $PIP_INDEX_URL} -r requirements.txt

# ── Playwright browser binary ──
RUN playwright install --with-deps chromium

# ── Application source code only (no config, no secrets) ──
COPY core ./core
COPY tools ./tools
COPY agent ./agent
COPY api ./api
COPY skills ./skills
COPY plugins ./plugins
COPY static ./static
COPY main.py launcher.py gui_app.py ./

# ── docker-compose.yml for self-upgrade ──
COPY docker-compose.yml ./

# ── Entrypoint ──
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# ── Runtime config/workspace paths (must be volume-mounted) ──
VOLUME ["/app/data", "/app/workspace"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8000/api/plugins || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
