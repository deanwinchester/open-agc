#!/bin/bash

# Open-AGC startup script — auto-installs dependencies on macOS & Linux

set -e

cd "$(dirname "$0")"

echo "==================================="
echo "     Starting Open-AGC (Panda)     "
echo "==================================="

# ── 1. Python ────────────────────────────────────────────────
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &> /dev/null; then
        PYTHON="$cmd"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Python not found. Attempting to install Python 3.11+..."
    if command -v brew &> /dev/null; then
        brew install python@3.12
    elif command -v apt-get &> /dev/null; then
        sudo apt-get update -qq
        sudo apt-get install -y -qq python3.11 python3.11-venv python3.11-dev 2>/dev/null ||
        sudo apt-get install -y -qq python3 python3-pip python3-venv
    elif command -v yum &> /dev/null; then
        sudo yum install -y -q python3.11 python3.11-devel 2>/dev/null ||
        sudo yum install -y -q python3 python3-pip
    elif command -v pacman &> /dev/null; then
        sudo pacman -S --noconfirm python python-pip
    else
        echo "Cannot auto-install Python. Please install Python 3.9+ manually:"
        echo "  https://www.python.org/downloads/"
        exit 1
    fi
    # Re-check after install
    PYTHON=""
    for cmd in python3.11 python3 python; do
        if command -v "$cmd" &> /dev/null; then
            PYTHON="$cmd"
            break
        fi
    done
    if [ -z "$PYTHON" ]; then
        echo "Python was installed but not found in PATH. Please restart your terminal."
        exit 1
    fi
fi

echo "Using: $($PYTHON --version)"

# Check Python version (need 3.9+ for modern package support)
PYTHON_VER=$($PYTHON -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')")
if ! $PYTHON -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"; then
    echo "Python $PYTHON_VER is too old. Need 3.9+."
    echo "Attempting to install Python 3.11..."
    if command -v apt-get &> /dev/null; then
        sudo apt-get install -y -qq python3.11 python3.11-venv 2>/dev/null && PYTHON="python3.11"
    elif command -v yum &> /dev/null; then
        sudo yum install -y -q python3.11 2>/dev/null && PYTHON="python3.11"
    fi
    PYTHON_VER=$($PYTHON -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')")
    if ! $PYTHON -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"; then
        echo "Cannot install Python 3.9+. Please manually install Python 3.9 or later:"
        echo "  https://www.python.org/downloads/"
        exit 1
    fi
    echo "Now using Python $PYTHON_VER"
fi

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
