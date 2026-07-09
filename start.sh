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
for cmd in python3.11 python3.10 python3.9 python3 python; do
    if command -v "$cmd" &> /dev/null; then
        if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    # No suitable Python found — try installing via brew (macOS, user-local)
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
    if [ -z "$PYTHON" ]; then
        echo "================================================"
        echo " Python 3.9+ is required but not found."
        echo " Your system Python version is too old."
        echo ""
        echo " Options:"
        echo "   1. Use pyenv to install a newer Python:"
        echo "      curl https://pyenv.run | bash"
        echo "      pyenv install 3.12"
        echo "      pyenv local 3.12"
        echo "   2. Install Python 3.12 manually:"
        echo "      https://www.python.org/downloads/"
        echo "================================================"
        exit 1
    fi
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
    if ! command -v npm &> /dev/null; then
        echo "npm not found. Attempting to install Node.js..."
        if command -v brew &> /dev/null; then
            brew install node
        elif command -v apt-get &> /dev/null; then
            sudo apt-get install -y -qq nodejs npm
        elif command -v yum &> /dev/null; then
            sudo yum install -y -q nodejs npm
        elif command -v pacman &> /dev/null; then
            sudo pacman -S --noconfirm nodejs npm
        else
            echo "==============================================="
            echo " Cannot auto-install Node.js."
            echo " Please install Node.js manually, then run:"
            echo "   npm install && npm run build"
            echo "==============================================="
            exit 1
        fi
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
