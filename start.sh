#!/bin/bash

# Open-AGC startup script — auto-installs dependencies on macOS & Linux

set -e

cd "$(dirname "$0")"

echo "==================================="
echo "     Starting Open-AGC (Panda)     "
echo "==================================="

# ── 1. Python ────────────────────────────────────────────────
# Search for a suitable Python (3.9+) — prefer higher versions first,
# since the system default (python3) may be too old (e.g. UOS has 3.7).
PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$cmd" &> /dev/null; then
        if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done

# Check for local .python/ (prebuilt binary)
if [ -z "$PYTHON" ] && [ -f ".python/bin/python3" ]; then
    if .python/bin/python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>/dev/null; then
        PYTHON=".python/bin/python3"
    fi
fi

if [ -z "$PYTHON" ]; then
    # macOS: use brew (user-local, no sudo)
    if command -v brew &> /dev/null; then
        echo "Python 3.9+ not found. Installing via brew..."
        brew install python@3.12
        for cmd in python3.12 python3.11 python3.10 python3; do
            if command -v "$cmd" &> /dev/null; then
                PYTHON="$cmd"
                break
            fi
        done
    fi
fi

if [ -z "$PYTHON" ]; then
    # Download prebuilt Python from python-build-standalone (no compile, no sudo)
    echo "Downloading prebuilt Python 3.12 to .python/..."
    mkdir -p .python
    PKG="cpython-3.12.13+20260623-x86_64-unknown-linux-gnu-install_only.tar.gz"
    URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260623/$PKG"
    if command -v curl &> /dev/null; then
        curl -fsSL "$URL" -o /tmp/python.tar.gz
    elif command -v wget &> /dev/null; then
        wget -q "$URL" -O /tmp/python.tar.gz
    else
        echo "Need curl or wget to download Python."
        exit 1
    fi
    tar -xzf /tmp/python.tar.gz -C .python --strip-components=1
    rm /tmp/python.tar.gz
    PYTHON=".python/bin/python3"
fi

if [ -z "$PYTHON" ] || ! $PYTHON -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>/dev/null; then
    echo "================================================"
    echo " Cannot find or install Python 3.9+."
    echo " Please install Python 3.9 or later manually:"
    echo "   https://www.python.org/downloads/"
    echo "================================================"
    exit 1
fi

echo "Using: $($PYTHON --version)"

# ── 2. Virtual environment ─────────────────────────────────
if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Creating one..."
    $PYTHON -m venv venv
fi

echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip inside venv
$PYTHON -m pip install --upgrade pip --quiet 2>/dev/null

# ── 3. Python dependencies ─────────────────────────────────
if [ -f "requirements.txt" ]; then
    echo "Installing Python dependencies..."
    $PYTHON -m pip install -r requirements.txt
fi

# ── 4. Node.js / frontend build ─────────────────────────────
if [ -d "static/dist" ] && [ -f "static/dist/open-agc.css" ] && [ -f "static/dist/open-agc.min.js" ]; then
    echo "Frontend assets found (static/dist/), skipping build."
else
    # Prefer local .node/ first, then check PATH
    if [ -f ".node/bin/npm" ]; then
        export PATH="$PWD/.node/bin:$PATH"
    elif ! command -v npm &> /dev/null; then
        echo "npm not found. Downloading portable Node.js to .node/..."
        mkdir -p .node
        if command -v curl &> /dev/null; then
            curl -fsSL https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz -o /tmp/node.tar.xz
        elif command -v wget &> /dev/null; then
            wget -q https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz -O /tmp/node.tar.xz
        else
            echo "Cannot download Node.js. Install manually: https://nodejs.org/"
            exit 1
        fi
        tar -xf /tmp/node.tar.xz -C .node --strip-components=1
        rm /tmp/node.tar.xz
        export PATH="$PWD/.node/bin:$PATH"
    fi

    if command -v npm &> /dev/null && [ -f "package.json" ]; then
        echo "Building frontend assets with Vite..."
        if [ ! -d "node_modules" ]; then
            echo "Installing Node.js dependencies..."
            npm install
        fi
        npm run build
        echo "Frontend build complete."
    fi
fi

# ── 5. Start server ────────────────────────────────────────
if [ -z "$PORT" ]; then
    if command -v lsof &> /dev/null; then
        if lsof -Pi :8000 -sTCP:LISTEN -t &>/dev/null ; then
            echo "Port 8000 is occupied, finding a free port..."
            PORT=$($PYTHON -c "import socket; s=socket.socket(); s.bind(('', 0)); print(s.getsockname()[1]); s.close()")
        else
            PORT=8000
        fi
    else
        PORT=8000
    fi
fi

echo "==================================="
echo "Open-AGC is running at:"
echo "http://localhost:$PORT"
echo "==================================="

$PYTHON -m uvicorn api.server:app --host 0.0.0.0 --port "$PORT"
